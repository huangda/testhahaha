"""直接从发布方 / 新闻站抓取选举民调，并做抽取、校验、去重与加权。

这一层补的是 `polls.py`（维基汇总表）与 `pollwatch.py`（只看标题）之间的空白：

* 找到最近的民调文章
* 抓正文，识别编码（含 Big5）
* 用候选人白名单抽取支持度，避免把「第 3 选区 54%」当成候选人支持度
* 校验明显错误（1.9%、把差距当支持度）
* 用 (机构, 调查结束日, 样本数) 去重
* 按「机构 × 时效(45 天半衰期，60 天归零) × 样本 × 类型」加权

用法：
    python -m tem polldirect --days 14 --limit 40
    python -m tem polldirect --days 30 --limit 80 --resolve 10

输出：
    config/polls_direct.json          抽取后的民调记录
    data/exports/poll_support.json    各选区主对战组合的加权支持度
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from concurrent.futures import as_completed
from pathlib import Path
from typing import Any, Iterable

from .paths import CONFIG_DIR, DATA_DIR, EXPORT_DIR, ensure_dirs

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

CNA_RSS = "https://feeds.feedburner.com/rsscna/politics"
CNA_SEARCH = "https://www.cna.com.tw/search/hysearchws.aspx?q={q}"
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
FORMOSA_HOME = "https://www.my-formosa.com.tw/"
TVBS_POLITICS = "https://news.tvbs.com.tw/politics"
KAMALAN_POLITICS = "https://www.kamalan-news.com/political"
GOOGLE_NEWS_BATCH_EXECUTE = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

POLL_WORDS = (
    "民調", "民调", "支持度", "支持率", "看好度", "領先", "领先", "選情", "选情",
)
REGION_HINT = re.compile(r"[\u4e00-\u9fa5]{2,4}(?:市|縣|县)")
DATE_IN_URL = re.compile(r"/(20\d{6})\d*\.aspx")
FORMOSA_DOC = re.compile(r"/DOC_\d+\.htm")
TVBS_ARTICLE = re.compile(r"(?:https?://news\.tvbs\.com\.tw)?/politics/\d+(?:\?.*)?$")
KAMALAN_ARTICLE = re.compile(
    r"(?:https?://www\.kamalan-news\.com)?/(?:political|local|activity|arts|industry)"
    r"/\d+/\d+(?:\?.*)?$"
)

# 机构权重：A 级白名单优先，B 级次之，党派内参/不详降权
SOURCE_TIERS: tuple[tuple[tuple[str, ...], float], ...] = (
    (("TVBS",), 1.20),
    (("聯合報", "中央社", "美麗島", "台湾民意基金会", "台灣民意基金會", "政大"), 1.10),
    (("震傳媒", "震传媒"), 0.95),
    (("世新",), 1.00),
    (("中時", "中时", "自由時報", "自由时报", "三立", "ETtoday", "風傳媒", "风传媒"), 0.90),
    (("趨勢", "趋势", "匯流", "汇流", "山水", "典通", "循證", "循证", "精湛",
      "關鍵", "关键", "皮爾森", "皮尔森", "鋒燦", "锋灿", "鉅聞", "巨闻", "Newtalk",
      "鉅聞", "巨聞", "菱傳", "菱传", "墨新聞", "墨新闻", "信民",
      "東森", "东森", "民視", "民视", "中評", "中评", "中國評論", "中国评论"), 0.85),
)
PARTY_INTERNAL = ("內參", "内参", "國民黨", "国民党", "民進黨", "民进党")

POLLSTERS = (
    "TVBS", "聯合報", "中國時報", "中時", "自由時報", "震傳媒", "信民協會", "信民", "信傳媒",
    "山水民意研究", "趨勢民調", "趋势民调", "匯流民調", "美丽岛电子报", "美麗島電子報",
    "台灣民意基金會",
    "政大選研", "政大選舉研究中心", "世新大學", "典通", "循證民調", "精湛民調",
    "關鍵調查", "皮爾森數據", "鉅聞天下", "巨聞天下", "巨闻天下", "震传媒",
    "菱傳媒", "菱传媒", "菱傳媒2.0", "菱传媒2.0", "鉅聞民調", "巨聞民調",
    "鋒燦傳媒", "ETtoday", "三立", "風傳媒", "東森新聞", "东森新闻",
    "年代", "中央社", "民視", "公視",
)
SHARE_BLOCKERS = (
    "差", "領先", "领先", "落後", "落后", "輸", "输", "贏", "赢", "超車", "超车", "反超",
    "勝", "胜", "不到",
    "投票率", "得票率", "換算", "换算", "估票", "滿意", "满意", "看好", "施政",
    "喜歡", "喜欢", "好感",
    "願景", "愿景",
    "下降", "上升", "增加", "減少", "减少", "漲幅", "涨幅",
)
DISTRICT_BLOCKERS = (
    "議員選區", "议员选区", "該選區", "该选区", "這區", "这区",
    "中正萬華", "中正万华", "士林北投", "大安文山", "中山大同", "松山信義", "松山信义",
    "板橋", "板桥", "新店", "中永和", "三峽", "三峡", "鶯歌", "莺歌",
    "土城", "樹林", "树林", "三重", "蘆洲", "芦洲", "中和", "永和",
)
SUBGROUP_BLOCKERS = (
    "支持者", "受訪者中", "受访者中", "上次", "2022", "2018",
    "轉而", "转而", "一定會投票", "一定会投票", "年齡", "年龄", "教育程度",
    "男性", "女性", "族群", "分區", "分区", "中立選民", "中立选民",
    "未表態選民", "未表态选民", "政黨傾向", "政党倾向",
)
HISTORY_CONTEXT = re.compile(
    r"(?:[1-9]|1[0-2])\s*月[^。\n]{0,12}(?:公布|公佈|调查|調查|结果|結果)"
    r"|(?:与|與|较|較)[^。\n]{0,8}(?:[1-9]|1[0-2])\s*月"
)
DISTRICT_CONTEXT = re.compile(
    r"第\s*\d+\s*(?:選區|选区)|議員選區|议员选区|(?:[一-龥]{2,6})(?:選區|选区)中"
)
METRIC_CONTEXT = re.compile(
    r"(?:換算|换算|得票率|投票率|滿意|满意|看好|施政)[^。\n]{0,28}$"
)


# ── HTTP / 编码 ───────────────────────────────────────────────────────────

def _decode(raw: bytes) -> str:
    m = re.search(rb'charset=["\']?([\w-]+)', raw[:4000], re.I)
    enc = (m.group(1).decode("ascii", "ignore") if m else "utf-8").lower()
    if enc in ("big5", "cp950", "ms950", "big5-hkscs"):
        enc = "big5"
    for candidate in (enc, "utf-8", "big5"):
        try:
            return raw.decode(candidate)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "ignore")


def fetch(url: str, *, timeout: int = 8, retries: int = 0) -> tuple[str, bytes]:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.geturl(), resp.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {url} ({last})")


def is_google_news_url(url: str) -> bool:
    """判断是否为 Google News 的 RSS 中转链接。"""
    return "news.google.com/rss/articles/" in (url or "")


def resolve_google_news_url(url: str, *, timeout: int = 10) -> str:
    """把 Google News RSS 中转链接还原成发布方原始网址。

    Google News 不再通过 HTTP 重定向直接跳转到原文。其文章页提供签名与时间戳，
    需要调用页面自身使用的 batchexecute 接口换取原始 URL。
    """
    if not is_google_news_url(url):
        return url

    article_id = urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]
    if not article_id:
        raise ValueError("Google News 链接缺少文章 ID")

    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        landing = _decode(resp.read())
    signature = re.search(r'data-n-a-sg=["\']([^"\']+)["\']', landing)
    timestamp = re.search(r'data-n-a-ts=["\']([^"\']+)["\']', landing)
    if not signature or not timestamp:
        raise ValueError("Google News 页面缺少网址还原参数")

    inner = [
        "garturlreq",
        [
            [
                "X", "X", ["X", "X"], None, None, 1, 1,
                "US:en", None, 1, None, None, None, None, None, 0, 1,
            ],
            "X",
            "X",
            1,
            [1, 1, 1],
            1,
            1,
            None,
            0,
            0,
            None,
            0,
        ],
        article_id,
        int(timestamp.group(1)),
        signature.group(1),
    ]
    request_item = [
        "Fbv4je",
        json.dumps(inner, ensure_ascii=False, separators=(",", ":")),
        None,
        "0",
    ]
    body = (
        "f.req="
        + urllib.parse.quote(
            json.dumps(
                [[request_item]],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    ).encode("utf-8")
    req = urllib.request.Request(
        GOOGLE_NEWS_BATCH_EXECUTE,
        data=body,
        headers={
            **UA,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = resp.read().decode("utf-8", "replace")

    # batchexecute 会在 JSON 前加上 ")]}'" 防脚本前缀。
    payload = payload.split("\n", 1)[-1].strip()
    resolved = ""
    try:
        rows = json.loads(payload)
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, list) and len(row) > 2 and row[1] == "Fbv4je":
                inner = json.loads(row[2])
                if isinstance(inner, list) and len(inner) > 1:
                    resolved = str(inner[1] or "")
                    break
    except (json.JSONDecodeError, TypeError, ValueError):
        resolved = ""

    if not resolved:
        match = re.search(r'(https?://[^"\\]+)', payload)
        resolved = match.group(1) if match else ""
    if not resolved.startswith(("http://", "https://")) or "news.google.com/" in resolved:
        raise ValueError("Google News 未返回有效原始网址")
    return resolved


def fetch_article(url: str, *, cache_days: int = 14) -> tuple[str, bytes, bool]:
    """抓取文章；网络暂时失败时回退到最近一次成功抓取的原文缓存。"""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cache_dir = DATA_DIR / "cache" / "polls"
    cache_path = cache_dir / f"{digest}.html"
    try:
        final, raw = fetch(url, retries=1)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(raw)
        (cache_dir / f"{digest}.url").write_text(final, encoding="utf-8")
        return final, raw, False
    except Exception:
        if not cache_path.exists():
            raise
        age = (time.time() - cache_path.stat().st_mtime) / 86400
        if age > cache_days:
            raise
        return url, cache_path.read_bytes(), True


# ── 正文 ──────────────────────────────────────────────────────────────────

def page_text(raw_html: str) -> tuple[str, str]:
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.S | re.I)
    if m:
        title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw_html, flags=re.S | re.I)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"</(?:p|div|li|h\d|tr|section|article)>", "\n", body, flags=re.I)
    body = html.unescape(re.sub(r"<[^>]+>", "", body))
    body = re.sub(r"[ \t\r\u3000]+", " ", body)
    body = re.sub(r"\n{1,}", "\n", body)
    return title, body.strip()


# ── 候选人与选区 ──────────────────────────────────────────────────────────

def load_universe() -> dict[str, list[str]]:
    """合併赔率、登记名册与民调表中出现的候选人，作为抽取白名单。

    赔率候选人排在前面，因此同一个县市的前两位通常也是最值得定向搜索的候选人。
    """
    from . import db

    buckets: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}

    def add(region: str, name: str) -> None:
        region = (region or "").strip()
        name = (name or "").strip()
        if not region or not name:
            return
        key = region.replace("臺", "台")
        labels.setdefault(key, region)
        if name not in buckets[key]:
            buckets[key].append(name)

    try:
        rows = db.fetch_latest_odds()
    except Exception:  # noqa: BLE001
        rows = []
    for row in rows:
        data = dict(row)
        if data.get("region_code") == "TW":
            continue
        name = data.get("candidate_zh") or ""
        region = data.get("region_name") or ""
        add(region, name)

    roster_path = CONFIG_DIR / "roster.json"
    try:
        roster = json.loads(roster_path.read_text(encoding="utf-8"))
        for region, candidates in (roster.get("regions") or {}).items():
            for item in candidates:
                add(region, item.get("name") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    # 维基汇总表有时会收录已退选但曾经出现在对战民调中的名字。
    try:
        poll_data = json.loads((CONFIG_DIR / "polls.json").read_text(encoding="utf-8"))
        for region, polls in (poll_data.get("raw") or {}).items():
            for poll in polls:
                for name in (poll.get("values") or {}):
                    if name and len(name) <= 12:
                        add(region, name)
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return {labels[key]: names for key, names in buckets.items()}


def candidate_query_groups(
    universe: dict[str, list[str]],
    *,
    per_region: int = 2,
) -> dict[str, list[str]]:
    """每个县市取赔率最高的前几位候选人，供定向搜索使用。"""
    return {
        region: [name for name in names[:max(1, per_region)] if name]
        for region, names in universe.items()
    }


def guess_region(text: str, regions: Iterable[str]) -> str:
    best = ""
    for name in regions:
        short = name.replace("臺", "台")
        matched = short in text or name in text
        if short == "台北市" and "北市" in text and "新北市" not in text:
            matched = True
        if short == "新竹市" and "竹市" in text:
            matched = True
        if short == "新竹县" and "竹縣" in text:
            matched = True
        if matched:
            if len(short) > len(best):
                best = name
    return best


def guess_region_by_candidates(
    text: str,
    universe: dict[str, list[str]],
) -> str:
    hits = [
        region for region, names in universe.items()
        if any(name and name in text for name in names)
    ]
    if len(hits) == 1:
        return hits[0]
    scored = [(sum(1 for name in universe[region] if name in text), region) for region in hits]
    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return ""
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return ""
    return scored[0][1]


def page_date(raw_html: str, text: str = "") -> str:
    """从 meta、JSON-LD 或可见发布时间推断文章日期。"""
    meta_patterns = (
        r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|datePublished)["\'][^>]+'
        r'content=["\'](20\d{2}-\d{1,2}-\d{1,2})',
        r'<meta[^>]+content=["\'](20\d{2}-\d{1,2}-\d{1,2})["\'][^>]+'
        r'(?:property|name)=["\'](?:article:published_time|datePublished)["\']',
        r'"datePublished"\s*:\s*"(20\d{2}-\d{1,2}-\d{1,2})',
    )
    for pattern in meta_patterns:
        match = re.search(pattern, raw_html, re.I | re.S)
        if match:
            try:
                return dt.date.fromisoformat(match.group(1)).isoformat()
            except ValueError:
                pass
    visible_patterns = (
        r"(?:發佈時間|發布時間|发布时间|發稿時間|发稿时间|日期)\s*[：:]\s*"
        r"(20\d{2})[./-](\d{1,2})[./-](\d{1,2})",
        r"(?:發佈者|發布者|作者)[^\n]{0,80}\n\s*"
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
    )
    for pattern in visible_patterns:
        visible = re.search(pattern, text)
        if visible:
            try:
                return dt.date(
                    int(visible.group(1)),
                    int(visible.group(2)),
                    int(visible.group(3)),
                ).isoformat()
            except ValueError:
                pass
    return ""


# ── 发现 ──────────────────────────────────────────────────────────────────

def _rss_items(xml: str) -> list[dict[str, str]]:
    out = []
    for item in re.findall(r"<item>(.*?)</item>", xml, re.S):
        title = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        link = re.search(r"<link>(.*?)</link>", item, re.S)
        pub = re.search(r"<pubDate>(.*?)</pubDate>", item, re.S)
        src = re.search(r"<source url=\"([^\"]+)\">(.*?)</source>", item, re.S)
        out.append({
            "title": html.unescape((title.group(1) if title else "").strip()),
            "url": (link.group(1) if link else "").strip(),
            "published": (pub.group(1) if pub else "").strip(),
            "source_url": (src.group(1) if src else "").strip(),
            "source": (html.unescape(src.group(2)) if src else "").strip(),
        })
    return out


def _rfc822_to_date(value: str) -> str:
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"):
        try:
            return dt.datetime.strptime(value.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def discover_cna_rss() -> list[dict[str, Any]]:
    try:
        _, raw = fetch(CNA_RSS)
    except Exception:  # noqa: BLE001
        return []
    items = []
    for item in _rss_items(_decode(raw)):
        if not any(w in item["title"] for w in POLL_WORDS):
            continue
        items.append({
            "url": item["url"], "title": item["title"],
            "source": "中央社", "date": _rfc822_to_date(item["published"]),
            "direct": True,
        })
    return items


def discover_cna_search(regions: Iterable[str], *, delay: float = 0.6) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for region in regions:
        q = f"{region.replace('臺', '台')} 民調"
        try:
            _, raw = fetch(CNA_SEARCH.format(q=urllib.parse.quote(q)))
        except Exception:  # noqa: BLE001
            time.sleep(delay)
            continue
        text = _decode(raw)
        for href, label in re.findall(r'href="(/news/[^"]+\.aspx)"[^>]*>(.*?)</a>', text, re.S):
            title = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
            if not title or href in seen:
                continue
            if not any(w in title for w in POLL_WORDS):
                continue
            if region.replace("臺", "台") not in title.replace("臺", "台"):
                continue
            seen.add(href)
            m = DATE_IN_URL.search(href)
            date = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else ""
            out.append({
                "url": "https://www.cna.com.tw" + href, "title": title,
                "source": "中央社", "date": date, "direct": True,
            })
        time.sleep(delay)
    return out


def _article_links(raw_html: str, base_url: str, url_pattern: re.Pattern[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        raw_html,
        re.I | re.S,
    ):
        href = html.unescape(match.group(1)).strip()
        if not url_pattern.search(href):
            continue
        url = urllib.parse.urljoin(base_url, href)
        if url in seen:
            continue
        title = html.unescape(re.sub(r"<[^>]+>", " ", match.group(2)))
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            continue
        seen.add(url)
        out.append({"url": url, "title": title})
    return out


def discover_formosa() -> list[dict[str, Any]]:
    """美丽岛电子报首页会直接列最新民调稿，编码为 Big5。"""
    try:
        _, raw = fetch(FORMOSA_HOME)
    except Exception:  # noqa: BLE001
        return []
    source = _decode(raw)
    out: list[dict[str, Any]] = []
    for item in _article_links(source, FORMOSA_HOME, FORMOSA_DOC):
        if not any(word in item["title"] for word in POLL_WORDS):
            continue
        out.append({
            **item,
            "source": "美丽岛电子报",
            "date": "",
            "direct": True,
        })
    return out


def discover_tvbs() -> list[dict[str, Any]]:
    """TVBS 政治版首页会列出最新民调新闻，链接可直接抓正文。"""
    try:
        _, raw = fetch(TVBS_POLITICS)
    except Exception:  # noqa: BLE001
        return []
    source = _decode(raw)
    out: list[dict[str, Any]] = []
    for item in _article_links(source, TVBS_POLITICS, TVBS_ARTICLE):
        # 同一篇新闻在推荐位、列表位会重复出现；URL 去重后再看标题。
        if "民調" not in item["title"] and "民调" not in item["title"]:
            continue
        item["url"] = item["url"].split("?", 1)[0]
        out.append({
            **item,
            "source": "TVBS",
            "date": "",
            "direct": True,
        })
    unique: dict[str, dict[str, Any]] = {}
    for item in out:
        unique.setdefault(item["url"], item)
    return list(unique.values())


def discover_kamalan() -> list[dict[str, Any]]:
    """从葛瑪蘭新聞網政治栏目补充宜兰本地民调原文。"""
    try:
        _, raw = fetch(KAMALAN_POLITICS)
    except Exception:  # noqa: BLE001
        return []
    source = _decode(raw)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _article_links(source, KAMALAN_POLITICS, KAMALAN_ARTICLE):
        url = item["url"].split("?", 1)[0]
        if url in seen or not any(word in item["title"] for word in POLL_WORDS):
            continue
        seen.add(url)
        out.append({
            **item,
            "url": url,
            "source": "葛瑪蘭新聞網",
            "date": "",
            "direct": True,
            "priority": 2,
        })
    return out


def discover_candidate_cna(
    queries: dict[str, list[str]],
    *,
    delay: float = 0.6,
) -> list[dict[str, Any]]:
    """按「县市 + 主要候选人」搜索中央社，减少纯议题新闻的噪声。"""
    def search_one(region: str, names: list[str]) -> list[dict[str, Any]]:
        region_q = region.replace("臺", "台")
        q = f"{region_q} {' '.join(names[:2])} 民調"
        try:
            _, raw = fetch(CNA_SEARCH.format(q=urllib.parse.quote(q)))
        except Exception:  # noqa: BLE001
            return []
        found: list[dict[str, Any]] = []
        text = _decode(raw)
        for href, label in re.findall(r'href="(/news/[^"]+\.aspx)"[^>]*>(.*?)</a>', text, re.S):
            title = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
            if not title:
                continue
            if not any(word in title for word in POLL_WORDS):
                continue
            compact_title = title.replace("臺", "台")
            if region_q not in compact_title and not any(
                name.replace("臺", "台") in compact_title for name in names
            ):
                continue
            match = DATE_IN_URL.search(href)
            date = (
                f"{match.group(1)[:4]}-{match.group(1)[4:6]}-{match.group(1)[6:8]}"
                if match else ""
            )
            found.append({
                "url": "https://www.cna.com.tw" + href,
                "title": title,
                "source": "中央社",
                "date": date,
                "direct": True,
            })
        return found

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    pool = ThreadPoolExecutor(max_workers=6)
    futures = {
        pool.submit(search_one, region, names): region
        for region, names in queries.items()
    }
    try:
        for future in as_completed(futures, timeout=60):
            for item in future.result():
                if item["url"] not in seen:
                    seen.add(item["url"])
                    out.append(item)
    except FutureTimeout:
        pass
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    return out


def discover_from_news_db(
    universe: dict[str, list[str]],
    *,
    days: int,
    per_region: int = 5,
) -> list[dict[str, Any]]:
    """从系统已采集的 RSS 新闻中挑出可直接抓取的民调原文。

    Google News 中转链接会在实际抓取时还原成发布方原文。每个县市只取
    若干篇，避免新北市占满全部名额。
    """
    from . import db

    try:
        rows = db.fetch_news_since(days=days, limit=5000)
    except Exception:  # noqa: BLE001
        return []
    regions = list(universe)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        data = dict(row)
        url = data.get("url") or ""
        title = data.get("title") or ""
        if not url:
            continue
        if not any(word in title for word in POLL_WORDS):
            continue
        region = guess_region(title, regions)
        if not region:
            named = {
                name for names in universe.values() for name in names
                if name and name in title
            }
            if named:
                region = next(
                    (
                        candidate_region for candidate_region, names in universe.items()
                        if named & set(names)
                    ),
                    "",
                )
        if not region:
            continue
        top2 = universe.get(region, [])[:2]
        score = sum(1 for name in top2 if name in title)
        if any(token in title for token in ("%", "％", "支持度", "支持率", "打平", "差距")):
            score += 2
        if any(
            name in title
            for name in ("自由時報", "TVBS", "ETtoday", "Yahoo", "葛瑪蘭新聞網")
        ):
            score += 1
        history = discovery_history_reference(f"{data.get('source_name') or ''} {title}") or {}
        score += int(history.get("priority") or 0) * 2
        published = (data.get("published_at") or "")[:10]
        grouped[region].append({
            "url": url,
            "title": title,
            "source": data.get("source_name") or "",
            "date": published,
            "direct": True,
            "priority": score,
            "history_priority": int(history.get("priority") or 0),
            "history_reference": history or None,
            "google_news": is_google_news_url(url),
        })
    out: list[dict[str, Any]] = []
    for region in regions:
        items = sorted(
            grouped.get(region, []),
            key=lambda item: (item["priority"], item["date"]),
            reverse=True,
        )
        seen_titles: set[str] = set()
        for item in items:
            signature = re.sub(r"[^\w\u4e00-\u9fff]+", "", item["title"])[:36]
            if signature in seen_titles:
                continue
            seen_titles.add(signature)
            out.append(item)
            if len(seen_titles) >= per_region:
                break
    return out


def discover_google(regions: Iterable[str], days: int, *, delay: float = 0.8) -> list[dict[str, Any]]:
    """只做发现：Google 新闻链接需要二次解析，这里先保留标题与来源。"""
    out: list[dict[str, Any]] = []
    for region in regions:
        q = f'"{region.replace("臺", "台")}" (民調 OR 支持度 OR 領先) when:{days}d'
        try:
            _, raw = fetch(GOOGLE_NEWS.format(q=urllib.parse.quote(q)))
        except Exception:  # noqa: BLE001
            time.sleep(delay)
            continue
        for item in _rss_items(_decode(raw)):
            if not any(w in item["title"] for w in POLL_WORDS):
                continue
            out.append({
                "url": item["url"], "title": item["title"],
                "source": item["source"] or "Google News",
                "date": _rfc822_to_date(item["published"]),
                "direct": False,
            })
        time.sleep(delay)
    return out


# ── 抽取 ──────────────────────────────────────────────────────────────────

def _num(value: str) -> float:
    try:
        return float(value.replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def _find(patterns: Iterable[str], text: str, default: str = "") -> str:
    for pattern in patterns:
        m = re.search(pattern, text)
        if m:
            return m.group(1).strip()
    return default


def _share_for(
    name: str,
    body: str,
    title: str = "",
    candidates: Iterable[str] = (),
) -> float | None:
    """从候选人附近的句中取出主口径支持度。

    同一篇报道常同时出现全市支持度、某个议员选区支持度、看好度与满意度。
    不能简单取第一颗百分比，而要优先「N% 市民/受访者支持某人」这类完整口径。
    """
    escaped = re.escape(name)
    scored: list[tuple[float, int, float]] = []
    patterns = (
        (
            escaped + r"[^。\n]{0,20}?(?:支持度|支持率)"
            r"(?:為|为|達|达|約|约|:|：)?\s*(\d{1,2}(?:\.\d+)?)\s*[％%]",
            15.0,
            "explicit-support",
        ),
        (
            escaped + r"[^。\n]{0,12}?(\d{1,2}(?:\.\d+)?)\s*[％%]"
            r"[^。\n]{0,8}?(?:支持度|支持率)",
            14.0,
            "explicit-support",
        ),
        (
            r"(\d{1,2}(?:\.\d+)?)\s*[％%][^。\n％%]{0,28}?"
            r"(?:市民|選民|选民|受訪者|受访者|民眾|民众)?[^。\n％%]{0,12}?"
            r"(?:表示支持|表態支持|表态支持|支持|投給|投给)[^。\n％%，,；;：:]{0,16}?" + escaped,
            11.0,
            "support-then-name",
        ),
        (
            r"(\d{1,2}(?:\.\d+)?)\s*[％%]\s*(?:的)?(?:市民|選民|选民|受訪者|受访者|民眾|民众)?"
            r"[^。\n]{0,8}?" + escaped,
            8.0,
            "percent-then-name",
        ),
        (
            escaped + r"[^。\n％%]{0,8}?(\d{1,2}(?:\.\d+)?)\s*[％%]",
            5.0,
            "name-then-percent",
        ),
        (
            escaped + r"[^。\n]{0,14}?(?:支持度|支持率)?[^。\n]{0,6}?"
            r"(?:為|为|達|达|有|獲得|获得|拿下)\s*(\d{1,2}(?:\.\d+)?)\s*[％%]",
            6.0,
            "name-support-percent",
        ),
    )
    title_start = 0
    title_end = len(title) if title else 0
    other_names = [item for item in candidates if item and item != name]
    for pattern, base_score, kind in patterns:
        for match in re.finditer(pattern, body):
            number = _num(match.group(1))
            if not 0 < number <= 100:
                continue
            context = body[max(0, match.start() - 38):min(len(body), match.end() + 24)]
            left_breaks = [
                position
                for position in (
                    body.rfind("。", 0, match.start()),
                    body.rfind("\n", 0, match.start()),
                )
                if position >= 0
            ]
            right_breaks = [
                position
                for position in (
                    body.find("。", match.end()),
                    body.find("\n", match.end()),
                )
                if position >= 0
            ]
            sentence_start = max(left_breaks, default=0) + 1
            sentence_end = min(right_breaks, default=len(body))
            sentence_context = body[sentence_start:sentence_end]
            matched = match.group(0)
            prefix = body[max(0, match.start() - 24):match.start()]
            suffix = body[match.end():min(len(body), match.end() + 18)]
            in_title = bool(title and match.start() < len(title))
            if kind != "explicit-support" and any(
                word in matched for word in SHARE_BLOCKERS
            ):
                continue
            metric_sentence = sentence_context
            if (
                any(
                    word in metric_sentence
                    for word in (
                        "看好", "滿意", "满意", "表現", "表现",
                        "可能當選", "可能当选", "當選機會", "当选机会",
                        "最適合", "最适合", "城市治理", "治理", "最能做好",
                        "帶領", "带领", "認同", "认同", "發展", "发展",
                    )
                )
                and not any(word in metric_sentence for word in ("支持度", "支持率"))
            ):
                continue
            if re.match(r"\s*(?:個|个)?百分點", suffix):
                continue
            if (
                kind == "name-then-percent"
                and any(
                    word in prefix
                    for word in ("領先", "领先", "落後", "落后", "差距", "相差")
                )
                and re.search(r"(?:近|約|约|達|达|超過|超过|大幅)\s*\d", matched)
            ):
                continue
            trailing = body[match.end():min(len(body), match.end() + 14)]
            if re.match(
                r"\s*(?:的)?(?:喜歡|喜欢|好感|看好|滿意|满意|施政|願景|愿景)",
                trailing,
            ):
                continue
            if any(word in matched for word in SUBGROUP_BLOCKERS) or any(
                word in sentence_context for word in SUBGROUP_BLOCKERS
            ):
                continue
            if HISTORY_CONTEXT.search(context):
                continue
            if METRIC_CONTEXT.search(body[max(0, match.start() - 44):match.start()]):
                continue
            if any(word in sentence_context for word in DISTRICT_BLOCKERS) or DISTRICT_CONTEXT.search(sentence_context):
                continue
            score = base_score
            if (
                in_title
                and kind == "name-then-percent"
                and any(
                    word in prefix
                    for word in ("領先", "领先", "落後", "落后", "差距", "相差")
                )
            ):
                # “甲42%领先乙31%”中的乙31%可以由正文确认；
                # 标题里的候选数字若缺少支持度口径，不给标题加权。
                score -= 10
            if kind == "percent-then-name":
                prefix = body[max(0, match.start() - 16):match.start()]
                previous_name = any(other in prefix for other in other_names)
                separated = any(sep in prefix[-4:] for sep in ("、", "，", ",", "；", ";"))
                crosses_name = any(other in matched for other in other_names)
                if crosses_name or (previous_name and not separated):
                    continue
            if any(word in context for word in ("全市", "市民", "受訪者", "受访者", "選民", "选民")):
                score += 4
            if title and title_start <= match.start() <= title_end:
                # 标题中的支持度通常就是该篇报道的主口径，权重应明显高于
                # 正文中的交叉分析、看好度与分区数字。
                score += 8
            if number < 15:
                score -= 6
            score += max(0.0, 3.0 * (1.0 - match.start() / max(len(body), 1)))
            scored.append((score, match.start(), number))
    if not scored:
        return None
    scored.sort(key=lambda item: (-item[0], item[1]))
    best_score, _position, value = scored[0]
    return value if best_score >= 4.0 else None


def _leading_pair_shares(body: str, candidates: Iterable[str]) -> dict[str, float]:
    """优先抓取首段「A以X%支持度领先B Y%」的全样本对战数字。"""
    names = [name for name in candidates if name]
    names.sort(key=len, reverse=True)
    if len(names) < 2:
        return {}
    name_pattern = "|".join(re.escape(name) for name in names)
    patterns = (
        re.compile(
            rf"(?P<a>{name_pattern})[^。\n]{{0,30}}?"
            rf"(?P<av>\d{{1,2}}(?:\.\d+)?)\s*[%％][^。\n]{{0,20}}?"
            rf"(?:支持度|支持率)?[^。\n]{{0,16}}?(?:領先|领先)"
            rf"[^。\n]{{0,30}}?(?P<b>{name_pattern})[^。\n]{{0,12}}?"
            rf"(?P<bv>\d{{1,2}}(?:\.\d+)?)\s*[%％]"
        ),
        re.compile(
            rf"(?:支持度|支持率)[^。\n]{{0,30}}?(?P<a>{name_pattern})"
            rf"[^。\n]{{0,12}}?(?P<av>\d{{1,2}}(?:\.\d+)?)\s*[%％]"
            rf"[^。\n]{{0,16}}?(?P<b>{name_pattern})[^。\n]{{0,12}}?"
            rf"(?P<bv>\d{{1,2}}(?:\.\d+)?)\s*[%％]"
        ),
    )
    body_start = body.find("\n")
    body_start = body_start + 1 if body_start >= 0 else 0
    headline = body.split("\n", 1)[0].strip()
    for pattern in patterns:
        for match in pattern.finditer(body[:3000]):
            if match.start() < body_start:
                continue
            line_start = body.rfind("\n", 0, match.start()) + 1
            line_end = body.find("\n", match.end())
            line_text = body[line_start:line_end if line_end >= 0 else len(body)].strip()
            if headline and headline in line_text:
                continue
            a, b = match.group("a"), match.group("b")
            if a == b:
                continue
            sentence_start = max(
                body.rfind("。", 0, match.start()),
                body.rfind("\n", 0, match.start()),
                0,
            )
            sentence_end_candidates = [
                pos for pos in (
                    body.find("。", match.end()),
                    body.find("\n", match.end()),
                )
                if pos >= 0
            ]
            sentence = body[sentence_start:min(sentence_end_candidates or [len(body)])]
            if any(word in sentence for word in SUBGROUP_BLOCKERS):
                continue
            av, bv = _num(match.group("av")), _num(match.group("bv"))
            if 0 < av <= 100 and 0 < bv <= 100 and av != bv:
                return {a: av, b: bv}
    return {}


def _ambiguous_title_pair(title: str, candidates: Iterable[str]) -> bool:
    """标题「A X%领先B Y%」未写明支持度时，Y可能是差距，不能单独入库。"""
    names = [name for name in candidates if name]
    names.sort(key=len, reverse=True)
    if len(names) < 2:
        return False
    name_pattern = "|".join(re.escape(name) for name in names)
    match = re.search(
        rf"(?:{name_pattern})[^。\n]{{0,16}}?\d{{1,3}}(?:\.\d+)?\s*[%％]"
        rf"[^。\n]{{0,16}}?(?:領先|领先)"
        rf"[^。\n]{{0,20}}?(?:{name_pattern})[^。\n]{{0,10}}?"
        rf"\d{{1,3}}(?:\.\d+)?\s*[%％]",
        title,
    )
    return bool(match and not any(word in match.group(0) for word in ("支持度", "支持率")))


def _fieldwork_dates(text: str, article_date: str) -> tuple[str, str]:
    """识别常见调查区间，并正确处理跨月与缺年份。"""
    pattern = re.compile(
        r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日"
        r"\s*(?:至|到|[-－—])\s*(?:(\d{1,2})\s*月\s*)?(\d{1,2})\s*日"
    )
    match = pattern.search(text)
    if not match:
        return "", ""
    ref: dt.date | None = None
    if article_date:
        try:
            ref = dt.date.fromisoformat(article_date[:10])
        except ValueError:
            ref = None
    year = int(match.group(1) or (ref or dt.date.today()).year)
    month1, day1 = int(match.group(2)), int(match.group(3))
    month2, day2 = int(match.group(4) or month1), int(match.group(5))
    try:
        start = dt.date(year, month1, day1)
        end = dt.date(year + (1 if month2 < month1 else 0), month2, day2)
    except ValueError:
        return "", ""
    if end < start:
        try:
            end = dt.date(year + 1, month2, day2)
        except ValueError:
            return "", ""
    return start.isoformat(), end.isoformat()


def _pollster_from(title: str, body: str) -> str:
    """区分「转载媒体」与真正执行/委托民调的机构。"""
    for name in POLLSTERS:
        if re.search(re.escape(name) + r"[^，。\n]{0,10}民[調调]", title):
            return name
    for name in POLLSTERS:
        if re.search(r"(?:委託|委托|由|《)\s*" + re.escape(name), body[:1600]):
            return name
    for name in POLLSTERS:
        if re.search(re.escape(name) + r"[^。\n]{0,8}民[調调]", body[:1600]):
            return name
    # 完整报道常在末段才写「这项民调由…执行」，不能只扫描正文前 1600 字。
    methodology_sentences = re.findall(
        r"[^。\n]*(?:這項|这项|本次)?民[調调][^。\n]*"
        r"(?:執行|执行|進行|进行|完成)[^。\n]*",
        body,
    )
    for sentence in methodology_sentences:
        hits = sorted(
            (
                sentence.find(name),
                name,
            )
            for name in POLLSTERS
            if name in sentence
        )
        if hits:
            return hits[0][1]
    site_names = {"美麗島電子報", "自由時報", "中央社", "民視", "三立", "風傳媒"}
    hits = sorted(
        (body.find(name), name)
        for name in POLLSTERS
        if name not in site_names and name in body[:2000]
    )
    if hits:
        return hits[0][1]
    return (
        _find(
            [
                r"[《【]([^》】]{2,20}(?:民調|民调|民意調查|民意调查))[》】]",
                r"(?:委託|委托)\s*([\u4e00-\u9fa5]{2,14}(?:民調|民调|民意調查|民意调查|研究|數據|数据))",
                r"([\u4e00-\u9fa5]{2,12}(?:民調|民意調查|基金會|協會|大學|中心))",
            ],
            body,
        )
        or ""
    )


def extract(
    text: str,
    title: str,
    region: str,
    candidates: list[str],
    *,
    article_date: str = "",
) -> dict[str, Any] | None:
    body = f"{title}\n{text}"
    leading_shares = _leading_pair_shares(body, candidates)
    if _ambiguous_title_pair(title, candidates) and len(leading_shares) < 2:
        return None
    shares: dict[str, float] = {}
    for name in candidates:
        if not name:
            continue
        value = _share_for(name, body, title, candidates)
        if value is not None:
            shares[name] = value
    if len(leading_shares) >= 2:
        shares = leading_shares
    if len(shares) >= 2 and len({round(value, 1) for value in shares.values()}) == 1:
        title_shares: dict[str, float] = {}
        for name in candidates:
            value = _share_for(name, title, title, candidates)
            if value is not None:
                title_shares[name] = value
        if len(title_shares) >= 2 and len({round(value, 1) for value in title_shares.values()}) > 1:
            shares = title_shares
    if len(shares) >= 3:
        ranked_values = sorted(shares.values(), reverse=True)
        keep_floor = max(8.0, ranked_values[1] * 0.5)
        shares = {name: value for name, value in shares.items() if value >= keep_floor}
    if len(shares) < 2:
        return None

    fieldwork_start, fieldwork_end = _fieldwork_dates(body, article_date)
    sample_size = _find(
        [
            r"(?:成功完訪|完成訪問|完成访问|有效樣本|有效样本)\s*"
            r"(?<![\d,])((?:\d{1,3}(?:,\d{3})+)|(?:\d{3,5}))\s*(?:人|份|位)",
            r"(?:成功訪問|成功访问|成功完訪|成功完访)(?:了)?\s*"
            r"(?<![\d,])((?:\d{1,3}(?:,\d{3})+)|(?:\d{3,5}))\s*(?:位|人|份)",
            r"(?<![\d,])((?:\d{1,3}(?:,\d{3})+)|(?:\d{3,5}))\s*"
            r"(?:份有效樣本|位受訪者|人受訪|人完訪)",
            r"訪問\s*(?<![\d,])((?:\d{1,3}(?:,\d{3})+)|(?:\d{3,5}))\s*(?:人|位)",
        ],
        body,
        "0",
    )
    margin = _find(
        [
            r"誤差[^。]{0,16}?(?:正負|正负|±)\s*([\d.]+)",
            r"误差[^。]{0,16}?(?:正负|±)\s*([\d.]+)",
        ],
        body,
    )

    record = {
        "region": region,
        "title": title,
        "pollster": _pollster_from(title, body),
        "sponsor": _find(
            [
                r"(?:這項民調|这项民调|本次民調|本次民调)\s*(?:由|委託|委托)?"
                r"([\u4e00-\u9fa5]{2,18}(?:基金會|基金会|協會|协会|辦公室|办公室))",
                r"([\u4e00-\u9fa5]{2,18}(?:黨部|党部|基金會|基金会|協會|协会|辦公室|办公室))"
                r"\s*(?:出資)?\s*(?:委託|委托)",
                r"([\u4e00-\u9fa5]{2,18}(?:基金會|基金会|協會|协会|辦公室|办公室))"
                r"\s*(?:出資|出资)(?:[、，,]\s*|[^。\n]{0,12}(?:委託|委托))",
            ],
            body,
        ),
        "executor": _find(
            [
                r"(?:委託|委托)([\u4e00-\u9fa5A-Za-z]{2,16})(?:執行|执行|進行|进行)",
                r"(?:由)?([\u4e00-\u9fa5A-Za-z]{2,16}(?:民調|民调|民意調查|民意调查|公司))"
                r"(?:執行|执行|進行|进行|完成)",
            ],
            body,
        ),
        "fieldwork_start": fieldwork_start,
        "fieldwork_end": fieldwork_end,
        "sample_size": int(_num(sample_size)),
        "margin_of_error": margin,
        "shares": shares,
        "undecided": _num(_find([r"(\d{1,2}(?:\.\d+)?)\s*[％%][^。]{0,8}(?:尚未決定|未表態|未表态|沒意見)"], body)),
        "margin": _num(_find([r"相差\s*(\d+(?:\.\d+)?)\s*個百分點"], body)),
        "method": "电话访问" if any(word in body for word in ("電話訪問", "电话访问", "CATI")) else "",
        "article_date": article_date,
        "kind": "politics",
        "tier": "direct",
        "source": "direct",
        "url": "",
    }
    if len(shares) >= 2:
        ranked = sorted(shares.values(), reverse=True)
        record["margin"] = round(ranked[0] - ranked[1], 1)
    return record


# ── 校验 / 去重 ───────────────────────────────────────────────────────────

def validate(record: dict[str, Any], today: dt.date | None = None) -> list[str]:
    today = today or dt.date.today()
    errors: list[str] = []
    shares = record.get("shares") or {}
    if len(shares) < 2:
        errors.append("支持度少于 2 人")
    values = list(shares.values())
    if values and max(values) < 15:
        errors.append("最高支持度 <15%（疑似解析错误）")
    if values and max(values) > 85:
        errors.append("最高支持度 >85%（疑似误取投票率或满意度）")
    if values and min(values) < 8 and max(values) > 30:
        errors.append("最低支持度 <8%（疑似把差距当支持度）")
    if sum(values) > 100.5:
        errors.append("支持度总和 >100%")
    title = str(record.get("title") or "")
    report_type = str(record.get("report_type") or "")
    if re.search(
        r"(?:半年|個月|个月|一年|歷時|历时).{0,10}"
        r"(?:狂追|追趕|追赶|落後|落后|超前).{0,10}"
        r"\d{1,2}(?:\.\d+)?\s*[%％]",
        title,
    ) or (
        report_type == "media-cite"
        and any(
            marker in title
            for marker in ("斷言", "断言", "將成", "将成", "會贏", "会赢")
        )
    ):
        errors.append("人物评论或趋势叙述，不是本次对战民调")
    if report_type == "media-cite":
        title_percentages = re.findall(
            r"(\d{1,3}(?:\.\d+)?)\s*[%％]",
            title,
        )
        title_candidates = [name for name in shares if name and name in title]
        if len(title_percentages) < 2 or len(title_candidates) < 2:
            errors.append("媒体转述标题缺少两位候选人的明确百分比")
    title_margin = 0.0
    title_margin_match = re.search(
        r"(?:差距|相差|僅差|仅差|只差)"
        r"[^。\n%％]{0,18}?(\d{1,2}(?:\.\d+)?)\s*(?:個|个)?(?:百分點|[%％])",
        title,
    )
    if not title_margin_match:
        title_margin_match = re.search(
            r"(?:領先|领先|緊咬|紧咬|落後|落后|輾壓|碾压|小勝|小胜|勝過|胜过)"
            r"[^。\n%％]{0,18}?(\d{1,2}(?:\.\d+)?)\s*(?:個|个)?百分點",
            title,
        )
    if not title_margin_match:
        title_margin_match = re.search(
            r"(?:領先|领先)[^。\n%％]{0,14}?"
            r"(?:近|約|约|達|达|超過|超过)\s*(\d{1,2}(?:\.\d+)?)\s*[%％]",
            title,
        )
    if title_margin_match:
        title_margin = _num(title_margin_match.group(1))
    parsed_margin = float(record.get("margin") or 0)
    if title_margin and parsed_margin and abs(title_margin - parsed_margin) > 2.5:
        errors.append(
            f"领先差距不一致（标题 {title_margin:g}，解析 {parsed_margin:g}）"
        )
    if len(values) >= 2 and len({round(value, 1) for value in values}) == 1:
        title_numbers = {
            round(_num(value), 1)
            for value in re.findall(r"(\d{1,2}(?:\.\d+)?)\s*[%％]", title)
        }
        if len(title_numbers) >= 2 or (title_margin and not parsed_margin):
            errors.append("两位候选人支持度完全相同，但与标题数字不一致")
    sample = record.get("sample_size") or 0
    if sample and not 300 <= sample <= 5000:
        errors.append(f"样本数异常：{sample}")
    for key in ("article_date", "fieldwork_end", "fieldwork_start"):
        value = record.get(key) or ""
        if value:
            try:
                d = dt.date.fromisoformat(value)
            except ValueError:
                errors.append(f"{key} 格式错误")
                continue
            if d > today:
                errors.append(f"{key} 晚于今天")
    return errors


def _reparse_cached_record(
    record: dict[str, Any],
    regions: list[str],
    universe: dict[str, list[str]],
) -> dict[str, Any] | None:
    """用本地原文缓存重新解析旧记录，不重新访问网络。"""
    url = str(record.get("url") or "")
    if not url or "news.google.com/" in url:
        return None
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    path = DATA_DIR / "cache" / "polls" / f"{digest}.html"
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    title, text = page_text(_decode(raw))
    region = (
        guess_region(title, regions)
        or guess_region_by_candidates(title, universe)
        or str(record.get("region") or "")
    )
    if not region:
        return None
    parsed = extract(
        text,
        title,
        region,
        universe.get(region) or [],
        article_date=str(record.get("article_date") or ""),
    )
    if not parsed or validate(parsed):
        return None
    parsed["url"] = url
    parsed["source"] = record.get("source") or parsed.get("source") or "direct"
    parsed["syndicated"] = list(record.get("syndicated") or [])
    has_dates = bool(parsed.get("fieldwork_start") and parsed.get("fieldwork_end"))
    has_sample = bool(parsed.get("sample_size"))
    parsed["report_type"] = (
        "full" if has_dates and has_sample
        else "partial" if has_dates or has_sample
        else "media-cite"
    )
    parsed["evidence_quality"] = (
        1.0 if parsed["report_type"] == "full"
        else 0.75 if parsed["report_type"] == "partial"
        else 0.4
    )
    parsed["quality_warnings"] = []
    return parsed


def dedupe(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple, dict[str, Any]] = {}
    for record in records:
        shares = record.get("shares") or {}
        pollster = re.sub(r"\s+", "", record.get("pollster") or "")
        key = (
            pollster,
            record.get("fieldwork_end") or record.get("fieldwork_start") or record.get("article_date") or "",
            int(record.get("sample_size") or 0)
            or tuple(sorted((k, round(v, 1)) for k, v in shares.items())),
        )
        if not all(key[:2]):
            key = (
                record.get("region") or "",
                record.get("article_date") or record.get("fieldwork_end") or "",
                tuple(sorted((k, round(v, 1)) for k, v in shares.items())),
            )
        old = best.get(key)
        evidence = {
            "url": record.get("url") or "",
            "source": record.get("source") or "",
            "title": record.get("title") or "",
        }
        if old is None:
            record["syndicated"] = [evidence] if evidence["url"] else []
            best[key] = record
            continue
        syndicated = old.setdefault("syndicated", [])
        if evidence["url"] and evidence["url"] not in {item.get("url") for item in syndicated}:
            syndicated.append(evidence)
        if (
            len(record.get("shares") or {}),
            bool(record.get("fieldwork_end")),
            bool(record.get("sample_size")),
        ) > (
            len(old.get("shares") or {}),
            bool(old.get("fieldwork_end")),
            bool(old.get("sample_size")),
        ):
            record["syndicated"] = syndicated
            best[key] = record
    # 第二层：同一份调查被两个页面转载时，其中一个页面可能没写调查日期。
    # 若县市、机构、样本数及所有支持度完全一致，仍视为同一份民调。
    merged: dict[tuple, dict[str, Any]] = {}
    for record in best.values():
        shares = record.get("shares") or {}
        sample = int(record.get("sample_size") or 0)
        key = (
            record.get("region") or "",
            sample or record.get("article_date") or "",
            tuple(sorted((name, round(float(value), 1)) for name, value in shares.items())),
        )
        old = merged.get(key)
        if old is None:
            merged[key] = record
            continue
        evidence = list(old.get("syndicated") or []) + list(record.get("syndicated") or [])
        density = (
            bool(record.get("fieldwork_end")),
            bool(record.get("fieldwork_start")),
            len(record.get("shares") or {}),
        )
        old_density = (
            bool(old.get("fieldwork_end")),
            bool(old.get("fieldwork_start")),
            len(old.get("shares") or {}),
        )
        chosen = record if density > old_density else old
        pollsters = [
            value for value in (old.get("pollster"), record.get("pollster"))
            if value and value not in ("不詳", "不详")
        ]
        if pollsters:
            chosen["pollster"] = min(pollsters, key=lambda value: (len(value), value))
        chosen["syndicated"] = [
            item for index, item in enumerate(evidence)
            if item.get("url") and item.get("url") not in {
                previous.get("url") for previous in evidence[:index]
            }
        ]
        merged[key] = chosen
    candidates = list(merged.values())
    dropped: set[int] = set()
    for i, high in enumerate(candidates):
        high_shares = high.get("shares") or {}
        high_quality = float(high.get("evidence_quality") or 0)
        if high_quality <= 0:
            continue
        high_date = high.get("fieldwork_end") or high.get("article_date") or ""
        for j, low in enumerate(candidates):
            if i == j or j in dropped:
                continue
            low_shares = low.get("shares") or {}
            low_quality = float(low.get("evidence_quality") or 0)
            if high.get("region") != low.get("region") or high_quality <= low_quality:
                continue
            if set(high_shares) != set(low_shares) or not high_shares:
                continue
            echoed = sum(
                1 for name, value in high_shares.items()
                if abs(float(value) - float(low_shares.get(name, -999))) <= 0.1
            )
            if not echoed:
                continue
            low_date = low.get("fieldwork_end") or low.get("article_date") or ""
            try:
                age = abs(
                    (dt.date.fromisoformat(high_date[:10])
                     - dt.date.fromisoformat(low_date[:10])).days
                )
            except (ValueError, TypeError):
                age = 0
            if age > 14:
                continue
            evidence = high.setdefault("syndicated", [])
            for item in low.get("syndicated") or []:
                if item.get("url") and item.get("url") not in {x.get("url") for x in evidence}:
                    evidence.append(item)
            dropped.add(j)
    return [record for index, record in enumerate(candidates) if index not in dropped]


# ── 加权 ──────────────────────────────────────────────────────────────────

def source_weight(source: str) -> float:
    source = source or ""
    if any(k in source for k in PARTY_INTERNAL):
        return 0.2
    if not source or "不詳" in source or "不详" in source:
        return 0.0
    base = 0.7
    for names, weight in SOURCE_TIERS:
        if any(name in source for name in names):
            base = weight
            break
    reference = discovery_history_reference(source)
    if reference:
        base *= float(reference.get("weight_multiplier") or 1.0)
    return base


def discovery_history_reference(value: str) -> dict[str, Any] | None:
    try:
        from .poll_accuracy import history_reference

        return history_reference(value)
    except (ImportError, OSError, ValueError, TypeError):
        return None


def discovery_priority(item: dict[str, Any]) -> tuple[int, int, str]:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("source", "title", "pollster")
    )
    reference = discovery_history_reference(text) or {}
    return (
        int(reference.get("priority") or 0),
        int(item.get("priority") or 0),
        str(item.get("date") or ""),
    )


def record_weight(record: dict[str, Any], today: dt.date, *, half_life: float = 45, cutoff: int = 60) -> float:
    date = record.get("fieldwork_end") or record.get("fieldwork_start") or record.get("article_date") or ""
    try:
        age = (today - dt.date.fromisoformat(date[:10])).days
    except (ValueError, TypeError):
        return 0.0
    if age < 0 or age > cutoff:
        return 0.0
    decay = 0.5 ** (age / half_life)
    sample = int(record.get("sample_size") or 0)
    sample_w = min(1.0, math.sqrt(sample / 1000)) if sample else 0.8
    type_w = 1.0 if len(record.get("shares") or {}) >= 2 else 0.5
    evidence_w = float(record.get("evidence_quality") or 1.0)
    source = " ".join(
        value for value in (
            record.get("pollster"), record.get("executor"), record.get("sponsor")
        ) if value
    )
    return decay * sample_w * type_w * evidence_w * source_weight(source)


def aggregate(records: Iterable[dict[str, Any]], *, today: dt.date | None = None) -> list[dict[str, Any]]:
    today = today or dt.date.today()
    buckets: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for record in records:
        shares = record.get("shares") or {}
        if len(shares) < 2:
            continue
        weight = record_weight(record, today)
        if weight <= 0:
            continue
        key = (record.get("region") or "", tuple(sorted(shares)))
        bucket = buckets.setdefault(
            key,
            {
                "num": defaultdict(float),
                "w": 0.0,
                "polls": set(),
                "latest": "",
                "records": [],
                "samples": [],
            },
        )
        for name, value in shares.items():
            bucket["num"][name] += weight * value
        bucket["w"] += weight
        bucket["polls"].add((record.get("pollster"), record.get("fieldwork_end") or record.get("article_date")))
        bucket["latest"] = max(bucket["latest"], record.get("fieldwork_end") or record.get("article_date") or "")
        bucket["records"].append(record)
        if record.get("sample_size"):
            bucket["samples"].append(int(record["sample_size"]))

    out = []
    for (region, pair), bucket in buckets.items():
        if bucket["w"] <= 0:
            continue
        raw = {name: round(value / bucket["w"], 2) for name, value in bucket["num"].items()}
        total = sum(raw.values()) or 1.0
        out.append({
            "region": region,
            "pair": list(pair),
            "raw": raw,
            "normalized": {name: round(value / total * 100, 1) for name, value in raw.items()},
            "polls": len(bucket["polls"]),
            "latest": bucket["latest"],
            "sample_size": sum(bucket["samples"]) or None,
            "sources": sorted({record.get("pollster") or "" for record in bucket["records"] if record.get("pollster")}),
        })
    out.sort(key=lambda item: (item["region"], -item["polls"]))
    return out


# ── 主流程 ────────────────────────────────────────────────────────────────

def _region_coverage(
    regions: Iterable[str],
    records: Iterable[dict[str, Any]],
    *,
    today: dt.date,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record.get("region") or ""].append(record)
    out: dict[str, dict[str, Any]] = {}
    for region in regions:
        items = grouped.get(region) or []
        dated: list[tuple[int, dict[str, Any]]] = []
        for record in items:
            value = (
                record.get("fieldwork_end")
                or record.get("fieldwork_start")
                or record.get("article_date")
                or ""
            )
            try:
                dated.append(((today - dt.date.fromisoformat(value[:10])).days, record))
            except (ValueError, TypeError):
                continue
        dated.sort(key=lambda item: item[0])
        latest = dated[0] if dated else None
        if latest is None:
            status = "missing"
            age = None
        else:
            age, record = latest
            if age <= 14:
                status = "fresh"
            elif age <= 30:
                status = "recent"
            elif age <= 60:
                status = "aging"
            else:
                status = "stale"
        out[region] = {
            "status": status,
            "polls": len(items),
            "age_days": age,
            "latest": latest[1].get("fieldwork_end") or latest[1].get("article_date") if latest else "",
            "pollster": latest[1].get("pollster") or "" if latest else "",
        }
    return out


def run(*, days: int = 45, limit: int = 70, resolve: int = 0, verbose: bool = True) -> dict[str, Any]:
    ensure_dirs()
    universe = load_universe()
    regions = list(universe)
    query_groups = candidate_query_groups(universe, per_region=2)
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=max(1, days))

    discovered = discover_formosa()
    discovered += discover_tvbs()
    discovered += discover_kamalan()
    from_news = discover_from_news_db(universe, days=days)
    discovered += from_news
    discovered += discover_cna_rss()
    # RSS 原文通常已足够；只有新闻库没有可用候选人民调时才逐县补查中央社。
    if len(from_news) < 4:
        discovered += discover_candidate_cna(query_groups)
    direct = [item for item in discovered if item.get("direct")]
    unique_items: dict[str, dict[str, Any]] = {}
    for item in direct:
        if item.get("date"):
            try:
                if dt.date.fromisoformat(item["date"][:10]) < cutoff:
                    continue
            except ValueError:
                pass
        unique_items.setdefault(item["url"], item)
    direct = list(unique_items.values())
    direct.sort(key=discovery_priority, reverse=True)
    if verbose:
        print(f"[polldirect] 定向发现 {len(direct)} 条（{len(query_groups)} 组县市候选人查询）")

    records: list[dict[str, Any]] = []
    carried_forward = 0
    previous_path = CONFIG_DIR / "polls_direct.json"
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            for old in previous.get("polls") or []:
                date_value = (
                    old.get("fieldwork_end")
                    or old.get("fieldwork_start")
                    or old.get("article_date")
                    or ""
                )
                try:
                    if date_value and dt.date.fromisoformat(date_value[:10]) < cutoff:
                        continue
                except ValueError:
                    continue
                old_errors = validate(old, today=today)
                if old_errors:
                    corrected = _reparse_cached_record(old, regions, universe)
                    if corrected is None:
                        continue
                    old = corrected
                records.append(old)
                carried_forward += 1
        except (OSError, json.JSONDecodeError):
            pass
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    fetched = 0
    extracted_new = 0
    cached_pages = 0
    direct_items = direct[:limit]

    def fetch_only(item: dict[str, Any]):
        try:
            source_url = resolve_google_news_url(item["url"])
            _, raw, from_cache = fetch_article(source_url)
            return source_url, raw, from_cache, ""
        except Exception as exc:  # noqa: BLE001
            return item["url"], b"", False, str(exc)

    prefetched: dict[str, tuple[str, bytes, bool, str]] = {}
    pool = ThreadPoolExecutor(max_workers=6)
    futures = {
        pool.submit(fetch_only, item): item
        for item in direct_items
    }
    try:
        for future in as_completed(futures, timeout=90):
            source_url, raw, from_cache, error = future.result()
            original = futures[future]
            prefetched[original["url"]] = (source_url, raw, from_cache, error)
    except FutureTimeout:
        pass
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)

    for item in direct_items:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        source_url, raw, from_cache, fetch_error = prefetched.get(
            item["url"], (item["url"], b"", False, "超过抓取总时限")
        )
        if fetch_error:
            continue
        fetched += 1
        cached_pages += int(from_cache)
        source_html = _decode(raw)
        title, text = page_text(source_html)
        published = page_date(source_html, text) or item.get("date") or ""
        region = (
            guess_region(title, regions)
            or guess_region_by_candidates(title, universe)
            or guess_region(item["title"] + "\n" + text, regions)
        )
        if not region:
            rejected.append({"title": title, "url": item["url"], "reason": "无法识别县市"})
            continue
        record = extract(
            text,
            title or item["title"],
            region,
            universe.get(region) or [],
            article_date=published,
        )
        if not record:
            rejected.append({"title": title, "url": item["url"], "reason": "未抽到两位候选人的支持度"})
            continue
        record["url"] = source_url
        record["source"] = item.get("source") or "direct"
        record["article_date"] = published
        record["history_reference"] = discovery_history_reference(
            " ".join(
                str(value or "")
                for value in (
                    record.get("pollster"),
                    record.get("executor"),
                    record.get("sponsor"),
                    record.get("source"),
                    title,
                )
            )
        )
        effective_date = record.get("fieldwork_end") or published
        if effective_date:
            try:
                if dt.date.fromisoformat(effective_date[:10]) < cutoff:
                    rejected.append(
                        {"title": title, "url": item["url"], "reason": f"早于 {cutoff.isoformat()}"}
                    )
                    continue
            except ValueError:
                pass
        errors = validate(record)
        record["errors"] = errors
        warnings: list[str] = []
        if from_cache:
            warnings.append("网络请求失败，使用最近抓取的原文缓存")
        if not record.get("fieldwork_end"):
            warnings.append("未识别调查日期，暂使用文章日期")
        if not record.get("sample_size"):
            warnings.append("未识别样本数")
        if not record.get("pollster"):
            warnings.append("未识别委托/发布机构，不参与加权")
        has_dates = bool(record.get("fieldwork_start") and record.get("fieldwork_end"))
        has_sample = bool(record.get("sample_size"))
        record["report_type"] = (
            "full" if has_dates and has_sample
            else "partial" if has_dates or has_sample
            else "media-cite"
        )
        record["evidence_quality"] = (
            1.0 if record["report_type"] == "full"
            else 0.75 if record["report_type"] == "partial"
            else 0.4
        )
        record["quality_warnings"] = warnings
        if errors:
            if verbose:
                print(f"  [skip] {title[:40]} -> {errors[0]}")
            rejected.append({"title": title, "url": item["url"], "reason": "；".join(errors)})
            continue
        records.append(record)
        extracted_new += 1
        if verbose:
            print(f"  [ok] {region} {record['pollster'] or record['source']} "
                  f"{record['shares']} {record['fieldwork_end'] or '日期未知'}")

    # 对过去45天已缓存的原文重新解析，修复旧版本解析器留下的错误记录。
    recovered = 0
    cache_root = DATA_DIR / "cache" / "polls"
    for url_file in cache_root.glob("*.url"):
        try:
            if dt.datetime.fromtimestamp(url_file.stat().st_mtime).date() < cutoff:
                continue
            url = url_file.read_text(encoding="utf-8").strip()
            if not url or url in seen or "news.google.com/" in url:
                continue
            html_file = url_file.with_suffix(".html")
            title, text = page_text(_decode(html_file.read_bytes()))
            if not any(word in f"{title}\n{text[:1200]}" for word in POLL_WORDS):
                continue
            region = (
                guess_region(title, regions)
                or guess_region_by_candidates(title, universe)
                or guess_region(text[:1600], regions)
            )
            if not region:
                continue
            article_date = page_date(html_file.read_text(encoding="utf-8", errors="ignore"), text)
            if not article_date:
                article_date = dt.datetime.fromtimestamp(
                    html_file.stat().st_mtime
                ).date().isoformat()
            record = extract(
                text,
                title,
                region,
                universe.get(region) or [],
                article_date=article_date,
            )
            if not record or validate(record):
                continue
            record["url"] = url
            record["source"] = record.get("pollster") or "缓存原文"
            record["errors"] = []
            has_dates = bool(record.get("fieldwork_start") and record.get("fieldwork_end"))
            has_sample = bool(record.get("sample_size"))
            record["report_type"] = (
                "full" if has_dates and has_sample
                else "partial" if has_dates or has_sample
                else "media-cite"
            )
            record["evidence_quality"] = (
                1.0 if record["report_type"] == "full"
                else 0.75 if record["report_type"] == "partial"
                else 0.4
            )
            record["quality_warnings"] = []
            record["syndicated"] = [
                {
                    "url": url,
                    "source": record["source"],
                    "title": title,
                }
            ]
            records.append(record)
            seen.add(url)
            recovered += 1
        except (OSError, ValueError, TypeError):
            continue

    unique = dedupe(records)
    summary = aggregate(unique)
    coverage = _region_coverage(regions, unique, today=today)
    source_counts = Counter(record.get("pollster") or record.get("source") or "不详" for record in unique)
    report_counts = Counter(record.get("report_type") or "unknown" for record in unique)
    status_counts = Counter(item["status"] for item in coverage.values())

    payload = {
        "meta": {
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "days": days,
            "search_mode": "county_top_candidates",
            "query_groups": len(query_groups),
            "discovered": len(discovered),
            "eligible": len(direct),
            "fetched": fetched,
            "cached_pages": cached_pages,
            "extracted": extracted_new,
            "recovered_from_cache": recovered,
            "candidates_before_dedupe": len(records),
            "carried_forward": carried_forward,
            "unique": len(unique),
            "regions": len({r.get("region") for r in unique}),
            "regions_with_weighted_data": len(summary),
            "coverage": dict(sorted(status_counts.items())),
            "sources": dict(source_counts.most_common()),
            "report_types": dict(report_counts.most_common()),
            "weighting": {
                "half_life_days": 45,
                "cutoff_days": 60,
                "note": "机构权重 × 样本量权重 × 45天半衰期；调查结束超过60天不参与加权",
            },
        },
        "polls": unique,
        "summary": summary,
        "coverage": coverage,
    }
    (CONFIG_DIR / "polls_direct.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (EXPORT_DIR / "poll_support.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (EXPORT_DIR / "poll_coverage.json").write_text(
        json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (EXPORT_DIR / "polls_rejected.json").write_text(
        json.dumps(rejected[:100], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload
