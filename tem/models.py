"""跨模組共用的資料結構。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass
class CandidateSnapshot:
    name_en: str
    name_zh: str
    yes_price: float | None = None
    pm_prob: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    volume: float | None = None
    liquidity: float | None = None
    chg_1d: float | None = None
    chg_1w: float | None = None
    image_url: str = ""
    clob_token_id: str = ""
    question: str = ""
    market_id: str = ""
    market_slug: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name_en": self.name_en,
            "name_zh": self.name_zh,
            "yes_price": self.yes_price,
            "pm_prob": self.pm_prob,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "volume": self.volume,
            "liquidity": self.liquidity,
            "chg_1d": self.chg_1d,
            "chg_1w": self.chg_1w,
            "image_url": self.image_url,
            "clob_token_id": self.clob_token_id,
            "question": self.question,
            "market_id": self.market_id,
            "market_slug": self.market_slug,
        }


@dataclass
class MarketSnapshot:
    region_code: str
    region_name: str
    event_slug: str = ""
    candidates: list[CandidateSnapshot] = field(default_factory=list)

    @property
    def total_volume(self) -> float:
        return sum(c.volume or 0.0 for c in self.candidates)

    @property
    def total_liquidity(self) -> float:
        return sum(c.liquidity or 0.0 for c in self.candidates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "region_code": self.region_code,
            "region_name": self.region_name,
            "event_slug": self.event_slug,
            "total_volume": round(self.total_volume, 2),
            "total_liquidity": round(self.total_liquidity, 2),
            "candidates": [c.as_dict() for c in self.candidates],
        }


def build_snapshots(
    rows: Iterable[Any],
    *,
    top_n: int = 8,
    name_map: dict[str, str] | None = None,
    region_names: dict[str, str] | None = None,
) -> dict[str, MarketSnapshot]:
    """把資料庫 / 採集器的賠率列轉成各縣市快照，並計算正規化機率。

    name_map 會覆寫資料庫內的顯示名，讓 config/name_map.json 的修改立即生效，
    不必重新採集賠率。

    region_names 同理用於縣市／全島名稱：改 config/regions.json 之後，
    歷史快照的舊名稱也會跟著換掉。
    """
    grouped: dict[str, list[Any]] = {}
    for row in rows:
        get = row.get if isinstance(row, dict) else (lambda k, r=row: r[k])
        code = get("region_code")
        if not code:
            continue
        grouped.setdefault(code, []).append(row)

    snapshots: dict[str, MarketSnapshot] = {}
    for code, items in grouped.items():
        first = items[0]
        get = first.get if isinstance(first, dict) else (lambda k, r=first: r[k])
        snap = MarketSnapshot(
            region_code=code,
            region_name=(region_names or {}).get(code) or get("region_name") or code,
            event_slug=get("event_slug") or "",
        )
        total = 0.0
        for item in items:
            g = item.get if isinstance(item, dict) else (lambda k, r=item: r[k])
            price = g("yes_price") or 0.0
            if price > 0:
                total += price
        for item in items:
            g = item.get if isinstance(item, dict) else (lambda k, r=item: r[k])
            price = g("yes_price")
            name_en = g("candidate_en") or ""
            display = (name_map or {}).get(name_en) or g("candidate_zh") or name_en
            snap.candidates.append(
                CandidateSnapshot(
                    name_en=name_en,
                    name_zh=display,
                    yes_price=price,
                    pm_prob=((price or 0.0) / total) if total > 0 else None,
                    best_bid=g("best_bid"),
                    best_ask=g("best_ask"),
                    volume=g("volume"),
                    liquidity=g("liquidity"),
                    chg_1d=g("chg_1d"),
                    chg_1w=g("chg_1w"),
                    image_url=g("image_url") or "",
                    clob_token_id=g("clob_token_id") or "",
                    question=g("question") or "",
                    market_id=str(g("market_id") or ""),
                    market_slug=g("market_slug") or "",
                )
            )
        # 依市場原始 YES 價格排序，只保留有意義的候選人
        snap.candidates.sort(key=lambda c: -(c.yes_price or 0.0))
        snap.candidates = [c for c in snap.candidates if (c.yes_price or 0.0) >= 0.001][:top_n]
        snapshots[code] = snap
    return snapshots
