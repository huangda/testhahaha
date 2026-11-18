"""Polymarket 賠率採集。

資料來源為 Polymarket 官方公開 Gamma API（免金鑰）：
  - 事件列表： https://gamma-api.polymarket.com/events?tag_slug=taiwan-elections
  - 單一事件： https://gamma-api.polymarket.com/events?slug=<slug>
使用者提供的 embed <script>/<figure> 片段只作為前端嵌入用途，其 market event slug
與此處 gamma API 的 slug 完全一致，因此可用同一份資料同時產出資料與嵌入碼。
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, asdict
from typing import Any, Iterable

from .config import Region, Settings
from .http import encode_query, fetch_json

GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
PLACEHOLDER_RE = re.compile(r"^Candidate [A-Z]$")
_MARKET_SLUGS_BY_EVENT: dict[str, dict[str, str]] = {}


@dataclass
class MarketRow:
    ts: str
    market_id: str
    event_slug: str
    region_code: str
    region_name: str
    candidate_en: str
    candidate_zh: str
    yes_price: float | None
    best_bid: float | None
    best_ask: float | None
    last_trade: float | None
    volume: float | None
    liquidity: float | None
    chg_1d: float | None
    chg_1w: float | None
    image_url: str
    clob_token_id: str
    question: str
    market_slug: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = ast.literal_eval(value)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, SyntaxError):
            return []
    return []


def fetch_events(*, tag_slug: str = "taiwan-elections", limit: int = 100) -> list[dict[str, Any]]:
    """以 tag 抓取所有台灣選舉事件。"""
    url = f"{GAMMA_EVENTS}?{encode_query({'tag_slug': tag_slug, 'closed': 'false', 'limit': limit})}"
    data = fetch_json(url, timeout=60)
    return data if isinstance(data, list) else []


def fetch_event_by_slug(slug: str) -> dict[str, Any] | None:
    url = f"{GAMMA_EVENTS}?{encode_query({'slug': slug})}"
    data = fetch_json(url, timeout=60)
    if isinstance(data, list):
        return data[0] if data else None
    return data if isinstance(data, dict) else None


def market_slug_for_id(event_slug: str, market_id: str) -> str:
    """以市场 ID 查找候选人专属 slug，兼容尚未写入新版字段的旧数据。"""
    event_slug = (event_slug or "").strip()
    market_id = str(market_id or "").strip()
    if not event_slug or not market_id:
        return ""
    mapping = _MARKET_SLUGS_BY_EVENT.get(event_slug)
    if mapping is None:
        event = fetch_event_by_slug(event_slug)
        mapping = {
            str(market.get("id") or ""): str(market.get("slug") or "")
            for market in ((event or {}).get("markets") or [])
            if market.get("id") and market.get("slug")
        }
        _MARKET_SLUGS_BY_EVENT[event_slug] = mapping
    return mapping.get(market_id, "")


def _match_region(event: dict[str, Any], settings: Settings, used: set[str]) -> Region | None:
    slug = event.get("slug") or ""
    title = (event.get("title") or "").lower()

    for region in settings.regions:
        if region.polymarket_slug and region.polymarket_slug == slug:
            return region
    if settings.nationwide and settings.nationwide.polymarket_slug == slug:
        return settings.nationwide

    for region in settings.regions:
        if region.code in used:
            continue
        for alias in region.aliases:
            if len(alias) > 3 and alias.lower() in title:
                return region
    if settings.nationwide and "party winner" in title:
        return settings.nationwide
    return None


def collect(
    settings: Settings,
    ts: str,
    *,
    events: list[dict[str, Any]] | None = None,
) -> tuple[list[MarketRow], list[dict[str, Any]], list[Region]]:
    """回傳 (市場列, 原始事件, 未對應到設定檔的事件)。"""
    events = events if events is not None else fetch_events(tag_slug=settings.polymarket_tag)
    rows: list[MarketRow] = []
    matched_regions: list[Region] = []
    unmatched: list[dict[str, Any]] = []
    used: set[str] = set()

    for event in events:
        region = _match_region(event, settings, used)
        if region is None:
            unmatched.append(event)
            continue
        used.add(region.code)
        matched_regions.append(region)
        for market in event.get("markets") or []:
            name_en = (market.get("groupItemTitle") or market.get("question") or "").strip()
            if not name_en or PLACEHOLDER_RE.match(name_en):
                continue
            if not market.get("active", True):
                continue
            prices = _parse_list(market.get("outcomePrices"))
            yes = _to_float(prices[0]) if prices else None
            token_ids = _parse_list(market.get("clobTokenIds"))
            rows.append(
                MarketRow(
                    ts=ts,
                    market_id=str(market.get("id")),
                    event_slug=event.get("slug") or "",
                    region_code=region.code,
                    region_name=region.name,
                    candidate_en=name_en,
                    candidate_zh=settings.display_name(name_en),
                    yes_price=yes,
                    best_bid=_to_float(market.get("bestBid")),
                    best_ask=_to_float(market.get("bestAsk")),
                    last_trade=_to_float(market.get("lastTradePrice")),
                    volume=_to_float(market.get("volume")),
                    liquidity=_to_float(market.get("liquidity")),
                    chg_1d=_to_float(market.get("oneDayPriceChange")),
                    chg_1w=_to_float(market.get("oneWeekPriceChange")),
                    image_url=market.get("image") or market.get("icon") or "",
                    clob_token_id=str(token_ids[0]) if token_ids else "",
                    question=market.get("question") or "",
                    market_slug=market.get("slug") or "",
                )
            )
    return rows, events, unmatched


def normalised_probabilities(rows: Iterable[MarketRow]) -> dict[str, float]:
    """把同一選區的 YES 價格正規化為加總 1 的機率分布。"""
    by_region: dict[str, list[MarketRow]] = {}
    for row in rows:
        if row.yes_price is None:
            continue
        by_region.setdefault(row.region_code, []).append(row)

    normalised: dict[str, float] = {}
    for region_rows in by_region.values():
        total = sum(r.yes_price or 0.0 for r in region_rows)
        if total <= 0:
            continue
        for row in region_rows:
            key = f"{row.region_code}::{row.candidate_en}"
            normalised[key] = (row.yes_price or 0.0) / total
    return normalised


def build_embed(event_slug: str, title: str, yes_price: float | None, locale: str = "zh-Hant") -> dict[str, str]:
    """產生與使用者範例相同格式的 Polymarket 嵌入碼。"""
    pct = f"{round((yes_price or 0) * 100)}%"
    event_url = f"https://polymarket.com/event/{event_slug}"
    embed_src = (
        f"https://embed.polymarket.com/market?event={event_slug}"
        "&rotate=true&buttons=false&height=300"
    )
    jsonld = (
        '<script type="application/ld+json">\n'
        "{\n"
        '  "@context": "https://schema.org",\n'
        '  "@type": "WebPage",\n'
        f'  "name": {json.dumps(title, ensure_ascii=False)},\n'
        f'  "description": "Prediction market: 是 {pct} on Polymarket.",\n'
        f'  "url": "{event_url}",\n'
        '  "publisher": {"@type": "Organization", "name": "Polymarket", "url": "https://polymarket.com"}\n'
        "}\n"
        "</script>"
    )
    figure = (
        f'<figure class="polymarket-embed" itemscope '
        f'itemtype="https://schema.org/WebPage" style="position:relative;display:inline-block;margin:0">\n'
        f'  <iframe title="{title} — Polymarket Prediction Market"\n'
        f'    src="{embed_src}" width="400" height="300" frameborder="0" allowtransparency="true"></iframe>\n'
        f'  <a href="{event_url}" aria-label="View on Polymarket" target="_blank" rel="noopener"\n'
        f'    style="position:absolute;top:16px;right:20px;width:120px;height:24px;z-index:10"></a>\n'
        f"</figure>"
    )
    return {"jsonld": jsonld, "figure": figure, "event_url": event_url}
