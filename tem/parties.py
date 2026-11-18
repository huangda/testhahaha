"""候选人政党分布：票仓版图用得到的三块数据。

1. `region_leaders`：每个县市「当前概率最高的候选人」及其政党、颜色 —— 地图着色用。
2. `candidate_parties`：{区域码: {候选人英文名: {party, color, office}}} —— 点击片区看细节用。
3. `distribution`：各党候选人数、参选县市数、市场与融合口径的期望席次分布。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

UNKNOWN_PARTY = "无党籍/未标注"
UNKNOWN_COLOR = "#8794ad"


def _color(party: str) -> str:
    from .profile import PARTY_COLORS

    return PARTY_COLORS.get(party, UNKNOWN_COLOR)


def _region_layout(forecasts: list[dict[str, Any]], national_code: str) -> dict[str, list[dict[str, Any]]]:
    by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in forecasts:
        code = row.get("region_code") or ""
        if not code or code == national_code:
            continue
        by_region[code].append(row)
    for rows in by_region.values():
        rows.sort(key=lambda r: -(r.get("fused_prob") if r.get("fused_prob") is not None else r.get("pm_prob") or 0))
    return by_region


def build(
    forecasts: list[dict[str, Any]],
    profile_fn: Callable[[str, str, str], dict[str, Any] | None],
    *,
    national_code: str = "TW",
) -> dict[str, Any]:
    by_region = _region_layout(forecasts, national_code)

    candidate_parties: dict[str, dict[str, dict[str, Any]]] = {}
    region_leaders: dict[str, dict[str, Any]] = {}
    parties: dict[str, dict[str, Any]] = {}
    seats_fused = 0.0
    seats_market = 0.0
    no_party = 0

    for code, rows in by_region.items():
        cand_map: dict[str, dict[str, Any]] = {}
        region_name = rows[0].get("region_name") or code
        region_party_fused: dict[str, float] = defaultdict(float)
        region_party_market: dict[str, float] = defaultdict(float)
        for rank, row in enumerate(rows):
            zh = row.get("candidate_zh") or ""
            en = row.get("candidate_en") or ""
            profile = profile_fn(region_name, zh, en) or {}
            party = (profile.get("party") or "").strip()
            if not party:
                no_party += 1
            color = profile.get("party_color") or _color(party)
            fused = row.get("fused_prob")
            market = row.get("pm_prob")
            cand_map[en] = {
                "name": zh or en,
                "name_en": en,
                "party": party or UNKNOWN_PARTY,
                "color": color,
                "office": profile.get("office") or profile.get("experience") or "",
                "market": market,
                "fused": fused,
                "rank": rank + 1,
            }
            if fused is not None:
                region_party_fused[party or UNKNOWN_PARTY] += max(fused, 0.0)
            if market is not None:
                region_party_market[party or UNKNOWN_PARTY] += max(market, 0.0)

            bucket = parties.setdefault(
                party or UNKNOWN_PARTY,
                {
                    "party": party or UNKNOWN_PARTY,
                    "color": color,
                    "candidates": 0,
                    "regions": 0,
                    "leads": 0,
                    "seats_fused": 0.0,
                    "seats_market": 0.0,
                    "top": [],
                },
            )
            bucket["candidates"] += 1
            if len(bucket["top"]) < 3 and row.get("fused_prob") is not None:
                bucket["top"].append({"name": zh or en, "region": region_name, "prob": row["fused_prob"]})

        top = rows[0]
        leader_party = cand_map.get(top.get("candidate_en") or "", {}).get("party") or UNKNOWN_PARTY
        leader_color = cand_map.get(top.get("candidate_en") or "", {}).get("color") or _color(leader_party)
        runner = rows[1] if len(rows) > 1 else None
        region_leaders[code] = {
            "region_name": region_name,
            "party": leader_party,
            "color": leader_color,
            "leader": top.get("candidate_zh") or top.get("candidate_en"),
            "leader_en": top.get("candidate_en"),
            "prob": top.get("fused_prob"),
            "market": top.get("pm_prob"),
            "runner_up": (runner.get("candidate_zh") or runner.get("candidate_en")) if runner else "",
            "runner_prob": runner.get("fused_prob") if runner else None,
            "margin": (top.get("fused_prob") or 0) - ((runner.get("fused_prob") if runner else 0) or 0),
            "candidates": len(rows),
            "second_party": (
                cand_map.get(runner.get("candidate_en") or "", {}).get("party") if runner else ""
            ),
        }
        candidate_parties[code] = cand_map
        for party, value in region_party_fused.items():
            bucket = parties.setdefault(
                party,
                {
                    "party": party,
                    "color": _color(party),
                    "candidates": 0,
                    "regions": 0,
                    "leads": 0,
                    "seats_fused": 0.0,
                    "seats_market": 0.0,
                    "top": [],
                },
            )
            bucket["seats_fused"] += value
            seats_fused += value
        for party, value in region_party_market.items():
            parties[party]["seats_market"] += value
            seats_market += value
        parties[leader_party]["leads"] += 1
        for party in region_party_fused:
            if party != leader_party:
                parties[party]["regions"] += 1
        parties[leader_party]["regions"] += 1

    rows_out = []
    for bucket in parties.values():
        bucket["seats_fused"] = round(bucket["seats_fused"], 2)
        bucket["seats_market"] = round(bucket["seats_market"], 2)
        bucket["top"].sort(key=lambda x: -(x.get("prob") or 0))
        rows_out.append(bucket)
    rows_out.sort(key=lambda b: (-b["seats_fused"], -b["candidates"]))

    return {
        "regions": len(by_region),
        "candidates": sum(len(v) for v in candidate_parties.values()),
        "no_party": no_party,
        "seats_fused": round(seats_fused, 2),
        "seats_market": round(seats_market, 2),
        "distribution": rows_out,
        "region_leaders": region_leaders,
        "candidate_parties": candidate_parties,
    }
