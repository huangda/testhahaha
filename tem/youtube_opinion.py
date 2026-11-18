"""YouTube 選舉輿情：以公開搜尋結果分析影片標題、頻道與播放表現。"""

from __future__ import annotations

import json
import math
import threading
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any

from .dedupe import bigrams, jaccard, normalize_title
from .http import encode_query, fetch_json
from .lexicon import score_text
from .paths import DATA_DIR
from .ptt_opinion import (
    _candidate_registry,
    _match_candidates,
    _name_sentiment,
    _post_topics,
)
from . import youtube_official

SCHEMA_VERSION = 2
ALLOWED_DAYS = {1, 3, 7, 15, 30}
CACHE_SECONDS = 600
API_BASES = (
    "https://yt.chocolatemoo53.com/api/v1",
    "https://yewtu.be/api/v1",
)
PIPED_API_BASES = (
    "https://pipedapi.ducks.party",
    "https://pipedapi.astartes.nl",
)
QUERIES = (
    "2026 九合一選舉",
    "縣市長選舉",
    "市長選舉",
    "縣市議員選舉",
    "台灣 選舉 民調",
    "新北市長選舉",
    "宜蘭縣長選舉",
)
CHANNELS = {
    "公視新聞": "UCexpzYDEnfmAvPSfG4xbcjA",
    "台視新聞": "UC8ROUUjHzEQm-ndb69CX8Ww",
    "華視新聞": "UCDCJyLpbfgeVE9iZiEam-Kg",
    "TVBS NEWS": "UC5nwNW4KdC0SzrhF9BXEYOQ",
    "三立新聞網": "UCIU8ha-NHmLjtUwU7dFiXUA",
    "東森新聞": "UCR3asjvr_WAaxwJYEDV_Bfw",
    "中央社影音": "UC7ymKGCl6EVLh7z6sq7TBZA",
    "中天電視": "UC5l1Yto5oOIgRXlI4p4VKbw",
    "民視新聞": "UC2VmWn8dAqkzlQqvy02E1PA",
    "鏡新聞": "UC4LjkybVKXCDlneVXlKAbmw",
    "udn video": "UC2M61YK4ntt9iK-23XhDwcw",
}
CANDIDATE_LIMIT = 12
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
_LOCK = threading.Lock()
_MEMORY: dict[int, dict[str, Any]] = {}


def _cache_path(days: int):
    target = DATA_DIR / "cache"
    target.mkdir(parents=True, exist_ok=True)
    return target / f"youtube-opinion-{days}d.json"


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
        fetched = datetime.fromisoformat(str(payload.get("fetched_at_iso") or ""))
        return (datetime.now(timezone.utc) - fetched).total_seconds() < seconds
    except (TypeError, ValueError):
        return False


def _source_label() -> str:
    return (
        "YouTube Data API v3（官方）"
        if youtube_official.configured()
        else "YouTube 公开搜索（Invidious 镜像）"
    )


def _has_content(payload: dict[str, Any] | None) -> bool:
    return bool(
        payload
        and int(payload.get("schema_version") or 0) == SCHEMA_VERSION
        and (
            (payload.get("counts") or {}).get("videos")
            or payload.get("videos")
        )
    )


def _latest_nonempty_cache(requested_days: int) -> dict[str, Any] | None:
    payload = _read_cache(requested_days)
    if not _has_content(payload):
        return None
    stale = dict(payload)
    stale["stale"] = True
    stale["requested_window_days"] = requested_days
    stale["fallback_window_days"] = requested_days
    stale["warning"] = (
        f"YouTube 当前无有效新数据，显示近 {requested_days} 天窗口自己的"
        "最近一次有效旧缓存"
    )
    return stale


def _duration_label(seconds: int) -> str:
    if seconds <= 0:
        return ""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _piped_item(item: dict[str, Any]) -> dict[str, Any] | None:
    url = str(item.get("url") or "")
    parsed = urllib.parse.urlparse(url)
    video_id = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    title = str(item.get("title") or "").strip()
    if not video_id or not title:
        return None
    uploader_url = str(item.get("uploaderUrl") or "")
    channel_id = uploader_url.rstrip("/").rsplit("/", 1)[-1] if "/channel/" in uploader_url else ""
    try:
        published = int(item.get("uploaded") or 0)
    except (TypeError, ValueError):
        published = 0
    # Piped 的 uploaded 是毫秒时间戳；Invidious 通常是秒。
    if published > 10_000_000_000:
        published //= 1000
    try:
        duration = max(0, int(item.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    try:
        views = max(0, int(item.get("views") or 0))
    except (TypeError, ValueError):
        views = 0
    return {
        "videoId": video_id,
        "title": title,
        "author": str(item.get("uploaderName") or ""),
        "authorId": channel_id,
        "published": published,
        "publishedText": str(item.get("uploadedDate") or ""),
        "viewCount": views,
        "lengthSeconds": duration,
        "description": str(item.get("shortDescription") or ""),
    }


def _fetch_piped_query(query: str) -> list[dict[str, Any]]:
    params = encode_query({"q": query, "filter": "videos"})
    for base in PIPED_API_BASES:
        try:
            data = fetch_json(
                f"{base}/search?{params}",
                timeout=8,
                retries=1,
                headers=UA,
            )
            rows = [
                converted
                for item in (data.get("items") or [])
                if isinstance(item, dict)
                and (converted := _piped_item(item))
            ]
            if rows:
                return rows
        except Exception:  # noqa: BLE001
            continue
    return []


def _fetch_piped_channel(name: str, channel_id: str) -> list[dict[str, Any]]:
    for base in PIPED_API_BASES:
        try:
            data = fetch_json(
                f"{base}/channel/{urllib.parse.quote(channel_id)}",
                timeout=8,
                retries=1,
                headers=UA,
            )
            rows = [
                converted
                for item in (data.get("relatedStreams") or [])
                if isinstance(item, dict)
                and (converted := _piped_item(item))
            ]
            if rows:
                return rows
        except Exception:  # noqa: BLE001
            continue
    return []


def _fetch_query(
    query: str,
    page: int = 1,
    *,
    days: int = 1,
    official: bool = False,
) -> tuple[list[dict[str, Any]], str | None]:
    official_warning = ""
    if page == 1 and official:
        try:
            rows = youtube_official.search_videos(query, days)
            return rows, None
        except youtube_official.YouTubeOfficialError as exc:
            official_warning = f"{query}：{exc}，已改用公开镜像"
    params = encode_query({
        "q": query,
        "type": "video",
        "sort_by": "upload_date",
        "page": page,
    })
    if page == 1:
        piped = _fetch_piped_query(query)
        if piped:
            return piped, None
    for base in API_BASES:
        try:
            data = fetch_json(
                f"{base}/search?{params}",
                timeout=8,
                retries=1,
                headers=UA,
            )
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)], official_warning or None
        except Exception:  # noqa: BLE001 - 多個公開鏡像依序嘗試
            continue
    return [], official_warning or f"{query}：公开镜像暂不可用"


def _priority_candidate_names() -> list[str]:
    candidates, _ = _candidate_registry()
    by_name = {item["name"]: item for item in candidates}
    ordered: list[str] = []
    try:
        from . import db

        rows = sorted(
            db.fetch_latest_odds(),
            key=lambda row: -(
                float(row["volume"] or 0) * max(0.01, float(row["yes_price"] or 0))
            ),
        )
        for row in rows:
            name = str(row["candidate_zh"] or "")
            if name in by_name and name not in ordered:
                ordered.append(name)
    except Exception:  # noqa: BLE001 - 市場資料不可用時改用名冊順序
        pass
    for item in candidates:
        if item.get("region") in ("新北市", "宜蘭縣") and item["name"] not in ordered:
            ordered.append(item["name"])
    if len(ordered) < CANDIDATE_LIMIT:
        for item in candidates:
            if item["name"] not in ordered:
                ordered.append(item["name"])
            if len(ordered) >= CANDIDATE_LIMIT:
                break
    return ordered[:CANDIDATE_LIMIT]


def _fetch_channel(name: str, channel_id: str) -> tuple[list[dict[str, Any]], str | None]:
    piped = _fetch_piped_channel(name, channel_id)
    if piped:
        return piped, None
    params = encode_query({"page": 1, "sort_by": "newest"})
    endpoint = f"/channels/{channel_id}/videos?{params}"
    for base in API_BASES:
        try:
            data = fetch_json(
                f"{base}{endpoint}",
                timeout=8,
                retries=1,
                headers=UA,
            )
            if isinstance(data, dict):
                videos = [
                    {**item, "author": item.get("author") or name, "_channel_name": name}
                    for item in data.get("videos") or []
                    if isinstance(item, dict)
                ]
                return videos, None
        except Exception:  # noqa: BLE001
            continue
    return [], f"{name}频道：公开镜像暂不可用"


def _merge_similar_videos(videos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合併不同頻道對同一事件的中文/英文近似標題。"""
    clusters: list[dict[str, Any]] = []
    for video in sorted(videos, key=lambda item: (-item["published_ts"], -item["views"])):
        normalized = normalize_title(video["title"])
        grams = bigrams(normalized)
        candidates = {item["name"] for item in video.get("candidates") or []}
        match = None
        for cluster in clusters:
            if abs(video["published_ts"] - cluster["latest_ts"]) > 3 * 86400:
                continue
            similarity = jaccard(grams, cluster["grams"])
            overlap = candidates & cluster["candidate_names"]
            if similarity >= 0.46 and (overlap or not candidates or not cluster["candidate_names"]):
                match = cluster
                break
        if match:
            match["videos"].append(video)
            match["latest_ts"] = max(match["latest_ts"], video["published_ts"])
            match["candidate_names"] |= candidates
        else:
            clusters.append({
                "grams": grams,
                "latest_ts": video["published_ts"],
                "candidate_names": candidates,
                "videos": [video],
            })

    merged: list[dict[str, Any]] = []
    for cluster in clusters:
        group = cluster["videos"]
        representative = max(group, key=lambda item: (item["views"], item["published_ts"])).copy()
        total_views = sum(item["views"] for item in group)
        total_likes = sum(int(item.get("likes") or 0) for item in group)
        total_comments = sum(int(item.get("comment_count") or 0) for item in group)
        weight_total = sum(1 + math.log1p(max(0, item["views"])) for item in group)
        sentiment = (
            sum(
                item["sentiment"] * (1 + math.log1p(max(0, item["views"])))
                for item in group
            ) / weight_total
            if weight_total else 0.0
        )
        candidate_map: dict[tuple[str, str, str], dict[str, Any]] = {}
        topics: list[str] = []
        for item in group:
            for candidate in item.get("candidates") or []:
                key = (candidate["region"], candidate["name"], candidate["office"])
                candidate_map.setdefault(key, candidate)
            for topic in item.get("topics") or []:
                if topic not in topics:
                    topics.append(topic)
        representative.update({
            "views": total_views,
            "likes": total_likes,
            "comment_count": total_comments,
            "published_ts": max(item["published_ts"] for item in group),
            "published_at": max(
                (item["published_at"] for item in group),
                key=lambda value: value or "",
            ),
            "published_text": max(
                group, key=lambda item: item["published_ts"]
            )["published_text"],
            "sentiment": round(sentiment, 3),
            "candidates": list(candidate_map.values()),
            "topics": topics[:5],
            "channels": list(dict.fromkeys(item["channel"] for item in group if item["channel"])),
            "sources": [
                {
                    "channel": item["channel"],
                    "url": item["url"],
                    "views": item["views"],
                    "published_at": item["published_at"],
                }
                for item in group
            ],
            "merged_count": len(group),
        })
        merged.append(representative)
    return merged


def _collect(days: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff = time.time() - days * 86400
    warnings: list[str] = []
    official_status = youtube_official.status()
    official_mode = bool(official_status.get("ok"))
    if official_status.get("configured") and not official_mode:
        warnings.append(
            f"YouTube官方API暂不可用，已使用公开镜像：{official_status.get('error') or '未知错误'}"
        )
    base_pages = 1
    candidate_pages = 1
    candidate_names = _priority_candidate_names()
    search_tasks = [
        (query, page)
        for query in QUERIES
        for page in range(1, base_pages + 1)
    ] + [
        (f"{name} 選舉", page)
        for name in candidate_names
        for page in range(1, candidate_pages + 1)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        search_futures = [
            pool.submit(
                _fetch_query,
                query,
                page,
                days=days,
                official=official_mode,
            )
            for query, page in search_tasks
        ]
        results = [future.result() for future in search_futures]
    raw: list[dict[str, Any]] = []
    for items, warning in results:
        raw.extend(items)
        if warning:
            warnings.append(warning)
    if official_mode:
        try:
            raw.extend(
                youtube_official.channel_videos(CHANNELS, days, per_channel=10)
            )
        except youtube_official.YouTubeOfficialError as exc:
            warnings.append(f"官方API频道读取失败：{exc}")
    else:
        with ThreadPoolExecutor(max_workers=8) as pool:
            channel_futures = [
                pool.submit(_fetch_channel, name, channel_id)
                for name, channel_id in CHANNELS.items()
            ]
            channel_results = [future.result() for future in channel_futures]
        for items, warning in channel_results:
            raw.extend(items)
            if warning:
                warnings.append(warning)

    if official_mode and raw:
        try:
            raw = youtube_official.hydrate_videos(raw)
        except youtube_official.YouTubeOfficialError as exc:
            warnings.append(f"官方API视频统计读取失败：{exc}")

    _candidates, aliases = _candidate_registry()
    seen: set[str] = set()
    videos: list[dict[str, Any]] = []
    for item in raw:
        video_id = str(item.get("videoId") or "").strip()
        title = str(item.get("title") or "").strip()
        published = item.get("published")
        if not video_id or not title or video_id in seen:
            continue
        try:
            published_ts = int(published or 0)
        except (TypeError, ValueError):
            published_ts = 0
        if published_ts < cutoff:
            continue
        matched = _match_candidates(title, aliases)
        if not matched and not _post_topics(title):
            election_like = any(
                term in title
                for term in (
                    "選舉", "选举", "九合一", "市長", "市长", "縣長", "县长",
                    "議員", "议员", "民調", "民调", "候選人", "候选人",
                )
            )
            if not election_like:
                continue
        seen.add(video_id)
        candidate_rows = [
            {
                **candidate,
                "sentiment": round(
                    _name_sentiment(title, candidate["name"], len(matched)),
                    3,
                ),
            }
            for candidate in matched
        ]
        sentiment = round(score_text(title)[0], 3)
        views = max(0, int(item.get("viewCount") or 0))
        likes = max(0, int(item.get("likeCount") or 0))
        comment_count = max(0, int(item.get("commentCount") or 0))
        seconds = max(0, int(item.get("lengthSeconds") or 0))
        published_dt = datetime.fromtimestamp(published_ts, tz=timezone.utc)
        videos.append({
            "id": video_id,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={video_id}",
            "channel": str(item.get("author") or ""),
            "channel_id": str(item.get("authorId") or ""),
            "channel_url": (
                f"https://www.youtube.com/channel/{item.get('authorId')}"
                if item.get("authorId") else ""
            ),
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            "published_ts": published_ts,
            "published_at": published_dt.isoformat(timespec="seconds"),
            "published_text": str(item.get("publishedText") or ""),
            "views": views,
            "likes": likes,
            "comment_count": comment_count,
            "duration_seconds": seconds,
            "duration": _duration_label(seconds),
            "sentiment": sentiment,
            "candidates": candidate_rows,
            "topics": _post_topics(title),
        })

    raw_video_count = len(videos)
    videos = _merge_similar_videos(videos)
    videos.sort(key=lambda video: (-video["published_ts"], -video["views"]))
    comment_videos: list[dict[str, Any]] = []
    comment_warnings: list[str] = []
    if official_mode and videos:
        try:
            comment_videos, comment_warnings = youtube_official.fetch_comments(
                [video for video in videos if video.get("candidates")],
                cutoff,
                max_videos=30,
                per_video=30,
            )
        except Exception as exc:  # noqa: BLE001
            comment_warnings.append(f"官方API评论读取失败：{type(exc).__name__}")
    comment_by_video = {
        str(row.get("video_id") or ""): row for row in comment_videos
    }
    for video in videos:
        sample = comment_by_video.get(video["id"])
        video["sampled_comments"] = int((sample or {}).get("sampled_comments") or 0)
        video["sampled_replies"] = int((sample or {}).get("sampled_replies") or 0)
    stats: dict[tuple[str, str, str], dict[str, Any]] = {}
    channel_counts: Counter[str] = Counter()
    topic_counts: Counter[str] = Counter()
    for video in videos:
        for source in video.get("sources") or []:
            if source.get("channel"):
                channel_counts[source["channel"]] += 1
        topic_counts.update(video.get("topics") or [])
        for candidate in video.get("candidates") or []:
            key = (candidate["region"], candidate["name"], candidate["office"])
            row = stats.setdefault(
                key,
                {
                    **candidate,
                    "videos": 0,
                    "views": 0,
                    "comments": 0,
                    "positive_videos": 0,
                    "negative_videos": 0,
                    "neutral_videos": 0,
                    "_sentiment": 0.0,
                    "_weight": 0.0,
                },
            )
            row["videos"] += 1
            row["views"] += video["views"]
            row["comments"] += int(video.get("comment_count") or 0)
            sentiment = float(candidate.get("sentiment") or 0)
            weight = 1.0 + math.log1p(max(0, video["views"]))
            row["_sentiment"] += sentiment * weight
            row["_weight"] += weight
            if sentiment > 0.12:
                row["positive_videos"] += 1
            elif sentiment < -0.12:
                row["negative_videos"] += 1
            else:
                row["neutral_videos"] += 1

    candidate_rows = list(stats.values())
    if candidate_rows:
        max_videos = max(row["videos"] for row in candidate_rows)
        max_views = max(row["views"] for row in candidate_rows) or 1
        for row in candidate_rows:
            row["tone"] = round(row["_sentiment"] / row["_weight"], 3) if row["_weight"] else 0.0
            row["heat"] = round(
                100 * (
                    0.6 * row["videos"] / max_videos
                    + 0.4 * math.sqrt(row["views"] / max_views)
                ),
                1,
            )
            row.pop("_sentiment", None)
            row.pop("_weight", None)
    candidate_rows.sort(key=lambda row: (-row["videos"], -row["views"]))

    positive = sum(1 for video in videos if video["sentiment"] > 0.12)
    negative = sum(1 for video in videos if video["sentiment"] < -0.12)
    sampled_comments = sum(
        int(row.get("sampled_comments") or 0) for row in comment_videos
    )
    sampled_replies = sum(
        int(row.get("sampled_replies") or 0) for row in comment_videos
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "source": "YouTube",
        "source_label": (
            "YouTube Data API v3（官方）" if official_mode else _source_label()
        ),
        "api_status": official_status,
        "window_days": days,
        "window_label": "近 24 小时" if days == 1 else f"近 {days} 天",
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "videos": len(videos),
            "raw_videos": raw_video_count,
            "merged_duplicates": max(0, raw_video_count - len(videos)),
            "channels": len(channel_counts),
            "candidate_videos": sum(1 for video in videos if video.get("candidates")),
            "candidates": len(candidate_rows),
            "positive_videos": positive,
            "negative_videos": negative,
            "views": sum(video["views"] for video in videos),
            "comment_videos": len(comment_videos),
            "comments": sampled_comments,
            "comment_replies": sampled_replies,
        },
        "candidates": candidate_rows[:40],
        "topics": [
            {"topic": topic, "count": count}
            for topic, count in topic_counts.most_common(12)
        ],
        "channels": [
            {"name": name, "count": count}
            for name, count in channel_counts.most_common(12)
        ],
        "videos": videos[:120],
        "comment_videos": comment_videos,
        "warnings": [
            *(
                [
                    "YouTube视频统计与最多30个热门视频的评论通过Data API v3官方接口读取；"
                    "每个视频按热度读取最多30条一级评论及接口返回的回复。"
                ]
                if official_mode
                else [
                    "YouTube官方API不可用，当前使用公开镜像读取视频；该模式不读取评论。"
                ]
            ),
            "视频标题语气和播放量不等于选民支持率，不进入胜率公式。",
            "评论只代表公开评论区样本，不代表全部观众或当地选民。",
            *comment_warnings,
            *warnings,
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
            stale["warning"] += "；已优先显示最近有效缓存，点击“刷新 YouTube”可重新抓取"
            return stale
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "source": "YouTube",
            "source_label": _source_label(),
            "window_days": days,
            "error": "暂无可用缓存，请点击“刷新 YouTube”抓取",
            "counts": {
                "videos": 0, "raw_videos": 0, "merged_duplicates": 0,
                "channels": 0, "candidate_videos": 0, "candidates": 0,
                "positive_videos": 0, "negative_videos": 0, "views": 0,
                "comment_videos": 0, "comments": 0, "comment_replies": 0,
            },
            "candidates": [], "topics": [], "channels": [], "videos": [],
            "warnings": ["YouTube 默认读取缓存，暂无可用记录，请点击“刷新 YouTube”。"],
        }

    if not _LOCK.acquire(blocking=False):
        stale = _latest_nonempty_cache(days)
        if stale:
            stale["warning"] += "；后台正在更新其他时间窗口，先显示最近有效缓存"
            return stale
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "source": "YouTube",
            "source_label": _source_label(),
            "window_days": days,
            "error": "YouTube 正在后台更新，请稍后重试",
            "counts": {
                "videos": 0, "raw_videos": 0, "merged_duplicates": 0,
                "channels": 0, "candidate_videos": 0, "candidates": 0,
                "positive_videos": 0, "negative_videos": 0, "views": 0,
                "comment_videos": 0, "comments": 0, "comment_replies": 0,
            },
            "candidates": [], "topics": [], "channels": [], "videos": [],
            "warnings": ["YouTube 正在后台更新，请稍后重试。"],
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
        except Exception as exc:  # noqa: BLE001
            stale = _latest_nonempty_cache(days)
            if stale:
                stale["warning"] += f"；刷新异常：{type(exc).__name__}"
                return stale
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "source": "YouTube",
                "source_label": _source_label(),
                "window_days": days,
                "error": f"{type(exc).__name__}: {exc}",
                "counts": {
                    "videos": 0, "raw_videos": 0, "merged_duplicates": 0,
                    "channels": 0, "candidate_videos": 0,
                    "candidates": 0, "positive_videos": 0,
                    "negative_videos": 0, "views": 0,
                    "comment_videos": 0, "comments": 0, "comment_replies": 0,
                },
                "candidates": [],
                "topics": [],
                "channels": [],
                "videos": [],
                "warnings": ["YouTube 公开镜像暂时不可用，请稍后刷新。"],
            }
    finally:
        _LOCK.release()
