"""founder_calls_json append + Notion snapshot rendering."""

import json
import sqlite3
from pathlib import Path

import pytest

from storage import database as db


@pytest.fixture()
def isolated_db(monkeypatch, tmp_path: Path) -> Path:
    p = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", p)

    def _init() -> None:
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
            "INSERT INTO deals VALUES (?,?,?,?,?)",
            ("mid1", "2026-01-01", "2026-01-01", "Acme Corp", None),
        )
        conn.commit()
        conn.close()

    _init()
    return p


def test_append_founder_call_and_dedupe(isolated_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", isolated_db)
    ok, reason = db.append_founder_call(
        "mid1",
        call_id="ff_1",
        source="fireflies",
        title="Intro",
        summary="hello",
    )
    assert ok and reason == "ok"
    with sqlite3.connect(isolated_db) as conn:
        raw = conn.execute(
            "SELECT founder_calls_json FROM deals WHERE message_id = ?",
            ("mid1",),
        ).fetchone()[0]
    calls = json.loads(raw)
    assert len(calls) == 1
    assert calls[0]["call_id"] == "ff_1"
    assert calls[0]["summary"] == "hello"

    ok2, reason2 = db.append_founder_call("mid1", call_id="ff_1", summary="again")
    assert not ok2 and reason2 == "duplicate"


def test_find_message_ids_by_company_name(isolated_db: Path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", isolated_db)
    mids = db.find_message_ids_by_company_name("acme corp")
    assert mids == ["mid1"]
