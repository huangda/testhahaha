"""YouTube Data API v3 官方公开视频、统计与评论读取。"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

BASE_URL = "https://www.googleapis.com/youtube/v3"
_STATUS_LOCK = threading.Lock()
_STATUS: dict[str, Any] = {"checked_at": 0.0, "ok": False, "error": ""}


class YouTubeOfficialError(RuntimeError):
    """已在信息中移除 API Key 的官方接口错误。"""


def _api_key() -> str:
    from .config import load_settings

    return str(load_settings().youtube_api_key or "").strip()


def configured() -> bool:
    return bool(_api_key())


def _get(resource: str, params: dict[str, Any], *, timeout: int = 20) -> dict[str, Any]:
    key = _api_key()
    if not key:
        raise YouTubeOfficialError("尚未配置 YOUTUBE_API_KEY")
    query = urllib.parse.urlencode(
        {name: value for name, value in {**params, "key": key}.items() if value is not None}
    )
    url = f"{BASE_URL}/{resource}?{query}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TEM-YouTube-Data-API/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        reason = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            error = payload.get("error") or {}
            errors = error.get("errors") or []
            reason = str(
                (errors[0].get("reason") if errors else "")
                or error.get("status")
                or ""
            )
            message = str(error.get("message") or "")
        except Exception:  # noqa: BLE001
            message = ""
        detail = "，".join(value for value in (reason, message) if value)
        raise YouTubeOfficialError(
            f"YouTube官方API HTTP {exc.code}" + (f"：{detail}" if detail else "")
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise YouTubeOfficialError(f"YouTube官方API请求失败：{type(exc).__name__}") from exc
    try:
        value = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise YouTubeOfficialError("YouTube官方API返回了无效JSON") from exc
    if not isinstance(value, dict):
        raise YouTubeOfficialError("YouTube官方API返回格式异常")
    return value


def status(*, force: bool = False) -> dict[str, Any]:
    key = _api_key()
    if not key:
        return {"configured": False, "ok": False, "error": "尚未配置 YOUTUBE_API_KEY"}
    with _STATUS_LOCK:
        if not force and time.time() - float(_STATUS.get("checked_at") or 0) < 600:
            return {"configured": True, **_STATUS}
        try:
            _get(
                "videos",
                {
                    "part": "id",
                    "chart": "mostPopular",
                    "regionCode": "TW",
                    "maxResults": 1,
                },
                timeout=15,
            )
            _STATUS.update({"checked_at": time.time(), "ok": True, "error": ""})
        except YouTubeOfficialError as exc:
            _STATUS.update({"checked_at": time.time(), "ok": False, "error": str(exc)})
        return {"configured": True, **_STATUS}


def _published_after(days: int) -> str:
    value = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _search_item(item: dict[str, Any]) -> dict[str, Any] | None:
    identity = item.get("id") or {}
    snippet = item.get("snippet") or {}
    video_id = str(identity.get("videoId") or "")
    title = str(snippet.get("title") or "").strip()
    if not video_id or not title:
        return None
    try:
        published = int(
            datetime.fromisoformat(
                str(snippet.get("publishedAt") or "").replace("Z", "+00:00")
            ).timestamp()
        )
    except (TypeError, ValueError):
        published = 0
    return {
        "videoId": video_id,
        "title": title,
        "author": str(snippet.get("channelTitle") or ""),
        "authorId": str(snippet.get("channelId") or ""),
        "published": published,
        "publishedText": str(snippet.get("publishedAt") or ""),
        "viewCount": 0,
        "likeCount": 0,
        "commentCount": 0,
        "lengthSeconds": 0,
        "description": str(snippet.get("description") or ""),
        "_official": True,
    }


def search_videos(query: str, days: int, *, max_results: int = 25) -> list[dict[str, Any]]:
    data = _get(
        "search",
        {
            "part": "snippet",
            "q": query,
            "type": "video",
            "order": "viewCount",
            "publishedAfter": _published_after(days),
            "regionCode": "TW",
            "relevanceLanguage": "zh-Hant",
            "maxResults": min(50, max(1, max_results)),
        },
    )
    return [
        converted
        for item in data.get("items") or []
        if isinstance(item, dict) and (converted := _search_item(item))
    ]


def _parse_duration(value: str) -> int:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?",
        str(value or ""),
    )
    if not match:
        return 0
    parts = {name: int(number or 0) for name, number in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def hydrate_videos(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not items:
        return items
    by_id = {str(item.get("videoId") or ""): item for item in items}
    ids = [video_id for video_id in by_id if video_id]
    for offset in range(0, len(ids), 50):
        chunk = ids[offset : offset + 50]
        data = _get(
            "videos",
            {
                "part": "snippet,contentDetails,statistics",
                "id": ",".join(chunk),
                "maxResults": 50,
            },
        )
        for row in data.get("items") or []:
            if not isinstance(row, dict):
                continue
            video_id = str(row.get("id") or "")
            item = by_id.get(video_id)
            if not item:
                continue
            snippet = row.get("snippet") or {}
            statistics = row.get("statistics") or {}
            content = row.get("contentDetails") or {}
            item.update(
                {
                    "title": str(snippet.get("title") or item.get("title") or ""),
                    "author": str(
                        snippet.get("channelTitle") or item.get("author") or ""
                    ),
                    "authorId": str(
                        snippet.get("channelId") or item.get("authorId") or ""
                    ),
                    "description": str(
                        snippet.get("description") or item.get("description") or ""
                    ),
                    "viewCount": int(statistics.get("viewCount") or 0),
                    "likeCount": int(statistics.get("likeCount") or 0),
                    "commentCount": int(statistics.get("commentCount") or 0),
                    "lengthSeconds": _parse_duration(str(content.get("duration") or "")),
                }
            )
    return list(by_id.values())


def video_comments(
    video_id: str,
    days: int,
    *,
    per_video: int = 50,
) -> tuple[dict[str, Any] | None, list[str]]:
    """按单个视频即时读取热门评论，供前端点击视频时使用。"""
    video_id = str(video_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id):
        raise YouTubeOfficialError("视频ID格式无效")
    data = _get(
        "videos",
        {
            "part": "snippet,contentDetails,statistics",
            "id": video_id,
            "maxResults": 1,
        },
    )
    items = data.get("items") or []
    if not items or not isinstance(items[0], dict):
        raise YouTubeOfficialError("没有找到该视频")
    row = items[0]
    snippet = row.get("snippet") or {}
    statistics = row.get("statistics") or {}
    published_at = str(snippet.get("publishedAt") or "")
    try:
        published_ts = int(
            datetime.fromisoformat(published_at.replace("Z", "+00:00")).timestamp()
        )
    except (TypeError, ValueError):
        published_ts = 0
    video = {
        "id": video_id,
        "video_id": video_id,
        "title": str(snippet.get("title") or ""),
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "channel": str(snippet.get("channelTitle") or ""),
        "published_at": published_at,
        "published_ts": published_ts,
        "views": int(statistics.get("viewCount") or 0),
        "likes": int(statistics.get("likeCount") or 0),
        "comment_count": int(statistics.get("commentCount") or 0),
    }
    groups, warnings = fetch_comments(
        [video],
        time.time() - max(1, int(days)) * 86400,
        max_videos=1,
        per_video=per_video,
    )
    return (groups[0] if groups else None), warnings


def channel_videos(channels: dict[str, str], days: int, *, per_channel: int = 10) -> list[dict[str, Any]]:
    if not channels:
        return []
    data = _get(
        "channels",
        {
            "part": "snippet,contentDetails",
            "id": ",".join(list(channels.values())[:50]),
            "maxResults": 50,
        },
    )
    uploads: list[tuple[str, str]] = []
    names_by_id = {channel_id: name for name, channel_id in channels.items()}
    for row in data.get("items") or []:
        if not isinstance(row, dict):
            continue
        channel_id = str(row.get("id") or "")
        playlist_id = str(
            ((row.get("contentDetails") or {}).get("relatedPlaylists") or {}).get(
                "uploads"
            )
            or ""
        )
        if channel_id and playlist_id:
            uploads.append((names_by_id.get(channel_id, ""), playlist_id))

    def fetch_playlist(pair: tuple[str, str]) -> list[dict[str, Any]]:
        channel_name, playlist_id = pair
        playlist = _get(
            "playlistItems",
            {
                "part": "snippet,contentDetails",
                "playlistId": playlist_id,
                "maxResults": max(1, min(50, per_channel)),
            },
        )
        out: list[dict[str, Any]] = []
        for row in playlist.get("items") or []:
            if not isinstance(row, dict):
                continue
            snippet = row.get("snippet") or {}
            content = row.get("contentDetails") or {}
            video_id = str(content.get("videoId") or "")
            title = str(snippet.get("title") or "")
            if not video_id or not title:
                continue
            try:
                published = int(
                    datetime.fromisoformat(
                        str(content.get("videoPublishedAt") or "").replace("Z", "+00:00")
                    ).timestamp()
                )
            except (TypeError, ValueError):
                published = 0
            if published and published < int(time.time()) - days * 86400:
                continue
            out.append(
                {
                    "videoId": video_id,
                    "title": title,
                    "author": channel_name
                    or str(snippet.get("channelTitle") or ""),
                    "authorId": str(snippet.get("channelId") or ""),
                    "published": published,
                    "publishedText": str(content.get("videoPublishedAt") or ""),
                    "viewCount": 0,
                    "likeCount": 0,
                    "commentCount": 0,
                    "lengthSeconds": 0,
                    "description": str(snippet.get("description") or ""),
                    "_official": True,
                }
            )
        return out

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(fetch_playlist, pair) for pair in uploads]
        try:
            for future in as_completed(futures, timeout=60):
                rows.extend(future.result())
        except TimeoutError:
            pass
    return rows


def fetch_comments(
    videos: list[dict[str, Any]],
    cutoff_ts: float,
    *,
    max_videos: int = 8,
    per_video: int = 20,
) -> tuple[list[dict[str, Any]], list[str]]:
    def heat(video: dict[str, Any]) -> float:
        return (
            math.log1p(max(0, int(video.get("views") or 0)))
            + 0.35
            * math.log1p(max(0, int(video.get("likes") or 0)))
            + 0.55
            * math.log1p(
                max(
                    0,
                    int(video.get("comment_count") or video.get("commentCount") or 0),
                )
            )
        )

    candidates = sorted(
        [
            video
            for video in videos
            if int(video.get("comment_count") or video.get("commentCount") or 0) > 0
        ],
        key=heat,
        reverse=True,
    )[:max_videos]
    warnings: list[str] = []

    def fetch_one(video: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        video_id = str(video.get("id") or "")
        try:
            data = _get(
                "commentThreads",
                {
                    "part": "snippet,replies",
                    "videoId": video_id,
                    "order": "relevance",
                    "maxResults": max(1, min(100, per_video)),
                    "textFormat": "plainText",
                },
            )
        except YouTubeOfficialError as exc:
            return None, str(exc)
        comments: list[dict[str, Any]] = []
        replies_count = 0
        for thread in data.get("items") or []:
            if not isinstance(thread, dict):
                continue
            snippet = (thread.get("snippet") or {}).get("topLevelComment") or {}
            comment_snippet = snippet.get("snippet") or {}
            published_at = str(comment_snippet.get("publishedAt") or "")
            try:
                published_ts = datetime.fromisoformat(
                    published_at.replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                published_ts = 0
            if published_ts and published_ts < cutoff_ts:
                continue
            replies: list[dict[str, Any]] = []
            for reply in ((thread.get("replies") or {}).get("comments") or []):
                if not isinstance(reply, dict):
                    continue
                reply_snippet = reply.get("snippet") or {}
                reply_time = str(reply_snippet.get("publishedAt") or "")
                try:
                    reply_ts = datetime.fromisoformat(
                        reply_time.replace("Z", "+00:00")
                    ).timestamp()
                except (TypeError, ValueError):
                    reply_ts = 0
                if reply_ts and reply_ts < cutoff_ts:
                    continue
                replies.append(
                    {
                        "id": str(reply.get("id") or ""),
                        "author": str(reply_snippet.get("authorDisplayName") or ""),
                        "text": str(
                            reply_snippet.get("textOriginal")
                            or reply_snippet.get("textDisplay")
                            or ""
                        ).strip(),
                        "published_at": reply_time,
                        "likes": int(reply_snippet.get("likeCount") or 0),
                    }
                )
            comments.append(
                {
                    "id": str(snippet.get("id") or ""),
                    "author": str(comment_snippet.get("authorDisplayName") or ""),
                    "text": str(
                        comment_snippet.get("textOriginal")
                        or comment_snippet.get("textDisplay")
                        or ""
                    ).strip(),
                    "published_at": published_at,
                    "likes": int(comment_snippet.get("likeCount") or 0),
                    "reply_count": int(
                        (thread.get("snippet") or {}).get("totalReplyCount") or 0
                    ),
                    "replies": replies,
                }
            )
            replies_count += len(replies)
        comments.sort(key=lambda item: (-int(item.get("likes") or 0), item.get("published_at") or ""), reverse=False)
        return (
            {
                "video_id": video_id,
                "title": video.get("title") or "",
                "url": video.get("url") or f"https://www.youtube.com/watch?v={video_id}",
                "channel": video.get("channel") or "",
                "published_at": video.get("published_at") or "",
                "comment_count": int(
                    video.get("comment_count") or video.get("commentCount") or 0
                ),
                "sampled_comments": len(comments),
                "sampled_replies": replies_count,
                "comments": comments,
            },
            "",
        )

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(fetch_one, video) for video in candidates]
        try:
            for future in as_completed(futures, timeout=90):
                try:
                    row, warning = future.result()
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"评论读取失败：{type(exc).__name__}")
                    continue
                if row:
                    rows.append(row)
                if warning:
                    warnings.append(warning)
        except TimeoutError:
            warnings.append("部分视频评论读取超过总时限")
    rows.sort(
        key=lambda item: (
            int(item.get("comment_count") or 0),
            int(item.get("sampled_comments") or 0),
        ),
        reverse=True,
    )
    return rows, list(dict.fromkeys(warnings))[:4]
