"""候選人言論的官方文字來源接入。

本模組只處理可公開核驗的文字：
  * 選舉黃頁：候選人身份、政黨、號次與參選職務校驗。
  * 候選人官方 YouTube：頻道 RSS 的標題與簡介，不下載影片。
  * 選舉公報：公報索引中的網頁文字版，不開啟 PDF、不執行 OCR。
  * 立法院 IVOD：現成會議文字記錄，不下載音視頻、不執行語音轉寫。

任何外部來源失敗都不會刪除既有言論；若本地有非空舊快取，會回傳舊資料
並附上明確的過期提示。
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from . import db
from .dedupe import normalize_title
from .http import fetch_bytes
from .paths import CONFIG_DIR, DATA_DIR
from .zhconv import to_simplified

SOURCE_LABELS = {
    "official_bulletin": "选举公报政见",
    "official_transcript": "立法院逐字稿",
    "official_account": "官方频道信息",
    "direct": "新闻直接引语",
    "paraphrase": "新闻转述",
}
SOURCE_CONFIDENCE = {
    "official_transcript": 0.94,
    "direct": 0.90,
    "official_bulletin": 0.84,
    "official_account": 0.78,
    "paraphrase": 0.55,
}
COMPARABLE_SOURCES = {
    "official_transcript",
    "official_bulletin",
    "direct",
}

ELECTION_DIRECTORY_BASE = "https://elections.olc.tw"
BULLETIN_INDEX_BASE = f"{ELECTION_DIRECTORY_BASE}/bulletins"
IVOD_BASE = "https://ivod.ly.gov.tw"
YOUTUBE_RSS = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
)
CACHE_DIR = DATA_DIR / "cache" / "statement_sources"
MAX_PRIORITY_CANDIDATES = 16
MAX_BULLETIN_REGIONS = 22

_POLICY_TERMS = (
    "政策", "政見", "政见", "承諾", "承诺", "主張", "主张", "推動", "推动",
    "建設", "建设", "改善", "提高", "降低", "增加", "減少", "减少", "補助",
    "补助", "興建", "兴建", "保障", "改革", "規劃", "规划", "支持", "反對",
    "反对", "交通", "捷運", "捷运", "住宅", "居住", "托育", "長照", "长照",
    "教育", "青年", "經濟", "经济", "就業", "就业", "醫療", "医疗", "環境",
    "环境", "能源", "治安", "預算", "预算", "兩岸", "两岸",
)
_SUPPORT_TERMS = (
    "支持", "贊成", "赞成", "推動", "推动", "主張", "主张", "承諾", "承诺",
    "保證", "保证", "同意", "優先", "优先", "擴大", "扩大", "補助", "补助",
    "興建", "兴建", "會做", "会做", "將推動", "将推动", "應該做", "应该做",
)
_OPPOSE_TERMS = (
    "反對", "反对", "不支持", "拒絕", "拒绝", "取消", "停止", "不會", "不会",
    "不應", "不应", "刪減", "删减", "暫緩", "暂缓", "不宜", "沒有必要", "没有必要",
)
_CONDITION_RE = re.compile(
    r"[^。！？!?]{0,35}(?:如果|若|只要|除非|前提|在.{0,18}(?:情況下|情况下)|一旦)[^。！？!?]{0,70}"
)
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
_DATE_RE = re.compile(r"(?P<year>20\d{2})[年./-](?P<month>\d{1,2})[月./-](?P<day>\d{1,2})")


@dataclass
class SourceResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    identity: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)


class _TextAndAnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.anchors: list[tuple[str, str]] = []
        self._skip = 0
        self._href = ""
        self._anchor_parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag in ("p", "div", "li", "tr", "br", "section", "article", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")
        if tag == "a":
            self._href = values.get("href") or ""
            self._anchor_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "svg"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("p", "div", "li", "tr", "section", "article", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")
        if tag == "a" and self._anchor_parts is not None:
            text = _clean_text(" ".join(self._anchor_parts))
            if self._href and text:
                self.anchors.append((self._href, text))
            self._href = ""
            self._anchor_parts = None

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self.parts.append(data)
        if self._anchor_parts is not None:
            self._anchor_parts.append(data)


def _clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"[\t\r\f\v\u3000 ]+", " ", value)
    value = re.sub(r"\n\s*\n+", "\n", value)
    return value.strip()


def _parse_page(raw_html: str) -> _TextAndAnchorParser:
    parser = _TextAndAnchorParser()
    try:
        parser.feed(raw_html)
    except Exception:  # noqa: BLE001 - 不完整 HTML 仍保留已經解析的文字
        pass
    return parser


def _decode(raw: bytes) -> str:
    match = re.search(rb'charset=["\']?([\w-]+)', raw[:5000], re.I)
    declared = match.group(1).decode("ascii", "ignore").lower() if match else ""
    if declared in ("big5", "cp950", "ms950", "big5-hkscs"):
        declared = "big5"
    for encoding in (declared, "utf-8", "big5", "gb18030"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _get_text(url: str, *, timeout: int = 12) -> str:
    return _decode(
        fetch_bytes(
            url,
            timeout=timeout,
            retries=1,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml,text/xml",
            },
        )
    )


def _cache_file(name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-") or "source"
    return CACHE_DIR / f"{safe}.json"


def _read_cache(name: str, max_age_hours: float) -> dict[str, Any] | None:
    path = _cache_file(name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(str(payload.get("fetched_at_iso") or ""))
        if datetime.now(timezone.utc) - stamp > timedelta(hours=max_age_hours):
            return None
        return payload
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _read_latest_cache(name: str) -> dict[str, Any] | None:
    path = _cache_file(name)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(name: str, **values: Any) -> None:
    try:
        payload = {
            "fetched_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **values,
        }
        _cache_file(name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _source_cached_rows(
    cache_name: str,
    warning: str,
) -> SourceResult:
    cached = _read_latest_cache(cache_name)
    rows = cached.get("rows") if cached else None
    if not rows:
        return SourceResult(warnings=[warning])
    return SourceResult(
        rows=[dict(item) for item in rows if isinstance(item, dict)],
        warnings=[
            f"{warning}；显示最近一次有效旧缓存"
            f"（{cached.get('fetched_at_iso') or '时间未知'}）"
        ],
        stats={"cache": True, "cached_at": cached.get("fetched_at_iso")},
    )


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", to_simplified(str(value or "")).replace("臺", "台")).lower()


def _substantive(text: str) -> bool:
    compact = re.sub(r"\W", "", text or "")
    return len(compact) >= 8 and any(term in (text or "") for term in _POLICY_TERMS)


def _topic(text: str) -> str:
    rules = (
        ("住房与土地", ("房價", "房价", "住宅", "社宅", "居住", "租屋", "土地", "都更", "房市")),
        ("交通建设", ("交通", "捷運", "捷运", "道路", "鐵路", "铁路", "公車", "公车", "停車", "停车", "機場", "机场")),
        ("经济就业", ("經濟", "经济", "就業", "就业", "薪資", "薪资", "產業", "产业", "投資", "投资", "中小企業", "中小企业")),
        ("社会福利", ("福利", "托育", "長照", "长照", "老人", "幼兒", "幼儿", "補助", "补助", "健保", "低收入")),
        ("教育青年", ("教育", "學生", "学生", "青年", "學校", "学校", "老師", "老师", "大學", "大学")),
        ("能源环境", ("能源", "電力", "电力", "核電", "核电", "再生能源", "減碳", "减碳", "環境", "环境", "污染")),
        ("两岸关系", ("兩岸", "两岸", "中國", "中国", "中共", "統獨", "统独", "國安", "国安", "國防", "国防")),
        ("治安司法", ("治安", "司法", "檢調", "检调", "警察", "犯罪", "詐騙", "诈骗", "廉政", "貪瀆", "贪渎")),
        ("财政预算", ("財政", "财政", "預算", "预算", "稅", "税", "負債", "负债", "財源", "财源")),
        ("地方治理", ("市政", "縣政", "县政", "治理", "行政效率", "公務員", "公务员", "地方建設", "地方建设")),
    )
    for topic, words in rules:
        if any(word in text for word in words):
            return topic
    return "其他政策"


def _polarity(text: str) -> tuple[int, str]:
    if any(term in text for term in _OPPOSE_TERMS):
        return -1, "opposes"
    if any(term in text for term in _SUPPORT_TERMS):
        return 1, "supports"
    return 0, "neutral"


def _condition(text: str) -> str:
    match = _CONDITION_RE.search(text or "")
    return match.group(0).strip()[:120] if match else ""


def _statement_id(source_id: str, candidate: str, text: str) -> str:
    raw = f"{source_id}|{candidate}|{normalize_title(text)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _row(
    candidate: dict[str, Any],
    *,
    source_id: str,
    source_kind: str,
    source_name: str,
    source_title: str,
    source_url: str,
    claim: str,
    context: str,
    published_at: str,
    confidence: float | None = None,
) -> dict[str, Any]:
    claim = re.sub(r"\s+", " ", claim or "").strip()[:300]
    polarity, stance = _polarity(claim)
    return {
        "id": _statement_id(source_id, candidate["name"], claim),
        "news_id": source_id,
        "candidate_name": candidate["name"],
        "region_name": candidate.get("region") or "",
        "party": candidate.get("party") or "",
        "office": candidate.get("office") or "",
        "published_at": published_at,
        "topic": _topic(claim),
        "claim": claim,
        "quote_text": re.sub(r"\s+", " ", context or claim)[:1200],
        "stance": stance,
        "polarity": polarity,
        "conditions": _condition(context or claim),
        "directness": source_kind,
        "confidence": confidence if confidence is not None else SOURCE_CONFIDENCE[source_kind],
        "source_name": source_name,
        "source_title": source_title[:500],
        "source_url": source_url,
        "extracted_at": db.utcnow_iso(),
    }


def _next_year_candidates(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """優先處理縣市長、已設官方頻道者，以及新北與宜蘭重點選區。"""
    ordered: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(item: dict[str, Any]) -> None:
        key = (
            str(item.get("region") or ""),
            str(item.get("name") or ""),
            str(item.get("office") or ""),
        )
        if key not in seen and key[1]:
            seen.add(key)
            ordered.append(item)

    for item in candidates:
        if item.get("region") in ("新北市", "宜蘭縣"):
            add(item)
    for item in candidates:
        if item.get("office") in ("縣市長", "直轄市長"):
            add(item)
    for item in candidates:
        add(item)
    return ordered[:MAX_PRIORITY_CANDIDATES]


def parse_directory_results(
    items: Any,
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    """從選舉黃頁自動補全結果挑出同姓名、同縣市、同職務的 2026 記錄。"""
    if not isinstance(items, list):
        return None
    region = _norm(str(candidate.get("region") or ""))
    office = to_simplified(str(candidate.get("office") or ""))
    office_terms = (
        ("直辖市长", "直辖市长")
        if office == "直辖市长"
        else ("县市长", "县市长", "县长")
    )
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "")
        compact = _norm(label)
        if "2026" not in compact or region not in compact:
            continue
        if not any(term in compact for term in office_terms):
            continue
        return {
            "id": str(item.get("id") or ""),
            "label": label,
            "value": str(item.get("value") or ""),
        }
    return None


def sync_election_directory(
    candidates: list[dict[str, Any]],
) -> SourceResult:
    """校驗 2026 候選人身份；身份紀錄本身不寫入言論時間線。"""
    selected = _next_year_candidates(candidates)
    cache_name = "election-directory-2026"
    cached = _read_cache(cache_name, 24 * 7)
    cached_identity: dict[tuple[str, str, str], dict[str, Any]] = {}
    cached_not_found: set[tuple[str, str, str]] = set()
    if cached and isinstance(cached.get("identity"), dict):
        cached_identity = {
            tuple(str(part) for part in key.split("\u001f", 2)): value
            for key, value in cached["identity"].items()
            if isinstance(value, dict)
        }
        cached_not_found = {
            tuple(str(part) for part in key.split("\u001f", 2))
            for key in cached.get("not_found") or []
            if isinstance(key, str)
        }
        selected_keys = {
            (
                str(item.get("region") or ""),
                str(item.get("name") or ""),
                str(item.get("office") or ""),
            )
            for item in selected
        }
        covered_keys = set(cached_identity) | cached_not_found
        if selected_keys.issubset(covered_keys):
            return SourceResult(
                identity=cached_identity,
                stats={
                    "verified": len(selected_keys & set(cached_identity)),
                    "queried": len(selected),
                    "cache": True,
                },
            )
        selected = [
            item
            for item in selected
            if (
                str(item.get("region") or ""),
                str(item.get("name") or ""),
                str(item.get("office") or ""),
            )
            not in covered_keys
        ]

    def fetch(candidate: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
        url = (
            f"{ELECTION_DIRECTORY_BASE}/candidates/s/?"
            + urllib.parse.urlencode({"term": candidate["name"]})
        )
        try:
            data = json.loads(_get_text(url, timeout=18))
            match = parse_directory_results(data, candidate)
            if not match:
                return candidate, None, "未找到同姓名、同县市、同职务的2026记录"
            return candidate, match, None
        except Exception as exc:  # noqa: BLE001 - 單一候選人失敗不阻斷其他人
            return candidate, None, f"{type(exc).__name__}: {exc}"

    identity: dict[tuple[str, str, str], dict[str, Any]] = dict(cached_identity)
    not_found: set[tuple[str, str, str]] = set(cached_not_found)
    warnings: list[str] = []
    verified = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch, candidate) for candidate in selected]
        for future in as_completed(futures):
            candidate, match, warning = future.result()
            if match:
                key = (
                    str(candidate.get("region") or ""),
                    str(candidate.get("name") or ""),
                    str(candidate.get("office") or ""),
                )
                identity[key] = match
                verified += 1
                # 以官方黃頁補正名冊中的政黨與號次；不把身份資料當成發言。
                candidate["directory_verified"] = True
                candidate["directory_id"] = match.get("id")
            elif warning:
                warnings.append(f"{candidate['name']}：{warning}")
                if warning.startswith("未找到"):
                    not_found.add(
                        (
                            str(candidate.get("region") or ""),
                            str(candidate.get("name") or ""),
                            str(candidate.get("office") or ""),
                        )
                    )

    if verified or not_found:
        _write_cache(
            cache_name,
            identity={
                "\u001f".join(key): value for key, value in identity.items()
            },
            not_found=[
                "\u001f".join(key) for key in sorted(not_found)
            ],
            verified=len(identity),
        )
        return SourceResult(
            identity=identity,
            warnings=warnings[:8],
            stats={
                "verified": len(identity),
                "queried": len(selected),
                "newly_verified": verified,
                "cache": bool(cached_identity),
                "not_found": len(not_found),
            },
        )
    stale = _read_latest_cache(cache_name)
    if stale and isinstance(stale.get("identity"), dict):
        return SourceResult(
            identity={
                tuple(str(part) for part in key.split("\u001f", 2)): value
                for key, value in stale["identity"].items()
                if isinstance(value, dict)
            },
            warnings=["选举黄页当前不可用；使用最近一次身份校验缓存"]
            + warnings[:5],
            stats={
                "verified": int(stale.get("verified") or 0),
                "queried": len(selected),
                "cache": True,
            },
        )
    return SourceResult(
        warnings=["选举黄页当前不可用"] + warnings[:5],
        stats={"verified": 0, "queried": len(selected)},
    )


def parse_youtube_feed(
    xml_text: str,
    *,
    account: dict[str, Any],
    candidate: dict[str, Any],
    days: int,
) -> list[dict[str, Any]]:
    """讀官方頻道 RSS；只保留標題與簡介，不下載影片。"""
    root = ET.fromstring(xml_text)
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    rows: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        video_id = (entry.findtext("yt:videoId", default="", namespaces=ns) or "").strip()
        title = _clean_text(entry.findtext("atom:title", default="", namespaces=ns) or "")
        published = (
            entry.findtext("atom:published", default="", namespaces=ns)
            or entry.findtext("atom:updated", default="", namespaces=ns)
            or ""
        )
        try:
            published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if not video_id or not title or published_dt < cutoff:
            continue
        description = _clean_text(
            entry.findtext("media:group/media:description", default="", namespaces=ns) or ""
        )
        claim = title
        context = f"{title}\n{description}".strip()
        if not _substantive(claim):
            # 標題是頻道發布資訊，不是完整逐字稿；沒有政策訊號時不強行入庫。
            continue
        url = f"https://www.youtube.com/watch?v={video_id}"
        rows.append(
            _row(
                candidate,
                source_id=f"youtube-rss:{video_id}",
                source_kind="official_account",
                source_name=f"YouTube 官方频道 · {account.get('account_name') or account.get('candidate')}",
                source_title=title,
                source_url=url,
                claim=claim,
                context=context,
                published_at=published_dt.isoformat(timespec="seconds"),
            )
        )
    return rows


def fetch_youtube_rss_statements(
    *,
    days: int,
    candidates: list[dict[str, Any]],
) -> SourceResult:
    cache_name = f"youtube-official-{days}d"
    try:
        config = json.loads(
            (CONFIG_DIR / "candidate_accounts.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return SourceResult(warnings=["候选人官方账号名录读取失败"])
    accounts = [
        item
        for item in config.get("accounts") or []
        if item.get("enabled")
        and item.get("platform") == "youtube"
        and item.get("verified")
    ]
    by_name = {str(item.get("name") or ""): item for item in candidates}

    def fetch(account: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str | None]:
        candidate = by_name.get(str(account.get("candidate") or ""))
        channel_id = str(account.get("account_id") or "").strip()
        if not candidate or not channel_id:
            return str(account.get("candidate") or ""), [], "候选人身份或频道ID缺失"
        try:
            xml_text = _get_text(
                YOUTUBE_RSS.format(channel_id=urllib.parse.quote(channel_id)),
                timeout=12,
            )
            return (
                str(account.get("candidate") or ""),
                parse_youtube_feed(
                    xml_text,
                    account=account,
                    candidate=candidate,
                    days=days,
                ),
                None,
            )
        except Exception as exc:  # noqa: BLE001
            return (
                str(account.get("candidate") or ""),
                [],
                f"{type(exc).__name__}: {exc}",
            )

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    ok_channels = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch, account) for account in accounts]
        for future in as_completed(futures):
            name, channel_rows, warning = future.result()
            if warning:
                warnings.append(f"{name}官方频道：{warning}")
            else:
                ok_channels += 1
            rows.extend(channel_rows)

    if rows:
        _write_cache(cache_name, rows=rows)
        return SourceResult(
            rows=rows,
            warnings=warnings[:8],
            stats={"channels": len(accounts), "ok_channels": ok_channels},
        )
    result = _source_cached_rows(cache_name, "YouTube 官方频道 RSS 暂无可用新数据")
    result.warnings.extend(warnings[:8])
    result.stats.update({"channels": len(accounts), "ok_channels": ok_channels})
    return result


def parse_bulletin_index(raw_html: str, region: str) -> list[dict[str, str]]:
    parser = _parse_page(raw_html)
    region_norm = _norm(region).replace("台", "")
    found: list[dict[str, str]] = []
    for href, title in parser.anchors:
        if "/bulletins/view/" not in href:
            continue
        compact = _norm(title).replace("台", "")
        if region_norm not in compact and compact.startswith(("台北", "新北", "桃园")):
            # 部分標題會把「臺」寫成「台」，上面的 _norm 已統一。
            pass
        is_mayor = (
            ("市長" in title and "市議員" not in title and "村里長" not in title)
            or ("縣長" in title and "縣市議員" not in title)
        )
        if not is_mayor:
            continue
        if region_norm and region_norm not in compact and region_norm.replace("台", "") not in compact.replace("台", ""):
            continue
        found.append(
            {
                "title": title,
                "url": urllib.parse.urljoin(ELECTION_DIRECTORY_BASE, html.unescape(href)),
            }
        )
    return found


def _extract_date(text: str) -> str:
    for match in _DATE_RE.finditer(text or ""):
        try:
            value = datetime(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
                tzinfo=timezone(timedelta(hours=8)),
            )
            return value.isoformat(timespec="seconds")
        except ValueError:
            continue
    return ""


def _is_2026_election(text: str) -> bool:
    compact = _norm(text)
    return "2026" in compact or "115年" in compact


def _candidate_segment(
    text: str,
    candidate_name: str,
    all_names: list[str],
) -> str:
    variants = {candidate_name, to_simplified(candidate_name)}
    positions = [text.find(name) for name in variants if name and text.find(name) >= 0]
    if not positions:
        return ""
    start = min(positions)
    following: list[int] = []
    for name in all_names:
        if _norm(name) == _norm(candidate_name):
            continue
        for variant in {name, to_simplified(name)}:
            position = text.find(variant, start + len(candidate_name))
            if position > start:
                following.append(position)
    end = min(following) if following else min(len(text), start + 9000)
    return text[start:end]


def extract_bulletin_statements(
    raw_html: str,
    *,
    candidate: dict[str, Any],
    all_names: list[str],
    source_url: str,
    source_title: str,
) -> list[dict[str, Any]]:
    parser = _parse_page(raw_html)
    text = _clean_text("\n".join(parser.parts))
    if not _is_2026_election(text):
        return []
    segment = _candidate_segment(text, str(candidate["name"]), all_names)
    if not segment:
        return []
    platform_positions = [
        position
        for marker in ("政見", "政见", "政見摘要", "政见摘要")
        if (position := segment.find(marker)) >= 0
    ]
    if not platform_positions:
        return []
    body = segment[min(platform_positions):]
    published_at = _extract_date(segment)
    rows: list[dict[str, Any]] = []
    used: set[str] = set()
    for match in _SENTENCE_RE.finditer(body):
        sentence = _clean_text(match.group(0)).strip("，,：:；; ")
        sentence = re.sub(r"^(?:政見|政见|政見摘要|政见摘要)[：:\s]*", "", sentence)
        if not _substantive(sentence) or sentence in used:
            continue
        used.add(sentence)
        rows.append(
            _row(
                candidate,
                source_id=f"bulletin:{hashlib.sha1(source_url.encode()).hexdigest()[:18]}:{len(rows)}",
                source_kind="official_bulletin",
                source_name=f"选举公报文字版 · {candidate.get('region') or ''}",
                source_title=source_title,
                source_url=source_url,
                claim=sentence,
                context=body[:2500],
                published_at=published_at,
            )
        )
        if len(rows) >= 8:
            break
    return rows


def fetch_bulletin_statements(
    candidates: list[dict[str, Any]],
) -> SourceResult:
    """逐縣市讀公報索引，只跟隨「網頁格式」連結。"""
    cache_name = "cec-bulletins-html-v1"
    cached = _read_cache(cache_name, 6)
    if cached and cached.get("scan_complete"):
        return SourceResult(
            rows=[
                dict(item)
                for item in cached.get("rows") or []
                if isinstance(item, dict)
            ],
            warnings=list(cached.get("warnings") or [])[:12],
            stats={
                "regions": int(cached.get("regions") or 0),
                "regions_done": int(cached.get("regions_done") or 0),
                "cache": True,
            },
        )
    mayors = [
        item
        for item in candidates
        if item.get("office") in ("縣市長", "直轄市長")
    ]
    by_region: dict[str, list[dict[str, Any]]] = {}
    for item in mayors:
        by_region.setdefault(str(item.get("region") or ""), []).append(item)
    regions = list(by_region)[:MAX_BULLETIN_REGIONS]
    all_names = [str(item.get("name") or "") for item in mayors if item.get("name")]
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []

    def fetch_region(region: str) -> tuple[str, list[dict[str, Any]], list[str]]:
        local_rows: list[dict[str, Any]] = []
        local_warnings: list[str] = []
        index_url = f"{BULLETIN_INDEX_BASE}/index/{urllib.parse.quote(region)}"
        try:
            links = parse_bulletin_index(_get_text(index_url, timeout=12), region)
        except Exception as exc:  # noqa: BLE001
            return region, [], [f"{region}公報索引：{type(exc).__name__}: {exc}"]
        if not links:
            return region, [], [f"{region}公報索引暂无县市长公报"]
        view = links[0]
        try:
            view_html = _get_text(view["url"], timeout=12)
        except Exception as exc:  # noqa: BLE001
            return region, [], [f"{region}公報详情：{type(exc).__name__}: {exc}"]
        view_parser = _parse_page(view_html)
        view_text = _clean_text("\n".join(view_parser.parts))
        if not _is_2026_election(view_text):
            return region, [], [f"{region}公報索引条目不属于2026年选举，已跳过"]
        web_links = [
            urllib.parse.urljoin(view["url"], href)
            for href, label in view_parser.anchors
            if "網頁格式" in label or "网页格式" in label
        ]
        if not web_links:
            local_warnings.append(f"{region}公報只有PDF或无网页文字版，已跳过（不扫描PDF）")
            return region, [], local_warnings
        text_url = web_links[0]
        try:
            text_html = _get_text(text_url, timeout=12)
        except Exception as exc:  # noqa: BLE001
            local_warnings.append(
                f"{region}公報网页文字版：{type(exc).__name__}: {exc}"
            )
            return region, [], local_warnings
        for candidate in by_region.get(region) or []:
            local_rows.extend(
                extract_bulletin_statements(
                    text_html,
                    candidate=candidate,
                    all_names=all_names,
                    source_url=text_url,
                    source_title=view["title"],
                )
            )
        if not local_rows:
            local_warnings.append(f"{region}公報网页未提取到候选人政见")
        return region, local_rows, local_warnings

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(fetch_region, region) for region in regions]
        done_regions = 0
        for future in as_completed(futures):
            _region, region_rows, region_warnings = future.result()
            done_regions += 1
            rows.extend(region_rows)
            warnings.extend(region_warnings)

    if rows:
        _write_cache(
            cache_name,
            rows=rows,
            warnings=warnings[:12],
            stats={"regions": len(regions), "regions_done": done_regions},
            regions=len(regions),
            regions_done=done_regions,
            scan_complete=done_regions == len(regions),
        )
        return SourceResult(
            rows=rows,
            warnings=warnings[:12],
            stats={"regions": len(regions), "regions_done": done_regions},
        )
    scan_complete = done_regions == len(regions)
    fatal_fragments = ("请求失败", "HTTP", "连接", "超时", "timed out", "timeout")
    no_source_failure = scan_complete and not any(
        fragment in warning
        for warning in warnings
        for fragment in fatal_fragments
    )
    failed_indexes = sum(
        1
        for warning in warnings
        if "公報索引：" in warning
        and any(fragment in warning for fragment in fatal_fragments)
    )
    mostly_reachable = done_regions == len(regions) and failed_indexes <= 12
    if no_source_failure or mostly_reachable:
        partial = not no_source_failure
        _write_cache(
            cache_name,
            rows=[],
            warnings=(
                warnings[:12]
                + (
                    ["部分县市公報索引本轮超时；6小时内优先使用本轮扫描结果"]
                    if partial
                    else []
                )
            )[:13],
            stats={"regions": len(regions), "regions_done": done_regions},
            regions=len(regions),
            regions_done=done_regions,
            scan_complete=True,
            partial=partial,
        )
        return SourceResult(
            warnings=(
                warnings[:12]
                + (
                    ["部分县市公報索引本轮超时；6小时内优先使用本轮扫描结果"]
                    if partial
                    else []
                )
            )[:13],
            stats={
                "regions": len(regions),
                "regions_done": done_regions,
                "cache": False,
                "partial": partial,
            },
        )
    result = _source_cached_rows(cache_name, "选举公报网页文字版暂无可用新数据")
    result.warnings.extend(warnings[:12])
    result.stats.update({"regions": len(regions), "regions_done": done_regions})
    return result


def extract_ivod_statements(
    raw_html: str,
    *,
    candidate: dict[str, Any],
    source_url: str,
    source_title: str = "",
    published_at: str = "",
    days: int = 120,
) -> list[dict[str, Any]]:
    """只解析 IVOD 頁面已存在的文字；不呼叫任何音視頻或轉寫服務。"""
    parser = _parse_page(raw_html)
    text = _clean_text("\n".join(parser.parts))
    names = {str(candidate["name"]), to_simplified(str(candidate["name"]))}
    positions = [text.find(name) for name in names if name and text.find(name) >= 0]
    if not positions:
        return []
    marker_positions = [
        position
        for marker in ("發言紀錄", "发言纪录", "會議紀錄", "会议纪录", "逐字稿", "質詢內容", "质询内容")
        if (position := text.find(marker)) >= 0
    ]
    start = min(marker_positions) if marker_positions else min(positions)
    body = text[start:start + 10000]
    row_date = published_at or _extract_date(body)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    if row_date:
        try:
            row_dt = datetime.fromisoformat(row_date)
            if row_dt.tzinfo is None:
                row_dt = row_dt.replace(tzinfo=timezone(timedelta(hours=8)))
            if row_dt < cutoff:
                return []
        except ValueError:
            return []
    elif not _is_2026_election(body):
        return []
    rows: list[dict[str, Any]] = []
    used: set[str] = set()
    for match in _SENTENCE_RE.finditer(body):
        sentence = _clean_text(match.group(0)).strip("，,：:；; ")
        if len(re.sub(r"\W", "", sentence)) < 18 or not _substantive(sentence):
            continue
        if sentence in used:
            continue
        used.add(sentence)
        rows.append(
            _row(
                candidate,
                source_id=f"ivod:{hashlib.sha1(source_url.encode()).hexdigest()[:18]}:{len(rows)}",
                source_kind="official_transcript",
                source_name="立法院 IVOD 会议文字记录",
                source_title=source_title or source_url,
                source_url=source_url,
                claim=sentence,
                context=body[:3000],
                published_at=row_date,
                confidence=SOURCE_CONFIDENCE["official_transcript"],
            )
        )
        if len(rows) >= 8:
            break
    return rows


def fetch_ivod_text_statements(
    candidates: list[dict[str, Any]],
    *,
    days: int = 120,
) -> SourceResult:
    """探測 IVOD 公開頁面中的既有文字紀錄。

    IVOD 部分主機限制非台灣網路或需要瀏覽器工作階段。本函式只讀 HTML，
    讀不到就跳過；絕不為補資料而下載影片或進行語音辨識。
    """
    cache_name = "ivod-text-v1"
    cached = _read_cache(cache_name, 4)
    if cached and cached.get("scan_complete"):
        return SourceResult(
            rows=[
                dict(item)
                for item in cached.get("rows") or []
                if isinstance(item, dict)
            ],
            warnings=list(cached.get("warnings") or [])[:10],
            stats={
                "candidates": int(cached.get("candidates") or 0),
                "transcripts": len(cached.get("rows") or []),
                "cache": True,
            },
        )
    selected = [
        item
        for item in _next_year_candidates(candidates)
        if item.get("office") in ("縣市長", "直轄市長", "立法委員")
    ][:8]

    def fetch(candidate: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        query = urllib.parse.urlencode({"keyword": candidate["name"]})
        url = f"{IVOD_BASE}/Search?{query}"
        try:
            page = _get_text(url, timeout=15)
        except Exception as exc:  # noqa: BLE001 - 不影響其他言論來源
            return [], f"{candidate['name']} IVOD：{type(exc).__name__}: {exc}"
        candidate_rows = extract_ivod_statements(
            page,
            candidate=candidate,
            source_url=url,
            source_title="立法院 IVOD 搜索页",
            days=days,
        )
        if candidate_rows:
            return candidate_rows, ""
        return [], f"{candidate['name']} IVOD：页面没有可读取的文字记录"

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch, candidate) for candidate in selected]
        for future in as_completed(futures):
            candidate_rows, warning = future.result()
            rows.extend(candidate_rows)
            if warning:
                warnings.append(warning)

    if rows:
        _write_cache(
            cache_name,
            rows=rows,
            warnings=warnings[:10],
            candidates=len(selected),
            scan_complete=True,
        )
        return SourceResult(
            rows=rows,
            warnings=warnings[:10],
            stats={"candidates": len(selected), "transcripts": len(rows)},
        )
    negative_warning = "立法院 IVOD 暂无可用网页文字记录"
    _write_cache(
        cache_name,
        rows=[],
        warnings=warnings[:10] + [negative_warning],
        candidates=len(selected),
        scan_complete=True,
    )
    return SourceResult(
        warnings=warnings[:10] + [negative_warning],
        stats={"candidates": len(selected), "transcripts": 0},
    )

