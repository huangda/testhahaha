"""从去重后的代表新闻中提取具体热点议题。"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from typing import Any, Iterable

from .roster import load_roster
from .zhconv import to_simplified

_TITLE_KEY = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")
_ISSUE_RULES: dict[str, tuple[float, tuple[str, ...]]] = {
    "民生经济": (
        1.0,
        ("物价", "就业", "薪资", "低薪", "经济", "产业", "中小企业", "投资",
         "消费", "商圈", "观光", "农业", "农损", "市场", "摊商"),
    ),
    "住房交通": (
        0.95,
        ("社宅", "社会住宅", "房价", "租金", "租屋", "居住正义", "都更",
         "交通", "捷运", "地铁", "铁路", "公路", "公车", "停车", "道路",
         "塞车", "桥梁", "机场", "港口"),
    ),
    "社会福利": (
        0.92,
        ("长照", "育儿", "托育", "敬老", "老人", "津贴", "健保", "医疗",
         "弱势", "身心障碍", "补助", "福利"),
    ),
    "教育青年": (
        0.84,
        ("教育", "学校", "学生", "青年", "创业", "大学", "技职", "学费",
         "儿少", "校园"),
    ),
    "能源环境": (
        0.88,
        ("能源", "电力", "停电", "台电", "电价", "绿能", "核能", "空污",
         "垃圾", "水情", "缺水", "治水", "净零", "环保"),
    ),
    "治安廉政": (
        0.83,
        ("治安", "诈骗", "毒品", "犯罪", "警察", "消防", "廉政", "黑金",
         "贿选", "贪腐", "司法", "搜索", "起诉", "交保"),
    ),
    "灾害治理": (
        0.78,
        ("台风", "地震", "淹水", "停班停课", "防灾", "救灾", "灾情",
         "水灾", "土石流", "豪雨"),
    ),
    "两岸国安": (
        0.58,
        ("两岸", "中共介选", "中共", "国安", "防务", "军演", "统战", "渗透",
         "外交", "国防"),
    ),
}
_SWING_TERMS = (
    "中间选民", "年轻家庭", "小家庭", "上班族", "首购", "租屋族",
    "物价", "房价", "薪资", "就业", "育儿", "托育", "长照", "交通",
    "诈骗", "治安", "停电", "电价", "淹水", "治水", "民生",
)
_MAIN_PARTIES = ("中国国民党", "民主进步党")


@lru_cache(maxsize=1)
def _party_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for entries in (load_roster().get("regions") or {}).values():
        for item in entries:
            name = to_simplified(str(item.get("name") or ""))
            party = to_simplified(str(item.get("party") or ""))
            if name and party:
                out[name] = party
    return out


def _clean_title(value: Any) -> str:
    title = to_simplified(str(value or "")).strip()
    return re.split(r"\s*[|｜]\s*", title, maxsplit=1)[0].strip()


def _title_key(title: str) -> str:
    return _TITLE_KEY.sub("", title.lower())[:140]


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _classify_issue(text: str) -> tuple[list[str], list[str], float, float]:
    category_hits: list[tuple[float, str, list[str]]] = []
    all_terms: list[str] = []
    for category, (weight, terms) in _ISSUE_RULES.items():
        hits = [term for term in terms if term in text]
        if hits:
            category_hits.append((weight * len(hits), category, hits))
            all_terms.extend(hits)
    category_hits.sort(key=lambda item: (-item[0], item[1]))
    categories = [item[1] for item in category_hits[:3]]
    matched = list(dict.fromkeys(all_terms))[:8]
    policy_strength = min(
        1.0,
        sum(min(1.0, len(term) / 4.0) for term in matched) / 3.0,
    )
    category_weight = category_hits[0][0] / max(1, len(category_hits[0][2])) if category_hits else 0.0
    swing_hits = [term for term in _SWING_TERMS if term in text]
    swing_score = min(
        1.0,
        0.5 * category_weight
        + 0.3 * min(1.0, len(swing_hits) / 2.0)
        + 0.2 * policy_strength,
    )
    return categories, matched, policy_strength, swing_score


def _party_effects(row: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    party_map = _party_map()
    grouped: dict[str, dict[str, Any]] = {}
    for item in _json_list(row.get("candidates")):
        if not isinstance(item, dict):
            continue
        name = to_simplified(str(item.get("name_zh") or item.get("name") or "")).strip()
        party = party_map.get(name, "")
        if party not in _MAIN_PARTIES or item.get("sentiment") is None:
            continue
        weight = max(1, int(item.get("mentions") or 1))
        entry = grouped.setdefault(party, {"weighted": 0.0, "weight": 0, "names": []})
        entry["weighted"] += float(item.get("sentiment") or 0.0) * weight
        entry["weight"] += weight
        if name and name not in entry["names"]:
            entry["names"].append(name)

    missing = [party for party in _MAIN_PARTIES if party not in grouped]
    if missing:
        return [], "未同时识别到蓝绿两阵营候选人，暂不作相对得失判断。"

    tones = {
        party: grouped[party]["weighted"] / max(1, grouped[party]["weight"])
        for party in _MAIN_PARTIES
    }
    difference = tones[_MAIN_PARTIES[0]] - tones[_MAIN_PARTIES[1]]
    if abs(difference) < 0.06:
        return [], "两党候选人在本事件中的情感差距不足 6 个百分点，暂不判定谁相对有利。"

    benefit = _MAIN_PARTIES[0] if difference > 0 else _MAIN_PARTIES[1]
    harmed = _MAIN_PARTIES[1] if difference > 0 else _MAIN_PARTIES[0]
    effects = []
    for party, direction in ((benefit, "相对有利"), (harmed, "相对不利")):
        entry = grouped[party]
        names = "、".join(entry["names"][:3])
        effects.append({
            "party": party,
            "direction": direction,
            "sentiment": round(tones[party], 3),
            "basis": f"{names}等候选人在本事件中的情感均值 {tones[party] * 100:+.0f}%。",
        })
    return effects, "依据同一事件中蓝绿候选人的情感差异，表示相对倾向，不等同于因果。"


def extract(
    rows: Iterable[dict[str, Any]],
    *,
    limit: int = 60,
) -> list[dict[str, Any]]:
    """返回有具体标题、摘要与关联实体的热点事件。

    按影响强度、媒体覆盖和关联实体数量排序。每一行对应一个已去重的
    新闻事件，不做没有原文依据的议题归纳。
    """
    seen: set[str] = set()
    issues: list[dict[str, Any]] = []

    for row in rows:
        if int(row.get("is_rep", 1) or 0) != 1:
            continue
        title = _clean_title(row.get("title"))
        if len(title) < 6:
            continue
        key = _title_key(title)
        if not key or key in seen:
            continue
        seen.add(key)

        regions = [
            to_simplified(str(item))
            for item in _json_list(row.get("regions"))
            if item
        ]
        candidates: list[str] = []
        for item in _json_list(row.get("candidates")):
            if isinstance(item, dict):
                name = item.get("name_zh") or item.get("name")
            else:
                name = str(item)
            name = to_simplified(str(name or "")).strip()
            if name and name not in candidates:
                candidates.append(name)

        topics: list[str] = []
        for item in _json_list(row.get("topics")):
            name = item.get("topic") if isinstance(item, dict) else str(item)
            name = to_simplified(str(name or "")).strip()
            if name and name not in topics:
                topics.append(name)

        summary = to_simplified(
            str(row.get("ai_summary") or row.get("summary") or "")
        ).strip()
        row_id = str(row.get("id") or "")
        text = f"{title} {summary} {' '.join(topics)}"
        issue_types, matched_terms, policy_strength, swing_score = _classify_issue(text)
        media_count = max(1, int(row.get("cluster_size") or 1))
        impact = _clamp(float(row.get("impact") or 0.3))
        sentiment = _clamp(float(row.get("sentiment") or 0.0), -1.0, 1.0)
        coverage = min(1.0, math.log1p(media_count) / math.log(6.0))
        attention = min(
            1.0,
            0.12 * len(candidates)
            + 0.08 * len(regions)
            + 0.04 * len(topics),
        )
        landscape_score = min(
            1.0,
            0.45 * policy_strength
            + 0.27 * impact
            + 0.16 * coverage
            + 0.12 * swing_score,
        )
        party_effects, party_effect_note = _party_effects(row)
        base_rank = 0.58 * impact + 0.27 * coverage + 0.15 * attention
        rank_score = 0.65 * landscape_score + 0.35 * base_rank

        url = str(row.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = ""

        issues.append({
            "news_id": row_id,
            "title": title[:180],
            "summary": summary[:360],
            "source": to_simplified(str(row.get("source_name") or "")).strip(),
            "published_at": row.get("published_at") or row.get("first_seen") or "",
            "url": url,
            "impact": round(impact, 3),
            "sentiment": round(sentiment, 3),
            "media_count": media_count,
            "regions": regions[:5],
            "candidates": candidates[:6],
            "topics": topics[:4],
            "is_policy": bool(issue_types),
            "issue_types": issue_types,
            "matched_terms": matched_terms[:5],
            "policy_strength": round(policy_strength, 3),
            "swing_score": round(swing_score, 3),
            "swing_label": "中间选民关注高" if swing_score >= 0.65 else (
                "中间选民值得关注" if swing_score >= 0.48 else "中间选民关联一般"
            ),
            "landscape_score": round(landscape_score, 3),
            "landscape_label": "可能改变选情" if landscape_score >= 0.62 else (
                "值得持续观察" if landscape_score >= 0.5 else "一般议题"
            ),
            "party_effects": party_effects,
            "party_effect_note": party_effect_note,
            "rank_score": round(rank_score, 4),
        })

    issues.sort(
        key=lambda item: (
            -item["rank_score"],
            -item["media_count"],
            -item["impact"],
            item["published_at"],
        )
    )
    return issues[:max(1, limit)]
