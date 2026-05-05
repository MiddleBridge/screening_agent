"""Aggressive web-search enrichment for thin deck/website facts.

Fires automatically after Gate2 facts extraction whenever key fields are
missing. Each missing field triggers a focused web search (SerpAPI/Tavily) and the
extracted value is written back into the facts dict in-place.

Key fields enriched (each independent — partial success is OK):
  • founders               → names + LinkedIn search URLs
  • founders nationality   → nationality/origin hint (for CEE checks)
  • geography / HQ         → city + country
  • founded_year           → year
  • funding_round          → "Seed" / "Series A" / etc.
  • funding_amount         → "$3M" / "€5M" / etc.
  • funding_date           → "2023" or "2023-Q2"
  • valuation              → if mentioned

Source URL is appended to ``inferred_signals`` per fact, e.g.:
    funding_source: Series B [https://crunchbase.com/...]

Per-field source URLs are also exposed as direct facts keys
(``founders_source_url``, ``geography_source_url``, ``founded_source_url``,
``funding_source_url``) so the memo can render an inline domain link.

No call is made when provider keys are unset or ``EXTERNAL_WEB_SEARCH=0``.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from urllib.parse import urlparse
from dataclasses import dataclass, field
from typing import Any

from config.fund_evidence_blocklist import public_fund_site_hosts

from agents.external_research import get_research_provider
from agents.schemas_gate25 import ExternalSource


# ── helpers ─────────────────────────────────────────────────────────────────

_SENTINEL = (
    "",
    "unknown",
    "n/a",
    "none",
    "not stated",
    "not specified",
    "not available",
    "not provided",
    "—",
    "not_found_in_deck",
)

# LLM-language placeholders that real models love to emit when a fact is missing.
# Anything matching one of these patterns is treated as "unknown" so enrichment
# fires (regex extractor or LLM fallback) instead of being silently skipped.
_LLM_PLACEHOLDER_PATTERNS = (
    re.compile(r"\bno\s+(team|founder|leadership|executive)s?\s+(?:slide|info|information|details?|page|section)\b", re.I),
    re.compile(r"\bnot\s+(?:found|listed|mentioned|present|provided|given|disclosed|stated|specified|available|public)\b", re.I),
    re.compile(r"\bno\s+(?:public|public\s+info|disclosed|relevant)\b", re.I),
    re.compile(r"\b(?:absent|missing)\s+from\s+(?:deck|website|email)\b", re.I),
    re.compile(r"\b(?:was|were)\s+not\s+(?:found|listed|mentioned)\b", re.I),
    re.compile(r"\bcould\s+not\s+(?:find|determine|locate)\b", re.I),
)


def _is_unknown(v: Any) -> bool:
    if v is None:
        return True
    t = str(v).strip().lower()
    if t in _SENTINEL:
        return True
    if t.startswith("not_found_in_") or t.startswith("not found in "):
        return True
    if any(p.search(t) for p in _LLM_PLACEHOLDER_PATTERNS):
        return True
    return False


def _trusted_domain_rank(url: str) -> int:
    """Lower = more trusted. Own-fund / portfolio pages excluded entirely."""
    u = (url or "").lower()
    if any(d in u for d in public_fund_site_hosts()):
        return 999
    preferred = (
        "crunchbase.com",
        "linkedin.com",
        "techcrunch.com",
        "forbes.com",
        "bloomberg.com",
        "eu-startups.com",
        "dealroom.co",
        "sifted.eu",
        "tracxn.com",
        "pitchbook.com",
        "wikipedia.org",
    )
    for i, d in enumerate(preferred):
        if d in u:
            return i
    return len(preferred)


def _best_source(sources: list[ExternalSource]) -> ExternalSource | None:
    if not sources:
        return None
    valid = [s for s in sources if s.url and _trusted_domain_rank(s.url) < 999]
    if not valid:
        return None
    valid.sort(key=lambda s: _trusted_domain_rank(s.url or ""))
    return valid[0]


def _linkedin_search_url(name: str, company: str = "") -> str:
    q = urllib.parse.quote_plus(f"{name} {company}".strip())
    return f"https://www.linkedin.com/search/results/people/?keywords={q}"


# ── extractors (regex-based on Tavily snippets) ─────────────────────────────


# Tokens that are common false positives in Tavily snippets — institutions,
# titles, navigation chrome, schools, page-fragment markers, etc. If a candidate
# "name" contains any of these, it's almost certainly not a real founder name.
_FOUNDER_NAME_BLOCKLIST = (
    "linkedin",
    "europe",
    "america",
    "crunchbase",
    "techcrunch",
    "tech crunch",
    "company",
    "headquarters",
    "based in",
    "founder",
    "co-founder",
    "ceo",
    "cto",
    "founded",
    "post",
    "posts",
    "profile",
    "wharton",
    "school",
    "university",
    "college",
    "institute",
    "academy",
    "growth leader",
    "growth manager",
    "engineering leader",
    "general partner",
    "vice president",
    "president",
    "director",
    "manager",
    "investor",
    "team",
    "leadership",
    "investment",
    "series",
    "spacelift",  # company name catches 'Spacelift Inc'
    "github",
    "twitter",
    "youtube",
    "blog",
    "podcast",
    "image",
    "credit",
    "photo",
    "video",
    "icon",
    "avatar",
    "logo",
    "press",
    "source",
    "signal",
    "boost",
    "click here",
    "read more",
    "learn more",
    "see more",
    # Crunchbase card labels / non-people
    "legal name",
    "operating status",
    "also known as",
    "funding",
    "status",
    "active",
    "overview",
    "details",
    "highlights",
    "headcount",
    "employees",
    "contacts",
    "about",
    "system",
    "initiative",
    "google cloud",
    "aws",
    "amazon web services",
    "microsoft azure",
    "azure",
    "gcp",
    "kubernetes",
    "terraform",
)

_FOUNDER_ROLE_TRIGGERS = (
    "founder",
    "co-founder",
    "cofounder",
    "founding",
    "founder & ceo",
    "founder and ceo",
    "ceo and founder",
    "founder at",
)


def _company_mention_regex(company: str) -> re.Pattern[str] | None:
    c = (company or "").strip().lower()
    if not c:
        return None
    # For one-token company names (e.g. "Spacelift") require word boundaries.
    if " " not in c and len(c) >= 4:
        return re.compile(rf"\b{re.escape(c)}\b", re.I)
    # For multi-word names, allow light punctuation/whitespace variation.
    parts = [p for p in re.split(r"\s+", c) if len(p) >= 3]
    if not parts:
        return None
    pat = r".{0,20}".join(re.escape(p) for p in parts)
    return re.compile(pat, re.I)


def _snippet_confirms_founder(
    s: ExternalSource,
    *,
    name: str,
    company: str,
) -> bool:
    """True only when the snippet clearly ties `name` to `company` in founder context."""
    text = f"{s.title or ''}. {s.snippet or ''}"
    low = text.lower()
    if not name or name.lower() not in low:
        return False
    comp_re = _company_mention_regex(company)
    if comp_re is None or not comp_re.search(low):
        return False
    # Must include some founder role hint in the same snippet.
    if not any(t in low for t in _FOUNDER_ROLE_TRIGGERS):
        return False
    return True


def _snippet_confirms_profile(
    s: ExternalSource,
    *,
    name: str,
    company: str,
) -> bool:
    """Stronger confirmation: LinkedIn/Crunchbase profile URL + founder context."""
    u = (s.url or "").lower()
    if not u:
        return False
    if ("linkedin.com/in/" not in u) and ("crunchbase.com/person/" not in u) and ("crunchbase.com/organization/" not in u):
        return False
    return _snippet_confirms_founder(s, name=name, company=company)


def _filter_confirmed_founder_names(
    names: list[str],
    snippets: list[ExternalSource],
    *,
    company: str,
    website_url: str = "",
    min_independent_snippets: int = 2,
) -> list[str]:
    """Drop randoms: keep only names confirmed by company-specific evidence.

    A name is kept iff:
      - it appears in >= `min_independent_snippets` distinct snippet URLs where the snippet mentions the company
        AND founder-role context, OR
      - it has a LinkedIn/Crunchbase profile URL snippet confirming founder context.
    """
    if not names:
        return []
    # Evidence per name: distinct source domains (netlocs) where the snippet
    # ties the person to THIS company in founder context.
    domain_sets: dict[str, set[str]] = {n: set() for n in names}
    profile_ok: dict[str, bool] = {n: False for n in names}
    company_host = ""
    try:
        company_host = urlparse(website_url).netloc.lower().lstrip("www.") if website_url else ""
    except Exception:
        company_host = ""
    for s in snippets:
        for n in names:
            if _snippet_confirms_founder(s, name=n, company=company):
                u = (s.url or "").strip()
                if u:
                    try:
                        netloc = urlparse(u).netloc.lower().lstrip("www.")
                    except Exception:
                        netloc = ""
                    domain_sets[n].add(netloc or u[:60])
                else:
                    domain_sets[n].add(f"(no_url){(s.title or '')[:60]}")
            if not profile_ok[n] and _snippet_confirms_profile(s, name=n, company=company):
                profile_ok[n] = True
    out: list[str] = []
    for n in names:
        doms = domain_sets.get(n) or set()
        # Cross-check rule: LinkedIn alone is not sufficient — require at least
        # one non-LinkedIn domain OR an official company-domain / Crunchbase hit.
        has_linkedin = any("linkedin.com" in d for d in doms)
        has_crunchbase = any("crunchbase.com" in d for d in doms)
        has_company = bool(company_host) and any(company_host in d for d in doms)
        non_linkedin = {d for d in doms if d and ("linkedin.com" not in d)}

        if has_crunchbase or has_company:
            out.append(n)
            continue
        if len(doms) >= min_independent_snippets and (non_linkedin or not has_linkedin):
            out.append(n)
            continue
        # If we have >=2 sources and one of them is LinkedIn, require the other
        # to be non-LinkedIn (otherwise it's just multiple LinkedIn pages).
        if has_linkedin and len(non_linkedin) >= 1:
            out.append(n)
            continue
    return out[:4]


def _looks_like_real_person_name(nm: str) -> bool:
    """Heuristic: is this string plausibly a real human full name?"""
    nml = (nm or "").strip().lower()
    if not nml or len(nm) < 5:
        return False
    if any(bad in nml for bad in _FOUNDER_NAME_BLOCKLIST):
        return False
    if "'s" in nml or nm.endswith("'s") or nm.endswith("’s"):
        return False
    # Strip common trailing punctuation from tokens.
    parts_raw = re.split(r"\s+", nm.strip())
    parts = [p.strip(".,:;()[]{}<>\"") for p in parts_raw if p.strip(".,:;()[]{}<>\"")]
    if not (2 <= len(parts) <= 3):
        return False
    for p in parts:
        if not p or not p[0].isupper():
            return False
        if len(p) < 2:
            return False
        # Reject short ALL-CAPS tokens (IT/AI/CEO/CTO/etc.) which are almost never
        # part of a real person name but frequently appear in company labels.
        if p.isupper() and len(p) <= 3:
            return False
        # Reject ALL-CAPS tokens longer than 4 chars (likely an acronym, e.g. CEO)
        if len(p) > 4 and p.isupper():
            return False
        # Reject tokens that end with a dot (often section labels).
        if p.endswith("."):
            return False
    return True


def _extract_founder_names(snippets: list[ExternalSource], company: str) -> list[str]:
    """Pull capitalized 2-3 word names from snippets that mention founder context."""
    company_low = (company or "").strip().lower()
    out: list[str] = []
    seen: set[str] = set()
    triggers = ("founded by", "co-founded by", "founder", "co-founder", "established by",
                "ceo and founder", "founder and ceo", "founders include")
    for s in snippets:
        text = f"{s.title or ''}. {s.snippet or ''}"
        low = text.lower()
        if not any(t in low for t in triggers):
            continue
        for m in re.finditer(
            r"\b([A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+){1,2})\b",
            text,
        ):
            nm = m.group(1).strip()
            nml = nm.lower()
            if nml in seen:
                continue
            if company_low and company_low in nml:
                continue
            # Directional founder-context requirement (prevents "Founded ... uses Google Cloud" junk):
            pre = low[max(0, m.start() - 120) : m.start()]
            post = low[m.end() : m.end() + 80]
            pre_ok = any(
                k in pre
                for k in (
                    "founded by",
                    "co-founded by",
                    "cofounded by",
                    "founders:",
                    "founders include",
                    "founder:",
                    "co-founder:",
                    "cofounder:",
                    "established by",
                )
            )
            post_ok = any(
                k in post
                for k in (
                    "— founder",
                    "- founder",
                    " founder",
                    "— co-founder",
                    "- co-founder",
                    " co-founder",
                    " ceo",
                    " cto",
                    " founder at",
                    " co-founder at",
                    " cofounder at",
                )
            )
            if not (pre_ok or post_ok):
                continue
            if not _looks_like_real_person_name(nm):
                continue
            seen.add(nml)
            out.append(nm)
            if len(out) >= 4:
                return out
    return out


def _match_linkedin_url_for_name(name: str, snippets: list[ExternalSource]) -> str:
    """Find a linkedin.com profile URL whose title/url best matches ``name``.

    Strategy:
      1. If a snippet's URL contains 'linkedin.com/in/' AND title or snippet
         contains the name → take it.
      2. Else if URL is linkedin.com/in/<slug> and slug contains parts of name
         → take it.
      3. Else return empty.
    """
    if not name:
        return ""
    parts = [p.lower() for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return ""
    name_low = name.lower()

    # Pass 1: title/snippet contains full name AND URL is linkedin profile
    for s in snippets:
        u = (s.url or "").lower()
        if "linkedin.com/in/" not in u:
            continue
        text = f"{s.title or ''} {s.snippet or ''}".lower()
        if name_low in text:
            return s.url

    # Pass 2: URL slug matches all name parts
    for s in snippets:
        u = (s.url or "").lower()
        if "linkedin.com/in/" not in u:
            continue
        slug = u.split("linkedin.com/in/")[-1].split("/")[0].split("?")[0]
        slug = re.sub(r"[^a-z]", "", slug)
        if all(p in slug for p in parts if len(p) >= 3):
            return s.url

    # Pass 3: URL slug matches at least first+last
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        for s in snippets:
            u = (s.url or "").lower()
            if "linkedin.com/in/" not in u:
                continue
            slug = u.split("linkedin.com/in/")[-1].split("/")[0].split("?")[0]
            slug = re.sub(r"[^a-z]", "", slug)
            if first in slug and last in slug:
                return s.url
    return ""


def _extract_funding(snippets: list[ExternalSource]) -> dict[str, str]:
    """Parse Series X / $YM / year from snippet text."""
    result = {"funding_round": "", "funding_amount": "", "funding_date": "", "valuation": ""}
    blob = " ".join(f"{s.title or ''} {s.snippet or ''}" for s in snippets)
    if not blob:
        return result

    # Round: "Series A", "Seed", "Pre-seed"
    m = re.search(r"\b(Pre-Seed|Pre-seed|Preseed|Seed|Series\s+[A-G])\b", blob, re.I)
    if m:
        result["funding_round"] = m.group(1).strip().title().replace("Series ", "Series ")

    # Amount: $3M, €5M, $10.5M, $100K
    m = re.search(r"([\$€£]\s?\d+(?:\.\d+)?\s?[MmKk])\b", blob)
    if m:
        result["funding_amount"] = m.group(1).replace(" ", "")

    # Date: "in 2023", "raised in 2022", "2023-Q2"
    m = re.search(r"\b(20\d{2})\b", blob)
    if m:
        result["funding_date"] = m.group(1)

    # Valuation: "$X valuation", "valued at $X"
    m = re.search(r"(?:valued at|valuation of|valuation)\s+([\$€£]\s?\d+(?:\.\d+)?\s?[MmBb])",
                  blob, re.I)
    if m:
        result["valuation"] = m.group(1).replace(" ", "")

    return result


def _extract_hq_location(snippets: list[ExternalSource]) -> str:
    """Pull HQ city / country from snippets."""
    for s in snippets:
        text = f"{s.title or ''}. {s.snippet or ''}"
        # "headquartered in X", "HQ in X", "based in X"
        for pat in (
            r"headquartered in\s+([A-Z][A-Za-z ,'.-]{3,60}?)(?:[,.]|$)",
            r"\bHQ\s+(?:in|location)?\s*[:\-]?\s*([A-Z][A-Za-z ,'.-]{3,60}?)(?:[,.]|$)",
            r"\bbased in\s+([A-Z][A-Za-z ,'.-]{3,60}?)(?:[,.]|$)",
            r"\b([A-Z][A-Za-z']{2,30})-based\b",
        ):
            m = re.search(pat, text)
            if m:
                loc = m.group(1).strip().rstrip(".,")
                if 3 <= len(loc) <= 80:
                    return loc
    return ""


def _extract_founded_year(snippets: list[ExternalSource]) -> str:
    blob = " ".join(f"{s.title or ''} {s.snippet or ''}" for s in snippets)
    m = re.search(r"founded in\s+(20\d{2}|19\d{2})", blob, re.I)
    if m:
        return m.group(1)
    m = re.search(r"\bfounded\s+(20\d{2})\b", blob, re.I)
    if m:
        return m.group(1)
    return ""


_NATIONALITY_CANDIDATES = [
    "Polish",
    "Ukrainian",
    "Romanian",
    "Bulgarian",
    "Czech",
    "Slovak",
    "Hungarian",
    "Lithuanian",
    "Latvian",
    "Estonian",
    "Croatian",
    "Serbian",
    "Slovenian",
    "American",
    "British",
    "German",
    "French",
    "Israeli",
]

# CEE city / country fingerprints that imply founder roots even when no explicit
# nationality is stated in the snippet. Maps marker → canonical nationality token.
_CEE_LOCATION_TO_NAT = {
    "warsaw": "Polish",
    "warszawa": "Polish",
    "krakow": "Polish",
    "kraków": "Polish",
    "wroclaw": "Polish",
    "wrocław": "Polish",
    "gdansk": "Polish",
    "gdańsk": "Polish",
    "poznan": "Polish",
    "poznań": "Polish",
    "lodz": "Polish",
    "łódź": "Polish",
    "katowice": "Polish",
    "vilnius": "Lithuanian",
    "kaunas": "Lithuanian",
    "riga": "Latvian",
    "tallinn": "Estonian",
    "tartu": "Estonian",
    "prague": "Czech",
    "praha": "Czech",
    "brno": "Czech",
    "bratislava": "Slovak",
    "budapest": "Hungarian",
    "bucharest": "Romanian",
    "cluj": "Romanian",
    "sofia": "Bulgarian",
    "ljubljana": "Slovenian",
    "zagreb": "Croatian",
    "belgrade": "Serbian",
    "kyiv": "Ukrainian",
    "kiev": "Ukrainian",
    "lviv": "Ukrainian",
}


def _extract_founder_nationality_hint(snippets: list[ExternalSource]) -> str:
    """Best-effort nationality/origin hint from snippets mentioning founders."""
    blob = " ".join(f"{s.title or ''}. {s.snippet or ''}" for s in snippets)
    if not blob:
        return ""
    blob_low = blob.lower()
    # Prefer explicit "X founder/co-founder" patterns (highest confidence).
    for nat in _NATIONALITY_CANDIDATES:
        pat = rf"\b{re.escape(nat)}\b[^.:\n]{{0,40}}\b(founder|co-founder|ceo|cto)\b"
        if re.search(pat, blob, re.I):
            return nat
    # Founders-near-location signal: "founded in <CEE city>", "based in Warsaw".
    for marker, nat in _CEE_LOCATION_TO_NAT.items():
        loc_pat = rf"\b(?:founded|based|originated|started|incorporated|launched)\s+(?:in|out\s+of)\s+{re.escape(marker)}\b"
        if re.search(loc_pat, blob_low):
            return nat
    # Founder LinkedIn city: "Vilnius, Lithuania" near a founder context.
    for marker, nat in _CEE_LOCATION_TO_NAT.items():
        if marker in blob_low and re.search(r"\b(founder|co-founder|ceo|cto|builder|founding)\b", blob_low):
            return nat
    # Fallback: first nationality token found anywhere.
    for nat in _NATIONALITY_CANDIDATES:
        if re.search(rf"\b{re.escape(nat)}\b", blob, re.I):
            return nat
    return ""


def _first_founder_surname_for_search(founders_raw: str) -> str:
    """Last token of the first founder name — only for **search queries**, not nationality inference."""
    chunk = (founders_raw or "").split(";")[0].strip()
    chunk = re.split(r"\s+[—\-]\s*Founder", chunk, maxsplit=1, flags=re.I)[0].strip()
    parts = [p for p in re.split(r"[\s,]+", chunk) if p and not p.isdigit()]
    if len(parts) >= 2:
        return parts[-1]
    return ""


def _funding_round_to_stage(round_label: str, amount: str = "") -> str:
    """Map a fundraising round to a normalized startup stage label."""
    s = (round_label or "").strip().lower()
    if not s:
        return ""
    if "pre-seed" in s or "preseed" in s:
        return "pre-seed"
    if "seed" in s:
        return "seed"
    if "series a" in s:
        return "series-a"
    if "series b" in s:
        return "series-b"
    if "series c" in s:
        return "series-c"
    if "series d" in s or "series e" in s or "series f" in s:
        return "growth"
    return ""


def _llm_extract_founders_and_nationality(
    snippets: list[ExternalSource],
    company: str,
) -> tuple[list[str], str]:
    """LLM fallback when regex finds nothing.

    Returns (founder_names, nationality_hint). Empty list / "" on any failure.
    Cheap call (light model, low temp). Disabled via ``LLM_FOUNDER_FALLBACK=0``.
    """
    if os.getenv("LLM_FOUNDER_FALLBACK", "1").strip().lower() in ("0", "false", "no", "off"):
        return [], ""
    try:
        from openai import OpenAI
        from config.llm_cost import OPENAI_MODEL_LIGHT
    except Exception:
        return [], ""
    if not (os.getenv("OPENAI_API_KEY") or "").strip():
        return [], ""
    if not snippets:
        return [], ""

    blob_parts = []
    for s in snippets[:10]:
        title = (s.title or "").strip()
        snip = (s.snippet or "").strip()
        url = (s.url or "").strip()
        line = f"- TITLE: {title}\n  URL: {url}\n  SNIPPET: {snip[:600]}"
        blob_parts.append(line)
    blob = "\n".join(blob_parts)

    prompt = (
        f"Extract founder / co-founder names of THE COMPANY '{company}' (and ONLY this company) "
        "from the following web search snippets.\n"
        "STRICT RULES:\n"
        "- Return ONLY real human full names where the snippet explicitly says they are "
        f"founder / co-founder / CEO / CTO of '{company}'.\n"
        "- IGNORE names of other people (investors, journalists, customers, employees, podcast hosts, "
        "advisors, blog post authors, partners at VC funds) even if they appear near the company name.\n"
        "- IGNORE generic phrases like 'Image Credit', 'Founder at', 'The Team', 'Wharton School', "
        "'Growth Leader' — these are not names.\n"
        f"- IGNORE the company name itself (e.g. '{company}', '{company} Inc', '{company} Team').\n"
        "- DO NOT invent. If no person is unambiguously named as founder/co-founder of "
        f"'{company}' in the snippets, return an empty list.\n"
        "- If snippets imply nationality of THE FOUNDERS (e.g. Polish, Ukrainian, Lithuanian, Czech, "
        "Romanian, Hungarian, Bulgarian, Estonian, Latvian, Slovenian, Serbian, Croatian), return one best guess.\n"
        "- If a CEE city (Warsaw, Kraków, Vilnius, Prague, Kyiv, Lviv, Riga, Tallinn, Budapest, "
        "Bucharest, Sofia, Zagreb, Belgrade, Bratislava etc.) appears next to a founder profile, "
        "infer matching nationality.\n"
        "- Output STRICT JSON only: {\"founders\": [\"First Last\", \"First Last\"], "
        "\"nationality\": \"Polish|Ukrainian|Lithuanian|...|\"}\n\n"
        "SNIPPETS:\n" + blob
    )

    try:
        client = OpenAI()
        resp = client.chat.completions.create(
            model=OPENAI_MODEL_LIGHT,
            response_format={"type": "json_object"},
            temperature=0,
            messages=[
                {"role": "system", "content": "You extract structured facts from web search snippets. Be conservative."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=300,
        )
        content = (resp.choices[0].message.content or "{}").strip()
        import json as _json
        data = _json.loads(content)
        founders = data.get("founders") or []
        nat = str(data.get("nationality") or "").strip()
        if not isinstance(founders, list):
            founders = []
        clean: list[str] = []
        for f in founders:
            n = str(f or "").strip()
            if n and 4 <= len(n) <= 80 and n.count(" ") in (1, 2):
                clean.append(n)
        return clean[:4], nat
    except Exception:
        return [], ""


# ── orchestrator ────────────────────────────────────────────────────────────


@dataclass
class EnrichmentReport:
    queries_used: int = 0
    fields_enriched: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)


def _append_inferred(facts: dict, line: str) -> None:
    cur = str(facts.get("inferred_signals") or "").strip()
    facts["inferred_signals"] = (cur + "\n" + line).strip() if cur else line


def enrich_thin_facts(
    facts: dict,
    *,
    company_name: str,
    website_url: str = "",
    max_queries: int = 10,
) -> EnrichmentReport:
    """Top-up missing fields in ``facts`` using Tavily web search.

    Modifies ``facts`` in place. Returns a report of what was enriched.
    No-op if Tavily provider unavailable or all key fields already present.

    For each enriched field-group, writes:
      • the value (e.g. ``facts['founders']``, ``facts['geography']``)
      • a per-group source URL (e.g. ``facts['founders_source_url']``) so the
        memo can render an inline domain link instead of a vague "osint" tag.
      • for founders: a parallel ``facts['founder_linkedin_urls']`` semicolon
        list aligned to the founders order, with REAL LinkedIn profile URLs
        scraped from Tavily snippets when available (fallback to search URL).
    """
    report = EnrichmentReport()
    provider, live = get_research_provider()
    if not live:
        return report

    company = (company_name or "").strip()
    if not company:
        return report

    if not isinstance(facts.get("tavily_queries"), list):
        facts["tavily_queries"] = []

    # Determine which fields are missing — each missing field gets one Tavily call.
    todo: list[str] = []
    if _is_unknown(facts.get("founders")):
        todo.append("founders")
    if _is_unknown(facts.get("founder_nationality_hint")):
        todo.append("founders_nationality")
    if _is_unknown(facts.get("geography")):
        todo.append("geography")
    if _is_unknown(facts.get("founded_year")):
        todo.append("founded_year")
    if (
        _is_unknown(facts.get("funding_round"))
        or _is_unknown(facts.get("funding_amount"))
        or _is_unknown(facts.get("funding_date"))
    ):
        todo.append("funding")
    if not todo:
        return report

    # Cap total queries to control cost.
    todo = todo[:max_queries]

    def _run_query(purpose: str, query: str) -> list[ExternalSource]:
        try:
            res = provider.search(query, max_results=6)
        except Exception:
            return []
        report.queries_used += 1
        try:
            facts["tavily_queries"].append(
                {"purpose": purpose, "query": query, "provider": provider.__class__.__name__}
            )
        except Exception:
            pass
        return res or []

    for field_name in todo:
        results: list[ExternalSource] = []
        query = ""
        if field_name == "founders":
            # Prefer Crunchbase org page (more authoritative than random LinkedIn search results).
            cb_q = f"{company} founders site:crunchbase.com/organization"
            results = _run_query("founders_crunchbase", cb_q)
            query = cb_q
            if not results:
                li_q = f"{company} (founder OR co-founder) site:linkedin.com/in"
                results = _run_query("founders_linkedin", li_q)
                query = li_q
        elif field_name == "founders_nationality":
            # Primary: explicit “company + founders nationality” (universal, not name-pattern heuristics).
            merged: list[ExternalSource] = []
            q1 = f"{company} founders nationality"
            merged.extend(_run_query("founders_nationality", q1) or [])

            def _has_nat(m: list[ExternalSource]) -> bool:
                return bool(_extract_founder_nationality_hint(m))

            if not _has_nat(merged):
                merged.extend(_run_query("founders_nationality_alt", f"{company} co-founder country of origin") or [])
            if not _has_nat(merged):
                merged.extend(
                    _run_query("founders_nationality_alt2", f"{company} founder country citizenship background") or []
                )
            if not _has_nat(merged):
                sur = _first_founder_surname_for_search(str(facts.get("founders") or ""))
                if sur and len(sur) >= 2:
                    merged.extend(
                        _run_query(
                            "founders_nationality_name",
                            f"{company} {sur} founder nationality",
                        )
                        or []
                    )
            results = merged
            query = q1
        elif field_name == "geography":
            query = f"{company} headquarters location based in"
            results = _run_query(field_name, query)
        elif field_name == "founded_year":
            query = f"{company} founded year history"
            results = _run_query(field_name, query)
        elif field_name == "funding":
            query = f"{company} funding round series amount raised"
            results = _run_query(field_name, query)
        else:
            continue

        if not results:
            continue

        best = _best_source(results)
        src_url = best.url if best else ""

        if field_name == "founders":
            names = _extract_founder_names(results, company)
            llm_nat = ""
            # Use LLM fallback when regex yields nothing OR only 1 name (most
            # CEE deals have 2+ co-founders; a single-name regex hit is often
            # a partial extraction). Cheap call, guarded by env flag.
            if len(names) < 2:
                llm_names, llm_nat = _llm_extract_founders_and_nationality(results, company)
                if llm_names:
                    # Merge: keep regex names, then add new LLM names.
                    seen_low = {n.lower() for n in names}
                    for n in llm_names:
                        if n.lower() not in seen_low:
                            names.append(n)
                            seen_low.add(n.lower())
                    _append_inferred(facts, "founders_extractor: llm_fallback_used")
            if names:
                # Cross-check against company-specific evidence so we never
                # attach random people to deals. LinkedIn alone is not enough —
                # require either company-domain or Crunchbase confirmation, or
                # multiple independent domains.
                names = _filter_confirmed_founder_names(
                    names,
                    results,
                    company=company,
                    website_url=website_url,
                    min_independent_snippets=2,
                )
            if names:
                facts["founders"] = "; ".join(f"{n} — Founder" for n in names)
                facts["founders_source_url"] = src_url
                report.fields_enriched.append("founders")
                if src_url:
                    report.sources_used.append(src_url)
                    _append_inferred(facts, f"founders_source: {', '.join(names)} [{src_url}]")
                li_urls: list[str] = []
                for n in names:
                    real_li = _match_linkedin_url_for_name(n, results)
                    li_urls.append(real_li or _linkedin_search_url(n, company))
                facts["founder_linkedin_urls"] = "; ".join(li_urls)
                # Opportunistically backfill nationality from same snippets.
                if _is_unknown(facts.get("founder_nationality_hint")):
                    nat_guess = llm_nat or _extract_founder_nationality_hint(results)
                    if nat_guess:
                        facts["founder_nationality_hint"] = nat_guess
                        if src_url and _is_unknown(facts.get("founder_nationality_source_url")):
                            facts["founder_nationality_source_url"] = src_url
                        _append_inferred(
                            facts,
                            f"founder_nationality_hint: {nat_guess} [{src_url or 'inferred_from_founders_query'}]",
                        )
            else:
                # Keep unknown so website crawl / other sources can fill it.
                _append_inferred(facts, "founders_extractor: rejected_unconfirmed_candidates")

        elif field_name == "founders_nationality":
            nat = _extract_founder_nationality_hint(results)
            if not nat and results:
                _, llm_nat = _llm_extract_founders_and_nationality(results, company)
                nat = (llm_nat or "").strip()
            if nat:
                facts["founder_nationality_hint"] = nat
                if src_url and _is_unknown(facts.get("founder_nationality_source_url")):
                    facts["founder_nationality_source_url"] = src_url
                report.fields_enriched.append("founders_nationality")
                if src_url:
                    report.sources_used.append(src_url)
                _append_inferred(facts, f"founder_nationality_hint: {nat} [{src_url}]")

        elif field_name == "geography":
            loc = _extract_hq_location(results)
            if loc:
                facts["geography"] = loc
                facts["geography_source_url"] = src_url
                report.fields_enriched.append("geography")
                if src_url:
                    report.sources_used.append(src_url)
                    _append_inferred(facts, f"hq_source: {loc} [{src_url}]")

        elif field_name == "founded_year":
            yr = _extract_founded_year(results)
            if yr:
                facts["founded_year"] = yr
                facts["founded_source_url"] = src_url
                report.fields_enriched.append("founded_year")
                if src_url:
                    report.sources_used.append(src_url)
                    _append_inferred(facts, f"founded_source: {yr} [{src_url}]")

        elif field_name == "funding":
            fund = _extract_funding(results)
            wrote = []
            for k, v in fund.items():
                if v and _is_unknown(facts.get(k)):
                    facts[k] = v
                    wrote.append(f"{k}={v}")
            if wrote:
                facts["funding_source_url"] = src_url
                report.fields_enriched.append("funding")
                if src_url:
                    report.sources_used.append(src_url)
                    _append_inferred(facts, f"funding_source: {', '.join(wrote)} [{src_url}]")
            # Backfill stage from funding round if not yet known.
            if _is_unknown(facts.get("stage")):
                stage_guess = _funding_round_to_stage(
                    str(facts.get("funding_round") or ""),
                    str(facts.get("funding_amount") or ""),
                )
                if stage_guess:
                    facts["stage"] = stage_guess
                    report.fields_enriched.append("stage")
                    _append_inferred(
                        facts,
                        f"stage_source: derived from funding_round={facts.get('funding_round')} [{src_url or 'rule'}]",
                    )

    return report
