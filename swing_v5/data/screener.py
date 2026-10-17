"""Public Screener pages. Page 1 is not a complete multi-page screen."""

from __future__ import annotations

import hashlib
import re
import urllib.request

SCREENS = {
    "piotroski": "https://www.screener.in/screens/2/piotroski-scan/",
    "magic": "https://www.screener.in/screens/59/magic-formula/",
    "coffee": "https://www.screener.in/screens/57601/coffee-can-portfolio/",
    "golden": "https://www.screener.in/screens/336509/golden-crossover/",
}


def parse_page(html: str, url: str) -> dict:
    match = re.search(r"(\d+) results found: Showing page (\d+) of (\d+)", html)
    query = re.search(r"<textarea[^>]*>(.*?)</textarea>", html, re.S)
    symbols = tuple(re.findall(r'href="/company/([^"/]+)/"', html))
    if match is None:
        return {"url": url, "status": "unparsed", "complete": False, "empty": False, "symbols": []}
    found, page, pages = (int(match.group(i)) for i in (1, 2, 3))
    return {
        "url": url,
        "results_found": found,
        "page": page,
        "pages": pages,
        "symbols_on_page": list(symbols),
        "query": html_unescape(query.group(1)).strip() if query else "",
        "complete": page == pages,
        "empty": found == 0,
        "content_hash": hashlib.sha256(html.encode()).hexdigest(),
    }


def html_unescape(text: str) -> str:
    return text.replace("&gt;", ">").replace("&lt;", "<").replace("&amp;", "&")


def fetch_page(url: str, timeout: int = 25) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "indian-swing-v5-research"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read().decode("utf-8", errors="replace")


def collect_screen(url: str, max_pages: int = 25) -> dict:
    try:
        status, html = fetch_page(url)
    except Exception as exc:
        return {"url": url, "status": "fetch_failed", "complete": False, "empty": False, "error": type(exc).__name__}
    if status != 200:
        return {"url": url, "status": f"http_{status}", "complete": False, "empty": False}
    first = parse_page(html, url)
    first["http_status"] = status
    pages = int(first.get("pages") or 1)
    symbols = list(first.get("symbols_on_page") or [])
    if pages > max_pages:
        first["status"] = "incomplete_not_all_pages"
        first["complete"] = False
        return first
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
        if status != 200 or parsed.get("page") != page:
            first["status"] = "pagination_failed"
            first["complete"] = False
            first["empty"] = False
            return first
        symbols.extend(parsed.get("symbols_on_page") or [])
    first["symbols_on_page"] = symbols
    first["complete"] = True
    first["status"] = "complete_all_pages" if pages > 1 else "complete_single_page"
    return first
