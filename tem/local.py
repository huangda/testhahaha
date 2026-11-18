"""22县市乡镇市长 / 里长基础资料。

由 `python -m tools.build_local` 生成到 `config/local_2026.json`：
乡镇市长候选人（中选会登记汇总表，含姓名/政党/登记日期），
以及每个乡镇市／区的村里数（里长应选名额）。

里长候选人名册要等选举公报公布后才有，这里只提供结构与总数。
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from .paths import CONFIG_DIR


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    path = CONFIG_DIR / "local_2026.json"
    if not path.exists():
        return {"meta": {"note": "尚未生成乡镇市长／里长资料"}, "regions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"meta": {"note": "资料格式错误"}, "regions": {}}

    for info in (data.get("regions") or {}).values():
        towns = info.get("towns") or []
        towns.sort(key=lambda t: -(t.get("villages") or 0))
        info["towns"] = towns
        info["summary"] = {
            "mayor_seats": sum(1 for t in towns if t.get("candidates")),
            "mayor_candidates": sum(len(t.get("candidates") or []) for t in towns),
            "contested": sum(1 for t in towns if len(t.get("candidates") or []) > 1),
            "villages": info.get("villages_total") or sum(t.get("villages") or 0 for t in towns),
            "villages_counted": sum(t.get("villages") or 0 for t in towns),
        }
    return data
