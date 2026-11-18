"""Facebook 公开页面抓取：Playwright + 本机 Edge，无 API Key。"""

from __future__ import annotations

import hashlib
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


TAIPEI = timezone(timedelta(hours=8))
EDGE_PATHS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)
TIME_PATTERN = re.compile(
    r"(?:\d+\s*(?:分鐘|分钟|小時|小时|天|日)前|"
    r"(?:昨天|今天)|"
    r"\d{4}年\d{1,2}月\d{1,2}日)"
)


def _parse_cookies(raw: str) -> list[dict[str, Any]]:
    raw = str(raw or "").strip()
    if not raw:
        return []
    cookies: list[dict[str, Any]] = []
    for part in re.split(r"[;\n]+", raw):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": ".facebook.com",
                "path": "/",
            }
        )
    return cookies


def _number(raw: str) -> int:
    value = str(raw or "").strip().replace(",", "")
    multiplier = 1
    if value.endswith(("万", "萬", "w", "W")):
        multiplier = 10_000
        value = value[:-1]
    elif value.endswith(("k", "K")):
        multiplier = 1_000
        value = value[:-1]
    try:
        return max(0, int(float(value) * multiplier))
    except ValueError:
        return 0


def _parse_published(value: str) -> int:
    text = re.sub(r"\s+", "", str(value or ""))
    if not text:
        return 0
    now = datetime.now(TAIPEI)
    relative = re.search(
        r"(\d+)(分鐘|分钟|小時|小时|天|日)前",
        text,
    )
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        if unit in {"分鐘", "分钟"}:
            moment = now - timedelta(minutes=amount)
        elif unit in {"小時", "小时"}:
            moment = now - timedelta(hours=amount)
        else:
            moment = now - timedelta(days=amount)
        return int(moment.timestamp())
    if "昨天" in text:
        return int((now - timedelta(days=1)).timestamp())
    if "今天" in text:
        return int(now.timestamp())
    absolute = re.search(
        r"(\d{4})年(\d{1,2})月(\d{1,2})日"
        r"(?:星期[一二三四五六日天])?"
        r"(?:上午|下午|晚上)?"
        r"(\d{1,2}):(\d{2})?",
        text,
    )
    if absolute:
        year, month, day = map(int, absolute.group(1, 2, 3))
        hour = int(absolute.group(4) or 0)
        minute = int(absolute.group(5) or 0)
        if "下午" in text or "晚上" in text:
            if hour < 12:
                hour += 12
        try:
            return int(
                datetime(
                    year,
                    month,
                    day,
                    hour,
                    minute,
                    tzinfo=TAIPEI,
                ).timestamp()
            )
        except ValueError:
            return 0
    return 0


def _extract_body(text: str) -> str:
    lines = [
        re.sub(r"\u00a0", " ", line).strip()
        for line in str(text or "").splitlines()
    ]
    lines = [line for line in lines if line]
    body: list[str] = []
    started = False
    for line in lines:
        if line == "·":
            started = True
            continue
        if not started:
            continue
        if line.startswith("所有心情") or line in {
            "讚", "留言", "分享", "查看更多",
        }:
            break
        if TIME_PATTERN.search(line):
            continue
        if re.fullmatch(r"[\d\s,.]+", line):
            continue
        body.append(line)
    if not body:
        for line in lines:
            if (
                not TIME_PATTERN.search(line)
                and line not in {"·", "讚", "留言", "分享"}
                and not line.startswith("所有心情")
                and len(line) >= 12
            ):
                body.append(line)
    return (
        "\n".join(body)
        .replace("…… 查看更多", "")
        .replace("顯示較少", "")
        .replace("显示较少", "")
        .strip()
    )


def _extract_metrics(text: str) -> tuple[int, int, int]:
    tail = str(text or "")
    if "所有心情" in tail:
        tail = tail.split("所有心情", 1)[1]
    numbers = re.findall(r"\d[\d,.]*(?:[Kk萬万])?", tail)
    values = [_number(value) for value in numbers[:3]]
    while len(values) < 3:
        values.append(0)
    return values[0], values[1], values[2]


def _post_identity(url: str) -> tuple[str, str]:
    clean = str(url or "")
    if not clean:
        return "", ""
    match = (
        re.search(r"/posts/([^/?#]+)", clean)
        or re.search(r"/videos/(\d+)", clean)
        or re.search(r"/reel/(\d+)", clean)
        or re.search(r"[?&]story_fbid=(\d+)", clean)
    )
    post_id = match.group(1) if match else ""
    clean = clean.split("?__cft__", 1)[0].split("&__cft__", 1)[0]
    return post_id, clean


def fetch_facebook_posts(
    accounts: list[dict[str, Any]],
    *,
    since_ts: int,
    max_per_account: int,
    cookies: str,
    headless: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not accounts:
        return [], ["没有已核验的 Facebook 候选人页面"]
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], ["未安装 Playwright，请安装 requirements.txt"]

    executable = next((path for path in EDGE_PATHS if path.exists()), None)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    with sync_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": bool(headless),
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
            ],
        }
        if executable:
            launch_options["executable_path"] = str(executable)
        browser = playwright.chromium.launch(**launch_options)
        context = browser.new_context(
            locale="zh-TW",
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            ),
        )
        parsed_cookies = _parse_cookies(cookies)
        if parsed_cookies:
            context.add_cookies(parsed_cookies)
        page = context.new_page()
        for account in accounts:
            page_url = str(account.get("url") or "").strip()
            candidate = str(account.get("candidate") or "").strip()
            region = str(account.get("region") or "").strip()
            if not page_url:
                continue
            account_rows: list[dict[str, Any]] = []
            seen: set[str] = set()
            try:
                page.goto(
                    page_url,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
                page.wait_for_timeout(4500)
                page.keyboard.press("Escape")
                for _ in range(10):
                    page.mouse.wheel(0, 2300)
                    page.wait_for_timeout(1200)
                    if page.locator('div[role="article"]').count() >= max_per_account:
                        break
                articles = page.locator('div[role="article"]')
                for index in range(articles.count()):
                    article = articles.nth(index)
                    try:
                        text = article.inner_text(timeout=2500)
                    except Exception:
                        continue
                    if not text.strip():
                        continue
                    expand = article.get_by_text("查看更多", exact=False)
                    if expand.count():
                        try:
                            expand.first.click(timeout=1200)
                            page.wait_for_timeout(150)
                            text = article.inner_text(timeout=2500)
                        except Exception:
                            pass
                    body = ""
                    message = article.locator(
                        '[data-ad-preview="message"],'
                        '[data-ad-comet-preview="message"]'
                    )
                    if message.count():
                        try:
                            body = message.first.inner_text(timeout=2000).strip()
                        except Exception:
                            body = ""
                    if not body:
                        body = _extract_body(text)
                    body = (
                        body.replace("顯示較少", "")
                        .replace("显示较少", "")
                        .strip()
                    )
                    if not body:
                        continue
                    post_url = ""
                    time_label = ""
                    for link_index in range(article.locator("a").count()):
                        link = article.locator("a").nth(link_index)
                        href = str(link.get_attribute("href") or "")
                        label = str(
                            link.get_attribute("aria-label")
                            or link.inner_text(timeout=800)
                            or ""
                        )
                        if TIME_PATTERN.search(label):
                            time_label = label
                        if (
                            "/posts/" in href
                            or "/videos/" in href
                            or "/reel/" in href
                            or "story_fbid=" in href
                        ):
                            post_url = (
                                f"https://www.facebook.com{href}"
                                if href.startswith("/")
                                else href
                            )
                            if TIME_PATTERN.search(label):
                                break
                    if not time_label:
                        found = TIME_PATTERN.search(text)
                        if found:
                            time_label = found.group(0)
                    published = _parse_published(time_label)
                    if published and published < since_ts:
                        continue
                    post_id, post_url = _post_identity(post_url)
                    if not post_id:
                        digest = hashlib.sha1(
                            f"{candidate}|{body}|{time_label}".encode("utf-8")
                        ).hexdigest()[:20]
                        post_id = digest
                    if post_id in seen:
                        continue
                    seen.add(post_id)
                    likes, comments, shares = _extract_metrics(text)
                    item = {
                        "platform": "facebook",
                        "platform_label": "Facebook",
                        "id": post_id,
                        "text": body,
                        "url": post_url or page_url,
                        "author": str(
                            account.get("account_name") or candidate
                        ),
                        "published_ts": published or int(time.time()),
                        "published_at": (
                            datetime.fromtimestamp(
                                published,
                                tz=timezone.utc,
                            ).isoformat(timespec="seconds")
                            if published else ""
                        ),
                        "likes": likes,
                        "comments": comments,
                        "shares": shares,
                        "views": 0,
                        "image": "",
                        "raw_ref": post_id,
                        "region": region,
                        "candidate": candidate,
                        "official_account": True,
                        "account_username": str(
                            account.get("username") or ""
                        ),
                        "account_verification": str(
                            account.get("verification") or ""
                        ),
                    }
                    age_hours = max(
                        0.0,
                        (time.time() - item["published_ts"]) / 3600,
                    )
                    item["age_hours"] = round(age_hours, 1)
                    item["heat"] = round(
                        math.log1p(likes)
                        + 0.62 * math.log1p(comments)
                        + 0.45 * math.log1p(shares)
                        + 24.0 / (24.0 + age_hours),
                        3,
                    )
                    account_rows.append(item)
                    if len(account_rows) >= max_per_account:
                        break
            except PlaywrightTimeout:
                warnings.append(f"{candidate}：Facebook页面加载超时")
            except Exception as exc:  # noqa: BLE001
                warnings.append(
                    f"{candidate}：Facebook读取失败（{type(exc).__name__}）"
                )
            if not account_rows:
                suffix = (
                    "；该页面可能需要 FACEBOOK_COOKIES"
                    if not cookies
                    else ""
                )
                warnings.append(f"{candidate}：未读取到公开帖文{suffix}")
            rows.extend(account_rows)
        browser.close()
    rows.sort(key=lambda row: int(row.get("published_ts") or 0), reverse=True)
    return rows, warnings
