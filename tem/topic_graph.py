"""基于本地新闻事件构建候选人、县市、议题与政党关系图。"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import db
from .config import load_settings
from .paths import CONFIG_DIR
from .roster import load_roster
from .zhconv import to_simplified

ALLOWED_HOURS = {24, 72, 168, 360, 720}
_INVALID_NAMES = {"男", "女", "男性", "女性", "性别", "姓名", "备注", "候选人"}
_PARTIES = ("中国国民党", "民主进步党", "台湾民众党")


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


def _name(value: Any) -> str:
    return to_simplified(str(value or "")).replace("臺", "台").strip()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def _candidate_catalog() -> tuple[dict[str, str], dict[str, str], set[str]]:
    """返回姓名到政党、姓名到县市、22县市名称集合。"""
    party_by_name: dict[str, str] = {}
    region_by_name: dict[str, str] = {}
    region_names: set[str] = set()

    for region, entries in (load_roster().get("regions") or {}).items():
        region_name = _name(region)
        region_names.add(region_name)
        for entry in entries or []:
            name = _name(entry.get("name"))
            party = _name(entry.get("party"))
            if name:
                party_by_name.setdefault(name, party)
                region_by_name.setdefault(name, region_name)

    council = _read_json(CONFIG_DIR / "council_2026.json")
    for region, payload in (council.get("regions") or {}).items():
        region_name = _name(region)
        region_names.add(region_name)
        for district in (payload or {}).get("districts") or []:
            for entry in district.get("candidates") or []:
                name = _name(entry.get("name"))
                party = _name(entry.get("party"))
                if name:
                    party_by_name.setdefault(name, party)
                    region_by_name.setdefault(name, region_name)

    extra = _read_json(CONFIG_DIR / "candidates_extra.json")
    for entry in extra.get("entries") or []:
        name = _name(entry.get("name"))
        region_name = _name(entry.get("region"))
        party = _name(entry.get("party"))
        if name:
            party_by_name.setdefault(name, party)
            region_by_name.setdefault(name, region_name)
            if region_name:
                region_names.add(region_name)
    return party_by_name, region_by_name, region_names


def _load_rows(hours: int) -> list[dict[str, Any]]:
    rows = db.fetch_news_hours_window(
        from_hours_ago=hours,
        to_hours_ago=0,
        limit=8000,
    )
    return [dict(row) for row in rows if int(row["is_rep"] or 0) == 1]


def _event_entities(
    row: dict[str, Any],
    party_by_name: dict[str, str],
    known_regions: set[str],
) -> dict[str, list[str]]:
    candidates: list[str] = []
    for item in _json_list(row.get("candidates")):
        if isinstance(item, dict):
            value = item.get("name_zh") or item.get("name")
        else:
            value = item
        name = _name(value)
        if name.startswith("local:"):
            name = name.split(":", 1)[1]
        if len(name) >= 2 and name not in _INVALID_NAMES and name not in candidates:
            candidates.append(name)

    regions = []
    for item in _json_list(row.get("regions")):
        region = _name(item)
        if region in known_regions and region not in regions:
            regions.append(region)

    topics = []
    for item in _json_list(row.get("topics")):
        if isinstance(item, dict):
            topic = item.get("topic") or item.get("name")
        else:
            topic = item
        topic = _name(topic)
        if 2 <= len(topic) <= 24 and topic not in topics:
            topics.append(topic)

    parties = []
    for name in candidates:
        party = party_by_name.get(name, "")
        if party and party not in parties:
            parties.append(party)
    return {
        "candidates": candidates[:8],
        "regions": regions[:4],
        "topics": topics[:8],
        "parties": parties[:4],
    }


def _node_kind(category: str) -> str:
    return {
        "candidates": "候选人",
        "regions": "县市",
        "topics": "选举议题",
        "parties": "政党",
    }[category]


def _build_groups(nodes: list[dict[str, Any]], priority_names: set[str]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        grouped[str(node["group"])].append(str(node["tag"]))
    return [
        {"name": name, "tags": tags}
        for name, tags in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    ]


def build_local_graph(hours: int = 720) -> dict[str, Any]:
    hours = hours if hours in ALLOWED_HOURS else 720
    settings = load_settings()
    party_by_name, region_by_name, known_regions = _candidate_catalog()
    priority_names = {
        _name(item.get("name"))
        for item in settings.priority_local_candidates
        if item.get("name")
    }
    rows = _load_rows(hours)
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)

    stats: dict[tuple[str, str], dict[str, Any]] = {}
    edge_stats: dict[tuple[str, str], dict[str, Any]] = {}
    event_count = 0

    def add_node(category: str, tag: str, row: dict[str, Any], weight: float) -> None:
        if not tag:
            return
        key = (category, tag)
        node = stats.setdefault(
            key,
            {
                "category": category,
                "tag": tag,
                "count": 0,
                "score": 0.0,
                "first": "",
                "last": "",
            },
        )
        node["count"] += 1
        node["score"] += weight
        published = str(row.get("published_at") or row.get("first_seen") or "")
        if published:
            node["first"] = min(filter(None, (node["first"], published)))
            node["last"] = max(node["last"], published)

    for row in rows:
        entities = _event_entities(row, party_by_name, known_regions)
        tags = [*entities["candidates"], *entities["regions"], *entities["topics"], *entities["parties"]]
        tags = list(dict.fromkeys(tag for tag in tags if tag))
        if not tags:
            continue
        event_count += 1
        impact = max(0.1, float(row.get("impact") or 0.35))
        cluster_size = max(1, int(row.get("cluster_size") or 1))
        weight = impact * (1.0 + 0.18 * math.log1p(cluster_size))
        for category in ("candidates", "regions", "topics", "parties"):
            for tag in entities[category]:
                add_node(category, tag, row, weight)

        category_by_tag = {
            tag: category
            for category in ("candidates", "regions", "topics", "parties")
            for tag in entities[category]
        }
        for index, source in enumerate(tags):
            source_category = category_by_tag[source]
            for target in tags[index + 1:]:
                target_category = category_by_tag[target]
                if source_category == target_category == "regions":
                    continue
                if source_category == target_category == "topics":
                    continue
                key = tuple(sorted((source, target)))
                edge = edge_stats.setdefault(
                    key,
                    {
                        "source": key[0],
                        "target": key[1],
                        "value": 0,
                        "score": 0.0,
                        "first": "",
                        "last": "",
                    },
                )
                edge["value"] += 1
                edge["score"] += weight
                published = str(row.get("published_at") or row.get("first_seen") or "")
                if published:
                    edge["first"] = min(filter(None, (edge["first"], published)))
                    edge["last"] = max(edge["last"], published)

    quotas = {"candidates": 20, "regions": 12, "topics": 14, "parties": 4}
    selected_stats: list[dict[str, Any]] = []
    for category, quota in quotas.items():
        rows_for_category = sorted(
            (item for item in stats.values() if item["category"] == category),
            key=lambda item: (-item["score"], -item["count"], item["tag"]),
        )
        candidate_rows = rows_for_category[:quota]
        if category == "candidates":
            candidate_rows.sort(
                key=lambda item: (
                    0 if item["tag"] in priority_names else 1,
                    -item["score"],
                    -item["count"],
                )
            )
        selected_stats.extend(candidate_rows)

    allowed = {item["tag"] for item in selected_stats}
    edges = [
        edge for edge in edge_stats.values()
        if edge["source"] in allowed and edge["target"] in allowed
    ]
    edges.sort(key=lambda item: (-item["value"], -item["score"], item["source"]))
    edges = edges[:70]
    connected = {tag for edge in edges for tag in (edge["source"], edge["target"])}
    selected_stats = [item for item in selected_stats if item["tag"] in connected]

    max_score = max((item["score"] for item in selected_stats), default=1.0) or 1.0
    primary_candidates = {
        item["tag"]
        for item in sorted(
            (item for item in selected_stats if item["category"] == "candidates"),
            key=lambda item: -item["score"],
        )[:4]
    }
    node_counts = {item["tag"]: max(1, item["count"]) for item in selected_stats}
    nodes: list[dict[str, Any]] = []
    for item in selected_stats:
        category = item["category"]
        tag = item["tag"]
        if category == "candidates":
            region = region_by_name.get(tag, "")
            group = (
                "新北市重点议员" if region == "新北市" and tag in priority_names else
                "宜兰县重点议员" if region == "宜兰县" and tag in priority_names else
                "2026县市长候选人"
            )
        elif category == "regions":
            group = "22县市"
        elif category == "parties":
            group = "政党阵营"
        else:
            group = "新闻议题"
        nodes.append({
            "id": tag,
            "tag": tag,
            "kind": _node_kind(category),
            "group": group,
            "groups": [group],
            "count": int(item["count"]),
            "score": round(100 * float(item["score"]) / max_score, 1),
            "burst": 0.0,
            "primary": tag in primary_candidates,
            "first_at": item["first"],
            "last_at": item["last"],
        })

    clean_edges = []
    for edge in edges:
        denominator = max(1, min(node_counts.get(edge["source"], 1), node_counts.get(edge["target"], 1)))
        clean_edges.append({
            **edge,
            "score": round(float(edge["score"]), 3),
            "share": round(min(1.0, edge["value"] / denominator), 3),
            "relation": "co_occurrence",
        })

    groups = _build_groups(nodes, priority_names)
    return {
        "schema_version": 1,
        "ok": True,
        "source": "本地新闻库",
        "scope": "local",
        "hours": hours,
        "fetched_at_iso": end.isoformat(timespec="seconds"),
        "graph_window": {
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
            "hours": hours,
            "days": round(hours / 24, 1),
        },
        "graph": {
            "nodes": nodes,
            "edges": clean_edges,
            "groups": groups,
            "note": "关系来自本地去重代表新闻中的共同出现；只表示事件关联，不表示因果或支持度。",
        },
        "groups": groups,
        "counts": {
            "events": event_count,
            "graph_nodes": len(nodes),
            "graph_edges": len(clean_edges),
        },
        "warnings": [] if rows else ["所选时间范围内暂无已完成分析的本地新闻。"],
    }


def related_news(
    source: str,
    target: str,
    *,
    hours: int = 720,
    limit: int = 8,
) -> dict[str, Any]:
    hours = hours if hours in ALLOWED_HOURS else 720
    source = _name(source)
    target = _name(target)
    if not source or not target:
        raise ValueError("关系节点不能为空")
    party_by_name, _region_by_name, known_regions = _candidate_catalog()
    rows = _load_rows(hours)
    found = []
    seen: set[str] = set()
    for row in rows:
        entities = _event_entities(row, party_by_name, known_regions)
        tags = {
            tag for category in ("candidates", "regions", "topics", "parties")
            for tag in entities[category]
        }
        if source not in tags or target not in tags:
            continue
        key = str(row.get("cluster_id") or row.get("url") or row.get("id") or "")
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "id": row.get("id"),
            "title": to_simplified(str(row.get("title") or ""))[:180],
            "source_name": str(row.get("source_name") or ""),
            "published_at": str(row.get("published_at") or row.get("first_seen") or ""),
            "url": str(row.get("url") or ""),
            "summary": to_simplified(str(row.get("ai_summary") or row.get("summary") or ""))[:220],
        })
        if len(found) >= max(1, min(20, limit)):
            break
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    return {
        "ok": True,
        "source": source,
        "target": target,
        "scope": "local",
        "window": {
            "start": start.isoformat(timespec="seconds"),
            "end": end.isoformat(timespec="seconds"),
            "hours": hours,
            "days": round(hours / 24, 1),
        },
        "news": found,
        "local_match_count": len(found),
    }
