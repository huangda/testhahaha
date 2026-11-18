"""新聞分析：LLM 優先，規則式引擎降級。輸出統一的結構化結果。"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable

from . import db, lexicon
from .config import Region, Settings
from .llm import LLMClient, LLMError
from .models import MarketSnapshot
from . import news as news_mod
from .news import detect_candidates, detect_regions
from .zhconv import to_simplified

SYSTEM_PROMPT = """你是一位专精台湾政治的资深选情分析师，熟悉 2026 年中华民国地方公职人员选举（九合一）。
你的任务是阅读一则新闻，输出可量化、可稽核的舆情分析。

务必遵守：
1. 仅根据新闻内容判断，不要补充新闻没有的信息，也不要预测谁会当选。
2. sentiment 代表「该新闻对整体选情氛围」的正负向，-1 极负面、0 中性、1 极正面。
3. 每位被提及的候选人，分别给出专属 sentiment（该则新闻对他的有利程度）。
4. impact 是这则新闻对选情的影响强度（0～1），例行的民调转述偏低，突发丑闻或提名定案偏高。
5. 只使用下列清单中的县市名称与候选人英文名，不要自创。
6. 所有文字字段（topics、summary）一律使用简体中文。
7. 只输出 JSON，不要任何说明文字或 markdown 围篱。"""

USER_TEMPLATE = """请分析以下新闻。

【可选县市】{regions}
【可选候选人（英文名 → 中文名）】{candidates}

【新闻标题】{title}
【新闻摘要】{summary}
【所属媒体】{source}

输出 JSON 结构：
{{
  "regions": ["新闻主要涉及的县市，最多 3 个；若为全岛性议题填 \\"全島\\""],
  "candidates": [{{"name": "候选人英文名", "sentiment": 0.0, "mentions": 1}}],
  "topics": ["议题标签，2-4 个，每组 4 字以内，简体中文"],
  "sentiment": 0.0,
  "impact": 0.0,
  "summary": "一句话（40 字内）摘要这则新闻对选情的重点，简体中文"
}}"""


def _client(settings: Settings) -> LLMClient | None:
    if not settings.llm_enabled or not settings.llm_api_key:
        return None
    return LLMClient(settings.llm_base_url, settings.llm_api_key, settings.llm_model, settings.llm_timeout)


def _candidate_pairs(settings: Settings, regions: list[Region], snapshots: dict[str, MarketSnapshot]) -> list[tuple[str, str]]:
    """挑出與這些縣市相關的候選人 (英文, 中文) 清單。"""
    codes = {r.code for r in regions} or {r.code for r in settings.regions}
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for code in codes:
        snap = snapshots.get(code)
        if not snap:
            continue
        for cand in snap.candidates:
            if cand.name_en not in seen:
                seen.add(cand.name_en)
                pairs.append((cand.name_en, cand.name_zh))
    selected_regions = {region.name for region in regions}
    if selected_regions:
        for item in settings.priority_local_candidates:
            if item.get("region") not in selected_regions:
                continue
            key = f"local:{item['name']}"
            if key not in seen:
                seen.add(key)
                pairs.append((key, item["name"]))
    if not pairs:
        pairs = list(settings.name_map.items())
    return pairs


def _normalise_llm(payload: dict[str, Any], settings: Settings, allowed: list[tuple[str, str]]) -> dict[str, Any]:
    allowed_map = {en: zh for en, zh in allowed}
    # 正規化表：別名與舊寫法都對回目前設定的標準名稱。
    # 這樣 config/regions.json 改名之後，資料庫裡的舊標籤
    # 與新聞裡的舊寫法仍然能正確歸屬。
    lookup: dict[str, str] = {}
    groups = list(settings.regions) + ([settings.nationwide] if settings.nationwide else [])
    for region in groups:
        for alias in [region.name, *region.aliases]:
            lookup[to_simplified(alias)] = region.name
        lookup[to_simplified(region.name)] = region.name
        lookup[region.name] = region.name
    regions: list[str] = []
    for raw in payload.get("regions") or []:
        hit = lookup.get(str(raw).strip())
        if hit and hit not in regions:
            regions.append(hit)
    regions = regions[:3]

    candidates: list[dict[str, Any]] = []
    for item in payload.get("candidates") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or ""
        if name not in allowed_map:
            continue
        try:
            sentiment = float(item.get("sentiment") or 0.0)
        except (TypeError, ValueError):
            sentiment = 0.0
        candidates.append(
            {
                "name": name,
                "name_zh": allowed_map[name],
                "mentions": int(item.get("mentions") or 1),
                "sentiment": max(-1.0, min(1.0, sentiment)),
            }
        )

    def clamp(value: Any, lo: float, hi: float) -> float:
        try:
            return max(lo, min(hi, float(value)))
        except (TypeError, ValueError):
            return 0.0

    topics = [str(t)[:12] for t in (payload.get("topics") or []) if str(t).strip()][:5]
    return {
        "regions": regions,
        "sentiment": clamp(payload.get("sentiment"), -1.0, 1.0),
        "impact": clamp(payload.get("impact"), 0.0, 1.0),
        "candidates": candidates,
        "topics": topics,
        "summary": str(payload.get("summary") or "")[:120],
    }


def analyze_news(
    settings: Settings,
    rows: Iterable[Any],
    snapshots: dict[str, MarketSnapshot],
    *,
    use_llm: bool = True,
    limit: int | None = None,
    verbose: bool = True,
) -> tuple[list[dict[str, Any]], str]:
    """分析新聞，回傳 (分析結果列, 使用的引擎名稱)。"""
    client = _client(settings) if use_llm else None
    engine = "llm" if client else "lexicon"
    model = settings.llm_model if client else "rule-based-zh-v1"

    rows = list(rows)
    if limit:
        rows = rows[:limit]

    results: list[dict[str, Any]] = []
    now = db.utcnow_iso()
    failures = 0
    candidate_index = news_mod.build_candidate_index(snapshots)

    def rule_analysis() -> dict[str, Any]:
        result = lexicon.analyze_item(title, summary, pairs)
        result["engine"] = "lexicon"
        result["model"] = "rule-based-zh-v1"
        return result

    for index, row in enumerate(rows, 1):
        title = row["title"]
        summary = row["summary"] or ""
        text = f"{title} {summary}"

        detected = detect_regions(text, settings, candidate_index)
        pairs = _candidate_pairs(settings, detected, snapshots)
        detected_names = [r.name for r in detected][:3]

        if client:
            try:
                payload = client.complete_json(
                    SYSTEM_PROMPT,
                    USER_TEMPLATE.format(
                        regions="、".join([r.name for r in settings.regions] + ([settings.nationwide.name] if settings.nationwide else [])),
                        candidates=json.dumps({en: zh for en, zh in pairs}, ensure_ascii=False),
                        title=title,
                        summary=summary[:1200] or "（無摘要）",
                        source=row["source_name"] or "未知",
                    ),
                )
                analysis = _normalise_llm(payload, settings, pairs)
                if not analysis["regions"]:
                    analysis["regions"] = detected_names or [settings.nationwide.name if settings.nationwide else "全島"]
            except (LLMError, RuntimeError) as exc:
                failures += 1
                if verbose and failures <= 3:
                    print(f"[analyze] LLM 失敗，改用本地语义引擎：{exc}")
                analysis = rule_analysis()
                analysis["regions"] = detected_names or [settings.nationwide.name if settings.nationwide else "全島"]
        else:
            analysis = rule_analysis()
            analysis["regions"] = detected_names or [settings.nationwide.name if settings.nationwide else "全島"]

        analysis.pop("_debug", None)
        analysis["news_id"] = row["id"]
        analysis["analyzed_at"] = now
        analysis.setdefault("engine", engine)
        analysis.setdefault("model", model)

        # 規則式偵測到的候選人若 LLM 漏掉，補上（避免召回率過低）
        known = {c["name"] for c in analysis["candidates"]}
        for name_en in detect_candidates(text, settings):
            if name_en not in known:
                name_zh = (
                    name_en.split(":", 1)[1]
                    if name_en.startswith("local:")
                    else settings.display_name(name_en)
                )
                analysis["candidates"].append(
                    {
                        "name": name_en,
                        "name_zh": name_zh,
                        "mentions": 1,
                        "sentiment": analysis["sentiment"],
                    }
                )

        results.append(analysis)
        if verbose and index % 20 == 0:
            print(f"[analyze] 已處理 {index}/{len(rows)} 則")

    if failures and verbose:
        print(f"[analyze] 共 {failures} 則回退到規則引擎")
    return results, model


def aggregate_regions(
    settings: Settings,
    news_rows: Iterable[Any],
    analyses: dict[str, dict[str, Any]],
    snapshots: dict[str, MarketSnapshot],
) -> list[dict[str, Any]]:
    """把逐篇分析聚合成各縣市輿情狀態。"""
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"scores": [], "pos": 0, "neu": 0, "neg": 0, "topics": Counter(), "weight": 0.0, "signals": 0}
    )
    name_to_code = {r.name: r.code for r in settings.regions}
    if settings.nationwide:
        name_to_code[settings.nationwide.name] = settings.nationwide.code

    for row in news_rows:
        analysis = analyses.get(row["id"])
        if not analysis:
            continue
        impact = float(analysis.get("impact") or 0.3)
        weight = 0.5 + impact
        sentiment = float(analysis.get("sentiment") or 0.0)
        for region_name in analysis.get("regions") or []:
            code = name_to_code.get(region_name)
            if not code:
                continue
            bucket = buckets[code]
            bucket["scores"].append((sentiment, weight))
            bucket["weight"] += weight
            if abs(sentiment) > 0.12:
                bucket["signals"] += 1
            if sentiment > 0.12:
                bucket["pos"] += 1
            elif sentiment < -0.12:
                bucket["neg"] += 1
            else:
                bucket["neu"] += 1
            for topic in analysis.get("topics") or []:
                bucket["topics"][topic] += 1

    rows_out: list[dict[str, Any]] = []
    all_regions = list(settings.regions) + ([settings.nationwide] if settings.nationwide else [])
    for region in all_regions:
        bucket = buckets.get(region.code)
        if not bucket:
            rows_out.append(
                {
                    "region_code": region.code,
                    "region_name": region.name,
                    "article_count": 0,
                    "avg_sentiment": 0.0,
                    "pos_count": 0,
                    "neu_count": 0,
                    "neg_count": 0,
                    "top_topics": [],
                    "momentum": 0.0,
                    "signal_count": 0,
                }
            )
            continue
        total_weight = bucket["weight"] or 1.0
        avg = sum(s * w for s, w in bucket["scores"]) / total_weight
        rows_out.append(
            {
                "region_code": region.code,
                "region_name": region.name,
                "article_count": len(bucket["scores"]),
                "avg_sentiment": round(avg, 4),
                "pos_count": bucket["pos"],
                "neu_count": bucket["neu"],
                "neg_count": bucket["neg"],
                "top_topics": [t for t, _ in bucket["topics"].most_common(5)],
                "momentum": round(bucket["weight"], 3),
                "signal_count": bucket["signals"],
            }
        )
    return rows_out


def candidate_sentiment_index(
    news_rows: Iterable[Any],
    analyses: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """彙整每位候選人的新聞提及量與情感均值。key = 候選人英文名。"""
    now = datetime.now().astimezone()

    def recency_weight(row: Any) -> float:
        try:
            raw = str(row["published_at"] or row["first_seen"] or "")
            published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=now.tzinfo)
            age_days = max(0.0, (now - published.astimezone(now.tzinfo)).total_seconds() / 86400)
            return 0.5 ** (age_days / 7.0)
        except (ValueError, TypeError, KeyError):
            return 1.0

    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "scores": [],
            "mentions": 0,
            "articles": 0,
            "positive": 0,
            "negative": 0,
            "effective_weight": 0.0,
        }
    )
    for row in news_rows:
        analysis = analyses.get(row["id"])
        if not analysis:
            continue
        weight = (0.5 + float(analysis.get("impact") or 0.3)) * recency_weight(row)
        for cand in analysis.get("candidates") or []:
            name = cand.get("name")
            if not name:
                continue
            entry = agg[name]
            entry["scores"].append((float(cand.get("sentiment") or 0.0), weight))
            entry["mentions"] += int(cand.get("mentions") or 1)
            entry["articles"] += 1
            entry["effective_weight"] += weight
            tone = float(cand.get("sentiment") or 0.0)
            if tone > 0.12:
                entry["positive"] += 1
            elif tone < -0.12:
                entry["negative"] += 1

    result: dict[str, dict[str, Any]] = {}
    for name, entry in agg.items():
        weight = sum(w for _, w in entry["scores"]) or 1.0
        result[name] = {
            "sentiment": round(sum(s * w for s, w in entry["scores"]) / weight, 4),
            "mentions": entry["mentions"],
            "articles": entry["articles"],
            "positive_articles": entry["positive"],
            "negative_articles": entry["negative"],
            "effective_weight": round(entry["effective_weight"], 4),
        }
    return result


def overall_verdict(region_states: Iterable[dict[str, Any]], snapshots: dict[str, MarketSnapshot]) -> dict[str, Any]:
    """產生全島層級的總結。"""
    states = {s["region_code"]: s for s in region_states}
    hot = sorted(
        [s for s in states.values() if s["region_code"] != "TW" and s["article_count"] > 0],
        key=lambda s: (-s["article_count"], s["avg_sentiment"]),
    )[:5]
    # 樣本門檻：至少 5 則新聞、且有 2 則以上帶有明確情感訊號，避免個位數樣本主導結論
    rankable = [s for s in states.values() if s["article_count"] >= 5 and s.get("signal_count", 0) >= 2]
    ranked_by_tone = sorted(rankable, key=lambda s: -s["avg_sentiment"])
    # 兩份名單互斥，避免同一縣市同時出現在「最正面」與「最負面」
    positive_count = min(5, max(1, len(ranked_by_tone) // 2)) if ranked_by_tone else 0
    negative_count = min(5, max(0, len(ranked_by_tone) - positive_count))
    most_positive = ranked_by_tone[:positive_count]
    most_negative = list(reversed(ranked_by_tone[len(ranked_by_tone) - negative_count :])) if negative_count else []

    tossups = []
    from .forecast import TOSSUP_MARGIN

    for code, snap in snapshots.items():
        if code == "TW" or len(snap.candidates) < 2:
            continue
        ordered = sorted(snap.candidates, key=lambda c: -(c.pm_prob or 0))
        if len(ordered) >= 2:
            gap = (ordered[0].pm_prob or 0) - (ordered[1].pm_prob or 0)
            if gap <= TOSSUP_MARGIN:
                tossups.append(
                    {
                        "region": snap.region_name,
                        "leader": ordered[0].name_zh,
                        "leader_prob": round(ordered[0].pm_prob or 0, 4),
                        "challenger": ordered[1].name_zh,
                        "challenger_prob": round(ordered[1].pm_prob or 0, 4),
                        "gap": round(gap, 4),
                    }
                )
    tossups.sort(key=lambda x: x["gap"])

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "hottest_regions": [{"region": s["region_name"], "articles": s["article_count"], "sentiment": s["avg_sentiment"]} for s in hot],
        "most_negative": [{"region": s["region_name"], "sentiment": s["avg_sentiment"], "articles": s["article_count"]} for s in most_negative],
        "most_positive": [{"region": s["region_name"], "sentiment": s["avg_sentiment"], "articles": s["article_count"]} for s in most_positive],
        "tossups": tossups,
    }
