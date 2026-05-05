# -*- coding: utf-8 -*-
"""HTTP hook: one Gmail poll per request. See main.py --scan-hook."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

_lock = threading.Lock()
_running = False


def _run_scan_once(force_rescan: bool) -> None:
    import main as main_mod

    main_mod.run_gmail_loop(once=True, force_rescan=force_rescan)


class _Handler(BaseHTTPRequestHandler):
    server_version = "FundScanHook/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _secret_ok(self) -> bool:
        secret = (os.getenv("SCAN_WEBHOOK_SECRET") or "").strip()
        if not secret:
            return False
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip() == secret
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        tok = (qs.get("token") or [""])[0]
        return tok == secret

    def _force_rescan(self) -> bool:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        return (qs.get("rescan") or [""])[0].lower() in ("1", "true", "yes", "on")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/scan":
            self.send_error(404)
            return
        self._handle_trigger()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/scan":
            self.send_error(404)
            return
        self._handle_trigger()

    def _handle_trigger(self) -> None:
        global _running
        if not (os.getenv("SCAN_WEBHOOK_SECRET") or "").strip():
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"SCAN_WEBHOOK_SECRET not set"}')
            return
        if not self._secret_ok():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return
        with _lock:
            if _running:
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"scan_already_running"}')
                return
            _running = True

        force = self._force_rescan()

        def job() -> None:
            global _running
            try:
                _run_scan_once(force)
            finally:
                with _lock:
                    _running = False

        threading.Thread(target=job, daemon=True).start()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        body = json.dumps({"status": "started", "force_rescan": force})
        self.wfile.write(body.encode())


def run_scan_hook_server() -> None:
    from pathlib import Path

    from dotenv import load_dotenv

    # Always load repo-root .env (do not rely on cwd).
    _root = Path(__file__).resolve().parent.parent
    load_dotenv(_root / ".env")

    if not (os.getenv("SCAN_WEBHOOK_SECRET") or "").strip():
        print("Set SCAN_WEBHOOK_SECRET in .env (repo root). Refusing to start.")
        raise SystemExit(1)
    port = int(os.getenv("SCAN_HOOK_PORT", "9847"))
    host = os.getenv("SCAN_HOOK_BIND", "127.0.0.1")
    httpd = HTTPServer((host, port), _Handler)
    print("Scan hook listening:", f"http://{host}:{port}/scan?token=YOUR_SECRET")
    print("Stop with Ctrl+C.")
    httpd.serve_forever()
