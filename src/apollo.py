"""Apollo.io integration: find people by company domain + title, enrich for email.

Search is always free. Enrichment (to reveal an email) costs 1 Apollo credit and only
runs when you explicitly call find_founder_email(). It is never triggered automatically
by scraping, email sending, or any CLI command.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .config import APOLLO_API_KEY, APOLLO_CONTACT_TITLES

_BASE = "https://api.apollo.io"
_TIMEOUT = 15


def _domain_from_url(url: str) -> str | None:
    try:
        host = urlparse(url).netloc or urlparse(url).path
        host = host.lower().removeprefix("www.")
        return host if "." in host else None
    except Exception:
        return None


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "x-api-key": APOLLO_API_KEY,
    }


def search_people_at_domain(
    domain: str,
    titles: list[str] | None = None,
    per_page: int = 5,
) -> list[dict]:
    """
    Search Apollo for people at a company domain filtered by title/seniority.
    This endpoint is FREE — no credit cost.
    """
    if not APOLLO_API_KEY:
        return []

    titles = titles or APOLLO_CONTACT_TITLES
    payload: dict = {
        "q_organization_domains_list": [domain],
        "person_titles": titles,
        "person_seniorities": ["founder", "owner", "c_suite"],
        "per_page": per_page,
        "page": 1,
    }

    try:
        resp = httpx.post(
            f"{_BASE}/api/v1/mixed_people/api_search",
            headers=_headers(),
            json=payload,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            print(f"  Apollo search: HTTP {resp.status_code}")
            return []
        data = resp.json()
        return data.get("people", [])
    except Exception as e:
        print(f"  Apollo search error: {e}")
        return []


def enrich_person_email(
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    domain: str | None = None,
    email: str | None = None,
    apollo_id: str | None = None,
) -> dict | None:
    """
    Reveal a person's verified work email via Apollo.
    Costs 1 credit per successful match. Only called from find_founder_email()
    when the free search didn't already have the email.
    """
    if not APOLLO_API_KEY:
        return None

    payload: dict = {"reveal_personal_emails": False}
    if apollo_id:
        payload["id"] = apollo_id
    if email:
        payload["email"] = email
    if first_name:
        payload["first_name"] = first_name
    if last_name:
        payload["last_name"] = last_name
    if domain:
        payload["organization_domain"] = domain

    try:
        resp = httpx.post(
            f"{_BASE}/api/v1/people/match",
            headers=_headers(),
            json=payload,
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            print(f"  Apollo enrich: HTTP {resp.status_code}")
            return None
        data = resp.json()
        return data.get("person")
    except Exception as e:
        print(f"  Apollo enrich error: {e}")
        return None


def find_founder_email(
    org_name: str,
    domain: str | None = None,
    source_url: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """
    Find a founder/CEO at a company and return their email.

    Flow:
      1. Free people search (no credits) — if the email is already on file, return it
      2. If search found a person but no email, spend 1 credit to reveal it

    This function is never called automatically. It only runs when you explicitly
    invoke it in Python (e.g. find_founder_email("Readily", domain="readily.co")).

    Returns (contact_name, email, title) or (None, None, None).
    """
    if not APOLLO_API_KEY:
        return None, None, None

    d = domain
    if not d and source_url:
        d = _domain_from_url(source_url)
    if not d:
        d = org_name.lower().replace(" ", "") + ".com"

    print(f"  Apollo: searching for founders at {d}...")
    people = search_people_at_domain(d)

    if not people:
        alt_domains = [
            org_name.lower().replace(" ", "") + ".co",
            org_name.lower().replace(" ", "-") + ".com",
        ]
        for alt in alt_domains:
            if alt != d:
                people = search_people_at_domain(alt)
                if people:
                    d = alt
                    break

    if not people:
        print(f"  Apollo: no founders found for {d}")
        return None, None, None

    best = people[0]
    name = best.get("name") or ""
    title = best.get("title") or ""
    email_addr = best.get("email")

    if email_addr:
        print(f"  Apollo: found {name} ({title}) — {email_addr} (free, no credit used)")
        return name, email_addr, title

    apollo_id = best.get("id")
    first = best.get("first_name")
    last = best.get("last_name")

    print(f"  Apollo: found {name} ({title}), revealing email (1 credit)...")
    person = enrich_person_email(
        first_name=first,
        last_name=last,
        domain=d,
        apollo_id=apollo_id,
    )
    if person:
        email_addr = person.get("email")
        if email_addr:
            print(f"  Apollo: revealed — {email_addr}")
            return name or person.get("name", ""), email_addr, title or person.get("title", "")

    print(f"  Apollo: could not get email for {name}")
    return name, None, title
