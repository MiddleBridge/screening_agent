"""Attach Fireflies meetings to pipeline deals (SQLite + Notion refresh)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from storage import database as db
from tools.fireflies_client import (
    FirefliesTranscriptBundle,
    fetch_transcript_bundle,
    parse_webhook_json,
)

log = logging.getLogger(__name__)


@dataclass
class FirefliesIngestResult:
    ok: bool
    message_id: str | None
    meeting_id: str
    reason: str
    skipped_duplicate: bool = False
    notion_error: str | None = None


def resolve_deal_message_id(
    *,
    client_reference_id: str | None,
    transcript_title: str | None,
) -> tuple[str | None, str]:
    """
    Returns (message_id, resolution_note).
    """
    cref = (client_reference_id or "").strip()
    if cref:
        if db.get_deal_for_notion(cref):
            return cref, "client_reference_id"
        log.warning("client_reference_id=%r not found in deals; trying title match", cref)

    title = (transcript_title or "").strip()
    if not title:
        return None, "no_title"

    mids = db.find_message_ids_for_fireflies_title(
        title,
        days=int(os.getenv("FIREFLIES_TITLE_MATCH_DAYS", "120") or "120"),
    )
    if len(mids) == 1:
        return mids[0], "title_unique"
    if not mids:
        return None, "title_no_match"
    return None, f"title_ambiguous({len(mids)})"


def ingest_fireflies_meeting(
    *,
    meeting_id: str,
    client_reference_id: str | None = None,
    source_label: str = "fireflies",
    bundle: FirefliesTranscriptBundle | None = None,
) -> FirefliesIngestResult:
    """
    Persist founder call + refresh Notion. Fetches from API if ``bundle`` is None.
    """
    mid_ff = (meeting_id or "").strip()
    if not mid_ff:
        return FirefliesIngestResult(False, None, "", "empty_meeting_id")

    api_key = (os.getenv("FIREFLIES_API_KEY") or "").strip()
    if bundle is None:
        bundle = fetch_transcript_bundle(mid_ff, api_key=api_key)
    if bundle is None:
        return FirefliesIngestResult(False, None, mid_ff, "fetch_failed")

    msg_id, how = resolve_deal_message_id(
        client_reference_id=client_reference_id,
        transcript_title=bundle.title,
    )
    if not msg_id:
        log.error(
            "Fireflies ingest: cannot map meeting=%s resolution=%s title=%r",
            mid_ff,
            how,
            bundle.title[:120],
        )
        return FirefliesIngestResult(False, None, mid_ff, how)

    summary = bundle.summary_text or "(no AI summary in Fireflies response)"
    transcript_combined = bundle.transcript_excerpt

    occurred = bundle.date_string[:10] if bundle.date_string else ""

    appended, reason = db.append_founder_call(
        msg_id,
        call_id=mid_ff,
        source=source_label,
        title=bundle.title or "Founder call",
        transcript_url=bundle.transcript_public_url(),
        occurred_at=occurred,
        attendees=bundle.attendees_line,
        summary=summary,
        transcript=transcript_combined,
    )
    if reason == "duplicate":
        return FirefliesIngestResult(
            True, msg_id, mid_ff, "duplicate", skipped_duplicate=True
        )
    if not appended:
        return FirefliesIngestResult(False, msg_id, mid_ff, reason)

    notion_err: str | None = None
    nkey = (os.getenv("NOTION_API_KEY") or "").strip()
    ndb = (os.getenv("NOTION_DATABASE_ID") or "").strip()
    if nkey and ndb:
        try:
            from agents.notion_sync import sync_one_deal_to_notion

            ensure = os.getenv("NOTION_ENSURE_SCHEMA", "1").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            sync_one_deal_to_notion(msg_id, ensure_schema=ensure)
        except Exception as e:
            notion_err = str(e)[:800]
            log.exception("Notion sync after Fireflies failed: %s", e)
    else:
        notion_err = "NOTION_* missing; SQLite updated only."

    return FirefliesIngestResult(
        True, msg_id, mid_ff, how, notion_error=notion_err
    )


def process_webhook_payload(body: dict[str, Any]) -> FirefliesIngestResult:
    mid, cref, ev = parse_webhook_json(body)
    if not mid:
        return FirefliesIngestResult(False, None, "", "no_meeting_id_in_payload")

    ev_l = (ev or "").lower()
    if (
        ev_l
        and "summarized" not in ev_l
        and "transcribed" not in ev_l
        and "transcription" not in ev_l
    ):
        log.info("Fireflies webhook ignored event=%r meeting=%s", ev, mid)
        return FirefliesIngestResult(True, None, mid, f"ignored_event:{ev_l}")

    return ingest_fireflies_meeting(meeting_id=mid, client_reference_id=cref)
