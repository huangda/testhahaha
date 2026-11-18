"""新聞事件去重：把同一件事的多家轉載合併成一個「事件」。

為什麼要做：425 則新聞裡，同一件事常被十幾家媒體轉載。原本每則各自計入
情感與聲量，等於把一件事放大成十幾件事，讓輿情分數失真（尤其負面事件
轉載特別多，會把縣市情感壓得過低）。

作法：
  1. 標題正規化：去來源後綴、去全形/標點、去「獨家／」「快訊／」這類前綴。
  2. 用字元 bigram 的 Jaccard 相似度比對（中文不必斷詞）。
  3. 只跟「時間相近（預設 72 小時）且至少共用一個縣市或候選人」的事件比對，
     避免同日不同事件被誤併。
  4. 貪婪分群：相似度 ≥ 門檻就歸入既有事件，否則自成新事件。

輸出寫回 news 表的 cluster_id / cluster_size / is_rep 三個欄位；
聚合輿情時只取每群的代表（is_rep=1），等於以「事件」為單位計算。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Iterable

THRESHOLD = 0.52
WINDOW_HOURS = 72
# 同一場民調常被各家寫成不同標題，光看字面相似度抓不到；
# 但「同一天 + 點名同兩位候選人 + 都是民調報導」幾乎可以確定是同一件事。
POLL_RULE_JACCARD = 0.18
POLL_RULE_SHARED_CANDIDATES = 2
SAME_DAY_JACCARD = 0.42
SAME_DAY_CROSS_CANDIDATE_JACCARD = 0.60

_SOURCE_SUFFIX = re.compile(r"\s*[-|｜]\s*[^-|｜]{2,20}$")
_PREFIX = re.compile(r"^(?:獨家|快訊|不斷更新|影|圖|直播|LIVE|最新|即時|專訪|有影)[／/：:、\s]*")
_BRACKET_TAG = re.compile(r"^[【\[][^】\]]{1,10}[】\]]\s*")
_NOISE = re.compile(r"[\s\u3000，。！？、；：「」『』（）()\[\]【】<>《》—–\-·…~〜!?,.:;'\"|｜/\\]+")
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]+")


def normalize_title(title: str) -> str:
    text = (title or "").strip()
    for _ in range(3):
        text = _SOURCE_SUFFIX.sub("", text)
        text = _BRACKET_TAG.sub("", text)
        text = _PREFIX.sub("", text)
    text = _EMOJI.sub("", text)
    text = _NOISE.sub("", text)
    return text


def bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tags(row: dict[str, Any], key: str) -> set[str]:
    """取縣市或候選人標籤。候選人在資料庫裡是 dict 清單，只取名字來比對。"""
    value = row.get(key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    out: set[str] = set()
    for item in value or []:
        if isinstance(item, dict):
            name = item.get("name_zh") or item.get("name") or ""
            if name:
                out.add(str(name))
        elif item:
            out.add(str(item))
    return out


def cluster_news(
    rows: Iterable[dict[str, Any]],
    *,
    threshold: float = THRESHOLD,
    window_hours: int = WINDOW_HOURS,
) -> dict[str, dict[str, Any]]:
    """回傳 {news_id: {cluster_id, cluster_size, is_rep, similarity}}。"""
    items: list[dict[str, Any]] = []
    for row in rows:
        when = _parse(row.get("published_at") or "") or _parse(row.get("first_seen") or "")
        items.append({
            "id": row["id"],
            "when": when,
            "norm": normalize_title(row.get("title") or ""),
            # 標題裡的「民調」可能在【】前綴內被正規化掉，所以用原始標題判斷
            "is_poll": "民調" in (row.get("title") or ""),
            "regions": _tags(row, "regions"),
            "candidates": _tags(row, "candidates"),
            "source": row.get("source_name") or "",
        })
    items.sort(key=lambda i: (i["when"] is None, i["when"]))

    clusters: list[dict[str, Any]] = []
    result: dict[str, dict[str, Any]] = {}
    window = timedelta(hours=window_hours)
    for item in items:
        item["grams"] = bigrams(item["norm"])
        best: dict[str, Any] | None = None
        best_sim = 0.0
        best_ok = False
        for cluster in clusters:
            same_day = bool(
                item["when"] and cluster["when"] and item["when"].date() == cluster["when"].date()
            )
            if item["when"] and cluster["when"] and abs(item["when"] - cluster["when"]) > window:
                continue
            # 兩邊都標了縣市卻完全不重疊 → 不同選區，不合併
            region_overlap = bool(cluster["regions"] & item["regions"])
            if cluster["regions"] and item["regions"] and not region_overlap:
                continue
            shared_candidates = len(cluster["candidates"] & item["candidates"])
            candidate_conflict = bool(
                cluster["candidates"] and item["candidates"]
                and not (cluster["candidates"] & item["candidates"])
            )
            sim = jaccard(item["grams"], cluster["grams"])
            if candidate_conflict and sim < SAME_DAY_CROSS_CANDIDATE_JACCARD:
                continue
            is_same_poll = (
                same_day
                and shared_candidates >= POLL_RULE_SHARED_CANDIDATES
                and item["is_poll"] and cluster["is_poll"]
                and sim >= POLL_RULE_JACCARD
            )
            is_same_day_news = same_day and sim >= SAME_DAY_JACCARD
            if sim >= threshold or is_same_poll or is_same_day_news:
                if not best_ok or sim > best_sim:
                    best, best_sim, best_ok = cluster, sim, True
        if best is not None and best_ok:
            best["members"].append(item)
            # 群代表的標題取最長者，避免代表太短導致後續比對失準
            if len(item["norm"]) > len(best["norm"]):
                best["norm"] = item["norm"]
                best["grams"] = item["grams"]
            best["regions"] |= item["regions"]
            best["candidates"] |= item["candidates"]
            best["is_poll"] = best["is_poll"] or item["is_poll"]
            result[item["id"]] = {
                "cluster_id": best["id"], "cluster_size": 0, "is_rep": 0,
                "similarity": round(best_sim, 3),
            }
        else:
            cluster = {
                "id": f"E{len(clusters) + 1:05d}",
                "when": item["when"],
                "norm": item["norm"],
                "grams": item["grams"],
                "regions": set(item["regions"]),
                "candidates": set(item["candidates"]),
                "is_poll": item["is_poll"],
                "members": [item],
            }
            clusters.append(cluster)
            result[item["id"]] = {
                "cluster_id": cluster["id"], "cluster_size": 0, "is_rep": 1,
                "similarity": 1.0,
            }

    for cluster in clusters:
        size = len(cluster["members"])
        rep_id = cluster["members"][0]["id"]
        for idx, member in enumerate(cluster["members"]):
            entry = result[member["id"]]
            entry["cluster_size"] = size
            entry["is_rep"] = 1 if idx == 0 else 0
    return result


def run_dedupe(*, days: int = 30) -> dict[str, Any]:
    """對近 N 天新聞重新分群並寫回資料庫。"""
    from . import db

    rows = [dict(r) for r in db.fetch_news_since(days=days, limit=5000)]
    mapping = cluster_news(rows)
    db.save_news_clusters(mapping)
    sizes = {}
    for entry in mapping.values():
        sizes[entry["cluster_size"]] = sizes.get(entry["cluster_size"], 0) + 1
    clusters = sum(1 for e in mapping.values() if e["is_rep"])
    merged = sum(e["cluster_size"] - 1 for e in mapping.values() if e["is_rep"])
    return {
        "articles": len(mapping),
        "events": clusters,
        "merged_articles": merged,
        "largest": max(sizes) if sizes else 0,
        "sizes": {k: v for k, v in sorted(sizes.items())[:12]},
    }
