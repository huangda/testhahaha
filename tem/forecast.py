"""輿情 × 賠率 × 民調 融合預測。

市場權重依成交量、流動性、掛單深度和價差動態調整：
高流動性市場以市場為主，低流動性市場以新鮮民調為主，新聞輿情固定佔約一成。
市場機率會先依市場可靠度向 50% 收縮，避免薄弱盤口直接產生極端勝率。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import Settings
from .models import MarketSnapshot

W_SENTIMENT = 0.10
MARKET_WEIGHTS = {
    "high": {"market": 0.55, "poll": 0.35, "sentiment": 0.10, "reliability": 0.90},
    "medium": {"market": 0.40, "poll": 0.50, "sentiment": 0.10, "reliability": 0.68},
    "low": {"market": 0.25, "poll": 0.65, "sentiment": 0.10, "reliability": 0.45},
}

# 「膠著選區」的判定線：融合機率前兩名差距 ≤ 此值。
# 調高會把更多邊緣選區納入（例如 2026-10 的新北市 13.9%）。
TOSSUP_MARGIN = 0.15

@dataclass
class Context:
    """融合用的外部訊號。"""

    polls: dict[str, Any] = field(default_factory=dict)
    attention: dict[str, Any] = field(default_factory=dict)
    depth: dict[str, Any] = field(default_factory=dict)


def _norm_name(name: str) -> str:
    return (name or "").replace("臺", "台").replace("黃", "黄").strip()


def load_context() -> Context:
    """從設定檔與資料庫組出融合所需的上下文，任何一項缺失都不影響其他項。"""
    from . import db
    from .polls import load_polls

    ctx = Context()
    try:
        ctx.polls = load_polls().get("regions") or {}
    except Exception:  # noqa: BLE001
        ctx.polls = {}
    try:
        for row in db.fetch_latest_attention():
            ctx.attention.setdefault(row["entity"], {})[row["source"]] = dict(row)
    except Exception:  # noqa: BLE001
        ctx.attention = {}
    try:
        for row in db.fetch_latest_market_depth():
            ctx.depth[str(row["market_id"])] = dict(row)
    except Exception:  # noqa: BLE001
        ctx.depth = {}
    return ctx


def _lookup(table: dict[str, Any], region_name: str) -> dict[str, Any] | None:
    want = _norm_name(region_name)
    if want in table:
        return table[want]
    for key, value in table.items():
        if _norm_name(key) == want:
            return value
    return None


# ---------------------------------------------------------------- 民調通道

def poll_channel(
    region_name: str,
    candidate_names: list[str],
    ctx: Context,
    market_probs: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """取與市場候選人組合最相符的民調對戰組合，再對齊候選人。"""
    from .polls import matchup_signal

    signal = matchup_signal(
        region_name,
        candidate_names,
        market_probs=dict(market_probs) if market_probs else None,
    )
    if not signal or not signal.get("shares"):
        return None
    wanted = {_norm_name(n): n for n in candidate_names if n}
    shares: dict[str, float] = {}
    for name, share in signal["shares"].items():
        key = _norm_name(name)
        if key in wanted:
            market_name = wanted[key]
            # 市場幾乎不給機率的候選人（多半是民調表裡同黨的對手），不該主導民調份額
            if market_probs and (market_probs.get(market_name) or 0.0) < 0.01:
                continue
            shares[market_name] = float(share)
    if len(shares) < 2:
        for name, share in signal["shares"].items():
            key = _norm_name(name)
            if key in wanted:
                shares[wanted[key]] = float(share)
    if len(shares) < 2:
        return None
    total = sum(shares.values())
    if total <= 0:
        return None
    shares = {name: value / total for name, value in shares.items()}
    # 用歷史回測的校準曲線，把「民調領先幅度」轉成勝率，取代直接拿支持度當機率
    from .backtest import calibrated_prob

    calibrated = calibrated_prob(signal.get("margin"))
    if calibrated is not None and signal.get("leader"):
        leader = wanted.get(_norm_name(signal["leader"]))
        if leader and leader in shares:
            others = {k: v for k, v in shares.items() if k != leader}
            other_total = sum(others.values()) or 1.0
            shares = {leader: calibrated}
            shares.update({k: v / other_total * (1 - calibrated) for k, v in others.items()})
    eff = float(signal.get("effective_samples") or 0.0)
    availability = min(1.0, eff / 3.0)
    return {
        "shares": shares,
        "raw": {name: (signal.get("weighted") or {}).get(name) for name in shares},
        "availability": round(availability, 3),
        "effective_samples": signal.get("effective_samples"),
        "freshness": signal.get("freshness"),
        "date": signal.get("effective_date"),
        "sources": signal.get("poll_count"),
        "leader": signal.get("leader"),
        "margin": signal.get("margin"),
        "matchup": signal.get("matchup"),
        "calibrated": calibrated,
        "bias": signal.get("bias") or {},
    }


# ---------------------------------------------------------------- 融合

def _market_profile(snapshot: MarketSnapshot, ctx: Context) -> dict[str, Any]:
    """以成交量、流动性和盘口质量划分市场可靠度。"""
    volume = float(snapshot.total_volume or 0.0)
    liquidity = float(snapshot.total_liquidity or 0.0)
    depths: list[float] = []
    spreads: list[float] = []
    for candidate in snapshot.candidates:
        depth = ctx.depth.get(str(candidate.market_id)) or {}
        if depth.get("depth_usdc") is not None:
            depths.append(float(depth.get("depth_usdc") or 0.0))
        if depth.get("spread") is not None:
            spreads.append(float(depth.get("spread") or 0.0))
    max_depth = max(depths, default=0.0)
    min_spread = min(spreads, default=None)

    if (
        volume >= 20_000
        and (liquidity >= 10_000 or max_depth >= 5_000)
        and (min_spread is None or min_spread <= 0.03)
    ):
        tier = "high"
    elif (
        volume >= 5_000
        and (liquidity >= 2_000 or max_depth >= 500)
        and (min_spread is None or min_spread <= 0.05)
    ):
        tier = "medium"
    else:
        tier = "low"
    return {
        "tier": tier,
        "volume": round(volume, 2),
        "liquidity": round(liquidity, 2),
        "depth_usdc": round(max_depth, 2),
        "spread": round(min_spread, 4) if min_spread is not None else None,
        **MARKET_WEIGHTS[tier],
    }


def fuse(
    settings: Settings,
    snapshots: dict[str, MarketSnapshot],
    candidate_sentiment: dict[str, dict[str, Any]],
    region_states: Iterable[dict[str, Any]],
    *,
    context: Context | None = None,
) -> list[dict[str, Any]]:
    ctx = context if context is not None else load_context()
    states = {s["region_code"]: s for s in region_states}
    rows: list[dict[str, Any]] = []

    for code, snapshot in snapshots.items():
        state = states.get(code, {})
        articles = int(state.get("article_count") or 0)
        region_sentiment = float(state.get("avg_sentiment") or 0.0)

        names = [c.name_zh for c in snapshot.candidates]
        polls_by_name = {c.name_zh: (c.pm_prob or 0.0) for c in snapshot.candidates}
        poll = poll_channel(snapshot.region_name, names, ctx, market_probs=polls_by_name)
        profile = _market_profile(snapshot, ctx)
        poll_strength = 0.0
        if poll:
            freshness = float(poll.get("freshness") or 0.0)
            source_count = int(poll.get("sources") or 0)
            poll_strength = max(
                0.15,
                min(1.0, 0.75 * freshness + 0.25 * min(1.0, source_count / 2.0)),
            )
        sentiment_weight = float(profile["sentiment"])
        poll_weight = float(profile["poll"]) * poll_strength if poll else 0.0
        market_weight = max(0.0, 1.0 - sentiment_weight - poll_weight)
        region_weights = {
            "market": market_weight,
            "poll": poll_weight,
            "sentiment": sentiment_weight,
        }
        scored: list[tuple[Any, dict[str, Any]]] = []
        for candidate in snapshot.candidates:
            pm = candidate.pm_prob or 0.0

            signal = candidate_sentiment.get(candidate.name_en)
            if signal and signal["articles"] > 0:
                cand_sent = float(signal["sentiment"])
                candidate_weight = int(signal["articles"]) / (int(signal["articles"]) + 6.0)
                sentiment = (
                    cand_sent * candidate_weight
                    + region_sentiment * (1 - candidate_weight)
                )
            else:
                region_weight = articles / (articles + 6.0) if articles else 0.0
                sentiment = region_sentiment * region_weight

            poll_share = (poll or {}).get("shares", {}).get(candidate.name_zh)
            # 在多候选人竞争中，直接逐人向 50% 收缩会虚增长期落后者的概率。
            # 用幂函数压低极端概率、保留候选人排序，等价于降低市场分布的“锐度”。
            market_score = max(pm, 1e-6) ** float(profile["reliability"])
            news_score = max(0.05, 1.0 + 0.5 * max(-1.0, min(1.0, sentiment)))
            scored.append((candidate, {
                "pm": pm,
                "market_score": market_score,
                "poll_share": poll_share,
                "news_score": news_score,
                "sentiment": sentiment,
            }))

        market_total = sum(s["market_score"] for _, s in scored) or 1.0
        news_total = sum(s["news_score"] for _, s in scored) or 1.0
        combined: list[tuple[Any, dict[str, Any], float, dict[str, float]]] = []
        for candidate, signal in scored:
            market_prob = signal["market_score"] / market_total
            news_prob = signal["news_score"] / news_total
            raw_score = (
                region_weights["market"] * market_prob
                + region_weights["poll"] * float(signal["poll_share"] or 0.0)
                + region_weights["sentiment"] * news_prob
            )
            combined.append((candidate, signal, raw_score, region_weights))

        raw_total = sum(score for _, _, score, _ in combined) or 1.0
        merged = [
            (candidate, signal, score / raw_total, applied_weights)
            for candidate, signal, score, applied_weights in combined
        ]

        for candidate, sig, prob, applied_weights in merged:
            poll_raw = (poll or {}).get("raw", {}).get(candidate.name_zh)
            rows.append({
                "region_code": code,
                "region_name": snapshot.region_name,
                "candidate_en": candidate.name_en,
                "candidate_zh": candidate.name_zh,
                "pm_prob": round(sig["pm"], 6),
                "pm_raw": candidate.yes_price,
                "poll_prob": round(sig["poll_share"], 6) if sig["poll_share"] is not None else None,
                "poll_share": poll_raw,
                "poll_margin": poll.get("margin") if poll else None,
                "news_sentiment": round(sig["sentiment"], 4),
                "fused_prob": round(prob, 6),
                "rationale": _rationale(
                    candidate.name_zh, sig["pm"], prob, sig["sentiment"], articles,
                    candidate.chg_1d, candidate.chg_1w, poll_raw,
                ),
                "edge": round(prob - sig["pm"], 6),
                "articles": articles,
                "region_sentiment": region_sentiment,
                "chg_1d": candidate.chg_1d,
                "chg_1w": candidate.chg_1w,
                "volume": candidate.volume,
                "poll_leader": poll.get("leader") if poll else None,
                "components": {
                    "article_count": articles,
                    "poll_effective_samples": poll.get("effective_samples") if poll else None,
                    "poll_calibrated": poll.get("calibrated") if poll else None,
                    "poll_date": poll.get("date") if poll else None,
                    "poll_freshness": poll.get("freshness") if poll else None,
                    "market_tier": profile["tier"],
                    "market_reliability": profile["reliability"],
                    "market_score": round(sig["market_score"], 6),
                    "market_weight": round(applied_weights["market"], 4),
                    "poll_weight": round(applied_weights["poll"], 4),
                    "sentiment_weight": round(applied_weights["sentiment"], 4),
                    "poll_strength": round(poll_strength, 4),
                    "market_volume": profile["volume"],
                    "market_liquidity": profile["liquidity"],
                    "market_depth_usdc": profile["depth_usdc"],
                    "market_spread": profile["spread"],
                },
                "leader": False,
            })

        region_rows = [r for r in rows if r["region_code"] == code]
        if region_rows:
            max(region_rows, key=lambda r: r["fused_prob"])["leader"] = True
    return rows


def candidate_news_sentiment_scores(
    snapshots: dict[str, MarketSnapshot],
    candidate_sentiment: dict[str, dict[str, Any]],
    region_states: Iterable[dict[str, Any]],
) -> dict[str, float]:
    """只计算候选人的新闻情感分，不执行胜率公式。"""
    states = {s["region_code"]: s for s in region_states}
    out: dict[str, float] = {}
    for code, snapshot in snapshots.items():
        state = states.get(code, {})
        articles = int(state.get("article_count") or 0)
        region_sentiment = float(state.get("avg_sentiment") or 0.0)
        for candidate in snapshot.candidates:
            signal = candidate_sentiment.get(candidate.name_en)
            if signal and signal["articles"] > 0:
                cand_sent = float(signal["sentiment"])
                candidate_weight = int(signal["articles"]) / (
                    int(signal["articles"]) + 6.0
                )
                sentiment = (
                    cand_sent * candidate_weight
                    + region_sentiment * (1 - candidate_weight)
                )
            else:
                region_weight = (
                    articles / (articles + 6.0) if articles else 0.0
                )
                sentiment = region_sentiment * region_weight
            out[candidate.name_en] = round(sentiment, 4)
    return out


def _rationale(
    candidate_zh: str,
    pm_prob: float | None,
    fused: float | None,
    sentiment: float,
    articles: int,
    chg_1d: float | None,
    chg_1w: float | None,
    poll_share: float | None,
) -> str:
    parts: list[str] = []
    if pm_prob is not None:
        parts.append(f"市場機率 {pm_prob * 100:.1f}%")
    if poll_share is not None:
        parts.append(f"民調加權 {poll_share:.1f}%")
    if fused is not None and pm_prob is not None:
        delta = (fused - pm_prob) * 100
        direction = "上修" if delta > 0.15 else ("下修" if delta < -0.15 else "持平")
        parts.append(f"模型修正 {direction} {abs(delta):.1f}%")
    if articles:
        tone = "偏正面" if sentiment > 0.12 else ("偏負面" if sentiment < -0.12 else "中性")
        parts.append(f"{articles} 則相關新聞、情感 {sentiment:+.2f}（{tone}）")
    else:
        parts.append("近期無足夠相關新聞")
    if chg_1d is not None and abs(chg_1d) >= 0.01:
        parts.append(f"24 小時賠率變動 {chg_1d * 100:+.1f}%")
    if chg_1w is not None and abs(chg_1w) >= 0.02:
        parts.append(f"一週變動 {chg_1w * 100:+.1f}%")
    return f"{candidate_zh}：" + "；".join(parts)


def candidate_signals(
    region_name: str,
    candidate_zh: str,
    candidate_en: str = "",
    market_id: str | int | None = None,
) -> dict[str, Any]:
    """給前端彈窗用的單一候選人外部訊號包。"""
    from . import db
    from .polls import candidate_matchup

    ctx = load_context()
    out: dict[str, Any] = {}

    try:
        out["attention"] = ctx.attention.get(candidate_zh) or ctx.attention.get(candidate_en) or {}
    except Exception:  # noqa: BLE001
        out["attention"] = {}

    if market_id is not None:
        out["depth"] = ctx.depth.get(str(market_id)) or {}

    try:
        item = candidate_matchup(region_name, candidate_zh)
        signal = (item or {}).get("signal")
        if signal:
            from .backtest import calibrated_prob

            raw = (signal.get("weighted") or {}).get(candidate_zh)
            out["poll"] = {
                "matchup": (item or {}).get("matchup") or signal.get("matchup"),
                "weighted": raw,
                "shares": signal.get("shares"),
                "leader": signal.get("leader"),
                "margin": signal.get("margin"),
                "win_prob": calibrated_prob(signal.get("margin")) if signal.get("leader") == candidate_zh else None,
                "date": (item or {}).get("effective_date"),
                "sources": (item or {}).get("poll_count"),
                "effective_samples": (item or {}).get("effective_samples"),
                "history": (item or {}).get("history") or [],
            }
            entry = _lookup(ctx.polls, region_name) or {}
            if entry.get("matchup") == out["poll"]["matchup"]:
                # 摘要層只保留代表性組合的歷史序列
                out["poll"]["history"] = entry.get("history") or []
    except Exception:  # noqa: BLE001
        pass

    # 模型輸出（最新一次預測）
    try:
        for row in db.fetch_latest_forecasts():
            if row["region_code"] and row["candidate_en"] == candidate_en:
                out["model"] = {
                    "market": row["pm_prob"],
                    "poll_prob": row["poll_prob"],
                    "fused": row["fused_prob"],
                    "rationale": row["rationale"],
                    "edge": row["fused_prob"] - row["pm_prob"] if row["pm_prob"] is not None else None,
                    "news_sentiment": row["news_sentiment"],
                    "region_sentiment": row["region_sentiment"],
                    "articles": row["articles"],
                    "components": json.loads(row["components"]) if row["components"] else {},
                }
                break
    except Exception:  # noqa: BLE001
        pass
    return out


def region_summary(forecast_rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """把逐候選人預測壓縮成各縣市摘要。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in forecast_rows:
        grouped.setdefault(row["region_code"], []).append(row)

    out: list[dict[str, Any]] = []
    for code, rows in grouped.items():
        rows = sorted(rows, key=lambda r: -r["fused_prob"])
        top = rows[0]
        runner = rows[1] if len(rows) > 1 else None
        pm_top = max(rows, key=lambda r: r["pm_prob"])
        components = top.get("components") or {}
        if isinstance(components, str):
            try:
                components = json.loads(components)
            except json.JSONDecodeError:
                components = {}
        margin = top["fused_prob"] - (runner["fused_prob"] if runner else 0.0)
        volatility = max((abs(r.get("chg_1w") or 0.0) for r in rows), default=0.0)
        out.append({
            "region_code": code,
            "region_name": top["region_name"],
            "leader_en": top["candidate_en"],
            "leader_zh": top["candidate_zh"],
            "fused_prob": top["fused_prob"],
            "pm_prob": top["pm_prob"],
            "poll_share": top.get("poll_share"),
            "poll_leader": top.get("poll_leader"),
            "poll_margin": top.get("poll_margin"),
            "poll_date": components.get("poll_date"),
            "poll_freshness": components.get("poll_freshness"),
            "runner_up_zh": runner["candidate_zh"] if runner else None,
            "runner_up_prob": runner["fused_prob"] if runner else None,
            "margin": round(margin, 6),
            "market_leader_zh": pm_top["candidate_zh"],
            "sentiment": top.get("region_sentiment") or 0.0,
            "articles": top.get("articles") or 0,
            "volatility_1w": round(volatility, 4),
            "tossup": margin <= TOSSUP_MARGIN,
            "sentiment_override": pm_top["candidate_en"] != top["candidate_en"],
            "poll_override": bool(
                top.get("poll_leader") and top.get("poll_leader") != top.get("candidate_zh")
            ),
            "rationale": top["rationale"],
        })
    out.sort(key=lambda r: (-r["fused_prob"], r["margin"]))
    return out
