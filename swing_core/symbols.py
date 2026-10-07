"""TradingView ticker map. Kept here so research does not import Lambda/boto3."""

from __future__ import annotations

SYMBOL_MAP = {
    "BAJAJ-AUTO": "BAJAJ_AUTO",
    "KLBRENG-B": "KLBRENG_B",
    "SBIFUNDS": "SBIFUNDS",
    "ZOMATO": "ETERNAL",
    "544554": "KVSCAST",
    "544434": "NEETUYOSHI",
    "544669": "ADMACH",
    "538787": "GBFL",
    "526071": "STELLANT",
    "538874": "NEXUS",
    "506605": "POLYCHEM",
    "541358": "UCIL",
    "505685": "TAPARIATOOL",
    "509953": "TRADWIN",
    "538565": "VISTARAMAR",
    "506180": "EMERGENT",
    "501151": "KARTIKINV",
    "539528": "AAYUSH",
    "539196": "AMBA",
    "540252": "VSL",
    "530215": "KINGSINFA",
    "512437": "APOLLOFIN",
    "524632": "SHUKRAPHAR",
    "514448": "JYOTIRES",
    "543709": "GARGI",
    "526935": "KALIND",
    "542866": "COLAB",
    "505358": "INTEGRAENG",
    "513119": "ONIX",
    "543931": "VEEFIN",
}


def map_symbol(raw: str) -> tuple[str, str]:
    raw = str(raw).strip()
    exchange = "BSE" if raw.isdigit() else "NSE"
    return exchange, SYMBOL_MAP.get(raw, raw)
