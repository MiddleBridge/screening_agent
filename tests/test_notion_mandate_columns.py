"""Mandate bucket helpers for Notion (HQ / nationality / stage columns)."""

from __future__ import annotations

from agents import notion_sync as ns


def test_mandate_stage_bucket():
    assert ns._mandate_stage_bucket("pre-seed") == "PRE-SEED"
    assert ns._mandate_stage_bucket("seed") == "SEED"
    assert ns._mandate_stage_bucket("seed-extension") == "SEED"
    assert ns._mandate_stage_bucket("series-a") == "OTHER"
    assert ns._mandate_stage_bucket("") == "OTHER"


def test_hq_in_cee_label_tri_state():
    assert ns._hq_in_cee_label("Riga, Latvia") == "YES"
    assert ns._hq_in_cee_label("San Francisco, CA") == "NO"
    assert ns._hq_in_cee_label("") == "UNCERTAIN"


def test_founder_cee_nationality_label():
    assert ns._founder_cee_nationality_label("Latvian", "") == "YES"
    assert ns._founder_cee_nationality_label("American", "american founder") == "NO"
    assert ns._founder_cee_nationality_label("", "") == "UNCERTAIN"
