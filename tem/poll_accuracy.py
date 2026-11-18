"""历史民调机构准确度：比较 2018/2022 选前民调与实际选举结果。"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from .paths import CONFIG_DIR
from .zhconv import to_simplified


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _norm_person(value: Any) -> str:
    return re.sub(r"\s+", "", to_simplified(str(value or ""))).strip()


def _norm_region(value: Any) -> str:
    return _norm_person(value).replace("臺", "台")


def _norm_source(value: Any) -> str:
    """去掉上游资料里附在机构名称末尾的场次编号。"""
    name = re.sub(r"\s+", " ", to_simplified(str(value or ""))).strip()
    name = re.sub(r"(?<=[\u4e00-\u9fffA-Za-z])\d{1,3}$", "", name).strip()
    aliases = {
        "东森": "东森新闻",
        "联合报": "联合报系",
        "台湾指标调查": "台湾指标",
        "世新大学": "世新大学",
    }
    return aliases.get(name, name) or "未注明机构"


INSTITUTION_ALIASES: dict[str, str] = {
    "菱传媒2.0": "菱传媒",
    "菱传媒": "菱传媒",
    "钜闻天下": "菱传媒",
    "巨闻天下": "菱传媒",
    "钜闻民调": "菱传媒",
    "巨闻民调": "菱传媒",
    "et today": "ETtoday",
    "ettoday新闻云": "ETtoday",
    "东森": "东森新闻",
    "联合报": "联合报系",
    "台湾指标调查": "台湾指标",
}


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(records)
    hits = sum(1 for item in records if item.get("correct"))
    errors = [
        float(item["margin_error"])
        for item in records
        if item.get("margin_error") is not None
    ]
    by_year: dict[str, dict[str, Any]] = {}
    for year in ("2018", "2022"):
        year_rows = [item for item in records if str(item.get("year")) == year]
        year_hits = sum(1 for item in year_rows if item.get("correct"))
        year_errors = [
            float(item["margin_error"])
            for item in year_rows
            if item.get("margin_error") is not None
        ]
        by_year[year] = {
            "samples": len(year_rows),
            "hits": year_hits,
            "hit_rate": round(year_hits / len(year_rows), 4) if year_rows else None,
            "avg_margin_error": (
                round(sum(year_errors) / len(year_errors), 1) if year_errors else None
            ),
        }
    return {
        "samples": count,
        "hits": hits,
        "hit_rate": round(hits / count, 4) if count else None,
        "avg_margin_error": round(sum(errors) / len(errors), 1) if errors else None,
        "by_year": by_year,
    }


@lru_cache(maxsize=1)
def load_poll_accuracy() -> dict[str, Any]:
    calibration = _load_json(CONFIG_DIR / "calibration.json")
    baseline = _load_json(CONFIG_DIR / "baseline.json")
    observations = calibration.get("observations") or []

    results: dict[tuple[str, str], dict[str, Any]] = {}
    for region_name, cycles in (baseline.get("regions") or {}).items():
        if not isinstance(cycles, dict):
            continue
        for year, result in cycles.items():
            if isinstance(result, dict):
                results[(_norm_region(region_name), str(year))] = result

    records: list[dict[str, Any]] = []
    for raw in observations:
        if not isinstance(raw, dict):
            continue
        year = str(raw.get("year") or "")
        region = to_simplified(str(raw.get("region") or ""))
        source_raw = to_simplified(str(raw.get("source") or ""))
        source = _norm_source(source_raw)
        poll_leader = to_simplified(str(raw.get("leader") or ""))
        poll_runner_up = to_simplified(str(raw.get("runner_up") or ""))
        poll_margin = _number(raw.get("margin"))
        actual = results.get((_norm_region(region), year), {})
        actual_winner = to_simplified(str(actual.get("winner") or raw.get("winner") or ""))
        actual_runner_up = to_simplified(str(actual.get("runner_up") or ""))
        actual_margin = _number(actual.get("margin"))
        margin_error = (
            round(abs(poll_margin - actual_margin), 1)
            if poll_margin is not None and actual_margin is not None
            else None
        )
        margin_bias = (
            round(poll_margin - actual_margin, 1)
            if poll_margin is not None and actual_margin is not None
            else None
        )
        correct = (
            bool(raw.get("leader_won"))
            if raw.get("leader_won") is not None
            else bool(poll_leader and _norm_person(poll_leader) == _norm_person(actual_winner))
        )
        records.append(
            {
                "year": year,
                "region": region,
                "date": str(raw.get("date") or ""),
                "source": source,
                "source_raw": source_raw,
                "poll_leader": poll_leader,
                "poll_runner_up": poll_runner_up,
                "poll_margin": poll_margin,
                "actual_winner": actual_winner,
                "actual_runner_up": actual_runner_up,
                "actual_margin": actual_margin,
                "margin_error": margin_error,
                "margin_bias": margin_bias,
                "correct": correct,
                "winner_party": to_simplified(str(actual.get("winner_party") or "")),
            }
        )

    source_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        source_records[record["source"]].append(record)

    institutions = []
    for source, rows in source_records.items():
        stats = _summary(rows)
        samples = int(stats["samples"])
        institutions.append(
            {
                "name": source,
                **stats,
                "sample_label": (
                    "较有参考性"
                    if samples >= 4
                    else ("样本有限" if samples >= 2 else "仅单次")
                ),
            }
        )
    institutions.sort(
        key=lambda item: (
            -(item.get("hit_rate") or 0),
            -int(item.get("samples") or 0),
            item.get("avg_margin_error") if item.get("avg_margin_error") is not None else 999,
            item["name"],
        )
    )
    for rank, institution in enumerate(institutions, 1):
        institution["rank"] = rank

    records.sort(
        key=lambda item: (
            str(item["year"]),
            str(item["date"]),
            str(item["source"]),
            str(item["region"]),
        ),
        reverse=True,
    )
    overall = _summary(records)
    eligible = [item for item in institutions if int(item["samples"]) >= 2]
    return {
        "meta": {
            "cycles": [2018, 2022],
            "updated_at": (calibration.get("meta") or {}).get("updated_at")
            or (baseline.get("meta") or {}).get("updated_at")
            or "",
            "window_days": (calibration.get("meta") or {}).get("window_days"),
            "samples": overall["samples"],
            "institutions": len(institutions),
            "hits": overall["hits"],
            "hit_rate": overall["hit_rate"],
            "avg_margin_error": overall["avg_margin_error"],
            "best_sufficient": eligible[0]["name"] if eligible else None,
            "method": "逐次比较选前民调领先者是否当选，并计算民调领先幅度与实际胜负差距的绝对误差。",
            "caution": "单一机构样本通常只有数次，排名只能作为参考；样本越多、命中越高、误差越小，可信度才越强。",
            "result_sources": (baseline.get("meta") or {}).get("sources") or [],
        },
        "institutions": institutions,
        "records": records,
    }


def history_reference(value: Any) -> dict[str, Any] | None:
    """返回发布机构可关联的2018/2022历史战绩。

    新品牌“钜闻天下／菱传媒2.0”关联到旧“菱传媒”战绩，但会在返回结果中
    明确标记为品牌关联，避免界面把两段历史混为完全相同的机构。
    """
    text = re.sub(r"\s+", "", to_simplified(str(value or ""))).lower()
    if not text:
        return None
    payload = load_poll_accuracy()
    by_name = {
        re.sub(r"\s+", "", str(item.get("name") or "")).lower(): item
        for item in payload.get("institutions") or []
    }
    matched_name = ""
    alias_matched = False
    for alias, target in sorted(
        INSTITUTION_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        alias_key = re.sub(r"\s+", "", to_simplified(alias)).lower()
        target_key = re.sub(r"\s+", "", to_simplified(target)).lower()
        if alias_key and alias_key in text and target_key in by_name:
            matched_name = target
            alias_matched = alias_key != target_key
            break
    if not matched_name:
        for name in sorted(by_name, key=len, reverse=True):
            if name and name in text:
                matched_name = by_name[name]["name"]
                break
    if not matched_name:
        return None
    item = by_name.get(re.sub(r"\s+", "", matched_name).lower())
    if not item:
        return None
    samples = int(item.get("samples") or 0)
    hits = int(item.get("hits") or 0)
    error = float(item.get("avg_margin_error") or 0.0)
    smoothed_hit_rate = (hits + 1.0) / (samples + 2.0)
    error_factor = 1.0 / (1.0 + error / 10.0)
    sample_factor = min(1.0, 0.45 + samples * 0.11)
    quality = smoothed_hit_rate * error_factor * sample_factor
    # 历史战绩只做有限加分，避免少数历史样本压过时效、样本量和阵营修正。
    weight_multiplier = round(0.97 + quality * 0.18, 3)
    if samples >= 3 and float(item.get("hit_rate") or 0) >= 0.8:
        priority = 3
    elif samples >= 2 and float(item.get("hit_rate") or 0) >= 0.5:
        priority = 2
    elif hits:
        priority = 1
    else:
        priority = 0
    return {
        "name": item.get("name"),
        "hits": hits,
        "samples": samples,
        "hit_rate": item.get("hit_rate"),
        "avg_margin_error": item.get("avg_margin_error"),
        "sample_label": item.get("sample_label"),
        "priority": priority,
        "weight_multiplier": weight_multiplier,
        "brand_alias": alias_matched,
        "current_brand": to_simplified(str(value or "")),
    }
