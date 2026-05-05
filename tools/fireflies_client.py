"""Fireflies.ai GraphQL client + webhook signature verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

FIREFLIES_GRAPHQL_URL = os.getenv("FIREFLIES_GRAPHQL_URL", "https://api.fireflies.ai/graphql").strip()


def verify_webhook_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool:
    """Verify Fireflies Webhooks V2 ``X-Hub-Signature`` (``sha256=<hex>``)."""
    if not secret or not signature_header:
        return False
    sig = signature_header.strip()
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


TRANSCRIPT_QUERY = """
query TranscriptForPipeline($transcriptId: String!) {
  transcript(id: $transcriptId) {
    id
    title
    dateString
    transcript_url
    meeting_link
    meeting_attendees {
      displayName
      email
      name
    }
    summary {
      short_summary
      overview
      bullet_gist
      shorthand_bullet
      gist
      action_items
      topics_discussed
    }
    sentences {
      text
      speaker_name
    }
  }
}
"""


@dataclass
class FirefliesTranscriptBundle:
    meeting_id: str
    title: str
    date_string: str
    transcript_url: str
    attendees_line: str
    summary_text: str
    transcript_excerpt: str

    def transcript_public_url(self) -> str:
        u = (self.transcript_url or "").strip()
        if u:
            return u
        return (self.meeting_link or "").strip()


def _flatten_summary(summary: dict[str, Any] | None) -> str:
    if not summary:
        return ""
    parts: list[str] = []
    for key in ("overview", "short_summary", "bullet_gist", "shorthand_bullet", "gist"):
        v = summary.get(key)
        if v and str(v).strip():
            parts.append(str(v).strip())
    topics = summary.get("topics_discussed")
    if isinstance(topics, list) and topics:
        parts.append("Topics:\n" + "\n".join(f"- {x}" for x in topics[:24] if str(x).strip()))
    ai = summary.get("action_items")
    if isinstance(ai, list) and ai:
        lines = []
        for x in ai[:30]:
            if isinstance(x, str) and x.strip():
                lines.append(f"- {x.strip()}")
            elif isinstance(x, dict):
                t = str(x.get("text") or x.get("item") or "").strip()
                if t:
                    lines.append(f"- {t}")
        if lines:
            parts.append("Action items:\n" + "\n".join(lines))
    return "\n\n".join(parts).strip()


def _attendees_line(meeting_attendees: list[dict[str, Any]] | None) -> str:
    if not meeting_attendees:
        return ""
    names: list[str] = []
    for a in meeting_attendees:
        if not isinstance(a, dict):
            continue
        n = str(a.get("displayName") or a.get("name") or "").strip()
        if n:
            names.append(n)
    return ", ".join(names[:40])


def _sentences_excerpt(sentences: list[dict[str, Any]] | None, max_chars: int) -> str:
    if not sentences or max_chars <= 0:
        return ""
    parts: list[str] = []
    total = 0
    for s in sentences:
        if not isinstance(s, dict):
            continue
        sp = str(s.get("speaker_name") or "").strip()
        tx = str(s.get("text") or "").strip()
        if not tx:
            continue
        line = f"{sp}: {tx}" if sp else tx
        if total + len(line) + 1 > max_chars:
            break
        parts.append(line)
        total += len(line) + 1
    return "\n".join(parts)


def fetch_transcript_bundle(meeting_id: str, *, api_key: str) -> FirefliesTranscriptBundle | None:
    mid = (meeting_id or "").strip()
    if not mid:
        return None
    key = (api_key or "").strip()
    if not key:
        log.error("FIREFLIES_API_KEY missing - cannot fetch transcript %s", mid)
        return None
    max_tr = int(os.getenv("FIREFLIES_MAX_TRANSCRIPT_CHARS", "48000") or "48000")
    payload = {
        "query": TRANSCRIPT_QUERY,
        "variables": {"transcriptId": mid},
    }
    try:
        with httpx.Client(timeout=45.0) as client:
            r = client.post(
                FIREFLIES_GRAPHQL_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {key}",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.exception("Fireflies GraphQL error for %s: %s", mid, e)
        return None
    if not isinstance(data, dict):
        return None
    errs = data.get("errors")
    if errs:
        log.error("Fireflies GraphQL errors for %s: %s", mid, errs[:2])
        return None
    tr = (data.get("data") or {}).get("transcript")
    if not isinstance(tr, dict):
        return None

    title = str(tr.get("title") or "").strip()
    date_string = str(tr.get("dateString") or "").strip()

    summary_text = _flatten_summary(tr.get("summary") if isinstance(tr.get("summary"), dict) else None)
    attendees_line = _attendees_line(
        tr.get("meeting_attendees") if isinstance(tr.get("meeting_attendees"), list) else None
    )
    sentences = tr.get("sentences") if isinstance(tr.get("sentences"), list) else None
    excerpt = _sentences_excerpt(sentences, max(0, max_tr))

    if os.getenv("FIREFLIES_FETCH_TRANSCRIPT", "1").strip().lower() in ("0", "false", "no", "off"):
        excerpt = ""

    return FirefliesTranscriptBundle(
        meeting_id=str(tr.get("id") or mid),
        title=title,
        date_string=date_string,
        transcript_url=str(tr.get("transcript_url") or "").strip(),
        attendees_line=attendees_line,
        summary_text=summary_text,
        transcript_excerpt=excerpt,
    )


def parse_webhook_json(body: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    """
    Returns (meeting_id, client_reference_id, event_label).
    Supports Webhooks V2 and legacy V1 shapes.
    """
    if not isinstance(body, dict):
        return None, None, None
    mid = body.get("meeting_id") or body.get("meetingId")
    cref = body.get("client_reference_id") or body.get("clientReferenceId")
    ev = body.get("event") or body.get("eventType")
    return (
        str(mid).strip() if mid else None,
        str(cref).strip() if cref else None,
        str(ev).strip() if ev else None,
    )


def meeting_excerpt_for_log(bundle: FirefliesTranscriptBundle | None) -> str:
    if not bundle:
        return ""
    return json.dumps(
        {"title": bundle.title, "date": bundle.date_string, "summary_len": len(bundle.summary_text)},
        ensure_ascii=False,
    )[:400]
