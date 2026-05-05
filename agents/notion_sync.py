"""Minimal Notion sync for pipeline visibility (80/20, no overengineering)."""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from storage.database import get_deal_for_notion, get_deals_for_notion, update_notion_sync_status
from storage.deal_snapshot import (
    DealSnapshot,
    NOTION_BLOCKED_INVALID_SNAPSHOT,
    NOTION_BLOCKED_TECHNICAL_FAILURE,
    NOTION_FAILED,
    NOTION_SYNCED,
    SnapshotValidation,
    build_deal_snapshot,
    validate_deal_snapshot,
)
from config.fund_evidence_blocklist import (
    bad_founder_nationality_url_fragments,
    nationality_url_skip_substrings,
)
from config.fund_thesis import FUND_SECTORS_STRONG, FUND_SECTORS_WEAK_OR_RISKY

log = logging.getLogger(__name__)


def _legacy_notion_brand_prefix() -> str:
    """ASCII-only token for historical Notion column titles (pre–fund rename in UI)."""
    return "".join(map(chr, (73, 110, 110, 111, 118, 111)))


def _legacy_notion_fit_decision_property() -> str:
    return f"{_legacy_notion_brand_prefix()} Fit Decision"


def _legacy_notion_fit_score_property() -> str:
    return f"{_legacy_notion_brand_prefix()} Fit Score"


def _legacy_notion_meets_criteria_property() -> str:
    return f"Meets {_legacy_notion_brand_prefix()} criteria"


def _notion_legacy_property_display_renames() -> tuple[tuple[str, str], ...]:
    return (
        (_legacy_notion_fit_decision_property(), "Fund Fit Decision"),
        (_legacy_notion_fit_score_property(), "Fund Fit Score"),
        (_legacy_notion_meets_criteria_property(), "Meets fund criteria"),
    )


def _rename_notion_legacy_property_names(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    db_props: dict[str, Any],
) -> dict[str, Any]:
    """Rename historical Notion column titles to current fund naming (no duplicate target)."""
    props = dict(db_props or {})
    patch: dict[str, Any] = {}
    for old, new in _notion_legacy_property_display_renames():
        if old in props and new not in props:
            patch[old] = {"name": new}
    if not patch:
        return props
    rr = client.patch(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers=_headers(api_key),
        json={"properties": patch},
        timeout=45,
    )
    try:
        rr.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = rr.json()
            detail = str(body.get("message") or body)[:400]
        except Exception:
            detail = (rr.text or "")[:400]
        raise RuntimeError(f"Failed to rename Notion properties: {detail}") from e
    return (rr.json() or {}).get("properties") or props


_SUPERSEDED_NOTION_PROPERTIES = ("Mandate pass",)


def _drop_superseded_notion_properties(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    db_props: dict[str, Any],
) -> dict[str, Any]:
    """Remove duplicate columns superseded by ``Meets fund criteria`` (PATCH → null)."""
    drops = {n: None for n in _SUPERSEDED_NOTION_PROPERTIES if n in (db_props or {})}
    if not drops:
        return db_props
    rr = client.patch(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers=_headers(api_key),
        json={"properties": drops},
        timeout=45,
    )
    try:
        rr.raise_for_status()
    except httpx.HTTPStatusError:
        return db_props
    return (rr.json() or {}).get("properties") or {k: v for k, v in (db_props or {}).items() if k not in drops}


NOTION_VERSION = "2022-06-28"
DEFAULT_NOTION_VIEW_API_VERSION = "2025-09-03"


@dataclass
class SyncStats:
    scanned: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    #: Set when ``prune_columns=True``: number of DB properties removed (0 = already matched allowlist).
    pruned_property_count: int | None = None


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _headers_notion_version(api_key: str, version: str) -> dict[str, str]:
    v = (version or NOTION_VERSION).strip() or NOTION_VERSION
    return {
        "Authorization": f"Bearer {api_key}",
        "Notion-Version": v,
        "Content-Type": "application/json",
    }


def _notion_property_id(db_props: dict[str, Any], name: str) -> str | None:
    meta = (db_props or {}).get(name)
    if not isinstance(meta, dict):
        return None
    pid = meta.get("id")
    if not pid:
        return None
    return str(pid).strip()


def _db_prop_key(db_props: dict[str, Any] | None, *candidates: str) -> str | None:
    """Return the first Notion property name that exists in ``db_props`` (schema migration-friendly)."""
    props = db_props or {}
    for c in candidates:
        if c and c in props:
            return c
    return None


def _ensure_pipeline_table_view_layout(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    db_props: dict[str, Any],
) -> None:
    """
    Order table-view columns: **Name (title)** → **Status** →
    **HQ (CEE)** / **Nationality (CEE)** / **Stage (mandate)** → **Message ID** → rest.

    Uses Notion Views API. Disable with ``NOTION_PIPELINE_TABLE_LAYOUT=0``.
    Legacy env ``NOTION_MESSAGE_ID_COLUMN_SECOND=0`` also disables this layout pass.
    """
    if os.getenv("NOTION_PIPELINE_TABLE_LAYOUT", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    if os.getenv("NOTION_MESSAGE_ID_COLUMN_SECOND", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return

    primary: list[tuple[str, str, int]] = []  # (Notion property name, property_id, width)
    for col_name, width in (
        ("Status", 220),
        ("HQ (CEE)", 100),
        ("Nationality (CEE)", 130),
        ("Stage (mandate)", 120),
        ("Message ID", 300),
    ):
        pid = _notion_property_id(db_props, col_name)
        if pid:
            primary.append((col_name, pid, width))

    vver = (os.getenv("NOTION_VIEW_API_VERSION") or DEFAULT_NOTION_VIEW_API_VERSION).strip()
    h = _headers_notion_version(api_key, vver)
    try:
        lr = client.get(
            "https://api.notion.com/v1/views",
            params={"database_id": database_id, "page_size": 100},
            headers=h,
            timeout=45,
        )
        lr.raise_for_status()
        results = (lr.json() or {}).get("results") or []
    except Exception:
        return

    want_ids = ["title"] + [t[1] for t in primary]

    for ref in results:
        if (ref.get("type") or "").lower() != "table":
            continue
        vid = str(ref.get("id") or "").strip()
        if not vid:
            continue
        try:
            gr = client.get(
                f"https://api.notion.com/v1/views/{vid}",
                headers=h,
                timeout=45,
            )
            gr.raise_for_status()
            full = gr.json() or {}
            cfg = full.get("configuration") or {}
            if (cfg.get("type") or "").lower() != "table":
                continue
            props = cfg.get("properties")
            if not isinstance(props, list) or not props:
                continue
            cur_ids = [str(p.get("property_id") or "") for p in props]
            if len(cur_ids) >= len(want_ids) and cur_ids[: len(want_ids)] == want_ids:
                continue

            title_rows = [p for p in props if str(p.get("property_id") or "") == "title"]
            if not title_rows:
                continue
            title_row = dict(title_rows[0])
            title_row["property_id"] = "title"
            title_row.setdefault("visible", True)

            used: set[str] = {"title"}
            new_props: list[dict[str, Any]] = [title_row]

            for _name, pid, width in primary:
                rows = [p for p in props if str(p.get("property_id") or "") == pid]
                row = dict(rows[0]) if rows else {"property_id": pid, "visible": True, "width": width}
                row["property_id"] = pid
                row.setdefault("visible", True)
                row["width"] = int(row.get("width") or width)
                new_props.append(row)
                used.add(pid)

            for p in props:
                pid = str(p.get("property_id") or "")
                if pid in used:
                    continue
                new_props.append(dict(p))

            new_cfg = dict(cfg)
            new_cfg["type"] = "table"
            new_cfg["properties"] = new_props
            pr = client.patch(
                f"https://api.notion.com/v1/views/{vid}",
                headers=h,
                json={"configuration": new_cfg},
                timeout=45,
            )
            pr.raise_for_status()
        except Exception:
            continue


def _normalize_database_id(raw: str) -> str:
    """
    Accept plain 32-char id, hyphenated uuid, or full Notion URL and return
    canonical hyphenated UUID-like id.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.split("?", 1)[0].strip().rstrip("/")
    # Match either 32-hex compact id or 36-char hyphenated UUID in the string.
    m = re.search(r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", s)
    if not m:
        return s
    token = m.group(1).replace("-", "")
    if len(token) != 32:
        return s
    return f"{token[:8]}-{token[8:12]}-{token[12:16]}-{token[16:20]}-{token[20:]}"


def _crm_deal_status(row: dict[str, Any]) -> str:
    """Partner-facing pipeline column (Notion ``Status`` select).

    Values: To be reviewed | Rejected | Schedule an intro call | Due diligence
    """
    fa = str(row.get("final_action") or "").strip().upper()
    st = str(row.get("status") or "").strip().upper()
    if fa in ("STOP",):
        return "Rejected"
    if st.startswith("REJECTED"):
        from agents.mandate_tri import gate2_rejected_but_mandate_tri_passes

        if not gate2_rejected_but_mandate_tri_passes(row):
            return "Rejected"
    elif st in ("GATE1_FAILED", "GATE2_FAILED", "SKIPPED"):
        return "Rejected"
    if fa in ("PASS_TO_PARTNER",):
        return "Schedule an intro call"
    if st in ("APPROVED", "APPROVED_DRAFT_CREATED"):
        return "Due diligence"
    if st in ("GATE2_INTERNAL_PASS",):
        return "Due diligence"
    # RUN_ENRICHED_SCREEN, ASK_FOR_MORE_INFO, TEST_CASE_ONLY, WAITING_HITL, in-flight, NEW, …
    return "To be reviewed"


def _persist_notion_reconciliation_to_sqlite(row: dict[str, Any]) -> None:
    """After a successful Notion upsert, align SQLite with mandate tri (fund_fit + soft Gate2)."""
    mid = str(row.get("message_id") or "").strip()
    if not mid:
        return
    try:
        from storage import database as db
        from agents.mandate_tri import gate2_rejected_but_mandate_tri_passes

        eff = _effective_fund_fit_decision(row)
        raw = str(row.get("fund_fit_decision") or "").strip().upper()
        if eff == "PASS" and raw == "UNCERTAIN":
            db.update_fund_fit_decision(mid, "PASS")
        st = str(row.get("status") or "").strip().upper()
        if st == "REJECTED_GATE2" and gate2_rejected_but_mandate_tri_passes(row):
            db.update_status(mid, db.STATUS_WAITING_HITL)
    except Exception as e:
        log.warning("Notion→SQLite reconciliation persist failed: %s", e)


def _effective_fund_fit_decision(row: dict[str, Any]) -> str:
    """DB may still say UNCERTAIN after Gate1; upgrade to PASS when mandate tri is satisfied."""
    raw = str(row.get("fund_fit_decision") or "").strip().upper()
    if raw != "UNCERTAIN":
        return raw
    try:
        from agents.mandate_tri import reconcile_uncertain_fund_fit_to_pass

        rj = row.get("gate2_facts_json")
        facts_obj = json.loads(rj) if rj else {}
        if not isinstance(facts_obj, dict):
            return raw
        return reconcile_uncertain_fund_fit_to_pass(
            raw,
            facts_obj,
            geography_fallback=str(row.get("gate1_detected_geography") or ""),
        )
    except Exception:
        return raw


def _notion_short_reason(text: str, *, max_len: int = 96) -> str:
    """Single-line reason for Notion rich_text next to Yes/No or PASS/FAIL."""
    t = " ".join((text or "").split())
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip(" ,;—") + "…"


def _mandate_fail_brief(
    *,
    hq_in_cee: bool,
    founder_has_cee: bool,
    stage_ok: bool,
    stage_norm: str,
) -> str:
    """Compact mandate-fail chips for Notion (no long duplicated prose)."""
    parts: list[str] = []
    if not (hq_in_cee or founder_has_cee):
        parts.append("no CEE")
    if not stage_ok:
        parts.append("stage ?" if (stage_norm or "") == "unknown" else f"stage {stage_norm}")
    return " · ".join(parts) if parts else "mandate"


def _source_label(row: dict[str, Any]) -> str:
    msg_id = str(row.get("message_id") or "").lower()
    if msg_id.startswith("test_"):
        return "test"
    return "email"


def _gmail_message_url(message_id: str) -> str:
    mid = (message_id or "").strip()
    if not mid:
        return ""
    return f"https://mail.google.com/mail/u/0/#inbox/{mid}"


def _linkedin_search_url(name: str, company: str = "") -> str:
    q = urllib.parse.quote_plus(f"{name} {company}".strip())
    return f"https://www.linkedin.com/search/results/people/?keywords={q}"


def _parse_cee_osint(inferred_blob: str) -> list[tuple[str, str]]:
    """Return (country_token, source_url) pairs from inferred_signals.

    Accepts both new (``cee_founder_roots_source``) and legacy
    (``cee_founder_roots_osint``) markers for backward compatibility with
    rows produced by older pipeline runs.

    Skips the fund's own public site hosts and portfolio org listings — not valid third-party nationality evidence.
    If the same token appears multiple times, keeps the entry with the best URL.
    """
    _SKIP = nationality_url_skip_substrings()
    best: dict[str, str] = {}
    order: list[str] = []
    for line in (inferred_blob or "").splitlines():
        line = line.strip()
        low = line.lower()
        if ("cee_founder_roots_source" not in low) and ("cee_founder_roots_osint" not in low):
            continue
        # Accept both ASCII quotes (") and curly quotes (“”).
        token_m = re.search(r'[“"]([^”"]+)[”"]', line)
        token = token_m.group(1).strip().lower() if token_m else ""
        if not token:
            continue
        url_m = re.search(r'\[?(https?://[^\]\s]+)\]?', line)
        url = url_m.group(1).strip() if url_m else ""
        if url and any(d in url.lower() for d in _SKIP):
            url = ""
        if token not in best:
            best[token] = url
            order.append(token)
        elif not best[token] and url:
            best[token] = url
    return [(t, best[t]) for t in order]


def _bad_nationality_source_url(url: str) -> bool:
    """Crunchbase / own-fund portfolio pages are not evidence for founder nationality."""
    u = (url or "").lower()
    if not u:
        return False
    return any(b in u for b in bad_founder_nationality_url_fragments())


def _founder_nationalities_with_sources(facts_obj: dict[str, Any]) -> list[tuple[str, str]]:
    """
    Best-effort: return list of (NationalityToken, source_url).
    Prefers explicit OSINT markers in inferred_signals; falls back to
    founder_nationality_hint + founder_nationality_source_url.
    """
    hint = str((facts_obj or {}).get("founder_nationality_hint") or "").strip()
    src = str((facts_obj or {}).get("founder_nationality_source_url") or "").strip()
    if _bad_nationality_source_url(src):
        src = ""
    if hint:
        # Allow multiple hints separated by ";".
        parts = [p.strip() for p in re.split(r"[;/,]\s*", hint) if p.strip()]
        if parts:
            return [(p.title(), src) for p in parts[:6]]
    inferred_blob = str((facts_obj or {}).get("inferred_signals") or "")
    pairs = _parse_cee_osint(inferred_blob)
    if pairs:
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for tok, url in pairs:
            t = (tok or "").strip().lower()
            if not t or t in seen:
                continue
            seen.add(t)
            u = "" if _bad_nationality_source_url(url) else url
            out.append((t.title(), u))
        return out
    return []


def _profile_fields(row: dict[str, Any]) -> tuple[str, str, str]:
    """
    Returns (founded_year, founders_summary, product_one_liner)
    from persisted Gate2 facts where available.
    """
    founded_year = ""
    founders_summary = ""
    one_liner = ""

    raw = row.get("gate2_facts_json")
    if raw:
        try:
            d = json.loads(raw)
            if not founded_year:
                founded_year = str(d.get("founded_year") or "").strip()
            founders = d.get("founders") or d.get("team") or []
            if isinstance(founders, str):
                raw_f = founders.strip()
                # If the string looks like scraped Crunchbase UI labels
                # (multiple "; — Founder" entries), extract only real names.
                if " — Founder" in raw_f and ";" in raw_f:
                    _noise = {"crunchbase", "legal name", "operating status",
                              "company type", "funding.", "profile"}
                    candidates = []
                    for chunk in raw_f.split(";"):
                        chunk = chunk.strip()
                        if chunk.lower().endswith("— founder"):
                            name_part = chunk[: chunk.lower().rfind("— founder")].strip()
                            # strip leading "Founders " prefix if present
                            if name_part.lower().startswith("founders "):
                                name_part = name_part[9:].strip()
                            # skip if it contains noise words
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
                    d.get("team_signals")
                    or d.get("team")
                    or ""
                ).strip()
            # Prefer product description from extracted deck facts (what business actually does),
            # and only then fallback to one-liner fields.
            if not one_liner:
                one_liner = str(
                    d.get("what_they_do")
                    or d.get("product_description")
                    or d.get("company_one_liner")
                    or d.get("one_liner")
                    or row.get("company_one_liner")
                    or ""
                ).strip()
        except Exception:
            pass
    if not one_liner:
        one_liner = str(row.get("company_one_liner") or "").strip()
    return founded_year, founders_summary, one_liner[:300]


def _title(row: dict[str, Any], *, score_first: bool = False) -> str:
    base = (row.get("company_name") or row.get("sender_name") or "Unknown").strip()[:150]
    return base


def _to_notion_date(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        except Exception:
            return None
    return dt.strftime("%Y-%m-%d")


_CEE_HQ_MARKERS = (
    "poland",
    "warsaw",
    "krak",
    "wroc",
    "pozna",
    "gdańsk",
    "gdansk",
    "lithuania",
    "vilnius",
    "latvia",
    "riga",
    "estonia",
    "tallinn",
    "czech",
    "prague",
    "brno",
    "ostrava",
    "slovakia",
    "bratislava",
    "hungary",
    "budapest",
    "romania",
    "bucharest",
    "bulgaria",
    "sofia",
    "slovenia",
    "ljubljana",
    "croatia",
    "zagreb",
    "serbia",
    "belgrade",
    "ukraine",
    "kyiv",
    "kiev",
    "lviv",
    "odesa",
    "moldova",
    "chișinău",
    "chisinau",
    "north macedonia",
    "macedonia",
    "skopje",
    "bosnia",
    "sarajevo",
    "albania",
    "tirana",
    "kosovo",
    "montenegro",
    "podgorica",
)


def _hq_in_cee_label(hq: str) -> str:
    t = (hq or "").strip().lower()
    if not t or t in ("unknown", "n/a", "none", "not stated", "—", "not available"):
        return "UNCERTAIN"
    if "(inferred)" in t:
        t = t.replace("(inferred)", "").strip()
    if any(m in t for m in _CEE_HQ_MARKERS):
        return "YES"
    return "NO"


def _founder_cee_nationality_label(hint: str, inferred_blob: str) -> str:
    blob = f"{hint} {inferred_blob}".lower()
    if not blob.strip():
        return "UNCERTAIN"
    cee_markers = (
        "polish",
        "poland",
        "czech",
        "slovak",
        "slovakia",
        "hungarian",
        "romanian",
        "bulgarian",
        "croatian",
        "serbian",
        "slovenian",
        "ukrainian",
        "estonian",
        "latvian",
        "lithuanian",
        "cee ",
        "cee-",
        "central europe",
        "eastern europe",
        "baltics",
        "balkan",
    )
    if any(x in blob for x in cee_markers):
        return "YES"
    if hint.strip() and not any(x in blob for x in cee_markers):
        if any(
            x in blob
            for x in (
                "american",
                "united states",
                " u.s.",
                "british",
                "uk ",
                "german",
                "french",
                "israeli",
            )
        ):
            return "NO"
    return "UNCERTAIN"


def _mandate_stage_bucket(stage_norm: str) -> str:
    """Coarse mandate bucket for Notion: PRE-SEED / SEED / OTHER (vs fine-grained ``Stage``)."""
    s = (stage_norm or "").strip().lower()
    if s == "pre-seed":
        return "PRE-SEED"
    if s in ("seed", "seed-extension"):
        return "SEED"
    return "OTHER"


_SECTOR_BUZZWORD_PREFIXES = (
    "ai-powered",
    "ai powered",
    "ai-driven",
    "ai driven",
    "ai-enabled",
    "ai enabled",
    "ml-powered",
    "ml powered",
    "ml-driven",
    "ml driven",
    "gen-ai",
    "genai",
    "generative ai",
)


def _normalize_sector_label(sector: str) -> str:
    """Strip 'AI-powered X' marketing prefix so we classify the underlying domain X.

    Spacelift writes 'AI-powered infrastructure orchestration' on its homepage —
    that's IaC/DevOps, not 'AI/ML'. We only treat the company as 'AI/ML' when
    the underlying product layer is genuinely an ML/AI primitive (vertical AI,
    foundation models, ML infra), not when 'AI-powered' is a marketing prefix.
    """
    s = (sector or "").strip().lower()
    if not s:
        return ""
    for pref in _SECTOR_BUZZWORD_PREFIXES:
        if s.startswith(pref):
            tail = s[len(pref):].lstrip(" -:,").strip()
            return tail or s
    if "ai/ml" in s and any(
        k in s for k in (
            "infrastructure",
            "orchestration",
            "devops",
            "iac",
            "deploy",
            "ci/cd",
            "compliance",
            "fintech",
            "banking",
            "credit",
            "insurance",
            "healthcare",
            "logistics",
            "marketplace",
            "ecommerce",
            "e-commerce",
            "food",
            "education",
        )
    ):
        # Drop the AI/ML decoration; let downstream domain words classify the deal.
        s = s.replace("ai/ml", "").replace(",", " ").strip(" /-,;:")
    return s


def _thesis_sector_label(sector: str) -> str:
    s_raw = (sector or "").strip().lower()
    s = _normalize_sector_label(s_raw)
    if not s or s in ("unknown", "n/a", "none", "not stated", "—"):
        return "UNCERTAIN"
    for weak in FUND_SECTORS_WEAK_OR_RISKY:
        if weak.lower() in s:
            return "NO"
    for st in FUND_SECTORS_STRONG:
        sl = st.lower()
        if sl in s or s in sl:
            return "YES"
    # Only fall through to broad keyword match when the surface text is short —
    # otherwise long descriptions catch generic words and over-tag.
    if len(s) <= 60 and any(
        k in s for k in ("ai", "ml", "saas", "b2b", "developer", "infrastructure", "data ", "fintech")
    ):
        return "YES"
    return "UNCERTAIN"


def _founders_snapshot_line(founders_summary: str, facts_obj: dict[str, Any]) -> str:
    fs = (founders_summary or "").strip()
    low = fs.lower()
    if not fs or fs.upper().startswith("NOT_FOUND") or "not_found_in_deck" in low:
        return "—"
    if "no team slide" in low and "deck" in low:
        return "— (not in deck)"
    return fs if len(fs) <= 400 else fs[:399].rstrip() + "…"


def _sources_technical_notes(snapshot: Any, facts_obj: dict[str, Any], *, tq_list: list[str]) -> str:
    """Short legend for partners: what Origin / Tavily / tokens / crawl mean and what this run used."""
    wc = str(getattr(snapshot, "website_crawl_md", "") or "").strip()
    n_pages = wc.count("## Source:")
    tok = str(getattr(snapshot, "token_usage_md", "") or "")

    lines: list[str] = [
        "_Konkretne wartości (Origin, lista Tavily, tokeny, USD) są w sekcji **📚 Sources** wyżej._",
        "",
        "**Jak czytać te pola (technicznie)**",
        "",
        "- **Origin** — skąd pochodzi wejście do screeningu: załącznik PDF (OCR decka), link do wątku Gmail, ewentualnie strona przy trybie „website-only”.",
        "- **Tavily requests** — zapytania do **Tavily** (wyszukiwarka WWW) w **auto-enrichment**, gdy deck/mail nie uzupełnił founders, geography, funding itd. Rozliczenie = **kredyty Tavily** (API); **nie** wlicza się do **Total LLM cost** (tam są wyłącznie modele językowe).",
        "- **Token usage** — zużycie **LLM**: Gate 1 (klasyfikacja maila), Gate 2 (analiza decka). `in` / `out` = tokeny wejścia i wyjścia; **$** = szacunek kosztu z telemetrii pipelinu (billing dostawcy LLM).",
        "- **Website crawl** — pobranie strony (HTTP / przeglądarka headless), ekstrakcja tekstu; **sam crawl nie zużywa tokenów LLM**; koszt to czas i infrastruktura lokalna.",
    ]
    if "Gate 2.5" in tok or "gate 2.5" in tok.lower():
        lines.append(
            "- **Gate 2.5** (jeśli widać poniżej) — zewnętrzny etap (OSINT / dodatkowe LLM); ma **osobne** tokeny i może dodatkowe wyszukiwania."
        )
    lines.extend(
        [
            "",
            "**Ten przebieg:**",
        ]
    )
    if wc:
        lines.append(f"- Crawler WWW: **tak** (~**{n_pages}** stron w `website_crawl_md`, liczone po nagłówkach `## Source:`).")
    else:
        lines.append("- Crawler WWW: **nie** albo brak zapisanego markdownu crawla.")
    if tq_list:
        lines.append(f"- Tavily: **tak** — **{len(tq_list)}** zapytań zalogowanych (lista pod nagłówkiem Tavily).")
    else:
        lines.append("- Tavily: **nie** w logach albo auto-enrichment nie uruchomił zapytań w tej iteracji.")
    lines.append("")
    return "\n".join(lines)


def _facts_tavily_queries(facts_obj: dict[str, Any]) -> list[str]:
    raw = facts_obj.get("tavily_queries")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            p = str(item.get("purpose") or "").strip()
            q = str(item.get("query") or "").strip()
            if q:
                out.append(f"{p}: {q}" if p else q)
        elif isinstance(item, str) and item.strip():
            out.append(item.strip())
    return out


def _token_usage_paragraph(row: dict[str, Any]) -> str:
    def _i(name: str, key_in: str, key_out: str, key_cost: str) -> str | None:
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


_SCHEMA_DIM_KEYS = (
    "timing",
    "problem",
    "wedge",
    "founder_market_fit",
    "product_love",
    "execution_speed",
    "market",
    "moat_path",
    "traction",
    "business_model",
    "distribution",
)

_DIM_LABEL = {
    "timing": "Timing",
    "problem": "Problem / pain",
    "wedge": "Wedge / differentiation",
    "founder_market_fit": "Team / founder–market fit",
    "product_love": "Product",
    "execution_speed": "Execution speed",
    "market": "Market",
    "moat_path": "Moat / defensibility",
    "traction": "Traction",
    "business_model": "Business model",
    "distribution": "Distribution",
}


def _clip(text: str, max_len: int = 420) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max(0, max_len - 1)].rstrip() + "…"


def _with_source(line: str, source: str) -> str:
    s = (line or "").rstrip()
    if not s:
        return s
    if "(source:" in s.lower():
        return s
    return f"{s} (source: {source})"


def _annotate_lines_with_source(text: str, default_source: str) -> str:
    """Append `(source: ...)` to value lines in Notion narrative blocks."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        ln = raw.rstrip()
        st = ln.strip()
        if not st:
            out.append(ln)
            continue
        # Section headers and structural lines should stay clean.
        if re.match(r"^\d+\)", st) or st.endswith(":"):
            out.append(ln)
            continue

        src = default_source
        l = st.lower()
        if "external" in l or "osint" in l or "tavily" in l:
            src = "tavily"
        elif "fact_on_site" in l or "on-site" in l or "website:" in l:
            src = "website_crawl"
        elif "final action" in l or "fund fit" in l or "gate 1" in l or "score" in l:
            src = "database+rules"
        elif "executive summary" in l or "rationale" in l or "why not higher" in l:
            src = "llm"
        out.append(_with_source(ln, src))
    return "\n".join(out)


def _facts_dict_from_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("gate2_facts_json")
    if not raw:
        return {}
    try:
        d = json.loads(str(raw))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _json_mixed_list(row: dict[str, Any], key: str) -> list[Any]:
    raw = row.get(key)
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


def _format_mixed_line(item: Any) -> str:
    if item is None:
        return ""
    if isinstance(item, dict):
        q = str(item.get("question") or "").strip()
        w = str(item.get("why_it_matters") or "").strip()
        if q and w:
            return _clip(f"{q} — {w}", 700)
        if q:
            return _clip(q, 700)
        name = str(item.get("name") or item.get("title") or item.get("topic") or "").strip()
        desc = str(
            item.get("description")
            or item.get("detail")
            or item.get("rationale")
            or item.get("reasoning")
            or item.get("evidence")
            or ""
        ).strip()
        score = item.get("score")
        head_parts: list[str] = []
        if name:
            head_parts.append(name)
        if score is not None:
            try:
                head_parts.append(f"({float(score):g}/10)")
            except (TypeError, ValueError):
                pass
        head = " ".join(head_parts).strip()
        if head and desc:
            return _clip(f"{head}: {desc}", 700)
        if head or desc:
            return _clip(head or desc, 700)
        try:
            return _clip(json.dumps(item, ensure_ascii=False), 400)
        except Exception:
            return ""
    return _clip(str(item).strip(), 700)


def _lines_from_items(items: list[Any]) -> list[str]:
    out: list[str] = []
    for it in items:
        line = _format_mixed_line(it)
        if line:
            out.append(line)
    return out


def _dim_high_low_lines(
    dims: dict[str, Any],
    *,
    high_threshold: int = 7,
    low_threshold: int = 4,
) -> tuple[list[str], list[str]]:
    highs: list[str] = []
    lows: list[str] = []
    if not isinstance(dims, dict):
        return highs, lows
    for key in _SCHEMA_DIM_KEYS:
        d = dims.get(key)
        if not isinstance(d, dict):
            continue
        try:
            sc = int(d.get("score"))
        except (TypeError, ValueError):
            continue
        reasoning = _clip(str(d.get("reasoning") or "").strip(), 380)
        label = _DIM_LABEL.get(key, key)
        if sc >= high_threshold and reasoning:
            highs.append(f"• {label} ({sc}/10): {reasoning}")
        if sc <= low_threshold and reasoning:
            lows.append(f"• {label} ({sc}/10): {reasoning}")
    return highs, lows


def _compact_dim_scores(dims: dict[str, Any]) -> str:
    parts: list[str] = []
    if not isinstance(dims, dict):
        return ""
    for key in _SCHEMA_DIM_KEYS:
        d = dims.get(key)
        if not isinstance(d, dict):
            continue
        try:
            sc = int(d.get("score"))
        except (TypeError, ValueError):
            continue
        parts.append(f"{_DIM_LABEL.get(key, key)} {sc}/10")
    return " · ".join(parts)


def render_notion_blocks(snapshot: DealSnapshot) -> list[dict[str, Any]]:
    """Build Notion memo blocks from a validated DealSnapshot only (no DB, no JSON parse)."""
    founded_year, founders_summary, one_liner = (
        snapshot.founded_year,
        snapshot.founders_summary,
        snapshot.one_liner,
    )
    facts_obj = snapshot.facts
    _sn = str(snapshot.sender_name or "").strip()
    _se = str(snapshot.sender_email or "").strip()
    if _sn and _se:
        sender = f"{_sn} <{_se}>"
    else:
        sender = _sn or _se
    received_iso = _to_notion_date(snapshot.created_at) or ""
    source_url = str(snapshot.source_url or "").strip()
    link_label = "Website" if source_url else "Gmail"
    primary_link = source_url or _gmail_message_url(str(snapshot.message_id or ""))
    fund_fit = str(snapshot.fund_fit_decision or "")
    deck_ev = str(snapshot.deck_evidence_decision or "")
    generic = str(snapshot.generic_vc_interest or "")
    final_action = str(snapshot.final_action or "")
    auth_risk = str(snapshot.auth_risk or "")
    rationale = str(
        snapshot.gate2_recommendation_rationale
        or snapshot.gate1_rejection_reason
        or snapshot.gate2_summary
        or ""
    ).strip()
    verdict = str(snapshot.gate2_recommendation or snapshot.final_action or "").strip()
    fund_score = snapshot.fund_fit_score
    deck_score = snapshot.deck_evidence_score
    gate1_verdict = str(snapshot.gate1_verdict or "").strip()
    external_score = snapshot.external_opportunity_score
    na = "—"
    is_website = bool(source_url)
    if is_website and (not one_liner):
        one_liner = str(
            facts_obj.get("product_description")
            or facts_obj.get("one_liner")
            or ""
        ).strip()
    if is_website and (not one_liner):
        try:
            strengths_arr = snapshot.gate2_strengths
            if strengths_arr:
                first = _format_mixed_line(strengths_arr[0])
                if first:
                    one_liner = first
        except Exception:
            pass
    result_obj: dict[str, Any] = snapshot.dimensions if isinstance(snapshot.dimensions, dict) else {}
    website_scores_obj = result_obj.get("website_scores") if isinstance(result_obj, dict) else None
    dim_highs, dim_lows = _dim_high_low_lines(result_obj)

    def _merge_str_lists(primary: list[str], fallback: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for bucket in (primary, fallback):
            for x in bucket:
                k = x.strip().lower()
                if k and k not in seen:
                    seen.add(k)
                    out.append(x)
        return out

    def _band_label(sc: int) -> str:
        if sc >= 10:
            return "10 (OUTLIER)"
        if sc >= 7:
            return "7–9 (STRONG)"
        if sc >= 4:
            return "4–6 (PARTIAL)"
        return "1–3 (WEAK)"

    def _first_sentence(s: str) -> str:
        t = (s or "").strip()
        if not t:
            return ""
        # Keep the first sentence-ish chunk (avoid huge walls of text in Notion).
        m = re.split(r"(?<=[.!?])\s+", t)
        return (m[0] if m else t)[:260].strip()

    strengths_txt = ""
    if is_website and isinstance(website_scores_obj, dict) and website_scores_obj:
        # Website runs: show score + band + first-sentence rationale, plus one quote if present.
        dim_items: list[tuple[str, int, dict[str, Any]]] = []
        for k, v in website_scores_obj.items():
            if not isinstance(v, dict) or "score" not in v:
                continue
            try:
                dim_items.append((k, int(v.get("score") or 0), v))
            except Exception:
                continue
        dim_items.sort(key=lambda x: x[1], reverse=True)
        top = dim_items[:3]
        lines: list[str] = []
        for k, sc, obj in top:
            rsn = _first_sentence(str(obj.get("reasoning") or ""))
            band = _band_label(sc)
            ev0 = ""
            ev = obj.get("evidence") or []
            if isinstance(ev, list) and ev:
                e0 = ev[0] if isinstance(ev[0], dict) else {}
                q = str(e0.get("quote") or "").strip()
                src = str(e0.get("source") or "").strip()
                if q:
                    ev0 = f' Quote: "{_clip(q, 280)}"' + (f" ({_clip(src, 120)})" if src else "")
            label = k.replace("_", " ")
            lines.append(f"{label} — {sc}/10 [{band}] — {_clip(rsn, 260)}{ev0}".strip())
        strengths_txt = "\n".join(f"• {x}" for x in lines if x)
    else:
        strength_items: list[Any] = []
        if snapshot.gate2_strengths:
            strength_items = snapshot.gate2_strengths
        elif result_obj.get("top_strengths"):
            strength_items = list(result_obj.get("top_strengths") or [])
        elif result_obj.get("strengths"):
            strength_items = list(result_obj.get("strengths") or [])
        elif (result_obj.get("vc_pack") or {}).get("top_strengths"):
            strength_items = list((result_obj.get("vc_pack") or {}).get("top_strengths") or [])
        elif (result_obj.get("website_vc_pack") or {}).get("strengths"):
            strength_items = list((result_obj.get("website_vc_pack") or {}).get("strengths") or [])
        strength_lines = _lines_from_items(strength_items)
        if not strength_lines and dim_highs:
            strength_lines = list(dim_highs)
        strengths_txt = "\n".join(f"• {s}" for s in strength_lines) if strength_lines else ""

    risks_txt = ""
    if is_website and isinstance(website_scores_obj, dict) and website_scores_obj:
        dim_items2: list[tuple[str, int, dict[str, Any]]] = []
        for k, v in website_scores_obj.items():
            if not isinstance(v, dict) or "score" not in v:
                continue
            try:
                dim_items2.append((k, int(v.get("score") or 0), v))
            except Exception:
                continue
        dim_items2.sort(key=lambda x: x[1])
        low = dim_items2[:3]
        lines2: list[str] = []
        for k, sc, obj in low:
            rsn = _first_sentence(str(obj.get("reasoning") or ""))
            band = _band_label(sc)
            miss0 = ""
            md = obj.get("missing_data") or []
            if isinstance(md, list) and md:
                miss0 = f" Missing: {_clip(str(md[0]), 180)}"
            label = k.replace("_", " ")
            lines2.append(f"{label} — {sc}/10 [{band}] — {_clip(rsn, 260)}{miss0}".strip())
        risks_txt = "\n".join(f"• {x}" for x in lines2 if x)
    else:
        risk_items: list[Any] = []
        if snapshot.gate2_concerns:
            risk_items = snapshot.gate2_concerns
        elif result_obj.get("top_risks"):
            risk_items = list(result_obj.get("top_risks") or [])
        elif result_obj.get("top_concerns"):
            risk_items = list(result_obj.get("top_concerns") or [])
        risk_lines = _lines_from_items(risk_items)
        if not risk_lines and dim_lows:
            risk_lines = list(dim_lows)
        risks_txt = "\n".join(f"• {s}" for s in risk_lines) if risk_lines else ""

    _mc_top = result_obj.get("missing_critical_data")
    missing_fb = (
        [str(x).strip() for x in _mc_top if str(x).strip()]
        if isinstance(_mc_top, list)
        else []
    )
    missing_src = _merge_str_lists(
        [str(x).strip() for x in snapshot.gate2_missing_critical_data if str(x).strip()],
        missing_fb,
    )
    kill_flags = (
        [str(x).strip() for x in snapshot.gate2_quality_flags if str(x).strip()]
        or [str(x).strip() for x in (result_obj.get("kill_flags") or []) if str(x).strip()]
        or [str(x).strip() for x in (result_obj.get("red_flags") or []) if str(x).strip()]
    )
    follow_parts = _lines_from_items(snapshot.gate2_should_ask_founder)
    follow_strs = follow_parts
    if not follow_strs:
        follow_strs = [
            str(x).strip()
            for x in (result_obj.get("follow_up_questions") or result_obj.get("follow_ups") or [])
            if str(x).strip()
        ]
    followups = follow_strs
    if not followups:
        mvn = result_obj.get("must_validate_next") or []
        if isinstance(mvn, list):
            for item in mvn[:8]:
                if isinstance(item, dict):
                    q = str(item.get("question") or "").strip()
                    w = str(item.get("why_it_matters") or "").strip()
                    if q:
                        followups.append(f"{q} — {w}" if w else q)
    ask_merge = result_obj.get("should_ask_founder") if isinstance(result_obj.get("should_ask_founder"), list) else []
    if ask_merge:
        followups = _merge_str_lists(followups, [str(x).strip() for x in ask_merge if str(x).strip()])

    missing = missing_src
    # Normalize missing: split multiline bullets + dedupe
    missing_norm: list[str] = []
    seen: set[str] = set()
    for item in (missing or []):
        for ln in str(item).splitlines():
            s = ln.strip()
            if not s:
                continue
            if s.startswith("-"):
                s = s.lstrip("-").strip()
            if not s:
                continue
            key = s.lower()
            if key in seen:
                continue
            seen.add(key)
            missing_norm.append(s)
    # Prefer detailed entries and drop shorter duplicates contained in longer lines.
    filtered_missing: list[str] = []
    for m in missing_norm:
        ml = m.lower()
        if any((ml != x.lower()) and (ml in x.lower()) and (len(x) > len(m)) for x in missing_norm):
            continue
        filtered_missing.append(m)
    missing_txt = "\n".join([f"• {m}" for m in filtered_missing[:16]]) if filtered_missing else ""
    kill_txt = ", ".join(kill_flags) if kill_flags else ""
    follow_txt = "\n".join([f"• {q}" for q in followups[:12]]) if followups else ""

    # Decision snapshot
    score_val = snapshot.deck_evidence_score
    if score_val is None:
        score_val = snapshot.gate2_overall_score
    score_str = f"{float(score_val):.2f}" if score_val is not None else "n/a"

    def _first_known(*candidates: Any) -> str:
        """Return first non-empty, non-sentinel string (treats 'unknown'/'n/a' as empty)."""
        sentinels = {"unknown", "n/a", "none", "not stated", "not specified", "—", "not available"}
        for c in candidates:
            t = str(c or "").strip()
            if not t:
                continue
            if t.lower() in sentinels:
                continue
            if t.lower().startswith("not_found_in_") or t.lower().startswith("not found in "):
                continue
            return t
        return ""

    stage = _first_known(snapshot.gate1_detected_stage, facts_obj.get("stage"))
    geo = _first_known(snapshot.gate1_detected_geography, facts_obj.get("geography"))
    sector_snap = _first_known(
        snapshot.gate1_detected_sector,
        facts_obj.get("sector"),
        facts_obj.get("market"),
    )
    inferred_blob = str(facts_obj.get("inferred_signals") or "")

    def _extract_inferred_value(prefix: str) -> str:
        for ln in inferred_blob.splitlines():
            s = ln.strip()
            if not s:
                continue
            if s.lower().startswith(prefix.lower()):
                return s.split(":", 1)[1].strip() if ":" in s else s
        return ""

    founder_nationality = _extract_inferred_value("founder_nationality_hint")
    registration_geo = _extract_inferred_value("company_registration_geo_hint")
    if not geo:
        inferred = ""
        if source_url.lower().endswith(".pl") or ".pl/" in source_url.lower():
            inferred = "Poland"
        else:
            try:
                lang = str(facts_obj.get("language") or "").lower()
                founders_blob = (founders_summary or "").lower()
                polish_markers = (
                    "ą", "ć", "ę", "ł", "ń", "ó", "ś", "ź", "ż", "sz", "cz",
                    "wicz", "icz", "ski", "cki", "dzki", "owski", "ewski",
                    "misztal", "zimoch", "kowalski", "nowak", "wiśniewski",
                )
                if "polish" in lang or (lang.strip().lower()[:2] == "pl" and len(lang.strip()) <= 8):
                    inferred = "Poland"
                elif any(m in founders_blob for m in polish_markers):
                    inferred = "Poland"
            except Exception:
                inferred = ""
        geo = f"{inferred} (inferred)" if inferred else ""
    why_blocked = ""
    if str(final_action or "").upper() not in ("PASS_TO_PARTNER", "PASS"):
        # Avoid misleading "hard reject" language when we're simply asking for more info
        # (common for website-only runs with thin geo/stage evidence).
        soft_hold = str(final_action or "").upper() in ("ASK_FOR_MORE_INFO", "RUN_ENRICHED_SCREEN")
        website_needs_deck = is_website and str(verdict or "").upper() in ("NEEDS_DECK", "NEEDS_FOUNDER_CALL")
        if soft_hold and website_needs_deck:
            why_blocked = "Website-only evidence insufficient — request deck / validate geo & stage."
        else:
            why_blocked = (
                str(snapshot.gate1_rejection_reason or "").strip()
                or (kill_flags[0] if kill_flags else "")
                or (f"Fund fit: {fund_fit}" if fund_fit and fund_fit != "PASS" else "")
                or (f"Gate 1: UNCERTAIN — geography/stage not confirmed" if "UNCERTAIN" in str(fund_fit or "") else "")
                or "—"
            )
    if not why_blocked:
        why_blocked = "—"

    internal_score_label = "Website VC score" if is_website else "Deck evidence / internal score"
    snap_lines = [
        "1) Scores",
        f"{internal_score_label}: {score_str}",
        f"Gate 2 overall (pipeline): {'%.2f' % float(snapshot.gate2_overall_score) if snapshot.gate2_overall_score is not None else 'n/a'}",
        f"External opportunity score: {'%.2f' % float(external_score) if external_score is not None else 'n/a'}",
        f"Fund fit score: {'%.2f' % float(fund_score) if fund_score is not None else 'n/a'}",
        "",
        "2) Mandate / routing",
        f"Fund fit decision: {fund_fit or na}",
        f"Gate 1 verdict: {gate1_verdict or na}",
        f"Verdict / recommendation: {verdict or na}",
    ]
    if sector_snap:
        snap_lines.append(f"Sector: {sector_snap}")
    snap_lines.append(f"Stage: {stage or na}")
    snap_lines.append(f"Geography: {geo or na}")
    snap_lines.append(f"Company registration geography: {registration_geo or na}")
    snap_lines.append(f"Founders nationalities: {founder_nationality or na}")
    if is_website:
        snap_lines.append(
            "Mode: Website-only (INITIAL) — unknowns are missing evidence, not a soft reject."
        )
    snap_lines.extend(["", "3) Blockers / uncertainty", f"Why blocked (if not pass): {why_blocked}"])
    snapshot_txt = _annotate_lines_with_source("\n".join(snap_lines), "database+rules")

    # Market context + VC narrative (dims / flags / why-not-higher)
    sat = "n/a"
    timing_sig = "n/a"
    comps = "n/a"
    try:
        sat_raw = (
            result_obj.get("market_saturation")
            or (result_obj.get("vc_scores") or {}).get("saturation_score")
        )
        timing_raw = (
            result_obj.get("timing_score")
            or (result_obj.get("vc_scores") or {}).get("timing_score")
        )
        comp_raw = result_obj.get("competition_density")
        if sat_raw is not None and str(sat_raw).strip():
            sat = f"{float(sat_raw):.1f}/10"
        if timing_raw is not None and str(timing_raw).strip():
            timing_sig = f"{float(timing_raw):.1f}/10"
        if comp_raw:
            comps = str(comp_raw)

        wnh = result_obj.get("why_not_higher") or []
        blob = " | ".join([str(x) for x in wnh if x]) if isinstance(wnh, list) else str(wnh)
        if sat == "n/a":
            m = re.search(r"saturation\s*[:=]?\s*(\d+(?:\.\d+)?)", blob, re.I)
            if m:
                sat = f"{float(m.group(1)):.1f}/10"
        if timing_sig == "n/a":
            m2 = re.search(r"timing[_\s-]*score\s*[:=]?\s*(\d+(?:\.\d+)?)", blob, re.I)
            if m2:
                timing_sig = f"{float(m2.group(1)):.1f}/10"
        fo = facts_obj
        if sat == "n/a":
            sat_raw2 = fo.get("market_saturation") or fo.get("saturation_score")
            if sat_raw2 is not None and str(sat_raw2).strip():
                sat = f"{float(sat_raw2):.1f}/10"
        if timing_sig == "n/a":
            timing_raw2 = fo.get("timing_score")
            if timing_raw2 is not None and str(timing_raw2).strip():
                timing_sig = f"{float(timing_raw2):.1f}/10"
        wnh_blob = str(fo.get("why_not_higher") or "")
        if sat == "n/a":
            m3 = re.search(r"saturation\s*[:=heuristic]*\s*(\d+(?:\.\d+)?)", wnh_blob, re.I)
            if m3:
                sat = f"{float(m3.group(1)):.1f}/10"
        if timing_sig == "n/a":
            m4 = re.search(r"timing[_\s]*score\s*[=:]*\s*(\d+(?:\.\d+)?)", wnh_blob, re.I)
            if m4:
                timing_sig = f"{float(m4.group(1)):.1f}/10"
        if comps == "n/a":
            comps = (
                "crowded (heuristic)"
                if sat != "n/a" and float(sat.split("/")[0]) < 4.0
                else "n/a"
            )
    except Exception:
        pass

    market_extra: list[str] = []
    wnh_list = result_obj.get("why_not_higher") or []
    if isinstance(wnh_list, list):
        for x in wnh_list[:10]:
            t = str(x).strip()
            if t:
                market_extra.append(f"• {t}")
    for dim_key in ("market", "timing"):
        dm = result_obj.get(dim_key)
        if isinstance(dm, dict):
            wnh_dim = str(dm.get("why_not_higher") or "").strip()
            if len(wnh_dim) > 15:
                market_extra.append(f"• {_DIM_LABEL.get(dim_key, dim_key)} — {_clip(wnh_dim, 400)}")
    slow_flags = result_obj.get("slow_execution_flags") if isinstance(result_obj.get("slow_execution_flags"), list) else []
    sol_flags = result_obj.get("solution_love_flags") if isinstance(result_obj.get("solution_love_flags"), list) else []
    if slow_flags:
        market_extra.append("Execution notes: " + "; ".join(str(x) for x in slow_flags[:8]))
    if sol_flags:
        market_extra.append("Product signals: " + "; ".join(str(x) for x in sol_flags[:8]))

    market_txt = _annotate_lines_with_source("\n".join(
        [
            "1) Heuristics",
            f"Saturation heuristic: {sat}",
            f"Timing signal: {timing_sig}",
            f"Competition read: {comps}",
            "",
            "2) Evidence / notes",
            "\n".join(market_extra) if market_extra else na,
            "",
            "3) Implication",
            "Heuristics are website-only and directional; validate with deck/call + real metrics.",
        ]
    ).strip(), "llm")

    next_step = str(
        result_obj.get("recommended_next_step")
        or snapshot.gate2_recommendation_rationale
        or snapshot.gate2_summary
        or ""
    ).strip()

    known_lines: list[str] = []
    if one_liner:
        known_lines.append(f"One-liner: {_clip(one_liner, 340)}")
    what_long = str(facts_obj.get("what_they_do") or "").strip()
    if what_long and what_long.lower() != (one_liner or "").lower():
        known_lines.append(f"What they do: {_clip(what_long, 560)}")
    if founders_summary:
        known_lines.append(f"Founders / team: {_clip(founders_summary, 320)}")
    if founder_nationality:
        known_lines.append(f"Founders nationalities (hint): {_clip(founder_nationality, 220)}")
    if registration_geo:
        known_lines.append(f"Company registration geo (hint): {_clip(registration_geo, 220)}")
    if founded_year:
        known_lines.append(f"Founded: {founded_year}")
    sector_row = str(snapshot.gate1_detected_sector or "").strip()
    sector_f = str(facts_obj.get("market") or facts_obj.get("sector") or "").strip()
    if sector_row:
        known_lines.append(f"Sector (pipeline): {sector_row}")
    elif sector_f:
        known_lines.append(f"Sector (extracted): {_clip(sector_f, 220)}")
    stage_f = str(facts_obj.get("stage") or facts_obj.get("stage_guess") or "").strip()
    if stage_f:
        known_lines.append(f"Stage (facts): {stage_f}")
    cust = str(
        facts_obj.get("customers")
        or facts_obj.get("customer")
        or facts_obj.get("target_customer")
        or ""
    ).strip()
    if cust:
        known_lines.append(f"Customers / ICP: {_clip(cust, 300)}")
    pricing = str(facts_obj.get("pricing") or facts_obj.get("pricing_signals") or "").strip()
    if pricing:
        known_lines.append(f"Pricing / model: {_clip(pricing, 260)}")
    traction_f = str(facts_obj.get("traction") or facts_obj.get("traction_signals") or "").strip()
    if traction_f:
        known_lines.append(f"Traction (claimed): {_clip(traction_f, 420)}")
    fr = str(facts_obj.get("fundraising_ask") or "").strip()
    uf = str(facts_obj.get("use_of_funds") or "").strip()
    if fr or uf:
        ru = " · ".join([p for p in [fr, uf] if p])
        known_lines.append(f"Raise / use of funds: {_clip(ru, 360)}")
    if not is_website and sender:
        known_lines.append(f"Sender: {sender}")
    if received_iso:
        known_lines.append(f"Received: {received_iso}")
    if primary_link:
        known_lines.append(f"{link_label}: {primary_link}")
    if not known_lines:
        fb = str(snapshot.gate2_summary or snapshot.company_one_liner or "").strip()
        if fb:
            known_lines.append(_clip(fb, 950))

    named_lines: list[str] = []
    f_founders = str(facts_obj.get("founders") or "").strip()
    if f_founders and f_founders.lower() not in ("unknown", "n/a", "none"):
        named_lines.append(f"Founders: {_clip(f_founders, 360)}")
    # logos_or_case_studies holds actual customer names; customer_proof is often
    # a marketing claim — use logos first and only fall back to customer_proof
    # when it contains concrete evidence (numbers / percentages).
    _logos = str(facts_obj.get("logos_or_case_studies") or "").strip()
    _proof = str(facts_obj.get("customer_proof") or "").strip()
    _logos_valid = bool(_logos) and _logos.lower() not in ("unknown", "n/a", "none", "not stated", "—")
    _proof_has_data = bool(re.search(r'\d', _proof))  # has at least one digit → concrete
    cust2 = _logos if _logos_valid else (_proof if _proof_has_data else "")
    if cust2:
        named_lines.append(f"Customers / logos: {_clip(cust2, 360)}")
    integ = str(facts_obj.get("integrations") or "").strip()
    if integ:
        named_lines.append(f"Integrations: {_clip(integ, 360)}")
    sec = str(facts_obj.get("security_compliance_signals") or "").strip()
    if sec:
        named_lines.append(f"Security / compliance: {_clip(sec, 360)}")

    unknown_lines: list[str] = []
    miss_txt = str(facts_obj.get("unclear_or_missing_data") or "").strip()
    if miss_txt:
        unknown_lines.append(_clip(miss_txt, 520))

    summary1 = _annotate_lines_with_source("\n".join(
        [
            "1) What we know (from source)",
            "\n".join(known_lines) if known_lines else na,
            "",
            "2) Named entities / specifics",
            "\n".join(named_lines) if named_lines else na,
            "",
            "3) Unknown / missing",
            "\n".join(unknown_lines) if unknown_lines else na,
        ]
    ).strip(), "website_crawl+llm")

    exec_sum = str(snapshot.gate2_summary or "").strip()
    cds = _compact_dim_scores(result_obj)
    internal_label2 = "Website VC score" if is_website else "Deck Evidence"
    part1 = "\n".join(
        [
            f"Verdict: {verdict or na}",
            f"{internal_label2} decision: {deck_ev or na} (score: {na if deck_score is None else deck_score})",
            f"Fund fit: {fund_fit or na} (score: {na if fund_score is None else fund_score})",
            f"Generic VC Interest: {generic or na}",
        ]
    ).strip()
    exec_for_display = _clip(exec_sum, 900) if exec_sum else ""
    if is_website and exec_for_display and re.search(r"\bMissing:\s*", exec_for_display, re.I):
        # Avoid repeating the whole Missing section inside the executive summary.
        exec_for_display = re.sub(
            r"\s*Missing:\s*[\s\S]+$",
            "",
            exec_for_display,
            flags=re.I,
        ).strip()
    part2 = "\n".join(
        [
            "Executive summary:",
            exec_for_display if exec_for_display else na,
            "",
            f"Rationale: {rationale or na}",
            (f"Full dimension scorecard: {cds}" if cds else ""),
        ]
    ).strip()
    part3 = "\n".join(
        [
            (f"Auth Risk: {auth_risk or na}" if not is_website else ""),
            f"Stage: {stage or na}",
            f"Geography: {geo or na}",
        ]
    ).strip()
    summary2 = _annotate_lines_with_source("\n".join(
        [
            "1) Signal / decision",
            part1,
            "",
            "2) Why",
            part2,
            "",
            "3) Context",
            part3,
        ]
    ).strip(), "llm+database+rules")

    mf_parts = [
        "Missing data:",
        missing_txt if missing_txt else "(nothing flagged in screening)",
        "",
        "Kill / quality flags:",
        kill_txt if kill_txt else "(none)",
        "",
        "Follow-up questions:",
        follow_txt if follow_txt else "(none suggested)",
    ]
    mf_txt = _annotate_lines_with_source("\n".join(mf_parts), "llm+website_crawl")

    if not strengths_txt:
        slf = result_obj.get("solution_love_flags") if isinstance(result_obj.get("solution_love_flags"), list) else []
        if slf:
            strengths_txt = "\n".join(f"• {str(x)}" for x in slf[:14])
    if not strengths_txt:
        prob = result_obj.get("problem")
        if isinstance(prob, dict):
            eu = prob.get("evidence_used") or []
            if isinstance(eu, list) and eu:
                strengths_txt = "\n".join(f"• {str(x)}" for x in eu[:10])
            elif str(prob.get("reasoning") or "").strip():
                strengths_txt = f"• Problem / pain (scoreboard): {_clip(str(prob.get('reasoning')), 520)}"

    if not risks_txt:
        sef = result_obj.get("slow_execution_flags") if isinstance(result_obj.get("slow_execution_flags"), list) else []
        if sef:
            risks_txt = "\n".join(f"• {str(x)}" for x in sef[:14])

    # Enforce 3-subsection structure for narrative sections (kept inside a single Notion paragraph).
    ev_rows = result_obj.get("evidence_table") if isinstance(result_obj, dict) else None
    ev_lines: list[str] = []
    if isinstance(ev_rows, list):
        for r in ev_rows[:10]:
            if not isinstance(r, dict):
                continue
            aspect = str(r.get("aspect") or "").strip()
            finding = str(r.get("finding") or "").strip()
            kind = str(r.get("kind") or "").strip()
            if aspect and finding:
                ev_lines.append(f"• {aspect} [{kind or 'fact'}]: {_clip(finding, 520)}")
    ev_txt = "\n".join(ev_lines) if ev_lines else na
    ask_txt = "\n".join([f"• {q}" for q in followups[:8]]) if followups else na

    strengths_txt = _annotate_lines_with_source("\n".join(
        [
            "1) Strong signals",
            strengths_txt if strengths_txt else na,
            "",
            "2) Evidence (on-site / extracted)",
            ev_txt,
            "",
            "3) Validate next",
            ask_txt,
        ]
    ).strip(), "llm+website_crawl")
    ev_dup_note = (
        "Same on-site crawl ledger as in 💪 Strengths (not duplicated here)."
        if ev_txt and ev_txt != na
        else na
    )
    risks_txt = _annotate_lines_with_source("\n".join(
        [
            "1) Main risks / weak signals",
            risks_txt if risks_txt else na,
            "",
            "2) On-site evidence",
            ev_dup_note,
            "",
            "3) Follow-ups",
            ask_txt,
        ]
    ).strip(), "llm+website_crawl")

    gsum = str(snapshot.gate2_summary or "").strip()
    rationale_clean = str(rationale or "").strip()
    rec_main = next_step or (_clip(gsum, 1100) if gsum else "")
    if is_website and rationale_clean and str(final_action or "").upper() in (
        "ASK_FOR_MORE_INFO",
        "RUN_ENRICHED_SCREEN",
    ):
        rec_why = rationale_clean
    elif gsum and rec_main and gsum not in rec_main:
        rec_why = _clip(gsum, 520)
    else:
        rec_why = rationale_clean or na
    rec_ask = "\n".join([f"• {q}" for q in followups[:8]]) if followups else na
    rec_txt = _annotate_lines_with_source("\n".join(
        [
            "1) Recommendation",
            rec_main or na,
            "",
            "2) Why",
            rec_why or na,
            "",
            "3) What to request / validate",
            rec_ask,
        ]
    ).strip(), "llm+rules")

    def _is_unknown(v: str) -> bool:
        """True if value is empty, sentinel, or pipeline-internal placeholder.

        Filters out:
          - blanks: "", "unknown", "n/a", "none", "not stated", "—"
          - deck sentinels: "NOT_FOUND_IN_DECK", "NOT_FOUND_*"
          - boilerplate: "no team slide or founder info found in deck"
        """
        t = (v or "").strip().lower()
        if not t or t in ("unknown", "n/a", "none", "not stated", "not specified", "—", "not available"):
            return True
        if t.startswith("not_found_in_") or t.startswith("not found in "):
            return True
        if "no team slide" in t and "deck" in t:
            return True
        return False

    company_name_str = str(snapshot.company_name or "")
    last_valuation = str(facts_obj.get("valuation") or "").strip()
    funding_round = str(facts_obj.get("funding_round") or "").strip()
    funding_amount = str(facts_obj.get("funding_amount") or "").strip()
    funding_date = str(facts_obj.get("funding_date") or "").strip()

    has_deck_data = bool(snapshot.pdf_filename) or (
        bool(snapshot.gate2_facts_json_raw) and not is_website
    )

    # ── Partner-facing memo (no routing verdict / REQUEST_DECK noise) ─────────
    hq_snap = geo if not _is_unknown(geo) else (registration_geo if not _is_unknown(registration_geo) else "")
    stage_snap = stage if not _is_unknown(stage) else str(facts_obj.get("stage") or "").strip()
    if _is_unknown(stage_snap):
        stage_snap = na
    val_snap = last_valuation if not _is_unknown(last_valuation) else na
    fr_snap = funding_round if not _is_unknown(funding_round) else na
    fa_snap = funding_amount if not _is_unknown(funding_amount) else na
    fd_snap = funding_date if not _is_unknown(funding_date) else na
    founders_snap = _founders_snapshot_line(founders_summary, facts_obj)
    nat_pairs = _founder_nationalities_with_sources(facts_obj)
    nat_list = "; ".join([n for n, _ in nat_pairs]) if nat_pairs else na
    nat_sources = []
    for n, u in nat_pairs[:6]:
        if u:
            host = ""
            try:
                host = urllib.parse.urlparse(u).netloc.lstrip("www.")
            except Exception:
                host = ""
            label = host or "source"
            nat_sources.append(f"{n} ([{label}]({u}))")
        else:
            nat_sources.append(f"{n} (source: inferred)")
    nat_sources_txt = "; ".join(nat_sources) if nat_sources else na
    cee_nat_snap = _founder_cee_nationality_label(founder_nationality, inferred_blob)
    hq_cee_snap = _hq_in_cee_label(hq_snap)

    sector_display = (
        sector_snap if not _is_unknown(sector_snap) else str(facts_obj.get("market") or facts_obj.get("sector") or "").strip()
    )
    if _is_unknown(sector_display):
        sector_display = na
    else:
        # Strip 'AI-powered' marketing prefix so partners see the actual domain.
        norm = _normalize_sector_label(sector_display)
        if norm and norm != sector_display.strip().lower():
            sector_display = f"{norm} (raw: {sector_display})"
    thesis_ln = _thesis_sector_label(sector_display)

    one_display = one_liner if not _is_unknown(one_liner) else str(facts_obj.get("what_they_do") or "").strip()
    if _is_unknown(one_display):
        one_display = na

    cust_display = na
    if cust2 and str(cust2).strip() and not _is_unknown(str(cust2)):
        cust_display = _clip(str(cust2), 400)
    elif cust and str(cust).strip() and not _is_unknown(str(cust)):
        cust_display = _clip(str(cust), 400)

    founded_display = founded_year if not _is_unknown(founded_year) else na

    legal_ent = str(facts_obj.get("legal_entity_name") or "").strip()
    reg_id = str(facts_obj.get("company_registry_id") or "").strip()
    website_snap = str(facts_obj.get("website_url") or "").strip()

    snap_company_lines: list[str] = [
        f"- **company name:** {company_name_str or na}",
    ]
    if website_snap:
        snap_company_lines.append(f"- **website:** {website_snap}")
    if legal_ent:
        snap_company_lines.append(f"- **legal entity (site):** {legal_ent}")
    if reg_id:
        snap_company_lines.append(f"- **company registration number:** {reg_id}")

    company_snapshot_txt = "\n".join(
        snap_company_lines
        + [
            f"- **founders:** {founders_snap}",
            f"- **founder nationalities:** {nat_list}",
            f"- **nationality sources:** {nat_sources_txt}",
            f"- **CEE link (founders):** {cee_nat_snap}",
            f"- **hq:** {hq_snap or na}",
            f"- **HQ in CEE:** {hq_cee_snap}",
            f"- **stage:** {stage_snap}",
            f"- **funding round:** {fr_snap}",
            f"- **money raised:** {fa_snap}",
            f"- **funding date:** {fd_snap}",
            f"- **valuation:** {val_snap}",
        ]
    )

    business_txt = "\n".join(
        [
            f"- **founded:** {founded_display}",
            f"- **sector:** {sector_display}",
            f"- **sector (investment thesis match):** {thesis_ln}",
            f"- **one-liner:** {one_display}",
        ]
    )

    tq = _facts_tavily_queries(facts_obj)
    tq_lines = "\n".join(f"- {q}" for q in tq) if tq else "— (none logged for this run; rescan after update to capture)"
    tok_lines = snapshot.token_usage_md

    origin_bits: list[str] = []
    if has_deck_data:
        origin_bits.append(f"Deck: `{str(snapshot.pdf_filename or 'deck.pdf')}`")
    if is_website and primary_link:
        origin_bits.append(f"Website: {primary_link}")
    if not is_website and primary_link:
        origin_bits.append(f"Gmail: {primary_link}")
    origin_line = " · ".join(origin_bits) if origin_bits else ""

    sources_txt = "\n".join(
        [
            *(["**Origin:** " + origin_line, ""] if origin_line else []),
            "**Tavily requests**",
            tq_lines,
            "",
            "**Token usage**",
            tok_lines,
        ]
    ).strip()
    tech_sources_legend = _sources_technical_notes(snapshot, facts_obj, tq_list=tq)

    # ── Notion blocks (sections rendered only if non-empty) ───────────────────
    def _h2(title: str) -> dict:
        return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _block_rich_text(title)}}

    def _p(text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _block_rich_text(text)}}

    generated_at_raw = str(snapshot.updated_at or snapshot.created_at or "").strip()
    generated_at = generated_at_raw.replace("T", " ")
    generated_at = generated_at.split(".")[0] if "." in generated_at else generated_at
    mail_title = str(snapshot.subject or "").strip() or na
    generated_txt = "\n".join(
        [
            f"- **generated at:** {generated_at or na}",
            f"- **mail title:** {mail_title}",
        ]
    )

    blocks: list[dict[str, Any]] = []
    blocks += [_h2("🕒 Generated"), _p(generated_txt)]
    blocks += [_h2("🏢 Company snapshot"), _p(company_snapshot_txt)]
    blocks += [_h2("💼 Business"), _p(business_txt)]
    blocks += [_h2("📚 Sources"), _p(sources_txt)]

    calls_list = list(getattr(snapshot, "founder_calls", None) or [])
    if calls_list:
        max_sum = int(os.getenv("FOUNDER_CALL_SUMMARY_MAX_CHARS", "12000") or "12000")
        max_tr = int(os.getenv("FOUNDER_CALL_TRANSCRIPT_MAX_CHARS", "8000") or "8000")
        ordered = sorted(
            calls_list,
            key=lambda c: str((c or {}).get("occurred_at") or (c or {}).get("appended_at") or ""),
            reverse=True,
        )
        call_chunks: list[str] = []
        for c in ordered:
            if not isinstance(c, dict):
                continue
            dt = str(c.get("occurred_at") or c.get("appended_at") or "").strip() or "—"
            ttl = str(c.get("title") or "Founder call").strip()
            src = str(c.get("source") or "call").strip().lower()
            att = str(c.get("attendees") or "").strip() or "n/a"
            url = str(c.get("transcript_url") or "").strip()
            cid = str(c.get("call_id") or "").strip()
            summ = str(c.get("summary") or "").strip()
            tr = str(c.get("transcript") or "").strip()
            head = f"**{dt} — {ttl}** ({src})"
            if cid:
                head += f" · id `{cid}`"
            body_lines = [
                head,
                f"- Attendees: {att}",
                f"- Recording / transcript link: {url or 'n/a'}",
                "",
                "**Summary** (e.g. from Fireflies)",
                (summ[:max(0, max_sum)] + ("…" if len(summ) > max_sum else "")) if summ else "_(no summary)_",
            ]
            if tr:
                tclip = tr[: max(0, max_tr)] + ("…" if len(tr) > max_tr else "")
                body_lines.extend(["", "**Transcript excerpt**", tclip])
            call_chunks.append("\n".join(body_lines))
        calls_txt = "\n\n---\n\n".join(call_chunks) if call_chunks else "—"
        blocks += [_h2("🎙 Founder calls"), _p(calls_txt)]

    # ── Traction signals (deterministic) ────────────────────────────────
    traction = getattr(snapshot, "traction", None) or {}
    if isinstance(traction, dict) and (traction.get("verdict") or traction.get("deck") or traction.get("website")):
        verdict = str(traction.get("verdict") or "NONE").upper()
        verdict_pl = {
            "BOTH": "YES — found in BOTH deck and website",
            "DECK_ONLY": "YES — found in deck only",
            "WEBSITE_ONLY": "YES — found on website only",
            "NONE": "NO traction signals detected in deck or website",
        }.get(verdict, "—")
        deck_hits = list(traction.get("deck") or [])
        web_hits = list(traction.get("website") or [])
        lines = [f"- **verdict:** {verdict_pl}"]
        if deck_hits:
            lines.append("- **from deck:**")
            for h in deck_hits[:8]:
                label = str(h.get("label", "")).strip()
                snippet = str(h.get("snippet", "")).strip()
                lines.append(f"    - {label}: \"{snippet}\"")
        if web_hits:
            lines.append("- **from website:**")
            for h in web_hits[:8]:
                label = str(h.get("label", "")).strip()
                snippet = str(h.get("snippet", "")).strip()
                lines.append(f"    - {label}: \"{snippet}\"")
        blocks += [_h2("📈 Traction signals"), _p("\n".join(lines))]

    # ── Raw markdown bundles (Notion ``code`` blocks; split across blocks if rich_text > 100 items).
    em_hdr = str(getattr(snapshot, "email_header_date", "") or "").strip()
    em_body_raw = str(getattr(snapshot, "email_body", "") or "")
    subj_line = str(snapshot.subject or "").strip() or na
    if em_hdr:
        date_line = em_hdr
        date_note = "Date (mail header / Gmail)"
    else:
        ca = _to_notion_date(snapshot.created_at) or str(snapshot.created_at or "").split("T")[0] or na
        date_line = f"_(RFC Date header not stored — pipeline record started {ca})_"
        date_note = "Date (Gmail / mail header)"
    body_out = (em_body_raw.strip() if em_body_raw.strip() else None) or (
        "_(empty)_" if not is_website else "_(website intake — primary copy in WEBSITE.MD below)_"
    )
    max_em = int(os.getenv("NOTION_EMAIL_MD_MAX_CHARS", "60000") or "60000")
    email_doc = "\n".join(
        [
            f"- **From:** {(sender or '').strip() or na}",
            f"- **{date_note}:** {date_line}",
            f"- **Subject:** {subj_line}",
            "",
            "## Body",
            "",
            body_out,
        ]
    )
    eclip = email_doc[: max(0, max_em)]
    blocks += [_h2("📧 EMAIL.MD (inbound)")]
    if len(email_doc) > len(eclip):
        blocks += [
            _p(
                f"(truncated: first {len(eclip):,} / {len(email_doc):,} chars; "
                "raise NOTION_EMAIL_MD_MAX_CHARS for more)"
            )
        ]
    blocks.extend(_notion_code_blocks_from_markdown(eclip))

    # Deck OCR extract: store the OCR markdown inside Notion (no file paths).
    deck_md = str(getattr(snapshot, "deck_ocr_md", "") or "").strip()
    if deck_md and not is_website:
        max_chars = int(os.getenv("NOTION_DECK_OCR_MAX_CHARS", "60000") or "60000")
        clipped = deck_md[: max(0, max_chars)]
        blocks += [_h2("🧾 PITCH DECK.MD (OCR)")]
        if len(deck_md) > len(clipped):
            blocks += [
                _p(
                    f"(truncated: showing first {len(clipped):,} / {len(deck_md):,} chars; "
                    "increase NOTION_DECK_OCR_MAX_CHARS to show more)"
                )
            ]
        blocks.extend(_notion_code_blocks_from_markdown(clipped))

    # Website crawl extract (markdown)
    web_md = str(getattr(snapshot, "website_crawl_md", "") or "").strip()
    if web_md:
        max_w = int(os.getenv("NOTION_WEBSITE_MD_MAX_CHARS", "60000") or "60000")
        wclip = web_md[: max(0, max_w)]
        blocks += [_h2("🌐 WEBSITE.MD (crawl)")]
        if len(web_md) > len(wclip):
            blocks += [
                _p(
                    f"(truncated: showing first {len(wclip):,} / {len(web_md):,} chars; "
                    "increase NOTION_WEBSITE_MD_MAX_CHARS to show more)"
                )
            ]
        blocks.extend(_notion_code_blocks_from_markdown(wclip))

    blocks += [
        _h2("🔧 Legenda źródeł i kosztów"),
        _p(tech_sources_legend),
    ]
    return blocks


def _append_optional_legacy_debug_blocks(
    blocks: list[dict[str, Any]],
    snapshot: DealSnapshot,
) -> list[dict[str, Any]]:
    if os.getenv("NOTION_MEMO_DEBUG_LEGACY", "0").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return blocks
    leg = (snapshot.gate2_snapshot_md_legacy or "").strip()
    if not leg:
        return blocks

    def _h2(title: str) -> dict[str, Any]:
        return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _block_rich_text(title)}}

    def _p(text: str) -> dict[str, Any]:
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _block_rich_text(text)}}

    out = list(blocks)
    out += [_h2("🧪 Debug: legacy gate2_snapshot_md"), _p(leg[:19000])]
    return out


def render_notion_diagnostic_blocks(
    snapshot: DealSnapshot,
    *,
    code: str,
    detail: str,
) -> list[dict[str, Any]]:
    def _h2(title: str) -> dict[str, Any]:
        return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _block_rich_text(title)}}

    def _p(text: str) -> dict[str, Any]:
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _block_rich_text(text)}}

    body = "\n".join(
        [
            f"**Notion sync code:** {code}",
            f"**Detail:** {detail}",
            f"**message_id:** {snapshot.message_id}",
            f"**Pipeline status:** {snapshot.status or '—'}",
            f"**last_error_code:** {snapshot.last_error_code or '—'}",
            "",
            "No full investment memo was rendered. Fix the pipeline row or data quality, then re-sync.",
        ]
    )
    blocks: list[dict[str, Any]] = [_h2("⚠️ Screening sync (diagnostic)"), _p(body)]
    return _append_optional_legacy_debug_blocks(blocks, snapshot)


def _dump_notion_artifacts(snapshot: DealSnapshot, blocks: list[dict[str, Any]]) -> None:
    if os.getenv("NOTION_ARTIFACT_DUMP", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return
    root = Path(__file__).resolve().parent.parent / "artifacts"
    rid = (snapshot.run_id or "").strip() or snapshot.message_id
    d = root / rid
    d.mkdir(parents=True, exist_ok=True)
    (d / "deal_snapshot.json").write_text(
        json.dumps(snapshot.to_json_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (d / "notion_blocks.json").write_text(
        json.dumps(blocks, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _prepare_notion_memo_for_row(
    row: dict[str, Any],
) -> tuple[list[dict[str, Any]], SnapshotValidation, str, DealSnapshot]:
    """Validate snapshot, persist blocked Notion statuses, return blocks + snapshot for artifacts."""
    mid = str(row.get("message_id") or "").strip()
    snap = build_deal_snapshot(mid) if mid else None
    if not snap:
        empty = DealSnapshot(message_id=mid or "unknown")
        if mid:
            update_notion_sync_status(mid, NOTION_BLOCKED_INVALID_SNAPSHOT, "deal row missing for snapshot")
        return (
            render_notion_diagnostic_blocks(
                empty,
                code=NOTION_BLOCKED_INVALID_SNAPSHOT,
                detail="deal row missing for snapshot",
            ),
            SnapshotValidation.BLOCKED_INVALID,
            "no snapshot",
            empty,
        )
    outcome, reason = validate_deal_snapshot(snap)
    if outcome == SnapshotValidation.BLOCKED_TECHNICAL:
        update_notion_sync_status(snap.message_id, NOTION_BLOCKED_TECHNICAL_FAILURE, reason)
        return (
            render_notion_diagnostic_blocks(
                snap,
                code=NOTION_BLOCKED_TECHNICAL_FAILURE,
                detail=reason,
            ),
            outcome,
            reason,
            snap,
        )
    if outcome == SnapshotValidation.BLOCKED_INVALID:
        update_notion_sync_status(snap.message_id, NOTION_BLOCKED_INVALID_SNAPSHOT, reason)
        return (
            render_notion_diagnostic_blocks(
                snap,
                code=NOTION_BLOCKED_INVALID_SNAPSHOT,
                detail=reason,
            ),
            outcome,
            reason,
            snap,
        )
    blocks = _append_optional_legacy_debug_blocks(render_notion_blocks(snap), snap)
    return blocks, outcome, reason, snap


def _finalize_notion_sync_status(
    message_id: str,
    *,
    validation: SnapshotValidation,
    children_patch_ok: bool,
    patch_error: str = "",
) -> None:
    if not children_patch_ok:
        update_notion_sync_status(message_id, NOTION_FAILED, patch_error or "notion_children_patch_failed")
        return
    if validation == SnapshotValidation.OK:
        update_notion_sync_status(message_id, NOTION_SYNCED, None)
    # BLOCKED_* already set in caller before patch


def _append_page_children_chunked(
    client: httpx.Client,
    api_key: str,
    page_id: str,
    blocks: list[dict[str, Any]],
) -> bool:
    """Notion caps ``children`` at 100 blocks per request."""
    chunk_size = 100
    for i in range(0, len(blocks), chunk_size):
        chunk = blocks[i : i + chunk_size]
        try:
            client.patch(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=_headers(api_key),
                json={"children": chunk},
                timeout=120,
            ).raise_for_status()
        except httpx.HTTPStatusError as e:
            body = ""
            try:
                body = (e.response.text or "")[:1200]
            except Exception:
                body = str(e)
            log.warning(
                "Notion append children failed page_id=%s chunk_start=%s: %s %s",
                page_id,
                i,
                getattr(e.response, "status_code", "?"),
                body,
            )
            return False
        except Exception as e:
            log.warning("Notion append children failed page_id=%s chunk_start=%s: %s", page_id, i, e)
            return False
    return True


def _ensure_page_summary_blocks(
    client: httpx.Client,
    *,
    api_key: str,
    page_id: str | None,
    desired_blocks: list[dict[str, Any]],
) -> bool:
    """Replace the auto-generated deal memo on the page with the given blocks.

    Archives every known pipeline section (heading + body until the next heading_2) so we
    never stack legacy ⚡ Decision blocks under the new 🏢 layout. Content *above* the first
    managed heading is treated as old sync garbage and archived (partner notes should live
    under their own heading outside this set).

    **Append before archive:** if the Notion ``children`` PATCH fails after we already
    archived the old body, the page would be empty. We append the new memo first, then
    archive the superseded block IDs.
    """
    if not page_id:
        return False
    desired = desired_blocks
    try:
        children = _list_all_page_children(client, page_id, api_key)
    except Exception:
        return False
    to_archive = _collect_managed_memo_block_ids(children)
    if children and not to_archive:
        # Page has blocks but none match our managed H2 titles (e.g. Notion template line,
        # columns, or an old diagnostic heading typo). Replace entire body so sync can run.
        to_archive = [str(b.get("id")) for b in children if b.get("id")]
    if not desired:
        # Nothing new to write — keep existing body (avoid wiping the page).
        return True
    if not _append_page_children_chunked(client, api_key, page_id, desired):
        return False
    if to_archive:
        if not _archive_blocks(client, api_key, to_archive):
            return False
    return True


def _as_rich_text(text: str, *, strip_outer: bool = True) -> list[dict[str, Any]]:
    """Parse simple inline markdown into Notion rich_text chunks.
    Supports: **bold text**, [label](url). Newlines preserved.

    ``strip_outer=False`` keeps leading/trailing whitespace (needed when splitting
    long markdown for Notion ``code`` blocks so chunk boundaries stay exact).
    """
    s = (text or "")
    if strip_outer:
        s = s.strip()
    if not s:
        return []
    chunks: list[dict[str, Any]] = []
    pattern = re.compile(r'\*\*([^*\n]+)\*\*|\[([^\]]+)\]\(([^)]*)\)')
    last = 0
    for m in pattern.finditer(s):
        if m.start() > last:
            plain = s[last:m.start()]
            for i in range(0, len(plain), 1900):
                chunks.append({"type": "text", "text": {"content": plain[i:i+1900]}})
        if m.group(1) is not None:
            chunks.append({
                "type": "text",
                "text": {"content": m.group(1)[:500]},
                "annotations": {"bold": True},
            })
        else:
            chunks.append({
                "type": "text",
                "text": {"content": m.group(2)[:300], "link": {"url": m.group(3)[:500]}},
            })
        last = m.end()
    if last < len(s):
        tail = s[last:]
        for i in range(0, len(tail), 1900):
            chunks.append({"type": "text", "text": {"content": tail[i:i+1900]}})
    return chunks or [{"type": "text", "text": {"content": s[:1900]}}]


def _block_rich_text(text: str) -> list[dict[str, Any]]:
    """Rich text for **page body** blocks. Notion rejects ``paragraph``/``code``/``heading_2`` with empty ``rich_text``."""
    chunks = _as_rich_text(text)
    if chunks:
        return chunks
    return [{"type": "text", "text": {"content": "—"}}]


# Notion API: ``code.rich_text`` may not exceed 100 items (validation_error).
_NOTION_CODE_RICH_TEXT_MAX = 95


def _notion_code_blocks_from_markdown(text: str, language: str = "markdown") -> list[dict[str, Any]]:
    """Split markdown into one or more ``code`` blocks so each stays under Notion's rich_text cap."""
    t = (text or "").replace("\x00", "")
    if not t.strip():
        return [
            {
                "object": "block",
                "type": "code",
                "code": {"rich_text": [{"type": "text", "text": {"content": "—"}}], "language": language},
            }
        ]
    out: list[dict[str, Any]] = []
    i = 0
    n = len(t)
    max_window = 48000
    while i < n:
        remaining = n - i
        take = min(max_window, remaining)
        while take > 0:
            chunk = t[i : i + take]
            rt = _as_rich_text(chunk, strip_outer=False)
            if len(rt) <= _NOTION_CODE_RICH_TEXT_MAX:
                if not rt:
                    rt = [{"type": "text", "text": {"content": "—"}}]
                out.append(
                    {"object": "block", "type": "code", "code": {"rich_text": rt, "language": language}}
                )
                i += take
                break
            take //= 2
        else:
            # Pathological: one character still explodes rich_text count — escape literally.
            out.append(
                {
                    "object": "block",
                    "type": "code",
                    "code": {
                        "rich_text": [{"type": "text", "text": {"content": repr(t[i : i + 1])}}],
                        "language": language,
                    },
                }
            )
            i += 1
    return out


def _prop_type(db_props: dict[str, Any], name: str) -> str:
    meta = (db_props or {}).get(name) or {}
    t = meta.get("type")
    return str(t) if isinstance(t, str) else ""


def _title_property_name(db_props: dict[str, Any]) -> str | None:
    for name, meta in (db_props or {}).items():
        if isinstance(meta, dict) and meta.get("type") == "title":
            return name
    return None


def _notion_props_for_row(
    row: dict[str, Any],
    db_props: dict[str, Any],
    *,
    score_first_title: bool = False,
    compact_mode: bool = False,
) -> dict[str, Any]:
    """
    Write only properties that already exist in the Notion DB.
    Key CRM columns (when present): Status, Meets fund criteria (PASS/FAIL), Fail reason, Message ID, etc.
    """
    out: dict[str, Any] = {}

    title_prop = _title_property_name(db_props)
    if title_prop:
        out[title_prop] = {"title": _as_rich_text(_title(row, score_first=score_first_title))}
    if "Message ID" in db_props:
        out["Message ID"] = {"rich_text": _as_rich_text(str(row.get("message_id") or ""))}
    if "Status" in db_props:
        status_val = _crm_deal_status(row)
        t = _prop_type(db_props, "Status")
        if t == "select":
            out["Status"] = {"select": {"name": status_val}}
        else:
            out["Status"] = {"rich_text": _as_rich_text(status_val)}
    if not compact_mode and "Verdict" in db_props:
        v = str(row.get("gate1_verdict") or "")
        t = _prop_type(db_props, "Verdict")
        if t == "select":
            out["Verdict"] = {"select": {"name": v or "UNKNOWN"}}
        else:
            out["Verdict"] = {"rich_text": _as_rich_text(v)}
    if not compact_mode and "Recommendation" in db_props:
        out["Recommendation"] = {"rich_text": _as_rich_text(str(row.get("final_action") or row.get("gate2_recommendation") or ""))}
    if not compact_mode:
        for pname, value in (
            ("Debug Override Used", bool(row.get("debug_override_used"))),
            ("Test Case", bool(row.get("test_case"))),
        ):
            if pname in db_props:
                t = _prop_type(db_props, pname)
                if t == "checkbox":
                    out[pname] = {"checkbox": value}
                else:
                    out[pname] = {"rich_text": _as_rich_text("yes" if value else "no")}
    if "Sector" in db_props:
        sector_val = str(row.get("gate1_detected_sector") or "")
        t = _prop_type(db_props, "Sector")
        if t == "select":
            out["Sector"] = {"select": {"name": sector_val or "Unknown"}}
        else:
            out["Sector"] = {"rich_text": _as_rich_text(sector_val)}
    if not compact_mode and "Geography" in db_props:
        out["Geography"] = {"rich_text": _as_rich_text(str(row.get("gate1_detected_geography") or ""))}
    if not compact_mode and "Email" in db_props:
        out["Email"] = {"email": (str(row.get("sender_email") or "").strip() or None)}
    if not compact_mode and "Sender" in db_props:
        out["Sender"] = {"rich_text": _as_rich_text(str(row.get("sender_name") or ""))}
    if not compact_mode and "Mail Subject" in db_props:
        out["Mail Subject"] = {"rich_text": _as_rich_text(str(row.get("subject") or ""))}
    if "Received At" in db_props:
        d = _to_notion_date(row.get("created_at"))
        t = _prop_type(db_props, "Received At")
        if t == "date":
            out["Received At"] = {"date": {"start": d} if d else None}
        else:
            out["Received At"] = {"rich_text": _as_rich_text(d or "")}
    if not compact_mode and "PDF Filename" in db_props:
        out["PDF Filename"] = {"rich_text": _as_rich_text(str(row.get("pdf_filename") or ""))}
    if not compact_mode and "Has PDF" in db_props:
        has_pdf = bool(row.get("has_pdf"))
        t = _prop_type(db_props, "Has PDF")
        if t == "checkbox":
            out["Has PDF"] = {"checkbox": has_pdf}
        else:
            out["Has PDF"] = {"rich_text": _as_rich_text("yes" if has_pdf else "no")}
    if not compact_mode and "Gmail Link" in db_props:
        url = _gmail_message_url(str(row.get("message_id") or ""))
        t = _prop_type(db_props, "Gmail Link")
        if t == "url":
            out["Gmail Link"] = {"url": (url or None)}
        else:
            out["Gmail Link"] = {"rich_text": _as_rich_text(url)}
    if not compact_mode and "Source" in db_props:
        src = _source_label(row)
        t = _prop_type(db_props, "Source")
        if t == "select":
            out["Source"] = {"select": {"name": src}}
        else:
            out["Source"] = {"rich_text": _as_rich_text(src)}
    if not compact_mode and "Subject" in db_props:
        out["Subject"] = {"rich_text": _as_rich_text(str(row.get("subject") or ""))}
    if not compact_mode and "Created At" in db_props:
        created = str(row.get("created_at") or "").strip()
        out["Created At"] = {"rich_text": _as_rich_text(created)}
    if not compact_mode and "Updated At" in db_props:
        updated = str(row.get("updated_at") or "").strip()
        out["Updated At"] = {"rich_text": _as_rich_text(updated)}
    if not compact_mode and "Rejection Reason" in db_props:
        out["Rejection Reason"] = {"rich_text": _as_rich_text(str(row.get("gate1_rejection_reason") or ""))}
    founded_year, founders_summary, one_liner = ("", "", "")
    if not compact_mode:
        founded_year, founders_summary, one_liner = _profile_fields(row)
    if not compact_mode and "Founded Year" in db_props:
        t = _prop_type(db_props, "Founded Year")
        if t == "number":
            yr_num = None
            if founded_year.isdigit() and len(founded_year) == 4:
                yr_num = float(founded_year)
            out["Founded Year"] = {"number": yr_num}
        else:
            out["Founded Year"] = {"rich_text": _as_rich_text(founded_year)}
    if not compact_mode and "Founders" in db_props:
        out["Founders"] = {"rich_text": _as_rich_text(founders_summary)}
    if not compact_mode and "Product One-liner" in db_props:
        out["Product One-liner"] = {"rich_text": _as_rich_text(one_liner)}

    # ── Fund mandate criteria table fields (derived from persisted Gate2 facts) ─
    facts_obj: dict[str, Any] = {}
    try:
        raw = row.get("gate2_facts_json")
        facts_obj = json.loads(raw) if raw else {}
    except Exception:
        facts_obj = {}

    hq = str(facts_obj.get("geography") or row.get("gate1_detected_geography") or "").strip()
    hq_in_cee = _hq_in_cee_label(hq) == "YES"

    nat_pairs = _founder_nationalities_with_sources(facts_obj)
    nat_tokens = [n for n, _ in nat_pairs if n]
    founder_has_cee = bool(nat_tokens)

    # Stage normalized (use existing rule-based classifier).
    try:
        from agents.fund_decision import classify_stage
    except Exception:
        classify_stage = None
    stage_raw = str(facts_obj.get("stage") or "").strip()
    funding_round = str(facts_obj.get("funding_round") or "").strip()
    stage_norm = ""
    if classify_stage is not None:
        stage_norm = classify_stage(stage_raw, funding_rounds=[funding_round] if funding_round else None)
    else:
        stage_norm = (stage_raw or "").strip().lower()
    if not stage_norm or stage_norm == "unknown":
        stage_norm = "unknown"

    stage_ok = stage_norm in ("pre-seed", "seed", "seed-extension")
    meets = (hq_in_cee or founder_has_cee) and stage_ok

    cee_link = "Unknown"
    if hq_in_cee and founder_has_cee:
        cee_link = "Both"
    elif hq_in_cee:
        cee_link = "HQ"
    elif founder_has_cee:
        cee_link = "Founder"

    mandate_fail_brief = ""
    if not meets:
        mandate_fail_brief = _mandate_fail_brief(
            hq_in_cee=hq_in_cee,
            founder_has_cee=founder_has_cee,
            stage_ok=stage_ok,
            stage_norm=stage_norm,
        )

    # Write only if properties exist in DB.
    if "HQ" in db_props:
        out["HQ"] = {"rich_text": _as_rich_text(hq)}
    if "HQ in CEE" in db_props:
        t = _prop_type(db_props, "HQ in CEE")
        if t == "checkbox":
            out["HQ in CEE"] = {"checkbox": hq_in_cee}
        else:
            out["HQ in CEE"] = {"rich_text": _as_rich_text("YES" if hq_in_cee else "NO")}
    hq_cee_tri = _hq_in_cee_label(hq)
    if "HQ (CEE)" in db_props:
        t = _prop_type(db_props, "HQ (CEE)")
        if t == "select":
            out["HQ (CEE)"] = {"select": {"name": hq_cee_tri}}
        else:
            out["HQ (CEE)"] = {"rich_text": _as_rich_text(hq_cee_tri)}
    nat_hint_for_cee = str(facts_obj.get("founder_nationality_hint") or "").strip()
    inferred_for_cee = str(facts_obj.get("inferred_signals") or "")
    nat_cee_tri = _founder_cee_nationality_label(nat_hint_for_cee, inferred_for_cee)
    if "Nationality (CEE)" in db_props:
        t = _prop_type(db_props, "Nationality (CEE)")
        if t == "select":
            out["Nationality (CEE)"] = {"select": {"name": nat_cee_tri}}
        else:
            out["Nationality (CEE)"] = {"rich_text": _as_rich_text(nat_cee_tri)}
    stage_mandate = _mandate_stage_bucket(stage_norm)
    if "Stage (mandate)" in db_props:
        t = _prop_type(db_props, "Stage (mandate)")
        if t == "select":
            out["Stage (mandate)"] = {"select": {"name": stage_mandate}}
        else:
            out["Stage (mandate)"] = {"rich_text": _as_rich_text(stage_mandate)}
    if "Founder nationalities" in db_props:
        t = _prop_type(db_props, "Founder nationalities")
        if t == "multi_select":
            out["Founder nationalities"] = {"multi_select": [{"name": n} for n in nat_tokens[:8]]}
        else:
            out["Founder nationalities"] = {"rich_text": _as_rich_text("; ".join(nat_tokens[:8]))}
    if "Nationality sources" in db_props:
        # Render: Latvian ([crunchbase.com](...)); Polish ([linkedin.com](...))
        items = []
        for n, u in nat_pairs[:8]:
            if u:
                host = ""
                try:
                    host = urllib.parse.urlparse(u).netloc.lstrip("www.")
                except Exception:
                    host = ""
                label = host or "source"
                items.append(f"{n} ([{label}]({u}))")
            else:
                items.append(f"{n} (source: inferred)")
        out["Nationality sources"] = {"rich_text": _as_rich_text("; ".join(items))}
    if "CEE link" in db_props:
        t = _prop_type(db_props, "CEE link")
        if t == "select":
            out["CEE link"] = {"select": {"name": cee_link}}
        else:
            out["CEE link"] = {"rich_text": _as_rich_text(cee_link)}
    if "Stage" in db_props:
        t = _prop_type(db_props, "Stage")
        if t == "select":
            out["Stage"] = {"select": {"name": stage_norm}}
        else:
            out["Stage"] = {"rich_text": _as_rich_text(stage_norm)}
    for pname, v in (
        ("Funding round", funding_round),
        ("Money raised", str(facts_obj.get("funding_amount") or "").strip()),
        ("Funding date", str(facts_obj.get("funding_date") or "").strip()),
        ("Valuation", str(facts_obj.get("valuation") or "").strip()),
    ):
        if pname in db_props:
            out[pname] = {"rich_text": _as_rich_text(v)}
    meets_key = _db_prop_key(db_props, "Meets fund criteria", _legacy_notion_meets_criteria_property())
    if meets_key:
        t = _prop_type(db_props, meets_key)
        val = "PASS" if meets else "FAIL"
        if t == "select":
            out[meets_key] = {"select": {"name": val}}
        else:
            out[meets_key] = {"rich_text": _as_rich_text(val)}
    if "Fail reason" in db_props:
        fail_cell = mandate_fail_brief if not meets else ""
        out["Fail reason"] = {"rich_text": _as_rich_text(fail_cell)}

    # ── Traction signals (deterministic) ──────────────────────────────────
    traction_obj: dict[str, Any] = {}
    try:
        raw_tr = row.get("traction_json")
        if raw_tr:
            parsed = json.loads(raw_tr)
            if isinstance(parsed, dict):
                traction_obj = parsed
    except Exception:
        traction_obj = {}
    tr_verdict = str(traction_obj.get("verdict") or "").upper()
    tr_deck = list(traction_obj.get("deck") or [])
    tr_web = list(traction_obj.get("website") or [])
    if "Traction" in db_props:
        t = _prop_type(db_props, "Traction")
        name = tr_verdict if tr_verdict in ("BOTH", "DECK_ONLY", "WEBSITE_ONLY", "NONE") else "NONE"
        if t == "select":
            out["Traction"] = {"select": {"name": name}}
        else:
            out["Traction"] = {"rich_text": _as_rich_text(name)}
    if "Traction (deck)" in db_props:
        t = _prop_type(db_props, "Traction (deck)")
        v = bool(tr_deck)
        if t == "checkbox":
            out["Traction (deck)"] = {"checkbox": v}
        else:
            out["Traction (deck)"] = {"rich_text": _as_rich_text("YES" if v else "NO")}
    if "Traction (website)" in db_props:
        t = _prop_type(db_props, "Traction (website)")
        v = bool(tr_web)
        if t == "checkbox":
            out["Traction (website)"] = {"checkbox": v}
        else:
            out["Traction (website)"] = {"rich_text": _as_rich_text("YES" if v else "NO")}
    return out


def _find_page_by_message_id(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    message_id: str,
) -> str | None:
    payload = {
        "filter": {"property": "Message ID", "rich_text": {"equals": message_id}},
        "page_size": 1,
    }
    r = client.post(
        f"https://api.notion.com/v1/databases/{database_id}/query",
        headers=_headers(api_key),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    results = (r.json() or {}).get("results") or []
    if not results:
        return None
    return results[0].get("id")


def _find_page_by_title_and_date(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    db_props: dict[str, Any],
    row: dict[str, Any],
) -> str | None:
    title_prop = _title_property_name(db_props)
    if not title_prop:
        return None
    title_val = _title(row).strip()
    if not title_val:
        return None
    payload: dict[str, Any] = {
        "filter": {
            "property": title_prop,
            "title": {"equals": title_val},
        },
        "page_size": 10,
    }
    r = client.post(
        f"https://api.notion.com/v1/databases/{database_id}/query",
        headers=_headers(api_key),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    results = (r.json() or {}).get("results") or []
    if not results:
        return None
    recv = _to_notion_date(row.get("created_at"))
    if not recv:
        return results[0].get("id")
    # If Received At exists, disambiguate duplicates by date.
    if "Received At" in db_props:
        for p in results:
            props = p.get("properties") or {}
            d = ((props.get("Received At") or {}).get("date") or {}).get("start")
            if d == recv:
                return p.get("id")
    return results[0].get("id")


def _discover_database_hints(client: httpx.Client, *, api_key: str) -> str:
    """Return a short list of database IDs visible to this integration."""
    try:
        r = client.post(
            "https://api.notion.com/v1/search",
            headers=_headers(api_key),
            json={"filter": {"property": "object", "value": "database"}, "page_size": 10},
            timeout=30,
        )
        r.raise_for_status()
        rows = (r.json() or {}).get("results") or []
        hints: list[str] = []
        for db in rows[:5]:
            db_id = str(db.get("id") or "")
            title_blocks = (db.get("title") or [])
            title = ""
            if title_blocks:
                title = str(title_blocks[0].get("plain_text") or "")
            hints.append(f"{title or '(untitled)'}: {db_id}")
        return "; ".join(hints) if hints else "no databases visible via /search"
    except Exception as e:
        return f"unable to fetch database hints ({e})"


def _notion_refresh_key_select_options(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    db_props: dict[str, Any],
) -> dict[str, Any]:
    """Align select option lists on an existing database (CRM Status)."""
    patch: dict[str, Any] = {}
    st = (db_props or {}).get("Status")
    if isinstance(st, dict) and st.get("type") == "select":
        patch["Status"] = {
            "select": {
                "options": [
                    {"name": "To be reviewed", "color": "yellow"},
                    {"name": "Rejected", "color": "red"},
                    {"name": "Schedule an intro call", "color": "blue"},
                    {"name": "Due diligence", "color": "purple"},
                ]
            }
        }
    if not patch:
        return db_props or {}
    try:
        rr = client.patch(
            f"https://api.notion.com/v1/databases/{database_id}",
            headers=_headers(api_key),
            json={"properties": patch},
            timeout=45,
        )
        rr.raise_for_status()
        return (rr.json() or {}).get("properties") or db_props
    except Exception:
        return db_props


def _ensure_notion_schema(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    db_props: dict[str, Any],
    compact_mode: bool = False,
) -> dict[str, Any]:
    """
    Add a minimal useful schema for pipeline ops if properties are missing.
    Only adds missing properties; does not alter existing types.
    """
    if compact_mode:
        wanted: dict[str, dict[str, Any]] = {
            "Message ID": {"rich_text": {}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "To be reviewed", "color": "yellow"},
                        {"name": "Rejected", "color": "red"},
                        {"name": "Schedule an intro call", "color": "blue"},
                        {"name": "Due diligence", "color": "purple"},
                    ]
                }
            },
            "Sector": {"select": {"options": []}},
            "Received At": {"date": {}},
            # Fund mandate criteria table (always ensured in compact mode)
            "HQ": {"rich_text": {}},
            "HQ in CEE": {"checkbox": {}},
            "HQ (CEE)": {
                "select": {
                    "options": [
                        {"name": "YES", "color": "green"},
                        {"name": "NO", "color": "red"},
                        {"name": "UNCERTAIN", "color": "gray"},
                    ]
                }
            },
            "Founder nationalities": {"multi_select": {"options": []}},
            "Nationality sources": {"rich_text": {}},
            "Nationality (CEE)": {
                "select": {
                    "options": [
                        {"name": "YES", "color": "green"},
                        {"name": "NO", "color": "red"},
                        {"name": "UNCERTAIN", "color": "gray"},
                    ]
                }
            },
            "CEE link": {
                "select": {
                    "options": [
                        {"name": "HQ", "color": "blue"},
                        {"name": "Founder", "color": "purple"},
                        {"name": "Both", "color": "green"},
                        {"name": "Unknown", "color": "gray"},
                    ]
                }
            },
            "Stage": {
                "select": {
                    "options": [
                        {"name": "pre-seed", "color": "green"},
                        {"name": "seed", "color": "green"},
                        {"name": "seed-extension", "color": "green"},
                        {"name": "late-seed", "color": "yellow"},
                        {"name": "series-a", "color": "yellow"},
                        {"name": "series-a-ready", "color": "yellow"},
                        {"name": "series-b+", "color": "red"},
                        {"name": "unknown", "color": "gray"},
                    ]
                }
            },
            "Stage (mandate)": {
                "select": {
                    "options": [
                        {"name": "PRE-SEED", "color": "green"},
                        {"name": "SEED", "color": "green"},
                        {"name": "OTHER", "color": "orange"},
                    ]
                }
            },
            "Funding round": {"rich_text": {}},
            "Money raised": {"rich_text": {}},
            "Funding date": {"rich_text": {}},
            "Valuation": {"rich_text": {}},
            "Meets fund criteria": {
                "select": {
                    "options": [
                        {"name": "PASS", "color": "green"},
                        {"name": "FAIL", "color": "red"},
                    ]
                }
            },
            "Fail reason": {"rich_text": {}},
            # Traction (deterministic)
            "Traction": {
                "select": {
                    "options": [
                        {"name": "BOTH", "color": "green"},
                        {"name": "DECK_ONLY", "color": "yellow"},
                        {"name": "WEBSITE_ONLY", "color": "yellow"},
                        {"name": "NONE", "color": "red"},
                    ]
                }
            },
            "Traction (deck)": {"checkbox": {}},
            "Traction (website)": {"checkbox": {}},
        }
    else:
        wanted = {
            "Message ID": {"rich_text": {}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "To be reviewed", "color": "yellow"},
                        {"name": "Rejected", "color": "red"},
                        {"name": "Schedule an intro call", "color": "blue"},
                        {"name": "Due diligence", "color": "purple"},
                    ]
                }
            },
            "Source": {"rich_text": {}},
            "Recommendation": {"rich_text": {}},
            "Verdict": {"rich_text": {}},
            "Sector": {"select": {"options": []}},
            "Geography": {"rich_text": {}},
            "Founded Year": {"rich_text": {}},
            "Founders": {"rich_text": {}},
            "Product One-liner": {"rich_text": {}},
            "Email": {"email": {}},
            "Sender": {"rich_text": {}},
            "Mail Subject": {"rich_text": {}},
            "Received At": {"date": {}},
            "PDF Filename": {"rich_text": {}},
            "Has PDF": {"checkbox": {}},
            "Gmail Link": {"url": {}},
            "Subject": {"rich_text": {}},
            "Updated At": {"rich_text": {}},
            "Created At": {"rich_text": {}},
            "Rejection Reason": {"rich_text": {}},
            "Debug Override Used": {"checkbox": {}},
            "Test Case": {"checkbox": {}},
            # Fund mandate criteria table (lean, deterministic)
            "HQ": {"rich_text": {}},
            "HQ in CEE": {"checkbox": {}},
            "HQ (CEE)": {
                "select": {
                    "options": [
                        {"name": "YES", "color": "green"},
                        {"name": "NO", "color": "red"},
                        {"name": "UNCERTAIN", "color": "gray"},
                    ]
                }
            },
            "Founder nationalities": {"multi_select": {"options": []}},
            "Nationality sources": {"rich_text": {}},
            "Nationality (CEE)": {
                "select": {
                    "options": [
                        {"name": "YES", "color": "green"},
                        {"name": "NO", "color": "red"},
                        {"name": "UNCERTAIN", "color": "gray"},
                    ]
                }
            },
            "CEE link": {
                "select": {
                    "options": [
                        {"name": "HQ", "color": "blue"},
                        {"name": "Founder", "color": "purple"},
                        {"name": "Both", "color": "green"},
                        {"name": "Unknown", "color": "gray"},
                    ]
                }
            },
            "Stage": {
                "select": {
                    "options": [
                        {"name": "pre-seed", "color": "green"},
                        {"name": "seed", "color": "green"},
                        {"name": "seed-extension", "color": "green"},
                        {"name": "late-seed", "color": "yellow"},
                        {"name": "series-a", "color": "yellow"},
                        {"name": "series-a-ready", "color": "yellow"},
                        {"name": "series-b+", "color": "red"},
                        {"name": "unknown", "color": "gray"},
                    ]
                }
            },
            "Stage (mandate)": {
                "select": {
                    "options": [
                        {"name": "PRE-SEED", "color": "green"},
                        {"name": "SEED", "color": "green"},
                        {"name": "OTHER", "color": "orange"},
                    ]
                }
            },
            "Funding round": {"rich_text": {}},
            "Money raised": {"rich_text": {}},
            "Funding date": {"rich_text": {}},
            "Valuation": {"rich_text": {}},
            "Meets fund criteria": {
                "select": {
                    "options": [
                        {"name": "PASS", "color": "green"},
                        {"name": "FAIL", "color": "red"},
                    ]
                }
            },
            "Fail reason": {"rich_text": {}},
            # Traction (deterministic regex over deck OCR + website crawl)
            "Traction": {
                "select": {
                    "options": [
                        {"name": "BOTH", "color": "green"},
                        {"name": "DECK_ONLY", "color": "yellow"},
                        {"name": "WEBSITE_ONLY", "color": "yellow"},
                        {"name": "NONE", "color": "red"},
                    ]
                }
            },
            "Traction (deck)": {"checkbox": {}},
            "Traction (website)": {"checkbox": {}},
        }
    to_add: dict[str, Any] = {}
    for name, conf in wanted.items():
        if name not in db_props:
            to_add[name] = conf
    if not to_add:
        return db_props

    rr = client.patch(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers=_headers(api_key),
        json={"properties": to_add},
        timeout=30,
    )
    try:
        rr.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = rr.json()
            detail = str(body.get("message") or body)[:400]
        except Exception:
            detail = (rr.text or "")[:400]
        raise RuntimeError(f"Failed to extend Notion schema: {detail}") from e
    return (rr.json() or {}).get("properties") or db_props


def _prune_notion_schema(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    db_props: dict[str, Any],
    compact_mode: bool = True,
) -> tuple[dict[str, Any], int]:
    """
    Remove extra properties from Notion database to keep a lean operational table.
    Keeps only compact operating columns.
    """
    title_prop = _title_property_name(db_props) or "Name"
    if compact_mode:
        keep = {
            title_prop,
            "Message ID",
            "Status",
            "Sector",
            "Received At",
            # Criteria table
            "HQ",
            "HQ in CEE",
            "HQ (CEE)",
            "Founder nationalities",
            "Nationality sources",
            "Nationality (CEE)",
            "CEE link",
            "Stage",
            "Stage (mandate)",
            "Funding round",
            "Money raised",
            "Funding date",
            "Valuation",
            "Meets fund criteria",
            "Fail reason",
            "Traction",
            "Traction (deck)",
            "Traction (website)",
        }
    else:
        keep = {title_prop}

    to_delete: dict[str, Any] = {}
    for name in (db_props or {}).keys():
        if name not in keep:
            to_delete[name] = None
    if not to_delete:
        return db_props, 0
    log.info(
        "Notion prune: removing %d properties not in compact allowlist: %s",
        len(to_delete),
        ", ".join(sorted(to_delete.keys())[:40]) + ("…" if len(to_delete) > 40 else ""),
    )

    rr = client.patch(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers=_headers(api_key),
        json={"properties": to_delete},
        timeout=30,
    )
    try:
        rr.raise_for_status()
    except httpx.HTTPStatusError as e:
        detail = ""
        try:
            body = rr.json()
            detail = str(body.get("message") or body)[:400]
        except Exception:
            detail = (rr.text or "")[:400]
        raise RuntimeError(f"Failed to prune Notion columns: {detail}") from e
    n = len(to_delete)
    return (rr.json() or {}).get("properties") or db_props, n


_DEPRECATED_NOTION_COLUMNS = (
    "Score",
    "Investment thesis",
    "Investment thesis rationale",
    # Removed from pipeline table (detail stays in page body / SQLite where needed)
    "Fund Fit Decision",
    "Deck Evidence Decision",
    "Deck Evidence Score",
    "Generic VC Interest",
    "Auth Risk",
    "External Opportunity Score",
    "Fund Fit Score",
    "Traction signals",
    "Final Action",
    "Screening Depth",
    "Mandate rationale",
    _legacy_notion_fit_decision_property(),
    _legacy_notion_fit_score_property(),
)

_DEPRECATED_NOTION_NAMES_LOWER = frozenset(n.lower() for n in _DEPRECATED_NOTION_COLUMNS)


def _notion_refresh_database_properties(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
) -> dict[str, Any]:
    r = client.get(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers=_headers(api_key),
        timeout=30,
    )
    r.raise_for_status()
    return (r.json() or {}).get("properties") or {}


def _remove_deprecated_notion_columns(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    db_props: dict[str, Any],
) -> dict[str, Any]:
    """Remove legacy DB properties via Notion API (PATCH property → null)."""
    if os.getenv("NOTION_REMOVE_DEPRECATED", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return db_props
    # Match case-insensitively: Notion shows one casing; API keys can differ.
    to_drop = [k for k in (db_props or {}) if k.lower() in _DEPRECATED_NOTION_NAMES_LOWER]
    if not to_drop:
        return db_props
    log.info("Removing deprecated Notion database properties: %s", ", ".join(sorted(to_drop)))

    def _patch_drop(names: list[str]) -> httpx.Response:
        return client.patch(
            f"https://api.notion.com/v1/databases/{database_id}",
            headers=_headers(api_key),
            json={"properties": {name: None for name in names}},
            timeout=45,
        )

    rr = _patch_drop(to_drop)
    try:
        rr.raise_for_status()
    except httpx.HTTPStatusError:
        # Large batch may hit validation limits or one blocked property; drop one-by-one.
        removed: list[str] = []
        failed: list[tuple[str, str]] = []
        for name in to_drop:
            one = _patch_drop([name])
            try:
                one.raise_for_status()
                removed.append(name)
            except httpx.HTTPStatusError:
                detail = ""
                try:
                    body = one.json()
                    detail = str(body.get("message") or body)[:400]
                except Exception:
                    detail = (one.text or "")[:400]
                failed.append((name, detail))
                log.warning("Could not remove Notion property %r: %s", name, detail)
        if failed and not removed:
            detail = "; ".join(f"{n}: {d}" for n, d in failed[:5])
            raise RuntimeError(
                f"Failed to remove deprecated Notion columns (tried one-by-one): {detail}"
            )
        if failed:
            log.warning(
                "Some deprecated Notion columns could not be removed (integration permissions or Notion restrictions): %s",
                ", ".join(n for n, _ in failed),
            )
    try:
        return _notion_refresh_database_properties(client, api_key=api_key, database_id=database_id)
    except Exception as e:
        log.warning("Notion schema re-fetch after deprecations failed: %s", e)
        return db_props


def _archive_pages_with_message_prefix(
    client: httpx.Client,
    *,
    api_key: str,
    database_id: str,
    prefix: str,
) -> int:
    """Archive rows whose Message ID starts with a prefix (e.g. test_)."""
    archived = 0
    next_cursor: str | None = None
    while True:
        payload: dict[str, Any] = {
            "filter": {"property": "Message ID", "rich_text": {"starts_with": prefix}},
            "page_size": 100,
        }
        if next_cursor:
            payload["start_cursor"] = next_cursor
        r = client.post(
            f"https://api.notion.com/v1/databases/{database_id}/query",
            headers=_headers(api_key),
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        body = r.json() or {}
        results = body.get("results") or []
        for page in results:
            pid = page.get("id")
            if not pid:
                continue
            rr = client.patch(
                f"https://api.notion.com/v1/pages/{pid}",
                headers=_headers(api_key),
                json={"archived": True},
                timeout=30,
            )
            rr.raise_for_status()
            archived += 1
        if not body.get("has_more"):
            break
        next_cursor = body.get("next_cursor")
    return archived


def sync_pipeline_to_notion(
    days: int = 30,
    *,
    prune_test_rows: bool = False,
    ensure_schema: bool = False,
    prune_columns: bool = False,
    schema_only: bool = False,
) -> SyncStats:
    api_key = os.getenv("NOTION_API_KEY", "").strip()
    db_id_raw = os.getenv("NOTION_DATABASE_ID", "").strip()
    db_id = _normalize_database_id(db_id_raw)
    if not api_key or not db_id:
        raise RuntimeError("Missing NOTION_API_KEY or NOTION_DATABASE_ID in environment.")

    compact_mode = os.getenv("NOTION_COMPACT_MODE", "1").strip().lower() in ("1", "true", "yes", "on")
    score_first_title = os.getenv("NOTION_TITLE_SCORE_PREFIX", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    pruned_property_count: int | None = None
    if schema_only:
        rows: list[dict[str, Any]] = []
        stats = SyncStats(scanned=0)
    else:
        rows = get_deals_for_notion(days=days)
        include_tests = os.getenv("NOTION_INCLUDE_TEST_DEALS", "0").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not include_tests:
            rows = [r for r in rows if not str(r.get("message_id") or "").lower().startswith("test_")]
        stats = SyncStats(scanned=len(rows))

    with httpx.Client() as client:
        schema_resp = client.get(
            f"https://api.notion.com/v1/databases/{db_id}",
            headers=_headers(api_key),
            timeout=30,
        )
        try:
            schema_resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = ""
            try:
                body = schema_resp.json()
                detail = str(body.get("message") or body)[:300]
            except Exception:
                detail = (schema_resp.text or "")[:300]
            hints = _discover_database_hints(client, api_key=api_key)
            raise RuntimeError(
                "Notion lookup failed. Use DATABASE id (not Data Source id), and ensure DB is shared with integration. "
                f"normalized_id={db_id}. notion_error={detail}. visible_databases={hints}"
            ) from e
        db_props = (schema_resp.json() or {}).get("properties") or {}
        if ensure_schema:
            db_props = _rename_notion_legacy_property_names(
                client,
                api_key=api_key,
                database_id=db_id,
                db_props=db_props,
            )
            db_props = _drop_superseded_notion_properties(
                client,
                api_key=api_key,
                database_id=db_id,
                db_props=db_props,
            )
            db_props = _ensure_notion_schema(
                client,
                api_key=api_key,
                database_id=db_id,
                db_props=db_props,
                compact_mode=compact_mode,
            )
        db_props = _notion_refresh_key_select_options(
            client,
            api_key=api_key,
            database_id=db_id,
            db_props=db_props,
        )
        if prune_columns:
            db_props, pruned_property_count = _prune_notion_schema(
                client,
                api_key=api_key,
                database_id=db_id,
                db_props=db_props,
                compact_mode=compact_mode,
            )
        db_props = _remove_deprecated_notion_columns(
            client,
            api_key=api_key,
            database_id=db_id,
            db_props=db_props,
        )
        _ensure_pipeline_table_view_layout(
            client,
            api_key=api_key,
            database_id=db_id,
            db_props=db_props,
        )
        if prune_test_rows:
            _archive_pages_with_message_prefix(
                client,
                api_key=api_key,
                database_id=db_id,
                prefix="test_",
            )

        if schema_only:
            if prune_columns:
                stats.pruned_property_count = pruned_property_count
            return stats

        for row in rows:
            props = _notion_props_for_row(
                row,
                db_props,
                score_first_title=score_first_title,
                compact_mode=compact_mode,
            )
            if not props:
                stats.skipped += 1
                continue

            msg_id = str(row.get("message_id") or "").strip()
            page_id = None
            if "Message ID" in db_props and msg_id:
                page_id = _find_page_by_message_id(
                    client,
                    api_key=api_key,
                    database_id=db_id,
                    message_id=msg_id,
                )
            if not page_id:
                page_id = _find_page_by_title_and_date(
                    client,
                    api_key=api_key,
                    database_id=db_id,
                    db_props=db_props,
                    row=row,
                )
            pid: str | None = page_id
            if page_id:
                rr = client.patch(
                    f"https://api.notion.com/v1/pages/{page_id}",
                    headers=_headers(api_key),
                    json={"properties": props},
                    timeout=30,
                )
                try:
                    rr.raise_for_status()
                except httpx.HTTPStatusError as e:
                    detail = ""
                    try:
                        body = rr.json()
                        detail = str(body.get("message") or body)[:400]
                    except Exception:
                        detail = (rr.text or "")[:400]
                    raise RuntimeError(
                        f"Notion update failed for key={msg_id or _title(row)}: {detail}"
                    ) from e
                stats.updated += 1
            else:
                rr = client.post(
                    "https://api.notion.com/v1/pages",
                    headers=_headers(api_key),
                    json={
                        "parent": {"database_id": db_id},
                        "properties": props,
                    },
                    timeout=30,
                )
                try:
                    rr.raise_for_status()
                except httpx.HTTPStatusError as e:
                    detail = ""
                    try:
                        body = rr.json()
                        detail = str(body.get("message") or body)[:400]
                    except Exception:
                        detail = (rr.text or "")[:400]
                    raise RuntimeError(
                        f"Notion create failed for key={msg_id or _title(row)}: {detail}"
                    ) from e
                stats.created += 1
                pid = (rr.json() or {}).get("id")

            blocks, val_out, _reason, snap = _prepare_notion_memo_for_row(row)
            _dump_notion_artifacts(snap, blocks)
            patch_ok = _ensure_page_summary_blocks(
                client,
                api_key=api_key,
                page_id=pid,
                desired_blocks=blocks,
            )
            _finalize_notion_sync_status(
                str(row.get("message_id") or ""),
                validation=val_out,
                children_patch_ok=patch_ok,
            )
            _persist_notion_reconciliation_to_sqlite(row)

            page_snapshot_on = os.getenv("NOTION_LEGACY_MD_SUBPAGE", "0").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            legacy_md = str(row.get("gate2_snapshot_md") or "").strip()
            if page_snapshot_on and legacy_md:
                _upsert_page_snapshot(
                    client,
                    api_key=api_key,
                    page_id=pid,
                    message_id=msg_id or _title(row),
                    snapshot_text=legacy_md,
                )

    if prune_columns:
        stats.pruned_property_count = pruned_property_count
    return stats


def sync_one_deal_to_notion(
    message_id: str,
    *,
    ensure_schema: bool = False,
    prune_columns: bool = False,
) -> SyncStats:
    """
    Upsert exactly one deal row to Notion.
    Useful for auto-sync right after processing an email, without scanning N days.
    """
    api_key = os.getenv("NOTION_API_KEY", "").strip()
    db_id_raw = os.getenv("NOTION_DATABASE_ID", "").strip()
    db_id = _normalize_database_id(db_id_raw)
    if not api_key or not db_id:
        raise RuntimeError("Missing NOTION_API_KEY or NOTION_DATABASE_ID in environment.")

    compact_mode = os.getenv("NOTION_COMPACT_MODE", "1").strip().lower() in ("1", "true", "yes", "on")
    score_first_title = os.getenv("NOTION_TITLE_SCORE_PREFIX", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    pruned_property_count: int | None = None

    with httpx.Client() as client:
        schema_resp = client.get(
            f"https://api.notion.com/v1/databases/{db_id}",
            headers=_headers(api_key),
            timeout=30,
        )
        schema_resp.raise_for_status()
        db_props = (schema_resp.json() or {}).get("properties") or {}
        if ensure_schema:
            db_props = _rename_notion_legacy_property_names(
                client,
                api_key=api_key,
                database_id=db_id,
                db_props=db_props,
            )
            db_props = _drop_superseded_notion_properties(
                client,
                api_key=api_key,
                database_id=db_id,
                db_props=db_props,
            )
            db_props = _ensure_notion_schema(
                client,
                api_key=api_key,
                database_id=db_id,
                db_props=db_props,
                compact_mode=compact_mode,
            )
        db_props = _notion_refresh_key_select_options(
            client,
            api_key=api_key,
            database_id=db_id,
            db_props=db_props,
        )
        if prune_columns:
            db_props, pruned_property_count = _prune_notion_schema(
                client,
                api_key=api_key,
                database_id=db_id,
                db_props=db_props,
                compact_mode=compact_mode,
            )
        db_props = _remove_deprecated_notion_columns(
            client,
            api_key=api_key,
            database_id=db_id,
            db_props=db_props,
        )
        _ensure_pipeline_table_view_layout(
            client,
            api_key=api_key,
            database_id=db_id,
            db_props=db_props,
        )

        row = get_deal_for_notion(message_id)
        if not row:
            return SyncStats(
                scanned=0,
                skipped=1,
                pruned_property_count=pruned_property_count if prune_columns else None,
            )
        stats = SyncStats(
            scanned=1,
            pruned_property_count=pruned_property_count if prune_columns else None,
        )

        props = _notion_props_for_row(
            row,
            db_props,
            score_first_title=score_first_title,
            compact_mode=compact_mode,
        )
        if not props:
            stats.skipped += 1
            return stats

        msg_id = str(row.get("message_id") or "").strip()
        page_id = None
        if "Message ID" in db_props and msg_id:
            page_id = _find_page_by_message_id(
                client,
                api_key=api_key,
                database_id=db_id,
                message_id=msg_id,
            )
        if not page_id:
            page_id = _find_page_by_title_and_date(
                client,
                api_key=api_key,
                database_id=db_id,
                db_props=db_props,
                row=row,
            )
        pid: str | None = page_id
        if page_id:
            rr = client.patch(
                f"https://api.notion.com/v1/pages/{page_id}",
                headers=_headers(api_key),
                json={"properties": props},
                timeout=30,
            )
            rr.raise_for_status()
            stats.updated += 1
        else:
            rr = client.post(
                "https://api.notion.com/v1/pages",
                headers=_headers(api_key),
                json={"parent": {"database_id": db_id}, "properties": props},
                timeout=30,
            )
            rr.raise_for_status()
            stats.created += 1
            pid = (rr.json() or {}).get("id")

        blocks, val_out, _reason, snap = _prepare_notion_memo_for_row(row)
        _dump_notion_artifacts(snap, blocks)
        patch_ok = _ensure_page_summary_blocks(
            client,
            api_key=api_key,
            page_id=pid,
            desired_blocks=blocks,
        )
        _finalize_notion_sync_status(
            str(row.get("message_id") or ""),
            validation=val_out,
            children_patch_ok=patch_ok,
        )
        _persist_notion_reconciliation_to_sqlite(row)

        page_snapshot_on = os.getenv("NOTION_LEGACY_MD_SUBPAGE", "0").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        legacy_md = str(row.get("gate2_snapshot_md") or "").strip()
        if page_snapshot_on and legacy_md:
            _upsert_page_snapshot(
                client,
                api_key=api_key,
                page_id=pid,
                message_id=msg_id or _title(row),
                snapshot_text=legacy_md,
            )
    return stats


def _plain_text_of_rich_text(rt: Any) -> str:
    if not rt:
        return ""
    if isinstance(rt, list) and rt:
        first = rt[0] or {}
        return str(first.get("plain_text") or first.get("text", {}).get("content") or "")
    return ""


def _block_plain_text(block: dict[str, Any]) -> str:
    if not block or not isinstance(block, dict):
        return ""
    t = str(block.get("type") or "")
    payload = block.get(t) or {}
    if not isinstance(payload, dict):
        return ""
    return _plain_text_of_rich_text(payload.get("rich_text"))


def _heading2_plain(block: dict[str, Any]) -> str:
    if (block.get("type") or "") != "heading_2":
        return ""
    return _plain_text_of_rich_text((block.get("heading_2") or {}).get("rich_text")).strip()


_NOTION_MANAGED_MEMO_HEADINGS: frozenset[str] = frozenset(
    {
        "🕒 Generated",
        "🏢 Company snapshot",
        "💼 Business",
        "📚 Sources",
        "📈 Traction signals",
        "📧 EMAIL.MD (inbound)",
        "🧾 PITCH DECK.MD (OCR)",
        "🧾 DECK.MD (OCR)",  # legacy section title
        "🌐 WEBSITE.MD (crawl)",
        "🎙 Founder calls",
        "🔧 Legenda źródeł i kosztów",
        "⚡ Decision",
        "🧭 CEE Heritage",
        "🧾 Facts",
        "🧠 Product & Business",
        "📈 Strengths",
        "⚠️ Risks",
        "⚠️ Screening sync (diagnostic)",  # render_notion_diagnostic_blocks
        "🧾 Sources",
        "⚡ 0. Decision",
        "🧭 1. Investment Fit",
        "🧾 2. Snapshot",
        "🧠 3. Product & Business",
        "📈 4. Upside",
        "⚠️ 5. Risks",
        "❓ 6. Open Questions",
        "🧾 7. Evidence",
    }
)


def _list_all_page_children(client: httpx.Client, page_id: str, api_key: str) -> list[dict[str, Any]]:
    """Paginate Notion block children (API caps page_size at 100)."""
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {"page_size": "100"}
        if cursor:
            params["start_cursor"] = cursor
        r = client.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=_headers(api_key),
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json() or {}
        out.extend(data.get("results") or [])
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return out


def _collect_managed_memo_block_ids(children: list[dict[str, Any]]) -> list[str]:
    """IDs to archive: loose blocks before the memo + every memo section (h2 in set + body until next h2)."""
    ids: list[str] = []
    n = len(children)
    first_managed: int | None = None
    for idx, b in enumerate(children):
        if _heading2_plain(b) in _NOTION_MANAGED_MEMO_HEADINGS:
            first_managed = idx
            break
    if first_managed is None:
        return ids
    for k in range(first_managed):
        bid = children[k].get("id")
        if bid:
            ids.append(str(bid))
    i = first_managed
    while i < n:
        b = children[i]
        h = _heading2_plain(b)
        if h in _NOTION_MANAGED_MEMO_HEADINGS:
            j = i
            while j < n:
                bid = children[j].get("id")
                if bid:
                    ids.append(str(bid))
                j += 1
                if j < n and (children[j].get("type") or "") == "heading_2":
                    break
            i = j
            continue
        i += 1
    return ids


def _archive_blocks(client: httpx.Client, api_key: str, block_ids: list[str]) -> bool:
    """Archive blocks; return False if any patch failed (caller must not append new memo)."""
    ok = True
    for bid in block_ids:
        try:
            client.patch(
                f"https://api.notion.com/v1/blocks/{bid}",
                headers=_headers(api_key),
                json={"archived": True},
                timeout=30,
            ).raise_for_status()
        except Exception:
            ok = False
    return ok


def _upsert_page_snapshot(
    client: httpx.Client,
    *,
    api_key: str,
    page_id: str | None,
    message_id: str,
    snapshot_text: str,
) -> None:
    """
    Disabled: the structured 7-section memo (built by render_notion_blocks)
    fully replaces this auto-snapshot block. Kept as no-op for backward compat
    with callers; archives any existing marker block on the page so old runs
    don't leave duplicate snapshots behind.
    """
    if not page_id:
        return
    # Archive any pre-existing "VC Snapshot (auto)" marker block to clean up legacy pages.
    try:
        r = client.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
            headers=_headers(api_key),
            timeout=30,
        )
        r.raise_for_status()
        for b in (r.json() or {}).get("results") or []:
            if (b.get("type") or "") != "paragraph":
                continue
            plain = _plain_text_of_rich_text((b.get("paragraph") or {}).get("rich_text"))
            if plain.startswith("VC Snapshot (auto)"):
                bid = b.get("id")
                if bid:
                    try:
                        client.patch(
                            f"https://api.notion.com/v1/blocks/{bid}",
                            headers=_headers(api_key),
                            json={"archived": True},
                            timeout=30,
                        ).raise_for_status()
                    except Exception:
                        pass
    except Exception:
        pass
    return
    # ── original implementation kept below for reference but unreachable ─────
    marker = f"VC Snapshot (auto) [message_id={message_id}]"
    text = (snapshot_text or "").strip()
    if not text:
        return

    # Ensure first line is a stable marker.
    lines = text.splitlines()
    if not lines:
        return
    if not lines[0].startswith("VC Snapshot (auto)"):
        lines.insert(0, marker)
    else:
        lines[0] = marker
    text = "\n".join(lines).strip()

    existing_block_id: str | None = None
    try:
        r = client.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
            headers=_headers(api_key),
            timeout=30,
        )
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
        for b in results:
            if (b.get("type") or "") != "paragraph":
                continue
            para = b.get("paragraph") or {}
            plain = _plain_text_of_rich_text(para.get("rich_text"))
            if plain.startswith("VC Snapshot (auto) [message_id="):
                if f"[message_id={message_id}]" in plain:
                    existing_block_id = b.get("id")
                    break
                existing_block_id = existing_block_id or b.get("id")
    except Exception:
        existing_block_id = None

    content = text[:1900]
    rich_text = [{"type": "text", "text": {"content": content}}]
    if existing_block_id:
        client.patch(
            f"https://api.notion.com/v1/blocks/{existing_block_id}",
            headers=_headers(api_key),
            json={"paragraph": {"rich_text": rich_text}},
            timeout=30,
        )
    else:
        client.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=_headers(api_key),
            json={"children": [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text}}]},
            timeout=30,
        )

