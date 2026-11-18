"""社交媒体监测：新北、宜兰热门贴文与热门评论。"""

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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

from .paths import CONFIG_DIR, DATA_DIR
from .zhconv import to_simplified
from . import facebook_social

SCHEMA_VERSION = 11
MIN_COMPATIBLE_SCHEMA_VERSION = 3
WINDOW_HOURS = 24
ALLOWED_HOURS = {24, 72, 168, 360, 720}
MAX_WINDOW_HOURS = max(ALLOWED_HOURS)
REFRESH_BUDGET_CNY = 2.0
EXTEND_BUDGET_CNY = 6.0
CNY_PER_USD = 7.2
REFRESH_BUDGET_USD = REFRESH_BUDGET_CNY / CNY_PER_USD
EXTEND_BUDGET_USD = EXTEND_BUDGET_CNY / CNY_PER_USD
CACHE_SECONDS = 1800
TOP_POSTS_PER_REGION = 200
BILIBILI_POSTS_PER_REGION = 500
MAX_POSTS = 400
NORMAL_QUERIES_PER_REGION = 4
QUERY_PAGE_LIMITS = {
    "threads": {"normal": 1, "extend": 1},
    "instagram": {"normal": 2, "extend": 2},
    "x": {"normal": 2, "extend": 4},
    "tiktok": {"normal": 2, "extend": 4},
    "douyin": {"normal": 2, "extend": 2},
    "xiaohongshu": {"normal": 2, "extend": 2},
    "kuaishou": {"normal": 2, "extend": 2},
    "bilibili": {"normal": 2, "extend": 4},
    "facebook": {"normal": 1, "extend": 1},
}
ACCOUNT_PAGE_LIMITS = {
    "threads": {"normal": 1, "extend": 1},
    "instagram": {"normal": 1, "extend": 4},
}
LEDGER_VERSION = 1
COMMENT_BATCH_LIMITS = {
    "threads": 100,
    "instagram": 100,
    "x": 100,
    "tiktok": 100,
    "douyin": 100,
    "xiaohongshu": 100,
    "kuaishou": 100,
    "bilibili": 20,
    "facebook": 0,
}
COMMENT_CALL_MULTIPLIER = {
    "threads": 4,
}
TOP_COMMENTS = 50
BASE_URL = "https://api.tikhub.io"
PLATFORMS = {
    "threads": "Threads",
    "instagram": "Instagram",
    "x": "X",
    "tiktok": "TikTok",
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "kuaishou": "快手",
    "bilibili": "Bilibili",
    "facebook": "Facebook",
}
PRICE_PER_CALL = {
    "threads": {"search": 0.002, "comment": 0.002, "fallback": 0.002},
    "instagram": {"search": 0.008, "comment": 0.008, "fallback": 0.0},
    "x": {"search": 0.001, "comment": 0.001, "fallback": 0.0},
    "tiktok": {"search": 0.001, "comment": 0.001, "fallback": 0.0},
    "douyin": {"search": 0.01, "comment": 0.001, "fallback": 0.0},
    "xiaohongshu": {"search": 0.01, "comment": 0.01, "fallback": 0.0},
    "kuaishou": {"search": 0.01, "comment": 0.001, "fallback": 0.0},
    "bilibili": {"search": 0.0, "comment": 0.0, "fallback": 0.0},
    "facebook": {"search": 0.0, "comment": 0.0, "fallback": 0.0},
}
REGION_QUERIES = {
    "新北市": (
        "新北市 選戰",
        "新北市長",
        "新北 選舉",
        "新北市 議員 選舉",
        "蘇巧慧 李四川 新北市長",
        "新北 候選人",
        "新北 選舉 爭議",
        "新北 選舉 政策",
    ),
    "宜蘭縣": (
        "宜蘭縣 選戰",
        "宜蘭縣長",
        "宜蘭 選舉",
        "宜蘭縣 議員 選舉",
        "林國漳 吳宗憲 宜蘭縣長",
        "宜蘭 候選人",
        "宜蘭 選舉 爭議",
        "宜蘭 選舉 政策",
    ),
}
ENRICHMENT_QUERIES = {
    "新北市": (
        "新北 九合一 2026",
        "新北市 議員 候選人",
        "#新北市長選舉",
        "#台湾选举 新北",
        "#2026九合一 新北",
    ),
    "宜蘭縣": (
        "宜蘭 九合一 2026",
        "宜蘭縣 議員 候選人",
        "#宜蘭縣長選舉",
        "#台湾选举 宜兰",
        "#2026九合一 宜兰",
    ),
}
BILIBILI_REGION_QUERIES = {
    "新北市": (
        "台湾选举 新北",
        "新北 选举",
        "新北市长",
        "新北市 选举",
        "新北 九合一",
        "新北 民调",
        "新北 候选人",
        "新北 议员 选举",
        "苏巧慧 李四川",
        "新北 选举 政策",
        "新北 选举 争议",
        "新北 选举 造势",
    ),
    "宜蘭縣": (
        "台湾选举 宜兰",
        "宜兰 选举",
        "宜兰县长",
        "宜兰县 选举",
        "宜兰 九合一",
        "宜兰 民调",
        "宜兰 候选人",
        "宜兰 议员 选举",
        "林国漳 吴宗宪",
        "宜兰 选举 政策",
        "宜兰 选举 争议",
        "宜兰 选举 造势",
    ),
}
_LOCKS_GUARD = threading.Lock()
_PLATFORM_LOCKS: dict[str, threading.Lock] = {}
_LEDGER_LOCK = threading.RLock()
_BILIBILI_RATE_LOCK = threading.Lock()
_BILIBILI_COOKIE_LOCK = threading.Lock()
_BILIBILI_LAST_REQUEST = 0.0
_BILIBILI_COOKIE = ""
_BILIBILI_COOKIE_AT = 0.0
_MEMORY: dict[str, dict[str, Any]] = {}
_BUDGET_USAGE: dict[str, dict[str, float]] = {}


def _platform_lock(platform: str) -> threading.Lock:
    with _LOCKS_GUARD:
        return _PLATFORM_LOCKS.setdefault(platform, threading.Lock())


class TikHubError(RuntimeError):
    """不包含密钥的 TikHub 请求错误。"""


def _api_key() -> str:
    from .config import load_settings

    return str(load_settings().tikhub_api_key or "").strip()


def _cache_path(platform: str) -> Path:
    target = DATA_DIR / "cache"
    target.mkdir(parents=True, exist_ok=True)
    return target / f"tikhub-social-{platform}-pool.json"


def _read_cache(platform: str) -> dict[str, Any] | None:
    try:
        value = json.loads(_cache_path(platform).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(platform: str, payload: dict[str, Any]) -> None:
    try:
        _cache_path(platform).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _ledger_path() -> Path:
    target = DATA_DIR / "cache"
    target.mkdir(parents=True, exist_ok=True)
    return target / "tikhub-social-budget-ledger.json"


def _read_ledger() -> dict[str, Any]:
    with _LEDGER_LOCK:
        try:
            payload = json.loads(_ledger_path().read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except (OSError, json.JSONDecodeError):
            pass
        return {"schema_version": LEDGER_VERSION, "platforms": {}}


def _write_ledger(payload: dict[str, Any]) -> None:
    with _LEDGER_LOCK:
        try:
            _ledger_path().write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass


def _today_key() -> str:
    return datetime.now().astimezone().date().isoformat()


def _platform_budget_meta(platform: str) -> dict[str, Any]:
    ledger = _read_ledger()
    platforms = ledger.get("platforms")
    if not isinstance(platforms, dict):
        platforms = {}
    item = platforms.get(platform)
    if not isinstance(item, dict):
        item = {}
    days = item.get("days")
    if not isinstance(days, dict):
        days = {}
    today = days.get(_today_key())
    if not isinstance(today, dict):
        today = {}
    return {
        "extend_used": bool(item.get("extend_used")),
        "extend_completed_at": str(item.get("extend_completed_at") or ""),
        "daily_spent_usd": float(today.get("spent_usd") or 0.0),
        "daily_refreshes": int(today.get("refreshes") or 0),
        "daily_extends": int(today.get("extends") or 0),
    }


def _record_budget_usage(platform: str, mode: str, spent_usd: float) -> None:
    with _LEDGER_LOCK:
        ledger = _read_ledger()
        platforms = ledger.setdefault("platforms", {})
        item = platforms.setdefault(platform, {})
        days = item.setdefault("days", {})
        today = days.setdefault(
            _today_key(),
            {"spent_usd": 0.0, "refreshes": 0, "extends": 0},
        )
        today["spent_usd"] = round(
            float(today.get("spent_usd") or 0.0) + max(0.0, spent_usd),
            6,
        )
        if mode in {"refresh", "extend"}:
            today["refreshes"] = int(today.get("refreshes") or 0) + 1
        if mode == "extend":
            today["extends"] = int(today.get("extends") or 0) + 1
            item["extend_used"] = True
            item["extend_completed_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
        item["last_mode"] = mode
        item["last_spent_usd"] = round(max(0.0, spent_usd), 6)
        item["last_timestamp"] = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        _write_ledger(ledger)


def _attach_budget_meta(payload: dict[str, Any], platform: str) -> dict[str, Any]:
    result = dict(payload)
    meta = _platform_budget_meta(platform)
    result.update(
        {
            "extend_used": meta["extend_used"],
            "extend_completed_at": meta["extend_completed_at"],
            "extend_budget_cny": (
                0.0
                 if platform in {"bilibili", "facebook"}
                else EXTEND_BUDGET_CNY
            ),
            "refresh_budget_cny": (
                0.0
                 if platform in {"bilibili", "facebook"}
                else REFRESH_BUDGET_CNY
            ),
            "daily_spent_cny": round(meta["daily_spent_usd"] * CNY_PER_USD, 2),
            "daily_refreshes": meta["daily_refreshes"],
            "daily_extends": meta["daily_extends"],
        }
    )
    return result


def _has_content(payload: dict[str, Any] | None) -> bool:
    if not payload or not payload.get("posts"):
        return False
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError):
        return False
    return MIN_COMPATIBLE_SCHEMA_VERSION <= schema_version <= SCHEMA_VERSION


def _fresh(payload: dict[str, Any] | None) -> bool:
    try:
        fetched = datetime.fromisoformat(str((payload or {}).get("fetched_at_iso") or ""))
        return (datetime.now(timezone.utc) - fetched).total_seconds() < CACHE_SECONDS
    except (TypeError, ValueError):
        return False


def _get(path: str, params: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    key = _api_key()
    if not key:
        raise TikHubError("尚未配置 TIKHUB_API_KEY")
    url = f"{BASE_URL}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "TEM-TikHub-Social/1.0",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        reason = ""
        try:
            body = json.loads(exc.read().decode("utf-8", "replace"))
            reason = str(body.get("message_zh") or body.get("message") or "")
        except Exception:  # noqa: BLE001
            reason = ""
        raise TikHubError(
            f"TikHub HTTP {exc.code}" + (f"：{reason}" if reason else "")
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise TikHubError(f"TikHub请求失败：{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise TikHubError("TikHub返回格式异常")
    code = int(value.get("code") or 0)
    if code and code != 200:
        raise TikHubError(str(value.get("message_zh") or value.get("message") or f"HTTP {code}"))
    data = value.get("data")
    return data if isinstance(data, dict) else value


def _post_json(path: str, payload: dict[str, Any], *, timeout: int = 30) -> dict[str, Any]:
    key = _api_key()
    if not key:
        raise TikHubError("尚未配置 TIKHUB_API_KEY")
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "TEM-TikHub-Social/1.0",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise TikHubError(f"TikHub HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001
        raise TikHubError(f"TikHub请求失败：{type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise TikHubError("TikHub返回格式异常")
    data = value.get("data")
    return data if isinstance(data, dict) else value


def _epoch(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000
        return int(number)
    except (TypeError, ValueError):
        pass
    text = str(value)
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        pass
    try:
        return int(parsedate_to_datetime(text).timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def _int(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text") or value.get("plaintext") or "")
    return ""


def _focus_names() -> dict[str, list[str]]:
    from .ptt_opinion import _candidate_registry

    candidates, _ = _candidate_registry()
    out = {"新北市": [], "宜蘭縣": []}
    for item in candidates:
        region = str(item.get("region") or "")
        name = str(item.get("name") or "")
        if region in out and name and name not in out[region]:
            out[region].append(name)
    return out


def _annotate_candidate_tags(
    row: dict[str, Any],
    names: dict[str, list[str]],
) -> dict[str, Any]:
    """按贴文正文和官方账号所属候选人补充候选人多标签。"""
    tags: list[str] = []
    official_candidate = str(row.get("candidate") or "").strip()
    if official_candidate:
        tags.append(official_candidate)
    text = to_simplified(str(row.get("text") or ""))
    region = str(row.get("region") or "")
    region_names = names.get(region) or [
        name for values in names.values() for name in values
    ]
    for name in region_names:
        plain_name = to_simplified(name)
        if plain_name and plain_name in text and name not in tags:
            tags.append(name)
    row["candidate_tags"] = tags
    row["candidate_tag"] = tags[0] if tags else ""
    return row


def _social_accounts(platform: str, names: dict[str, list[str]]) -> list[dict[str, Any]]:
    try:
        payload = json.loads(
            (CONFIG_DIR / "social_accounts.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return []
    allowed = {name for rows in names.values() for name in rows}
    return [
        dict(item)
        for item in payload.get("accounts") or []
        if item.get("enabled", True)
        and item.get("platform") == platform
        and item.get("candidate") in allowed
    ]


def _account_posts(
    platform: str,
    accounts: list[dict[str, Any]],
    *,
    max_pages: int = 1,
) -> tuple[list[dict[str, Any]], list[str], int]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    calls = 0

    def fetch(account: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        if platform == "threads":
            data = _get(
                "/api/v1/threads/web/fetch_user_posts",
                {"user_id": account["account_id"]},
            )
            items = (((data.get("mediaData") or {}).get("edges")) or [])
            converted: list[dict[str, Any]] = []
            for edge in items:
                node = (edge or {}).get("node") or {}
                thread_items = node.get("thread_items") or []
                if not thread_items:
                    continue
                post = (thread_items[0] or {}).get("post") or {}
                info = post.get("text_post_app_info") or {}
                fragments = ((info.get("text_fragments") or {}).get("fragments") or [])
                text = _text(post.get("caption")) or "\n".join(
                    str(fragment.get("plaintext") or "")
                    for fragment in fragments
                    if isinstance(fragment, dict)
                )
                user = post.get("user") or {}
                code = str(post.get("code") or "")
                username = str(user.get("username") or account.get("username") or "")
                images = ((post.get("image_versions2") or {}).get("candidates") or [])
                converted_row = _post(
                    platform="threads",
                    post_id=str(post.get("pk") or post.get("id") or ""),
                    text=text,
                    url=f"https://www.threads.net/@{username}/post/{code}" if code else "",
                    author=str(user.get("full_name") or username),
                    published_ts=_epoch(post.get("taken_at")),
                    likes=_int(post.get("like_count") or info.get("like_count")),
                    comments=_int(info.get("direct_reply_count")),
                    shares=_int(info.get("repost_count")) + _int(info.get("quote_count")),
                    views=0,
                    image=str(images[0].get("url") or "") if images and isinstance(images[0], dict) else "",
                    raw_ref=str(post.get("pk") or ""),
                )
                if converted_row:
                    converted.append(converted_row)
            return converted, 1
        if platform == "instagram":
            output: list[dict[str, Any]] = []
            cursor = ""
            page_calls = 0
            cutoff = time.time() - MAX_WINDOW_HOURS * 3600
            for _page in range(max(1, max_pages)):
                params: dict[str, Any] = {
                    "username": account["username"],
                    "count": 50,
                    "first": 50,
                }
                if cursor:
                    params["after"] = cursor
                data = _get("/api/v1/instagram/v3/get_user_posts", params)
                page_calls += 1
                page_rows = _instagram_posts(data)
                output.extend(page_rows)
                next_cursor = str(data.get("next_max_id") or "")
                if not next_cursor:
                    page_info = (data.get("data") or {}).get("page_info") or {}
                    next_cursor = str(page_info.get("end_cursor") or "")
                oldest = min(
                    (int(row.get("published_ts") or 0) for row in page_rows),
                    default=0,
                )
                if not next_cursor or next_cursor == cursor or (oldest and oldest < cutoff):
                    break
                cursor = next_cursor
            return output, page_calls
        return [], 0

    for offset in range(0, len(accounts), 2):
        batch = accounts[offset:offset + 2]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {pool.submit(fetch, account): account for account in batch}
            for future in as_completed(futures, timeout=45):
                account = futures[future]
                calls += 1
                try:
                    account_rows, page_calls = future.result()
                except Exception as exc:  # noqa: BLE001
                    warnings.append(
                        f"{account.get('candidate')}官方账号读取失败：{type(exc).__name__}"
                    )
                    continue
                for row in account_rows:
                    row.update(
                        {
                            "region": account.get("region") or row.get("region") or "",
                            "candidate": account.get("candidate") or "",
                            "official_account": True,
                            "account_username": account.get("username") or "",
                            "account_verification": account.get("verification") or "",
                        }
                    )
                    row["age_hours"] = round(
                        max(0, (time.time() - int(row.get("published_ts") or 0)) / 3600),
                        1,
                    )
                    row["heat"] = round(_heat(row, int(time.time()) - MAX_WINDOW_HOURS * 3600), 3)
                rows.extend(account_rows)
                calls += max(0, page_calls - 1)
        time.sleep(0.35)
    return rows, warnings, calls


def _region_for(text: str, names: dict[str, list[str]]) -> str:
    plain = to_simplified(text).replace("臺", "台")
    scores = {}
    for region, candidates in names.items():
        plain_region = to_simplified(region).replace("臺", "台")
        aliases = [plain_region, plain_region.replace("縣", "").replace("县", "")]
        score = sum(3 for alias in aliases if alias and alias in plain)
        score += sum(2 for name in candidates if to_simplified(name) in plain)
        scores[region] = score
    chosen = max(scores, key=scores.get)
    return chosen if scores[chosen] > 0 else ""


def _heat(post: dict[str, Any], cutoff: int) -> float:
    age_hours = max(0.0, (time.time() - int(post.get("published_ts") or 0)) / 3600)
    recency = 24.0 / (24.0 + age_hours)
    return (
        math.log1p(_int(post.get("likes")))
        + 0.62 * math.log1p(_int(post.get("comments")))
        + 0.45 * math.log1p(_int(post.get("shares")))
        + 0.20 * math.log1p(_int(post.get("views")))
        + recency
    )


def _post(
    *,
    platform: str,
    post_id: str,
    text: str,
    url: str,
    author: str,
    published_ts: int,
    likes: int,
    comments: int,
    shares: int,
    views: int,
    image: str = "",
    raw_ref: str = "",
) -> dict[str, Any] | None:
    text = str(text or "").strip()
    if not text or not post_id:
        return None
    return {
        "platform": platform,
        "platform_label": PLATFORMS[platform],
        "id": str(post_id),
        "text": text,
        "url": url,
        "author": str(author or ""),
        "published_ts": int(published_ts or 0),
        "published_at": (
            datetime.fromtimestamp(published_ts, tz=timezone.utc).isoformat(timespec="seconds")
            if published_ts else ""
        ),
        "likes": max(0, int(likes or 0)),
        "comments": max(0, int(comments or 0)),
        "shares": max(0, int(shares or 0)),
        "views": max(0, int(views or 0)),
        "image": image or "",
        "raw_ref": raw_ref or str(post_id),
    }


def _threads_posts(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        node = result.get("node") or {}
        thread = node.get("thread") or {}
        items = thread.get("thread_items") or []
        if not items:
            continue
        post = (items[0] or {}).get("post") or {}
        if not isinstance(post, dict):
            continue
        user = post.get("user") or {}
        info = post.get("text_post_app_info") or {}
        fragments = ((info.get("text_fragments") or {}).get("fragments") or [])
        text = _text(post.get("caption")) or "\n".join(
            str(fragment.get("plaintext") or "") for fragment in fragments if isinstance(fragment, dict)
        )
        code = str(post.get("code") or "")
        pk = str(post.get("pk") or post.get("id") or "")
        username = str(user.get("username") or "")
        image = ""
        versions = ((post.get("image_versions2") or {}).get("candidates") or [])
        if versions and isinstance(versions[0], dict):
            image = str(versions[0].get("url") or "")
        item = _post(
            platform="threads",
            post_id=pk,
            text=text,
            url=f"https://www.threads.net/@{username}/post/{code}" if username and code else "",
            author=str(user.get("full_name") or username),
            published_ts=_epoch(post.get("taken_at")),
            likes=_int(post.get("like_count") or info.get("like_count")),
            comments=_int(info.get("direct_reply_count")),
            shares=_int(info.get("repost_count")) + _int(info.get("quote_count")),
            views=0,
            image=image,
            raw_ref=pk,
        )
        if item:
            out.append(item)
    return out


def _instagram_posts(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in data.get("items") or []:
        if not isinstance(row, dict):
            continue
        owner = row.get("owner") or {}
        code = str(row.get("code") or row.get("shortcode") or "")
        published = _epoch(row.get("taken_at") or row.get("taken_at_timestamp"))
        if not published:
            try:
                published = int((int(row.get("pk") or 0) >> 23) + 1314220021721) // 1000
            except (TypeError, ValueError):
                published = 0
        item = _post(
            platform="instagram",
            post_id=code,
            text=str(row.get("caption_text") or ""),
            url=f"https://www.instagram.com/p/{code}/" if code else "",
            author=str(owner.get("full_name") or owner.get("username") or ""),
            published_ts=published,
            likes=_int(row.get("like_count")),
            comments=_int(row.get("comment_count")),
            shares=0,
            views=_int(row.get("view_count")),
            image=str(row.get("thumbnail_url") or ""),
            raw_ref=code,
        )
        if item:
            out.append(item)
    return out


def _x_posts(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in data.get("timeline") or []:
        if not isinstance(row, dict) or row.get("type") not in (None, "tweet"):
            continue
        tweet_id = str(row.get("tweet_id") or "")
        username = str(row.get("screen_name") or (row.get("user_info") or {}).get("screen_name") or "")
        user = row.get("user_info") or {}
        media = ((row.get("media") or {}).get("photo") or [])
        image = str(media[0].get("media_url_https") or "") if media and isinstance(media[0], dict) else ""
        item = _post(
            platform="x",
            post_id=tweet_id,
            text=str(row.get("text") or ""),
            url=f"https://x.com/{username}/status/{tweet_id}" if username else "",
            author=str(user.get("name") or username),
            published_ts=_epoch(row.get("created_at")),
            likes=_int(row.get("favorites")),
            comments=_int(row.get("replies")),
            shares=_int(row.get("retweets")) + _int(row.get("quotes")),
            views=_int(row.get("views")),
            image=image,
            raw_ref=tweet_id,
        )
        if item:
            out.append(item)
    return out


def _tiktok_posts(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    rows = data.get("aweme_list") or data.get("search_item_list") or []
    for wrapper in rows:
        row = wrapper
        if isinstance(wrapper, dict):
            row = wrapper.get("aweme_info") or wrapper.get("search_aweme_info") or wrapper
        if not isinstance(row, dict):
            continue
        aweme_id = str(row.get("aweme_id") or "")
        author = row.get("author") or {}
        stats = row.get("statistics") or {}
        video = row.get("video") or {}
        cover = (video.get("cover") or video.get("origin_cover") or {})
        urls = cover.get("url_list") or []
        username = str(author.get("unique_id") or author.get("nickname") or "")
        item = _post(
            platform="tiktok",
            post_id=aweme_id,
            text=str(row.get("desc") or ""),
            url=str(row.get("share_url") or (f"https://www.tiktok.com/@{username}/video/{aweme_id}" if username else "")),
            author=str(author.get("nickname") or username),
            published_ts=_epoch(row.get("create_time")),
            likes=_int(stats.get("digg_count")),
            comments=_int(stats.get("comment_count")),
            shares=_int(stats.get("share_count")),
            views=_int(stats.get("play_count")),
            image=str(urls[0] if urls else ""),
            raw_ref=aweme_id,
        )
        if item:
            out.append(item)
    return out


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _douyin_posts(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _walk_dicts(data):
        aweme_id = str(row.get("aweme_id") or row.get("AwemeId") or "")
        desc = str(row.get("desc") or row.get("Desc") or "")
        if not aweme_id or aweme_id in seen or not desc:
            continue
        stats = row.get("statistics") or row.get("Statistics") or {}
        author = row.get("author") or row.get("Author") or {}
        video = row.get("video") or row.get("Video") or {}
        cover = video.get("cover") or video.get("Cover") or video.get("origin_cover") or {}
        urls = cover.get("url_list") or cover.get("UrlList") or []
        username = str(
            author.get("unique_id")
            or author.get("UniqueId")
            or author.get("nickname")
            or author.get("Nickname")
            or ""
        )
        item = _post(
            platform="douyin",
            post_id=aweme_id,
            text=desc,
            url=str(row.get("share_url") or row.get("ShareUrl") or (f"https://www.douyin.com/video/{aweme_id}")),
            author=str(author.get("nickname") or author.get("Nickname") or username),
            published_ts=_epoch(row.get("create_time") or row.get("CreateTime")),
            likes=_int(stats.get("digg_count") or stats.get("DiggCount")),
            comments=_int(stats.get("comment_count") or stats.get("CommentCount")),
            shares=_int(stats.get("share_count") or stats.get("ShareCount")),
            views=_int(stats.get("play_count") or stats.get("PlayCount")),
            image=str(urls[0] if urls else ""),
            raw_ref=aweme_id,
        )
        if item:
            seen.add(aweme_id)
            out.append(item)
    return out


def _xiaohongshu_posts(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for wrapper in _walk_dicts(data):
        row = wrapper.get("note_card") or wrapper.get("note") or wrapper
        if not isinstance(row, dict):
            continue
        note_id = str(row.get("note_id") or row.get("id") or "")
        if not note_id or note_id in seen:
            continue
        user = row.get("user") or {}
        interact = row.get("interact_info") or row.get("interact") or {
            "liked_count": row.get("liked_count"),
            "comment_count": row.get("comments_count") or row.get("comment_count"),
            "share_count": row.get("shared_count"),
            "view_count": row.get("view_count"),
        }
        title = str(row.get("title") or "")
        desc = str(row.get("desc") or row.get("description") or "")
        text = "\n".join(part for part in (title, desc) if part).strip()
        if not text:
            continue
        images = row.get("images_list") or []
        image = ""
        if images and isinstance(images[0], dict):
            image = str(images[0].get("url") or images[0].get("url_size_large") or "")
        item = _post(
            platform="xiaohongshu",
            post_id=note_id,
            text=text,
            url=f"https://www.xiaohongshu.com/explore/{note_id}",
            author=str(user.get("nickname") or user.get("nick_name") or ""),
            published_ts=_epoch(row.get("time") or row.get("timestamp") or row.get("publish_time") or row.get("create_time")),
            likes=_int(interact.get("liked_count") or interact.get("like_count")),
            comments=_int(interact.get("comment_count")),
            shares=_int(interact.get("share_count")),
            views=_int(interact.get("view_count")),
            image=str(row.get("cover") or image),
            raw_ref=note_id,
        )
        if item:
            seen.add(note_id)
            out.append(item)
    return out


def _kuaishou_posts(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in _walk_dicts(data):
        photo = row.get("photo") if isinstance(row.get("photo"), dict) else row
        photo_id = str(photo.get("id") or photo.get("photo_id") or "")
        if not photo_id or photo_id in seen:
            continue
        caption = str(photo.get("caption") or photo.get("desc") or "")
        if not caption:
            continue
        user = photo.get("user") or {}
        item = _post(
            platform="kuaishou",
            post_id=photo_id,
            text=caption,
            url=f"https://www.kuaishou.com/short-video/{photo_id}",
            author=str(user.get("user_name") or user.get("name") or photo.get("user_name") or ""),
            published_ts=_epoch(photo.get("timestamp") or photo.get("create_time")),
            likes=_int(photo.get("likeCount") or photo.get("like_count")),
            comments=_int(photo.get("commentCount") or photo.get("comment_count")),
            shares=_int(photo.get("shareCount") or photo.get("share_count")),
            views=_int(photo.get("viewCount") or photo.get("view_count")),
            image=str(photo.get("cover") or photo.get("coverUrl") or ""),
            raw_ref=photo_id,
        )
        if item:
            seen.add(photo_id)
            out.append(item)
    return out


def _bilibili_posts(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    root = data.get("data") if isinstance(data.get("data"), dict) else data
    for row in root.get("result") or []:
        if not isinstance(row, dict) or row.get("type") != "video":
            continue
        bvid = str(row.get("bvid") or "")
        title = re.sub(r"<[^>]+>", "", str(row.get("title") or "")).strip()
        if not bvid or not title:
            continue
        item = _post(
            platform="bilibili",
            post_id=bvid,
            text=title,
            url=f"https://www.bilibili.com/video/{bvid}",
            author=str(row.get("author") or ""),
            published_ts=_epoch(row.get("pubdate")),
            likes=_int(row.get("like")),
            comments=_int(row.get("review")),
            shares=0,
            views=_int(row.get("play")),
            image=("https:" + str(row.get("pic"))) if str(row.get("pic") or "").startswith("//") else str(row.get("pic") or ""),
            raw_ref=bvid,
        )
        if item:
            item["bili_aid"] = str(row.get("aid") or row.get("id") or "")
            out.append(item)
    return out


def _ensure_bilibili_cookie(*, force: bool = False) -> str:
    global _BILIBILI_COOKIE, _BILIBILI_COOKIE_AT
    with _BILIBILI_COOKIE_LOCK:
        if (
            not force
            and _BILIBILI_COOKIE
            and time.time() - _BILIBILI_COOKIE_AT < 6 * 3600
        ):
            return _BILIBILI_COOKIE
        request = urllib.request.Request(
            "https://api.bilibili.com/x/frontend/finger/spi",
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json, text/plain, */*",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(
                    response.read().decode("utf-8", "replace")
                )
            data = payload.get("data") or {}
            b3 = str(data.get("b_3") or "")
            b4 = str(data.get("b_4") or "")
            if b3:
                _BILIBILI_COOKIE = f"buvid3={b3}"
                if b4:
                    _BILIBILI_COOKIE += f"; buvid4={b4}"
                _BILIBILI_COOKIE_AT = time.time()
        except Exception:
            _BILIBILI_COOKIE = ""
            _BILIBILI_COOKIE_AT = 0.0
        return _BILIBILI_COOKIE


def _bilibili_free_get(path: str, params: dict[str, Any]) -> dict[str, Any]:
    """Bilibili公开Web接口，不消耗TikHub额度。"""
    global _BILIBILI_LAST_REQUEST
    url = "https://api.bilibili.com" + path + "?" + urllib.parse.urlencode(params)
    last_error = ""
    for attempt in range(3):
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
            "Origin": "https://www.bilibili.com",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8",
        }
        cookie = _ensure_bilibili_cookie(force=attempt > 0)
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(url, headers=headers)
        try:
            # 免费接口对请求频率很敏感，全局串行并保持至少1.2秒间隔。
            with _BILIBILI_RATE_LOCK:
                wait_seconds = max(
                    0.0,
                    1.2 - (time.monotonic() - _BILIBILI_LAST_REQUEST),
                )
                if wait_seconds:
                    time.sleep(wait_seconds)
                with urllib.request.urlopen(request, timeout=20) as response:
                    payload = json.loads(
                        response.read().decode("utf-8", "replace")
                    )
                _BILIBILI_LAST_REQUEST = time.monotonic()
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}"
            if exc.code in {412, 429} and attempt < 2:
                time.sleep(2.0 * (attempt + 1))
                continue
            raise TikHubError(
                f"Bilibili免费接口风控或限流：HTTP {exc.code}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise TikHubError(
                f"Bilibili免费接口不可用：{type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise TikHubError("Bilibili免费接口返回格式异常")
        code = int(payload.get("code") or 0)
        if code == 0:
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        last_error = str(payload.get("message") or f"code {code}")
        if code in {-352, -412, 412, 429} and attempt < 2:
            time.sleep(2.0 * (attempt + 1))
            continue
        raise TikHubError(f"Bilibili免费接口返回错误：{last_error}")
    raise TikHubError(f"Bilibili免费接口请求失败：{last_error}")


def _nested_value(data: Any, keys: set[str]) -> str:
    for row in _walk_dicts(data):
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


def _search_next_state(
    platform: str,
    data: dict[str, Any],
    current_cursor: str,
    pagination: dict[str, str] | None,
) -> tuple[str, dict[str, str]]:
    state = dict(pagination or {})
    if platform == "instagram":
        cursor = _nested_value(data, {"next_max_id", "end_cursor"})
    elif platform == "x":
        cursor = _nested_value(data, {"next_cursor", "cursor"})
    elif platform == "douyin":
        cursor = _nested_value(data, {"cursor"})
    elif platform == "kuaishou":
        cursor = _nested_value(data, {"pcursor"})
    elif platform == "xiaohongshu":
        state["search_id"] = _nested_value(data, {"search_id"})
        state["search_session_id"] = _nested_value(
            data,
            {"search_session_id"},
        )
        cursor = ""
    else:
        cursor = ""
    if cursor and cursor == current_cursor:
        cursor = ""
    return cursor, state


def _filter_social_rows(
    rows: list[dict[str, Any]],
    query: str,
    names: dict[str, list[str]],
    intended_region: str,
) -> list[dict[str, Any]]:
    cutoff = int(time.time()) - MAX_WINDOW_HOURS * 3600
    query_plain = to_simplified(query).replace("臺", "台").casefold()
    query_election_context = any(
        term in query_plain
        for term in (
            "选举", "选战", "候选人", "九合一", "投票", "民调",
            "市长", "县长", "议员", "政策", "争议", "造势",
            "election", "mayor", "council", "poll", "campaign",
            "vote", "politics", "kmt", "dpp", "tpp",
        )
    )
    filtered: list[dict[str, Any]] = []
    for row in rows:
        published = int(row.get("published_ts") or 0)
        if not published or published < cutoff:
            continue
        text = row.get("text") or ""
        plain = to_simplified(text).replace("臺", "台").casefold()
        election_like = any(
            term in plain
            for term in (
                "选举", "選舉", "选战", "選戰", "候选人", "候選人",
                "县市长", "縣市長", "县市议员", "縣市議員",
                "地方选举", "地方選舉", "九合一", "投票", "民调", "民調",
                "election", "mayor", "council", "poll", "campaign",
                "vote", "politics", "kmt", "dpp", "tpp",
            )
        )
        if not election_like and not query_election_context:
            continue
        region_scores: dict[str, int] = {}
        for candidate_region, candidate_names in names.items():
            region_plain = to_simplified(candidate_region).replace("台", "").casefold()
            short_region = region_plain.replace("县", "").replace("市", "")
            direct_region = (
                region_plain in plain
                or bool(short_region and short_region in plain)
            )
            candidate_hit = any(
                to_simplified(name).casefold() in plain
                for name in candidate_names
            )
            region_scores[candidate_region] = (
                (4 if direct_region else 0) + (2 if candidate_hit else 0)
            )
        region = max(region_scores, key=region_scores.get)
        relevance = region_scores[region]
        if not region or relevance <= 0:
            continue
        if relevance == 2 and not any(
            term in plain
            for term in (
                "新北", "宜兰", "台湾", "九合一", "选战", "選戰",
                "县市长", "縣市長", "议员", "議員",
                "taiwan", "election", "mayor", "council", "politics",
            )
        ):
            continue
        row["relevance"] = relevance
        row["region"] = region
        row["age_hours"] = round(max(0, (time.time() - published) / 3600), 1)
        row["heat"] = round(_heat(row, cutoff) + relevance * 0.08, 3)
        filtered.append(row)
    return filtered


def _search_page(
    platform: str,
    query: str,
    names: dict[str, list[str]],
    intended_region: str,
    page: int = 1,
    *,
    cursor: str = "",
    pagination: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], str, str, dict[str, str]]:
    pagination = dict(pagination or {})
    try:
        if platform == "threads":
            data = _get("/api/v1/threads/web/search_posts", {"query": query})
            rows = _threads_posts(data)
        elif platform == "instagram":
            params: dict[str, Any] = {"tag": query.replace(" ", "")}
            if cursor:
                params["max_id"] = cursor
            data = _get(
                "/api/v1/instagram/v3/get_hashtag_posts",
                params,
            )
            rows = _instagram_posts(data)
        elif platform == "x":
            params = {"keyword": query, "search_type": "Latest"}
            if cursor:
                params["cursor"] = cursor
            data = _get(
                "/api/v1/twitter/web/fetch_search_timeline",
                params,
            )
            rows = _x_posts(data)
        elif platform == "tiktok":
            data = _get(
                "/api/v1/tiktok/app/v3/fetch_video_search_result",
                {
                    "keyword": query,
                    "count": 20,
                    "offset": max(0, page - 1) * 20,
                    "sort_type": 0,
                    "publish_time": 0,
                    "region": "TW",
                },
            )
            rows = _tiktok_posts(data)
        elif platform == "douyin":
            cursor_value: Any = int(cursor) if str(cursor).isdigit() else cursor
            data = _post_json(
                "/api/v1/douyin/search/fetch_video_search_v4",
                {"keyword": query, "cursor": cursor_value or 0},
            )
            rows = _douyin_posts(data)
        elif platform == "xiaohongshu":
            params = {
                "keyword": query,
                "page": max(1, page),
                "sort_type": "popularity_descending",
                "note_type": "不限",
                "time_filter": "不限",
            }
            if pagination.get("search_id"):
                params["search_id"] = pagination["search_id"]
            if pagination.get("search_session_id"):
                params["search_session_id"] = pagination["search_session_id"]
            data = _get(
                "/api/v1/xiaohongshu/app_v2/search_notes",
                params,
            )
            rows = _xiaohongshu_posts(data)
        elif platform == "kuaishou":
            data = _get(
                "/api/v1/kuaishou/app/search_video_v2",
                {"keyword": query, "pcursor": cursor},
            )
            rows = _kuaishou_posts(data)
        elif platform == "bilibili":
            try:
                data = _bilibili_free_get(
                    "/x/web-interface/search/type",
                    {
                        "search_type": "video",
                        "keyword": query,
                        "page": page,
                        "page_size": 20,
                        "order": "pubdate",
                    },
                )
            except TikHubError as free_exc:
                if any(
                    marker in str(free_exc)
                    for marker in ("风控", "限流", "HTTP 412", "HTTP 429")
                ):
                    raise
                data = _get(
                    "/api/v1/bilibili/web/fetch_general_search",
                    {
                        "keyword": query,
                        "order": "pubdate",
                        "page": page,
                        "page_size": 20,
                        "duration": 0,
                        "pubtime_begin_s": 0,
                        "pubtime_end_s": 0,
                    },
                )
            rows = _bilibili_posts(data)
        else:
            return [], f"不支持的平台：{platform}", "", pagination
        next_cursor, next_pagination = _search_next_state(
            platform,
            data,
            cursor,
            pagination,
        )
    except TikHubError as exc:
        return (
            [],
            f"{PLATFORMS.get(platform, platform)}「{query}」：{exc}",
            "",
            pagination,
        )
    return (
        _filter_social_rows(rows, query, names, intended_region),
        "",
        next_cursor,
        next_pagination,
    )


def _search(
    platform: str,
    query: str,
    names: dict[str, list[str]],
    intended_region: str,
    page: int = 1,
) -> tuple[list[dict[str, Any]], str]:
    rows, warning, _, _ = _search_page(
        platform,
        query,
        names,
        intended_region,
        page=page,
    )
    return rows, warning


def _search_paginated(
    platform: str,
    query: str,
    names: dict[str, list[str]],
    intended_region: str,
    *,
    max_pages: int,
) -> tuple[list[dict[str, Any]], list[str], int]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    cursor = ""
    pagination: dict[str, str] = {}
    calls = 0
    empty_pages = 0
    for page in range(1, max(1, max_pages) + 1):
        page_rows, warning, next_cursor, next_pagination = _search_page(
            platform,
            query,
            names,
            intended_region,
            page=page,
            cursor=cursor,
            pagination=pagination,
        )
        calls += 1
        if warning:
            warnings.append(warning)
            break
        pagination.update(next_pagination)
        if not page_rows:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
            rows.extend(page_rows)
        if page >= max_pages:
            break
        if platform in {"instagram", "x", "douyin", "kuaishou"}:
            if not next_cursor:
                break
            cursor = next_cursor
        elif platform == "xiaohongshu":
            if not (
                pagination.get("search_id")
                or pagination.get("search_session_id")
            ):
                break
        time.sleep(0.18)
    return rows, warnings, calls


def _search_bilibili_full(
    query: str,
    names: dict[str, list[str]],
    region: str,
    *,
    max_pages: int = 12,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Bilibili免费搜索多页读取，直到时间边界、空页或页数上限。"""
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    empty_pages = 0
    calls = 0
    for page in range(1, max_pages + 1):
        calls += 1
        page_rows, warning = _search("bilibili", query, names, region, page=page)
        if warning:
            warnings.append(warning)
            break
        if not page_rows:
            empty_pages += 1
            if empty_pages >= 2:
                break
            continue
        empty_pages = 0
        rows.extend(page_rows)
        time.sleep(0.25)
    return rows, warnings, calls


def _find_comments(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {
                "comments",
                "comment_list",
                "timeline",
                "thread",
                "edges",
                "rootComments",
                "replies",
                "pinned_replies",
            } and isinstance(child, list):
                for item in child:
                    if isinstance(item, dict):
                        found.append(item)
            elif isinstance(child, (dict, list)):
                found.extend(_find_comments(child))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_comments(item))
    return found


def _comment_row(item: dict[str, Any], cutoff: int) -> dict[str, Any] | None:
    row = item.get("node") if isinstance(item.get("node"), dict) else item
    row = row.get("comment") if isinstance(row.get("comment"), dict) else row
    user = (
        row.get("user")
        or row.get("user_info")
        or row.get("author")
        or row.get("member")
        or row
    )
    if isinstance(user, dict) and isinstance(user.get("core"), dict):
        user = {**user, **user.get("core", {})}
    text = (
        row.get("text")
        or row.get("content")
        or row.get("comment")
        or row.get("body")
        or row.get("caption")
        or ""
    )
    if isinstance(text, dict):
        text = text.get("text") or text.get("content") or text.get("message") or ""
    likes = (
        row.get("comment_like_count")
        or row.get("like_count")
        or row.get("digg_count")
        or row.get("favorites")
        or row.get("likes")
        or row.get("like")
        or 0
    )
    published = _epoch(
        row.get("created_at")
        or row.get("create_time")
        or row.get("published_at")
        or row.get("timestamp")
        or row.get("taken_at")
        or row.get("ctime")
    )
    if published and published < cutoff:
        return None
    comment_id = str(
        row.get("comment_id")
        or row.get("cid")
        or row.get("post_id")
        or row.get("rpid_str")
        or row.get("rpid")
        or row.get("pk")
        or row.get("id")
        or ""
    )
    if not str(text).strip() or not comment_id:
        return None
    reply_control = row.get("reply_control")
    location = (
        reply_control.get("location")
        if isinstance(reply_control, dict)
        else ""
    )
    return {
        "id": comment_id,
        "author": str(
            user.get("full_name")
            or user.get("nickname")
            or user.get("name")
            or user.get("screen_name")
            or user.get("username")
            or user.get("uname")
            or user.get("author_name")
            or ""
        ),
        "text": str(text).strip(),
        "likes": _int(likes),
        "published_ts": published,
        "published_at": (
            datetime.fromtimestamp(published, tz=timezone.utc).isoformat(timespec="seconds")
            if published else ""
        ),
        "location": str(row.get("authorArea") or location or ""),
    }


def _comments(platform: str, post: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    data: dict[str, Any] | None = None
    last_error = ""
    for attempt in range(3 if platform == "threads" else 1):
        try:
            if platform == "threads":
                data = _get(
                    "/api/v1/threads/web/fetch_post_comments",
                    {"post_id": post["raw_ref"]},
                )
            elif platform == "instagram":
                data = _get(
                    "/api/v1/instagram/v3/get_post_comments",
                    {"code": post["raw_ref"], "sort_order": "popular"},
                )
            elif platform == "x":
                data = _get(
                    "/api/v1/twitter/web/fetch_post_comments",
                    {"tweet_id": post["raw_ref"]},
                )
            elif platform == "tiktok":
                data = _get(
                    "/api/v1/tiktok/web/fetch_post_comment",
                    {
                        "aweme_id": post["raw_ref"],
                        "cursor": 0,
                        "count": 30,
                        "current_region": "TW",
                    },
                )
            elif platform == "douyin":
                data = _get(
                    "/api/v1/douyin/app/v3/fetch_video_comments",
                    {"aweme_id": post["raw_ref"], "cursor": 0, "count": 20},
                )
            elif platform == "xiaohongshu":
                data = _get(
                    "/api/v1/xiaohongshu/app_v2/get_note_comments",
                    {
                        "note_id": post["raw_ref"],
                        "cursor": "",
                        "index": 0,
                        "pageArea": "UNFOLDED",
                        "sort_strategy": "like_count",
                    },
                )
            elif platform == "kuaishou":
                data = _get(
                    "/api/v1/kuaishou/app/fetch_video_comment",
                    {"photo_id": post["raw_ref"], "pcursor": ""},
                )
            elif platform == "bilibili":
                try:
                    aid = str(post.get("bili_aid") or "")
                    if not aid:
                        info = _bilibili_free_get(
                            "/x/web-interface/view",
                            {"bvid": post["raw_ref"]},
                        )
                        aid = str(info.get("aid") or "")
                    if not aid:
                        raise TikHubError("Bilibili视频AID缺失")
                    data = _bilibili_free_get(
                        "/x/v2/reply",
                        {
                            "type": 1,
                            "oid": aid,
                            "sort": 2,
                            "pn": 1,
                            "ps": 50,
                        },
                    )
                except TikHubError:
                    data = _get(
                        "/api/v1/bilibili/app/fetch_video_comments",
                        {
                            "bv_id": post["raw_ref"],
                            "mode": 3,
                            "next_offset": 1,
                        },
                    )
            elif platform == "facebook":
                return [], "Facebook免费方案当前只读取帖文，不批量读取评论"
            else:
                return [], "不支持的平台"
            break
        except TikHubError as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(1.2)
            elif attempt == 1:
                time.sleep(1.6)
    rows: list[dict[str, Any]] = []
    if data is not None:
        for item in _find_comments(data):
            converted = _comment_row(item, 0)
            if converted:
                rows.append(converted)
    if not rows and platform == "threads":
        try:
            code = str(post.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            detail = _get(
                "/api/v1/threads/web/fetch_post_detail_v2",
                {"post_id": code} if code else {"url": post.get("url") or ""},
            )
            for item in _find_comments(detail):
                converted = _comment_row(item, 0)
                if converted:
                    rows.append(converted)
        except TikHubError as exc:
            last_error = last_error or str(exc)
    unique = {row["id"]: row for row in rows}
    ordered = sorted(unique.values(), key=lambda row: (-row["likes"], -(row["published_ts"] or 0)))[
        :TOP_COMMENTS
    ]
    return ordered, ("" if ordered else last_error)


def _collect(
    platform: str,
    requested_hours: int = 24,
    *,
    mode: str = "refresh",
) -> dict[str, Any]:
    if platform not in PLATFORMS:
        raise TikHubError("平台参数无效")
    mode = "extend" if mode == "extend" else "refresh"
    extended = mode == "extend"
    names = _focus_names()
    standard_limit = None if extended else NORMAL_QUERIES_PER_REGION
    query_pairs: list[tuple[str, str]] = []
    if platform == "facebook":
        query_pairs = []
    elif platform == "bilibili":
        bili_limit = 12 if extended else 8
        new_taipei = list(BILIBILI_REGION_QUERIES["新北市"][:bili_limit])
        yilan = list(BILIBILI_REGION_QUERIES["宜蘭縣"][:bili_limit])
        rotation = (
            _platform_budget_meta("bilibili")["daily_refreshes"] * 2
        ) % max(1, min(len(new_taipei), len(yilan)))
        if rotation:
            new_taipei = new_taipei[rotation:] + new_taipei[:rotation]
            yilan = yilan[rotation:] + yilan[:rotation]
        for index in range(max(len(new_taipei), len(yilan))):
            if index < len(new_taipei):
                query_pairs.append(("新北市", new_taipei[index]))
            if index < len(yilan):
                query_pairs.append(("宜蘭縣", yilan[index]))
    else:
        for region in ("新北市", "宜蘭縣"):
            base_queries = (
                REGION_QUERIES[region]
                if standard_limit is None
                else REGION_QUERIES[region][:standard_limit]
            )
            for query in base_queries:
                query_pairs.append((region, query))
    if extended and platform not in {"bilibili", "facebook"}:
        extra_limit = len(ENRICHMENT_QUERIES["新北市"])
        for region in ("新北市", "宜蘭縣"):
            for query in ENRICHMENT_QUERIES[region][:extra_limit]:
                pair = (region, query)
                if pair not in query_pairs:
                    query_pairs.append(pair)
    queries = list(dict.fromkeys(query_pairs))
    warnings: list[str] = []
    posts: list[dict[str, Any]] = []
    search_calls = 0
    enrichment_calls = 0
    page_mode = "extend" if extended else "normal"
    page_limit = QUERY_PAGE_LIMITS[platform][page_mode]
    if platform == "facebook":
        from .config import load_settings

        settings = load_settings()
        facebook_accounts = _social_accounts("facebook", names)
        facebook_rows, facebook_warnings = (
            facebook_social.fetch_facebook_posts(
                facebook_accounts,
                since_ts=int(time.time()) - MAX_WINDOW_HOURS * 3600,
                max_per_account=40 if extended else 20,
                cookies=settings.facebook_cookies,
                headless=settings.facebook_headless,
            )
        )
        posts.extend(facebook_rows)
        warnings.extend(facebook_warnings)
        search_calls += len(facebook_accounts)
    elif platform == "bilibili":
        for offset in range(0, len(queries), 2):
            batch = queries[offset:offset + 2]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(
                        _search_bilibili_full,
                        query,
                        names,
                        region,
                        max_pages=page_limit,
                    )
                    for region, query in batch
                ]
                for future in as_completed(futures, timeout=240 if extended else 120):
                    rows, query_warnings, calls = future.result()
                    posts.extend(rows)
                    warnings.extend(query_warnings)
                    search_calls += calls
            time.sleep(0.4)
    else:
        for offset in range(0, len(queries), 2):
            batch = queries[offset:offset + 2]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {
                    pool.submit(
                        _search_paginated,
                        platform,
                        query,
                        names,
                        region,
                        max_pages=page_limit,
                    ): (region, query)
                    for region, query in batch
                }
                for future in as_completed(
                    futures,
                    timeout=180 if extended else 90,
                ):
                    try:
                        rows, query_warnings, calls = future.result()
                    except Exception as exc:  # noqa: BLE001
                        region, query = futures[future]
                        warnings.append(
                            f"{PLATFORMS[platform]}「{query}」："
                            f"{type(exc).__name__}"
                        )
                        search_calls += 1
                        continue
                    posts.extend(rows)
                    warnings.extend(query_warnings)
                    search_calls += calls
            time.sleep(0.25)
    account_page_limit = ACCOUNT_PAGE_LIMITS.get(
        platform,
        {"normal": 1, "extend": 1},
    )[page_mode]
    accounts = _social_accounts(platform, names)
    account_rows, account_warnings, account_calls = _account_posts(
        platform,
        accounts,
        max_pages=account_page_limit,
    )
    posts.extend(account_rows)
    warnings.extend(account_warnings)
    # 日常刷新若内容仍少，只追加两组长尾词；首次扩池已经预载完整长尾词。
    if not extended and platform != "facebook":
        for region in ("新北市", "宜蘭縣"):
            recent_count = sum(
                1
                for row in posts
                if row.get("region") == region
                and int(row.get("published_ts") or 0)
                >= time.time() - requested_hours * 3600
            )
            if recent_count >= 30:
                continue
            for query in ENRICHMENT_QUERIES[region][:2]:
                rows, query_warnings, calls = _search_paginated(
                    platform,
                    query,
                    names,
                    region,
                    max_pages=1,
                )
                posts.extend(rows)
                warnings.extend(query_warnings)
                enrichment_calls += 1
                search_calls += calls
                time.sleep(0.2)
            warnings.append(
                f"{region} 近{requested_hours}小时少于30篇，已补充长尾关键词。"
            )
    unique = {
        (row["platform"], row["id"]): row
        for row in posts
    }
    posts = list(unique.values())
    selected: list[dict[str, Any]] = [
        row for row in posts if row.get("official_account")
    ]
    for region in ("新北市", "宜蘭縣"):
        region_rows = sorted(
            [
                row
                for row in posts
                if row.get("region") == region and not row.get("official_account")
            ],
            key=lambda row: float(row.get("heat") or 0),
            reverse=True,
        )
        region_limit = (
            BILIBILI_POSTS_PER_REGION
            if platform == "bilibili"
            else TOP_POSTS_PER_REGION
        )
        selected.extend(region_rows[:region_limit])
    selected = list({
        (row.get("platform") or "", row.get("id") or ""): row
        for row in selected
    }.values())
    selected.sort(
        key=lambda row: (
            row.get("region") or "",
            -float(row.get("heat") or 0),
        )
    )
    for row in selected:
        _annotate_candidate_tags(row, names)
    prices = PRICE_PER_CALL.get(platform)
    budget_cny = 0.0 if platform in {"bilibili", "facebook"} else (
        EXTEND_BUDGET_CNY if extended else REFRESH_BUDGET_CNY
    )
    platform_budget_usd = budget_cny / CNY_PER_USD
    budget_comment_limit = COMMENT_BATCH_LIMITS.get(platform, 6)
    if prices:
        comment_multiplier = COMMENT_CALL_MULTIPLIER.get(platform, 1)
        remaining_usd = max(
            0.0,
            platform_budget_usd - search_calls * prices["search"],
        )
        comment_unit = (
            prices["comment"] * comment_multiplier
            + prices["fallback"]
        )
        if comment_unit > 0:
            budget_comment_limit = min(
                budget_comment_limit,
                int(remaining_usd / comment_unit),
            )
    else:
        budget_comment_limit = COMMENT_BATCH_LIMITS.get(platform, 6)
    comment_targets: list[dict[str, Any]] = []
    comment_limit = max(0, min(budget_comment_limit, len(selected)))
    quotas = (
        ((24, 12), (168, 8), (MAX_WINDOW_HOURS, 6))
        if extended
        else ((24, 4), (168, 3), (MAX_WINDOW_HOURS, 2))
    )
    for window, quota in quotas:
        rows = sorted(
            [
                row for row in selected
                if float(row.get("age_hours") or 0) <= window
            ],
            key=lambda row: float(row.get("heat") or 0),
            reverse=True,
        )
        for row in rows:
            if len(comment_targets) >= comment_limit:
                break
            if row not in comment_targets:
                comment_targets.append(row)
                if sum(
                    1 for item in comment_targets
                    if float(item.get("age_hours") or 0) <= window
                ) >= quota:
                    break
    if len(comment_targets) < comment_limit:
        for row in sorted(
            selected,
            key=lambda row: float(row.get("heat") or 0),
            reverse=True,
        ):
            if row not in comment_targets:
                comment_targets.append(row)
            if len(comment_targets) >= comment_limit:
                break
    if comment_targets:
        for offset in range(0, len(comment_targets), 4):
            batch = comment_targets[offset:offset + 4]
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    pool.submit(_comments, platform, row): row for row in batch
                }
                for future in as_completed(futures, timeout=90):
                    row = futures[future]
                    comments, warning = future.result()
                    row["comments_data"] = comments
                    row["comments_warning"] = warning
                    if warning:
                        warnings.append(warning)
            time.sleep(0.2)
    for row in selected:
        row.setdefault("comments_data", [])
    comment_calls = len(comment_targets)
    fallback_calls = len(comment_targets) if platform == "threads" else 0
    comment_multiplier = COMMENT_CALL_MULTIPLIER.get(platform, 1)
    comment_api_calls = comment_calls * comment_multiplier
    min_cost = (
        search_calls * prices["search"] + comment_calls * prices["comment"]
        if prices else None
    )
    max_cost = (
        search_calls * prices["search"]
        + comment_api_calls * prices["comment"]
        + fallback_calls * prices["fallback"]
        if prices else None
    )
    if prices:
        _BUDGET_USAGE[platform] = {
            "timestamp": time.time(),
            "spent_usd": float(max_cost or 0.0),
            "budget_usd": float(platform_budget_usd),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "configured": True,
        "source": "TikHub",
        "source_label": (
            "Bilibili免费公开接口（TikHub失败兜底）"
            if platform == "bilibili"
            else (
                "Facebook · Playwright 公开页面"
                if platform == "facebook"
                else f"TikHub · {PLATFORMS[platform]}公开搜索"
            )
        ),
        "platform": platform,
        "platform_label": PLATFORMS[platform],
        "window_hours": MAX_WINDOW_HOURS,
        "pool_window_hours": MAX_WINDOW_HOURS,
        "budget_mode": mode,
        "budget_cny": round(budget_cny, 2),
        "target_posts_per_region": (
            BILIBILI_POSTS_PER_REGION
            if platform == "bilibili"
            else TOP_POSTS_PER_REGION
        ),
        "fetched_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "query_count": len(queries) + enrichment_calls,
        "enrichment_calls": enrichment_calls,
        "estimated_api_calls": (
            search_calls
            + account_calls
            + comment_api_calls
            + fallback_calls
        ),
        "cost_estimate": {
            "currency": "USD",
            "budget_cny": round(budget_cny, 2),
            "global_budget_cny": round(budget_cny, 2),
            "cny_per_usd": CNY_PER_USD,
            "min": round(min_cost, 4) if min_cost is not None else None,
            "max": round(max_cost, 4) if max_cost is not None else None,
            "search_calls": search_calls,
            "account_calls": account_calls,
            "comment_calls": comment_calls,
            "comment_api_calls": comment_api_calls,
            "fallback_calls": fallback_calls,
            "comment_budget_limit": comment_limit,
        } if prices else None,
        "counts": {
            "posts": len(selected),
            "comments": sum(
                len(row.get("comments_data") or [])
                for row in selected
            ),
            "regions": len({
                row["region"] for row in selected if row.get("region")
            }),
            "likes": sum(int(row.get("likes") or 0) for row in selected),
        },
        "error": (
            warnings[0]
            if platform == "facebook" and not selected and warnings
            else ""
        ),
        "posts": selected,
        "warnings": [
            "平台内容池一次抓取近30天公开贴文；24小时、3天、7天、15天、30天窗口均从同一缓存池筛选。",
            "评论不按发布时间二次截断，读取热帖当前可返回的热门评论。",
            "点击批量结果中未取到评论的贴文时，会按该贴文再调用一次评论接口补抓。",
            "每个平台独立展示，不与其他平台合并；普通平台每个地区最多保留200篇，Bilibili最多500篇。",
            "首次扩池最高6元，日常刷新最高2元；预算按每次点击单独计算。",
            "已核验的新北、宜兰县市长候选人官方Threads／Instagram账号纳入内容池。",
            "评论按点赞数排序，最多保留10条；未展示的回复不纳入统计。",
            *warnings,
        ],
    }


def _view(pool: dict[str, Any], hours: int) -> dict[str, Any]:
    cutoff = time.time() - hours * 3600
    focus_names = _focus_names()
    posts = []
    for source_row in pool.get("posts") or []:
        if int(source_row.get("published_ts") or 0) < cutoff:
            continue
        posts.append(
            _annotate_candidate_tags(dict(source_row), focus_names)
        )
    posts.sort(
        key=lambda row: (
            row.get("region") or "",
            -float(row.get("heat") or 0),
        )
    )
    view = dict(pool)
    view.update(
        {
            "schema_version": SCHEMA_VERSION,
            "cache_schema_version": pool.get("schema_version"),
            "window_hours": hours,
            "pool_window_hours": MAX_WINDOW_HOURS,
            "posts": posts,
            "counts": {
                "posts": len(posts),
                "comments": sum(len(row.get("comments_data") or []) for row in posts),
                "regions": len({row.get("region") for row in posts if row.get("region")}),
                "likes": sum(int(row.get("likes") or 0) for row in posts),
            },
            "warnings": [
                *list(pool.get("warnings") or []),
                f"当前显示近{hours}小时；切换时间只筛选同一平台30天缓存池，不重复调用TikHub。",
            ],
        }
    )
    return view


def _cached_row_matches_focus(
    row: dict[str, Any],
    names: dict[str, list[str]],
) -> bool:
    """旧缓存合并前重新校验地区，清理历史误归类内容。"""
    plain = to_simplified(str(row.get("text") or "")).replace("臺", "台")
    if not plain:
        return False
    for region, candidates in names.items():
        region_plain = to_simplified(region).replace("台", "")
        short_region = region_plain.replace("县", "").replace("市", "")
        direct = (
            region_plain in plain
            or (short_region and short_region in plain)
        )
        candidate = any(
            to_simplified(name) in plain
            for name in candidates
        )
        if direct:
            return True
        if candidate and any(
            term in plain
            for term in (
                "台湾", "九合一", "选战", "选举", "候选人",
                "县市长", "议员", "民调", "政策",
            )
        ):
            return True
    return False


def _merge_social_pool(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    platform: str,
) -> dict[str, Any]:
    """把本次结果与最近非空缓存合并，避免部分失败冲掉可用内容。"""
    if not _has_content(previous):
        return current
    cutoff = int(time.time()) - MAX_WINDOW_HOURS * 3600
    names = _focus_names()
    merged_by_id: dict[tuple[str, str], dict[str, Any]] = {}
    for row in (previous or {}).get("posts") or []:
        post_id = str(row.get("id") or "").strip()
        if not post_id or int(row.get("published_ts") or 0) < cutoff:
            continue
        if not _cached_row_matches_focus(row, names):
            continue
        merged_by_id[(platform, post_id)] = dict(row)
    current_ids: set[str] = set()
    for row in current.get("posts") or []:
        post_id = str(row.get("id") or "").strip()
        if not post_id or not _cached_row_matches_focus(row, names):
            continue
        current_ids.add(post_id)
        key = (platform, post_id)
        old_row = merged_by_id.get(key) or {}
        combined = dict(old_row)
        combined.update(row)
        if not combined.get("comments_data") and old_row.get("comments_data"):
            combined["comments_data"] = old_row["comments_data"]
        if not combined.get("comments_warning") and old_row.get("comments_warning"):
            combined["comments_warning"] = old_row["comments_warning"]
        merged_by_id[key] = combined
    official = [
        row for row in merged_by_id.values()
        if row.get("official_account")
    ]
    regular_by_region: dict[str, list[dict[str, Any]]] = {}
    for row in merged_by_id.values():
        if row.get("official_account"):
            continue
        region = str(row.get("region") or "")
        regular_by_region.setdefault(region, []).append(row)
    selected = list(official)
    region_limit = (
        BILIBILI_POSTS_PER_REGION
        if platform == "bilibili"
        else TOP_POSTS_PER_REGION
    )
    for rows in regular_by_region.values():
        rows.sort(
            key=lambda row: float(row.get("heat") or 0),
            reverse=True,
        )
        selected.extend(rows[:region_limit])
    selected = list({
        (row.get("platform") or platform, str(row.get("id") or "")): row
        for row in selected
    }.values())
    selected.sort(
        key=lambda row: (
            row.get("region") or "",
            -float(row.get("heat") or 0),
        )
    )
    result = dict(current)
    old_ids = {
        str(row.get("id") or "").strip()
        for row in (previous or {}).get("posts") or []
        if str(row.get("id") or "").strip()
    }
    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "posts": selected,
            "counts": {
                "posts": len(selected),
                "comments": sum(
                    len(row.get("comments_data") or [])
                    for row in selected
                ),
                "regions": len({
                    row.get("region")
                    for row in selected
                    if row.get("region")
                }),
                "likes": sum(int(row.get("likes") or 0) for row in selected),
            },
            "merged_from_cache": True,
            "new_posts": len(current.get("posts") or []),
            "retained_from_cache": len(old_ids - current_ids),
        }
    )
    return result


def _get_social_core(
    platform: str = "threads",
    *,
    hours: int = 24,
    force: bool = False,
    extend: bool = False,
) -> dict[str, Any]:
    platform = platform if platform in PLATFORMS else "threads"
    hours = hours if hours in ALLOWED_HOURS else 24
    extend = bool(extend and force)
    extend_fallback = False
    if extend and platform not in {"bilibili", "facebook"}:
        meta = _platform_budget_meta(platform)
        if meta["extend_used"]:
            extend = False
            extend_fallback = True
    pool = _MEMORY.get(platform)
    if not _has_content(pool):
        disk = _read_cache(platform)
        if _has_content(disk):
            pool = disk
            _MEMORY[platform] = disk
    if not force:
        if _has_content(pool):
            cached_view = _view(pool, hours)
            if not _fresh(pool):
                cached_view["stale"] = True
                cached_view["warning"] = "当前显示上次有效缓存，点击“刷新当前平台”获取最新数据"
            return cached_view
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "configured": True,
            "source": "TikHub",
            "platform": platform,
            "platform_label": PLATFORMS[platform],
            "window_hours": hours,
            "needs_refresh": True,
            "error": "该平台暂无缓存，请先点击“扩建内容池”建立完整缓存",
            "posts": [],
            "counts": {"posts": 0, "comments": 0, "regions": 0, "likes": 0},
            "warnings": [],
        }
    if platform not in {"bilibili", "facebook"} and not _api_key():
        provider_error = "尚未配置 TIKHUB_API_KEY"
        if _has_content(pool):
            stale = _view(pool, hours)
            stale["stale"] = True
            stale["warning"] = f"{provider_error}，继续显示最近缓存"
            return stale
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "configured": False,
            "source": "TikHub",
            "platform": platform,
            "platform_label": PLATFORMS[platform],
            "window_hours": hours,
            "error": provider_error,
            "posts": [],
            "counts": {"posts": 0, "comments": 0, "regions": 0, "likes": 0},
            "warnings": [],
        }
    platform_lock = _platform_lock(platform)
    if not platform_lock.acquire(blocking=False):
        if _has_content(pool):
            stale = _view(pool, hours)
            stale["stale"] = True
            stale["warning"] = "TikHub正在后台更新，先显示最近缓存"
            return stale
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "source": "TikHub",
            "platform": platform,
            "platform_label": PLATFORMS[platform],
            "window_hours": hours,
            "error": "TikHub正在后台更新，请稍后重试",
            "posts": [],
            "counts": {"posts": 0, "comments": 0, "regions": 0, "likes": 0},
            "warnings": [],
        }
    try:
        budget_mode = "extend" if extend else "refresh"
        payload = _collect(platform, hours, mode=budget_mode)
        spent_usd = float(
            ((payload.get("cost_estimate") or {}).get("max") or 0.0)
        )
        _record_budget_usage(platform, budget_mode, spent_usd)
        if extend_fallback:
            payload["extend_already_used"] = True
            payload["budget_notice"] = "首次扩充已完成，本次按最高2元进行日常补充"
        if not payload.get("posts"):
            if _has_content(pool):
                stale = _view(pool, hours)
                stale["stale"] = True
                stale["refresh_empty"] = True
                stale["extend_already_used"] = extend_fallback
                stale["budget_notice"] = (
                    "首次扩充已完成，本次按最高2元进行日常补充"
                    if extend_fallback else ""
                )
                stale["refresh_attempted_at_iso"] = payload.get("fetched_at_iso")
                stale["refresh_warnings"] = list(payload.get("warnings") or [])
                stale["warnings"] = [
                    *list(stale.get("warnings") or []),
                    *list(payload.get("warnings") or []),
                    "本次刷新未返回可用贴文，已保留最近非空缓存。",
                ]
                stale["warning"] = "本次刷新未返回可用贴文，继续显示最近非空缓存"
                return stale
            payload["refresh_empty"] = True
            payload["warning"] = "本次刷新未返回可用贴文"
            return _view(payload, hours)
        if _has_content(pool):
            payload = _merge_social_pool(pool, payload, platform)
            if payload.get("retained_from_cache"):
                payload["partial_refresh"] = True
                payload["warning"] = (
                    f"本次抓取 {payload.get('new_posts', 0)} 条，"
                    f"已合并保留 {payload.get('retained_from_cache', 0)} 条最近缓存"
                )
        _MEMORY[platform] = payload
        _write_cache(platform, payload)
        return _view(payload, hours)
    except Exception as exc:  # noqa: BLE001
        cached = _read_cache(platform)
        if _has_content(cached):
            stale = _view(cached, hours)
            stale["stale"] = True
            stale["warning"] = f"刷新失败，显示最近缓存：{type(exc).__name__}"
            return stale
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "source": "TikHub",
            "platform": platform,
            "platform_label": PLATFORMS[platform],
            "window_hours": hours,
            "error": f"{type(exc).__name__}: {exc}",
            "posts": [],
            "counts": {"posts": 0, "comments": 0, "regions": 0, "likes": 0},
            "warnings": [],
        }
    finally:
        platform_lock.release()


def get_social(
    platform: str = "threads",
    *,
    hours: int = 24,
    force: bool = False,
    extend: bool = False,
) -> dict[str, Any]:
    normalized = platform if platform in PLATFORMS else "threads"
    payload = _get_social_core(
        normalized,
        hours=hours,
        force=force,
        extend=extend,
    )
    return _attach_budget_meta(payload, normalized)


def get_post_comments(platform: str, post_id: str) -> dict[str, Any]:
    """点击贴文时按ID补抓热门评论，不重新搜索贴文。"""
    platform = platform if platform in PLATFORMS else "threads"
    post_id = str(post_id or "").strip()
    if not post_id:
        raise TikHubError("缺少贴文ID")
    payload = _MEMORY.get(platform) or _read_cache(platform) or {}
    post = next(
        (
            dict(row)
            for row in payload.get("posts") or []
            if str(row.get("id") or "") == post_id
        ),
        None,
    )
    if not post:
        raise TikHubError("当前平台缓存中没有该贴文，请先刷新该平台")
    prices = PRICE_PER_CALL.get(platform)
    usage = _BUDGET_USAGE.get(platform)
    if prices and usage and time.time() - usage["timestamp"] < CACHE_SECONDS:
        unit = (
            prices["comment"] * COMMENT_CALL_MULTIPLIER.get(platform, 1)
            + prices["fallback"]
        )
        if usage["spent_usd"] + unit > usage["budget_usd"] + 1e-9:
            budget_cny = usage["budget_usd"] * CNY_PER_USD
            raise TikHubError(
                f"该平台本次{budget_cny:.0f}元预算已用完，请稍后再刷新"
            )
    comments, warning = _comments(platform, post)
    if prices:
        _record_budget_usage(
            platform,
            "comment",
            (
                prices["comment"]
                * COMMENT_CALL_MULTIPLIER.get(platform, 1)
                + (prices["fallback"] if warning else 0.0)
            ),
        )
    if prices and usage and time.time() - usage["timestamp"] < CACHE_SECONDS:
        usage["spent_usd"] += prices["comment"] + (prices["fallback"] if warning else 0.0)
    return {
        "ok": True,
        "source": "TikHub",
        "platform": platform,
        "platform_label": PLATFORMS[platform],
        "post_id": post_id,
        "comments": comments,
        "warning": warning,
    }
