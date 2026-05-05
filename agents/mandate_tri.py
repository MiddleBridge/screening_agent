"""Deterministic CEE + pre-seed/seed mandate tri (pipeline + Notion + snapshot)."""

from __future__ import annotations

import json
from typing import Any


def mandate_preseed_seed_cee_tri_passes(
    facts_obj: dict[str, Any],
    *,
    geography_fallback: str = "",
) -> bool:
    """CEE link: **HQ in CEE OR founder nationality CEE** (at least one YES, not both required);
    plus Stage (mandate) in PRE-SEED or SEED. Matches ``meets`` OR logic for geography/founders.
    """
    from agents import notion_sync as ns
    from agents.fund_decision import classify_stage

    fo = facts_obj or {}
    hq = str(fo.get("geography") or geography_fallback or "").strip()
    hq_lab = ns._hq_in_cee_label(hq)
    nat_lab = ns._founder_cee_nationality_label(
        str(fo.get("founder_nationality_hint") or "").strip(),
        str(fo.get("inferred_signals") or ""),
    )
    if hq_lab != "YES" and nat_lab != "YES":
        return False
    stage_raw = str(fo.get("stage") or "").strip()
    funding_round = str(fo.get("funding_round") or "").strip()
    sn = classify_stage(stage_raw, funding_rounds=[funding_round] if funding_round else None)
    if not sn or sn == "unknown":
        sn = "unknown"
    return ns._mandate_stage_bucket(sn) in ("PRE-SEED", "SEED")


def reconcile_uncertain_fund_fit_to_pass(
    fund_fit_decision: str,
    facts_obj: dict[str, Any],
    *,
    geography_fallback: str = "",
) -> str:
    """When Gate1 left fund_fit UNCERTAIN but facts satisfy the mandate tri, return PASS."""
    fd = str(fund_fit_decision or "").strip().upper()
    if fd != "UNCERTAIN":
        return fd
    if mandate_preseed_seed_cee_tri_passes(facts_obj, geography_fallback=geography_fallback):
        return "PASS"
    return fd


def gate2_rejected_but_mandate_tri_passes(row: dict[str, Any]) -> bool:
    """True only for REJECTED_GATE2 + full mandate tri from persisted facts."""
    st = str((row or {}).get("status") or "").strip().upper()
    if st != "REJECTED_GATE2":
        return False
    try:
        raw = (row or {}).get("gate2_facts_json")
        facts_obj = json.loads(raw) if raw else {}
        if not isinstance(facts_obj, dict):
            return False
        return mandate_preseed_seed_cee_tri_passes(
            facts_obj,
            geography_fallback=str((row or {}).get("gate1_detected_geography") or ""),
        )
    except Exception:
        return False
