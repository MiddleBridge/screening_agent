"""Mandate tri + fund_fit reconciliation."""

from __future__ import annotations

from agents.mandate_tri import (
    gate2_rejected_but_mandate_tri_passes,
    mandate_preseed_seed_cee_tri_passes,
    reconcile_uncertain_fund_fit_to_pass,
)


def test_tri_passes_latvia_seed():
    facts = {
        "geography": "Riga, Latvia",
        "founder_nationality_hint": "Latvian",
        "stage": "Seed",
        "funding_round": "Seed",
    }
    assert mandate_preseed_seed_cee_tri_passes(facts, geography_fallback="")
    assert reconcile_uncertain_fund_fit_to_pass("UNCERTAIN", facts, geography_fallback="") == "PASS"


def test_tri_passes_diaspora_us_hq_cee_founder_only():
    """HQ outside CEE is OK if founder nationality is CEE (OR logic)."""
    facts = {
        "geography": "San Francisco, CA",
        "founder_nationality_hint": "Latvian",
        "stage": "Seed",
    }
    assert mandate_preseed_seed_cee_tri_passes(facts, geography_fallback="")
    assert reconcile_uncertain_fund_fit_to_pass("UNCERTAIN", facts, geography_fallback="") == "PASS"


def test_tri_fails_no_cee_hq_nor_founder():
    facts = {
        "geography": "San Francisco, CA",
        "founder_nationality_hint": "American",
        "stage": "Seed",
    }
    assert not mandate_preseed_seed_cee_tri_passes(facts, geography_fallback="")
    assert reconcile_uncertain_fund_fit_to_pass("UNCERTAIN", facts, geography_fallback="") == "UNCERTAIN"


def test_reconcile_does_not_override_fail():
    facts = {"geography": "Riga, Latvia", "founder_nationality_hint": "Latvian", "stage": "Seed"}
    assert reconcile_uncertain_fund_fit_to_pass("FAIL", facts, geography_fallback="") == "FAIL"


def test_gate2_rejected_soft_row():
    row = {
        "status": "REJECTED_GATE2",
        "gate2_facts_json": '{"geography": "Riga, Latvia", "founder_nationality_hint": "Latvian", "stage": "Seed"}',
        "gate1_detected_geography": "",
    }
    assert gate2_rejected_but_mandate_tri_passes(row)
    row2 = {**row, "status": "REJECTED_EXTERNAL"}
    assert not gate2_rejected_but_mandate_tri_passes(row2)
