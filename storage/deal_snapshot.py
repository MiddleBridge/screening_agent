"""Canonical deal snapshot for Notion and debugging — single object built from SQLite only."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional

from storage import database as db


# Notion sync lifecycle (stored in deals.notion_sync_status)
NOTION_NOT_ATTEMPTED = "NOTION_NOT_ATTEMPTED"
NOTION_SYNCED = "NOTION_SYNCED"
NOTION_FAILED = "NOTION_FAILED"
NOTION_BLOCKED_INVALID_SNAPSHOT = "NOTION_BLOCKED_INVALID_SNAPSHOT"
NOTION_BLOCKED_TECHNICAL_FAILURE = "NOTION_BLOCKED_TECHNICAL_FAILURE"


class SnapshotValidation(str, Enum):
    OK = "ok"
    BLOCKED_TECHNICAL = "blocked_technical"
    BLOCKED_INVALID = "blocked_invalid"


_TECH_STATUSES = frozenset(
    {
        db.STATUS_ERROR,
        db.STATUS_PDF_DOWNLOAD_FAILED,
        db.STATUS_PDF_EXTRACTION_FAILED,
        db.STATUS_SKIPPED_COST_CAP,
    }
)

_TECH_ERROR_CODES = frozenset(
    {
        "PDF_DOWNLOAD_FAILED",
        "PDF_EXTRACTION_FAILED",
        "DECK_TEXT_UNREADABLE",
        "PROCESSING_ERROR",
    }
)

_EXEMPT_MISSING_GATE2 = frozenset(
    {
        db.STATUS_GATE1_FAILED,
        db.STATUS_SKIPPED,
    }
)


def _json_mixed_list(raw: Any) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        arr = json.loads(str(raw))
        return arr if isinstance(arr, list) else []
    except Exception:
        s = str(raw).strip()
        return [s] if s else []


def _parse_object_json(raw: Any) -> tuple[dict[str, Any], Optional[str]]:
    if raw is None or not str(raw).strip():
        return {}, "missing"
    try:
        d = json.loads(str(raw))
        if isinstance(d, dict):
            return d, None
        return {}, "not_a_dict"
    except json.JSONDecodeError as e:
        return {}, f"json_decode:{e.msg}"


def _parse_optional_object(raw: Any) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    if raw is None or not str(raw).strip():
        return None, None
    try:
        d = json.loads(str(raw))
        if isinstance(d, dict):
            return d, None
        return None, "not_a_dict"
    except json.JSONDecodeError as e:
        return None, f"json_decode:{e.msg}"


def _profile_from_facts(facts: dict[str, Any], company_one_liner: str) -> tuple[str, str, str]:
    founded_year = ""
    founders_summary = ""
    one_liner = ""
    if facts:
        founded_year = str(facts.get("founded_year") or "").strip()
        founders = facts.get("founders") or facts.get("team") or []
        if isinstance(founders, str):
            raw_f = founders.strip()
            if " — Founder" in raw_f and ";" in raw_f:
                _noise = {
                    "crunchbase",
                    "legal name",
                    "operating status",
                    "company type",
                    "funding.",
                    "profile",
                }
                candidates = []
                for chunk in raw_f.split(";"):
                    chunk = chunk.strip()
                    if chunk.lower().endswith("— founder"):
                        name_part = chunk[: chunk.lower().rfind("— founder")].strip()
                        if name_part.lower().startswith("founders "):
                            name_part = name_part[9:].strip()
                        if name_part and not any(n in name_part.lower() for n in _noise):
                            candidates.append(name_part)
                founders_summary = ", ".join(candidates) if candidates else raw_f
            else:
                founders_summary = raw_f
        elif isinstance(founders, list) and founders:
            names: list[str] = []
            for f in founders[:4]:
                if isinstance(f, dict):
                    n = str(f.get("name") or "").strip()
                    if n:
                        names.append(n)
                elif isinstance(f, str):
                    s = f.strip()
                    if s:
                        names.append(s)
            founders_summary = ", ".join(names[:4])
        if not founders_summary:
            founders_summary = str(
                facts.get("team_signals") or facts.get("team") or ""
            ).strip()
        if not one_liner:
            one_liner = str(
                facts.get("what_they_do")
                or facts.get("product_description")
                or facts.get("company_one_liner")
                or facts.get("one_liner")
                or company_one_liner
                or ""
            ).strip()
    if not one_liner:
        one_liner = str(company_one_liner or "").strip()
    return founded_year, founders_summary, one_liner[:300]


def _token_usage_paragraph_from_row(row: dict[str, Any]) -> str:
    def _i(name: str, key_in: str, key_out: str, key_cost: str) -> Optional[str]:
        try:
            vi = row.get(key_in)
            vo = row.get(key_out)
            vc = row.get(key_cost)
            if vi is None and vo is None and (vc is None or float(vc or 0) == 0):
                return None
            tin = int(vi or 0)
            tout = int(vo or 0)
            c = float(vc or 0)
            return f"- **{name}:** in {tin:,} · out {tout:,} · ${c:.4f}"
        except Exception:
            return None

    lines: list[str] = []
    for tup in (
        ("Gate 1", "gate1_input_tokens", "gate1_output_tokens", "gate1_cost_usd"),
        ("Gate 2", "gate2_input_tokens", "gate2_output_tokens", "gate2_cost_usd"),
        ("Gate 2.5 (external)", "gate25_input_tokens", "gate25_output_tokens", "gate25_cost_usd"),
    ):
        s = _i(tup[0], tup[1], tup[2], tup[3])
        if s:
            lines.append(s)
    try:
        sc = row.get("gate25_search_calls")
        tc = row.get("gate25_tavily_credits")
        if sc is not None or tc is not None:
            lines.append(
                f"- **Gate 2.5 searches:** {int(sc or 0)} · **Tavily credits (est.):** {int(tc or 0)}"
            )
    except Exception:
        pass
    total = 0.0
    for k in ("gate1_cost_usd", "gate2_cost_usd", "gate25_cost_usd"):
        try:
            v = row.get(k)
            if v is not None:
                total += float(v)
        except Exception:
            pass
    if total > 0 or lines:
        lines.append(f"- **Total LLM cost (sum of stages):** ${total:.4f}")
    return "\n".join(lines) if lines else "— (not recorded for this run)"


@dataclass
class DealSnapshot:
    message_id: str
    # Email
    sender_name: str = ""
    sender_email: str = ""
    subject: str = ""
    email_body: str = ""
    email_header_date: str = ""
    created_at: str = ""
    updated_at: str = ""
    # Company / source
    company_name: str = ""
    company_one_liner: str = ""
    source_url: str = ""
    pdf_filename: str = ""
    has_pdf: int = 0
    deck_ocr_md: str = ""
    website_crawl_md: str = ""
    # Traction signals (deterministic check)
    traction: dict[str, Any] = field(default_factory=dict)
    # Pipeline
    status: str = ""
    gate2_status: str = ""
    last_error_code: str = ""
    last_error_detail: str = ""
    # Gate 1
    gate1_verdict: str = ""
    gate1_rejection_reason: str = ""
    gate1_detected_sector: str = ""
    gate1_detected_geography: str = ""
    gate1_detected_stage: str = ""
    # Gate 2 (parsed once)
    facts: dict[str, Any] = field(default_factory=dict)
    dimensions: dict[str, Any] = field(default_factory=dict)
    facts_parse_error: Optional[str] = None
    dimensions_parse_error: Optional[str] = None
    gate2_strengths: list[Any] = field(default_factory=list)
    gate2_concerns: list[Any] = field(default_factory=list)
    gate2_missing_critical_data: list[Any] = field(default_factory=list)
    gate2_should_ask_founder: list[Any] = field(default_factory=list)
    gate2_quality_flags: list[Any] = field(default_factory=list)
    gate2_overall_score: Optional[float] = None
    gate2_summary: str = ""
    gate2_recommendation: str = ""
    gate2_recommendation_rationale: str = ""
    # Derived profile (from facts during build)
    founded_year: str = ""
    founders_summary: str = ""
    one_liner: str = ""
    # Gate 2.5
    gate25_external: Optional[dict[str, Any]] = None
    gate25_final: Optional[dict[str, Any]] = None
    gate25_parse_errors: list[str] = field(default_factory=list)
    # Decision
    fund_fit_decision: str = ""
    deck_evidence_decision: str = ""
    generic_vc_interest: str = ""
    final_action: str = ""
    auth_risk: str = ""
    screening_depth: str = ""
    deck_evidence_score: Optional[float] = None
    external_opportunity_score: Optional[float] = None
    fund_fit_score: Optional[float] = None
    # Audit
    test_case: bool = False
    run_id: Optional[str] = None
    run_total_cost_usd: Optional[float] = None
    token_usage_md: str = ""
    # Legacy (debug only for Notion)
    gate2_snapshot_md_legacy: str = ""
    # Raw gate JSON strings as stored (for debugging artifacts only)
    gate2_facts_json_raw: str = ""
    gate2_dimensions_json_raw: str = ""
    # Founder calls (Fireflies etc.) — persisted JSON, rendered in Notion memo
    founder_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-serializable dict for artifacts (lists/dicts preserved)."""
        d = asdict(self)
        return d


def build_deal_snapshot(message_id: str) -> Optional[DealSnapshot]:
    """
    Load deal from SQLite and parse all JSON fields once.
    Does not call LLMs, Gmail, web search, or Notion.
    """
    row = db.get_deal_for_notion(message_id)
    if not row:
        return None
    facts, fe = _parse_object_json(row.get("gate2_facts_json"))
    dims, de = _parse_object_json(row.get("gate2_dimensions_json"))
    g25_ex, e1 = _parse_optional_object(row.get("gate25_external_json"))
    g25_fin, e2 = _parse_optional_object(row.get("gate25_final_decision_json"))
    g25_errs = [x for x in (e1, e2) if x]

    fy, fs, ol = _profile_from_facts(facts, str(row.get("company_one_liner") or ""))

    fund_fit_raw = str(row.get("fund_fit_decision") or "")
    fund_fit_eff = fund_fit_raw
    try:
        from agents.mandate_tri import reconcile_uncertain_fund_fit_to_pass

        fund_fit_eff = reconcile_uncertain_fund_fit_to_pass(
            fund_fit_raw,
            facts,
            geography_fallback=str(row.get("gate1_detected_geography") or ""),
        )
    except Exception:
        fund_fit_eff = fund_fit_raw

    traction_obj: dict[str, Any] = {}
    try:
        raw_tr = row.get("traction_json")
        if raw_tr:
            parsed = json.loads(raw_tr)
            if isinstance(parsed, dict):
                traction_obj = parsed
    except Exception:
        traction_obj = {}

    founder_calls: list[dict[str, Any]] = []
    try:
        fc_raw = row.get("founder_calls_json")
        if fc_raw:
            fcp = json.loads(fc_raw) if isinstance(fc_raw, str) else fc_raw
            if isinstance(fcp, list):
                founder_calls = [x for x in fcp if isinstance(x, dict)]
    except Exception:
        founder_calls = []

    snap = DealSnapshot(
        message_id=str(row.get("message_id") or ""),
        sender_name=str(row.get("sender_name") or ""),
        sender_email=str(row.get("sender_email") or ""),
        subject=str(row.get("subject") or ""),
        email_body=str(row.get("email_body") or ""),
        email_header_date=str(row.get("email_header_date") or ""),
        created_at=str(row.get("created_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        company_name=str(row.get("company_name") or ""),
        company_one_liner=str(row.get("company_one_liner") or ""),
        source_url=str(row.get("source_url") or ""),
        pdf_filename=str(row.get("pdf_filename") or ""),
        has_pdf=int(row.get("has_pdf") or 0),
        deck_ocr_md=str(row.get("deck_ocr_md") or ""),
        website_crawl_md=str(row.get("website_crawl_md") or ""),
        traction=traction_obj,
        status=str(row.get("status") or ""),
        gate2_status=str(row.get("gate2_status") or ""),
        last_error_code=str(row.get("last_error_code") or ""),
        last_error_detail=str(row.get("last_error_detail") or ""),
        gate1_verdict=str(row.get("gate1_verdict") or ""),
        gate1_rejection_reason=str(row.get("gate1_rejection_reason") or ""),
        gate1_detected_sector=str(row.get("gate1_detected_sector") or ""),
        gate1_detected_geography=str(row.get("gate1_detected_geography") or ""),
        gate1_detected_stage=str(row.get("gate1_detected_stage") or ""),
        facts=facts,
        dimensions=dims,
        facts_parse_error=fe,
        dimensions_parse_error=de,
        gate2_strengths=_json_mixed_list(row.get("gate2_strengths")),
        gate2_concerns=_json_mixed_list(row.get("gate2_concerns")),
        gate2_missing_critical_data=_json_mixed_list(row.get("gate2_missing_critical_data")),
        gate2_should_ask_founder=_json_mixed_list(row.get("gate2_should_ask_founder")),
        gate2_quality_flags=_json_mixed_list(row.get("gate2_quality_flags")),
        gate2_overall_score=(
            float(row["gate2_overall_score"]) if row.get("gate2_overall_score") is not None else None
        ),
        gate2_summary=str(row.get("gate2_summary") or ""),
        gate2_recommendation=str(row.get("gate2_recommendation") or ""),
        gate2_recommendation_rationale=str(row.get("gate2_recommendation_rationale") or ""),
        founded_year=fy,
        founders_summary=fs,
        one_liner=ol,
        gate25_external=g25_ex,
        gate25_final=g25_fin,
        gate25_parse_errors=g25_errs,
        fund_fit_decision=str(fund_fit_eff or ""),
        deck_evidence_decision=str(row.get("deck_evidence_decision") or ""),
        generic_vc_interest=str(row.get("generic_vc_interest") or ""),
        final_action=str(row.get("final_action") or ""),
        auth_risk=str(row.get("auth_risk") or ""),
        screening_depth=str(row.get("screening_depth") or ""),
        deck_evidence_score=(
            float(row["deck_evidence_score"]) if row.get("deck_evidence_score") is not None else None
        ),
        external_opportunity_score=(
            float(row["external_opportunity_score"])
            if row.get("external_opportunity_score") is not None
            else None
        ),
        fund_fit_score=(
            float(row["fund_fit_score"]) if row.get("fund_fit_score") is not None else None
        ),
        test_case=bool(row.get("test_case")),
        run_id=(str(row.get("run_id")).strip() if row.get("run_id") else None),
        run_total_cost_usd=(
            float(row["run_total_cost_usd"]) if row.get("run_total_cost_usd") is not None else None
        ),
        token_usage_md=_token_usage_paragraph_from_row(row),
        gate2_snapshot_md_legacy=str(row.get("gate2_snapshot_md") or ""),
        gate2_facts_json_raw=str(row.get("gate2_facts_json") or ""),
        gate2_dimensions_json_raw=str(row.get("gate2_dimensions_json") or ""),
        founder_calls=founder_calls,
    )
    return snap


def validate_deal_snapshot(snapshot: DealSnapshot) -> tuple[SnapshotValidation, str]:
    """
    Decide whether a full investment memo is allowed.
    Returns (outcome, human-readable reason).
    """
    st = (snapshot.status or "").strip()
    if st in _TECH_STATUSES:
        return SnapshotValidation.BLOCKED_TECHNICAL, f"pipeline status: {st}"
    lec = (snapshot.last_error_code or "").strip()
    if lec in _TECH_ERROR_CODES:
        return SnapshotValidation.BLOCKED_TECHNICAL, f"last_error_code: {lec}"

    if st not in _EXEMPT_MISSING_GATE2:
        raw_facts = (snapshot.gate2_facts_json_raw or "").strip()
        raw_dims = (snapshot.gate2_dimensions_json_raw or "").strip()
        if not raw_facts:
            return SnapshotValidation.BLOCKED_INVALID, "missing gate2_facts_json"
        if not raw_dims:
            return SnapshotValidation.BLOCKED_INVALID, "missing gate2_dimensions_json"
        if snapshot.facts_parse_error:
            return SnapshotValidation.BLOCKED_INVALID, f"gate2_facts_json: {snapshot.facts_parse_error}"
        if snapshot.dimensions_parse_error:
            return (
                SnapshotValidation.BLOCKED_INVALID,
                f"gate2_dimensions_json: {snapshot.dimensions_parse_error}",
            )

    return SnapshotValidation.OK, "ok"


def gate25_ran(snapshot: DealSnapshot) -> bool:
    """True if external / Gate 2.5 JSON artifacts are present."""
    return bool(snapshot.gate25_external or snapshot.gate25_final)
