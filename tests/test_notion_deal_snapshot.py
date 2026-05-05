"""Regression: DB row → DealSnapshot → Notion blocks (no silent JSON fallback in renderer)."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from agents.notion_sync import render_notion_blocks
from storage import database as db
from storage.deal_snapshot import build_deal_snapshot, gate25_ran, validate_deal_snapshot


def _flatten_blocks_text(blocks: list[dict]) -> str:
    parts: list[str] = []
    for b in blocks:
        t = b.get("type") or ""
        payload = b.get(t) or {}
        if isinstance(payload, dict):
            for rt in payload.get("rich_text") or []:
                if isinstance(rt, dict):
                    parts.append(str(rt.get("plain_text") or rt.get("text", {}).get("content") or ""))
    return "\n".join(parts)


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "pipeline.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path


def test_build_validate_render_no_gate25_leak(isolated_db):
    mid = "test_notion_snapshot_1"
    email = SimpleNamespace(
        message_id=mid,
        sender_email="founder@acme.test",
        sender_name="Founder",
        subject="Seed deck",
        body="Hello",
        website_url=None,
        has_pdf=1,
        pdf_filename="deck.pdf",
    )
    db.save_deal_email(email, status=db.STATUS_WAITING_HITL)

    facts = {
        "what_they_do": "Acme builds workflow tools for SMBs.",
        "company_one_liner": "SMB workflow SaaS",
        "tavily_queries": [],
    }
    dims = {
        "market": {"score": 6, "reasoning": "Narrow wedge; sizing from deck only."},
        "timing": {"score": 5, "reasoning": "Early category."},
    }
    gate25_secret = "GATE25_SYNTHETIC_TAM_CLAIM_X7"

    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            """
            UPDATE deals SET
                company_name=?,
                gate2_status=?,
                gate2_facts_json=?,
                gate2_dimensions_json=?,
                gate2_summary=?,
                gate2_recommendation=?,
                final_action=?,
                fund_fit_decision=?,
                deck_evidence_decision=?,
                run_id=?,
                gate25_external_json=NULL,
                gate25_final_decision_json=NULL
            WHERE message_id=?
            """,
            (
                "Acme Inc",
                "PASS",
                json.dumps(facts),
                json.dumps(dims),
                "Solid early signal; validate GTM.",
                "PASS_INTERNAL",
                "PASS_TO_PARTNER",
                "PASS",
                "PASS",
                "run-test-1",
                mid,
            ),
        )

    snap = build_deal_snapshot(mid)
    assert snap is not None
    assert not gate25_ran(snap)
    v, _ = validate_deal_snapshot(snap)
    assert v.value == "ok"

    blocks = render_notion_blocks(snap)
    blob = _flatten_blocks_text(blocks)
    assert "Acme" in blob or "workflow" in blob.lower()
    assert gate25_secret not in blob

    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            "UPDATE deals SET gate25_final_decision_json=? WHERE message_id=?",
            (json.dumps({"market_comment": gate25_secret}), mid),
        )

    snap2 = build_deal_snapshot(mid)
    assert snap2 is not None
    assert gate25_ran(snap2)
    blocks2 = render_notion_blocks(snap2)
    # Renderer is snapshot-only and does not inject Gate 2.5 narrative into the main memo.
    assert gate25_secret not in _flatten_blocks_text(blocks2)


def test_invalid_json_blocks_sync_diagnostic_path(isolated_db):
    mid = "test_bad_json"
    email = SimpleNamespace(
        message_id=mid,
        sender_email="x@y.z",
        sender_name="X",
        subject="S",
        body="b",
        website_url=None,
        has_pdf=0,
        pdf_filename=None,
    )
    db.save_deal_email(email, status=db.STATUS_WAITING_HITL)
    with sqlite3.connect(isolated_db) as conn:
        conn.execute(
            """
            UPDATE deals SET
                gate2_facts_json=?,
                gate2_dimensions_json=?,
                gate2_status=?,
                final_action=?
            WHERE message_id=?
            """,
            ("NOT JSON", "{}", "PASS", "PASS_TO_PARTNER", mid),
        )

    snap = build_deal_snapshot(mid)
    assert snap is not None
    assert snap.facts_parse_error
    v, reason = validate_deal_snapshot(snap)
    assert v.value == "blocked_invalid"
    assert "gate2_facts_json" in reason
