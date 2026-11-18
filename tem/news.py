"""新聞採集：Google 新聞 RSS + 各媒體 RSS。"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from concurrent.futures import as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Any, Iterable

from . import db
from .config import Region, Settings
from .http import fetch_bytes
from .paths import CONFIG_DIR
from .zhconv import to_simplified

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
GOOGLE_SUFFIX_RE = re.compile(r"\s+-\s+[^-]{2,30}$")
ENCODING_RE = re.compile(rb'encoding=["\']([\w\-]+)["\']', re.I)
_FOCUS_CANDIDATE_CACHE: dict[tuple[str, ...], list[dict[str, str]]] = {}

_ENTERTAINMENT_SOURCE_TERMS = (
    "星闻", "星光", "娱乐", "综艺", "影视", "戏剧",
)
_ENTERTAINMENT_WU_TERMS = (
    "艺人", "歌手", "综艺", "节目", "主持", "演唱会", "专辑", "唱片",
    "娱乐圈", "演艺圈", "金钟", "金曲", "商演", "江祖平", "海产",
    "鹿希派", "吴姗儒", "宪哥", "求婚", "闪婚", "婚礼", "婚宴",
    "红包", "卖房", "卖楼", "房产", "财务危机", "少赚", "全不站台",
    "sandy", "康康", "高凌风", "罗大佑", "量子水", "脱手3房",
)
_YILAN_POLITICS_TERMS = (
    "宜兰", "县长", "县政", "林国漳", "陈琬惠", "竞选", "候选",
    "拜票", "造势", "政见", "民调", "竞总", "溪北", "溪南",
    "宜兰大学", "头城", "罗东", "苏澳", "礁溪", "壮围", "员山",
    "冬山", "五结", "三星", "大同乡", "南澳", "乡镇长", "县议员",
)
@lru_cache(maxsize=4096)
def _simplified_term(value: str) -> str:
    return to_simplified(value)


def same_name_news_excluded(
    candidate_name: str,
    region: str,
    title: str,
    summary: str = "",
    source_name: str = "",
) -> bool:
    """排除与正式候选人同名但实际属于娱乐人物的新闻。"""
    name = _simplified_term(candidate_name).replace("臺", "台")
    region_text = _simplified_term(region).replace("臺", "台")
    if name != "吴宗宪" or region_text != "宜兰县":
        return False
    haystack = _simplified_term(f"{title} {summary}")
    if name not in haystack:
        return False
    source = _simplified_term(source_name)
    if any(term in haystack for term in _YILAN_POLITICS_TERMS):
        return False
    entertainment = any(term in haystack for term in _ENTERTAINMENT_WU_TERMS)
    entertainment_source = any(term in source for term in _ENTERTAINMENT_SOURCE_TERMS)
    if entertainment or entertainment_source:
        return True
    return False


def purge_unrelated_same_name_news(days: int = 120) -> dict[str, Any]:
    """清理数据库里已经混入的同名娱乐人物新闻。"""
    end = datetime.now(timezone.utc) + timedelta(days=1)
    start = end - timedelta(days=days)
    rows = db.fetch_news_between(
        start.isoformat(timespec="seconds"),
        end.isoformat(timespec="seconds"),
        limit=50000,
    )
    targets = []
    for row in rows:
        item = dict(row)
        text = f"{item.get('title') or ''} {item.get('summary') or ''}"
        if "吴宗宪" not in _simplified_term(text) and "吳宗憲" not in text:
            continue
        if same_name_news_excluded(
            "吳宗憲",
            "宜蘭縣",
            item.get("title") or "",
            item.get("summary") or "",
            item.get("source_name") or "",
        ):
            targets.append(item)
    deleted = db.delete_news_ids(item["id"] for item in targets)
    return {
        "matched": len(targets),
        "deleted": deleted,
        "ids": [item["id"] for item in targets],
    }


def _focus_candidates(settings: Settings) -> list[dict[str, str]]:
    """新北、宜兰正式县市长候选人；同名不做额外排除。"""
    cache_key = ("mayors",)
    if cache_key in _FOCUS_CANDIDATE_CACHE:
        return _FOCUS_CANDIDATE_CACHE[cache_key]
    rows: list[dict[str, str]] = []
    try:
        roster = json.loads(
            (CONFIG_DIR / "roster.json").read_text(encoding="utf-8")
        )
        for region in ("新北市", "宜蘭縣"):
            for item in (roster.get("regions") or {}).get(region) or []:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                rows.append(
                    {
                        "region": region,
                        "name": name,
                        "district": region,
                        "party": str(item.get("party") or ""),
                        "office": "縣市長",
                    }
                )
    except Exception:
        pass
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in rows:
        key = (
            str(item.get("region") or ""),
            str(item.get("name") or ""),
        )
        if key[0] and key[1]:
            unique[key] = item
    result = list(unique.values())
    _FOCUS_CANDIDATE_CACHE[cache_key] = result
    return result


@dataclass
class NewsItem:
    id: str
    source_id: str
    source_name: str
    lang: str
    title: str
    url: str
    published_at: str
    summary: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(value)
    text = TAG_RE.sub(" ", text)
    return WS_RE.sub(" ", text).strip()


def decode_payload(raw: bytes) -> str:
    """依 XML 宣告 / HTTP 內容嗅探編碼，避免 Big5、GB18030 來源變亂碼。"""
    declared = None
    match = ENCODING_RE.search(raw[:200])
    if match:
        declared = match.group(1).decode("ascii", "replace").lower()
    for encoding in (declared, "utf-8", "big5", "gb18030", "cp950"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _parse_date(value: str | None) -> str:
    """統一輸出 UTC 'YYYY-MM-DD HH:MM:SS'，方便 SQLite 字串比較與排序。"""
    if not value:
        return ""
    value = value.strip()
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0, tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def _item_id(title: str, url: str) -> str:
    # Google News 會對同一則新聞產生不同追蹤參數，取標題為主鍵較穩定
    basis = re.sub(r"[^\w\u4e00-\u9fff]+", "", title) or url
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


def parse_feed(xml_text: str, feed: dict[str, Any]) -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text.encode("utf-8", "replace"))
    except ET.ParseError:
        return items

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    nodes = root.iter("item")
    found = list(nodes)
    if not found:
        found = list(root.iter("{http://www.w3.org/2005/Atom}entry"))

    for node in found:
        def pick(tag: str, atom: bool = False) -> str:
            key = f"{{http://www.w3.org/2005/Atom}}{tag}" if atom else tag
            child = node.find(key)
            return child.text if child is not None and child.text else ""

        title = clean_text(pick("title", True) or pick("title"))
        if not title:
            continue
        link = pick("link", True)
        if not link:
            link_node = node.find("link")
            link = link_node.text if link_node is not None and link_node.text else ""
        summary = pick("description") or pick("summary", True) or pick("content", True)
        published = _parse_date(pick("pubDate") or pick("updated", True) or pick("published", True))

        source_name = pick("source") or feed.get("name", "")
        title_clean = GOOGLE_SUFFIX_RE.sub("", title) if feed.get("kind") == "google_news" else title

        items.append(
            NewsItem(
                id=_item_id(title_clean, link),
                source_id=feed.get("id", ""),
                source_name=clean_text(source_name) or feed.get("name", ""),
                lang=feed.get("lang", ""),
                title=title_clean,
                url=link,
                published_at=published,
                summary=clean_text(summary)[:1200],
            )
        )
    return items


def fetch_and_parse(
    url: str,
    feed: dict[str, Any],
    *,
    timeout: int = 10,
    retries: int = 1,
) -> list[NewsItem]:
    raw = fetch_bytes(url, timeout=timeout, retries=retries)
    return parse_feed(decode_payload(raw), feed)


def _recent_enough(item: NewsItem, max_age_days: int) -> bool:
    if not item.published_at:
        return True
    try:
        dt = datetime.fromisoformat(item.published_at)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt >= datetime.now(timezone.utc) - timedelta(days=max_age_days)


def _matches_election(item: NewsItem, settings: Settings) -> bool:
    haystack = _simplified_term(f"{item.title} {item.summary}")
    focus_candidates = _focus_candidates(settings)
    excluded_same_name = False
    for candidate in focus_candidates:
        name = _simplified_term(candidate["name"])
        if name not in haystack:
            continue
        if same_name_news_excluded(
            candidate["name"],
            candidate["region"],
            item.title,
            item.summary,
            item.source_name,
        ):
            excluded_same_name = True
            continue
        if len(name) >= 2:
            return True
        region = _simplified_term(candidate["region"])
        if region in haystack and any(
            term in haystack for term in ("议员", "议会", "选举", "民调", "政见", "候选")
        ):
            return True
    if excluded_same_name:
        return False
    exclusive_terms = settings.taiwan_exclusive_election_keywords or [
        "九合一",
        "县市长",
        "直辖市长",
        "乡镇市长",
        "村里长",
        "六都",
    ]
    if any(_simplified_term(term) in haystack for term in exclusive_terms):
        return True

    # 其余选举词必须同时出现22县市或正式候选人；单有「台湾」不足以
    # 识别地方选举，避免国际新闻中的「台湾友邦、外国议员」被误收。
    local_terms = [
        *(keyword for region in settings.regions for keyword in region.keywords),
        *(name for name in settings.name_map.values() if name),
        *(item["name"] for item in focus_candidates),
    ]
    if not any(_simplified_term(term) in haystack for term in local_terms):
        return False
    return any(_simplified_term(term) in haystack for term in settings.election_keywords)


def fetch_all(
    settings: Settings,
    *,
    max_age_days: int = 14,
    max_per_feed: int = 180,
) -> list[NewsItem]:
    """抓取所有來源，過濾出與選舉相關的新聞並去重。"""
    collected: dict[str, NewsItem] = {}
    errors: list[str] = []

    jobs: list[tuple[dict[str, Any], str]] = []
    for feed in settings.feeds:
        if feed.get("kind") == "google_news":
            # Google News 對「關鍵字 OR 關鍵字」的支援很差，逐一查詢效果遠佳
            if feed.get("priority_local_candidates"):
                focus_candidates = _focus_candidates(settings)
                regions = list(dict.fromkeys(
                    item["region"] for item in focus_candidates
                ))
                queries = []
                for region in regions:
                    queries.extend(
                        [
                            f"{region} 選戰",
                            f"{region} 縣市長 選舉",
                            f"{region} 議員 選舉",
                            f"{region} 議員 選戰",
                        ]
                    )
                queries.extend(
                    f'"{item["name"]}"'
                    for item in focus_candidates
                    if item.get("name")
                )
                queries = list(dict.fromkeys(queries))
            else:
                queries = list(feed.get("queries") or settings.queries or ["九合一 選舉"])
            if feed.get("query_mode") == "combined":
                queries = [" OR ".join(queries)]
            for query in queries:
                url = feed["url_template"].format(q=urllib.parse.quote(query))
                jobs.append((feed, url))
        elif feed.get("url"):
            jobs.append((feed, feed["url"]))

    def run_job(job: tuple[dict[str, Any], str]):
        feed, url = job
        try:
            return feed, fetch_and_parse(url, feed, timeout=10, retries=1), ""
        except Exception as exc:  # noqa: BLE001 - 單一來源失敗不應中斷整體
            return feed, [], f"{feed.get('id')}: {exc}"

    pool = ThreadPoolExecutor(max_workers=8)
    futures = [pool.submit(run_job, job) for job in jobs]
    completed = 0
    try:
        for future in as_completed(futures, timeout=180):
            feed, items, error = future.result()
            completed += 1
            if error:
                errors.append(error)
                continue
            items = [i for i in items if i.title and _recent_enough(i, max_age_days)]
            for item in items[:max_per_feed]:
                if not _matches_election(item, settings):
                    continue
                existing = collected.get(item.id)
                if existing is None or _feed_weight(feed) > _feed_weight_by_id(settings, existing.source_id):
                    collected[item.id] = item
    except FutureTimeout:
        errors.append(f"{len(jobs) - completed} 个新闻来源超过100秒总时限，已跳过")
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    if errors:
        print(f"[news] {len(errors)} 個來源失敗：{' | '.join(errors[:3])}{' ...' if len(errors) > 3 else ''}")

    return sorted(collected.values(), key=lambda i: i.published_at or "", reverse=True)


def _feed_weight(feed: dict[str, Any]) -> float:
    try:
        return float(feed.get("weight", 1.0))
    except (TypeError, ValueError):
        return 1.0


def _feed_weight_by_id(settings: Settings, feed_id: str) -> float:
    for feed in settings.feeds:
        if feed.get("id") == feed_id:
            return _feed_weight(feed)
    return 1.0


def build_candidate_index(snapshots: dict[str, Any]) -> dict[str, str]:
    """候選人（中/英文）→ 縣市代碼，用於把新聞歸到正確選區。"""
    index: dict[str, str] = {}
    for code, snapshot in (snapshots or {}).items():
        for candidate in getattr(snapshot, "candidates", []):
            if candidate.name_zh and candidate.name_zh != candidate.name_en:
                index[candidate.name_zh] = code
            if candidate.name_en:
                index[candidate.name_en.lower()] = code
    return index


def detect_regions(
    text: str,
    settings: Settings,
    candidate_index: dict[str, str] | None = None,
) -> list[Region]:
    """以縣市名 / 別名 / 候選人姓名判斷新聞屬於哪些選區。"""
    weights: dict[str, int] = {}
    for region in settings.regions:
        score = 0
        for kw in region.keywords:
            if kw and kw in text:
                score += len(kw) * 2
        if score:
            weights[region.code] = weights.get(region.code, 0) + score

    if candidate_index:
        lowered = text.lower()
        for token, code in candidate_index.items():
            if token and token in lowered:
                weights[code] = weights.get(code, 0) + 3
    normalized_text = _simplified_term(text)
    for item in _focus_candidates(settings):
        name = _simplified_term(str(item.get("name") or ""))
        if not name or name not in normalized_text:
            continue
        region = settings.region_by_name(str(item.get("region") or ""))
        if region:
            weights[region.code] = weights.get(region.code, 0) + 3

    ranked = sorted(weights.items(), key=lambda kv: -kv[1])
    if not ranked:
        return []
    top_score = ranked[0][1]
    # 縣市名命中權重 ≥ 6（每字 ×2）；僅候選人姓名命中為 3。
    # 門檻設 3 可保留「新聞只提候選人不提縣市」的情況，同時排除更弱的雜訊。
    threshold = max(3, top_score * 0.5)
    ordered: list[Region] = []
    for code, score in ranked[:4]:
        if score < threshold:
            break
        region = settings.region_by_name(code)
        if region:
            ordered.append(region)
    return ordered[:3]


def detect_candidates(text: str, settings: Settings) -> list[str]:
    """回傳在新聞中出現的 Polymarket 人名（英文原字串，作為對照鍵）。"""
    found: list[str] = []
    for english, chinese in settings.name_map.items():
        if chinese and chinese in text:
            found.append(english)
        elif english and english.lower() in text.lower():
            found.append(english)
    for item in _focus_candidates(settings):
        key = f"local:{item['name']}"
        if (
            _simplified_term(item["name"]) in _simplified_term(text)
            and key not in found
        ):
            found.append(key)
    return found


def merge_items(existing: Iterable[NewsItem], new_items: Iterable[NewsItem]) -> list[NewsItem]:
    merged = {i.id: i for i in existing}
    for item in new_items:
        merged[item.id] = item
    return list(merged.values())
