"""Polymarket 深度數據：盤口、逐筆成交、持倉集中度。

原本只用 Gamma API 的價格與總量。這裡補上三個更有資訊量的維度：

  1. **盤口**（CLOB `/book`）：最佳買賣價、價差、5% 內的掛單深度。
     價差突然放大通常代表做市商撤單、資訊不對稱升高。
  2. **逐筆成交**（data-api `/trades`）：近 24 小時成交筆數、金額、
     買賣方向失衡、最大單（大戶動向）。
  3. **持倉集中度**（data-api `/holders`）：前幾大持有人的佔比與 HHI。
     少數地址吃下多數部位，價格更容易被單邊推動。

這三者都不需要 API 金鑰。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from .http import fetch_json

CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
UA = {"User-Agent": "TEM-election-monitor/1.0 (local research tool; python-urllib)"}


def order_book(token_id: str) -> dict[str, Any]:
    """取盤口，計出價差與深度。"""
    data = fetch_json(f"{CLOB}/book?token_id={token_id}", timeout=30, retries=2, headers=UA)
    bids = [(float(b["price"]), float(b["size"])) for b in data.get("bids") or []]
    asks = [(float(a["price"]), float(a["size"])) for a in data.get("asks") or []]
    bids.sort(key=lambda x: -x[0])
    asks.sort(key=lambda x: x[0])
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = round((best_bid + best_ask) / 2, 4) if best_bid and best_ask else (best_bid or best_ask)
    spread = round(best_ask - best_bid, 4) if best_bid and best_ask else None
    # 中間價 5% 內的掛單深度（USDC 名目）
    depth = 0.0
    if mid:
        for price, size in bids:
            if price >= mid * 0.95:
                depth += price * size
        for price, size in asks:
            if price <= mid * 1.05:
                depth += price * size
    return {
        "condition_id": data.get("market"),
        "asset_id": data.get("asset_id"),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        # 機率點數（1.0 = 100 個百分點）。相對價差在極端價位會爆掉，只在中間價位才給
        "spread_pts": round(spread * 100, 2) if spread is not None else None,
        "spread_pct": (
            round(spread / mid, 4) if (spread is not None and mid and 0.05 <= mid <= 0.95) else None
        ),
        "depth_usdc": round(depth, 2),
        "bid_levels": len(bids),
        "ask_levels": len(asks),
    }


def recent_trades(condition_id: str, *, hours: int = 24, limit: int = 500) -> dict[str, Any]:
    """近 N 小時的逐筆成交統計。"""
    data = fetch_json(
        f"{DATA_API}/trades?market={condition_id}&limit={limit}",
        timeout=30, retries=2, headers=UA,
    )
    if not isinstance(data, list):
        data = []
    cutoff = time.time() - hours * 3600
    rows = [t for t in data if float(t.get("timestamp") or 0) >= cutoff]
    if not rows:  # 冷門市場可能整天沒成交，退回最近幾筆
        rows = data[: min(len(data), 20)]

    buy = sell = 0.0
    largest = 0.0
    largest_trade: dict[str, Any] | None = None
    traders: set[str] = set()
    for t in rows:
        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        notional = size * price
        side = (t.get("side") or "").upper()
        if side == "BUY":
            buy += notional
        elif side == "SELL":
            sell += notional
        if notional > largest:
            largest = notional
            largest_trade = {
                "side": side, "notional": round(notional, 2), "price": price,
                "ts": datetime.fromtimestamp(float(t.get("timestamp") or 0), timezone.utc).isoformat(timespec="minutes"),
            }
        if t.get("proxyWallet"):
            traders.add(t["proxyWallet"])
    total = buy + sell
    return {
        "trades": len(rows),
        "volume_usdc": round(total, 2),
        "buy_usdc": round(buy, 2),
        "sell_usdc": round(sell, 2),
        # 正值 = 買方主導
        "flow_imbalance": round((buy - sell) / total, 3) if total else None,
        "unique_traders": len(traders),
        "largest_usdc": round(largest, 2),
        "largest_trade": largest_trade,
    }


def holders(condition_id: str, *, limit: int = 50) -> dict[str, Any]:
    """持倉集中度：最大持有人佔比與 HHI。"""
    data = fetch_json(
        f"{DATA_API}/holders?market={condition_id}&limit={limit}",
        timeout=30, retries=2, headers=UA,
    )
    if not isinstance(data, list):
        return {}
    # 只取 YES（outcomeIndex 0）那一側，代表「押他當選」的部位
    yes = next((g for g in data if int(g.get("outcomeIndex") or 0) == 0), None)
    if yes is None and data:
        yes = data[0]
    if not yes:
        return {}
    amounts = sorted((float(h.get("amount") or 0) for h in yes.get("holders") or []), reverse=True)
    total = sum(amounts)
    if total <= 0:
        return {"holder_count": len(amounts), "top_share": None, "hhi": None}
    shares = [a / total for a in amounts]
    return {
        "holder_count": len(amounts),
        "top_share": round(shares[0], 4),
        "top3_share": round(sum(shares[:3]), 4),
        "hhi": round(sum(s * s for s in shares), 4),
        "yes_amount": round(total, 2),
        "top_holders": [
            {
                "name": (h.get("name") or h.get("pseudonym") or "")[:24],
                "amount": round(float(h.get("amount") or 0), 2),
            }
            for h in (yes.get("holders") or [])[:5]
        ],
    }


def collect_market_depth(
    markets: Iterable[dict[str, Any]],
    *,
    delay: float = 0.25,
    hours: int = 24,
) -> list[dict[str, Any]]:
    """markets: [{market_id, token_id, region_code, candidate_zh, event_slug}]。"""
    rows: list[dict[str, Any]] = []
    for item in markets:
        token_id = item.get("token_id")
        if not token_id:
            continue
        row: dict[str, Any] = {
            "market_id": item.get("market_id"),
            "token_id": token_id,
            "region_code": item.get("region_code"),
            "candidate_zh": item.get("candidate_zh"),
            "candidate_en": item.get("candidate_en"),
            "event_slug": item.get("event_slug"),
        }
        try:
            book = order_book(token_id)
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"book:{type(exc).__name__}"
            rows.append(row)
            continue
        row.update(book)
        condition_id = book.get("condition_id")
        if condition_id:
            time.sleep(delay)
            try:
                row.update(recent_trades(condition_id, hours=hours))
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"trades:{type(exc).__name__}"
            time.sleep(delay)
            try:
                row.update(holders(condition_id))
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"holders:{type(exc).__name__}"
        rows.append(row)
        time.sleep(delay)
    return rows


def depth_signal(row: dict[str, Any]) -> dict[str, Any]:
    """把深度數據壓成幾個可比較的訊號值。"""
    depth = row.get("depth_usdc") or 0.0
    imbalance = row.get("flow_imbalance")
    top_share = row.get("top_share")
    return {
        "spread_pts": row.get("spread_pts"),
        "spread_pct": row.get("spread_pct"),
        "depth_usdc": depth,
        # 深度分數：10 萬 USDC 掛單深度視為滿分
        "depth_score": round(min(1.0, depth / 100_000.0), 3),
        "flow_imbalance": imbalance,
        "large_trade": row.get("largest_usdc"),
        "top_share": top_share,
        # 集中度越高越容易被單邊推動
        "concentration": top_share,
        "note": _note(row),
    }


def _note(row: dict[str, Any]) -> str:
    bits: list[str] = []
    spread = row.get("spread")
    if spread is not None:
        if spread >= 0.04:
            bits.append("價差偏大")
        elif spread <= 0.01:
            bits.append("盤口緊密")
    depth = row.get("depth_usdc")
    if depth is not None and depth < 3000:
        bits.append("掛單稀薄")
    imbalance = row.get("flow_imbalance")
    if imbalance is not None and abs(imbalance) >= 0.3:
        bits.append("買方主導" if imbalance > 0 else "賣方主導")
    top = row.get("top_share")
    if top is not None and top >= 0.5:
        bits.append("持倉高度集中")
    return "、".join(bits)


def save_snapshot(ts: str, rows: list[dict[str, Any]]) -> str:
    from .paths import EXPORT_DIR, ensure_dirs

    ensure_dirs()
    path = EXPORT_DIR / f"market-depth-{ts.replace(':', '').replace('-', '')[:15]}.json"
    path.write_text(json.dumps({"ts": ts, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
