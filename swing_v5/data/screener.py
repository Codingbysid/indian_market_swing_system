"""Public Screener pages. An unparsed page is never a complete screen."""

from __future__ import annotations

import hashlib
import re
import time
import urllib.request
from pathlib import Path

SCREENS = {
    "piotroski": "https://www.screener.in/screens/2/piotroski-scan/",
    "magic": "https://www.screener.in/screens/59/magic-formula/",
    "coffee": "https://www.screener.in/screens/57601/coffee-can-portfolio/",
    "golden": "https://www.screener.in/screens/336509/golden-crossover/",
}

_COMPANY = re.compile(r'href="/company/([^"/]+)/(?:consolidated/)?"')


def html_unescape(text: str) -> str:
    return text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")


def parse_page(html: str, url: str) -> dict:
    match = re.search(r"(\d+) results found: Showing page (\d+) of (\d+)", html)
    query = re.search(r"<textarea[^>]*>(.*?)</textarea>", html, re.S)
    company_ids = list(dict.fromkeys(_COMPANY.findall(html)))
    if match is None:
        return {
            "url": url,
            "status": "unparsed",
            "parsed": False,
            "complete": False,
            "empty": False,
            "company_ids": company_ids,
            "nse_symbols": [],
            "content_hash": hashlib.sha256(html.encode()).hexdigest(),
        }
    found, page, pages = (int(match.group(i)) for i in (1, 2, 3))
    return {
        "url": url,
        "status": "parsed",
        "parsed": True,
        "results_found": found,
        "page": page,
        "pages": pages,
        "company_ids": company_ids,
        "nse_symbols": [],
        "identity": "company_url_slug_not_a_verified_nse_ticker",
        "query": html_unescape(query.group(1)).strip() if query else "",
        "complete": False,
        "empty": found == 0,
        "content_hash": hashlib.sha256(html.encode()).hexdigest(),
    }


def fetch_page(url: str, timeout: int = 25, retries: int = 2) -> tuple[int, str]:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "indian-swing-v5-research"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last = exc
            time.sleep(0.3 * (attempt + 1))
    raise last or RuntimeError("fetch failed")


def _archive(archive_dir: Path | None, name: str, html: str) -> str:
    digest = hashlib.sha256(html.encode()).hexdigest()
    if archive_dir is not None:
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / f"{name}-{digest[:12]}.html").write_text(html, encoding="utf-8")
    return digest


def collect_screen(url: str, max_pages: int = 25, archive_dir: Path | None = None, symbol_map: dict | None = None) -> dict:
    try:
        status, html = fetch_page(url)
    except Exception as exc:
        return {"url": url, "status": "fetch_failed", "parsed": False, "complete": False, "empty": False, "error": type(exc).__name__}
    if status != 200:
        return {"url": url, "status": f"http_{status}", "parsed": False, "complete": False, "empty": False}
    first = parse_page(html, url)
    first["page_hashes"] = [_archive(archive_dir, "page-1", html)]
    first["http_status"] = status
    if not first.get("parsed"):
        first["status"] = "unparsed"
        first["complete"] = False
        first["empty"] = False
        return first
    pages = int(first["pages"])
    if pages < 1 or pages > max_pages:
        first["status"] = "incomplete_not_all_pages"
        first["complete"] = False
        return first
    pages_html = [first]
    for page in range(2, pages + 1):
        joiner = "&" if "?" in url else "?"
        try:
            status, html = fetch_page(f"{url}{joiner}page={page}")
        except Exception as exc:
            first["status"] = "pagination_failed"
            first["complete"] = False
            first["empty"] = False
            first["error"] = type(exc).__name__
            return first
        parsed = parse_page(html, url)
        first["page_hashes"].append(_archive(archive_dir, f"page-{page}", html))
        if status != 200 or not parsed.get("parsed") or parsed.get("page") != page:
            first["status"] = "pagination_failed"
            first["complete"] = False
            first["empty"] = False
            return first
        if parsed.get("query") != first.get("query"):
            first["status"] = "query_changed_during_collection"
            first["complete"] = False
            return first
        pages_html.append(parsed)
    ids: list[str] = []
    for parsed in pages_html:
        ids.extend(parsed.get("company_ids") or [])
    duplicates = len(ids) - len(set(ids))
    unique_ids = list(dict.fromkeys(ids))
    resolved = []
    unresolved = []
    for company_id in unique_ids:
        ticker = (symbol_map or {}).get(company_id)
        if ticker:
            resolved.append(ticker)
        else:
            unresolved.append(company_id)
    first["company_ids"] = unique_ids
    first["duplicate_ids"] = duplicates
    first["nse_symbols"] = resolved
    first["unresolved_company_ids"] = unresolved
    expected = int(first["results_found"])
    if expected == 0 and pages == 1:
        first["status"] = "empty_screen"
        first["complete"] = True
        first["empty"] = True
        return first
    if duplicates:
        first["status"] = "duplicate_membership"
        first["complete"] = False
        first["empty"] = False
        return first
    if expected > 0 and not unique_ids:
        first["status"] = "incomplete_rows_not_parsed"
        first["complete"] = False
        first["empty"] = False
        return first
    if len(unique_ids) < expected:
        first["status"] = "incomplete_row_coverage"
        first["complete"] = False
        first["empty"] = False
        first["parsed_ids"] = len(unique_ids)
        return first
    first["status"] = "complete_all_pages"
    first["complete"] = True
    first["empty"] = False
    return first
