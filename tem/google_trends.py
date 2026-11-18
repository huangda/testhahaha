"""Google Trends 台湾每日热搜中的选举相关条目。

使用 Google Trends 官方 RSS（免密钥）扫描台湾当前热搜榜，只保留与
选举、候选人、政党及选务政策有关的条目。结果缓存 30 分钟；该数据是
Google 给出的近似热度，不是绝对搜索次数，也不进入胜率模型。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any

from .http import fetch_bytes
from .paths import DATA_DIR
from .zhconv import to_simplified

RSS_URL = "https://trends.google.com/trending/rss?geo=TW&hl=zh-TW"
CACHE_DIR = DATA_DIR / "cache" / "google_trends"
CACHE_TTL = 30 * 60
SCHEMA_VERSION = 3
HISTORY_RETENTION = 14 * 24 * 60 * 60
SNAPSHOT_RETENTION = 365 * 24 * 60 * 60
TAIPEI = timezone(timedelta(hours=8))
NS = {"ht": "https://trends.google.com/trending/rss"}

ELECTION_TERMS = (
    "選舉", "选举", "九合一", "市長", "市长", "縣長", "县长", "議員", "议员",
    "立委", "候選", "候选", "提名", "徵召", "征召", "民調", "民调", "罷免",
    "罢免", "投票", "選務", "选务", "政黨", "政党", "國民黨", "国民党",
    "民進黨", "民进党", "民眾黨", "民众党", "藍白", "蓝白", "競選", "竞选",
    "造勢", "造势", "輔選", "辅选", "政見", "政见", "縣市長", "县市长",
)

CATEGORY_RULES = (
    ("选举", ELECTION_TERMS),
    ("政治", ("政治", "政府", "總統", "总统", "行政院", "立法院", "國會", "国会", "兩岸", "两岸", "中共", "國防", "国防", "國安", "国安", "外交", "部長", "部长", "市長", "市长", "縣長", "县长")),
    ("社会民生", ("社會", "社会", "治安", "犯罪", "詐騙", "诈骗", "毒品", "車禍", "车祸", "教育", "學校", "学校", "醫療", "医疗", "健保", "托育", "長照", "长照", "勞工", "劳工", "青年", "住宅", "租屋", "房價", "房价", "捷運", "捷运", "交通", "公車", "公车", "停車", "停车")),
    ("经济民生", ("經濟", "经济", "股市", "台股", "匯率", "汇率", "物價", "物价", "薪資", "薪资", "薪水", "就業", "就业", "消費", "消费", "補助", "补助", "優惠", "优惠", "避險", "避险")),
    ("灾害天气", ("颱風", "台风", "豪雨", "地震", "淹水", "災情", "灾情", "救災", "救灾", "天氣", "天气")),
    ("娱乐", ("金鐘", "金钟", "金馬", "金马", "演唱會", "演唱会", "藝人", "艺人", "電影", "电影", "戲劇", "戏剧", "綜藝", "综艺")),
    ("体育", ("棒球", "籃球", "篮球", "足球", "網球", "网球", "賽事", "赛事", "球員", "球员", "冠軍", "冠军")),
)
FOCUS_CATEGORIES = {"选举", "政治", "社会民生", "经济民生", "灾害天气"}
_CANDIDATE_INDEX: dict[str, dict[str, str]] | None = None


def _cache_path() -> Any:
    digest = hashlib.sha1(RSS_URL.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _read_cache(path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _write_cache(path, payload: dict[str, Any]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _traffic_number(text: str | None) -> int:
    digits = re.sub(r"\D+", "", text or "")
    return int(digits) if digits else 0


def _category_of(text: str) -> tuple[str, list[str]]:
    for label, terms in CATEGORY_RULES:
        if any(term in text for term in terms):
            return label, [term for term in terms if term in text]
    return "其他", []


def _candidate_index() -> dict[str, dict[str, str]]:
    global _CANDIDATE_INDEX
    if _CANDIDATE_INDEX is not None:
        return _CANDIDATE_INDEX
    from .roster import load_roster

    index: dict[str, dict[str, str]] = {}
    for region, entries in (load_roster().get("regions") or {}).items():
        for entry in entries or []:
            name = to_simplified(str(entry.get("name") or "")).strip()
            if len(name) < 2:
                continue
            index[name] = {
                "name": name,
                "party": to_simplified(str(entry.get("party") or "")),
                "region": to_simplified(str(region)),
            }
    _CANDIDATE_INDEX = index
    return index


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _available_dates(snapshots: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(item.get("date") or "")
            for item in snapshots
            if item.get("date") and item.get("items")
        },
        reverse=True,
    )


def daily_election_trends(
    *,
    force: bool = False,
    date: str = "",
) -> dict[str, Any]:
    """返回台湾当前热搜；date 参数读取本地已保存的历史日榜。"""
    path = _cache_path()
    cached = _read_cache(path)
    now = time.time()
    requested_date = str(date or "").strip()
    if requested_date:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", requested_date):
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "historical": True,
                "date": requested_date,
                "items": [],
                "available_dates": _available_dates(
                    list((cached or {}).get("snapshots") or [])
                ),
                "error": "历史日期格式应为 YYYY-MM-DD",
            }
        snapshots = list((cached or {}).get("snapshots") or [])
        snapshot = next(
            (
                item for item in snapshots
                if str(item.get("date") or "") == requested_date
            ),
            None,
        )
        if snapshot:
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": True,
                "historical": True,
                "date": requested_date,
                "source": "Google Trends · 台湾每日热搜榜",
                "fetched_at": snapshot.get("captured_at_ts") or 0,
                "fetched_at_iso": snapshot.get("captured_at") or "",
                "scanned": int(snapshot.get("scanned") or 0),
                "matched": len(snapshot.get("items") or []),
                "focus": int(snapshot.get("focus") or 0),
                "items": list(snapshot.get("items") or []),
                "available_dates": _available_dates(snapshots),
                "history_started_at": snapshot.get("captured_at") or "",
                "cached": True,
                "stale": False,
                "note": "历史日榜为系统当天实际保存的 Google Trends 台湾热搜快照。",
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "historical": True,
            "date": requested_date,
            "source": "Google Trends · 台湾每日热搜榜",
            "fetched_at": 0,
            "fetched_at_iso": "",
            "scanned": 0,
            "matched": 0,
            "focus": 0,
            "items": [],
            "available_dates": _available_dates(snapshots),
            "history_started_at": "",
            "cached": True,
            "stale": False,
            "error": "该日期没有已保存的台湾热搜快照",
        }
    if (
        cached
        and cached.get("schema_version") == SCHEMA_VERSION
        and not force
        and now - float(cached.get("fetched_at") or 0) < CACHE_TTL
    ):
        return {**cached, "cached": True, "stale": False}

    try:
        raw = fetch_bytes(
            RSS_URL,
            timeout=30,
            retries=2,
            headers={"Accept": "application/rss+xml,application/xml,text/xml"},
        )
        root = ET.fromstring(raw)
        source_items = root.findall("./channel/item")
        items: list[dict[str, Any]] = []
        for item in source_items:
            title = (item.findtext("title") or "").strip()
            news: list[dict[str, str]] = []
            for entry in item.findall("ht:news_item", NS):
                news.append(
                    {
                        "title": (entry.findtext("ht:news_item_title", default="", namespaces=NS) or "").strip(),
                        "url": (entry.findtext("ht:news_item_url", default="", namespaces=NS) or "").strip(),
                        "source": (entry.findtext("ht:news_item_source", default="", namespaces=NS) or "").strip(),
                    }
                )
            combined = re.sub(r"\s+", "", " ".join([title, *(n["title"] for n in news)]))
            combined_simplified = to_simplified(combined)
            category, matched = _category_of(combined)
            candidates = [
                details for name, details in _candidate_index().items()
                if name in combined_simplified
            ]
            items.append(
                {
                    "term": to_simplified(title),
                    "traffic": _traffic_number(item.findtext("ht:approx_traffic", namespaces=NS)),
                    "traffic_label": (
                        item.findtext("ht:approx_traffic", default="", namespaces=NS) or ""
                    ).strip(),
                    "published_at": (item.findtext("pubDate") or "").strip(),
                    "category": category,
                    "focus": category in FOCUS_CATEGORIES,
                    "candidates": candidates,
                    "matched_terms": [to_simplified(term) for term in matched],
                    "news": [
                        {
                            **entry,
                            "title": to_simplified(entry["title"]),
                            "source": to_simplified(entry["source"]),
                        }
                        for entry in news[:2]
                    ],
                }
            )
        items.sort(key=lambda row: row["traffic"], reverse=True)
        focus_count = sum(1 for row in items if row["focus"])
        now_iso = _iso_now()
        history: dict[str, dict[str, Any]] = {}
        for entry in (cached or {}).get("history") or []:
            term = str(entry.get("term") or "")
            if term:
                history[term] = dict(entry)
        for item in items:
            term = item["term"]
            previous = history.get(term) or {}
            history[term] = {
                **item,
                "first_seen": previous.get("first_seen") or now_iso,
                "last_seen": now_iso,
                "last_seen_ts": now,
                "seen_count": int(previous.get("seen_count") or 0) + 1,
                "max_traffic": max(int(previous.get("max_traffic") or 0), int(item["traffic"] or 0)),
            }
        history_rows = [
            entry for entry in history.values()
            if now - float(entry.get("last_seen_ts") or 0) <= HISTORY_RETENTION
        ]
        history_rows.sort(key=lambda entry: float(entry.get("last_seen_ts") or 0), reverse=True)

        today = datetime.now(TAIPEI).date().isoformat()
        snapshots = [
            dict(entry)
            for entry in (cached or {}).get("snapshots") or []
            if float(entry.get("captured_at_ts") or 0)
            >= now - SNAPSHOT_RETENTION
        ]
        snapshots = [
            entry for entry in snapshots
            if str(entry.get("date") or "") != today
        ]
        snapshots.append(
            {
                "date": today,
                "captured_at": now_iso,
                "captured_at_ts": now,
                "scanned": len(source_items),
                "focus": focus_count,
                "items": items,
            }
        )
        snapshots.sort(
            key=lambda entry: str(entry.get("date") or ""),
            reverse=True,
        )

        def window(hours: int) -> list[dict[str, Any]]:
            cutoff = now - hours * 3600
            rows = [
                entry for entry in history_rows
                if float(entry.get("last_seen_ts") or 0) >= cutoff
            ]
            rows.sort(
                key=lambda entry: (
                    -int(entry.get("max_traffic") or 0),
                    -float(entry.get("last_seen_ts") or 0),
                )
            )
            return rows[:100]

        payload = {
            "schema_version": SCHEMA_VERSION,
            "source": "Google Trends · 台湾每日热搜榜",
            "source_url": "https://trends.google.com/trending?geo=TW&hl=zh-TW",
            "fetched_at": now,
            "fetched_at_iso": now_iso,
            "scanned": len(source_items),
            "matched": len(items),
            "focus": focus_count,
            "items": items,
            "history": history_rows,
            "snapshots": snapshots,
            "available_dates": _available_dates(snapshots),
            "windows": {
                "24": window(24),
                "72": window(72),
                "168": window(168),
            },
            "history_started_at": min(
                (entry.get("first_seen") for entry in history_rows if entry.get("first_seen")),
                default=now_iso,
            ),
            "note": "Google Trends 每日热搜榜的近似热度，不是绝对搜索次数。",
        }
        _write_cache(path, payload)
        return {**payload, "cached": False, "stale": False}
    except Exception as exc:  # noqa: BLE001 - 上游失败时允许使用旧缓存
        if cached:
            return {**cached, "cached": True, "stale": True, "error": str(exc)}
        return {
            "schema_version": SCHEMA_VERSION,
            "source": "Google Trends · 台湾每日热搜榜",
            "fetched_at": now,
            "fetched_at_iso": _iso_now(),
            "scanned": 0,
            "matched": 0,
            "focus": 0,
            "items": [],
            "history": [],
            "snapshots": [],
            "available_dates": [],
            "windows": {"24": [], "72": [], "168": []},
            "history_started_at": _iso_now(),
            "cached": False,
            "stale": False,
            "error": str(exc),
        }
