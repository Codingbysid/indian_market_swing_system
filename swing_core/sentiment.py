"""Timestamped, explainable news/event overlay. It is not a trained return forecast.
NSE filings are primary; optional Google News adds headline context for holdings.
A configured backend must satisfy the explicit contract below. Missing != neutral.
"""

from __future__ import annotations
import hashlib, html, json, math, re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET
import requests

RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NEGATIVE = (
    r"\bcredit rating downgrade",
    r"\bdefault(?:ed)? on",
    r"\bfraud\b",
    r"\binsolvency\b",
    r"\bauditor resign",
    r"\bprofit (?:falls|fell|declines|dropped|slumps)\b",
    r"\bnet loss widens\b",
)
POSITIVE = (
    r"\b(?:wins|bags|secures|awarded) (?:a |an |new )?(?:order|contract)",
    r"\bprofit (?:rises|rose|increases|jumps)\b",
    r"\brating upgrade\b",
)


def parse_time(value: str):
    try:
        d = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        try:
            d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            try:
                d = datetime.strptime(value, "%d-%b-%Y %H:%M:%S").replace(
                    tzinfo=ZoneInfo("Asia/Kolkata")
                )
            except (ValueError, TypeError):
                return None
    return (d if d.tzinfo else d.replace(tzinfo=ZoneInfo("Asia/Kolkata"))).astimezone(
        timezone.utc
    )


def tone(text):
    text = html.unescape(re.sub("<[^>]+>", " ", text)).lower()
    # Negated or routine templates must not become adverse company events.
    if re.search(r"\bno (?:fraud|default)|statement of (?:profit|deviation)", text):
        return 0, []
    neg = [p for p in NEGATIVE if re.search(p, text)]
    pos = [p for p in POSITIVE if re.search(p, text)]
    return (-1 if neg else 1 if pos else 0), neg + pos


def parse_feed(
    raw: str, metadata: list[dict], as_of: datetime, *, source: str, query_symbol=None
):
    root = ET.fromstring(raw)
    out = []
    seen = set()
    now = as_of.astimezone(timezone.utc)
    for it in root.findall(".//item"):
        title = html.unescape(it.findtext("title", ""))
        description = it.findtext("description", "")
        link = it.findtext("link", "")
        stamp = parse_time(it.findtext("pubDate", ""))
        if stamp is None or not now - timedelta(days=7) <= stamp <= now:
            continue
        full = (title + " " + description).lower()
        normalized = re.sub(r"[^a-z0-9]+", " ", full)
        symbol = None
        for m in metadata:
            s = m["Symbol"]
            name = m["Name"].lower().replace(" ltd.", "").replace(" limited", "")
            exact_pdf = bool(re.search("/" + re.escape(s) + r"_", link, re.I))
            name = re.sub(r"[^a-z0-9]+", " ", name).strip()
            exact_name = len(name) > 6 and name in normalized
            exact_ticker = bool(
                re.search(
                    r"\b(?:nse|nsei)\s*[:\-]?\s*" + re.escape(s) + r"\b", full, re.I
                )
            )
            scoped_ticker = (
                query_symbol == s
                and bool(re.search(r"\b" + re.escape(s) + r"\b", title, re.I))
                and bool(
                    re.search(r"\bstock|\bshare|\bcontract|\border|\bprofit", full)
                )
            )
            if (exact_pdf or exact_name or exact_ticker or scoped_ticker) and (
                query_symbol is None or s == query_symbol
            ):
                symbol = s
                break
        if symbol is None:
            continue
        digest = hashlib.sha256(
            (symbol + "|" + re.sub(r"\s+", " ", title.lower().split(" - ")[0])).encode()
        ).hexdigest()[:20]
        if digest in seen:
            continue
        seen.add(digest)
        score, patterns = tone(full)
        out.append(
            {
                "id": digest,
                "symbol": symbol,
                "title": title,
                "url": link,
                "published_at": stamp.isoformat(),
                "observed_at": now.isoformat(),
                "source": source,
                "tone": score,
                "matched_rules": patterns,
                "event_review": bool(
                    re.search(
                        r"board meeting|financial results|earnings|fraud|insolvency|default on",
                        full,
                    )
                ),
            }
        )
    return out


def fetch_overlay(
    metadata: list[dict],
    held_symbols: list[str],
    *,
    as_of=None,
    backend_url=None,
    include_news=True,
    session=None,
):
    now = as_of or datetime.now(timezone.utc)
    session = session or requests.Session()
    items = []
    health = []
    sources = [("nse", RSS, None)]
    if include_news:
        names = {m["Symbol"]: m["Name"] for m in metadata}
        for s in held_symbols:
            q = quote('"' + names.get(s, s).replace(" Ltd.", "") + '" stock when:7d')
            sources.append(
                (
                    "google_news",
                    f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en",
                    s,
                )
            )
    for name, url, symbol in sources:
        try:
            for attempt in range(2 if name == "nse" else 1):
                try:
                    r = session.get(
                        url,
                        timeout=(3, 6),
                        headers={"User-Agent": "indian-swing-research/4.0"},
                    )
                    r.raise_for_status()
                    break
                except requests.RequestException:
                    if name != "nse" or attempt == 1:
                        raise
            if len(r.content) > 2_000_000:
                raise ValueError("oversize_feed")
            events = parse_feed(r.text, metadata, now, source=name, query_symbol=symbol)
            items += events
            health.append(
                {
                    "provider": name,
                    "symbol": symbol,
                    "status": "OK",
                    "matched_events": len(events),
                }
            )
        except (requests.RequestException, ValueError, ET.ParseError) as e:
            health.append(
                {
                    "provider": name,
                    "symbol": symbol,
                    "status": "UNAVAILABLE",
                    "error": type(e).__name__,
                }
            )
    if backend_url:
        try:
            if urlparse(backend_url).scheme != "https":
                raise ValueError("backend_requires_https")
            r = session.get(
                backend_url, params={"symbols": ",".join(held_symbols)}, timeout=(3, 6)
            )
            r.raise_for_status()
            payload = r.json()
            # Contract: events[{symbol,title,url,published_at,tone(-1..1)}], as_of ISO.
            stamp = parse_time(payload.get("as_of", ""))
            if stamp is None or not now - timedelta(hours=24) <= stamp <= now:
                raise ValueError("stale_backend")
            for e in payload["events"]:
                ts = parse_time(e.get("published_at", ""))
                score = float(e["tone"])
                if (
                    e["symbol"] not in held_symbols
                    or ts is None
                    or not now - timedelta(days=7) <= ts <= now
                    or not math.isfinite(score)
                    or abs(score) > 1
                ):
                    continue
                items.append(
                    {
                        **e,
                        "source": "backend",
                        "published_at": ts.isoformat(),
                        "observed_at": now.isoformat(),
                        "id": hashlib.sha256(
                            (e["symbol"] + e["title"]).encode()
                        ).hexdigest()[:20],
                        "event_review": bool(e.get("event_review", False)),
                    }
                )
            health.append({"provider": "backend", "status": "OK"})
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            health.append(
                {
                    "provider": "backend",
                    "status": "UNAVAILABLE",
                    "error": type(e).__name__,
                }
            )
    dedup = {e["id"]: e for e in items}
    items = list(dedup.values())
    summaries = {}
    for m in metadata:
        s = m["Symbol"]
        events = [e for e in items if e["symbol"] == s]
        fresh = [
            e
            for e in events
            if parse_time(e["published_at"]) >= now - timedelta(hours=48)
        ]
        weights = [
            math.exp(-(now - parse_time(e["published_at"])).total_seconds() / 86400 / 2)
            for e in events
        ]
        score = (
            sum(e["tone"] * w for e, w in zip(events, weights)) / sum(weights)
            if weights
            else None
        )
        summaries[s] = {
            "status": "OBSERVED" if events else "UNKNOWN",
            "headline_tone": score,
            "event_count": len(events),
            "review_required": any(e["event_review"] or e["tone"] < 0 for e in fresh),
            "action": "HOLD_RULES_UNCHANGED",
            "events": events[:8],
        }
    return {
        "as_of": now.isoformat(),
        "method": "rules_v1_headlines_and_filings",
        "trained_alpha": False,
        "health": health,
        "symbols": summaries,
        "backend_configured": bool(backend_url),
    }
