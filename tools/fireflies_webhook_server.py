# -*- coding: utf-8 -*-
"""HTTP receiver for Fireflies Webhooks V2 (and legacy V1). See ``main.py --fireflies-hook``."""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from tools.fireflies_client import verify_webhook_signature
from tools.fireflies_ingest import process_webhook_payload

log = logging.getLogger(__name__)

_lock = threading.Lock()
_running = False


def _load_env() -> None:
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env")


class _Handler(BaseHTTPRequestHandler):
    server_version = "FundFirefliesHook/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/fireflies", "/fireflies/"):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Fireflies webhook endpoint. POST JSON here (configure in Fireflies dashboard).\n"
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in ("/fireflies/webhook", "/fireflies/webhook/"):
            self.send_error(404)
            return

        secret = (os.getenv("FIREFLIES_WEBHOOK_SIGNING_SECRET") or "").strip()
        if not secret:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"FIREFLIES_WEBHOOK_SIGNING_SECRET not set"}')
            return

        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        sig = self.headers.get("X-Hub-Signature") or self.headers.get("x-hub-signature")

        if not verify_webhook_signature(raw, sig, secret):
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid_signature"}')
            return

        if not (os.getenv("FIREFLIES_API_KEY") or "").strip():
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"FIREFLIES_API_KEY not set"}')
            return

        try:
            body: Any = json.loads(raw.decode("utf-8") if raw else "{}")
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"invalid_json"}')
            return
        if not isinstance(body, dict):
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"expected_object"}')
            return

        global _running
        with _lock:
            if _running:
                self.send_response(409)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"ingest_already_running"}')
                return
            _running = True

        def job() -> None:
            global _running
            try:
                res = process_webhook_payload(body)
                log.info(
                    "Fireflies ingest meeting=%s ok=%s reason=%s msg_id=%s dup=%s notion_err=%s",
                    res.meeting_id,
                    res.ok,
                    res.reason,
                    res.message_id,
                    res.skipped_duplicate,
                    (res.notion_error or "")[:120],
                )
            except Exception:
                log.exception("Fireflies ingest job failed")
            finally:
                with _lock:
                    _running = False

        threading.Thread(target=job, daemon=True).start()

        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        out = {"status": "accepted", "meeting_id": body.get("meeting_id") or body.get("meetingId")}
        self.wfile.write(json.dumps(out).encode("utf-8"))


def run_fireflies_webhook_server() -> None:
    _load_env()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

    if not (os.getenv("FIREFLIES_WEBHOOK_SIGNING_SECRET") or "").strip():
        print("Set FIREFLIES_WEBHOOK_SIGNING_SECRET in .env (Fireflies dashboard signing secret).")
        raise SystemExit(1)
    if not (os.getenv("FIREFLIES_API_KEY") or "").strip():
        print("Set FIREFLIES_API_KEY to fetch transcript details after webhook.")
        raise SystemExit(1)

    port = int(os.getenv("FIREFLIES_HOOK_PORT", "9848") or "9848")
    host = os.getenv("FIREFLIES_HOOK_BIND", "127.0.0.1")
    httpd = HTTPServer((host, port), _Handler)
    print(f"Fireflies webhook: http://{host}:{port}/fireflies/webhook")
    print("Configure HTTPS URL in Fireflies (use a reverse proxy / tunnel for remote).")
    print("Stop with Ctrl+C.")
    httpd.serve_forever()
