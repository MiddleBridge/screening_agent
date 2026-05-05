# -*- coding: utf-8 -*-
"""Deterministic traction signal detector.

Scans deck OCR markdown and/or website markdown for concrete traction
signals (ARR, MRR, paying customers, growth %, pilots/LOIs, retention,
etc.). No LLM, no scoring -- just a regex sweep + a 4-state verdict.

Returned shape (json-serializable) used by main.py + notion_sync.py:

    {
        "verdict": "BOTH" | "DECK_ONLY" | "WEBSITE_ONLY" | "NONE",
        "deck": [{"label": str, "snippet": str}, ...],
        "website": [{"label": str, "snippet": str}, ...],
    }
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

_PATTERNS: Tuple[Tuple[str, str], ...] = (
    ("ARR / MRR",            r"\b(?:ARR|MRR|annual recurring revenue|monthly recurring revenue)\b"),
    ("Revenue figure",       r"(?:revenue|sales|GMV|TPV)\s*(?:of|:)?\s*[$EUR]?\s*\d[\d.,]*\s*[kKmMbB]?"),
    ("Money raised",         r"(?:raised|secured)\s+[$EUR]?\s*\d[\d.,]*\s*[kKmMbB]?\s*(?:in|from|seed|pre-seed|series)"),
    ("Paying customers (#)", r"\b\d[\d,\.]{1,9}\s*(?:paying|active)?\s*(?:customers|clients|users|merchants|subscribers)\b"),
    ("Users (#)",            r"\b\d[\d,\.]{1,9}\s*(?:MAU|DAU|users|signups|sign-ups|downloads)\b"),
    ("Growth rate",          r"\b\d{1,3}\s*%\s*(?:MoM|YoY|month[-\s]?over[-\s]?month|year[-\s]?over[-\s]?year|growth)\b"),
    ("Logos / case studies", r"\b(?:trusted by|our customers|case stud(?:y|ies)|customer logos|used by)\b"),
    ("Pilots / LOIs",        r"\b(?:pilot|paid pilot|LOI|letter of intent|signed contract|design partner)s?\b"),
    ("Retention / churn",    r"\b(?:NRR|GRR|net (?:dollar )?retention|gross retention|churn)\s*(?:of|:)?\s*\d"),
    ("Conversion / CAC",     r"\b(?:CAC|payback|LTV|conversion rate)\s*(?:of|:)?\s*[$EUR]?\s*\d"),
    ("Partners / investors", r"\b(?:partnered with|backed by|co-funded by|investors include|our partners)\b"),
)


_MAILTO = re.compile(r"\(?mailto:[^)\s]{6,400}\)?", flags=re.I)
_MD_LINK = re.compile(r"\[([^\]]{0,200})\]\([^)]+\)")
_JUNK_LINE = re.compile(
    r"^(## Structured data|### Images on this page|- schema_|schema_name:|- schema_)",
    flags=re.I,
)


def _section_bounds(text: str, idx: int) -> tuple[int, int]:
    """Slice of combined crawl markdown between ``## Source:`` markers containing idx."""
    markers = [m.start() for m in re.finditer(r"^## Source:\s*https?://\S+\s*$", text, flags=re.MULTILINE)]
    if not markers:
        return 0, len(text)
    if idx < markers[0]:
        return 0, markers[0]
    for i, st in enumerate(markers):
        end = markers[i + 1] if i + 1 < len(markers) else len(text)
        if st <= idx < end:
            return st, end
    return markers[-1], len(text)


def _clean_traction_snippet(raw: str, *, max_len: int = 220) -> str:
    s = raw or ""
    s = _MAILTO.sub(" ", s)
    s = _MD_LINK.sub(r"\1", s)
    s = re.sub(r"\s*## Source:\s*https?://\S+\s*", " ", s)
    # Drop junk headings/lines often glued into one line after whitespace collapse
    pieces: list[str] = []
    for line in s.replace("\r", "").split("\n"):
        t = line.strip()
        if not t or _JUNK_LINE.match(t):
            continue
        pieces.append(t)
    s = " ".join(pieces)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 3].rsplit(" ", 1)[0] + "..."
    return s


def _snippet_for_match(text: str, m: re.Match, *, max_len: int = 220) -> str:
    """Context around regex match; stay inside one crawled page section; strip mailto/markdown noise."""
    if not m:
        return ""
    a, b = _section_bounds(text, m.start())
    scope = text[a:b]
    mid = (m.start() - a + m.end() - a) // 2
    half = max_len // 2
    start = max(0, mid - half)
    end = min(len(scope), start + max_len)
    if end - start < max_len:
        start = max(0, end - max_len)
    if start > 0:
        back = scope[max(0, start - 80) : start]
        cut = back.rfind("\n")
        if cut < 0:
            cut = back.rfind(" ")
        if cut >= 0:
            start = max(0, start - 80) + cut + 1
            end = min(len(scope), start + max_len)
    chunk = scope[start:end]
    return _clean_traction_snippet(chunk, max_len=max_len)


def _scan(text: str) -> List[Dict[str, str]]:
    if not text:
        return []
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    for label, pattern in _PATTERNS:
        try:
            m = re.search(pattern, text, flags=re.IGNORECASE)
        except re.error:
            continue
        if not m or label in seen:
            continue
        seen.add(label)
        snippet = _snippet_for_match(text, m)
        out.append({"label": label, "snippet": snippet})
    return out


def detect_traction(*, deck_md: str = "", website_md: str = "") -> Dict[str, Any]:
    """Return the traction report as a json-serializable dict."""
    deck_hits = _scan(deck_md)
    web_hits = _scan(website_md)
    if deck_hits and web_hits:
        verdict = "BOTH"
    elif deck_hits:
        verdict = "DECK_ONLY"
    elif web_hits:
        verdict = "WEBSITE_ONLY"
    else:
        verdict = "NONE"
    return {
        "verdict": verdict,
        "deck": deck_hits,
        "website": web_hits,
    }


def verdict_human(verdict: str) -> str:
    return {
        "BOTH": "YES - found in BOTH deck and website",
        "DECK_ONLY": "YES - found in deck only",
        "WEBSITE_ONLY": "YES - found on website only",
        "NONE": "NO traction signals detected in deck or website",
    }.get(str(verdict or "").upper(), "-")
