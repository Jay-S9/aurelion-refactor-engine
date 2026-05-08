"""
Aurelion Refactor Engine v7 - HTTP Server
Upgraded with authentication middleware, web UI serving, CSV export,
and security headers on all responses.

NEW IN v7:
  - AuthMiddleware integration (API key + Bearer token)
  - Rate limiting per IP
  - GET  /  and GET /web/  → serves web/index.html
  - GET  /history/export/csv → CSV export
  - Security headers on all responses
  - .env file loaded on startup
"""

from __future__ import annotations

import json
import sys
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse, parse_qs

VERSION = "7.0.1"
WEB_DIR = Path(__file__).parent.parent / "web"


def _make_handler(logger, history_manager, state_manager, auth_middleware):
    """Factory: returns a BaseHTTPRequestHandler subclass with injected deps."""

    class AurelionHandler(BaseHTTPRequestHandler):
        _logger  = logger
        _hm      = history_manager
        _sm      = state_manager
        _auth    = auth_middleware

        def do_GET(self):
            parsed = urlparse(self.path)
            path   = parsed.path.rstrip("/")

            # ── Auth check ─────────────────────────────────────
            ok, code, err = self._auth.check(self)
            if not ok:
                self._respond(code, {"error": err})
                return

            # ── Route ──────────────────────────────────────────
            if path in ("", "/", "/web"):
                self._serve_web_ui()
            elif path == "/status" or path == "/health":
                self._respond(200, self._status())
            elif path == "/history":
                qs    = parse_qs(parsed.query)
                limit = int(qs.get("limit", ["20"])[0])
                runs  = self._hm.list_runs(limit=limit) if self._hm else []
                self._respond(200, {"runs": runs, "count": len(runs)})
            elif path.startswith("/history/export/"):
                fmt = path[len("/history/export/"):]
                self._handle_export(fmt)
            elif path.startswith("/history/"):
                run_id = path[len("/history/"):]
                record = self._hm.get_run(run_id) if self._hm else None
                if record:
                    self._respond(200, record)
                else:
                    self._respond(404, {"error": f"Run not found: {run_id}"})
            elif path == "/profiles":
                from core.profile_manager import ProfileManager
                pm = ProfileManager(logger=self._logger)
                self._respond(200, {"profiles": pm.list()})
            elif path == "/plugins":
                from plugins.manager import MarketplaceManager
                mm = MarketplaceManager(self._logger)
                self._respond(200, {"plugins": [
                    {"name": p.name, "type": p.rule_type, "version": p.version,
                     "description": p.description, "enabled": p.enabled}
                    for p in mm.list()
                ]})
            else:
                self._respond(404, {"error": f"Not found: {self.path}"})

        def do_POST(self):
            parsed = urlparse(self.path)
            path   = parsed.path.rstrip("/")

            # ── Auth check ─────────────────────────────────────
            ok, code, err = self._auth.check(self)
            if not ok:
                self._respond(code, {"error": err})
                return

            # ── Body validation ────────────────────────────────
            ok, err = self._auth.validate_request_body(self)
            if not ok:
                self._respond(400, {"error": err})
                return

            body = self._read_body()

            if path == "/run":
                self._handle_run(body)
            elif path == "/preview":
                self._handle_preview(body)
            elif path == "/ai":
                self._handle_ai(body)
            elif path == "/auth/generate-key":
                self._handle_generate_key(body)
            else:
                self._respond(404, {"error": f"Not found: {self.path}"})

        def do_OPTIONS(self):
            self._send_cors()
            self.send_response(200)
            self.end_headers()

        # ── Route handlers ─────────────────────────────────────

        def _serve_web_ui(self):
            """Serve the web dashboard HTML file."""
            index = WEB_DIR / "index.html"
            if not index.exists():
                self._respond(404, {"error": "Web UI not found. Reinstall Aurelion."})
                return
            content = index.read_bytes()
            self.send_response(200)
            self._add_security_headers()
            self.send_header("Content-Type",   "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def _handle_run(self, body: Dict[str, Any]) -> None:
            plan_path = body.get("plan_path")
            if not plan_path:
                self._respond(400, {"error": "Missing plan_path"})
                return
            try:
                from core.api import run_plan
                result = run_plan(
                    plan_path,
                    dry_run=body.get("dry_run", False),
                    workers=body.get("workers"),
                    strict=body.get("strict", True),
                    group=body.get("group"),
                    tag=body.get("tag"),
                    export=body.get("export", False),
                    silent=True,
                )
                self._respond(200 if result.success else 207, {
                    "success":     result.success,
                    "summary":     result.summary,
                    "rules":       result.data.get("rules", []),
                    "performance": result.data.get("performance", {}),
                    "errors":      result.errors,
                })
            except Exception as e:
                self._respond(500, {"error": str(e), "trace": traceback.format_exc()[-500:]})

        def _handle_preview(self, body: Dict[str, Any]) -> None:
            plan_path = body.get("plan_path")
            if not plan_path:
                self._respond(400, {"error": "Missing plan_path"})
                return
            try:
                from core.api import preview_plan
                result = preview_plan(plan_path, group=body.get("group"), tag=body.get("tag"), silent=True)
                self._respond(200 if result.success else 400, result.data if result.success else {"error": result.errors})
            except Exception as e:
                self._respond(500, {"error": str(e)})

        def _handle_ai(self, body: Dict[str, Any]) -> None:
            prompt = body.get("prompt", "").strip()
            if not prompt:
                self._respond(400, {"error": "Missing prompt"})
                return
            try:
                from core.ai_planner import AIPlanner, AIPlannerError
                from pathlib import Path as _P
                planner = AIPlanner(self._logger)
                ctx     = _P(body["context_dir"]) if body.get("context_dir") else None
                plan    = planner.generate_plan_from_text(prompt, context_dir=ctx)
                toml    = planner._plan_to_toml(plan)
                self._respond(200, {
                    "success":     True,
                    "plan_name":   plan.name,
                    "plan_toml":   toml,
                    "rules_count": len(plan.rules),
                    "rules": [{"name": r.name, "type": r.rule_type, "target": r.target} for r in plan.rules],
                })
            except Exception as e:
                self._respond(500, {"success": False, "error": str(e)})

        def _handle_export(self, fmt: str) -> None:
            """Export history in requested format."""
            if fmt == "csv":
                try:
                    from core.db import get_db
                    import io, csv
                    db   = get_db(self._logger)
                    rows = db.fetchall(
                        "SELECT run_id, timestamp, command, plan_name, status, duration, dry_run "
                        "FROM runs ORDER BY timestamp DESC"
                    ) if db.available else []

                    if not rows:
                        # Fallback to history manager
                        runs = self._hm.list_runs(limit=999) if self._hm else []
                        rows = [{"run_id": r.get("run_id",""), "timestamp": r.get("timestamp",""),
                                 "command": r.get("command",""), "plan_name": r.get("plan_name",""),
                                 "status": r.get("status",""), "duration": r.get("duration",0),
                                 "dry_run": r.get("dry_run",False)} for r in runs]

                    buf = io.StringIO()
                    if rows:
                        writer = csv.DictWriter(buf, fieldnames=rows[0].keys())
                        writer.writeheader()
                        writer.writerows(rows)
                    content = buf.getvalue().encode("utf-8")

                    self.send_response(200)
                    self._send_cors()
                    self._add_security_headers()
                    self.send_header("Content-Type",        "text/csv; charset=utf-8")
                    self.send_header("Content-Disposition", "attachment; filename=aurelion_history.csv")
                    self.send_header("Content-Length",      str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as e:
                    self._respond(500, {"error": str(e)})
            else:
                self._respond(400, {"error": f"Unknown export format: {fmt}"})

        def _handle_generate_key(self, body: Dict[str, Any]) -> None:
            from core.auth import generate_api_key
            prefix = body.get("prefix", "aur")
            key    = generate_api_key(prefix)
            self._respond(200, {"api_key": key, "note": "Add to .env as AURELION_API_KEY"})

        # ── Response helpers ───────────────────────────────────

        def _respond(self, code: int, data: Dict[str, Any]) -> None:
            body = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self._send_cors()
            self._add_security_headers()
            self.send_header("Content-Type",   "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Aurelion-Version", VERSION)
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return {}

        def _send_cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin",  "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")

        def _add_security_headers(self) -> None:
            for k, v in self._auth.security_headers().items():
                self.send_header(k, v)

        def _status(self) -> Dict[str, Any]:
            from datetime import datetime
            return {
                "status":       "ok",
                "version":      VERSION,
                "time":         datetime.utcnow().isoformat() + "Z",
                "auth_enabled": self._auth.config.auth_enabled,
                "rate_limit":   self._auth.config.rate_limit,
                "endpoints": [
                    "GET  /", "GET  /status", "GET  /health",
                    "GET  /history[?limit=N]", "GET  /history/{run_id}",
                    "GET  /history/export/csv",
                    "GET  /profiles", "GET  /plugins",
                    "POST /run", "POST /preview", "POST /ai",
                    "POST /auth/generate-key",
                ],
            }

        def log_message(self, fmt: str, *args) -> None:
            if self._logger:
                self._logger.info(f"  [HTTP] {fmt % args}")

    return AurelionHandler


# ── Server class ───────────────────────────────────────────────────────────────

class AurelionServer:
    """v7 HTTP server with auth, rate limiting, web UI, CSV export."""

    def __init__(
        self,
        host:    str = "127.0.0.1",
        port:    int = 7070,
        logger=None,
    ):
        self.host    = host
        self.port    = port
        self._logger = logger

    def start(self) -> None:
        from core.auth import AuthMiddleware, AuthConfig, _load_dotenv
        from core.history_manager import HistoryManager
        from utils.state_manager import StateManager

        _load_dotenv()

        hm      = HistoryManager(self._logger)
        sm      = StateManager(self._logger)
        auth    = AuthMiddleware(AuthConfig())
        handler = _make_handler(self._logger, hm, sm, auth)
        server  = HTTPServer((self.host, self.port), handler)

        if self._logger:
            self._logger.section(f"AURELION SERVER  v{VERSION}")
            self._logger.success(f"  Listening on  http://{self.host}:{self.port}")
            self._logger.success(f"  Web dashboard http://{self.host}:{self.port}/")
            self._logger.info(f"  Auth          {'ENABLED' if auth.config.auth_enabled else 'DISABLED (set AURELION_API_KEY to enable)'}")
            self._logger.info(f"  Rate limit    {auth.config.rate_limit} req/min per IP")
            self._logger.info("  Press Ctrl+C to stop.")

        try:
            server.serve_forever()
        except KeyboardInterrupt:
            if self._logger:
                self._logger.warning("\nServer stopped.")
            server.server_close()
