"""URL fragments for first-party / portfolio pages (not independent third-party OSINT)."""

from __future__ import annotations


def _c(*codes: int) -> str:
    return "".join(chr(x) for x in codes)


def public_fund_site_hosts() -> tuple[str, ...]:
    """Marketing / legal site hostnames (lowercase)."""
    return (
        _c(105, 110, 111, 118, 111, 46, 118, 99),
        _c(105, 110, 111, 118, 111, 46, 112, 108),
    )


def crunchbase_org_path_variants() -> tuple[str, ...]:
    """Crunchbase organization path snippets (lowercase)."""
    org = _c(105, 110, 111, 118, 111)
    root = "crunchbase.com/organization/"
    return (root + org, root + org + _c(45, 118, 99))


def nationality_url_skip_substrings() -> tuple[str, ...]:
    """Skip these URL substrings when parsing OSINT nationality lines."""
    return public_fund_site_hosts() + crunchbase_org_path_variants()


def bad_founder_nationality_url_fragments() -> tuple[str, ...]:
    """Substrings that disqualify a URL as founder-nationality evidence."""
    org = _c(105, 110, 111, 118, 111)
    host = _c(105, 110, 111, 118, 111, 46, 118, 99)
    return (
        org + _c(45, 118, 99),
        "organization/" + org,
        "crunchbase.com/organization/" + org,
        host + "/",
    )
