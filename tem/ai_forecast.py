"""使用大模型综合市场、民调与新闻舆情，生成候选人胜率。"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .config import Settings
from .forecast import _market_profile, load_context, poll_channel
from .http import post_json
from .models import CandidateSnapshot, MarketSnapshot
from .paths import EXPORT_DIR
from .roster import load_roster
from .zhconv import to_simplified

SCHEMA_VERSION = 1
MAX_MARKET_EFFECT_PP = 35.0
MAX_POLL_EFFECT_PP = 20.0
MAX_POLL_PRIORITY_EFFECT_PP = 40.0
MAX_SENTIMENT_EFFECT_PP = 5.0
MAX_TOTAL_EFFECT_PP = 50.0
MAX_EVENT_ADJUSTMENT_PP = 5.0
MAX_AI_ANCHOR_DEVIATION_PP = 5.0
LLM_TIMEOUT = 240
LLM_MAX_TOKENS = 20000
_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _norm_name(value: Any) -> str:
    return to_simplified(str(value or "")).replace("臺", "台").strip()


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _roster_for_region(region_name: str) -> list[dict[str, Any]]:
    wanted = _norm_name(region_name)
    for name, entries in (load_roster().get("regions") or {}).items():
        if _norm_name(name) == wanted:
            return [dict(item) for item in entries or []]
    return []


def _official_snapshot(
    snapshot: MarketSnapshot,
    roster_entries: list[dict[str, Any]],
) -> tuple[MarketSnapshot, dict[str, CandidateSnapshot]]:
    """只保留正式候选人，并重新归一化市场概率。"""
    by_name = {_norm_name(item.get("name")): item for item in roster_entries}
    candidates: list[CandidateSnapshot] = []
    matched: dict[str, CandidateSnapshot] = {}
    for candidate in snapshot.candidates:
        entry = by_name.get(_norm_name(candidate.name_zh))
        if not entry:
            continue
        copied = CandidateSnapshot(**candidate.as_dict())
        candidates.append(copied)
        matched[_norm_name(entry.get("name"))] = copied
    total = sum(max(0.0, item.yes_price or 0.0) for item in candidates)
    for candidate in candidates:
        candidate.pm_prob = (
            max(0.0, candidate.yes_price or 0.0) / total if total > 0 else 0.0
        )
    return (
        MarketSnapshot(
            region_code=snapshot.region_code,
            region_name=snapshot.region_name,
            event_slug=snapshot.event_slug,
            candidates=candidates,
        ),
        matched,
    )


def _sentiment_index(
    candidate_sentiment: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for key, value in candidate_sentiment.items():
        if not isinstance(value, dict):
            continue
        index[str(key)] = value
        for name_key in ("name_zh", "name", "candidate_zh"):
            if value.get(name_key):
                index[_norm_name(value[name_key])] = value
    return index


def _sentiment_for_candidate(
    entry: dict[str, Any],
    market_candidate: CandidateSnapshot | None,
    signals: dict[str, dict[str, Any]],
    region_sentiment: float,
    article_count: int,
) -> tuple[float, int]:
    signal = None
    for key in (
        market_candidate.name_en if market_candidate else "",
        entry.get("en"),
        entry.get("name"),
    ):
        if key and _norm_name(key) in signals:
            signal = signals[_norm_name(key)]
            break
    if signal and int(signal.get("articles") or 0) > 0:
        articles = int(signal.get("articles") or 0)
        candidate_weight = articles / (articles + 6.0)
        sentiment = (
            _float(signal.get("sentiment"))
            * candidate_weight
            + region_sentiment * (1 - candidate_weight)
        )
        return sentiment, articles
    region_weight = article_count / (article_count + 6.0) if article_count else 0.0
    return region_sentiment * region_weight, 0


def _build_input(
    settings: Settings,
    snapshots: dict[str, MarketSnapshot],
    candidate_sentiment: dict[str, dict[str, Any]],
    region_states: Iterable[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    ctx = load_context()
    states = {str(item.get("region_code")): item for item in region_states}
    sentiment_signals = _sentiment_index(candidate_sentiment)
    regions: list[dict[str, Any]] = []
    sources: dict[str, dict[str, Any]] = {}
    for code, original in snapshots.items():
        if code == "TW":
            continue
        entries = _roster_for_region(original.region_name)
        if not entries:
            continue
        official_snapshot, matched_market = _official_snapshot(original, entries)
        state = states.get(code, {})
        region_sentiment = _float(state.get("avg_sentiment"))
        region_articles = int(state.get("article_count") or 0)
        official_names = [str(item.get("name") or "") for item in entries]
        market_probs = {
            name: probs
            for name, probs in (
                (candidate.name_zh, candidate.pm_prob or 0.0)
                for candidate in official_snapshot.candidates
            )
        }
        poll = poll_channel(
            original.region_name,
            official_names,
            ctx,
            market_probs=market_probs if any(market_probs.values()) else None,
        )
        profile = _market_profile(official_snapshot, ctx)
        poll_sources = int((poll or {}).get("sources") or 0)
        poll_freshness = float((poll or {}).get("freshness") or 0.0)
        poll_samples = float((poll or {}).get("effective_samples") or 0.0)
        poll_age_days = None
        if (poll or {}).get("date"):
            try:
                poll_age_days = (
                    datetime.now(timezone.utc).date()
                    - datetime.strptime(str(poll["date"]), "%Y-%m-%d").date()
                ).days
            except ValueError:
                poll_age_days = None
        poll_quality = (
            0.50 * min(1.0, poll_freshness)
            + 0.30 * min(1.0, poll_sources / 4.0)
            + 0.20 * min(1.0, poll_samples / 3.0)
        )
        fresh_poll = poll_age_days is not None and poll_age_days <= 14
        many_polls = poll_sources >= 4 or poll_samples >= 1.0
        if poll and fresh_poll and many_polls:
            reference_priority = "民调优先"
            reference_reason = "最新民调在14天内，民调份数或有效样本较多，民调应成为主依据"
        elif poll and fresh_poll and poll_sources >= 2 and profile["tier"] in {"low", "medium"}:
            reference_priority = "民调优先"
            reference_reason = "最新民调较新且市场盘口或成交质量较弱，民调应成为主依据"
        elif poll and (fresh_poll or poll_freshness >= 0.35) and poll_sources >= 2:
            reference_priority = "市场与民调并重"
            reference_reason = "市场与民调均有一定质量，需要交叉校正"
        else:
            reference_priority = "市场优先"
            reference_reason = "民调缺失、偏旧或覆盖不足，市场仍为主要参考"
        candidate_rows: list[dict[str, Any]] = []
        source_candidates: dict[str, dict[str, Any]] = {}
        for entry in entries:
            name = to_simplified(str(entry.get("name") or ""))
            market_candidate = matched_market.get(_norm_name(name))
            news_sentiment, articles = _sentiment_for_candidate(
                entry,
                market_candidate,
                sentiment_signals,
                region_sentiment,
                region_articles,
            )
            poll_probability = (poll or {}).get("shares", {}).get(str(entry.get("name")))
            row = {
                "号次": entry.get("number"),
                "姓名": name,
                "政党": to_simplified(str(entry.get("party") or "")),
                "经历": to_simplified(str(entry.get("experience") or "")),
                "市场概率": round((market_candidate.pm_prob or 0.0) * 100, 2)
                if market_candidate else 0.0,
                "市场24小时变动": round((market_candidate.chg_1d or 0.0) * 100, 2)
                if market_candidate and market_candidate.chg_1d is not None else None,
                "市场一周变动": round((market_candidate.chg_1w or 0.0) * 100, 2)
                if market_candidate and market_candidate.chg_1w is not None else None,
                "民调概率": round(float(poll_probability) * 100, 2)
                if poll_probability is not None else None,
                "民调加权支持度": (poll or {}).get("raw", {}).get(str(entry.get("name"))),
                "新闻情感": round(news_sentiment, 4),
                "相关新闻数": articles,
            }
            candidate_rows.append(row)
            source_candidates[_norm_name(name)] = {
                "entry": entry,
                "market_candidate": market_candidate,
                "news_sentiment": news_sentiment,
                "articles": articles,
                "poll_probability": poll_probability,
            }
        regions.append({
            "region_code": code,
            "region_name": to_simplified(original.region_name),
            "市场层级": profile["tier"],
            "市场总成交": profile["volume"],
            "市场流动性": profile["liquidity"],
            "盘口深度": profile["depth_usdc"],
            "买卖价差": profile["spread"],
            "民调领先者": to_simplified(str((poll or {}).get("leader") or "")) or None,
            "民调领先幅度": (poll or {}).get("margin"),
            "民调日期": (poll or {}).get("date"),
            "民调份数": poll_sources,
            "最新民调距今天数": poll_age_days,
            "民调新鲜度": poll_freshness,
            "有效民调样本": poll_samples,
            "民调质量分": round(poll_quality, 4),
            "民调阵营构成": (poll or {}).get("bias") or {},
            "市场可靠度": profile["reliability"],
            "综合参考优先级": reference_priority,
            "参考优先级原因": reference_reason,
            "近7天县市新闻数": region_articles,
            "近7天县市平均情感": round(region_sentiment, 4),
            "热门议题": (state.get("top_topics") or [])[:5],
            "正式候选人": candidate_rows,
        })
        sources[code] = {
            "region": original,
            "snapshot": official_snapshot,
            "poll": poll,
            "profile": profile,
            "region_sentiment": region_sentiment,
            "region_articles": region_articles,
            "official_candidates": source_candidates,
            "reference_priority": reference_priority,
            "reference_reason": reference_reason,
        }
    payload = {
        "预测条件": {
            "市场调整上限_百分点": MAX_MARKET_EFFECT_PP,
            "民调调整上限_百分点": MAX_POLL_EFFECT_PP,
            "民调优先时民调调整上限_百分点": MAX_POLL_PRIORITY_EFFECT_PP,
            "舆情调整上限_百分点": MAX_SENTIMENT_EFFECT_PP,
            "每候选人合计调整上限_百分点": MAX_TOTAL_EFFECT_PP,
            "说明": "调整分相对同选区等概率基准；同一县市全部正式候选人的最终概率必须合计100%。",
        },
        "县市": regions,
    }
    return payload, sources


def _quality_issues(payload: dict[str, Any], result: dict[str, Any]) -> list[str]:
    ai_regions = {
        str(item.get("region_code") or ""): item
        for item in result.get("regions") or []
        if isinstance(item, dict)
    }
    issues: list[str] = []
    for region in payload.get("县市") or []:
        code = str(region.get("region_code") or "")
        ai_region = ai_regions.get(code)
        if not ai_region:
            continue
        expected = region.get("正式候选人") or []
        by_name = {
            _norm_name(item.get("name")): item
            for item in ai_region.get("candidates") or []
            if isinstance(item, dict)
        }
        market_ranked = sorted(
            expected, key=lambda item: -_float(item.get("市场概率"))
        )
        ai_values = []
        for item in expected:
            ai_item = by_name.get(_norm_name(item.get("姓名")))
            if ai_item:
                ai_values.append(_float(ai_item.get("probability")))
        if len(market_ranked) < 2 or len(ai_values) < 2:
            continue
        market_gap = (
            _float(market_ranked[0].get("市场概率"))
            - _float(market_ranked[1].get("市场概率"))
        )
        ai_gap = max(ai_values) - sorted(ai_values, reverse=True)[1]
        if (
            region.get("综合参考优先级") != "民调优先"
            and market_gap >= 15.0
            and ai_gap < market_gap * 0.20
        ):
            issues.append(
                f"{code}：市场前二差 {market_gap:.1f} 个百分点，"
                f"AI前二仅差 {ai_gap:.1f} 个百分点"
            )
    return issues


def _merge_usage(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    out = dict(first)
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        out[key] = int(first.get(key) or 0) + int(second.get(key) or 0)
    return out


def _request_ai(
    settings: Settings,
    payload: dict[str, Any],
    *,
    correction: str = "",
    attempt: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    system = f"""
你是台湾地方选举概率预测引擎。只能使用输入数据，不得联网或引入外部事实。
你需要彻底综合市场赔率、加权民调、近7天新闻舆情，并为每位正式候选人分别给出三个调整分。

规则：
1. 调整分单位为百分点，是相对“同选区等概率基准”的上调或下调。
2. 市场调整范围为 -{MAX_MARKET_EFFECT_PP:g} 到 +{MAX_MARKET_EFFECT_PP:g}。
3. 民调调整范围通常为 -{MAX_POLL_EFFECT_PP:g} 到 +{MAX_POLL_EFFECT_PP:g}；
   县市“综合参考优先级”为“民调优先”时，可放宽到
   -{MAX_POLL_PRIORITY_EFFECT_PP:g} 到 +{MAX_POLL_PRIORITY_EFFECT_PP:g}。
4. 舆情调整范围为 -{MAX_SENTIMENT_EFFECT_PP:g} 到 +{MAX_SENTIMENT_EFFECT_PP:g}。
5. 单人三项调整合计范围为 -{MAX_TOTAL_EFFECT_PP:g} 到 +{MAX_TOTAL_EFFECT_PP:g}。
6. 先读取县市级的“综合参考优先级”：
   - “民调优先”：必须把新鲜、覆盖较多的民调作为主要依据，民调对最终胜率的影响应大于市场，
     尤其不能因为小数盘口给出极端赔率就压过民调；
   - “市场与民调并重”：两项交叉校正，哪一项更新、更完整、与舆情更一致，哪项作用更大；
   - “市场优先”：民调缺失、偏旧或覆盖不足时，市场仍是主锚。
7. 市场成交、流动性、盘口深度和价差较弱时，降低市场调整的可信度。
   当“民调优先”且盘口小时，即使市场赔率很极端，也应按民调方向重新校正，不得机械照搬市场概率。
8. 民调质量看“民调份数、民调新鲜度、有效民调样本、民调质量分”；单份、过旧或党内初选民调不能成为主依据。
9. 新闻舆情只作小幅修正，不能因新闻数量多就大幅改变概率。
10. “调整分相对等概率基准”不等于“每人都只调整几个百分点”。市场概率90%对10%时，
   强领先者应有接近上限的明显上调，弱势候选人应明显下调，不能把结果压成接近50%对50%。
11. 例如两人市场为90%和10%、高市场质量且无民调时，领先者最终概率应在85%左右，
   不应是51%；市场调整约+35、竞争者约-35，其余调整接近0。
12. 先看等概率基准。若三人选区基准为33.3%，候选人市场概率为55%，胜率必须体现这种明显领先，
    不能只给出34%左右的平均化结果。
13. 当市场与民调方向冲突时，严格按“综合参考优先级”决定主依据；舆情只做小幅校正。
14. 市场与民调缺失时不得虚构，只能根据其他已给数据谨慎判断。
15. 每个县市必须覆盖全部正式候选人，不得遗漏、增加或改名。
16. 同一县市所有候选人的最终 probability 合计必须为100.0。
17. 不要给出无法由输入支持的99%以上确定性。

summary 用普通书面中文说明该县市的选情和判断依据，点明本次是市场还是民调起主要作用及原因，
避免“等概率基准、百分点、上修、下修、置信度”等技术词，也不要写成口语。
应让非专业读者看懂市场、民调和新闻舆情分别起了什么作用。

只输出JSON，不要Markdown：
{{"regions":[{{"region_code":"TPE","confidence":"高|中|低","uncertainty_pp":8,"summary":"不超过100字",
"candidates":[{{"name":"姓名","probability":数字}}]}}]}}
""".strip()
    if correction:
        system += (
            "\n\n上一次结果未通过质量检查："
            + correction
            + "。请重新输出完整JSON，保留全部县市和候选人；"
            "市场优先的县市，市场领先明显时必须保持相应差距；"
            "民调优先的县市，新鲜且覆盖较多的民调必须继续主导，不能改回机械照搬市场。"
        )
    request_payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
        ],
        "temperature": 0.1,
        "max_tokens": LLM_MAX_TOKENS,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    response = post_json(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        request_payload,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=LLM_TIMEOUT,
        retries=2,
    )
    content = str(response["choices"][0]["message"]["content"] or "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        match = _JSON_BLOCK.search(content)
        if not match:
            raise RuntimeError("AI 未返回可解析的 JSON") from exc
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise RuntimeError("AI 返回格式不是对象")
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (EXPORT_DIR / f"ai-forecast-raw-{stamp}-{attempt}.json").write_text(
        content, encoding="utf-8"
    )
    (EXPORT_DIR / "ai-forecast-usage.json").write_text(
        json.dumps({
            "model": response.get("model") or settings.llm_model,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "usage": usage,
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    issues = _quality_issues(payload, parsed)
    if issues:
        if attempt == 0:
            retry_result, retry_usage = _request_ai(
                settings,
                payload,
                correction="；".join(issues[:10]),
                attempt=1,
            )
            return retry_result, _merge_usage(usage, retry_usage)
        raise RuntimeError(
            "AI结果在纠错重试后仍未通过贡献一致性检查："
            + "；".join(issues[:10])
        )
    return parsed, usage


def _probability_vector(
    region: dict[str, Any],
    expected: list[dict[str, Any]],
) -> dict[str, float]:
    raw_candidates = region.get("candidates") or []
    by_name = {
        _norm_name(item.get("name")): item
        for item in raw_candidates
        if isinstance(item, dict)
    }
    expected_names = [_norm_name(item.get("姓名")) for item in expected]
    missing = [name for name in expected_names if name not in by_name]
    unknown = [name for name in by_name if name not in expected_names]
    if missing or unknown:
        raise RuntimeError(
            f"{region.get('region_code')} 候选人校验失败；缺少={missing}；多出={unknown}"
        )
    out: dict[str, float] = {}
    for name in expected_names:
        item = by_name[name]
        out[name] = _clamp(_float(item.get("probability")), 0.0, 100.0)
    return out


def _request_effects(
    settings: Settings,
    adjustment_payload: dict[str, Any],
    *,
    correction: str = "",
    attempt: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    system = f"""
你是预测结果归因器。输入已经给出每位候选人的目标调整百分点 target_delta。
请把 target_delta 拆成市场 market_effect、民调 poll_effect、舆情 sentiment_effect 三项。

强制规则：
1. 先遵守县市级的 reference_priority：
   - “民调优先”时，对同时有民调概率的候选人，poll_effect 的绝对幅度必须大于 market_effect；
   - “市场与民调并重”时，两项按证据质量分配；
   - “市场优先”时才以 market_effect 为主。
2. market_effect + poll_effect + sentiment_effect 必须精确等于 target_delta，误差不得超过0.1。
3. market_effect 限制在 -{MAX_MARKET_EFFECT_PP:g} 到 +{MAX_MARKET_EFFECT_PP:g}。
4. poll_effect 通常限制在 -{MAX_POLL_EFFECT_PP:g} 到 +{MAX_POLL_EFFECT_PP:g}；
   “民调优先”时限制在 -{MAX_POLL_PRIORITY_EFFECT_PP:g} 到 +{MAX_POLL_PRIORITY_EFFECT_PP:g}。
5. sentiment_effect 限制在 -{MAX_SENTIMENT_EFFECT_PP:g} 到 +{MAX_SENTIMENT_EFFECT_PP:g}。
6. 市场概率高于等概率基准时，市场贡献通常为正；低于基准时通常为负。
7. 民调贡献表示民调相对等概率基准的调整；民调优先时它应成为最大贡献项，舆情贡献只能小幅影响。
8. 覆盖全部县市和全部正式候选人，不得遗漏、增加或改名。
9. 只能使用输入数据，不得联网或引入外部事实。

只输出JSON：
{{"regions":[{{"region_code":"TPE","candidates":[
{{"name":"姓名","market_effect":数字,"poll_effect":数字,"sentiment_effect":数字}}
]}}]}}
""".strip()
    if correction:
        system += "\n\n上一次输出未通过校验：" + correction + "。请重新输出完整JSON。"
    request_payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    adjustment_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": 0.0,
        "max_tokens": 10000,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    response = post_json(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        request_payload,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=LLM_TIMEOUT,
        retries=2,
    )
    content = str(response["choices"][0]["message"]["content"] or "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        match = _JSON_BLOCK.search(content)
        if not match:
            raise RuntimeError("AI归因未返回可解析的JSON") from exc
        parsed = json.loads(match.group(0))
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    (EXPORT_DIR / f"ai-forecast-effects-raw-{stamp}-{attempt}.json").write_text(
        content, encoding="utf-8"
    )
    issues: list[str] = []
    region_map = {
        str(item.get("region_code") or ""): item
        for item in parsed.get("regions") or []
        if isinstance(item, dict)
    }

    # 民调优先时，最终概率已按民调主锚生成；这里确保贡献拆分也体现该优先级。
    adjustment_map = {
        str(item.get("region_code") or ""): item
        for item in adjustment_payload.get("regions") or []
        if isinstance(item, dict)
    }
    for code, specification in adjustment_map.items():
        if specification.get("reference_priority") != "民调优先":
            continue
        actual_region = region_map.get(code) or {}
        actual_candidates = {
            _norm_name(item.get("name")): item
            for item in actual_region.get("candidates") or []
            if isinstance(item, dict)
        }
        for candidate in specification.get("candidates") or []:
            if candidate.get("poll_probability") is None:
                continue
            item = actual_candidates.get(_norm_name(candidate.get("name")))
            if not item:
                continue
            market = _float(item.get("market_effect"))
            poll = _float(item.get("poll_effect"))
            if abs(poll) + 0.01 >= abs(market):
                continue
            target = _float(candidate.get("target_delta"))
            sentiment = _float(item.get("sentiment_effect"))
            residual = target - sentiment
            market = round(residual * 0.35, 4)
            poll = round(residual - market, 4)
            item["market_effect"] = market
            item["poll_effect"] = poll

    for region in adjustment_payload.get("regions") or []:
        code = str(region.get("region_code") or "")
        actual_region = region_map.get(code)
        if not actual_region:
            issues.append(f"{code} 缺少县市")
            continue
        actual = {
            _norm_name(item.get("name")): item
            for item in actual_region.get("candidates") or []
            if isinstance(item, dict)
        }
        expected_names = {
            _norm_name(item.get("name"))
            for item in region.get("candidates") or []
        }
        if set(actual) != expected_names:
            issues.append(f"{code} 候选人不一致")
            continue
        for candidate in region.get("candidates") or []:
            name = _norm_name(candidate.get("name"))
            item = actual[name]
            market = _float(item.get("market_effect"))
            poll = _float(item.get("poll_effect"))
            sentiment = _float(item.get("sentiment_effect"))
            target = _float(candidate.get("target_delta"))
            if abs(market) > MAX_MARKET_EFFECT_PP + 0.01:
                issues.append(f"{code}/{name} 市场贡献超限")
            poll_limit = (
                MAX_POLL_PRIORITY_EFFECT_PP
                if region.get("reference_priority") == "民调优先"
                else MAX_POLL_EFFECT_PP
            )
            if abs(poll) > poll_limit + 0.01:
                issues.append(f"{code}/{name} 民调贡献超限")
            if abs(sentiment) > MAX_SENTIMENT_EFFECT_PP + 0.01:
                issues.append(f"{code}/{name} 舆情贡献超限")
            if (
                region.get("reference_priority") == "民调优先"
                and candidate.get("poll_probability") is not None
                and abs(poll) + 0.5 < abs(market)
            ):
                issues.append(f"{code}/{name} 民调优先但市场贡献仍大于民调贡献")
            if abs(market + poll + sentiment - target) > 0.15:
                issues.append(
                    f"{code}/{name} 三项合计 {market + poll + sentiment:.1f}，"
                    f"目标 {target:.1f}"
                )
    if issues:
        if attempt == 0:
            retry, retry_usage = _request_effects(
                settings,
                adjustment_payload,
                correction="；".join(issues[:12]),
                attempt=1,
            )
            return retry, _merge_usage(usage, retry_usage)
        raise RuntimeError("AI贡献归因未通过校验：" + "；".join(issues[:10]))
    return parsed, usage


def _bounded_zero_sum_deltas(values: list[float], cap: float) -> list[float]:
    """把概率调整投影到合计为0且每人绝对值不超过cap的范围。"""
    out = [_clamp(value, -cap, cap) for value in values]
    for _ in range(50):
        residual = -sum(out)
        if abs(residual) < 1e-10:
            break
        if residual > 0:
            active = [index for index, value in enumerate(out) if value < cap - 1e-10]
        else:
            active = [index for index, value in enumerate(out) if value > -cap + 1e-10]
        if not active:
            break
        share = residual / len(active)
        changed = False
        for index in active:
            before = out[index]
            out[index] = _clamp(before + share, -cap, cap)
            changed = changed or abs(out[index] - before) > 1e-12
        if not changed:
            break
    return out


def _one_decimal_probabilities(values: list[float]) -> list[float]:
    """把百分比分配为一位小数，且强制精确合计100.0。"""
    targets = [int(max(0.0, value) * 10 + 1e-9) for value in values]
    remainder = 1000 - sum(targets)
    if remainder > 0:
        order = sorted(
            range(len(values)),
            key=lambda index: (max(0.0, values[index]) * 10 - targets[index]),
            reverse=True,
        )
        for index in order[:remainder]:
            targets[index] += 1
    elif remainder < 0:
        order = sorted(range(len(targets)), key=lambda index: targets[index], reverse=True)
        for index in order[:abs(remainder)]:
            if targets[index] > 0:
                targets[index] -= 1
    return [unit / 10 for unit in targets]


def _ai_fuse_legacy(
    settings: Settings,
    snapshots: dict[str, MarketSnapshot],
    candidate_sentiment: dict[str, dict[str, Any]],
    region_states: Iterable[dict[str, Any]],
    *,
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """由大模型生成调整分；程序只执行边界检查与同选区归一化。"""
    if not settings.llm_enabled or not settings.llm_api_key:
        raise RuntimeError("AI 综合胜率需要启用大模型并配置 API Key")
    payload, sources = _build_input(
        settings, snapshots, candidate_sentiment, region_states
    )
    if progress_callback:
        progress_callback(0.10, "准备AI预测数据")
    result, usage = _request_ai(settings, payload)
    if progress_callback:
        progress_callback(0.58, "AI胜率已生成")
    ai_regions = {
        str(item.get("region_code") or ""): item
        for item in result.get("regions") or []
        if isinstance(item, dict)
    }
    expected_codes = set(sources)
    missing_regions = sorted(expected_codes - set(ai_regions))
    unknown_regions = sorted(set(ai_regions) - expected_codes)
    if missing_regions or unknown_regions:
        raise RuntimeError(
            f"AI 县市校验失败；缺少={missing_regions}；多出={unknown_regions}"
        )
    targets: dict[str, list[tuple[dict[str, Any], float, float]]] = {}
    adjustment_regions: list[dict[str, Any]] = []
    for code, source in sources.items():
        payload_region = next(
            region for region in payload["县市"] if region["region_code"] == code
        )
        expected = list(payload_region["正式候选人"])
        probabilities = _probability_vector(ai_regions[code], expected)
        neutral = 100.0 / max(1, len(expected))
        raw_scores = [
            _clamp(
                probabilities[_norm_name(item["姓名"])],
                max(0.05, neutral - MAX_TOTAL_EFFECT_PP),
                min(100.0, neutral + MAX_TOTAL_EFFECT_PP),
            )
            for item in expected
        ]
        raw_total = sum(raw_scores) or 1.0
        normalized_scores = [score / raw_total * 100.0 for score in raw_scores]
        bounded_deltas = _bounded_zero_sum_deltas(
            [score - neutral for score in normalized_scores],
            MAX_TOTAL_EFFECT_PP,
        )
        final_probs = [neutral + delta for delta in bounded_deltas]
        targets[code] = [
            (item, final_probs[index], neutral)
            for index, item in enumerate(expected)
        ]
        adjustment_regions.append({
            "region_code": code,
            "region_name": payload_region["region_name"],
            "reference_priority": payload_region.get("综合参考优先级"),
            "reference_reason": payload_region.get("参考优先级原因"),
            "poll_effect_limit_pp": (
                MAX_POLL_PRIORITY_EFFECT_PP
                if payload_region.get("综合参考优先级") == "民调优先"
                else MAX_POLL_EFFECT_PP
            ),
            "candidates": [
                {
                    "name": item["姓名"],
                    "target_delta": round(final_probs[index] - neutral, 4),
                    "market_probability": item.get("市场概率"),
                    "poll_probability": item.get("民调概率"),
                    "news_sentiment": item.get("新闻情感"),
                    "articles": item.get("相关新闻数"),
                }
                for index, item in enumerate(expected)
            ],
        })
    effects_result, effect_usage = _request_effects(
        settings, {"regions": adjustment_regions}
    )
    if progress_callback:
        progress_callback(0.90, "AI贡献拆分完成")
    usage = _merge_usage(usage, effect_usage)
    effect_regions = {
        str(item.get("region_code") or ""): item
        for item in effects_result.get("regions") or []
        if isinstance(item, dict)
    }
    rows: list[dict[str, Any]] = []
    validation: list[dict[str, Any]] = []
    for code, source in sources.items():
        ai_region = ai_regions[code]
        original: MarketSnapshot = source["region"]
        profile = source["profile"]
        poll = source["poll"]
        region_sentiment = source["region_sentiment"]
        expected = [item for item, _probability, _neutral in targets[code]]
        normalized = list(targets[code])
        neutral = normalized[0][2] if normalized else 50.0
        market_ranked = sorted(
            expected, key=lambda item: -_float(item.get("市场概率"))
        )
        final_ranked = sorted(normalized, key=lambda item: -item[1])
        market_gap = (
            _float(market_ranked[0].get("市场概率"))
            - _float(market_ranked[1].get("市场概率"))
            if len(market_ranked) > 1 else 0.0
        )
        final_gap = (
            final_ranked[0][1] - final_ranked[1][1]
            if len(final_ranked) > 1 else 0.0
        )
        if (
            source.get("reference_priority") != "民调优先"
            and market_gap >= 15.0
            and final_gap < market_gap * 0.20
        ):
            raise RuntimeError(
                f"{code} AI概率过度平均化：市场前二差 {market_gap:.1f} 个百分点，"
                f"AI前二仅差 {final_gap:.1f} 个百分点"
            )
        effect_region = effect_regions[code]
        effect_by_name = {
            _norm_name(item.get("name")): {
                "market": _float(item.get("market_effect")),
                "poll": _float(item.get("poll_effect")),
                "sentiment": _float(item.get("sentiment_effect")),
            }
            for item in effect_region.get("candidates") or []
            if isinstance(item, dict)
        }
        applied_rows: list[tuple[dict[str, Any], dict[str, float], float, dict[str, float]]] = []
        for item, probability, _candidate_neutral in normalized:
            applied = effect_by_name[_norm_name(item["姓名"])]
            applied_rows.append((item, applied, probability, applied))
        applied_rows.sort(key=lambda item: -item[2])
        leader_name = _norm_name(applied_rows[0][0]["姓名"]) if applied_rows else ""
        for index, (item, raw_effect, probability, applied) in enumerate(applied_rows):
            name = _norm_name(item["姓名"])
            source_candidate = source["official_candidates"][name]
            entry = source_candidate["entry"]
            market_candidate: CandidateSnapshot | None = source_candidate["market_candidate"]
            candidate_en = (
                (market_candidate.name_en if market_candidate else "")
                or str(entry.get("en") or "")
                or str(entry.get("name") or name)
            )
            pm_prob = market_candidate.pm_prob if market_candidate else 0.0
            summary = to_simplified(str(ai_region.get("summary") or ""))
            priority = source.get("reference_priority")
            basis_text = (
                "本次新鲜民调较多，以民调为主要依据，市场用于校正"
                if priority == "民调优先"
                else (
                    "本次市场和民调交叉校正"
                    if priority == "市场与民调并重"
                    else "本次以市场赔率为主要依据，再用民调校正"
                )
            )
            rationale = (
                f"综合胜率为 {probability:.1f}%。{basis_text}，近期新闻舆情只作小幅参考。"
                + (f" {summary}" if summary else "")
            )
            components = {
                "calculation_engine": "llm",
                "ai_model": settings.llm_model,
                "ai_confidence": str(ai_region.get("confidence") or "低"),
                "ai_uncertainty_pp": ai_region.get("uncertainty_pp"),
                "ai_region_summary": summary,
                "reference_priority": source.get("reference_priority"),
                "reference_reason": source.get("reference_reason"),
                "ai_neutral_probability": round(neutral / 100.0, 6),
                "ai_market_effect_pp_raw": round(raw_effect["market"], 4),
                "ai_poll_effect_pp_raw": round(raw_effect["poll"], 4),
                "ai_sentiment_effect_pp_raw": round(raw_effect["sentiment"], 4),
                "ai_market_effect_pp": round(applied["market"], 4),
                "ai_poll_effect_pp": round(applied["poll"], 4),
                "ai_sentiment_effect_pp": round(applied["sentiment"], 4),
                "ai_total_effect_pp": round(
                    applied["market"] + applied["poll"] + applied["sentiment"], 4
                ),
                "ai_effect_limits_pp": {
                    "market": MAX_MARKET_EFFECT_PP,
                    "poll": (
                        MAX_POLL_PRIORITY_EFFECT_PP
                        if source.get("reference_priority") == "民调优先"
                        else MAX_POLL_EFFECT_PP
                    ),
                    "poll_priority": MAX_POLL_PRIORITY_EFFECT_PP,
                    "sentiment": MAX_SENTIMENT_EFFECT_PP,
                    "total": MAX_TOTAL_EFFECT_PP,
                },
                "ai_usage": usage,
                "market_tier": profile["tier"],
                "market_reliability": profile["reliability"],
                "market_volume": profile["volume"],
                "market_liquidity": profile["liquidity"],
                "market_depth_usdc": profile["depth_usdc"],
                "market_spread": profile["spread"],
                "poll_count": (poll or {}).get("sources"),
                "poll_effective_samples": (poll or {}).get("effective_samples"),
                "poll_calibrated": (poll or {}).get("calibrated"),
                "poll_date": (poll or {}).get("date"),
                "poll_freshness": (poll or {}).get("freshness"),
                "poll_strength": None,
                "market_weight": None,
                "poll_weight": None,
                "sentiment_weight": None,
                "article_count": source_candidate["articles"],
            }
            rows.append({
                "region_code": code,
                "region_name": original.region_name,
                "candidate_en": candidate_en,
                "candidate_zh": name,
                "pm_prob": round(pm_prob or 0.0, 6),
                "pm_raw": market_candidate.yes_price if market_candidate else None,
                "poll_prob": round(source_candidate["poll_probability"], 6)
                if source_candidate["poll_probability"] is not None else None,
                "poll_share": (poll or {}).get("raw", {}).get(str(entry.get("name"))),
                "poll_margin": (poll or {}).get("margin"),
                "news_sentiment": round(source_candidate["news_sentiment"], 4),
                "fused_prob": round(probability / 100.0, 6),
                "rationale": rationale,
                "edge": round(probability / 100.0 - (pm_prob or 0.0), 6),
                "articles": source_candidate["articles"],
                "region_sentiment": region_sentiment,
                "chg_1d": market_candidate.chg_1d if market_candidate else None,
                "chg_1w": market_candidate.chg_1w if market_candidate else None,
                "volume": market_candidate.volume if market_candidate else None,
                "poll_leader": (poll or {}).get("leader"),
                "components": components,
                "leader": name == leader_name,
            })
        validation.append({
            "region_code": code,
            "candidates": len(applied_rows),
            "sum_probability": round(sum(item[2] for item in applied_rows), 6),
            "market_top_gap_pp": round(market_gap, 4),
            "ai_top_gap_pp": round(final_gap, 4),
            "max_adjustment": round(max(
                (
                    abs(item[3]["market"] + item[3]["poll"] + item[3]["sentiment"])
                    for item in applied_rows
                ),
                default=0.0,
            ), 4),
        })
    if progress_callback:
        progress_callback(1.0, "AI胜率校验完成")
    return rows, {
        "schema_version": SCHEMA_VERSION,
        "engine": "llm",
        "model": settings.llm_model,
        "usage": usage,
        "limits": {
            "market_pp": MAX_MARKET_EFFECT_PP,
            "poll_pp": MAX_POLL_EFFECT_PP,
            "poll_priority_pp": MAX_POLL_PRIORITY_EFFECT_PP,
            "sentiment_pp": MAX_SENTIMENT_EFFECT_PP,
            "total_pp": MAX_TOTAL_EFFECT_PP,
        },
        "validation": validation,
    }


def _anchor_targets(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """先由民调与市场质量计算基础概率，AI不直接重写基准。"""
    anchors: dict[str, dict[str, Any]] = {}
    for region in payload.get("县市") or []:
        code = str(region.get("region_code") or "")
        candidates = list(region.get("正式候选人") or [])
        if not code or not candidates:
            continue
        has_poll = any(item.get("民调概率") is not None for item in candidates)
        has_market = any(_float(item.get("市场概率")) > 0 for item in candidates)
        poll_quality = _float(region.get("民调质量分"))
        market_reliability = _float(region.get("市场可靠度"))
        if has_market and has_poll:
            poll_weight = 0.10 + 0.35 * max(0.0, min(1.0, poll_quality))
            if market_reliability >= 0.85:
                poll_weight = min(poll_weight, 0.25)
            elif market_reliability >= 0.60:
                poll_weight = min(poll_weight, 0.38)
            else:
                poll_weight = min(poll_weight, 0.45)
            market_weight = 1.0 - poll_weight
            priority = "市场主导"
        elif has_market:
            market_weight, poll_weight = 1.0, 0.0
            priority = "市场主导"
        elif has_poll:
            market_weight, poll_weight = 0.0, 1.0
            priority = "民调替代"
        else:
            market_weight, poll_weight = 0.0, 0.0
            priority = "资料不足"

        neutral = 100.0 / max(1, len(candidates))
        raw: dict[str, float] = {}
        for item in candidates:
            name = _norm_name(item.get("姓名"))
            market = _float(item.get("市场概率"))
            poll = _float(item.get("民调概率"), 0.0) if item.get("民调概率") is not None else 0.0
            raw[name] = max(
                0.0,
                market_weight * market + poll_weight * poll,
            )
        total = sum(raw.values()) or 1.0
        probabilities = {
            name: max(0.05, value / total * 100.0)
            for name, value in raw.items()
        }
        normalized_total = sum(probabilities.values()) or 1.0
        probabilities = {
            name: value / normalized_total * 100.0
            for name, value in probabilities.items()
        }

        if not has_market:
            confidence, uncertainty = "低", 13
        elif market_reliability >= 0.85 and poll_weight <= 0.25:
            confidence, uncertainty = "高", 7
        elif market_reliability >= 0.60:
            confidence, uncertainty = "中", 9
        else:
            confidence, uncertainty = "低", 12

        anchors[code] = {
            "priority": priority,
            "market_weight": market_weight,
            "poll_weight": poll_weight,
            "neutral": neutral,
            "confidence": confidence,
            "uncertainty_pp": uncertainty,
            "candidates": {
                _norm_name(item.get("姓名")): {
                    "base_probability": probabilities[_norm_name(item.get("姓名"))],
                    "market_probability": _float(item.get("市场概率")),
                    "poll_probability": (
                        _float(item.get("民调概率"))
                        if item.get("民调概率") is not None
                        else None
                    ),
                    "market_effect_pp": market_weight
                    * (_float(item.get("市场概率")) - neutral),
                    "poll_effect_pp": poll_weight
                    * (
                        (
                            _float(item.get("民调概率"))
                            if item.get("民调概率") is not None
                            else 0.0
                        )
                        - neutral
                    ),
                }
                for item in candidates
            },
        }
    return anchors


def _request_event_adjustments(
    settings: Settings,
    payload: dict[str, Any],
    anchors: dict[str, dict[str, Any]],
    *,
    correction: str = "",
    attempt: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    compact_regions: list[dict[str, Any]] = []
    for region in payload.get("县市") or []:
        code = str(region.get("region_code") or "")
        anchor = anchors.get(code)
        if not anchor:
            continue
        compact_regions.append({
            "region_code": code,
            "region_name": region.get("region_name"),
            "reference_priority": anchor["priority"],
            "reference_reason": region.get("参考优先级原因"),
            "market_weight": anchor["market_weight"],
            "poll_weight": anchor["poll_weight"],
            "poll_quality": region.get("民调质量分"),
            "market_reliability": region.get("市场可靠度"),
            "hot_topics": region.get("热门议题") or [],
            "candidates": [
                {
                    "name": item.get("姓名"),
                    "anchor_probability": round(
                        anchor["candidates"][_norm_name(item.get("姓名"))]["base_probability"], 3
                    ),
                    "market_probability": item.get("市场概率"),
                    "poll_probability": item.get("民调概率"),
                    "news_sentiment": item.get("新闻情感"),
                    "articles": item.get("相关新闻数"),
                }
                for item in region.get("正式候选人") or []
            ],
        })
    system = f"""
你是选举预测的事件校正器，不负责重新计算基础胜率。
基础胜率已经由程序按“民调质量、市场质量、资料新鲜度”加权完成。

任务：
1. 只在有明确新闻事件、议题变化、数据质量问题足以改变判断时，对候选人基础胜率做小幅校正。
2. 每位候选人 adjustment_pp 必须在 -{MAX_EVENT_ADJUSTMENT_PP:g} 到 +{MAX_EVENT_ADJUSTMENT_PP:g} 之间。
3. 没有具体证据时必须给 0，不得因为市场领先或民调领先就重复加分。
4. 同一县市所有调整应尽量互相抵消，程序会强制合计为0并限制幅度。
5. 不得引入输入之外的事实，不得给出新的最终胜率。
6. 只能列出输入中的候选人和县市，不得遗漏、增加或改名。
7. summary 用普通书面中文，说明基础胜率的主要来源，以及AI是否因具体事件做了调整。

只输出JSON，不要Markdown：
{{"regions":[{{"region_code":"TPE","summary":"不超过100字","adjustments":[
{{"name":"姓名","adjustment_pp":数字,"reason":"不超过40字"}}
]}}]}}
""".strip()
    if correction:
        system += "\n\n上一次输出未通过校验：" + correction + "\n请重新输出完整JSON。"
    request_payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": json.dumps(
                    {"县市": compact_regions},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": 0.0,
        "max_tokens": 12000,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    response = post_json(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        request_payload,
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=LLM_TIMEOUT,
        retries=2,
    )
    content = str(response["choices"][0]["message"]["content"] or "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        match = _JSON_BLOCK.search(content)
        if not match:
            raise RuntimeError("AI事件校正未返回可解析JSON") from exc
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise RuntimeError("AI事件校正返回格式不是对象")
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (EXPORT_DIR / f"ai-anchored-raw-{stamp}-{attempt}.json").write_text(
        content, encoding="utf-8"
    )
    (EXPORT_DIR / "ai-forecast-usage.json").write_text(
        json.dumps({
            "model": response.get("model") or settings.llm_model,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "usage": usage,
            "method": "anchored_event_adjustment",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    issues: list[str] = []
    region_map = {
        str(item.get("region_code") or ""): item
        for item in parsed.get("regions") or []
        if isinstance(item, dict)
    }
    if set(region_map) != set(anchors):
        issues.append("县市列表不一致")
    for code, anchor in anchors.items():
        result_region = region_map.get(code)
        if not result_region:
            continue
        actual = {
            _norm_name(item.get("name")): item
            for item in result_region.get("adjustments") or []
            if isinstance(item, dict)
        }
        if set(actual) != set(anchor["candidates"]):
            issues.append(f"{code} 候选人不一致")
            continue
        for name, item in actual.items():
            adjustment = _float(item.get("adjustment_pp"))
            if abs(adjustment) > MAX_EVENT_ADJUSTMENT_PP + 0.01:
                issues.append(f"{code}/{name} 事件调整超限")
    if issues:
        if attempt == 0:
            retry, retry_usage = _request_event_adjustments(
                settings,
                payload,
                anchors,
                correction="；".join(issues[:12]),
                attempt=1,
            )
            return retry, _merge_usage(usage, retry_usage)
        raise RuntimeError("AI事件校正未通过校验：" + "；".join(issues[:10]))
    return parsed, usage


def _ai_fuse_anchor_adjust(
    settings: Settings,
    snapshots: dict[str, MarketSnapshot],
    candidate_sentiment: dict[str, dict[str, Any]],
    region_states: Iterable[dict[str, Any]],
    *,
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """程序建立民调/市场基准，AI仅在明确事件证据下做小幅校正。"""
    if not settings.llm_enabled or not settings.llm_api_key:
        raise RuntimeError("AI 综合胜率需要启用大模型并配置 API Key")
    payload, sources = _build_input(
        settings, snapshots, candidate_sentiment, region_states
    )
    anchors = _anchor_targets(payload)
    if progress_callback:
        progress_callback(0.20, "民调与市场基础胜率已计算")
    result, usage = _request_event_adjustments(settings, payload, anchors)
    if progress_callback:
        progress_callback(0.82, "AI事件校正已完成")
    ai_regions = {
        str(item.get("region_code") or ""): item
        for item in result.get("regions") or []
        if isinstance(item, dict)
    }

    rows: list[dict[str, Any]] = []
    validation: list[dict[str, Any]] = []
    for code, source in sources.items():
        region = next(
            item for item in payload.get("县市") or [] if item.get("region_code") == code
        )
        anchor = anchors[code]
        ai_region = ai_regions[code]
        expected = list(region.get("正式候选人") or [])
        ai_adjustments = {
            _norm_name(item.get("name")): item
            for item in ai_region.get("adjustments") or []
            if isinstance(item, dict)
        }
        raw_adjustments = [
            _float(ai_adjustments[_norm_name(item.get("姓名"))].get("adjustment_pp"))
            for item in expected
        ]
        balanced_adjustments = _bounded_zero_sum_deltas(
            raw_adjustments, MAX_EVENT_ADJUSTMENT_PP
        )
        base_scores = [
            anchor["candidates"][_norm_name(item.get("姓名"))]["base_probability"]
            for item in expected
        ]
        raw_final = [
            max(0.05, base_scores[index] + balanced_adjustments[index])
            for index in range(len(expected))
        ]
        final_total = sum(raw_final) or 1.0
        final_scores = [value / final_total * 100.0 for value in raw_final]

        ranked = sorted(
            zip(expected, base_scores, final_scores),
            key=lambda item: -item[2],
        )
        leader_name = _norm_name(ranked[0][0].get("姓名")) if ranked else ""
        summary = to_simplified(str(ai_region.get("summary") or "")).strip()
        for item, base_score, final_score in ranked:
            name = _norm_name(item.get("姓名"))
            source_candidate = source["official_candidates"][name]
            entry = source_candidate["entry"]
            market_candidate: CandidateSnapshot | None = source_candidate["market_candidate"]
            candidate_en = (
                (market_candidate.name_en if market_candidate else "")
                or str(entry.get("en") or "")
                or str(entry.get("name") or name)
            )
            candidate_anchor = anchor["candidates"][name]
            actual_event_effect = final_score - base_score
            market_effect = candidate_anchor["market_effect_pp"]
            poll_effect = candidate_anchor["poll_effect_pp"]
            adjustment_item = ai_adjustments[name]
            adjustment_reason = to_simplified(
                str(adjustment_item.get("reason") or "")
            ).strip()
            pm_prob = market_candidate.pm_prob if market_candidate else 0.0
            priority = source.get("reference_priority")
            base_text = (
                f"基础胜率 {base_score:.1f}%，按民调权重 {anchor['poll_weight']:.0%}、"
                f"市场权重 {anchor['market_weight']:.0%} 计算"
            )
            if abs(actual_event_effect) >= 0.05 and adjustment_reason:
                adjust_text = f"；AI因具体事件调整 {actual_event_effect:+.1f} 个百分点：{adjustment_reason}"
            else:
                adjust_text = "；没有足够的新增事件改变基础判断"
            rationale = (
                f"综合胜率为 {final_score:.1f}%。{base_text}{adjust_text}。"
                + (f" {summary}" if summary else "")
            )
            components = {
                "calculation_engine": "llm",
                "calculation_method": "anchored_event_adjustment",
                "ai_model": settings.llm_model,
                "ai_confidence": anchor["confidence"],
                "ai_uncertainty_pp": anchor["uncertainty_pp"],
                "ai_region_summary": summary,
                "reference_priority": priority,
                "reference_reason": source.get("reference_reason"),
                "anchor_probability": round(base_score / 100.0, 6),
                "ai_neutral_probability": round(anchor["neutral"] / 100.0, 6),
                "ai_market_effect_pp": round(market_effect, 4),
                "ai_poll_effect_pp": round(poll_effect, 4),
                "ai_sentiment_effect_pp": round(actual_event_effect, 4),
                "ai_event_adjustment_pp": round(actual_event_effect, 4),
                "ai_adjustment_reason": adjustment_reason,
                "ai_total_effect_pp": round(
                    market_effect + poll_effect + actual_event_effect, 4
                ),
                "ai_effect_limits_pp": {
                    "event_adjustment": MAX_EVENT_ADJUSTMENT_PP,
                },
                "ai_usage": usage,
                "market_weight": anchor["market_weight"],
                "poll_weight": anchor["poll_weight"],
                "poll_quality": region.get("民调质量分"),
                "market_tier": source["profile"]["tier"],
                "market_reliability": source["profile"]["reliability"],
                "market_volume": source["profile"]["volume"],
                "market_liquidity": source["profile"]["liquidity"],
                "market_depth_usdc": source["profile"]["depth_usdc"],
                "market_spread": source["profile"]["spread"],
                "poll_count": (source.get("poll") or {}).get("sources"),
                "poll_effective_samples": (source.get("poll") or {}).get("effective_samples"),
                "poll_calibrated": (source.get("poll") or {}).get("calibrated"),
                "poll_date": (source.get("poll") or {}).get("date"),
                "poll_freshness": (source.get("poll") or {}).get("freshness"),
                "article_count": source_candidate["articles"],
            }
            rows.append({
                "region_code": code,
                "region_name": source["region"].region_name,
                "candidate_en": candidate_en,
                "candidate_zh": name,
                "pm_prob": round(pm_prob or 0.0, 6),
                "pm_raw": market_candidate.yes_price if market_candidate else None,
                "poll_prob": round(source_candidate["poll_probability"], 6)
                if source_candidate["poll_probability"] is not None else None,
                "poll_share": (source.get("poll") or {}).get("raw", {}).get(str(entry.get("name"))),
                "poll_margin": (source.get("poll") or {}).get("margin"),
                "news_sentiment": round(source_candidate["news_sentiment"], 4),
                "fused_prob": round(final_score / 100.0, 6),
                "rationale": rationale,
                "edge": round(final_score / 100.0 - (pm_prob or 0.0), 6),
                "articles": source_candidate["articles"],
                "region_sentiment": source["region_sentiment"],
                "chg_1d": market_candidate.chg_1d if market_candidate else None,
                "chg_1w": market_candidate.chg_1w if market_candidate else None,
                "volume": market_candidate.volume if market_candidate else None,
                "poll_leader": (source.get("poll") or {}).get("leader"),
                "components": components,
                "leader": name == leader_name,
            })
        validation.append({
            "region_code": code,
            "candidates": len(expected),
            "sum_probability": round(sum(final_scores), 6),
            "base_sum_probability": round(sum(base_scores), 6),
            "max_event_adjustment_pp": round(
                max((abs(value) for value in balanced_adjustments), default=0.0), 4
            ),
            "market_weight": anchor["market_weight"],
            "poll_weight": anchor["poll_weight"],
        })
    if progress_callback:
        progress_callback(1.0, "基础胜率与AI事件校正已完成")
    return rows, {
        "schema_version": 2,
        "engine": "llm_anchor",
        "model": settings.llm_model,
        "usage": usage,
        "method": "anchored_event_adjustment",
        "limits": {
            "event_adjustment_pp": MAX_EVENT_ADJUSTMENT_PP,
        },
        "weights": {
            code: {
                "market": anchor["market_weight"],
                "poll": anchor["poll_weight"],
            }
            for code, anchor in anchors.items()
        },
        "validation": validation,
    }


def _request_unified_probabilities(
    settings: Settings,
    payload: dict[str, Any],
    anchors: dict[str, dict[str, Any]],
    *,
    correction: str = "",
    attempt: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    compact_regions: list[dict[str, Any]] = []
    for region in payload.get("县市") or []:
        code = str(region.get("region_code") or "")
        anchor = anchors.get(code)
        if not anchor:
            continue
        compact_regions.append({
            "region_code": code,
            "region_name": region.get("region_name"),
            "reference_priority": anchor["priority"],
            "reference_reason": region.get("参考优先级原因"),
            "market_weight": anchor["market_weight"],
            "poll_weight": anchor["poll_weight"],
            "poll_quality": region.get("民调质量分"),
            "poll_composition": region.get("民调阵营构成") or {},
            "market_reliability": region.get("市场可靠度"),
            "hot_topics": region.get("热门议题") or [],
            "candidates": [
                {
                    "name": item.get("姓名"),
                    "anchor_probability": round(
                        anchor["candidates"][_norm_name(item.get("姓名"))]["base_probability"], 3
                    ),
                    "market_probability": item.get("市场概率"),
                    "poll_probability": item.get("民调概率"),
                    "news_sentiment": item.get("新闻情感"),
                    "articles": item.get("相关新闻数"),
                }
                for item in region.get("正式候选人") or []
            ],
        })
    system = f"""
你是台湾地方选举综合胜率计算器。程序已经根据数据质量给出每位候选人的基础胜率，
你必须统一综合所有输入因素，输出同选区全部正式候选人的最终胜率，并用普通书面中文解释原因。

必须综合考虑：
1. 市场赔率始终是第一锚点。成交量大、流动性高、盘口深、价差小时更信任市场；
   成交少、流动性低或盘口薄时降低信任，但不能把市场高概率直接当成最终结论。
2. 新鲜民调用于校正市场。民调较新、来源较多时提高权重；民调较旧、样本弱或与市场明显冲突时降低权重。
   民调权重不能超过市场，最终判断仍以市场为主。
3. 民调若来自政党、竞选团队或具倾向机构，要结合结果是否有利委托方判断：
   - 绿营民调显示绿营候选人领先，可信度较低；
   - 绿营民调显示蓝营或其他阵营候选人领先，属于违背委托方利益的结果，可信度较高；
   - 蓝营民调按同样规则反向处理；不得机械平均或反向篡改支持度。
4. 新闻舆情只能小幅修正。新闻数量多不必然大幅改变胜率，负面新闻多也不等于一定落选。
5. 市场与民调方向冲突时，先保留市场主锚，再用高质量、新鲜、反阵营有利的民调校正。
6. 缺少市场或民调的候选人可以参考其他资料，但必须给出较低概率，并降低该县市置信度。
7. AI最终胜率可以偏离基础胜率，但每位候选人最多偏离
   {MAX_AI_ANCHOR_DEVIATION_PP:g} 个百分点。
8. 不得虚构输入中没有的事件；没有足够证据时，最终胜率应接近基础胜率。
9. 同一县市最终 probability 合计必须为100.0，并保留一位小数。
10. 所有正式候选人都必须出现，不得遗漏、增加或改名。

输出JSON，不要Markdown：
{{"regions":[{{"region_code":"TPE","summary":"不超过100字","candidates":[
{{"name":"姓名","probability":数字,"reason":"不超过80字，说明基础和调整原因"}}
]}}]}}
""".strip()
    if correction:
        system += "\n\n上一次输出未通过校验：" + correction + "\n请重新输出完整JSON。"
    response = post_json(
        f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        {
            "model": settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"县市": compact_regions},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "temperature": 0.0,
            "max_tokens": 14000,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        },
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        timeout=LLM_TIMEOUT,
        retries=2,
    )
    content = str(response["choices"][0]["message"]["content"] or "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        match = _JSON_BLOCK.search(content)
        if not match:
            raise RuntimeError("AI综合胜率未返回可解析JSON") from exc
        parsed = json.loads(match.group(0))
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (EXPORT_DIR / f"ai-unified-raw-{stamp}-{attempt}.json").write_text(
        content, encoding="utf-8"
    )
    (EXPORT_DIR / "ai-forecast-usage.json").write_text(
        json.dumps({
            "model": response.get("model") or settings.llm_model,
            "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "usage": usage,
            "method": "ai_unified_final",
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    issues: list[str] = []
    result_regions = {
        str(item.get("region_code") or ""): item
        for item in parsed.get("regions") or []
        if isinstance(item, dict)
    }
    if set(result_regions) != set(anchors):
        issues.append("县市列表不一致")
    for code, anchor in anchors.items():
        result_region = result_regions.get(code)
        if not result_region:
            continue
        result_candidates = {
            _norm_name(item.get("name")): item
            for item in result_region.get("candidates") or []
            if isinstance(item, dict)
        }
        if set(result_candidates) != set(anchor["candidates"]):
            issues.append(f"{code} 候选人不一致")
            continue
        values = {
            name: _clamp(_float(item.get("probability")), 0.0, 100.0)
            for name, item in result_candidates.items()
        }
        total = sum(values.values())
        if total <= 0:
            issues.append(f"{code} 胜率合计无效")
            continue
        normalized = {name: value / total * 100.0 for name, value in values.items()}
        for name, probability in normalized.items():
            anchor_probability = anchor["candidates"][name]["base_probability"]
            if abs(probability - anchor_probability) > MAX_AI_ANCHOR_DEVIATION_PP + 0.2:
                issues.append(
                    f"{code}/{name} 偏离基础胜率过大 "
                    f"({probability - anchor_probability:+.1f}点)"
                )
    if issues:
        if attempt == 0:
            retry, retry_usage = _request_unified_probabilities(
                settings,
                payload,
                anchors,
                correction="；".join(issues[:12]),
                attempt=1,
            )
            return retry, _merge_usage(usage, retry_usage)
        raise RuntimeError("AI综合胜率未通过校验：" + "；".join(issues[:10]))
    return parsed, usage


def ai_fuse(
    settings: Settings,
    snapshots: dict[str, MarketSnapshot],
    candidate_sentiment: dict[str, dict[str, Any]],
    region_states: Iterable[dict[str, Any]],
    *,
    progress_callback: Callable[[float, str], None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """AI统一给出最终胜率；基础胜率和数据质量约束由程序提供。"""
    if not settings.llm_enabled or not settings.llm_api_key:
        raise RuntimeError("AI 综合胜率需要启用大模型并配置 API Key")
    payload, sources = _build_input(
        settings, snapshots, candidate_sentiment, region_states
    )
    anchors = _anchor_targets(payload)
    if progress_callback:
        progress_callback(0.20, "民调与市场基础胜率已计算")
    result, usage = _request_unified_probabilities(settings, payload, anchors)
    if progress_callback:
        progress_callback(0.82, "AI综合胜率已生成")
    result_regions = {
        str(item.get("region_code") or ""): item
        for item in result.get("regions") or []
        if isinstance(item, dict)
    }
    rows: list[dict[str, Any]] = []
    validation: list[dict[str, Any]] = []
    for code, source in sources.items():
        region = next(
            item for item in payload.get("县市") or [] if item.get("region_code") == code
        )
        anchor = anchors[code]
        result_region = result_regions[code]
        expected = list(region.get("正式候选人") or [])
        result_candidates = {
            _norm_name(item.get("name")): item
            for item in result_region.get("candidates") or []
            if isinstance(item, dict)
        }
        raw_values = [
            max(0.0, _float(result_candidates[_norm_name(item.get("姓名"))].get("probability")))
            for item in expected
        ]
        total = sum(raw_values) or 1.0
        final_scores = _one_decimal_probabilities(
            [value / total * 100.0 for value in raw_values]
        )
        base_scores = [
            anchor["candidates"][_norm_name(item.get("姓名"))]["base_probability"]
            for item in expected
        ]
        ranked = sorted(
            zip(expected, base_scores, final_scores),
            key=lambda item: -item[2],
        )
        leader_name = _norm_name(ranked[0][0].get("姓名")) if ranked else ""
        summary = to_simplified(str(result_region.get("summary") or "")).strip()
        for item, base_score, final_score in ranked:
            name = _norm_name(item.get("姓名"))
            source_candidate = source["official_candidates"][name]
            entry = source_candidate["entry"]
            market_candidate: CandidateSnapshot | None = source_candidate["market_candidate"]
            candidate_en = (
                (market_candidate.name_en if market_candidate else "")
                or str(entry.get("en") or "")
                or str(entry.get("name") or name)
            )
            candidate_anchor = anchor["candidates"][name]
            ai_adjustment = final_score - base_score
            reason = to_simplified(
                str(result_candidates[name].get("reason") or "")
            ).strip()
            pm_prob = market_candidate.pm_prob if market_candidate else 0.0
            priority = source.get("reference_priority")
            rationale = (
                f"AI综合胜率为 {final_score:.1f}%。基础胜率 {base_score:.1f}%，"
                f"民调权重 {anchor['poll_weight']:.0%}、市场权重 {anchor['market_weight']:.0%}。"
                + (f" AI综合修正 {ai_adjustment:+.1f} 个百分点：{reason}" if reason
                   else " AI综合判断后未对基础胜率作明显修正。")
                + (f" {summary}" if summary else "")
            )
            components = {
                "calculation_engine": "llm",
                "calculation_method": "ai_unified_final",
                "ai_model": settings.llm_model,
                "ai_confidence": anchor["confidence"],
                "ai_uncertainty_pp": anchor["uncertainty_pp"],
                "ai_region_summary": summary,
                "ai_reason": reason,
                "reference_priority": priority,
                "reference_reason": source.get("reference_reason"),
                "anchor_probability": round(base_score / 100.0, 6),
                "ai_final_adjustment_pp": round(ai_adjustment, 4),
                "ai_market_effect_pp": round(candidate_anchor["market_effect_pp"], 4),
                "ai_poll_effect_pp": round(candidate_anchor["poll_effect_pp"], 4),
                "ai_sentiment_effect_pp": round(ai_adjustment, 4),
                "ai_total_effect_pp": round(
                    candidate_anchor["market_effect_pp"]
                    + candidate_anchor["poll_effect_pp"]
                    + ai_adjustment,
                    4,
                ),
                "ai_effect_limits_pp": {
                    "anchor_deviation": MAX_AI_ANCHOR_DEVIATION_PP,
                },
                "ai_usage": usage,
                "market_weight": anchor["market_weight"],
                "poll_weight": anchor["poll_weight"],
                "poll_quality": region.get("民调质量分"),
                "poll_composition": region.get("民调阵营构成") or {},
                "market_tier": source["profile"]["tier"],
                "market_reliability": source["profile"]["reliability"],
                "market_volume": source["profile"]["volume"],
                "market_liquidity": source["profile"]["liquidity"],
                "market_depth_usdc": source["profile"]["depth_usdc"],
                "market_spread": source["profile"]["spread"],
                "poll_count": (source.get("poll") or {}).get("sources"),
                "poll_effective_samples": (source.get("poll") or {}).get("effective_samples"),
                "poll_calibrated": (source.get("poll") or {}).get("calibrated"),
                "poll_date": (source.get("poll") or {}).get("date"),
                "poll_freshness": (source.get("poll") or {}).get("freshness"),
                "article_count": source_candidate["articles"],
            }
            rows.append({
                "region_code": code,
                "region_name": source["region"].region_name,
                "candidate_en": candidate_en,
                "candidate_zh": name,
                "pm_prob": round(pm_prob or 0.0, 6),
                "pm_raw": market_candidate.yes_price if market_candidate else None,
                "poll_prob": round(source_candidate["poll_probability"], 6)
                if source_candidate["poll_probability"] is not None else None,
                "poll_share": (source.get("poll") or {}).get("raw", {}).get(str(entry.get("name"))),
                "poll_margin": (source.get("poll") or {}).get("margin"),
                "news_sentiment": round(source_candidate["news_sentiment"], 4),
                "fused_prob": round(final_score / 100.0, 6),
                "rationale": rationale,
                "edge": round(final_score / 100.0 - (pm_prob or 0.0), 6),
                "articles": source_candidate["articles"],
                "region_sentiment": source["region_sentiment"],
                "chg_1d": market_candidate.chg_1d if market_candidate else None,
                "chg_1w": market_candidate.chg_1w if market_candidate else None,
                "volume": market_candidate.volume if market_candidate else None,
                "poll_leader": (source.get("poll") or {}).get("leader"),
                "components": components,
                "leader": name == leader_name,
            })
        validation.append({
            "region_code": code,
            "candidates": len(expected),
            "sum_probability": round(sum(final_scores), 6),
            "max_anchor_deviation_pp": round(
                max(
                    (abs(final_scores[index] - base_scores[index]) for index in range(len(expected))),
                    default=0.0,
                ),
                4,
            ),
            "market_weight": anchor["market_weight"],
            "poll_weight": anchor["poll_weight"],
        })
    if progress_callback:
        progress_callback(1.0, "AI综合胜率校验完成")
    return rows, {
        "schema_version": 3,
        "engine": "llm_unified",
        "model": settings.llm_model,
        "usage": usage,
        "method": "ai_unified_final",
        "limits": {
            "anchor_deviation_pp": MAX_AI_ANCHOR_DEVIATION_PP,
        },
        "validation": validation,
    }
