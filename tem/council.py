"""22县市议员选举资料。

资料由 `python -m tools.build_council` 生成到 `config/council_2026.json`：
每个选区的应选名额、各党推荐人数、登记人数（中选会），
以及国民党／民进党的具名提名名单（维基百科）。

这个模组只负责读取与做汇总，不打网络。
"""

from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache
from typing import Any

from .paths import CONFIG_DIR


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    path = CONFIG_DIR / "council_2026.json"
    if not path.exists():
        return {"meta": {"note": "尚未生成议员资料"}, "regions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"meta": {"note": "议员资料格式错误"}, "regions": {}}

    for info in (data.get("regions") or {}).values():
        totals: Counter[str] = Counter()
        named: Counter[str] = Counter()
        for dist in info.get("districts") or []:
            counts = dist.get("party_counts") or {}
            if isinstance(counts, dict):
                counts = [{"party": p, "count": c} for p, c in counts.items()]
            counts.sort(key=lambda item: -item["count"])
            dist["party_counts"] = counts
            for item in counts:
                totals[item["party"]] += item["count"]
            for cand in dist.get("candidates") or []:
                named[cand.get("party") or ""] += 1
        districts = info.get("districts") or []
        base = info.get("baseline_2022") or {}
        if isinstance(base.get("seats"), dict):
            base["seats"] = [{"party": p, "seats": v} for p, v in base["seats"].items()]
        info["summary"] = {
            "districts": len(districts),
            "seats": sum(d.get("seats") or 0 for d in districts),
            "registered": sum(d.get("registered") or 0 for d in districts),
            "recommended": sum(d.get("recommended") or 0 for d in districts),
            "unaffiliated": sum(d.get("unaffiliated") or 0 for d in districts),
            "party_totals": [{"party": p, "count": c} for p, c in totals.most_common()],
            "named_total": sum(len(d.get("candidates") or []) for d in districts),
            "named": [{"party": p, "count": c} for p, c in named.most_common()],
        }
    return data


def region(code: str) -> dict[str, Any] | None:
    for name, info in (load().get("regions") or {}).items():
        if info.get("code") == code:
            return {"name": name, **info}
    return None
