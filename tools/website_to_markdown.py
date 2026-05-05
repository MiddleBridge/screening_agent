"""Fetch and crawl a startup website -> structured markdown for LLM screening."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import extruct
import httpx
import trafilatura
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig

DEFAULT_MAX_PAGES = 20
DEFAULT_TIMEOUT = 20.0
# Max URLs in the initial BFS frontier (homepage HTML + sitemap + probes); expansion continues via BFS.
DEFAULT_MAX_SEED_URLS = 72
# Fallback paths only when link discovery finds nothing useful (same-domain nav/footer is preferred).
PROBE_PATHS: list[str] = [
    "/",
    "/about",
    "/about-us",
    "/company",
    "/team",
    "/founders",
    "/leadership",
    "/contact",
    "/careers",
    "/career",
    "/jobs",
    "/job",
    "/customers",
    "/case-studies",
    "/pricing",
    "/news",
    "/blog",
    "/for-businesses",
    "/for-business",
    "/for-customers",
    "/for-customer",
    "/privacy",
    "/privacy-policy",
    "/terms",
    "/terms-of-service",
    "/terms-and-conditions",
]

_SKIP_ASSET_SUFFIXES: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".ico",
    ".svg",
    ".css",
    ".js",
    ".woff",
    ".woff2",
    ".pdf",
    ".zip",
)

_PATH_PRIORITY_FRAGMENTS: tuple[str, ...] = (
    "/privacy",
    "/privacy-policy",
    "/terms",
    "/legal",
    "/gdpr",
    "/about",
    "/news",
    "/blog",
    "/post/",
    "/article",
    "/press",
    "/media",
    "/careers",
    "/jobs",
    "/contact",
    "/team",
    "/company",
    "/customers",
    "/for-business",
    "/for-customer",
    "/pricing",
)

# Crawled immediately after home — often missing from JS-heavy first HTML (Wix, etc.).
EARLY_CRAWL_PATHS: tuple[str, ...] = (
    "/privacy",
    "/privacy-policy",
    "/cookie-policy",
    "/cookies",
    "/legal",
    "/gdpr",
    "/terms",
    "/terms-of-service",
    "/terms-and-conditions",
    "/about",
    "/about-us",
    "/company",
    "/blog",
    "/news",
    "/press",
    "/media",
)


def _merge_discovered_url_lists(*lists: list[str]) -> list[str]:
    """Preserve order, dedupe by normalized URL key."""
    seen: set[str] = set()
    out: list[str] = []
    for lst in lists:
        for u in lst:
            if not (u or "").strip():
                continue
            k = _normalize_url_key(u)
            if k not in seen:
                seen.add(k)
                out.append(u.strip())
    return out


def _norm_sitemap_loc(loc: str, mirror_hosts: set[str]) -> str:
    loc = (loc or "").strip().split("?")[0]
    if not loc.startswith("http"):
        return ""
    if not _host_allowed_for_mirror(loc, mirror_hosts):
        return ""
    p = urlparse(loc)
    stem = f"{p.scheme}://{p.netloc}{(p.path or '/')}"
    if len(p.path or "/") > 1 and stem.endswith("/"):
        stem = stem.rstrip("/")
    return stem


def _fetch_sitemap_same_origin_urls(origin: str, mirror_hosts: set[str], warnings: list[str]) -> list[str]:
    """Collect same-origin page URLs from sitemap.xml (and one-level nested child sitemaps)."""
    out: list[str] = []
    base = (origin or "").rstrip("/") or origin

    def ingest_xml_text(text: str) -> tuple[list[str], list[str]]:
        """Return (page_urls, child_sitemap_xml_urls)."""
        locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", text or "", flags=re.I)
        pages: list[str] = []
        children: list[str] = []
        for raw in locs:
            st = _norm_sitemap_loc(raw, mirror_hosts)
            if not st:
                continue
            if st.lower().endswith(".xml"):
                children.append(st)
            else:
                pages.append(st)
        return pages, children

    for path in ("/sitemap.xml", "/sitemap_index.xml"):
        url = base + path
        for ua in _BROWSER_UAS[:2]:
            try:
                r = httpx.get(
                    url,
                    timeout=12.0,
                    follow_redirects=True,
                    headers={
                        "User-Agent": ua,
                        "Accept": "application/xml,text/xml,application/xhtml+xml;q=0.8,*/*;q=0.5",
                        "Accept-Language": "en;q=0.9",
                    },
                )
                if r.status_code >= 400 or not (r.text or "").strip():
                    continue
                pages, children = ingest_xml_text(r.text)
                for p in pages:
                    if p not in out:
                        out.append(p)
                # Index files often list only nested sitemaps — fetch a few for blog/post URLs.
                if children:
                    for child in children[:6]:
                        try:
                            r2 = httpx.get(
                                child,
                                timeout=10.0,
                                follow_redirects=True,
                                headers={"User-Agent": ua, "Accept": "application/xml,*/*"},
                            )
                            if r2.status_code >= 400 or not (r2.text or "").strip():
                                continue
                            sub_pages, _ = ingest_xml_text(r2.text)
                            for p in sub_pages:
                                if p not in out:
                                    out.append(p)
                        except Exception:
                            continue
                if out:
                    warnings.append(f"sitemap: {len(out)} same-origin URLs from {path}")
                return out
            except Exception as exc:
                warnings.append(f"sitemap_fetch_failed {url}: {exc.__class__.__name__}")
    return out


def _normalize_url_key(url: str) -> str:
    """Canonical key for deduping same page across www/non-www and trailing slashes."""
    try:
        p = urlparse(url.strip())
        host = (p.netloc or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path or "/"
        if len(path) > 1 and path.endswith("/"):
            path = path.rstrip("/")
        return f"{(p.scheme or 'https').lower()}://{host}{path}"
    except Exception:
        return url.strip()


def _extract_same_origin_links(
    html: str,
    page_url: str,
    root_url: str,
    *,
    mirror_hosts: Optional[set[str]] = None,
) -> list[str]:
    """Collect http(s) links on the same marketing domain cluster (e.g. .io + .com)."""
    if not (html or "").strip():
        return []
    allowed = mirror_hosts if mirror_hosts is not None else _marketing_mirror_hosts(root_url)
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if href.startswith("mailto:") or href.startswith("tel:"):
            continue
        full = urljoin(page_url, href)
        p = urlparse(full)
        if p.scheme not in ("http", "https"):
            continue
        if not _host_allowed_for_mirror(full, allowed):
            continue
        path_l = (p.path or "/").lower()
        if any(path_l.endswith(sfx) for sfx in _SKIP_ASSET_SUFFIXES):
            continue
        # Stable URL without fragment; drop query to avoid duplicate Wix navigation URLs.
        stem = f"{p.scheme}://{p.netloc}{(p.path or '/')}"
        if stem.endswith("/") and len((p.path or "/")) > 1:
            stem = stem.rstrip("/")
        key = _normalize_url_key(stem)
        if key not in seen:
            seen.add(key)
            out.append(stem)
    return out


def _priority_score(path: str) -> int:
    pl = (path or "").lower()
    score = 0
    for i, frag in enumerate(_PATH_PRIORITY_FRAGMENTS):
        if frag in pl:
            score += len(_PATH_PRIORITY_FRAGMENTS) - i
    return score


def _sort_discovered_urls(urls: list[str]) -> list[str]:
    scored = [(-_priority_score(urlparse(u).path), u) for u in urls]
    scored.sort(key=lambda t: (t[0], t[1]))
    return [u for _, u in scored]


_WIX_FILL_DIMS = re.compile(r"/fill/w_(\d+),h_(\d+)")
_SOCIAL_IMG_ALTS = frozenset(
    {"linkedin", "youtube", "twitter", "facebook", "instagram", "tiktok", "x"}
)


def _img_fill_dimensions(src: str) -> tuple[int, int]:
    """Wix CDN embeds display size in /fill/w_N,h_M/. Unknown -> treat as medium."""
    m = _WIX_FILL_DIMS.search(src or "")
    if m:
        return int(m.group(1)), int(m.group(2))
    return (512, 512)


def _img_dedupe_key(src: str) -> str:
    try:
        path = urlparse(src).path
        if "/media/" in path:
            return path.split("/media/", 1)[1].split("/")[0][:96]
    except Exception:
        pass
    return src[:160]


def _skip_embedded_image(src: str, alt: str) -> bool:
    pl = urlparse(src).path.lower()
    if pl.endswith(".ico") and "favicon" in pl:
        return True
    alt_l = (alt or "").strip().lower()
    if alt_l in _SOCIAL_IMG_ALTS:
        return True
    w, h = _img_fill_dimensions(src)
    # Icons / nav chrome on Wix (e.g. w_39,h_39 LinkedIn)
    if w <= 72 and h <= 72:
        return True
    return False


def _append_image_markdown(html: str, page_url: str, *, max_images: int = 12) -> str:
    """Trafilatura drops <img>; list a few meaningful asset URLs (not every CDN variant)."""
    if not (html or "").strip():
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    candidates: list[tuple[int, str, str]] = []
    seen_keys: set[str] = set()
    for img in soup.find_all("img", src=True):
        raw = (img.get("src") or "").strip()
        if not raw:
            continue
        src = urljoin(page_url, raw)
        alt = (img.get("alt") or "").strip()
        if _skip_embedded_image(src, alt):
            continue
        dk = _img_dedupe_key(src)
        if dk in seen_keys:
            continue
        seen_keys.add(dk)
        w, h = _img_fill_dimensions(src)
        area = w * h
        label = alt or urlparse(src).path.split("/")[-1][:48]
        candidates.append((area, label, src))
    candidates.sort(key=lambda t: (-t[0], t[2]))
    lines: list[str] = []
    for _, label, src in candidates[:max_images]:
        lines.append(f"- Image ({label}): {src}")
    if not lines:
        return ""
    return "\n\n### Images on this page\n" + "\n".join(lines)


def _build_url_queue(
    root_url: str,
    discovered: list[str],
    *,
    max_seed_urls: int = DEFAULT_MAX_SEED_URLS,
) -> list[str]:
    """Seed URLs for BFS: home, legal/blog probes first, then links (+ sitemap), then PROBE_PATHS.

    Previously the queue stopped at ``max_pages`` before ``/privacy`` etc. were ever pushed
    if the homepage exposed many internal anchors (common on Wix).
    """
    parsed = urlparse(root_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    home = origin if origin.endswith("/") else origin + "/"
    seen_keys: set[str] = set()
    ordered: list[str] = []

    def push(u: str) -> None:
        if len(ordered) >= max_seed_urls:
            return
        k = _normalize_url_key(u)
        if k in seen_keys:
            return
        seen_keys.add(k)
        ordered.append(u)

    push(home)
    for path in EARLY_CRAWL_PATHS:
        full = urljoin(origin.rstrip("/") + "/", path.lstrip("/"))
        push(full)
    for u in _sort_discovered_urls(discovered):
        push(u)
    for path in PROBE_PATHS:
        if path == "/":
            continue
        full = urljoin(origin.rstrip("/") + "/", path.lstrip("/"))
        push(full)
    return ordered


MIN_MARKDOWN_CHARS_FOR_PAGE = 40


def _page_markdown_useful(md: str) -> bool:
    s = (md or "").strip()
    if len(s) < MIN_MARKDOWN_CHARS_FOR_PAGE:
        return False
    # Drop pages that are only "## Source:" placeholders or schema-only noise.
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    if len(lines) <= 2 and all(
        ln.startswith("##") or ln.startswith("- schema") or ln.startswith("schema_") for ln in lines
    ):
        return False
    return True


@dataclass
class WebsitePageRecord:
    url: str
    title: str
    meta_description: str
    raw_html: str
    markdown: str
    text_length: int
    fetch_ok: bool = True
    status_code: Optional[int] = None
    error: Optional[str] = None


@dataclass
class WebsiteMarkdownResult:
    root_url: str
    pages: list[WebsitePageRecord]
    combined_markdown: str
    fetch_warnings: list[str] = field(default_factory=list)
    extraction_quality_score: int = 5


def normalize_root_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise ValueError("Empty URL")
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    parsed = urlparse(u)
    if not parsed.netloc:
        raise ValueError(f"Invalid URL: {url}")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


_BROWSER_UAS: list[str] = [
    # Recent macOS Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    # Recent Chrome on Linux (Tavily/Cloudflare-friendly)
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.91 Safari/537.36",
    # Internal UA (kept for transparency / observability)
    "FundWebsiteScreening/1.0",
]


_COMMON_TLD_SWAPS = {
    "pl": ["com", "io", "co", "ai", "eu"],
    "com": ["pl", "io", "co"],
    "io": ["com", "co", "ai"],
    "co": ["com", "io"],
    "ai": ["com", "io"],
    "eu": ["com", "io"],
}


def _origin_variants(root_url: str, *, include_tld_swaps: bool = False) -> list[str]:
    """Return candidate origins (https/http x www/non-www) preserving original first.

    When include_tld_swaps=True, also append a small set of plausible TLD swaps
    (e.g. flyingbisons.pl -> flyingbisons.com) for the case where DNS for the
    user-supplied TLD does not resolve at all.
    """
    parsed = urlparse(root_url)
    host = (parsed.netloc or "").lower()
    if not host:
        return [root_url]
    bare = host[4:] if host.startswith("www.") else host
    www = host if host.startswith("www.") else f"www.{host}"
    schemes = ["https", "http"]
    hosts: list[str] = []
    for h in [host, bare, www]:
        if h and h not in hosts:
            hosts.append(h)
    if include_tld_swaps:
        parts = bare.split(".")
        if len(parts) >= 2:
            tld = parts[-1]
            stem = ".".join(parts[:-1])
            swaps = _COMMON_TLD_SWAPS.get(tld, [])
            for new_tld in swaps:
                cand_bare = f"{stem}.{new_tld}"
                cand_www = f"www.{cand_bare}"
                for h in (cand_bare, cand_www):
                    if h not in hosts:
                        hosts.append(h)
    out: list[str] = []
    seen: set[str] = set()
    for s in schemes:
        for h in hosts:
            v = f"{s}://{h}"
            if v not in seen:
                seen.add(v)
                out.append(v)
    return out


def _resolve_reachable_origin(root_url: str) -> tuple[str, list[str]]:
    """Probe origin variants with HEAD/GET; return first reachable origin + warnings.

    We use this so that single-shot crawl of `https://flyingbisons.pl` does not
    silently return zero content when the site only resolves on `www.` or `https`.
    Two passes:
      1) host-as-given x scheme/www variants
      2) common TLD swaps (e.g. .pl -> .com) when no variant resolved
    """
    warnings: list[str] = []
    parsed = urlparse(root_url)
    host = (parsed.netloc or "").lower()
    base_origin = f"{parsed.scheme}://{host}"

    def _probe(origin: str) -> Optional[str]:
        for ua in _BROWSER_UAS[:2]:
            try:
                r = httpx.get(
                    origin + "/",
                    timeout=8.0,
                    follow_redirects=True,
                    headers={
                        "User-Agent": ua,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en;q=0.9, pl;q=0.8",
                    },
                )
                if r.status_code < 400 and (r.text or "").strip():
                    final_host = (urlparse(str(r.url)).netloc or host).lower()
                    return f"{urlparse(str(r.url)).scheme}://{final_host}"
            except Exception as exc:
                warnings.append(f"origin_probe_failed {origin}: {exc.__class__.__name__}")
                continue
        return None

    for origin in _origin_variants(root_url):
        final_origin = _probe(origin)
        if final_origin:
            if final_origin != base_origin:
                warnings.append(f"origin_resolved: {origin} -> {final_origin}")
            return final_origin, warnings

    # Pass 2: TLD swaps. Only used when nothing resolved in pass 1, to avoid
    # accidentally pointing the resolver at a different company on a different TLD.
    swap_candidates = [
        v for v in _origin_variants(root_url, include_tld_swaps=True) if v not in _origin_variants(root_url)
    ]
    for origin in swap_candidates:
        final_origin = _probe(origin)
        if final_origin:
            warnings.append(f"origin_tld_swap: {root_url} -> {final_origin}")
            return final_origin, warnings

    return base_origin, warnings


def _same_site(url: str, root: str) -> bool:
    def _norm(host: str) -> str:
        h = (host or "").lower().strip()
        return h[4:] if h.startswith("www.") else h

    try:
        return _norm(urlparse(url).netloc) == _norm(urlparse(root).netloc)
    except Exception:
        return False


def _bare_hostname(netloc: str) -> str:
    h = (netloc or "").lower().strip().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def _marketing_mirror_hosts(seed_url: str) -> set[str]:
    """stem.io and stem.com often point at the same marketing site; crawl both for /news, /blog, etc."""
    h = _bare_hostname(urlparse(seed_url).netloc)
    out: set[str] = {h}
    if "." not in h:
        return out
    stem, tld = h.rsplit(".", 1)
    if tld in ("io", "co", "app", "ai", "dev", "tech"):
        out.add(f"{stem}.com")
    elif tld == "com":
        out.add(f"{stem}.io")
    return out


def _host_allowed_for_mirror(url: str, mirror_hosts: set[str]) -> bool:
    try:
        return _bare_hostname(urlparse(url).netloc) in mirror_hosts
    except Exception:
        return False


def _extract_meta_description(html: str) -> str:
    hit = re.search(
        r"<meta[^>]+(?:name|property)=[\"'](?:description|og:description)[\"'][^>]+content=[\"']([^\"']+)[\"']",
        html,
        flags=re.I,
    )
    return (hit.group(1) if hit else "").strip()


def _extract_title(html: str) -> str:
    hit = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.I | re.S)
    return re.sub(r"\s+", " ", hit.group(1)).strip() if hit else ""


def _schema_to_markdown(structured_data: dict[str, Any]) -> str:
    if not structured_data:
        return ""
    try:
        blob = json.dumps(structured_data, ensure_ascii=False)
    except Exception:
        return ""
    lines: list[str] = []
    for m in re.finditer(r'"(?:name|legalName)"\s*:\s*"([^"]{2,120})"', blob):
        text = m.group(1).strip()
        if text.lower() not in {"logo", "home", "about"}:
            lines.append(f"- schema_name: {text}")
        if len(lines) >= 10:
            break

    # Address signals (Organization / LocalBusiness / PostalAddress in JSON-LD).
    # Keep this as compact markdown hints — deterministic enrichment reads these.
    addr_bits: list[str] = []
    for key in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"):
        for m in re.finditer(rf'"{key}"\s*:\s*"([^"]{{2,140}})"', blob):
            val = m.group(1).strip()
            if val and val.lower() not in {"unknown", "n/a", "none"}:
                addr_bits.append(val)
                if len(addr_bits) >= 6:
                    break
        if len(addr_bits) >= 6:
            break
    if addr_bits:
        # De-dupe while preserving order (avoid repeating country twice).
        seen: set[str] = set()
        uniq: list[str] = []
        for x in addr_bits:
            k = x.strip().lower()
            if not k or k in seen:
                continue
            seen.add(k)
            uniq.append(x.strip())
        lines.append(f"- schema_address: {', '.join(uniq[:6])}")

    # sameAs links can include LinkedIn; keep only a tiny hint.
    sameas = re.findall(r'"sameAs"\s*:\s*\[\s*([^\]]+)\]', blob)
    if sameas:
        raw = sameas[0]
        urls = re.findall(r'"(https?://[^"]{6,300})"', raw)
        li = [u for u in urls if "linkedin.com/company" in u.lower()]
        if li:
            lines.append(f"- schema_linkedin: {li[0]}")
    return "## Structured data\n" + "\n".join(lines) if lines else ""


def _dedupe_repeated_lines(markdown: str) -> str:
    raw_lines = markdown.splitlines()
    norm = [ln.strip() for ln in raw_lines]
    counts = Counter(
        ln
        for ln in norm
        if ln and len(ln) <= 120 and not ln.startswith("## Source:") and not ln.startswith("#")
    )
    drop = {ln for ln, c in counts.items() if c >= 3}
    if not drop:
        return markdown
    kept = [ln for ln in raw_lines if ln.strip() not in drop]
    return "\n".join(kept).strip() or markdown


def _is_article_style_page(url: str) -> bool:
    """Blog posts / news articles (not just the homepage)."""
    path_s = urlparse(url).path.lower()
    if "/post/" in path_s:
        return True
    if "/news/" in path_s or path_s.rstrip("/").endswith("/news"):
        return True
    if "/article" in path_s:
        return True
    parts = [p for p in path_s.split("/") if p]
    if len(parts) >= 2 and parts[0] == "blog":
        return True
    return False


def _grant_boilerplate_lv(md: str) -> bool:
    """Heavy Latvian EU-grant boilerplate (skip as 'news' excerpt)."""
    s = (md or "").lower()
    if len(s) < 350:
        return False
    return "atveseļošanas" in s and "energoefektivitātes" in s


def _published_date_hint(html: str) -> str:
    if not html:
        return ""
    m = re.search(
        r'(?:property|name)=["\']article:published_time["\']\s+(?:content|value)=["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )
    if not m:
        m = re.search(
            r'(?:content|value)=["\']([^"\']+)["\']\s+(?:property|name)=["\']article:published_time["\']',
            html,
            flags=re.I,
        )
    if m:
        return m.group(1).strip()[:10]
    m2 = re.search(r"<time[^>]+datetime=[\"']([^\"']+)[\"']", html, flags=re.I)
    if m2:
        return m2.group(1).strip()[:10]
    return ""


def _first_heading_md(md: str) -> str:
    for line in (md or "").splitlines():
        t = line.strip()
        if t.startswith("#"):
            return re.sub(r"^#+\s*", "", t).strip()[:180]
    return ""


def _looks_like_error_or_missing_page(p: WebsitePageRecord) -> bool:
    """Skip soft-404 marketing pages (e.g. Wix returns 200 with 'Page Not Found' body)."""
    sc = p.status_code
    if sc is not None and sc >= 400:
        return True
    md = (p.markdown or "").lower()
    title = (p.title or "").strip().lower()
    if "404" in title and ("not found" in title or "error" in title):
        return True
    if title == "page not found" or title.endswith(": page not found"):
        return True
    if "this page isn't available" in md or "this page is not available" in md:
        return True
    if "page not found" in md and ("404" in md or "error" in title):
        return True
    return False


def _excerpt_for_digest(md: str, limit: int = 700) -> str:
    lines_out: list[str] = []
    skip_img = False
    for line in (md or "").splitlines():
        st = line.strip()
        if st.startswith("### Images on this page"):
            skip_img = True
            continue
        if skip_img:
            if st.startswith("#"):
                skip_img = False
            else:
                continue
        if st.startswith("## Structured data"):
            break
        lines_out.append(line)
    blob = "\n".join(lines_out).strip()
    blob = re.sub(r"\n{3,}", "\n\n", blob)
    if len(blob) > limit:
        blob = blob[: limit - 1].rsplit("\n", 1)[0] + "…"
    return blob


def _build_news_posts_digest(pages: list[WebsitePageRecord]) -> str:
    arts: list[WebsitePageRecord] = []
    for p in pages:
        if not p.fetch_ok:
            continue
        if not _is_article_style_page(p.url):
            continue
        md = p.markdown or ""
        if _grant_boilerplate_lv(md):
            continue
        if _looks_like_error_or_missing_page(p):
            continue
        arts.append(p)
    if not arts:
        return ""
    arts.sort(
        key=lambda r: (_published_date_hint(r.raw_html or "") or "1970-01-01", r.url),
        reverse=True,
    )
    lines = [
        "## News & posts (digest)",
        "",
        "Numbered excerpts from article URLs; full pages are repeated below with `## Source:`.",
        "",
    ]
    for i, p in enumerate(arts, start=1):
        date = _published_date_hint(p.raw_html or "")
        title = (p.title or "").strip()
        low_title = title.lower()
        if not title or low_title in ("privacy policy", "terms of service", "cookie policy"):
            title = _first_heading_md(p.markdown or "") or urlparse(p.url).path
        head = f"### #{i}"
        if date:
            head += f" — {date}"
        head += f" — {title}"
        lines.append(head)
        lines.append("")
        lines.append(p.url)
        lines.append("")
        lines.append(_excerpt_for_digest(p.markdown or "", 720))
        lines.append("")
    return "\n".join(lines).rstrip()


def _quality_heuristic(pages: list[WebsitePageRecord], combined_len: int) -> int:
    if not pages:
        return 1
    ok = sum(1 for p in pages if p.fetch_ok and p.text_length > 100)
    score = 4 + (1 if ok >= 1 else 0) + (2 if ok >= 3 else 0)
    if combined_len > 3000:
        score += 1
    if combined_len > 12000:
        score += 1
    if len(pages) >= 5:
        score += 1
    return max(1, min(10, score))


async def _crawl_pages(
    root_url: str,
    max_pages: int,
    *,
    max_seed_urls: int = DEFAULT_MAX_SEED_URLS,
) -> tuple[list[WebsitePageRecord], list[str]]:
    pages: list[WebsitePageRecord] = []
    warnings: list[str] = []
    run_cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        wait_until="domcontentloaded",
        page_timeout=25_000,
        wait_for_timeout=8_000,
        remove_consent_popups=True,
        magic=True,
        verbose=False,
        max_retries=0,
    )
    consecutive_failures = 0
    link_root = root_url
    mirror_hosts: set[str] = _marketing_mirror_hosts(link_root)

    async def crawl_one(crawler: Any, u: str) -> Optional[WebsitePageRecord]:
        nonlocal consecutive_failures, link_root, mirror_hosts
        try:
            result = await crawler.arun(url=u, config=run_cfg)
        except Exception as exc:
            fb = _fallback_http_extract(u)
            if fb is not None:
                warnings.append(f"{u}: crawl4ai_failed_fallback_used")
                consecutive_failures = 0
                return fb
            warnings.append(f"{u}: {exc}")
            consecutive_failures += 1
            return None

        final_url = (getattr(result, "redirected_url", None) or getattr(result, "url", None) or u).strip()
        if not _host_allowed_for_mirror(final_url, mirror_hosts):
            return None
        html_raw = (getattr(result, "html", None) or "").strip()
        html_clean = (getattr(result, "cleaned_html", None) or "").strip()
        body_html = html_clean or html_raw
        status_code = getattr(result, "status_code", None)
        if not getattr(result, "success", False) or not body_html:
            fb = _fallback_http_extract(final_url)
            if fb is not None:
                warnings.append(f"{final_url}: crawl4ai_unreadable_fallback_used")
                consecutive_failures = 0
                return fb
            consecutive_failures += 1
            return None

        base_html = html_raw if html_raw else body_html
        text = trafilatura.extract(
            body_html,
            url=final_url,
            include_links=True,
            include_tables=True,
            include_formatting=True,
            favor_precision=True,
        ) or ""
        img_md = _append_image_markdown(base_html, final_url)
        schema_md = ""
        with contextlib.suppress(Exception):
            schema_md = _schema_to_markdown(extruct.extract(base_html, base_url=final_url))
        markdown = text.strip()
        if img_md:
            markdown = (markdown + img_md).strip() if markdown else img_md.strip()
        if schema_md:
            markdown = (markdown + "\n\n" + schema_md).strip() if markdown else schema_md.strip()
        consecutive_failures = 0
        return WebsitePageRecord(
            url=final_url,
            title=_extract_title(base_html),
            meta_description=_extract_meta_description(base_html),
            raw_html=base_html[:500_000],
            markdown=markdown,
            text_length=len(markdown),
            fetch_ok=True,
            status_code=status_code,
        )

    async with AsyncWebCrawler() as crawler:
        p0 = urlparse(root_url)
        home_guess = f"{p0.scheme}://{p0.netloc}/"
        rec0 = await crawl_one(crawler, home_guess)
        if rec0 is None:
            rec0 = await crawl_one(crawler, root_url.rstrip("/") or root_url)

        discovered: list[str] = []
        link_root = root_url
        mirror_hosts = _marketing_mirror_hosts(link_root)
        if rec0:
            pages.append(rec0)
            link_root = rec0.url
            mirror_hosts = _marketing_mirror_hosts(link_root)
            raw = rec0.raw_html or ""
            if rec0.fetch_ok and raw:
                discovered = _extract_same_origin_links(
                    raw, rec0.url, link_root, mirror_hosts=mirror_hosts
                )
                warnings.append(f"link_discovery: {len(discovered)} same-origin links from homepage")

        scheme_lr = urlparse(link_root).scheme or "https"
        sm: list[str] = []
        for host in sorted(mirror_hosts):
            sm.extend(
                _fetch_sitemap_same_origin_urls(f"{scheme_lr}://{host}", mirror_hosts, warnings)
            )
        if sm:
            discovered = _merge_discovered_url_lists(discovered, sm)

        seed_list = _build_url_queue(link_root, discovered, max_seed_urls=max_seed_urls)
        visited: set[str] = {_normalize_url_key(p.url) for p in pages}
        pending_seen: set[str] = set(visited)
        pending: deque[str] = deque()
        for u in seed_list:
            nu = _normalize_url_key(u)
            if nu in pending_seen:
                continue
            pending_seen.add(nu)
            pending.append(u)

        max_pending = max(max_pages * 25, 400)

        while pending and len(pages) < max_pages:
            u = pending.popleft()
            nu = _normalize_url_key(u)
            if nu in visited:
                continue
            visited.add(nu)
            rec = await crawl_one(crawler, u)
            if rec is None:
                if consecutive_failures >= 3:
                    warnings.append("crawl_stopped_early: too many consecutive failures")
                    break
                continue
            pages.append(rec)
            html_more = (rec.raw_html or "").strip()
            if html_more and len(pending) < max_pending:
                more = _extract_same_origin_links(
                    html_more, rec.url, link_root, mirror_hosts=mirror_hosts
                )
                for link in _sort_discovered_urls(more):
                    lk = _normalize_url_key(link)
                    if lk not in pending_seen and lk not in visited:
                        pending_seen.add(lk)
                        pending.append(link)

    return pages, warnings


def _fallback_http_extract(url: str) -> Optional[WebsitePageRecord]:
    """Direct httpx GET with multiple browser UAs and full redirect follow."""
    r = None
    for ua in _BROWSER_UAS:
        try:
            r = httpx.get(
                url,
                timeout=12.0,
                follow_redirects=True,
                headers={
                    "User-Agent": ua,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en;q=0.9, pl;q=0.8",
                },
            )
            if r.status_code < 400 and (r.text or "").strip():
                break
            r = None
        except Exception:
            r = None
            continue
    if r is None:
        return None
    try:
        if r.status_code >= 400 or not (r.text or "").strip():
            return None
        html = r.text
        text = trafilatura.extract(html, url=str(r.url), include_links=True, include_tables=True, favor_precision=True) or ""
        img_md = _append_image_markdown(html, str(r.url))
        schema_md = ""
        with contextlib.suppress(Exception):
            schema_md = _schema_to_markdown(extruct.extract(html, base_url=str(r.url)))
        markdown = text.strip()
        if img_md:
            markdown = (markdown + img_md).strip() if markdown else img_md.strip()
        if schema_md:
            markdown = (markdown + "\n\n" + schema_md).strip() if markdown else schema_md.strip()
        if not (markdown or "").strip():
            return None
        return WebsitePageRecord(
            url=str(r.url),
            title=_extract_title(html),
            meta_description=_extract_meta_description(html),
            raw_html=html[:500_000],
            markdown=markdown,
            text_length=len(markdown),
            fetch_ok=True,
            status_code=r.status_code,
        )
    except Exception:
        return None


def fetch_website_markdown(
    url: str,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_seed_urls: Optional[int] = None,
    timeout_seconds: int = DEFAULT_TIMEOUT,
) -> WebsiteMarkdownResult:
    root_url = normalize_root_url(url)
    ms = (
        max_seed_urls
        if max_seed_urls is not None
        else int(os.getenv("WEB_CRAWL_MAX_SEED_URLS", str(DEFAULT_MAX_SEED_URLS)) or DEFAULT_MAX_SEED_URLS)
    )
    # Probe origin variants (https/http x www/non-www) before kicking off Crawl4AI.
    # This avoids "INSUFFICIENT_EVIDENCE / 0 search_calls" outcomes for sites that
    # only resolve on www. or http://, e.g. flyingbisons.pl -> https://www.flyingbisons.com.
    resolved_origin, origin_warnings = _resolve_reachable_origin(root_url)
    if resolved_origin and resolved_origin not in (root_url, root_url.rstrip("/")):
        try:
            root_url = normalize_root_url(resolved_origin)
        except Exception:
            pass
    try:
        pages, warnings = asyncio.run(
            _crawl_pages(root_url, max_pages=max_pages, max_seed_urls=ms)
        )
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            pages, warnings = loop.run_until_complete(
                _crawl_pages(root_url, max_pages=max_pages, max_seed_urls=ms)
            )
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    warnings = list(origin_warnings) + list(warnings or [])

    # If Crawl4AI still produced nothing usable, retry probing each remaining
    # origin variant via plain httpx -- this rescues sites that block headless
    # browsers but answer fine to direct GETs with browser UAs.
    if not any(p.fetch_ok and (p.markdown or "").strip() for p in (pages or [])):
        rescued: list[WebsitePageRecord] = []
        for origin in _origin_variants(root_url):
            for path in PROBE_PATHS:
                full = origin if path == "/" else origin + path
                rec = _fallback_http_extract(full)
                if rec is not None:
                    rescued.append(rec)
                    if len(rescued) >= max_pages:
                        break
            if rescued:
                break
        if rescued:
            warnings.append(f"crawl_rescued_via_httpx_origin: {urlparse(rescued[0].url).scheme}://{urlparse(rescued[0].url).netloc}")
            pages = list(pages or []) + rescued

    _ = timeout_seconds
    ok_pages = [p for p in pages if p.fetch_ok and _page_markdown_useful(p.markdown)]
    combined = "\n\n---\n\n".join(f"## Source: {p.url}\n\n{p.markdown}" for p in ok_pages)
    combined = _dedupe_repeated_lines(combined)
    if os.getenv("WEB_CRAWL_NEWS_DIGEST", "1").strip().lower() not in ("0", "false", "no", "off"):
        digest = _build_news_posts_digest([p for p in (pages or []) if p.fetch_ok])
        if digest:
            combined = digest + "\n\n---\n\n" + combined
    if not combined.strip():
        warnings.append("No readable text extracted from crawled pages.")
    return WebsiteMarkdownResult(
        root_url=root_url,
        pages=pages,
        combined_markdown=combined or "(empty)",
        fetch_warnings=warnings,
        extraction_quality_score=_quality_heuristic(pages, len(combined)),
    )
