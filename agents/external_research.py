"""External web research abstraction — no fake URLs or placeholder data.

Default path uses **no paid third-party search** (no Tavily/SerpAPI bill): callers get
``NullExternalResearchProvider`` and empty snippets unless you opt in.

Opt-in:
  - Tavily: set ``TAVILY_API_KEY``
  - SerpAPI: set ``SERPAPI_API_KEY``

Provider selection:
  - ``EXTERNAL_WEB_SEARCH=serpapi`` -> force SerpAPI
  - ``EXTERNAL_WEB_SEARCH=tavily``  -> force Tavily
  - ``EXTERNAL_WEB_SEARCH=auto``    -> prefer SerpAPI, then Tavily
  - ``EXTERNAL_WEB_SEARCH=0`` (or false/off) -> disable paid search
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

from agents.schemas_gate25 import ExternalSource


class ExternalResearchProvider(Protocol):
    def search(self, query: str, max_results: int = 5) -> list[ExternalSource]:
        ...


class NullExternalResearchProvider:
    """No API key — returns empty results (caller must not invent research)."""

    def search(self, query: str, max_results: int = 5) -> list[ExternalSource]:
        return []


class TavilyResearchProvider:
    """https://tavily.com — set TAVILY_API_KEY in environment."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("TAVILY_API_KEY", "").strip()
        if not self.api_key:
            raise ValueError("TAVILY_API_KEY missing")

    def search(self, query: str, max_results: int = 5) -> list[ExternalSource]:
        body = json.dumps(
            {
                "api_key": self.api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "basic",
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return []

        out: list[ExternalSource] = []
        for r in data.get("results") or []:
            url = (r.get("url") or "").strip() or None
            title = (r.get("title") or "")[:500]
            content = (r.get("content") or r.get("raw_content") or "")[:2000]
            if url and "example.com" in url.lower():
                continue
            out.append(
                ExternalSource(
                    title=title or "(no title)",
                    url=url,
                    publisher=None,
                    date=None,
                    snippet=content or None,
                    source_type="web",
                )
            )
        return out


class SerpAPIResearchProvider:
    """https://serpapi.com — set SERPAPI_API_KEY in environment."""

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.getenv("SERPAPI_API_KEY", "").strip()
        if not self.api_key:
            raise ValueError("SERPAPI_API_KEY missing")

    def search(self, query: str, max_results: int = 5) -> list[ExternalSource]:
        q = urllib.parse.quote_plus(query)
        url = (
            "https://serpapi.com/search.json"
            f"?engine=google&q={q}&num={max(1, min(int(max_results), 10))}&api_key={self.api_key}"
        )
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "fund-screening-ai/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return []

        out: list[ExternalSource] = []
        for r in data.get("organic_results") or []:
            u = (r.get("link") or "").strip() or None
            title = (r.get("title") or "")[:500]
            snippet = (r.get("snippet") or r.get("snippet_highlighted_words") or "")
            if isinstance(snippet, list):
                snippet = " ".join(str(x) for x in snippet)
            snippet = str(snippet or "")[:2000]
            if u and "example.com" in u.lower():
                continue
            out.append(
                ExternalSource(
                    title=title or "(no title)",
                    url=u,
                    publisher=None,
                    date=None,
                    snippet=snippet or None,
                    source_type="web",
                )
            )
        return out


def get_research_provider() -> tuple[ExternalResearchProvider, bool]:
    """
    Returns (provider, is_live).
    is_live False when using null provider — external scores must be capped.
    """
    mode = os.getenv("EXTERNAL_WEB_SEARCH", "auto").strip().lower()
    if mode in ("0", "false", "no", "off", "none", "disabled"):
        return NullExternalResearchProvider(), False

    serp_key = os.getenv("SERPAPI_API_KEY", "").strip()
    key = os.getenv("TAVILY_API_KEY", "").strip()

    if mode in ("serpapi", "serp", "google"):
        if serp_key:
            try:
                return SerpAPIResearchProvider(api_key=serp_key), True
            except ValueError:
                pass
        return NullExternalResearchProvider(), False

    if mode in ("tavily", "tvly"):
        if key:
            try:
                return TavilyResearchProvider(api_key=key), True
            except ValueError:
                pass
        return NullExternalResearchProvider(), False

    # auto mode: prefer SerpAPI for broader Google coverage.
    if serp_key:
        try:
            return SerpAPIResearchProvider(api_key=serp_key), True
        except ValueError:
            pass
    if key:
        try:
            return TavilyResearchProvider(api_key=key), True
        except ValueError:
            pass
    return NullExternalResearchProvider(), False
