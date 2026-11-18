"""PTT 選情輿情：看板貼文、候選人提及與標題語氣。

本模組只使用 PTT 公開網頁，不快取帳號或 IP，也不把論壇聲量直接塞進
勝率公式。輸出定位是「近期討論熱度與議題證據」，供使用者點回原文核驗。
"""

from __future__ import annotations

import html
import json
import math
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from html.parser import HTMLParser
from typing import Any

from .attention import PTT_UA, ptt_board_recent, ptt_search
from .http import fetch_text
from .lexicon import score_text, topics as extract_topics
from .paths import CONFIG_DIR, DATA_DIR
from .roster import load_roster

LOCAL = timezone(timedelta(hours=8))
SCHEMA_VERSION = 1
ALLOWED_DAYS = {1, 3, 7, 15, 30}
CACHE_SECONDS = 600
_LOCK = threading.Lock()
_MEMORY: dict[int, dict[str, Any]] = {}
_INVALID_CANDIDATE_NAME = re.compile(
    r"^(?:男|女|男性|女性|性別|性别|候選人|候选人|姓名|備註|备注)$"
)


def _valid_candidate_name(name: str) -> bool:
    value = str(name or "").strip()
    return len(value) >= 2 and not _INVALID_CANDIDATE_NAME.fullmatch(value)

_ELECTION_TERMS = (
    "九合一",
    "縣市長",
    "縣長",
    "市长选举",
    "市長選舉",
    "縣長選舉",
    "選舉",
    "民調",
    "造勢",
    "競選",
    "候選人",
    "提名",
    "初選",
    "政見",
    "投票",
    "輔選",
    "市長",
    "議員",
    "鄉長",
    "鎮長",
    "里長",
    "選區",
    "競總",
    "掃街",
    "拜票",
)
_BOLD_NEGATIVE = (
    "爭議",
    "質疑",
    "批評",
    "砲轟",
    "翻車",
    "造假",
    "涉貪",
    "起訴",
    "告發",
    "罷免",
    "退選",
)
_KNOWN_ALIASES = {
    "沈伯洋": ("Puma", "Puma Shen", "黑熊"),
    "蔣萬安": ("萬安", "安寶", "蔣寶寶"),
    "蘇巧慧": ("巧慧",),
    "李四川": ("四川", "川伯"),
    "何欣純": ("欣純",),
    "鄭朝方": ("朝方",),
    "徐欣瑩": ("欣瑩",),
    "林國漳": ("國漳",),
    "吳宗憲": ("宗憲",),
    "陳亭妃": ("亭妃",),
    "謝龍介": ("龍介",),
    "柯志恩": ("志恩",),
    "賴瑞隆": ("瑞隆",),
}
_GENERIC_CANDIDATE_PATTERNS = (
    re.compile(
        r"(?:市長|縣長|議員|鄉長|鎮長|里長)候選人[\s：:・]*(?P<name>[\u4e00-\u9fff]{2,4})"
    ),
    re.compile(r"(?P<name>[\u4e00-\u9fff]{2,4})[\s：:・]*(?:競選總部|宣布參選|投入選戰)"),
)
_GENERIC_NAME_BLOCK = {
    "號次", "号次", "名單", "名单", "登記", "登记", "資格", "资格",
    "政見", "政见", "辯論", "辩论", "活動", "活动", "結果", "结果",
    "呼吁", "各界", "共同", "聲援", "声援",
}


class _ArticleTextParser(HTMLParser):
    """擷取 PTT 文章 main-container 的正文文字。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_main = False
        self.depth = 0
        self.skip_depth: int | None = None
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in ("script", "style"):
            self.skip_depth = self.depth + 1
            return
        if not self.in_main:
            if values.get("id") == "main-container":
                self.in_main = True
                self.depth = 1
            return
        if tag in ("div", "p"):
            self.depth += 1
            self.parts.append("\n")
        elif tag == "br":
            self.parts.append("\n")
        classes = values.get("class") or ""
        if "article-metaline" in classes:
            self.skip_depth = self.depth

    def handle_endtag(self, tag: str) -> None:
        if not self.in_main:
            return
        if tag in ("div", "p"):
            if self.skip_depth is not None and self.depth <= self.skip_depth:
                self.skip_depth = None
            self.depth -= 1
            if self.depth <= 0:
                self.in_main = False
                self.depth = 0

    def handle_data(self, data: str) -> None:
        if self.in_main and self.skip_depth is None and data.strip():
            self.parts.append(data)


def _cache_path(days: int):
    target = DATA_DIR / "cache"
    target.mkdir(parents=True, exist_ok=True)
    return target / f"ptt-opinion-{days}d.json"


def _read_cache(days: int) -> dict[str, Any] | None:
    try:
        payload = json.loads(_cache_path(days).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(days: int, payload: dict[str, Any]) -> None:
    try:
        _cache_path(days).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _fresh(payload: dict[str, Any], seconds: int = CACHE_SECONDS) -> bool:
    try:
        stamp = datetime.fromisoformat(str(payload.get("fetched_at_iso") or ""))
        return (datetime.now(timezone.utc) - stamp).total_seconds() < seconds
    except (TypeError, ValueError):
        return False


def _has_content(payload: dict[str, Any] | None) -> bool:
    return bool(
        payload
        and (
            (payload.get("counts") or {}).get("posts")
            or payload.get("posts")
        )
    )


def _latest_nonempty_cache(requested_days: int) -> dict[str, Any] | None:
    """只返回同一时间窗口的旧缓存，禁止用30天数据冒充24小时数据。"""
    payload = _read_cache(requested_days)
    if not _has_content(payload):
        return None
    stale = dict(payload)
    stale["stale"] = True
    stale["requested_window_days"] = requested_days
    stale["fallback_window_days"] = requested_days
    stale["warning"] = (
        f"PTT 当前无有效新数据，显示该时间窗口上次有效缓存"
        f"（近 {requested_days} 天）"
    )
    return stale


def _parse_listing_date(value: str, now: date) -> date | None:
    match = re.search(r"(\d{1,2})/(\d{1,2})", value or "")
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    result = date(now.year, month, day)
    if result > now + timedelta(days=7):
        result = result.replace(year=now.year - 1)
    return result


def _load_config(name: str) -> dict[str, Any]:
    try:
        payload = json.loads((CONFIG_DIR / name).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@lru_cache(maxsize=1)
def _candidate_registry() -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """正式縣市長、重點議員與鄉鎮市長名冊，加上可用別名。"""
    records: dict[tuple[Any, ...], dict[str, Any]] = {}
    alias_ids: dict[str, set[tuple[Any, ...]]] = {}

    def add(
        name: str,
        region: str,
        party: str,
        *,
        number: Any = None,
        office: str = "",
        district: str = "",
        english: str = "",
        aliases: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        name = name.strip()
        if not _valid_candidate_name(name):
            return {}
        key = (region, name, office, district)
        item = records.setdefault(
            key,
            {
                "name": name,
                "region": region,
                "party": party,
                "number": number,
                "office": office,
                "district": district,
            },
        )
        alias_values = {name, *aliases}
        if english:
            alias_values.add(english)
            first = english.split()[0]
            if len(first) >= 4:
                alias_values.add(first)
        if len(name) >= 3:
            alias_values.add(name[1:])
        for alias in alias_values:
            alias = alias.strip()
            if alias:
                alias_ids.setdefault(alias, set()).add(key)
        return item

    roster = _load_config("roster.json").get("regions") or {}
    for region, entries in roster.items():
        for entry in entries or []:
            name = str(entry.get("name") or "")
            add(
                name,
                region,
                str(entry.get("party") or ""),
                number=entry.get("number"),
                office="縣市長" if region.endswith("縣") else "直轄市長",
                english=str(entry.get("en") or ""),
                aliases=_KNOWN_ALIASES.get(name, ()),
            )

    council = (_load_config("council_2026.json").get("regions") or {})
    for region, info in council.items():
        for district in (info or {}).get("districts") or []:
            for entry in district.get("candidates") or []:
                add(
                    str(entry.get("name") or ""),
                    region,
                    str(entry.get("party") or ""),
                    office="縣市議員",
                    district=str(district.get("name") or ""),
                )

    local = (_load_config("local_2026.json").get("regions") or {})
    for region, info in local.items():
        for town in (info or {}).get("towns") or []:
            for entry in town.get("candidates") or []:
                add(
                    str(entry.get("name") or ""),
                    region,
                    str(entry.get("party") or ""),
                    office="區長" if region == "新北市" else "鄉鎮市長",
                    district=str(town.get("name") or ""),
                )

    name_map = _load_config("name_map.json").get("politicians") or {}
    for english, zh_name in name_map.items():
        for item in records.values():
            if item["name"] == zh_name:
                alias_ids.setdefault(str(english), set()).add(
                    (item["region"], item["name"], item["office"], item["district"])
                )
                first = str(english).split()[0]
                if len(first) >= 4:
                    alias_ids.setdefault(first, set()).add(
                        (item["region"], item["name"], item["office"], item["district"])
                    )

    alias_candidates: dict[str, list[dict[str, Any]]] = {}
    for alias, keys in alias_ids.items():
        alias_candidates[alias] = [records[key] for key in keys if key in records]
    return list(records.values()), alias_candidates


def _match_candidates(title: str, aliases: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    found: dict[tuple[str, str, str], dict[str, Any]] = {}
    title_lower = title.lower()
    strong_context = _election_related(title, [])
    for alias in sorted(aliases, key=len, reverse=True):
        matched = alias in title
        if not matched and re.search(r"[A-Za-z]", alias):
            matched = alias.lower() in title_lower
        if not matched:
            continue
        if len(alias) < 3 and not re.search(r"[A-Za-z]", alias) and not strong_context:
            continue
        for item in aliases[alias]:
            found[(item["region"], item["name"], item["office"])] = item
    return list(found.values())


def _generic_candidate_names(title: str) -> list[str]:
    found: list[str] = []
    for pattern in _GENERIC_CANDIDATE_PATTERNS:
        for match in pattern.finditer(title):
            name = match.group("name").strip()
            if any(block in name for block in _GENERIC_NAME_BLOCK):
                continue
            if name and name not in found:
                found.append(name)
    return found


def _article_snippet(url: str, title: str) -> str:
    try:
        page = fetch_text(url, timeout=12, retries=1, headers=PTT_UA)
    except Exception:  # noqa: BLE001 - 代表帖正文失敗時仍保留標題
        return ""
    parser = _ArticleTextParser()
    try:
        parser.feed(page)
    except Exception:  # noqa: BLE001
        return ""
    text = re.sub(r"\s+", " ", html.unescape("".join(parser.parts))).strip()
    title_hits = [match.end() for match in re.finditer(re.escape(title), text[:1400])]
    if title_hits:
        text = text[title_hits[-1]:].strip()
    text = re.sub(
        r"^(?:[A-Z][a-z]{2}\s+){2}\d{1,2}\s+\d{1,2}:\d{2}:\d{2}\s+\d{4}\s*",
        "",
        text,
    )
    text = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[IP已隐藏]", text)
    for marker in ("※ 發信站", "※ 文章網址", "◆ From", "-- "):
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return text[:260]


def _election_related(title: str, candidates: list[dict[str, Any]]) -> bool:
    if candidates:
        return True
    text = title or ""
    return any(term in text for term in _ELECTION_TERMS)


def _post_topics(title: str) -> list[str]:
    rows = [row["topic"] for row in extract_topics(title, limit=3)]
    rules = (
        ("民調選情", ("民調", "民调", "支持度", "看好度")),
        ("提名初選", ("初選", "初选", "提名", "徵召", "征召")),
        ("競選造勢", ("造勢", "造势", "站台", "掃街", "扫街", "晚會", "晚会")),
        ("政黨整合", ("藍白", "蓝白", "合作", "整合", "結盟", "结盟")),
        ("爭議攻防", ("爭議", "争议", "批評", "批评", "質疑", "质疑", "砲轟", "炮轰")),
        ("司法廉能", ("司法", "起訴", "起诉", "涉貪", "涉贪", "罷免", "罢免")),
        ("政策民生", ("政策", "交通", "房價", "房价", "薪資", "薪资", "托育", "長照", "长照")),
    )
    for topic, keys in rules:
        if any(key in title for key in keys) and topic not in rows:
            rows.append(topic)
    if not rows and any(term in title for term in _ELECTION_TERMS):
        rows.append("選舉攻防")
    return rows[:4]


def _name_sentiment(title: str, name: str, candidate_count: int) -> float:
    """候選人專屬的標題方向；多人同題時避免把同一句情緒複製給所有人。"""
    index = title.find(name)
    if index < 0:
        return 0.0
    if candidate_count == 1:
        return score_text(title)[0]
    window = title[max(0, index - 24): min(len(title), index + len(name) + 24)]
    score = score_text(window)[0]
    if any(term in window for term in _BOLD_NEGATIVE):
        score -= 0.35
    lead_pattern = re.compile(
        re.escape(name) + r".{0,8}(領先|超車|勝過|贏過|勝出)"
    )
    trail_pattern = re.compile(
        re.escape(name) + r".{0,8}(落後|輸給|落敗|敬陪末座)"
    )
    if lead_pattern.search(title):
        score += 0.45
    if trail_pattern.search(title):
        score -= 0.45
    return max(-1.0, min(1.0, score))


def _collect(days: int) -> dict[str, Any]:
    now = datetime.now(LOCAL)
    today = now.date()
    cutoff = today - timedelta(days=days - 1)
    roster = load_roster().get("regions") or {}
    _candidates, candidate_aliases = _candidate_registry()
    roster_regions = sorted(roster, key=len, reverse=True)

    raw_posts: list[dict[str, Any]] = []
    board_pages = {1: 2, 3: 3, 7: 5, 15: 7, 30: 9}[days]
    gossip_pages = {1: 2, 3: 3, 7: 4, 15: 5, 30: 6}[days]
    search_pages = 1
    search_plan = [
        ("Gossiping", "九合一"),
        ("Gossiping", "市長選舉"),
        ("Gossiping", "縣市長"),
        ("Gossiping", "議員"),
        ("HatePolitics", "選舉"),
        ("HatePolitics", "議員"),
    ]
    def fetch_board(board: str, pages: int) -> list[dict[str, Any]]:
        return [
            {**item, "board": board}
            for item in ptt_board_recent(board=board, pages=pages, delay=0.18)
        ]

    def fetch_search(board: str, query: str) -> list[dict[str, Any]]:
        return [
            {**item, "board": board}
            for item in ptt_search(query, board=board, pages=search_pages, delay=0.18)
        ]

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(fetch_board, "HatePolitics", board_pages),
            pool.submit(fetch_board, "Gossiping", gossip_pages),
            *[
                pool.submit(fetch_search, board, query)
                for board, query in search_plan
            ],
        ]
        for future in futures:
            try:
                raw_posts.extend(future.result())
            except Exception:  # noqa: BLE001 - 单一路抓取失败不阻断其他来源
                continue

    seen: set[str] = set()
    posts: list[dict[str, Any]] = []
    rejected_old = 0
    for raw in raw_posts:
        url = str(raw.get("url") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not url or not title or url in seen:
            continue
        published = _parse_listing_date(str(raw.get("date") or ""), today)
        if published is None:
            continue
        if published < cutoff:
            rejected_old += 1
            continue
        matched = _match_candidates(title, candidate_aliases)
        known_names = {item["name"] for item in matched}
        for name in _generic_candidate_names(title):
            if name in known_names:
                continue
            region = next(
                (
                    value for value in roster_regions
                    if value in title or value.replace("臺", "台") in title
                ),
                "",
            )
            matched.append({
                "name": name,
                "region": region,
                "party": "",
                "number": None,
                "office": "其他候選人",
                "district": "",
            })
            known_names.add(name)
        if not _election_related(title, matched):
            continue
        seen.add(url)
        generic_score = score_text(title)[0]
        candidate_scores = [
            {
                **candidate,
                "sentiment": round(
                    _name_sentiment(title, candidate["name"], len(matched)),
                    3,
                ),
            }
            for candidate in matched
        ]
        post_topics = _post_topics(title)
        posts.append({
            "url": url,
            "board": raw.get("board") or "HatePolitics",
            "title": title,
            "date": published.isoformat(),
            "nrec": raw.get("nrec") or "",
            "score": int(raw.get("score") or 0),
            "sentiment": round(generic_score, 3),
            "candidates": candidate_scores,
            "topics": post_topics,
        })

    posts.sort(
        key=lambda item: (
            item["date"],
            abs(int(item.get("score") or 0)),
            item["title"],
        ),
        reverse=True,
    )

    snippet_posts = posts[:9]
    with ThreadPoolExecutor(max_workers=6) as pool:
        snippets = list(pool.map(
            lambda post: _article_snippet(post["url"], post["title"]),
            snippet_posts,
        ))
    for post, snippet in zip(snippet_posts, snippets):
        post["snippet"] = snippet

    stats: dict[str, dict[str, Any]] = {}
    for post in posts:
        for candidate in post.get("candidates") or []:
            name = candidate["name"]
            row = stats.setdefault(
                name,
                {
                    **candidate,
                    "posts": 0,
                    "positive_posts": 0,
                    "negative_posts": 0,
                    "neutral_posts": 0,
                    "controversial_posts": 0,
                    "_weighted_sentiment": 0.0,
                    "_sentiment_weight": 0.0,
                },
            )
            row["posts"] += 1
            sentiment = float(candidate.get("sentiment") or 0)
            weight = 1.0 + math.log1p(abs(int(post.get("score") or 0)))
            row["_weighted_sentiment"] += sentiment * weight
            row["_sentiment_weight"] += weight
            if sentiment > 0.12:
                row["positive_posts"] += 1
            elif sentiment < -0.12:
                row["negative_posts"] += 1
            else:
                row["neutral_posts"] += 1
            if abs(sentiment) >= 0.35 or (
                len(post.get("candidates") or []) > 1
                and abs(float(post.get("sentiment") or 0)) < 0.12
            ):
                row["controversial_posts"] += 1

    candidate_rows = list(stats.values())
    if candidate_rows:
        max_posts = max(row["posts"] for row in candidate_rows)
        for row in candidate_rows:
            row["tone"] = round(
                row["_weighted_sentiment"] / row["_sentiment_weight"],
                3,
            ) if row["_sentiment_weight"] else 0.0
            row["heat"] = round(100 * row["posts"] / max_posts, 1)
            representative = next(
                (
                    post
                    for post in posts
                    if row["name"] in {c["name"] for c in post.get("candidates") or []}
                ),
                None,
            )
            row["representative"] = representative
            row.pop("_weighted_sentiment", None)
            row.pop("_sentiment_weight", None)
    candidate_rows.sort(
        key=lambda row: (
            -int(row.get("posts") or 0),
            -int(row.get("positive_posts") or 0),
            row.get("name") or "",
        )
    )

    topic_counts: Counter[str] = Counter()
    board_counts: Counter[str] = Counter()
    for post in posts:
        board_counts[post["board"]] += 1
        topic_counts.update(post.get("topics") or [])
    topics = [
        {"topic": topic, "count": count}
        for topic, count in topic_counts.most_common(14)
    ]
    positive = sum(1 for post in posts if float(post.get("sentiment") or 0) > 0.12)
    negative = sum(1 for post in posts if float(post.get("sentiment") or 0) < -0.12)
    controversial = sum(
        1
        for post in posts
        if abs(float(post.get("sentiment") or 0)) >= 0.35
        or (
            post.get("candidates")
            and len({c.get("name") for c in post["candidates"]}) > 1
            and abs(float(post.get("sentiment") or 0)) < 0.12
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "source": "PTT",
        "window_days": days,
        "window_label": "近 24 小时（按日期）" if days == 1 else f"近 {days} 天",
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "boards": ["HatePolitics", "Gossiping"],
        "counts": {
            "posts": len(posts),
            "candidate_posts": sum(1 for post in posts if post.get("candidates")),
            "candidates": len(candidate_rows),
            "positive_posts": positive,
            "negative_posts": negative,
            "controversial_posts": controversial,
            "rejected_old": rejected_old,
            "board_counts": dict(board_counts),
        },
        "candidates": candidate_rows[:40],
        "topics": topics,
        "posts": posts[:120],
        "warnings": [
            "PTT 用户结构不具全岛代表性；讨论热度不等同于民调支持率。",
            "标题语气仅提供议题线索，正式判断请点击原文查看上下文。",
            "PTT 列表只提供日期；24 小时窗口按当天日期近似，不能精确到小时。",
        ],
    }


def get_opinion(days: int = 1, *, force: bool = False) -> dict[str, Any]:
    days = days if days in ALLOWED_DAYS else 1
    if not force:
        memory = _MEMORY.get(days)
        if _has_content(memory) and _fresh(memory):
            return memory
        disk = _read_cache(days)
        if _has_content(disk) and _fresh(disk):
            _MEMORY[days] = disk
            return disk

    if not force:
        stale = _latest_nonempty_cache(days)
        if stale:
            stale["warning"] += "；已优先显示最近有效缓存，点击“刷新 PTT”可重新抓取"
            return stale
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "source": "PTT",
            "window_days": days,
            "error": "暂无可用缓存，请点击“刷新 PTT”抓取",
            "counts": {
                "posts": 0, "candidate_posts": 0, "candidates": 0,
                "positive_posts": 0, "negative_posts": 0,
                "controversial_posts": 0, "board_counts": {},
            },
            "candidates": [], "topics": [], "posts": [],
            "warnings": ["PTT 默认读取缓存，暂无可用记录，请点击“刷新 PTT”。"],
        }

    if not _LOCK.acquire(blocking=False):
        stale = _latest_nonempty_cache(days)
        if stale:
            stale["warning"] += "；后台正在更新其他时间窗口，先显示最近有效缓存"
            return stale
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "source": "PTT",
            "window_days": days,
            "error": "PTT 正在后台更新，请稍后重试",
            "counts": {
                "posts": 0, "candidate_posts": 0, "candidates": 0,
                "positive_posts": 0, "negative_posts": 0,
                "controversial_posts": 0, "board_counts": {},
            },
            "candidates": [], "topics": [], "posts": [],
            "warnings": ["PTT 正在后台更新，请稍后重试。"],
        }
    try:
        memory = _MEMORY.get(days)
        if _has_content(memory) and _fresh(memory) and not force:
            return memory
        try:
            payload = _collect(days)
            if _has_content(payload):
                _MEMORY[days] = payload
                _write_cache(days, payload)
                return payload
            stale = _latest_nonempty_cache(days)
            if stale:
                return stale
            _write_cache(days, payload)
            return payload
        except Exception as exc:  # noqa: BLE001 - 上游失敗時回退舊快取
            stale = _latest_nonempty_cache(days)
            if stale:
                stale["warning"] += f"；刷新异常：{type(exc).__name__}"
                return stale
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "source": "PTT",
                "window_days": days,
                "error": f"{type(exc).__name__}: {exc}",
                "counts": {
                    "posts": 0,
                    "candidate_posts": 0,
                    "candidates": 0,
                    "positive_posts": 0,
                    "negative_posts": 0,
                    "controversial_posts": 0,
                    "board_counts": {},
                },
                "candidates": [],
                "topics": [],
                "posts": [],
                "warnings": ["PTT 当前无法读取，请稍后重试。"],
            }
    finally:
        _LOCK.release()
