"""
Aurelion Refactor Engine v4 - Dependency Resolver
Builds a Directed Acyclic Graph (DAG) from rule dependencies,
topologically sorts execution order, and detects circular references.

Design:
  DependencyResolver.resolve(rules) → List[RuleBase]
    ├─ Build adjacency list from depends_on fields
    ├─ Detect cycles via DFS with three-colour marking
    ├─ Topological sort (Kahn's algorithm for stable ordering)
    └─ Return rules in safe execution order

NEW IN v4:
  - depends_on field on RuleBase (List[str] of rule names)
  - DependencyResolver class with full DAG analysis
  - DependencyError with rich cycle path reporting
  - visualize_graph() for text-based DAG rendering
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from engines.rule_engine import RuleBase


# ── Exception ─────────────────────────────────────────────────────────────────

class DependencyError(Exception):
    """Raised when the dependency graph has cycles or references unknown rules."""

    def __init__(self, message: str, cycles: Optional[List[List[str]]] = None):
        self.cycles = cycles or []
        super().__init__(message)


# ── Dependency Resolver ───────────────────────────────────────────────────────

class DependencyResolver:
    """
    Analyses rule dependency declarations, validates the graph,
    and returns rules in a safe topological execution order.

    Usage:
        resolver = DependencyResolver(logger)
        ordered  = resolver.resolve(plan.enabled_rules)
    """

    def __init__(self, logger=None):
        self._logger = logger

    def resolve(self, rules: List[RuleBase]) -> List[RuleBase]:
        """
        Validate and topologically sort rules by their depends_on relationships.

        Returns:
            Rules in valid execution order.

        Raises:
            DependencyError: if cycles exist or unknown rule names are referenced.
        """
        if not rules:
            return rules

        rule_map: Dict[str, RuleBase] = {r.name: r for r in rules}
        errors: List[str] = []

        # ── 1. Validate all depends_on references ─────────────────
        for rule in rules:
            deps = getattr(rule, "depends_on", []) or []
            for dep in deps:
                if dep not in rule_map:
                    errors.append(
                        f"Rule '{rule.name}' depends on '{dep}' "
                        f"which does not exist in this plan."
                    )
                if dep == rule.name:
                    errors.append(f"Rule '{rule.name}' cannot depend on itself.")

        if errors:
            raise DependencyError(
                f"Dependency validation failed:\n" +
                "\n".join(f"  • {e}" for e in errors)
            )

        # ── 2. Build adjacency list ────────────────────────────────
        # adj[A] = set of rules that must run AFTER A
        # in_degree[B] = number of rules B depends on
        adj: Dict[str, Set[str]]     = defaultdict(set)
        in_degree: Dict[str, int]    = {r.name: 0 for r in rules}

        for rule in rules:
            deps = getattr(rule, "depends_on", []) or []
            for dep in deps:
                if rule.name not in adj[dep]:
                    adj[dep].add(rule.name)
                    in_degree[rule.name] += 1

        # ── 3. Cycle detection + topological sort (Kahn's algorithm)
        # Start with all rules that have no dependencies
        queue: deque[str] = deque(
            sorted(name for name, deg in in_degree.items() if deg == 0)
        )
        ordered_names: List[str] = []

        while queue:
            current = queue.popleft()
            ordered_names.append(current)

            # Reduce in-degree for all successors
            for successor in sorted(adj[current]):
                in_degree[successor] -= 1
                if in_degree[successor] == 0:
                    queue.append(successor)

        # ── 4. Cycle detection ────────────────────────────────────
        if len(ordered_names) != len(rules):
            # Some rules were never enqueued → they are part of cycles
            cyclic = {r.name for r in rules} - set(ordered_names)
            cycles = self._find_cycles(cyclic, adj)
            cycle_strs = [" → ".join(c) + f" → {c[0]}" for c in cycles]
            raise DependencyError(
                f"Circular dependency detected among rules: "
                f"{', '.join(sorted(cyclic))}\n"
                + "\n".join(f"  Cycle: {c}" for c in cycle_strs),
                cycles=cycles,
            )

        # ── 5. Map names back to Rule objects ─────────────────────
        ordered = [rule_map[name] for name in ordered_names]

        # Log reordering if it changed anything
        if self._logger:
            original_names = [r.name for r in rules]
            if original_names != ordered_names:
                self._logger.info("  [DEP] Execution order adjusted for dependencies:")
                for i, name in enumerate(ordered_names):
                    orig_pos = original_names.index(name) + 1
                    new_pos  = i + 1
                    marker   = "  →" if orig_pos != new_pos else "   "
                    self._logger.info(
                        f"  {marker} [{new_pos}] {name}"
                        + (f"  (was [{orig_pos}])" if orig_pos != new_pos else "")
                    )

        return ordered

    def build_graph_summary(self, rules: List[RuleBase]) -> List[str]:
        """
        Return a list of strings representing the dependency graph.
        Used by plan visualizer for text-based graph output.
        """
        lines: List[str] = []
        rule_map = {r.name: r for r in rules}

        for rule in rules:
            deps = getattr(rule, "depends_on", []) or []
            if deps:
                dep_str = ", ".join(deps)
                lines.append(f"  {rule.name}  ←  [{dep_str}]")
            else:
                lines.append(f"  {rule.name}  (no dependencies)")

        return lines

    def visualize_graph(self, rules: List[RuleBase]) -> str:
        """
        Render a compact text-based dependency graph.

        Example output:
          ┌─ setup-env
          ├─ update-api  ←depends on─ setup-env
          └─ bump-version ←depends on─ update-api
        """
        if not rules:
            return "  (empty)"

        lines: List[str] = []
        total = len(rules)

        for i, rule in enumerate(rules):
            is_last = (i == total - 1)
            prefix  = "  └─ " if is_last else "  ├─ "
            deps    = getattr(rule, "depends_on", []) or []

            if deps:
                dep_str = ", ".join(deps)
                lines.append(f"{prefix}{rule.name}  ⟵  {dep_str}")
            else:
                lines.append(f"{prefix}{rule.name}")

        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────
    # Internal
    # ──────────────────────────────────────────────────────────────

    def _find_cycles(
        self,
        cyclic_names: Set[str],
        adj: Dict[str, Set[str]],
    ) -> List[List[str]]:
        """
        DFS cycle finder limited to the cyclic subgraph.
        Returns a list of cycles (each cycle is a list of rule names).
        """
        visited:     Set[str] = set()
        rec_stack:   Set[str] = set()
        cycles:      List[List[str]] = []
        parent_path: List[str] = []

        def dfs(node: str) -> None:
            visited.add(node)
            rec_stack.add(node)
            parent_path.append(node)

            for neighbour in sorted(adj.get(node, set())):
                if neighbour not in cyclic_names:
                    continue
                if neighbour not in visited:
                    dfs(neighbour)
                elif neighbour in rec_stack:
                    # Found a cycle — extract the cycle path
                    cycle_start = parent_path.index(neighbour)
                    cycle = parent_path[cycle_start:]
                    cycles.append(cycle[:])

            parent_path.pop()
            rec_stack.discard(node)

        for name in sorted(cyclic_names):
            if name not in visited:
                dfs(name)

        return cycles
