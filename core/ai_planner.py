"""
Aurelion Refactor Engine v6 - AI Planner
Converts natural language instructions into validated Aurelion plan objects
using the Anthropic Claude API with structured JSON output.

Design principles:
  - The LLM is ONLY a translator: NL → structured JSON → Plan
  - All output is validated through the existing rule_parser pipeline
  - The planner never writes files directly; it returns Plan objects
  - Structured system prompt enforces strict JSON output
  - Falls back gracefully when the API is unavailable

Flow:
  generate_plan_from_text(prompt) → Plan
    ├─ Build structured system prompt with schema
    ├─ Call Claude API with JSON-mode instructions
    ├─ Parse and validate structured response
    ├─ Feed validated JSON through existing _build_plan()
    └─ Return validated Plan object (or raise AIPlannnerError)

NEW IN v6:
  - AIPlannerError with LLM response attached
  - generate_plan_from_text(prompt, context_dir) → Plan
  - generate_toml_from_text(prompt) → str (raw TOML for saving)
  - explain_plan(plan) → str (human-readable plan explanation)
  - suggest_improvements(plan) → list[str]
"""

from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Exception ─────────────────────────────────────────────────────────────────

class AIPlannerError(Exception):
    """Raised when AI plan generation fails."""
    def __init__(self, message: str, raw_response: str = ""):
        super().__init__(message)
        self.raw_response = raw_response


# ── System prompt ──────────────────────────────────────────────────────────────

_SCHEMA_DESCRIPTION = """
You are Aurelion Plan Compiler — a specialist that converts natural language
refactoring instructions into structured Aurelion plan configurations.

RULE TYPES:
  replace       — text search-and-replace across files matching a glob
  replace_file  — copy a source file over matching target files
  inject        — prepend/append/replace a template into matching files

GLOB PATTERNS:
  **/*.py        all Python files recursively
  src/**/*.ts    TypeScript files under src/
  **/*.md        all markdown files
  **/config.*    any config file

REQUIRED FIELDS PER RULE TYPE:
  replace:       name, type, find, replace, target
  replace_file:  name, type, source, target
  inject:        name, type, source, target, mode (replace|prepend|append)

OPTIONAL FIELDS:
  case_insensitive: true/false  (default false)
  enabled: true/false           (default true)
  group: string                 (logical grouping)
  tags: [list of strings]
  depends_on: [list of rule names]
  workers: integer              (parallelism)
  exclude_dirs: [list of dirs]

OUTPUT FORMAT — respond ONLY with valid JSON, no markdown, no explanation:
{
  "plan": {"name": "Plan Name Here"},
  "defaults": {"encoding": "utf-8", "workers": 1},
  "rules": [
    {
      "name": "unique-kebab-case-name",
      "type": "replace",
      "find": "exact text to find",
      "replace": "replacement text",
      "target": "**/*.py",
      "group": "optional-group"
    }
  ]
}

RULES:
- Rule names must be unique, lowercase, kebab-case
- Be specific about file targets — use extensions when possible
- If the instruction is ambiguous, infer the most sensible defaults
- Never generate rules that delete files
- Always validate that find text differs from replace text
"""


# ── AI Planner ─────────────────────────────────────────────────────────────────

class AIPlanner:
    """
    Translates natural language refactoring instructions into Aurelion plans
    using the Claude API with structured JSON output.
    """

    MODEL = "claude-sonnet-4-20250514"
    MAX_TOKENS = 2000
    TEMPERATURE = 0.1     # low temperature for deterministic structured output

    def __init__(self, logger=None):
        self._logger = logger

    # ──────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────

    def generate_plan_from_text(
        self,
        prompt: str,
        context_dir: Optional[Path] = None,
        plan_name: Optional[str] = None,
    ):
        """
        Convert a natural language instruction into a validated Plan object.

        Args:
            prompt:      The natural language instruction.
            context_dir: Optional project directory for context hints.
            plan_name:   Optional override for the plan name.

        Returns:
            A validated Plan object ready for execution.

        Raises:
            AIPlannerError: if the LLM response cannot be parsed or validated.
        """
        from utils.rule_parser import _build_plan, PlanValidationError

        if self._logger:
            self._logger.info("  [AI] Generating plan from prompt...")

        # Enrich prompt with directory context if available
        enriched = self._enrich_prompt(prompt, context_dir)

        # Call the API
        raw_json = self._call_api(enriched)

        # Parse the JSON
        raw_dict = self._parse_json_response(raw_json)

        # Override plan name if specified
        if plan_name:
            raw_dict.setdefault("plan", {})["name"] = plan_name

        # Build a synthetic source path for _build_plan
        source_path = context_dir / "__ai_generated__.toml" if context_dir else Path("ai_plan.toml")

        # Feed through existing validated pipeline
        try:
            plan = _build_plan(raw_dict, source_path)
        except PlanValidationError as e:
            try:
                from core.db import get_db
                db = get_db(self._logger)
                if db.available:
                    db.insert_ai_prompt(
                        prompt=prompt, response=raw_json[:2000],
                        status="validation_failed",
                    )
            except Exception:
                pass
            raise AIPlannerError(
                f"AI-generated plan failed validation:\n{e}",
                raw_response=raw_json,
            ) from e

        # Compute scores
        confidence_score   = self._compute_confidence(plan, raw_json)
        validation_score   = 1.0 if len(plan.rules) > 0 else 0.0

        if self._logger:
            self._logger.success(
                f"  [AI] Generated plan '{plan.name}' — "
                f"{len(plan.rules)} rule(s) | "
                f"confidence={confidence_score:.0%} | "
                f"validation={validation_score:.0%}"
            )

        # Store prompt + response in DB
        try:
            from core.db import get_db
            db = get_db(self._logger)
            if db.available:
                db.insert_ai_prompt(
                    prompt=prompt,
                    response=raw_json[:2000],
                    plan_name=plan.name,
                    rules_count=len(plan.rules),
                    confidence=confidence_score,
                    validation_score=validation_score,
                    status="success",
                )
        except Exception:
            pass

        return plan

    def generate_toml_from_text(
        self,
        prompt: str,
        context_dir: Optional[Path] = None,
    ) -> str:
        """
        Generate a plan and return it as a TOML string (for saving to disk).
        Useful for `aurelion ai "..." --save plan.toml`
        """
        plan = self.generate_plan_from_text(prompt, context_dir)
        return self._plan_to_toml(plan)

    def explain_plan(self, plan) -> str:
        """
        Ask the AI to produce a human-readable explanation of what a plan does.
        Returns a plain-text summary string.
        """
        rules_desc = "\n".join(
            f"  - [{r.rule_type}] '{r.name}': {self._rule_summary(r)}"
            for r in plan.rules
        )
        prompt = (
            f"Explain in plain English what this Aurelion refactoring plan does.\n"
            f"Plan name: {plan.name}\n"
            f"Rules:\n{rules_desc}\n"
            f"Keep the explanation under 100 words. Be concrete and specific."
        )
        try:
            return self._call_api_text(prompt)
        except Exception as e:
            return f"[AI explanation unavailable: {e}]"

    def suggest_improvements(self, plan) -> List[str]:
        """
        Ask the AI to suggest improvements or flag risks in the plan.
        Returns a list of suggestion strings.
        """
        rules_json = json.dumps(
            [{"name": r.name, "type": r.rule_type, "target": r.target} for r in plan.rules],
            indent=2
        )
        prompt = (
            f"Review this Aurelion refactoring plan and suggest improvements "
            f"or flag potential risks. Return a JSON array of short strings "
            f"(each under 20 words). No markdown, just the JSON array.\n\n"
            f"Rules:\n{rules_json}"
        )
        try:
            raw = self._call_api(prompt)
            suggestions = json.loads(raw.strip())
            if isinstance(suggestions, list):
                return [str(s) for s in suggestions[:8]]
        except Exception:
            pass
        return ["[AI suggestions unavailable]"]

    def analyze_diff_risk(
        self,
        matches: List[Dict[str, Any]],
        rule_name: str,
        find: str,
        replace: str,
    ) -> Dict[str, Any]:
        """
        Analyze diff matches for potential risks using AI.
        Returns {risk_level, summary, risky_files}.
        """
        if not matches:
            return {"risk_level": "none", "summary": "No matches to analyze.", "risky_files": []}

        # Build a compact representation
        sample = []
        for m in matches[:5]:
            for ctx in (m.get("context") or [])[:2]:
                sample.append(f"  File: {m['file']}, L{ctx['line']}: {ctx['old']!r} → {ctx['new']!r}")

        sample_text = "\n".join(sample)
        prompt = (
            f"Analyze these planned text changes for refactoring risks.\n"
            f"Operation: replace '{find}' with '{replace}' (rule: {rule_name})\n"
            f"Sample changes:\n{sample_text}\n\n"
            f"Return JSON only: "
            f'{"{"}"risk_level": "low|medium|high", "summary": "one sentence", '
            f'"risky_files": ["list of concerning file paths"]{"}"}'
        )
        try:
            raw  = self._call_api(prompt)
            data = json.loads(self._extract_json(raw))
            return data
        except Exception:
            return {"risk_level": "unknown", "summary": "Analysis unavailable.", "risky_files": []}

    # ──────────────────────────────────────────────────────────────
    # Internal: API calls
    # ──────────────────────────────────────────────────────────────

    def _call_api(self, user_message: str) -> str:
        """
        Call Claude API expecting structured JSON output.
        Returns the raw string content of the response.
        """
        import urllib.request

        payload = json.dumps({
            "model":       self.MODEL,
            "max_tokens":  self.MAX_TOKENS,
            "system":      _SCHEMA_DESCRIPTION,
            "messages":    [{"role": "user", "content": user_message}],
        }).encode("utf-8")

        import os
        headers = {
            "content-type":      "application/json",
            "anthropic-version": "2023-06-01",
        }
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            headers["x-api-key"] = api_key

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            raise AIPlannerError(
                f"API request failed (HTTP {e.code}): {body_text[:300]}"
            )
        except Exception as e:
            raise AIPlannerError(f"API request failed: {e}")

        # Extract text content
        content = body.get("content", [])
        for block in content:
            if block.get("type") == "text":
                return block["text"].strip()

        raise AIPlannerError("API returned no text content.", raw_response=str(body))

    def _call_api_text(self, prompt: str) -> str:
        """Call API without a schema system prompt (for free-text responses)."""
        import urllib.request

        payload = json.dumps({
            "model":      self.MODEL,
            "max_tokens": 500,
            "messages":   [{"role": "user", "content": prompt}],
        }).encode("utf-8")

        import os
        headers = {
            "content-type":      "application/json",
            "anthropic-version": "2023-06-01",
        }
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            headers["x-api-key"] = api_key

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise AIPlannerError(f"API request failed: {e}")

        content = body.get("content", [])
        for block in content:
            if block.get("type") == "text":
                return block["text"].strip()
        return ""

    # ──────────────────────────────────────────────────────────────
    # Internal: helpers
    # ──────────────────────────────────────────────────────────────

    def _enrich_prompt(self, prompt: str, context_dir: Optional[Path]) -> str:
        """Add directory context to the user prompt if available."""
        if not context_dir or not context_dir.exists():
            return prompt

        # Collect file extensions present in the project
        extensions: Dict[str, int] = {}
        try:
            for p in context_dir.rglob("*"):
                if p.is_file() and p.suffix and not any(
                    part.startswith(".") for part in p.parts
                ):
                    extensions[p.suffix] = extensions.get(p.suffix, 0) + 1
        except Exception:
            pass

        if extensions:
            top_ext = sorted(extensions.items(), key=lambda x: -x[1])[:6]
            ext_str = ", ".join(f"{e}({n})" for e, n in top_ext)
            return (
                f"{prompt}\n\n"
                f"[Project context: directory='{context_dir}', "
                f"top extensions: {ext_str}]"
            )
        return prompt

    def _parse_json_response(self, raw: str) -> Dict[str, Any]:
        """
        Extract and parse JSON from the LLM response.
        Handles cases where the model wraps JSON in code fences.
        """
        cleaned = self._extract_json(raw)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise AIPlannerError(
                f"AI response is not valid JSON: {e}\nResponse:\n{raw[:500]}",
                raw_response=raw,
            )

        if not isinstance(data, dict):
            raise AIPlannerError(
                f"AI response must be a JSON object, got: {type(data).__name__}",
                raw_response=raw,
            )

        if "rules" not in data or not isinstance(data["rules"], list):
            raise AIPlannerError(
                "AI response missing 'rules' array.",
                raw_response=raw,
            )

        return data

    def _extract_json(self, text: str) -> str:
        """Strip markdown code fences if present, then find the JSON object."""
        # Remove ```json ... ``` fences
        text = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()

        # Find the outermost { ... } block
        start = text.find("{")
        if start == -1:
            return text

        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

        return text[start:]

    def _compute_confidence(self, plan, raw_json: str) -> float:
        """
        Heuristic confidence score (0.0–1.0) for a generated plan.
        Based on: number of rules, rule completeness, JSON validity.
        """
        score = 0.0
        if not plan or not plan.rules:
            return 0.0

        score += 0.3   # Base: JSON parsed and plan built
        # Each complete rule adds up to 0.5 total
        per_rule = 0.5 / max(len(plan.rules), 1)
        for rule in plan.rules:
            if rule.name and rule.rule_type and rule.target:
                score += per_rule
            if hasattr(rule, "find") and rule.find:
                score += per_rule * 0.3
        score += 0.2 if len(plan.rules) >= 1 else 0.0
        return min(round(score, 2), 1.0)

    def _plan_to_toml(self, plan) -> str:
        """Convert a Plan object back to TOML format (for saving to disk)."""
        lines = [
            "# Generated by Aurelion AI Planner",
            f"# {plan.name}",
            "",
            "[plan]",
            f'name = "{plan.name}"',
            "",
            "[defaults]",
            'encoding = "utf-8"',
            "workers  = 1",
            "",
        ]
        for rule in plan.rules:
            lines.append("[[rules]]")
            lines.append(f'name = "{rule.name}"')
            lines.append(f'type = "{rule.rule_type}"')
            if hasattr(rule, "find"):
                lines.append(f'find    = "{rule.find}"')
                lines.append(f'replace = "{rule.replace}"')
            if hasattr(rule, "source"):
                lines.append(f'source = "{rule.source}"')
            if hasattr(rule, "mode"):
                lines.append(f'mode = "{rule.mode}"')
            lines.append(f'target = "{rule.target}"')
            if rule.group:
                lines.append(f'group = "{rule.group}"')
            if getattr(rule, "case_insensitive", False):
                lines.append("case_insensitive = true")
            if getattr(rule, "depends_on", []):
                deps = json.dumps(rule.depends_on)
                lines.append(f"depends_on = {deps}")
            lines.append("")
        return "\n".join(lines)

    def _rule_summary(self, rule) -> str:
        if rule.rule_type == "replace":
            return f"replace '{getattr(rule, 'find', '?')}' → '{getattr(rule, 'replace', '?')}' in {rule.target}"
        elif rule.rule_type == "replace_file":
            return f"copy '{getattr(rule, 'source', '?')}' to {rule.target}"
        elif rule.rule_type == "inject":
            return f"inject '{getattr(rule, 'source', '?')}' ({getattr(rule, 'mode', 'replace')}) into {rule.target}"
        return f"type={rule.rule_type}, target={rule.target}"


# ── Module-level convenience functions ────────────────────────────────────────

def generate_plan_from_text(
    prompt: str,
    context_dir: Optional[Path] = None,
    plan_name: Optional[str] = None,
    logger=None,
):
    """Module-level wrapper for AIPlanner.generate_plan_from_text."""
    return AIPlanner(logger).generate_plan_from_text(prompt, context_dir, plan_name)


def generate_toml_from_text(prompt: str, context_dir: Optional[Path] = None, logger=None) -> str:
    """Module-level wrapper for AIPlanner.generate_toml_from_text."""
    return AIPlanner(logger).generate_toml_from_text(prompt, context_dir)
