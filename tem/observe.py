"""新文易数（tag.observe.tw）只读补充数据。

这一层只提供外部新闻覆盖与热词趋势，不参与胜率计算，也不把媒体篇数
当成民意支持率。接口响应会缓存到 data/cache/observe/，避免页面切换时间
时重复请求上游。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .http import encode_query, fetch_json, fetch_text
from .paths import DATA_DIR
from .zhconv import to_simplified

BASE_URL = "https://tag.observe.tw/api/v1"
SOURCE_LABEL = "新文易数 tag.observe.tw"
CACHE_DIR = DATA_DIR / "cache" / "observe"
DATE_CACHE = CACHE_DIR / ".article-dates.json"

# 上游只支援事件串 1/3/7/31 天；15 天使用 31 天窗口，界面会明确标注。
PERIOD_DAYS = {24: 1, 72: 3, 168: 7, 360: 31, 720: 31}
THREAD_TTL = 30 * 60
TREND_TTL = 10 * 60
GROUP_TTL = 60 * 60
THREAD_DETAIL_TTL = 30 * 60

_lock = threading.Lock()
_date_lock = threading.Lock()
_TAIWAN_CONTEXT_TERMS = (
    "台灣", "台湾", "全島", "全岛", "台北", "新北", "桃園", "桃园", "台中",
    "台南", "高雄", "基隆", "新竹", "苗栗", "彰化", "南投", "雲林", "云林",
    "嘉義", "嘉义", "屏東", "屏东", "宜蘭", "宜兰", "花蓮", "花莲", "台東",
    "台东", "澎湖", "金門", "金门", "連江", "连江",
)
_TAIWAN_PARTY_TERMS = (
    "中國國民黨", "中国国民党", "國民黨", "国民党", "民主進步黨", "民主进步党",
    "民進黨", "民进党", "台灣民眾黨", "台湾民众党", "民眾黨", "民众党", "藍白", "蓝白",
)
_FOREIGN_CONTEXT_TERMS = (
    "美國", "美国", "美軍", "美军", "伊朗", "以色列", "烏克蘭", "乌克兰",
    "俄羅斯", "俄罗斯", "中東", "中东", "波斯灣", "波斯湾", "荷莫茲", "霍尔木兹",
    "川普", "特朗普", "白宮", "白宫", "北韓", "朝鲜", "日本首相", "英國", "英国",
)
_STRONG_TAG_TERMS = (
    "九合一", "直轄市長", "直辖市长", "縣市長", "县市长", "市長選舉",
    "市长选举", "縣長選舉", "县长选举", "地方選舉", "地方选举", "參選",
    "参选", "候選", "候选", "提名", "徵召", "征召", "初選", "初选",
    "民調", "民调", "競選", "竞选", "造勢", "造势", "輔選", "辅选",
    "政見", "政见", "罷免", "罢免", "看板",
)
_STRICT_LOCAL_ELECTION_TERMS = (
    "九合一",
    "直轄市長選舉", "直辖市长选举",
    "縣市長選舉", "县市长选举",
    "市長選舉", "市长选举",
    "縣長選舉", "县长选举",
    "地方選舉", "地方选举",
    "縣市議員選舉", "县市议员选举",
    "議員選舉", "议员选举",
    "鄉鎮市長選舉", "乡镇市长选举",
    "里長選舉", "里长选举",
    "候選人", "候选人",
    "登記參選", "登记参选",
    "抽籤號次", "抽签号次",
    "競選總部", "竞选总部",
    "造勢", "造势",
    "站台",
    "輔選", "辅选",
    "拜票",
    "掃街", "扫街",
    "政見", "政见",
    "選戰", "选战",
    "選罷法", "选罢法",
    "賄選", "贿选",
    "買票", "买票",
    "投票日",
    "投票所",
    "開票", "开票",
    "中選會", "中选会",
    "選務", "选务",
)
_SELF_SUFFICIENT_ELECTION_TERMS = (
    "九合一",
    "直轄市長選舉", "直辖市长选举",
    "縣市長選舉", "县市长选举",
    "市長選舉", "市长选举",
    "縣長選舉", "县长选举",
    "地方選舉", "地方选举",
    "縣市議員選舉", "县市议员选举",
    "議員選舉", "议员选举",
    "鄉鎮市長選舉", "乡镇市长选举",
    "里長選舉", "里长选举",
    "登記參選", "登记参选",
    "抽籤號次", "抽签号次",
    "號次", "号次",
    "選罷法", "选罢法",
    "賄選", "贿选",
    "投票日",
    "投票所",
    "開票", "开票",
    "中選會", "中选会",
    "選務", "选务",
)
_LOCAL_POSITION_TERMS = (
    "直轄市長", "直辖市长", "縣市長", "县市长", "市長", "市长",
    "縣長", "县长", "縣市議員", "县市议员", "議員", "议员",
    "鄉鎮市長", "乡镇市长", "里長", "里长",
)
_MAYOR_GROUP = "2026 22县市候选人"
_IRRELEVANT_GRAPH_GROUP_TERMS = (
    "艺人",
    "歌手",
    "乐团",
    "乐坛",
    "娱乐",
    "外语艺人",
    "电视电影",
)
_CANDIDATE_ELECTION_CONTEXT = (
    "民調", "民调", "支持度", "看好度", "領先", "领先", "落後", "落后",
    "參選", "参选", "提名", "徵召", "征召", "初選", "初选",
    "競選", "竞选", "站台", "造勢", "造势", "輔選", "辅选",
    "政見", "政见", "選戰", "选战", "對手", "对手",
    "賄選", "贿选", "選罷法", "选罢法", "黨內", "党内",
)
_YEAR_PREFIX = re.compile(r"^\s*(20\d{2})")


def _is_historical_label(label: str, current_year: int) -> bool:
    """隐藏早于本届选举年的平台分组/标签，避免历史职位继续参与分类。"""
    text = to_simplified(str(label or "")).strip()
    match = _YEAR_PREFIX.match(text)
    if match and int(match.group(1)) < current_year:
        return True
    return any(term in text for term in ("歷屆", "历届", "上屆", "上届", "前任", "前屆", "前届"))

def _cache_path(url: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _read_cache(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) and "data" in payload else None


def _write_cache(path: Path, url: str, data: Any, fetched_at: float) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    body = {
        "url": url,
        "fetched_at": fetched_at,
        "fetched_at_iso": datetime.fromtimestamp(
            fetched_at, tz=timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "data": data,
    }
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _write_failure(
    path: Path,
    url: str,
    error: str,
    failed_at: float,
    *,
    data: Any = None,
) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    body = {
        "url": url,
        "fetched_at": failed_at,
        "fetched_at_iso": datetime.fromtimestamp(
            failed_at, tz=timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "data": data,
        "error": error,
    }
    tmp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def clear_cache() -> int:
    """完整刷新时清空外部补充缓存，下一次进入舆情页会重新读取上游。"""
    with _lock:
        paths = list(CACHE_DIR.glob("*.json")) if CACHE_DIR.exists() else []
        removed = 0
        for path in paths:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed


def article_dates(urls: list[str]) -> dict[str, str]:
    """批量读取文章发布日期；优先缓存，失败时返回空字符串。"""
    clean_urls = list(dict.fromkeys(
        str(url or "").strip()
        for url in urls
        if str(url or "").strip().startswith(("http://", "https://"))
    ))[:12]
    if not clean_urls:
        return {}
    with _date_lock:
        try:
            cache = json.loads(DATE_CACHE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cache = {}
    now = time.time()
    output: dict[str, str] = {}
    pending: list[str] = []
    for url in clean_urls:
        entry = cache.get(url) if isinstance(cache.get(url), dict) else {}
        date_value = str(entry.get("date") or "")
        age = now - float(entry.get("checked_at") or 0)
        if date_value or age < 86400:
            output[url] = date_value
        else:
            pending.append(url)

    def fetch_date(url: str) -> tuple[str, str, float]:
        checked_at = time.time()
        try:
            raw = fetch_text(
                url,
                timeout=7,
                retries=1,
                headers={"Accept": "text/html,application/xhtml+xml"},
            )
        except Exception:  # noqa: BLE001 - 单篇日期失败不影响事件详情
            return url, "", checked_at
        patterns = (
            r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|datePublished)["\'][^>]+'
            r'content=["\'](20\d{2}-\d{1,2}-\d{1,2})',
            r'<meta[^>]+content=["\'](20\d{2}-\d{1,2}-\d{1,2})["\'][^>]+'
            r'(?:property|name)=["\'](?:article:published_time|datePublished)["\']',
            r'"datePublished"\s*:\s*"(20\d{2}-\d{1,2}-\d{1,2})',
        )
        for pattern in patterns:
            match = re.search(pattern, raw, re.I | re.S)
            if match:
                try:
                    return url, datetime.strptime(
                        match.group(1), "%Y-%m-%d"
                    ).date().isoformat(), checked_at
                except ValueError:
                    continue
        return url, "", checked_at

    if pending:
        with ThreadPoolExecutor(max_workers=min(6, len(pending))) as pool:
            for url, date_value, checked_at in pool.map(fetch_date, pending):
                output[url] = date_value
                cache[url] = {"date": date_value, "checked_at": checked_at}
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temp = DATE_CACHE.with_suffix(".tmp")
            temp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            temp.replace(DATE_CACHE)
        except OSError:
            pass
    return {url: output.get(url, "") for url in clean_urls}


def _cached_json(
    settings: Any,
    endpoint: str,
    params: dict[str, Any],
    *,
    ttl: int,
) -> tuple[Any, dict[str, Any]]:
    """读取新鲜缓存；否则请求上游。失败时优先返回旧缓存。"""
    query = encode_query(params)
    url = f"{BASE_URL}{endpoint}" + (f"?{query}" if query else "")
    path = _cache_path(url)
    now = time.time()

    with _lock:
        cached = _read_cache(path)
    if cached:
        age = now - float(cached.get("fetched_at") or 0)
        if cached.get("error") and age < min(ttl, 120):
            return cached.get("data"), {
                "state": "stale" if cached.get("data") is not None else "error",
                "fetched_at": cached.get("fetched_at_iso"),
                "url": url,
                "warning": cached.get("error"),
                "negative_cache": True,
            }
        if not cached.get("error") and age < ttl:
            return cached.get("data"), {
                "state": "cache",
                "fetched_at": cached.get("fetched_at_iso"),
                "url": url,
            }

    headers = {"Accept": "application/json", "User-Agent": "TEM-election-monitor/1.0"}
    api_key = str(getattr(settings, "tag_observe_api_key", "") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key

    try:
        data = fetch_json(url, headers=headers, timeout=25, retries=2)
        fetched_at = time.time()
        with _lock:
            _write_cache(path, url, data, fetched_at)
        return data, {
            "state": "live",
            "fetched_at": datetime.fromtimestamp(
                fetched_at, tz=timezone.utc
            ).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "url": url,
        }
    except Exception as exc:  # noqa: BLE001 - 外部服务失败不能影响本地舆情
        error = f"{type(exc).__name__}: {exc}"
        if cached:
            try:
                with _lock:
                    _write_failure(
                        path,
                        url,
                        error,
                        time.time(),
                        data=cached.get("data"),
                    )
            except OSError:
                pass
            return cached.get("data"), {
                "state": "stale",
                "fetched_at": cached.get("fetched_at_iso"),
                "url": url,
                "warning": error,
            }
        try:
            with _lock:
                _write_failure(path, url, error, time.time())
        except OSError:
            pass
        return None, {
            "state": "error",
            "fetched_at": None,
            "url": url,
            "warning": error,
        }


def _roster_candidate_names() -> set[str]:
    names: set[str] = set()
    try:
        from .roster import load_roster

        for entries in (load_roster().get("regions") or {}).values():
            for entry in entries or []:
                name = to_simplified(str(entry.get("name") or "")).strip()
                if name:
                    names.add(name)
    except Exception:  # noqa: BLE001 - 名册不可用時仍保留市場姓名
        pass
    return names


def _election_terms(settings: Any) -> tuple[set[str], set[str]]:
    candidates = {
        to_simplified(str(name)).strip()
        for name in getattr(settings, "name_map", {}).values()
        if name and not str(name).startswith("__")
    }
    candidates.update(_roster_candidate_names())
    regions: set[str] = set()
    for region in getattr(settings, "regions", []):
        regions.add(to_simplified(region.name))
        regions.update(to_simplified(name) for name in region.aliases)
    nationwide = getattr(settings, "nationwide", None)
    if nationwide:
        regions.add(to_simplified(nationwide.name))
        regions.update(to_simplified(name) for name in nationwide.aliases)
    regions = {
        name for name in regions
        if len(name) >= 2 and name not in {"全国", "全國", "台湾", "台灣"}
    }
    return candidates, regions


def _county_mayor_candidates() -> list[dict[str, str]]:
    """本届22县市全部正式县市长候选人，用于关系图候选人中心视图。"""
    result: list[dict[str, str]] = []
    try:
        from .roster import load_roster

        regions = load_roster().get("regions") or {}
    except Exception:  # noqa: BLE001 - 名册不可用时保留上游节点
        return result
    for region, entries in regions.items():
        region_name = to_simplified(str(region or ""))
        for entry in entries or []:
            party = to_simplified(str(entry.get("party") or ""))
            name = to_simplified(str(entry.get("name") or "")).strip()
            if name:
                result.append({
                    "name": name,
                    "party": party,
                    "region": region_name,
                })
    return result


def _roster_mayor_parties() -> dict[str, str]:
    """本届县市长正式候选人的政党对照，用于同名人物消歧。"""
    result: dict[str, str] = {}
    try:
        from .roster import load_roster

        regions = load_roster().get("regions") or {}
    except Exception:  # noqa: BLE001
        return result
    for entries in regions.values():
        for entry in entries or []:
            name = to_simplified(str(entry.get("name") or "")).strip()
            party = to_simplified(str(entry.get("party") or "")).strip()
            if name and party:
                result.setdefault(name, party)
    return result


def _irrelevant_graph_group(name: str) -> bool:
    compact = to_simplified(str(name or ""))
    return any(term in compact for term in _IRRELEVANT_GRAPH_GROUP_TERMS)


def _thread_relevant(text: str, candidates: set[str], regions: set[str]) -> bool:
    text = to_simplified(text)
    strict_hits = sum(1 for term in _STRICT_LOCAL_ELECTION_TERMS if term in text)
    self_sufficient_hits = sum(
        1 for term in _SELF_SUFFICIENT_ELECTION_TERMS if term in text
    )
    matched_candidates = [
        term for term in candidates if len(term) >= 2 and term in text
    ]
    candidate_hits = len(matched_candidates)
    region_hits = sum(1 for term in regions if term in text)
    local_position_hits = sum(1 for term in _LOCAL_POSITION_TERMS if term in text)
    candidate_context_hits = sum(
        1 for term in _CANDIDATE_ELECTION_CONTEXT if term in text
    )
    taiwan_hits = sum(1 for term in _TAIWAN_CONTEXT_TERMS if term in text)
    party_hits = sum(1 for term in _TAIWAN_PARTY_TERMS if term in text)
    foreign_hits = sum(1 for term in _FOREIGN_CONTEXT_TERMS if term in text)

    # 国际新闻若要保留，必须同时出现明确的台湾地方选举锚点。
    if foreign_hits and not (taiwan_hits or region_hits or candidate_hits or party_hits):
        return False
    # 本届正式候选人无论事件是否直接使用“选举”字样，都应保留。
    # 两字姓名重名风险较高，仍要求县市、政党或选举语境辅助确认。
    if any(len(name) >= 3 for name in matched_candidates):
        return True
    if candidate_hits and (
        self_sufficient_hits
        or candidate_context_hits
        or region_hits
        or party_hits
    ):
        return True
    if self_sufficient_hits:
        return True
    if strict_hits and (candidate_hits or region_hits or local_position_hits or party_hits):
        return True
    # 候选人必须同时处于民调、参选、竞选、站台、政见、选罢法等选举语境。
    if candidate_hits and candidate_context_hits:
        return True
    # 没有点名正式县市长候选人时，至少要有地方职位 + 县市/政党 + 民调语境。
    if (
        local_position_hits
        and region_hits
        and candidate_context_hits
        and not any(term in text for term in ("立法院", "立院", "立法委员", "立委"))
    ):
        return True
    return False


def _thread_duration_hours(thread: dict[str, Any]) -> float:
    try:
        first = datetime.fromisoformat(
            str(thread.get("first_time") or "").replace("Z", "+00:00")
        )
        last = datetime.fromisoformat(
            str(thread.get("last_time") or "").replace("Z", "+00:00")
        )
        return max(0.0, (last - first).total_seconds() / 3600)
    except (TypeError, ValueError):
        return float(thread.get("hours") or 0)


def _tag_relevant(tag: str, candidates: set[str], regions: set[str]) -> bool:
    tag = to_simplified(tag)
    if any(term in tag for term in _STRONG_TAG_TERMS):
        return True
    if tag in candidates:
        return True
    return any(len(term) >= 3 and term in tag for term in candidates)


def _ranking_window(payload: Any) -> dict[str, Any]:
    """读取上游排行快照的实际统计窗口。"""
    snapshot = payload.get("snapshot") if isinstance(payload, dict) else {}
    if not isinstance(snapshot, dict):
        return {}
    basis = snapshot.get("basis") if isinstance(snapshot.get("basis"), dict) else {}
    start = str(basis.get("coverageFrom") or basis.get("validFrom") or "")
    end = str(snapshot.get("hourStart") or snapshot.get("computedAt") or "")
    hours: float | None = None
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        hours = max(0.0, (end_dt - start_dt).total_seconds() / 3600)
    except (TypeError, ValueError):
        pass
    return {
        "start": start,
        "end": end,
        "hours": round(hours, 1) if hours is not None else None,
        "days": round(hours / 24, 1) if hours is not None else None,
        "article_count": int(snapshot.get("articleCount") or 0),
        "media_count": int(snapshot.get("mediaCount") or 0),
    }


def _sparkline(entry: dict[str, Any]) -> list[float]:
    points = []
    for item in entry.get("trend") or []:
        value = item.get("score") if isinstance(item, dict) else None
        if value is not None:
            points.append(round(float(value), 3))
    if len(points) <= 14:
        return points
    indexes = sorted({round(index * (len(points) - 1) / 13) for index in range(14)})
    return [points[index] for index in indexes]


def _trend_points(entry: dict[str, Any]) -> list[dict[str, Any]]:
    points = []
    for item in entry.get("trend") or []:
        if not isinstance(item, dict) or item.get("score") is None:
            continue
        points.append({
            "t": item.get("t"),
            "value": round(float(item.get("score") or 0), 3),
            "count": item.get("count"),
        })
    return points


def normalize_thread(thread: dict[str, Any]) -> dict[str, Any]:
    coverage = thread.get("coverage") or {}
    outlets = coverage.get("outlets") or []
    share = coverage.get("share") or {}
    camps = coverage.get("camps") or {}
    major_tags = [
        to_simplified(str(tag))
        for tag in (thread.get("majorTags") or [])
        if tag
    ]
    news = []
    for item in thread.get("news") or []:
        if not isinstance(item, dict):
            continue
        news.append({
            "id": item.get("id"),
            "title": to_simplified(str(item.get("title") or ""))[:180],
            "media": str(item.get("media") or ""),
            "camp": str(item.get("camp") or "other"),
            "url": str(item.get("url") or ""),
            "published_at": item.get("publishedAt") or item.get("published_at") or "",
        })
    news.sort(key=lambda item: str(item.get("published_at") or "9999"))
    title = to_simplified(str(thread.get("maxTag") or "未命名事件"))
    if not title and major_tags:
        title = major_tags[0]
    outlet_rows = []
    seen_outlets = set()
    outlet_camps = {"blue": 0, "green": 0, "other": 0}
    for item in outlets:
        if not isinstance(item, dict):
            continue
        media = str(item.get("media") or "").strip()
        camp = str(item.get("camp") or "other")
        if not media or media in seen_outlets:
            continue
        seen_outlets.add(media)
        outlet_rows.append({"media": media, "camp": camp})
        if camp not in outlet_camps:
            camp = "other"
        outlet_camps[camp] += 1
    return {
        "id": thread.get("id"),
        "title": title,
        "tags": major_tags[:8],
        "first_time": thread.get("firstTime"),
        "last_time": thread.get("lastTime"),
        "hours": thread.get("hours"),
        "weight": round(
            float(
                thread.get("storyWeight")
                or thread.get("dayWeight")
                or thread.get("weight")
                or 0
            ),
            3,
        ),
        "best_rank": thread.get("bestRank"),
        "coverage": {
            "articles": int(coverage.get("articles") or 0),
            "media_count": len(outlet_rows),
            "outlets": outlet_rows,
            "camps": outlet_camps if outlet_rows else {
                "blue": int(camps.get("blue") or 0),
                "green": int(camps.get("green") or 0),
                "other": int(camps.get("other") or 0),
            },
            # 上游份额是蓝/绿媒体覆盖率，不是民调支持率。
            "share": {
                "blue": share.get("blue"),
                "green": share.get("green"),
            },
            "lean": coverage.get("lean"),
            "blindspot": coverage.get("blindspot") or [],
        },
        "news": news[:12],
        "source": SOURCE_LABEL,
    }


def normalize_trend(entry: dict[str, Any]) -> dict[str, Any]:
    history = entry.get("history") or {}
    normalized = entry.get("normalized")
    previous = history.get("24")
    change = None
    if normalized is not None and previous is not None:
        change = round(float(normalized) - float(previous), 3)
    related = []
    for item in (entry.get("related") or [])[:16]:
        if isinstance(item, dict) and item.get("tag"):
            related.append({
                "tag": to_simplified(str(item.get("tag"))),
                "count": item.get("count"),
                "share": item.get("share"),
            })
    return {
        "tag": to_simplified(str(entry.get("tag") or "")),
        "rank": entry.get("rank"),
        "rank_24h": entry.get("rank24h"),
        "score": round(float(entry.get("score") or 0), 3),
        "normalized": round(float(normalized), 3) if normalized is not None else None,
        "count": int(entry.get("count") or 0),
        "burst": round(float(entry.get("burst") or 0), 3),
        "change_24h": change,
        "is_new": bool(entry.get("new")),
        "sparkline": _sparkline(entry),
        "trend_points": _trend_points(entry),
        "related": related,
        "source": SOURCE_LABEL,
    }


def _tag_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = item
        elif isinstance(item, (list, tuple)) and item:
            name = item[0]
        elif isinstance(item, dict):
            name = item.get("tag") or item.get("name") or item.get("label")
        else:
            continue
        name = to_simplified(str(name or "")).strip()
        if name and name not in out:
            out.append(name)
    return out


def normalize_groups(payload: Any, *, current_year: int = 2026) -> list[dict[str, Any]]:
    """把平台标签分组整理为稳定的名称与成员标签。"""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        nested = item.get("group")
        if isinstance(nested, dict):
            item = {**nested, **{key: value for key, value in item.items() if key != "group"}}
        name = to_simplified(
            str(item.get("name") or item.get("title") or item.get("label") or "")
        ).strip()
        if not name or name in seen:
            return
        if _is_historical_label(name, current_year):
            return
        tags: list[str] = []
        for key in ("top", "tags", "topTags", "members", "sampleTags", "keywords", "items"):
            tags.extend(_tag_list(item.get(key)))
        tags = list(dict.fromkeys(tags))[:24]
        raw_tag_count = item.get("tags")
        tag_count = int(raw_tag_count) if isinstance(raw_tag_count, int) else int(
            item.get("tagCount")
            or item.get("tag_count")
            or item.get("count")
            or len(tags)
        )
        seen.add(name)
        found.append({
            "id": item.get("id") or item.get("slug") or name,
            "name": name,
            "description": to_simplified(
                str(item.get("description") or item.get("summary") or "")
            ).strip()[:180],
            "tags": tags,
            "tag_count": tag_count,
            "active": item.get("active"),
            "recent": item.get("recent"),
        })

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    add(item)
                else:
                    visit(item, depth + 1)
            return
        if isinstance(value, dict):
            handled = False
            for key in ("groups", "items", "overview", "data", "results"):
                if key in value:
                    visit(value[key], depth + 1)
                    handled = True
            if not handled and depth > 0:
                for name, child in value.items():
                    if isinstance(child, list) and any(
                        isinstance(entry, (str, dict, list, tuple)) for entry in child
                    ):
                        add({"name": name, "tags": child})
                    elif isinstance(child, dict):
                        visit(child, depth + 1)

    visit(payload)
    found.sort(key=lambda item: (-int(item.get("tag_count") or 0), item["name"]))
    return found[:60]


def _node_kind(tag: str, candidates: set[str], regions: set[str]) -> str:
    if "国民党" in tag or "民进党" in tag or "民众党" in tag or "党" in tag:
        return "政党"
    if tag in candidates:
        return "候选人"
    if tag in regions:
        return "县市"
    if any(term in tag for term in _STRONG_TAG_TERMS):
        return "选举议题"
    return "相关议题"


def build_topic_graph(
    trends: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    candidates: set[str],
    regions: set[str],
    *,
    current_year: int = 2026,
) -> dict[str, Any]:
    """从热词榜的共同标签关系生成小规模关系图。"""
    groups_by_tag: dict[str, list[str]] = {}
    for group in groups:
        for tag in group.get("tags") or []:
            groups_by_tag.setdefault(tag, []).append(group["name"])

    primary = [
        item for item in trends
        if not _is_historical_label(str(item.get("tag") or ""), current_year)
    ][:12]
    related_values: dict[str, float] = {}
    edges: list[dict[str, Any]] = []
    for trend in primary:
        for related in trend.get("related") or []:
            target = str(related.get("tag") or "")
            if not target:
                continue
            if _is_historical_label(target, current_year):
                continue
            related_values[target] = related_values.get(target, 0.0) + float(
                related.get("count") or 0
            )
            edges.append({
                "source": trend["tag"],
                "target": target,
                "value": int(related.get("count") or 0),
                "share": round(float(related.get("share") or 0), 3),
            })

    primary_names = {item["tag"] for item in primary}
    extras = [
        tag for tag, _ in sorted(related_values.items(), key=lambda pair: -pair[1])
        if tag not in primary_names
    ][:14]
    allowed = primary_names | set(extras)
    nodes = []
    trend_map = {item["tag"]: item for item in primary}
    for tag in list(trend_map) + extras:
        trend = trend_map.get(tag)
        kind = _node_kind(tag, candidates, regions)
        memberships = list(dict.fromkeys(groups_by_tag.get(tag, [])))
        primary_group = next(
            (name for name in memberships if "党" in name),
            memberships[0] if memberships else kind,
        )
        nodes.append({
            "id": tag,
            "tag": tag,
            "kind": kind,
            "group": primary_group,
            "groups": memberships or [kind],
            "count": int(trend.get("count") or 0) if trend else int(related_values.get(tag) or 0),
            "score": float(trend.get("score") or 0) if trend else 0.0,
            "burst": float(trend.get("burst") or 0) if trend else 0.0,
            "primary": bool(trend),
        })

    edge_keys: set[tuple[str, str]] = set()
    clean_edges = []
    for edge in edges:
        if edge["source"] not in allowed or edge["target"] not in allowed:
            continue
        key = tuple(sorted((edge["source"], edge["target"])))
        if key in edge_keys:
            continue
        edge_keys.add(key)
        clean_edges.append(edge)
    clean_edges.sort(key=lambda item: -item["value"])
    return {
        "nodes": nodes,
        "edges": clean_edges[:42],
        "groups": [
            {"name": group["name"], "tags": group["tags"]}
            for group in groups
        ],
        "note": "关系强弱来自新文易数同一新闻中的共同标签；只表示议题关联，不表示因果或支持度。",
    }


def _timeline_points(payload: Any) -> list[dict[str, Any]]:
    candidates: list[list[dict[str, Any]]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(value, list):
            points = []
            for index, item in enumerate(value):
                if not isinstance(item, dict):
                    continue
                when = next(
                    (
                        item.get(key)
                        for key in ("t", "time", "date", "bucket", "timestamp")
                        if item.get(key) not in (None, "")
                    ),
                    None,
                )
                if when is None:
                    continue
                value_num = next(
                    (
                        item.get(key)
                        for key in ("weight", "score", "articles", "articleCount", "count", "hourlyCount")
                        if item.get(key) is not None
                    ),
                    None,
                )
                if value_num is None and all(
                    item.get(key) is not None for key in ("blue", "green", "other")
                ):
                    value_num = sum(float(item.get(key) or 0) for key in ("blue", "green", "other"))
                try:
                    number = float(value_num)
                except (TypeError, ValueError):
                    continue
                points.append({
                    "t": str(when),
                    "value": round(number, 3),
                    "rank": item.get("rank") or item.get("bestRank"),
                })
            if points:
                candidates.append(points)
                return
            for child in value:
                visit(child, depth + 1)
            return
        if isinstance(value, dict):
            for key in ("points", "series", "timeline", "trend", "data", "items"):
                if key in value:
                    visit(value[key], depth + 1)

    visit(payload)
    if not candidates:
        return []
    points = max(candidates, key=len)
    if all(str(point["t"]).startswith(("19", "20")) for point in points):
        points.sort(key=lambda point: point["t"])
    if len(points) > 120:
        indexes = sorted({
            round(index * (len(points) - 1) / 119)
            for index in range(120)
        })
        points = [points[index] for index in indexes]
    return points


def _coverage_detail(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    source = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else payload
    if not isinstance(source, dict) or not (
        "outlets" in source or "share" in source or "articles" in source
    ):
        return None
    raw_outlets = source.get("byOutlet") or source.get("outlets") or []
    outlets = []
    if isinstance(raw_outlets, list):
        seen_outlets = set()
        for item in raw_outlets:
            if not isinstance(item, dict):
                continue
            media = str(item.get("media") or "")
            if not media or media in seen_outlets:
                continue
            seen_outlets.add(media)
            article_value = item.get("articles")
            article_count = (
                int(article_value)
                if isinstance(article_value, int)
                else len(article_value) if isinstance(article_value, list)
                else 0
            )
            outlets.append({
                "media": media,
                "title": to_simplified(str(item.get("title") or item.get("media") or "")),
                "camp": str(item.get("camp") or "other"),
                "articles": article_count,
            })
    camps: dict[str, int] = {"blue": 0, "green": 0, "other": 0}
    article_camps: dict[str, int] = {"blue": 0, "green": 0, "other": 0}
    raw_camps = source.get("camps") or {}
    if isinstance(raw_camps, dict):
        for camp in camps:
            camps[camp] = int(raw_camps.get(camp) or 0)
    elif isinstance(raw_camps, list):
        for item in raw_camps:
            if not isinstance(item, dict):
                continue
            camp = str(item.get("camp") or "other")
            if camp in camps:
                camps[camp] = int(item.get("outlets") or 0)
                article_camps[camp] = int(item.get("articles") or 0)
    if outlets:
        camps = {"blue": 0, "green": 0, "other": 0}
        article_camps = {"blue": 0, "green": 0, "other": 0}
        for item in outlets:
            camp = str(item.get("camp") or "other")
            if camp not in camps:
                camp = "other"
            camps[camp] += 1
            article_camps[camp] += int(item.get("articles") or 0)
    share = source.get("share") or {}
    if not share and sum(article_camps.values()):
        blue_green = article_camps["blue"] + article_camps["green"]
        if blue_green:
            share = {
                "blue": round(article_camps["blue"] / blue_green * 100, 1),
                "green": round(article_camps["green"] / blue_green * 100, 1),
            }
    return {
        "articles": int(source.get("articles") or 0),
        "from": source.get("from"),
        "to": source.get("to"),
        "media_count": len(outlets)
        if outlets
        else int(source.get("outlets") or 0)
        if isinstance(source.get("outlets"), int)
        else len(raw_outlets) if isinstance(raw_outlets, list) else 0,
        "outlets": outlets,
        "camps": camps,
        "article_camps": article_camps,
        "share": {"blue": share.get("blue"), "green": share.get("green")},
        "blindspot": source.get("blindspot") or [],
    }


def _coverage_news(payload: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    """兼容旧接口：从覆盖明细整理报道，不作为事件代表新闻使用。"""
    if not isinstance(payload, dict):
        return []
    source = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else payload
    by_outlet = source.get("byOutlet") or []
    if not isinstance(by_outlet, list):
        return []
    found: dict[str, dict[str, Any]] = {}
    for outlet in by_outlet:
        if not isinstance(outlet, dict):
            continue
        for article in outlet.get("articles") or []:
            if not isinstance(article, dict):
                continue
            title = to_simplified(str(article.get("title") or "")).strip()
            url = str(article.get("url") or "").strip()
            if not title or (not url and title in found):
                continue
            key = url or title
            if key in found:
                continue
            found[key] = {
                "id": article.get("id"),
                "title": title[:200],
                "url": url,
                "media": str(article.get("media") or outlet.get("media") or ""),
                "media_title": to_simplified(
                    str(outlet.get("title") or outlet.get("media") or "")
                ),
                "camp": str(article.get("camp") or outlet.get("camp") or "other"),
                "published_at": article.get("publishedAt") or article.get("published_at") or "",
                "summary": to_simplified(
                    str(article.get("description") or article.get("summary") or "")
                )[:300],
            }
    items = sorted(found.values(), key=lambda item: str(item.get("published_at") or "9999"))
    if len(items) > limit:
        indexes = sorted({
            round(index * (len(items) - 1) / max(1, limit - 1))
            for index in range(limit)
        })
        items = [items[index] for index in indexes]
    return items


def _hourly_news(
    payload: Any,
    coverage: dict[str, Any] | None,
    *,
    limit: int = 36,
) -> list[dict[str, Any]]:
    """整理事件逐小时代表新闻，保留同一文章的首次出现时间。"""
    if not isinstance(payload, dict):
        return []
    hours = payload.get("hours") or []
    if not isinstance(hours, list):
        return []
    outlets = (coverage or {}).get("outlets") or []
    camp_by_media = {
        str(item.get("media") or ""): str(item.get("camp") or "other")
        for item in outlets
        if isinstance(item, dict) and item.get("media")
    }
    title_by_media = {
        str(item.get("media") or ""): str(item.get("title") or "")
        for item in outlets
        if isinstance(item, dict) and item.get("media")
    }
    ordered_hours = sorted(
        (item for item in hours if isinstance(item, dict)),
        key=lambda item: str(item.get("hourStart") or ""),
    )
    found: dict[str, dict[str, Any]] = {}
    for hour in ordered_hours:
        hour_start = str(hour.get("hourStart") or "")
        for item in hour.get("news") or []:
            if not isinstance(item, dict):
                continue
            title = to_simplified(str(item.get("title") or "")).strip()
            media = str(item.get("media") or "").strip()
            url = str(item.get("url") or "").strip()
            key = url or f"{media}|{title}"
            if not title or key in found:
                continue
            found[key] = {
                "id": item.get("id"),
                "title": title[:200],
                "url": url,
                "media": media,
                "media_title": title_by_media.get(media, media),
                "camp": camp_by_media.get(media, "other"),
                "published_at": hour_start,
                "hour": hour_start,
            }
    items = list(found.values())
    if len(items) > limit:
        indexes = sorted({
            round(index * (len(items) - 1) / max(1, limit - 1))
            for index in range(limit)
        })
        items = [items[index] for index in indexes]
    return items


def get_thread_detail(settings: Any, thread_id: int) -> dict[str, Any]:
    """按需读取单个事件串的时序与覆盖详情。"""
    if thread_id <= 0:
        raise ValueError("事件组 ID 无效")
    endpoints = (
        (f"/events/threads/{thread_id}/series", {}, THREAD_DETAIL_TTL),
        (f"/events/threads/{thread_id}/coverage", {}, THREAD_DETAIL_TTL),
        (f"/events/threads/{thread_id}", {}, THREAD_DETAIL_TTL),
    )
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_cached_json, settings, endpoint, params, ttl=ttl)
            for endpoint, params, ttl in endpoints
        ]
        results = [future.result() for future in futures]
    (
        (series_data, series_meta),
        (coverage_data, coverage_meta),
        (thread_data, thread_meta),
    ) = results
    warnings = []
    for label, meta in (
        ("时间线", series_meta),
        ("覆盖详情", coverage_meta),
        ("逐小时代表新闻", thread_meta),
    ):
        if meta.get("warning"):
            warnings.append(
                f"{label}更新失败，已使用{'旧缓存' if meta.get('state') == 'stale' else '本地降级'}：{meta['warning']}"
            )
    states = [
        series_meta.get("state"),
        coverage_meta.get("state"),
        thread_meta.get("state"),
    ]
    state = "live" if "live" in states else (
        "cache" if all(value == "cache" for value in states) else (
            "stale" if any(value in ("cache", "stale") for value in states) else "error"
        )
    )
    coverage = _coverage_detail(coverage_data)
    return {
        "schema_version": 1,
        "ok": bool(
            series_data is not None
            or coverage_data is not None
            or thread_data is not None
        ),
        "id": thread_id,
        "source": SOURCE_LABEL,
        "state": state,
        "points": _timeline_points(series_data),
        "coverage": coverage,
        "news": _coverage_news(coverage_data),
        "hourly_news": _hourly_news(thread_data, coverage),
        "warnings": warnings,
    }


def related_news(
    settings: Any,
    source: str,
    target: str,
    *,
    limit: int = 8,
) -> dict[str, Any]:
    """按关系图上游统计窗口，从本地新闻库找同时提到两个节点的代表报道。"""
    from . import db

    source = to_simplified(str(source or "")).strip()
    target = to_simplified(str(target or "")).strip()
    if not source or not target:
        raise ValueError("关系节点不能为空")
    ranking, _meta = _cached_json(
        settings,
        "/ranking",
        {
            "category": "news",
            "order": "burst",
            "gate": "all",
            "limit": 200,
            "trend": 1,
            "related": 1,
            "signals": 0,
            "ranks": 0,
            "archive": 0,
        },
        ttl=TREND_TTL,
    )
    window = _ranking_window(ranking)
    try:
        start_dt = datetime.fromisoformat(str(window.get("start") or "").replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(window.get("end") or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=35)
    days = max(2, math.ceil(max(1.0, (end_dt - start_dt).total_seconds() / 86400)) + 1)
    rows = db.fetch_news_since(days=days, limit=8000)
    found = []
    seen = set()
    for raw in rows:
        item = {key: raw[key] for key in raw.keys()}
        published = str(item.get("published_at") or item.get("first_seen") or "")
        try:
            when = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if when < start_dt or when > end_dt:
            continue
        text = to_simplified(
            " ".join(
                str(item.get(key) or "")
                for key in ("title", "summary", "ai_summary", "candidates", "topics", "regions")
            )
        ).casefold()
        if source.casefold() not in text or target.casefold() not in text:
            continue
        key = str(item.get("url") or item.get("id") or item.get("title") or "")
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "id": item.get("id"),
            "title": to_simplified(str(item.get("title") or ""))[:180],
            "source_name": str(item.get("source_name") or ""),
            "published_at": published,
            "url": str(item.get("url") or ""),
            "summary": to_simplified(str(item.get("ai_summary") or item.get("summary") or ""))[:220],
        })
        if len(found) >= max(1, min(20, limit)):
            break
    return {
        "ok": True,
        "source": source,
        "target": target,
        "window": window,
        "news": found,
        "local_match_count": len(found),
    }


def get_insights(
    settings: Any,
    hours: int = 720,
    *,
    offset: int = 0,
    limit: int = 12,
    candidate_filter: str = "",
    sort: str = "relevance",
    graph_only: bool = False,
) -> dict[str, Any]:
    """返回经过选举相关性过滤的事件串和热词趋势。

    事件串按台北日期逐日读取，避免 period 接口固定只返回前 12 个。
    每日数据先去除 folded 子事件与跨日重复，再完成本地选举相关性筛选和分页。
    """
    hours = hours if hours in PERIOD_DAYS else 720
    days = PERIOD_DAYS[hours]
    offset = max(0, int(offset or 0))
    # 页面分页通常使用 12 条；导出接口会一次请求全部匹配事件。
    limit = min(2000, max(1, int(limit or 12)))
    candidate_filter = to_simplified(str(candidate_filter or "")).strip()
    sort = sort if sort in {"relevance", "duration", "latest"} else "relevance"
    try:
        current_year = int(str(getattr(settings, "election_date", "") or "2026")[:4])
    except ValueError:
        current_year = 2026
    candidates, regions = _election_terms(settings)
    official_candidates = _roster_candidate_names() or candidates
    warnings: list[str] = []
    taipei = timezone(timedelta(hours=8))
    today = datetime.now(taipei).date()
    dates = [
        (today - timedelta(days=index)).isoformat()
        for index in range(days - 1, -1, -1)
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        thread_futures = [] if graph_only else [
            pool.submit(
                _cached_json,
                settings,
                "/events/threads",
                {"day": day},
                ttl=THREAD_TTL,
            )
            for day in dates
        ]
        trend_future = pool.submit(
            _cached_json,
            settings,
            "/ranking",
            {
                "category": "news",
                "order": "burst",
                "gate": "all",
                "limit": 200,
                "trend": 1,
                "related": 1,
                "signals": 0,
                "ranks": 0,
                "archive": 0,
            },
            ttl=TREND_TTL,
        )
        group_future = pool.submit(
            _cached_json,
            settings,
            "/tag-groups/overview",
            {},
            ttl=GROUP_TTL,
        )
        thread_results = [future.result() for future in thread_futures]
        trend_data, trend_meta = trend_future.result()
        group_data, group_meta = group_future.result()

    thread_states = [meta.get("state") for _, meta in thread_results]
    thread_fetched = [
        meta.get("fetched_at")
        for _, meta in thread_results
        if meta.get("fetched_at")
    ]
    if graph_only:
        thread_state = "skipped"
    elif "live" in thread_states:
        thread_state = "live"
    elif thread_states and all(state == "cache" for state in thread_states):
        thread_state = "cache"
    elif any(state in ("cache", "stale") for state in thread_states):
        thread_state = "stale"
    else:
        thread_state = "error"
    thread_meta = {
        "state": thread_state,
        "fetched_at": max(thread_fetched, default=None),
    }
    thread_errors = list(
        dict.fromkeys(
            str(meta.get("warning"))
            for _, meta in thread_results
            if meta.get("warning")
        )
    )
    if thread_errors:
        warnings.append(
            f"事件串部分日期更新失败，已使用旧缓存：{'；'.join(thread_errors[:2])}"
        )
    for label, meta in (("热词", trend_meta), ("标签分组", group_meta)):
        if meta.get("warning"):
            warnings.append(f"{label}更新失败，已使用{'旧缓存' if meta.get('state') == 'stale' else '本地降级'}：{meta['warning']}")

    raw_records = 0
    folded_records = 0
    duplicate_records = 0
    unique: dict[Any, dict[str, Any]] = {}
    for day, (thread_data, _) in zip(dates, thread_results):
        for item in (thread_data or {}).get("threads") or []:
            if not isinstance(item, dict):
                continue
            raw_records += 1
            if item.get("foldedInto"):
                folded_records += 1
                continue
            thread_id = item.get("id")
            if thread_id in (None, ""):
                continue
            normalized = normalize_thread(item)
            previous = unique.get(thread_id)
            if previous:
                duplicate_records += 1
                combined_weight = round(
                    float(previous.get("weight") or 0)
                    + float(normalized.get("weight") or 0),
                    3,
                )
                if str(normalized.get("last_time") or "") > str(previous.get("last_time") or ""):
                    previous.update(normalized)
                previous["weight"] = combined_weight
                continue
            normalized["day"] = day
            unique[thread_id] = normalized

    all_threads: list[dict[str, Any]] = []
    for thread in unique.values():
        text = " ".join(
            [str(tag) for tag in thread.get("tags") or []]
            + [
                str(news_item.get("title") or "")
                for news_item in thread.get("news") or []
                if isinstance(news_item, dict)
            ]
        )
        matched_candidates = sorted(
            {
                name
                for name in official_candidates
                if len(name) >= 2 and name in text
            },
            key=lambda name: (-len(name), name),
        )
        thread["candidates"] = matched_candidates
        thread["duration_hours"] = round(_thread_duration_hours(thread), 1)
        if _thread_relevant(text, candidates, regions):
            all_threads.append(thread)
    candidate_counts = Counter(
        name
        for thread in all_threads
        for name in thread.get("candidates") or []
    )
    event_candidates = [
        {"name": name, "count": count}
        for name, count in candidate_counts.most_common()
    ]
    filtered_threads = all_threads
    if candidate_filter:
        filtered_threads = [
            thread
            for thread in all_threads
            if candidate_filter in (thread.get("candidates") or [])
        ]

    def last_timestamp(thread: dict[str, Any]) -> float:
        try:
            return datetime.fromisoformat(
                str(thread.get("last_time") or "").replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            return 0.0

    if sort == "duration":
        filtered_threads.sort(
            key=lambda item: (
                -float(item.get("duration_hours") or 0),
                -float(item.get("weight") or 0),
                -last_timestamp(item),
            )
        )
    elif sort == "latest":
        filtered_threads.sort(
            key=lambda item: (
                -last_timestamp(item),
                -float(item.get("weight") or 0),
            )
        )
    else:
        filtered_threads.sort(
            key=lambda item: (
                -float(item.get("weight") or 0),
                -int(item.get("hours") or 0),
                -last_timestamp(item),
            )
        )

    total_events = len(filtered_threads)
    page_threads = filtered_threads[offset: offset + limit]
    remaining = max(0, total_events - offset - len(page_threads))

    trends: list[dict[str, Any]] = []
    for item in (trend_data or {}).get("entries") or []:
        if isinstance(item, dict) and _tag_relevant(
            str(item.get("tag") or ""),
            candidates,
            regions,
        ):
            trends.append(normalize_trend(item))
    trends.sort(key=lambda item: (
        -float(item.get("burst") or 0),
        -float(item.get("score") or 0),
        str(item.get("tag") or ""),
    ))
    trends = trends[:16]
    all_groups = normalize_groups(group_data, current_year=current_year)
    graph = build_topic_graph(
        trends,
        all_groups,
        candidates,
        regions,
        current_year=current_year,
    )
    groups = graph["groups"]

    states = [thread_meta.get("state"), trend_meta.get("state"), group_meta.get("state")]
    state = "live" if "live" in states else (
        "cache" if all(value == "cache" for value in states) else (
            "stale" if any(value in ("cache", "stale") for value in states) else "error"
        )
    )
    fetched = [
        thread_meta.get("fetched_at"),
        trend_meta.get("fetched_at"),
        group_meta.get("fetched_at"),
    ]
    graph_window = _ranking_window(trend_data)
    return {
        "schema_version": 1,
        "ok": bool(unique or trend_data is not None),
        "source": SOURCE_LABEL,
        "usage_note": "蓝绿份额只表示媒体报道覆盖差异，不是民调或候选人的支持率。",
        "state": state,
        "fetched_at": max((value for value in fetched if value), default=None),
        "window": {
            "hours": hours,
            "requested_days": days,
            "exact": hours not in (360, 720),
            "dates": dates,
            "note": (
                "15 天和 30 天筛选使用新文易数 31 天事件串窗口。"
                if hours in (360, 720)
                else ""
            ),
        },
        "threads": page_threads,
        "event_facets": {
            "candidates": event_candidates,
            "selected_candidate": candidate_filter,
            "sort": sort,
            "all_election_events": len(all_threads),
        },
        "event_pagination": {
            "offset": offset,
            "limit": limit,
            "returned": len(page_threads),
            "loaded": min(total_events, offset + len(page_threads)),
            "total": total_events,
            "remaining": remaining,
            "has_more": remaining > 0,
            "read_events": len(unique),
            "filtered_out": max(0, len(unique) - len(all_threads)),
            "raw_records": raw_records,
            "folded_records": folded_records,
            "duplicate_records": duplicate_records,
            "candidate": candidate_filter,
            "sort": sort,
            "all_election_events": len(all_threads),
        },
        "trends": trends,
        "groups": groups,
        "graph": graph,
        "graph_window": graph_window,
        "counts": {
            "threads": total_events,
            "threads_page": len(page_threads),
            "threads_all_election": len(all_threads),
            "trends": len(trends),
            "groups": len(groups),
            "graph_nodes": len(graph["nodes"]),
            "graph_edges": len(graph["edges"]),
            "thread_articles": sum(
                item["coverage"]["articles"] for item in filtered_threads
            ),
        },
        "cache": {
            "threads": thread_meta.get("state"),
            "trends": trend_meta.get("state"),
            "groups": group_meta.get("state"),
        },
        "warnings": warnings,
    }


def status(settings: Any) -> dict[str, Any]:
    key = str(getattr(settings, "tag_observe_api_key", "") or "")
    try:
        cache_files = len(list(CACHE_DIR.glob("*.json"))) if CACHE_DIR.exists() else 0
    except OSError:
        cache_files = 0
    return {
        "source": SOURCE_LABEL,
        "configured": bool(key),
        "api_key_hint": f"••••{key[-4:]}" if key else "",
        "mode": "个人额度（每分钟 1000 次）" if key else "公开额度（每 IP 每分钟 60 次）",
        "cache_files": cache_files,
        "cache_ttl_seconds": {
            "threads": THREAD_TTL,
            "trends": TREND_TTL,
            "groups": GROUP_TTL,
            "thread_detail": THREAD_DETAIL_TTL,
        },
        "usage_note": "外部数据仅作新闻覆盖与趋势补充，不参与胜率公式。",
    }
