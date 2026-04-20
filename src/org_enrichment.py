"""
Resolve company homepages via You.com (YDC) web search, then generate a short
"work towards …" phrase via Groq or OpenRouter (both have free tiers).

When enriching, the live ARPA-H ADVOCATE Teaming table is scraped so each org's official
"research focus" text can ground the phrase (see ADVOCATE_TEAMING_URL).
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .config import (
    YDC_API_KEY,
    YDC_SEARCH_COUNT,
    ENRICH_LLM_PRIMARY,
    GROQ_API_KEY,
    GROQ_MODEL,
    OPENROUTER_API_KEY,
    OPENROUTER_HTTP_REFERER,
    OPENROUTER_ENRICH_GAP_SEC,
    OPENROUTER_MODEL,
    REQUEST_DELAY_SEC,
)
from .extractor import ExtractedData

SKIP_RESULT_DOMAINS = frozenset(
    {
        "linkedin.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "instagram.com",
        "youtube.com",
        "youtu.be",
        "crunchbase.com",
        "bloomberg.com",
        "pitchbook.com",
        "wikipedia.org",
        "wikimedia.org",
        "reddit.com",
        "medium.com",
        "arxiv.org",
        "nih.gov",
        "google.com",
        "bing.com",
    }
)

_YDC_SEARCH_URL = "https://ydc-index.io/v1/search"
_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
ADVOCATE_TEAMING_URL = (
    "https://arpa-h.gov/explore-funding/programs/advocate/teaming"
)


def _norm_org_key(organization: str) -> str:
    return " ".join(organization.strip().lower().split())


def fetch_advocate_teaming_research_by_org(
    *,
    timeout: float = 60.0,
) -> dict[str, str]:
    """
    Map normalized organization name -> "research focus" cell text from the public
    ADVOCATE Teaming table on arpa-h.gov.
    """
    out: dict[str, str] = {}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            r = client.get(ADVOCATE_TEAMING_URL)
            r.raise_for_status()
    except Exception:
        return out
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        return out
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 5:
            continue
        org = tds[1].get_text(strip=True)
        research = tds[4].get_text(strip=True)
        if not org or not research:
            continue
        k = _norm_org_key(org)
        prev = out.get(k)
        if prev is None or len(research) > len(prev):
            out[k] = research
    return out


def _domain_blocked(netloc: str) -> bool:
    d = (netloc or "").lower()
    if d.startswith("www."):
        d = d[4:]
    return any(d == blocked or d.endswith("." + blocked) for blocked in SKIP_RESULT_DOMAINS)


def _hints_from_item(item: ExtractedData) -> dict[str, str | None]:
    email = item.emails[0] if item.emails else ""
    domain = None
    if "@" in email:
        domain = email.split("@")[-1].lower().strip()
    return {"contact_name": item.contact_name, "email_domain": domain}


_SKIP_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "icloud.com",
        "protonmail.com",
        "aol.com",
        "live.com",
        "msn.com",
        "me.com",
        "googlemail.com",
    }
)


def build_search_query(organization: str, hints: dict[str, str | None]) -> str:
    org = " ".join((organization or "").split())
    parts = [f'"{org}"', "official", "website"]
    dom = hints.get("email_domain") or ""
    if dom and dom not in _SKIP_EMAIL_DOMAINS:
        parts.append(dom)
    return " ".join(parts)


def web_search(query: str, num: int = 10) -> list[dict[str, str]]:
    """Run a You.com (YDC) web search for org resolution."""
    if not YDC_API_KEY:
        raise ValueError(
            "Web search not configured. Set YDC_API_KEY in .env "
            "(get one free at https://documentation.you.com/)."
        )
    count = min(max(1, num), 20)
    headers = {"X-API-Key": YDC_API_KEY}
    params = {"query": query, "count": min(count, YDC_SEARCH_COUNT)}
    with httpx.Client(timeout=45.0) as client:
        r = client.get(_YDC_SEARCH_URL, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
    web = (data.get("results") or {}).get("web") or []
    out: list[dict[str, str]] = []
    for item in web:
        link = (item.get("url") or "").strip()
        if not link.startswith("http"):
            continue
        title = (item.get("title") or "").strip()
        desc = (item.get("description") or "").strip()
        snippets = item.get("snippets") or []
        if isinstance(snippets, list) and snippets:
            desc = f"{desc} {' '.join(str(x) for x in snippets[:5])}".strip()
        out.append(
            {
                "link": link.split("#")[0],
                "title": title,
                "snippet": desc,
            }
        )
    return out


def pick_homepage_url(results: list[dict[str, str]]) -> str | None:
    for it in results:
        link = it.get("link") or ""
        try:
            parsed = urlparse(link)
        except Exception:
            continue
        if parsed.scheme not in ("http", "https"):
            continue
        if _domain_blocked(parsed.netloc):
            continue
        return link
    return None


def format_snippets_for_llm(results: list[dict[str, str]], max_items: int = 6) -> list[str]:
    lines = []
    cap = 380
    for it in results[:max_items]:
        t = it.get("title", "")
        s = it.get("snippet", "")
        if t or s:
            line = f"{t}: {s}".strip(": ").strip()
            if len(line) > cap:
                line = line[: cap - 1] + "…"
            lines.append(line)
    return lines


def _strip_llm_json_text(raw: str) -> str:
    t = raw.strip()
    m = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", t, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return t


def _json_from_reasoning_prose(text: str) -> str | None:
    if not text or '{"approach_phrase"' not in text:
        return None
    idx = text.rfind('{"approach_phrase"')
    if idx < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text, idx)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and "approach_phrase" in obj:
        return json.dumps(obj)
    return None


def _strip_json_object(text: str) -> dict | None:
    text = _strip_llm_json_text(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    i = text.find("{")
    j = text.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(text[i : j + 1])
        except json.JSONDecodeError:
            pass
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


def _validate_phrase(phrase: str, organization: str) -> str | None:
    phrase = " ".join(phrase.split()).strip()
    if len(phrase) < 4 or len(phrase) > 72:
        return None
    if len(phrase.split()) > 7:
        return None
    if phrase.endswith("."):
        phrase = phrase.rstrip(".")
    low = phrase.lower()
    org_compact = " ".join(organization.lower().split())
    if len(org_compact) > 4 and org_compact in low:
        return None
    return phrase


def generate_approach_phrase(
    organization: str,
    website: str | None,
    snippets: list[str],
    *,
    arpa_h_listing_research: str | None = None,
) -> str | None:
    """
    Short fragment for email templates, e.g. "found the work towards {phrase} very compelling."
    Max ~7 words / 72 chars; no fabricated claims.
    """
    context = "\n".join(snippets[:8]) if snippets else "(no search snippets)"
    if len(context) > 3200:
        context = context[:3199] + "…"
    site_line = f"Resolved website (may be empty): {website or 'unknown'}\n"

    primary_block = ""
    if arpa_h_listing_research and arpa_h_listing_research.strip():
        arpa = " ".join(arpa_h_listing_research.split())
        if len(arpa) > 2800:
            arpa = arpa[:2799] + "…"
        primary_block = (
            "PRIMARY SOURCE (verbatim \"Research focus\" for this organization from the official "
            f"ARPA-H ADVOCATE Teaming directory: {ADVOCATE_TEAMING_URL}):\n"
            f"{arpa}\n\n"
            "Accuracy rules: The phrase you output MUST be directly supported by the PRIMARY SOURCE above. "
            "Treat it as ground truth. Do not add products, FDA status, clinical outcomes, or partnerships "
            "that are not stated or clearly implied there. If the listing emphasizes defense or general AI "
            "without clinical care, reflect that honestly—do not reframe as cardiovascular-specific unless "
            "the primary source supports it (e.g. their stated teaming interests may mention cardiovascular).\n"
            "Secondary context (web search snippets only): use to disambiguate the company name or website; "
            "do NOT use snippets to introduce factual claims absent from the PRIMARY SOURCE.\n\n"
        )
    else:
        primary_block = (
            "No matching row was found on the ARPA-H ADVOCATE Teaming table for this exact organization name. "
            "Use ONLY the web search snippets below for factual claims. If snippets are thin or ambiguous, "
            "prefer a cautious, general phrase (e.g. \"AI systems for operational settings\") and do NOT "
            "invent clinical, FDA, or cardiovascular specifics.\n\n"
        )

    user_prompt = (
        f"Company name: {organization}\n"
        f"{site_line}"
        f"{primary_block}"
        f"Web search result lines (secondary):\n{context}\n\n"
        "Reader sentence (you only fill the blank ___):\n"
        "\"I recently came across [company] and found the work towards ___ very compelling.\"\n"
        "[Company] is filled separately by the email software—your string completes only \"work towards ___\". "
        "The blank must read as natural English after \"work towards\" (noun phrase or gerund phrase), "
        "e.g. \"deployable health-adjacent sensing and AI\", \"agentic clinical decision support\". "
        "Short, human, not a tagline stack. Avoid buzzword piles.\n"
        "Audience: ARPA-H ADVOCATE (Agentic AI–Enabled Cardiovascular Care Transformation). "
        "When the primary source supports a care or cardiovascular angle, prefer that wording; "
        "when it does not, stay faithful to what they actually wrote.\n"
        "Hard style rules: max 7 words; max ~70 characters; no company name; no period at the end; "
        "no quotation marks inside the string.\n"
        "Return ONLY JSON: {\"approach_phrase\": \"...\"}."
    )
    system = (
        "You output only valid JSON: {\"approach_phrase\": \"...\"}. "
        "The value is a short phrase that completes \"work towards ___\" in a cold email. "
        "Prioritize factual fidelity to the ARPA-H teaming profile when provided; never invent claims."
    )

    raw = _llm_json_completion(system, user_prompt)
    if not raw:
        return None
    data = _strip_json_object(raw)
    if not data or "approach_phrase" not in data:
        return None
    phrase = data.get("approach_phrase")
    if not isinstance(phrase, str):
        return None
    return _validate_phrase(phrase, organization)


def _groq_chat_completion(system: str, user: str) -> str | None:
    if not GROQ_API_KEY:
        return None
    from groq import Groq
    from groq import APIStatusError, RateLimitError

    client = Groq(api_key=GROQ_API_KEY)
    max_attempts = 10
    completion = None
    for attempt in range(max_attempts):
        try:
            completion = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.3,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            break
        except RateLimitError:
            if attempt + 1 < max_attempts:
                wait_s = min(120.0, 8.0 * (2**attempt))
                print(
                    f"    (Groq rate limit — backing off {wait_s:.0f}s, "
                    f"attempt {attempt + 1}/{max_attempts})"
                )
                time.sleep(wait_s)
                continue
            print("    (Groq retries exhausted; trying fallback if configured.)")
            return None
        except APIStatusError as e:
            if getattr(e, "status_code", None) == 429:
                if attempt + 1 < max_attempts:
                    wait_s = min(120.0, 8.0 * (2**attempt))
                    print(
                        f"    (Groq 429 — backing off {wait_s:.0f}s, "
                        f"attempt {attempt + 1}/{max_attempts})"
                    )
                    time.sleep(wait_s)
                    continue
                print("    (Groq retries exhausted; trying fallback if configured.)")
                return None
            return None
        except Exception:
            return None
    if not completion:
        return None
    if completion.choices:
        c = completion.choices[0].message.content
        return c if isinstance(c, str) else None
    return None


def _llm_json_completion(system: str, user: str) -> str | None:
    primary = (ENRICH_LLM_PRIMARY or "groq").strip().lower()
    if primary not in ("groq", "openrouter"):
        primary = "groq"

    def or_complete() -> str | None:
        if not OPENROUTER_API_KEY:
            return None
        return _openrouter_chat_completion(system, user)

    def groq_complete() -> str | None:
        return _groq_chat_completion(system, user)

    if primary == "groq":
        out = groq_complete()
        if out is not None:
            return out
        out = or_complete()
        if out is not None:
            return out
    else:
        out = or_complete()
        if out is not None:
            return out
        out = groq_complete()
        if out is not None:
            return out

    if not OPENROUTER_API_KEY and not GROQ_API_KEY:
        raise ValueError(
            "No LLM API key configured. Set GROQ_API_KEY or OPENROUTER_API_KEY in .env "
            "(both have free tiers)."
        )
    return None


def _openrouter_chat_completion(system: str, user: str) -> str | None:
    payload: dict = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 2048,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": OPENROUTER_HTTP_REFERER,
        "X-Title": "Outreach Mastered org enrichment",
    }
    data: dict | None = None
    max_attempts = 12
    for attempt in range(max_attempts):
        body = {**payload}
        with httpx.Client(timeout=120.0) as client:
            r = client.post(_OPENROUTER_CHAT_URL, headers=headers, json=body)
            if r.is_error and "response_format" in body:
                body.pop("response_format", None)
                r = client.post(_OPENROUTER_CHAT_URL, headers=headers, json=body)
            if r.status_code == 429:
                if attempt + 1 < max_attempts:
                    ra = r.headers.get("retry-after") or r.headers.get("Retry-After")
                    try:
                        wait_s = float(ra) if ra else 0.0
                    except (TypeError, ValueError):
                        wait_s = 0.0
                    if wait_s <= 0:
                        wait_s = min(300.0, 25.0 * (2**attempt))
                    print(f"    (OpenRouter 429 — backing off {wait_s:.0f}s, attempt {attempt + 1}/{max_attempts})")
                    time.sleep(wait_s)
                    continue
                print("    (OpenRouter 429 — retries exhausted; skipping this phrase.)")
                return None
            r.raise_for_status()
            data = r.json()
            break
    if not data:
        return None
    choices = data.get("choices") or []
    if not choices:
        return None
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    reasoning = msg.get("reasoning")
    if isinstance(reasoning, str) and reasoning.strip():
        extracted = _json_from_reasoning_prose(reasoning)
        if extracted:
            return extracted
    return None


def resolve_website(org: str, hints: dict[str, str | None]) -> tuple[str | None, list[str]]:
    query = build_search_query(org, hints)
    results = web_search(query)
    url = pick_homepage_url(results)
    snippets = format_snippets_for_llm(results)
    return url, snippets


def enrich_org_metadata(
    items: list[ExtractedData],
    *,
    listing_url: str | None = None,
    limit: int | None = None,
    url_only: bool = False,
    preserve_source_url: bool = False,
    only_empty_wtd: bool = False,
    arpa_profiles_prefetched: dict[str, str] | None = None,
) -> tuple[list[ExtractedData], int]:
    """
    For each distinct organization, run web search to resolve homepage + LLM to generate
    a "What they do" phrase. Rows sharing the same org name reuse one lookup.
    """
    if not items:
        return items, 0

    if listing_url:
        print(f"  (scraped listing: {listing_url[:90]}...)")

    print("  Web search: You.com (YDC)")

    if not YDC_API_KEY:
        raise ValueError("Set YDC_API_KEY for --enrich-org (see README).")
    if not url_only and not OPENROUTER_API_KEY and not GROQ_API_KEY:
        raise ValueError(
            "Set GROQ_API_KEY or OPENROUTER_API_KEY for --enrich-org phrase generation (see README)."
        )

    if not url_only:
        _p = (ENRICH_LLM_PRIMARY or "groq").strip().lower()
        if _p not in ("groq", "openrouter"):
            _p = "groq"
        _groq_first = _p == "groq" and GROQ_API_KEY
        if _groq_first:
            _fb = (
                f" · fallback OpenRouter ({OPENROUTER_MODEL})"
                if OPENROUTER_API_KEY
                else ""
            )
            print(f"  LLM: Groq ({GROQ_MODEL}){_fb}")
        elif OPENROUTER_API_KEY:
            _fb = f" · fallback Groq ({GROQ_MODEL})" if GROQ_API_KEY else ""
            print(f"  LLM: OpenRouter ({OPENROUTER_MODEL}){_fb}")
        elif GROQ_API_KEY:
            print(f"  LLM: Groq ({GROQ_MODEL})")

    ordered_keys: list[str] = []
    seen_keys: set[str] = set()
    key_to_representative: dict[str, ExtractedData] = {}

    for item in items:
        key = _norm_org_key(item.organization)
        if key not in key_to_representative:
            key_to_representative[key] = item
        if key not in seen_keys:
            seen_keys.add(key)
            ordered_keys.append(key)

    if only_empty_wtd:

        def any_row_missing_wtd(k: str) -> bool:
            for it in items:
                if _norm_org_key(it.organization) != k:
                    continue
                if not (getattr(it, "what_they_do", None) or "").strip():
                    return True
            return False

        ordered_keys = [k for k in ordered_keys if any_row_missing_wtd(k)]
        if not ordered_keys:
            print(
                "  (--enrich-only-empty: no organizations with an empty What they do; "
                "nothing to enrich.)"
            )
            return items, 0

    if limit is not None:
        ordered_keys = ordered_keys[: max(0, limit)]

    arpa_profiles: dict[str, str] = {}
    if not url_only:
        if arpa_profiles_prefetched is not None:
            arpa_profiles = arpa_profiles_prefetched
            if arpa_profiles:
                print(
                    f"  ARPA-H ADVOCATE Teaming: using {len(arpa_profiles)} prefetched org profile(s)"
                )
            else:
                print(
                    "  ARPA-H ADVOCATE Teaming: prefetched profiles empty; "
                    "phrases will use web snippets only—review for accuracy."
                )
        else:
            arpa_profiles = fetch_advocate_teaming_research_by_org()
            if arpa_profiles:
                print(
                    f"  ARPA-H ADVOCATE Teaming: loaded {len(arpa_profiles)} org profile(s) from "
                    f"{ADVOCATE_TEAMING_URL}"
                )
            else:
                print(
                    "  ARPA-H ADVOCATE Teaming: could not load directory (network/HTML change); "
                    "phrases will use web snippets only—review for accuracy."
                )

    cache: dict[str, tuple[str | None, str | None]] = {}

    for i, key in enumerate(ordered_keys):
        rep = key_to_representative[key]
        org_name = rep.organization.strip() or "(unknown)"
        hints = _hints_from_item(rep)
        print(f"  Enrich org {i + 1}/{len(ordered_keys)}: {org_name[:60]}...")
        try:
            time.sleep(REQUEST_DELAY_SEC)
            website, snippets = resolve_website(org_name, hints)
            phrase: str | None = None
            llm_site = website
            if preserve_source_url:
                existing = (rep.source_url or "").strip()
                if existing.startswith("http://") or existing.startswith("https://"):
                    llm_site = existing
            if not url_only:
                time.sleep(REQUEST_DELAY_SEC)
                listing_research = arpa_profiles.get(key)
                if listing_research:
                    print(
                        f"    -> ARPA-H listing match: yes ({len(listing_research)} chars of research focus)"
                    )
                else:
                    print("    -> ARPA-H listing match: no (name must match teaming table exactly)")
                phrase = generate_approach_phrase(
                    org_name,
                    llm_site,
                    snippets,
                    arpa_h_listing_research=listing_research,
                )
            cache[key] = (website, phrase)
            if website:
                print(f"    -> site: {website[:80]}")
            if phrase:
                print(f"    -> phrase: {phrase[:100]}")
            elif not url_only:
                print(
                    "    -> phrase: (none — check GROQ_API_KEY / OPENROUTER_API_KEY, "
                    "model, rate limits, or logs)"
                )
        except Exception as e:
            print(f"    -> skip ({e})")
            cache[key] = (None, None)
        _gap_primary = (ENRICH_LLM_PRIMARY or "groq").strip().lower()
        if (
            OPENROUTER_API_KEY
            and _gap_primary == "openrouter"
            and not url_only
            and OPENROUTER_ENRICH_GAP_SEC > 0
            and i + 1 < len(ordered_keys)
        ):
            time.sleep(OPENROUTER_ENRICH_GAP_SEC)

    for item in items:
        key = _norm_org_key(item.organization)
        if key not in cache:
            continue
        website, phrase = cache[key]
        if website and not preserve_source_url:
            item.source_url = website
        if phrase:
            item.what_they_do = phrase

    n_phrases = sum(1 for _w, p in cache.values() if p)
    return items, n_phrases


def check_enrich_org_config(*, url_only: bool = False) -> str | None:
    """Return error message if config is incomplete, else None."""
    if not YDC_API_KEY:
        return "Missing YDC_API_KEY (get one free at https://documentation.you.com/)."
    if not url_only and not OPENROUTER_API_KEY and not GROQ_API_KEY:
        return "Missing GROQ_API_KEY and OPENROUTER_API_KEY (need at least one for phrase generation)."
    return None
