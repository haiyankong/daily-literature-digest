#!/usr/bin/env python3
"""Daily literature digest generator for GitHub Actions.

The script searches public metadata APIs, asks a configured model provider to
write an English-only email digest from the candidate records, sends it with
Gmail SMTP, and updates a small sent-item state file to reduce duplicate pushes.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import hashlib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import quote
from xml.etree import ElementTree

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "data" / "sent_items.json"
OUTPUT_DIR = ROOT / "outputs"
LOCAL_TZ = "America/New_York"

DEFAULT_MODEL_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"

@dataclass
class Candidate:
    section: str
    category: str
    title: str
    authors: str
    date: str
    venue: str
    doi: str
    url: str
    abstract: str
    why_candidate: str
    source: str

    @property
    def key(self) -> str:
        if self.doi:
            return "doi:" + self.doi.lower().strip()
        if self.url:
            return "url:" + self.url.lower().strip()
        return "title:" + normalize_space(self.title).lower()


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value if value else default


def required_env(name: str) -> str:
    value = env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def max_candidates_for_model() -> int:
    return int(env("MAX_CANDIDATES_FOR_MODEL", "80"))


def max_email_candidates() -> int:
    return int(env("MAX_EMAIL_CANDIDATES", "60"))


def max_output_tokens() -> int:
    return int(env("MAX_OUTPUT_TOKENS", "9000"))


def max_publication_age_days() -> int:
    return int(env("MAX_PUBLICATION_AGE_DAYS", "365"))


def model_api_timeout_seconds() -> int:
    return int(env("MODEL_API_TIMEOUT_SECONDS", "300"))


def model_api_retries() -> int:
    return max(int(env("MODEL_API_RETRIES", "3")), 1)


def model_api_retry_delay_seconds(attempt: int) -> int:
    return int(env("MODEL_API_RETRY_DELAY_SECONDS", "10")) * attempt


def model_provider(config: dict[str, Any]) -> str:
    provider = env("MODEL_PROVIDER", str(config.get("model_provider") or DEFAULT_MODEL_PROVIDER))
    provider = normalize_space(provider).lower()
    return "anthropic" if provider == "claude" else provider


def section_caps() -> dict[str, int]:
    return {
        "Section A": int(env("SECTION_A_CANDIDATE_CAP", "12")),
        "Section B": int(env("SECTION_B_CANDIDATE_CAP", "8")),
        "Section C": int(env("SECTION_C_CANDIDATE_CAP", "60")),
    }


def section_row_limits_enabled() -> bool:
    return env("RESPECT_SECTION_ROW_LIMITS", "").lower() in {"1", "true", "yes"}


def rows_for_search(section_config: dict[str, Any], key: str) -> int:
    if section_row_limits_enabled() and section_config.get(key):
        return int(section_config[key])
    return max_candidates_for_model()


def load_digest_config() -> dict[str, Any]:
    raw = env("DIGEST_CONFIG_JSON")
    raw_b64 = env("DIGEST_CONFIG_JSON_B64")
    config_path = env("DIGEST_CONFIG_PATH")

    if raw_b64:
        raw = base64.b64decode(raw_b64).decode("utf-8")
    elif config_path:
        raw = Path(config_path).read_text(encoding="utf-8")

    if not raw:
        raise RuntimeError(
            "Missing private digest configuration. Add DIGEST_CONFIG_JSON as a GitHub Secret, "
            "or set DIGEST_CONFIG_PATH for local testing."
        )

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"DIGEST_CONFIG_JSON is not valid JSON: {exc}") from exc

    sections = config.get("sections")
    if not isinstance(sections, list) or not sections:
        raise RuntimeError("Digest config must contain a non-empty 'sections' list.")

    return config


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_markup(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_space(text)


def compact(text: str, limit: int = 900) -> str:
    text = normalize_space(text)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def date_parts(value: Any) -> str:
    if not value:
        return ""
    parts = value.get("date-parts", [[]])[0] if isinstance(value, dict) else []
    if not parts:
        return ""
    if len(parts) == 1:
        return f"{parts[0]:04d}"
    if len(parts) == 2:
        return f"{parts[0]:04d}-{parts[1]:02d}"
    return f"{parts[0]:04d}-{parts[1]:02d}-{parts[2]:02d}"


def request_json(url: str, params: dict[str, Any], email: str, timeout: int = 30) -> dict[str, Any]:
    headers = {"User-Agent": f"DailyLiteratureDigest/1.0 (mailto:{email})"}
    response = requests.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def private_error_summary(exc: Exception) -> str:
    """Return an error summary that does not include private query parameters."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    reason = normalize_space(getattr(response, "reason", "") or "")
    if status_code:
        suffix = f"status={status_code}"
        if reason:
            suffix += f" reason={reason}"
        return f"{type(exc).__name__} ({suffix})"
    return type(exc).__name__


def private_response_error(provider: str, response: requests.Response) -> RuntimeError:
    reason = normalize_space(response.reason or "")
    message = f"{provider} API error status={response.status_code}"
    if reason:
        message += f" reason={reason}"
    return RuntimeError(message)


def transient_model_status(status_code: int) -> bool:
    return status_code in {408, 409, 429, 500, 502, 503, 504}


def post_model_json(provider: str, url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    attempts = model_api_retries()
    timeout = model_api_timeout_seconds()

    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            if attempt >= attempts:
                raise RuntimeError(
                    f"{provider} request failed after {attempts} attempts: {private_error_summary(exc)}"
                ) from None
            print(
                f"{provider} request attempt {attempt}/{attempts} failed: {private_error_summary(exc)}; retrying.",
                file=sys.stderr,
            )
            time.sleep(model_api_retry_delay_seconds(attempt))
            continue
        except requests.exceptions.RequestException as exc:
            raise RuntimeError(f"{provider} request failed: {private_error_summary(exc)}") from None

        if response.status_code >= 400:
            if transient_model_status(response.status_code) and attempt < attempts:
                print(
                    f"{provider} API transient error attempt {attempt}/{attempts}: "
                    f"status={response.status_code}; retrying.",
                    file=sys.stderr,
                )
                time.sleep(model_api_retry_delay_seconds(attempt))
                continue
            raise private_response_error(provider, response)

        return response.json()

    raise RuntimeError(f"{provider} request failed after {attempts} attempts.")


def crossref_to_candidate(item: dict[str, Any], section: str, category: str, why: str) -> Candidate:
    title = normalize_space(" ".join(item.get("title") or []))
    authors = ", ".join(
        normalize_space(f"{a.get('given', '')} {a.get('family', '')}")
        for a in item.get("author", [])[:8]
        if a.get("given") or a.get("family")
    )
    if len(item.get("author", [])) > 8:
        authors += ", et al."
    venue = normalize_space("; ".join(item.get("container-title") or []))
    date = (
        date_parts(item.get("published-online"))
        or date_parts(item.get("published-print"))
        or date_parts(item.get("published"))
        or date_parts(item.get("created"))
        or date_parts(item.get("indexed"))
    )
    doi = normalize_space(item.get("DOI", ""))
    url = item.get("URL", "")
    if doi and not url:
        url = f"https://doi.org/{doi}"
    return Candidate(
        section=section,
        category=category,
        title=title,
        authors=authors or "Not listed in metadata",
        date=date,
        venue=venue or "Venue not listed",
        doi=doi,
        url=url,
        abstract=compact(strip_markup(item.get("abstract", ""))),
        why_candidate=why,
        source="Crossref",
    )


def search_crossref(
    query: str,
    section: str,
    category: str,
    why: str,
    start_date: str,
    end_date: str,
    email: str,
    rows: int,
) -> list[Candidate]:
    params = {
        "query.bibliographic": query,
        "filter": f"from-index-date:{start_date},until-index-date:{end_date},type:journal-article",
        "rows": rows,
        "select": "DOI,title,author,published,published-online,published-print,container-title,URL,abstract,created,indexed,type",
        "mailto": email,
        "sort": "indexed",
        "order": "desc",
    }
    try:
        data = request_json("https://api.crossref.org/works", params, email)
        return [
            crossref_to_candidate(item, section, category, why)
            for item in data.get("message", {}).get("items", [])
            if item.get("title")
        ]
    except Exception as exc:
        print(
            f"Crossref query failed for private query redacted: {private_error_summary(exc)}",
            file=sys.stderr,
        )
        return []


def search_crossref_journal(
    journal: str,
    section: str,
    category: str,
    start_date: str,
    end_date: str,
    email: str,
    rows: int,
) -> list[Candidate]:
    params = {
        "query.container-title": journal,
        "filter": f"from-index-date:{start_date},until-index-date:{end_date},type:journal-article",
        "rows": rows,
        "select": "DOI,title,author,published,published-online,published-print,container-title,URL,abstract,created,indexed,type",
        "mailto": email,
        "sort": "indexed",
        "order": "desc",
    }
    try:
        data = request_json("https://api.crossref.org/works", params, email)
        out = []
        for item in data.get("message", {}).get("items", []):
            candidate = crossref_to_candidate(
                item,
                section,
                category,
                f"Newly indexed item from tracked journal: {journal}",
            )
            # Crossref's query.container-title is a fuzzy text search and can
            # return papers from similarly-named journals (e.g. searching "Brain"
            # also returns "Brain Research", "Brain and Cognition", etc.).
            # Only keep candidates whose recorded venue actually contains the
            # requested journal name, or whose recorded venue is empty (rare cases
            # where Crossref has no container-title metadata).
            if journal.lower() in candidate.venue.lower() or not candidate.venue:
                out.append(candidate)
        return out
    except Exception as exc:
        print(
            f"Crossref journal query failed for private journal redacted: {private_error_summary(exc)}",
            file=sys.stderr,
        )
        return []


def openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not inverted:
        return ""
    words: list[tuple[int, str]] = []
    for word, positions in inverted.items():
        for pos in positions:
            words.append((pos, word))
    return compact(" ".join(word for _, word in sorted(words)), 900)


def search_openalex(
    query: str,
    section: str,
    category: str,
    why: str,
    start_date: str,
    end_date: str,
    email: str,
    rows: int,
) -> list[Candidate]:
    params = {
        "search": query,
        "filter": f"from_publication_date:{start_date},to_publication_date:{end_date}",
        "per-page": min(rows, 200),
        "mailto": email,
    }
    try:
        data = request_json("https://api.openalex.org/works", params, email)
    except Exception as exc:
        print(
            f"OpenAlex query failed for private query redacted: {private_error_summary(exc)}",
            file=sys.stderr,
        )
        return []

    out = []
    for item in data.get("results", []):
        source = (item.get("primary_location") or {}).get("source") or {}
        doi = (item.get("doi") or "").replace("https://doi.org/", "")
        authors = ", ".join(
            a.get("author", {}).get("display_name", "")
            for a in item.get("authorships", [])[:8]
            if a.get("author", {}).get("display_name")
        )
        if len(item.get("authorships", [])) > 8:
            authors += ", et al."
        out.append(
            Candidate(
                section=section,
                category=category,
                title=normalize_space(item.get("title", "")),
                authors=authors or "Not listed in metadata",
                date=item.get("publication_date", "") or str(item.get("publication_year", "")),
                venue=source.get("display_name", "") or "Venue not listed",
                doi=doi,
                url=item.get("doi") or item.get("id", ""),
                abstract=openalex_abstract(item.get("abstract_inverted_index")),
                why_candidate=why,
                source="OpenAlex",
            )
        )
    return out


def pubmed_search(
    term: str,
    section: str,
    category: str,
    why: str,
    start_date: str,
    end_date: str,
    email: str,
    rows: int,
) -> list[Candidate]:
    date_range = f'("{start_date.replace("-", "/")}"[EDAT] : "{end_date.replace("-", "/")}"[EDAT])'
    query = f"({term}) AND {date_range}"
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    try:
        search = request_json(
            f"{base}/esearch.fcgi",
            {
                "db": "pubmed",
                "term": query,
                "retmode": "json",
                "retmax": rows,
                "sort": "pub date",
                "email": email,
            },
            email,
        )
        ids = search.get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []
        xml = requests.get(
            f"{base}/efetch.fcgi",
            params={"db": "pubmed", "id": ",".join(ids), "retmode": "xml", "email": email},
            headers={"User-Agent": f"DailyLiteratureDigest/1.0 (mailto:{email})"},
            timeout=30,
        )
        xml.raise_for_status()
    except Exception as exc:
        print(
            f"PubMed query failed for private query redacted: {private_error_summary(exc)}",
            file=sys.stderr,
        )
        return []

    out = []
    root = ElementTree.fromstring(xml.text)
    for article in root.findall(".//PubmedArticle"):
        pmid = normalize_space("".join(article.findtext(".//PMID") or ""))
        title = normalize_space("".join(article.findtext(".//ArticleTitle") or ""))
        journal = normalize_space(article.findtext(".//Journal/Title") or "")
        year = article.findtext(".//PubDate/Year") or ""
        month = article.findtext(".//PubDate/Month") or ""
        day = article.findtext(".//PubDate/Day") or ""
        date = "-".join([p for p in [year, month, day] if p])
        abstract = normalize_space(" ".join(t.text or "" for t in article.findall(".//AbstractText")))
        author_names = []
        for author in article.findall(".//Author")[:8]:
            last = author.findtext("LastName") or ""
            fore = author.findtext("ForeName") or ""
            coll = author.findtext("CollectiveName") or ""
            name = normalize_space(f"{fore} {last}") or normalize_space(coll)
            if name:
                author_names.append(name)
        if len(article.findall(".//Author")) > 8:
            author_names.append("et al.")
        doi = ""
        for aid in article.findall(".//ArticleId"):
            if aid.attrib.get("IdType") == "doi" and aid.text:
                doi = normalize_space(aid.text)
                break
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else (f"https://doi.org/{doi}" if doi else "")
        if title:
            out.append(
                Candidate(
                    section=section,
                    category=category,
                    title=title,
                    authors=", ".join(author_names) or "Not listed in metadata",
                    date=date,
                    venue=journal or "PubMed-indexed venue",
                    doi=doi,
                    url=url,
                    abstract=compact(abstract),
                    why_candidate=why,
                    source="PubMed",
                )
            )
    return out


def load_state() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        out = set()
        for item in data.get("items", []):
            if item.get("key_hash"):
                out.add(str(item["key_hash"]))
            elif item.get("key"):
                out.add(candidate_key_hash(str(item["key"])))
        return out
    except Exception:
        return set()


def load_sent_dates() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {str(value) for value in data.get("sent_dates", []) if value}
    except Exception:
        return set()


def candidate_key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def save_state(sent_key_hashes: set[str], new_items: list[Candidate], now_iso: str, sent_date: str = "") -> None:
    existing: dict[str, dict[str, Any]] = {}
    sent_dates = set()
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            sent_dates = {str(value) for value in data.get("sent_dates", []) if value}
            for item in data.get("items", []):
                if item.get("key_hash"):
                    existing[str(item["key_hash"])] = {
                        "key_hash": str(item["key_hash"]),
                        "sent_at": item.get("sent_at", ""),
                    }
                elif item.get("key"):
                    key_hash = candidate_key_hash(str(item["key"]))
                    existing[key_hash] = {
                        "key_hash": key_hash,
                        "sent_at": item.get("sent_at", ""),
                    }
        except Exception:
            existing = {}

    for key_hash in sent_key_hashes:
        existing.setdefault(key_hash, {"key_hash": key_hash, "sent_at": ""})

    for candidate in new_items:
        key_hash = candidate_key_hash(candidate.key)
        existing[key_hash] = {
            "key_hash": key_hash,
            "sent_at": now_iso,
        }

    trimmed = sorted(existing.values(), key=lambda x: x.get("sent_at", ""), reverse=True)[:1500]
    if sent_date:
        sent_dates.add(sent_date)
    trimmed_sent_dates = sorted(sent_dates)[-120:]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps({"items": trimmed, "sent_dates": trimmed_sent_dates}, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def dedupe_candidates(candidates: list[Candidate], sent: set[str]) -> list[Candidate]:
    seen = set()
    out = []
    for candidate in candidates:
        if not candidate.title:
            continue
        key = candidate_key_hash(candidate.key)
        if key in seen or key in sent:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def parse_publication_date(value: str) -> datetime.date | None:
    text = normalize_space(value)
    match = re.match(r"^(\d{4})(?:[-/ ]([A-Za-z]{3,9}|\d{1,2})(?:[-/ ](\d{1,2}))?)?", text)
    if not match:
        return None

    year = int(match.group(1))
    month_text = match.group(2)
    day_text = match.group(3)

    if not month_text:
        return datetime(year, 12, 31).date()

    if month_text.isdigit():
        month = int(month_text)
    else:
        month_map = {
            name.lower(): index
            for index, name in enumerate(calendar.month_name)
            if name
        }
        month_map.update(
            {
                name.lower(): index
                for index, name in enumerate(calendar.month_abbr)
                if name
            }
        )
        month = month_map.get(month_text.lower())
        if not month:
            return datetime(year, 12, 31).date()

    if not 1 <= month <= 12:
        return datetime(year, 12, 31).date()

    if day_text:
        day = int(day_text)
    else:
        day = calendar.monthrange(year, month)[1]

    try:
        return datetime(year, month, day).date()
    except ValueError:
        return datetime(year, month, calendar.monthrange(year, month)[1]).date()


def filter_recent_publications(candidates: list[Candidate], end_date: str) -> list[Candidate]:
    max_age = max_publication_age_days()
    if max_age <= 0:
        return candidates

    try:
        end = datetime.fromisoformat(end_date).date()
    except ValueError:
        return candidates

    cutoff = end - timedelta(days=max_age)
    kept: list[Candidate] = []
    dropped = 0
    for candidate in candidates:
        publication_date = parse_publication_date(candidate.date)
        if publication_date is None or publication_date >= cutoff:
            kept.append(candidate)
        else:
            dropped += 1

    if dropped:
        print(f"Publication-age filter dropped {dropped} older candidate record(s).")
    return kept


def candidate_search_text(candidate: Candidate) -> str:
    return " ".join(
        [
            candidate.title,
            candidate.authors,
            candidate.venue,
            candidate.abstract,
            candidate.why_candidate,
        ]
    ).lower()


def candidate_paper_text(candidate: Candidate) -> str:
    """Only text from the paper itself (title + abstract), not pipeline metadata.

    Used by require_terms filtering so that query strings embedded in
    why_candidate cannot cause an off-topic paper to pass a require_terms gate.
    """
    return " ".join([candidate.title, candidate.abstract]).lower()


def candidate_matches_term(candidate: Candidate, term: str) -> bool:
    term = normalize_space(term).lower()
    if not term:
        return False
    text = candidate_search_text(candidate)
    if re.search(r"\w", term):
        return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text))
    return term in text


def candidate_matches_any_term(candidate: Candidate, terms: list[str]) -> bool:
    return any(candidate_matches_term(candidate, str(term)) for term in terms)


def candidate_matches_require_term(candidate: Candidate, term: str) -> bool:
    """Check whether a required term appears in the paper's own text (title + abstract)."""
    term = normalize_space(term).lower()
    if not term:
        return False
    text = candidate_paper_text(candidate)
    if re.search(r"\w", term):
        return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text))
    return term in text


def require_filter_candidates(candidates: list[Candidate], *configs: dict[str, Any]) -> list[Candidate]:
    """Keep only candidates that contain at least one require_term in their title or abstract.

    This is intentionally strict: it checks candidate_paper_text() (title +
    abstract only) so that query phrases embedded in the why_candidate field
    cannot cause an off-topic paper to slip through.

    If no section config supplies any require_terms, all candidates are kept.
    """
    require_terms: list[str] = []
    for config in configs:
        terms = config.get("require_terms") if isinstance(config, dict) else []
        if isinstance(terms, list):
            require_terms.extend(str(term) for term in terms)

    if not require_terms:
        return candidates

    kept = []
    dropped = 0
    for candidate in candidates:
        if any(candidate_matches_require_term(candidate, t) for t in require_terms):
            kept.append(candidate)
        else:
            dropped += 1
    if dropped:
        print(f"require_terms filter dropped {dropped} off-topic candidate(s).")
    return kept


def nested_filter_config(section_config: dict[str, Any], key: str, name: str) -> dict[str, Any]:
    filters = section_config.get(key) or {}
    if not isinstance(filters, dict):
        return {}
    config = filters.get(name) or {}
    return config if isinstance(config, dict) else {}


def filter_candidates(candidates: list[Candidate], *configs: dict[str, Any]) -> list[Candidate]:
    exclude_terms: list[str] = []
    for config in configs:
        terms = config.get("exclude_terms") if isinstance(config, dict) else []
        if isinstance(terms, list):
            exclude_terms.extend(str(term) for term in terms)

    if not exclude_terms:
        return candidates

    return [
        candidate
        for candidate in candidates
        if not candidate_matches_any_term(candidate, exclude_terms)
    ]


def collect_candidates(start_date: str, end_date: str, email: str, config: dict[str, Any]) -> list[Candidate]:
    candidates: list[Candidate] = []

    for index, section_config in enumerate(config.get("sections", []), start=1):
        section_title = section_config.get("title") or f"Section {index}"
        section_type = section_config.get("type")
        default_category = section_config.get("category") or section_title

        if section_type == "topic_search":
            rows = rows_for_search(section_config, "rows_per_query")
            include_pubmed = bool(section_config.get("include_pubmed", True))
            for item in section_config.get("queries", []):
                query_config = item if isinstance(item, dict) else {}
                if isinstance(item, str):
                    query = item
                    why = section_config.get("instructions", "Private topic-search query.")
                else:
                    query = item.get("query", "")
                    why = item.get("why") or section_config.get("instructions", "Private topic-search query.")
                query = normalize_space(query)
                if not query:
                    continue
                found = []
                found.extend(search_crossref(query, section_title, default_category, why, start_date, end_date, email, rows))
                found.extend(search_openalex(query, section_title, default_category, why, start_date, end_date, email, rows))
                if include_pubmed:
                    found.extend(pubmed_search(query, section_title, default_category, why, start_date, end_date, email, rows))
                # Apply require_terms first: drop papers that do not contain
                # a required keyword in their own title or abstract.  This is
                # the primary guard against Crossref/OpenAlex returning papers
                # that matched a broad OR-clause rather than the core topic keyword.
                found = require_filter_candidates(found, section_config)
                candidates.extend(filter_candidates(found, section_config, query_config))
                time.sleep(0.05)
            continue

        if section_type in {"journal_watchlist", "full_journal_push"}:
            rows = rows_for_search(section_config, "rows_per_journal")
            journal_categories = section_config.get("journal_categories")
            if isinstance(journal_categories, dict):
                iterable = [
                    (category, journal)
                    for category, journals in journal_categories.items()
                    for journal in journals
                ]
            else:
                iterable = [(default_category, journal) for journal in section_config.get("journals", [])]
            for category, journal in iterable:
                journal = normalize_space(journal)
                if not journal:
                    continue
                found = search_crossref_journal(journal, section_title, category, start_date, end_date, email, rows)
                candidates.extend(
                    filter_candidates(
                        found,
                        section_config,
                        nested_filter_config(section_config, "category_filters", category),
                        nested_filter_config(section_config, "journal_filters", journal),
                    )
                )
                time.sleep(0.05)
            continue

        print(f"Skipping unknown section type for {section_title}: {section_type}", file=sys.stderr)

    return candidates


def limit_candidates_for_model(candidates: list[Candidate]) -> list[Candidate]:
    caps = section_caps()
    total_cap = min(max_candidates_for_model(), max_email_candidates())
    kept: list[Candidate] = []
    omitted_by_section: dict[str, int] = {}

    for section_config in caps:
        section_items = [candidate for candidate in candidates if candidate.section.startswith(section_config)]
        cap = caps[section_config]
        kept.extend(section_items[:cap])
        if len(section_items) > cap:
            omitted_by_section[section_config] = len(section_items) - cap

    uncategorized = [
        candidate
        for candidate in candidates
        if not any(candidate.section.startswith(section) for section in caps)
    ]
    remaining = max(total_cap - len(kept), 0)
    kept.extend(uncategorized[:remaining])
    if len(uncategorized) > remaining:
        omitted_by_section["Other"] = len(uncategorized) - remaining

    if len(kept) > total_cap:
        overflow = len(kept) - total_cap
        kept = kept[:total_cap]
        omitted_by_section["Global cap"] = omitted_by_section.get("Global cap", 0) + overflow

    omitted_total = sum(omitted_by_section.values())
    if omitted_total:
        details = ", ".join(f"{section}: {count}" for section, count in omitted_by_section.items())
        print(f"Omitted {omitted_total} candidate records due to digest length caps ({details}).")

    return kept


def candidate_payload(candidates: list[Candidate]) -> str:
    payload = []
    for candidate in candidates:
        item = asdict(candidate)
        item["key"] = candidate.key
        item["abstract"] = compact(item.get("abstract", ""), 1100)
        payload.append(item)
    return json.dumps(payload, ensure_ascii=True, indent=2)


def build_digest_prompt(
    candidates: list[Candidate],
    start_date: str,
    end_date: str,
    sender: str,
    recipient: str,
    config: dict[str, Any],
) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return f"""
You are preparing an email-ready daily literature digest for a psychology and cognitive-neuroscience researcher.

Hard requirements:
- Output English only.
- Use only the candidate records supplied below. Do not invent papers, DOIs, authors, dates, or summaries.
- Include every supplied candidate record in the digest, except exact duplicates or records excluded by the section relevance rules. Do not select only the most important papers and do not omit lower-priority supplied records merely to shorten the digest.
- Include a concise Subject line, From line, and To line.
- Keep the digest readable and avoid flooding.
- Mention the coverage window: {start_date} to {end_date}, generated on {today}.
- Avoid duplicates across sections: if a paper fits multiple sections, highlight it once and briefly cross-reference it if needed.
- Follow the digest configuration exactly for section titles, inclusion/exclusion preferences, and final highlight instructions.
- The candidate list has already been capped before reaching you. If the list is long, keep Section A and Section B readable but complete, and make Section C compact.
- Do not print scope descriptions, parenthetical scope notes, excluded-item examples, discarded-item examples, implementation notes, or suggestions for follow-up services.
- Do not create "Highlighted", "Compact", "Grouped by journal", "Related", or other extra subsections inside Section A, Section B, or Section C.
- Do not write placeholder lines such as "additional papers included" or "see candidate set"; every included candidate must have its own visible entry.
- Do not end with "End of digest", "If you want", or a menu of possible next actions.

Required output structure:

Subject: Daily Literature Digest - {start_date} to {end_date}
From: {sender}
To: {recipient}

Coverage window: {start_date} to {end_date}. Generated on {today}.

Section A: fNIRS Topic Tracker
If there are no Section A records, write exactly: No candidate records today.
Otherwise list each Section A record as a numbered item using exactly this fields-only format:
1. Title
   Authors: ...
   Date / Venue: ...
   DOI / URL: ...
   Why matched: ...
   Summary: one to two concise sentences.
   Priority: 1-5

Section B: CNS Psychology / Cognitive Neuroscience / BCI Watchlist
If there are no Section B records, write exactly: No high-fit candidate records today.
Do not mention excluded or discarded records.
Otherwise use the same numbered fields-only format as Section A.

Section C: Full Journal Push
If there are no Section C records, write exactly: No candidate records today.
Otherwise list each Section C record as one compact numbered line:
1. Title - Authors. Venue, date. DOI / URL. Note: one concise sentence.
Do not group Section C by journal, topic, age, category, or importance.

{config.get("final_section_title", "Today Highlight")}
Use the final-section instruction from the digest configuration for how many papers to name. Use bullet points only. Choose only from records already listed above. Keep each bullet to one sentence.

From: {sender}
To: {recipient}

Digest configuration, for rules only; do not quote it directly:
{json.dumps(config, ensure_ascii=True, indent=2)}

Candidate records:
{candidate_payload(candidates)}
""".strip()


def compose_with_openai(prompt: str) -> str:
    api_key = env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is missing. Add it to GitHub repository secrets.")

    model = env("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    output_tokens = int(env("OPENAI_MAX_OUTPUT_TOKENS", str(max_output_tokens())))
    data = post_model_json(
        "OpenAI",
        "https://api.openai.com/v1/responses",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        {
            "model": model,
            "input": prompt,
            "max_output_tokens": output_tokens,
        },
    )
    if data.get("output_text"):
        return data["output_text"].strip()

    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    text = "\n".join(chunks).strip()
    if not text:
        raise RuntimeError("OpenAI response did not contain output text.")
    return text


def compose_with_anthropic(prompt: str) -> str:
    api_key = env("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is missing. Add it to GitHub repository secrets.")

    model = env("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL)
    output_tokens = int(env("ANTHROPIC_MAX_OUTPUT_TOKENS", str(max_output_tokens())))
    data = post_model_json(
        "Anthropic",
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": api_key,
            "anthropic-version": env("ANTHROPIC_VERSION", DEFAULT_ANTHROPIC_VERSION),
            "Content-Type": "application/json",
        },
        {
            "model": model,
            "max_tokens": output_tokens,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    chunks = []
    for content in data.get("content", []):
        if content.get("type") == "text" and content.get("text"):
            chunks.append(content["text"])
    text = "\n".join(chunks).strip()
    if not text:
        raise RuntimeError("Anthropic response did not contain output text.")
    return text


def compose_with_model(
    candidates: list[Candidate],
    start_date: str,
    end_date: str,
    sender: str,
    recipient: str,
    config: dict[str, Any],
) -> str:
    prompt = build_digest_prompt(candidates, start_date, end_date, sender, recipient, config)
    provider = model_provider(config)
    if provider == "openai":
        return compose_with_openai(prompt)
    if provider == "anthropic":
        return compose_with_anthropic(prompt)
    raise RuntimeError("Unsupported MODEL_PROVIDER. Use 'openai' or 'anthropic'.")


def fallback_digest(
    candidates: list[Candidate],
    start_date: str,
    end_date: str,
    sender: str,
    recipient: str,
    config: dict[str, Any],
) -> str:
    lines = [
        f"Subject: Daily Literature Digest - {start_date} to {end_date}",
        f"From: {sender}",
        f"To: {recipient}",
        "",
        f"Coverage window: {start_date} to {end_date}.",
    ]
    for section_config in config.get("sections", []):
        section = section_config.get("title", "Section")
        lines.extend(["", section, ""])
        items = [c for c in candidates if c.section == section]
        if not items:
            lines.append("No newly discovered candidate records.")
            continue
        for c in items[:60]:
            link = c.url or (f"https://doi.org/{c.doi}" if c.doi else "")
            note = compact(c.abstract or c.why_candidate, 220)
            lines.append(f"- {c.title}. {c.venue}, {c.date}. {link}. Note: {note}")
    final_title = config.get("final_section_title", "Today Highlight")
    lines.extend(["", final_title, "", "AI summarization was unavailable; review the highest-fit candidate records above."])
    return "\n".join(lines)


def extract_subject(body: str, start_date: str, end_date: str) -> str:
    for line in body.splitlines()[:8]:
        if line.lower().startswith("subject:"):
            subject = normalize_space(line.split(":", 1)[1])
            if subject:
                return subject
    return f"Daily Literature Digest - {start_date} to {end_date}"


def send_email(subject: str, body: str, sender: str, recipient: str, app_password: str) -> None:
    password = app_password.replace(" ", "").strip()
    if not password:
        raise RuntimeError("GMAIL_APP_PASSWORD is missing. Add it to GitHub repository secrets.")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    context = ssl.create_default_context()
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(sender, password)
        smtp.sendmail(sender, [recipient], msg.as_string())


def should_run_time_gate(now_local: datetime | None = None) -> bool:
    if ZoneInfo is None:
        return True
    now = now_local or datetime.now(ZoneInfo(LOCAL_TZ))
    target_hour = int(env("TARGET_LOCAL_HOUR", "5"))
    window_hours = max(int(env("SCHEDULE_WINDOW_HOURS", "6")), 1)
    window_start = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
    window_end = window_start + timedelta(hours=window_hours)
    return window_start <= now < window_end


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="Send the digest by Gmail SMTP.")
    parser.add_argument("--time-gate", action="store_true", help="Only run during the target local hour.")
    parser.add_argument("--lookback-days", type=int, default=int(env("LOOKBACK_DAYS", "2")))
    parser.add_argument("--allow-fallback", action="store_true", help="Send a metadata-only digest if OpenAI is unavailable.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now_local = datetime.now(ZoneInfo(LOCAL_TZ)) if ZoneInfo else datetime.now()
    scheduled_sent_date = now_local.date().isoformat() if args.time_gate else ""
    if args.time_gate:
        if not should_run_time_gate(now_local):
            print(f"Skipping: current {LOCAL_TZ} time is outside the scheduled delivery window.")
            return 0
        if scheduled_sent_date in load_sent_dates():
            print(f"Skipping: scheduled digest already sent for {scheduled_sent_date}.")
            return 0

    sender = required_env("GMAIL_ADDRESS")
    recipient = required_env("DIGEST_RECIPIENT")
    config = load_digest_config()
    end = now_local.date()
    start = end - timedelta(days=max(args.lookback_days, 1))
    start_date = start.isoformat()
    end_date = end.isoformat()

    sent_state = load_state()
    candidates = filter_recent_publications(collect_candidates(start_date, end_date, sender, config), end_date)
    candidates = dedupe_candidates(candidates, sent_state)
    candidates = limit_candidates_for_model(candidates)
    print(f"Collected {len(candidates)} unsent candidate records for {start_date} to {end_date}.")

    try:
        body = compose_with_model(candidates, start_date, end_date, sender, recipient, config)
    except Exception as exc:
        if not args.allow_fallback:
            raise
        print(
            f"AI summarization unavailable, using fallback digest: {private_error_summary(exc)}",
            file=sys.stderr,
        )
        body = fallback_digest(candidates, start_date, end_date, sender, recipient, config)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / f"daily_literature_digest_{end_date}.txt"
    output_path.write_text(body + "\n", encoding="utf-8")
    print(f"Wrote digest to {output_path}")

    if args.send:
        subject = extract_subject(body, start_date, end_date)
        send_email(subject, body, sender, recipient, env("GMAIL_APP_PASSWORD"))
        print(f"Sent digest to {recipient} from {sender}.")
        save_state(sent_state, candidates, datetime.now(timezone.utc).isoformat(), sent_date=scheduled_sent_date)
    else:
        print("Dry run only; email not sent.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
