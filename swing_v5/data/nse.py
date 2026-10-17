"""NSE report candidates. A URL is recorded only after a response is observed."""

from __future__ import annotations

import urllib.request

CANDIDATE_URLS = [
    "https://www.nseindia.com/all-reports",
    "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_20261016_F_0000.csv.zip",
    "https://archives.nseindia.com/content/historical/EQUITIES/2026/OCT/cm16OCT2026bhav.csv.zip",
]


def probe_url(url: str, timeout: int = 20) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(400)
            zip_header = payload[:2] == b"PK"
            return {
                "url": url,
                "status": "response",
                "http_status": response.status,
                "content_type": response.headers.get("Content-Type"),
                "sample_bytes": len(payload),
                "zip_header": zip_header,
                "historical_file_verified": False,
                "note": "zip signature observed; full bhavcopy schema was not parsed into bars" if zip_header else "not a verified historical file",
            }
    except Exception as exc:
        return {"url": url, "status": "failed", "error": type(exc).__name__, "historical_file_verified": False}
