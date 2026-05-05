"""Fireflies webhook signing + DB title matching."""

from __future__ import annotations

import json

import pytest

from storage import database as db
from tools.fireflies_client import verify_webhook_signature


def test_verify_fireflies_hmac() -> None:
    secret = "test-secret"
    raw = b'{"event":"meeting.summarized","meeting_id":"abc123"}'
    import hashlib
    import hmac

    expected_sig = (
        "sha256=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    )
    assert verify_webhook_signature(raw, expected_sig, secret) is True
    assert verify_webhook_signature(raw, "sha256=wrong", secret) is False


@pytest.fixture()
def iso_db(monkeypatch, tmp_path):
    import sqlite3
    from pathlib import Path

    p = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", p)
    conn = sqlite3.connect(p)
    conn.execute(
        """
        CREATE TABLE deals (
            message_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            company_name TEXT,
            founder_calls_json TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO deals VALUES (?,?,?,?,NULL)",
        ("mid_acme", "2026-01-01", "2026-01-01", "Acme Robotics"),
    )
    conn.commit()
    conn.close()
    return p


def test_find_message_ids_for_fireflies_title_unique(iso_db, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", iso_db)
    mids = db.find_message_ids_for_fireflies_title(
        "Intro call with Acme Robotics founders",
        days=365,
    )
    assert mids == ["mid_acme"]
