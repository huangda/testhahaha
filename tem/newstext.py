"""新闻正文抓取、清洗与本地缓存。"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .http import fetch_bytes
from .paths import DATA_DIR

CACHE_DIR = DATA_DIR / "cache" / "news_bodies"
WHITESPACE = re.compile(r"[ \t\u3000]+")
BLANK_LINES = re.compile(r"\n{3,}")


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
    return raw.decode("utf-8", "ignore")


def _clean_block(raw: str) -> str:
    raw = re.sub(r"<!--.*?-->|<script\b.*?</script>|<style\b.*?</style>|<noscript\b.*?</noscript>",
                 "", raw, flags=re.I | re.S)
    raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    raw = re.sub(r"</(?:p|div|li|h\d|section|article|blockquote|tr)>", "\n", raw, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", raw))
    lines = []
    for line in text.splitlines():
        line = WHITESPACE.sub(" ", line).strip()
        if not line:
            continue
        if line in ("责任编辑", "責任編輯", "分享", "广告", "廣告"):
            continue
        lines.append(line)
    return BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def extract_body(raw_html: str) -> str:
    """优先抽取 article/main/正文容器，退回清理后的完整页面正文。"""
    candidates: list[str] = []
    patterns = (
        r"<article\b[^>]*>(.*?)</article>",
        r"<main\b[^>]*>(.*?)</main>",
        r'<div\b[^>]*class=["\'][^"\']*(?:article|content|post|story|body|entry)[^"\']*["\'][^>]*>(.*?)</div>',
        r"<section\b[^>]*>(.*?)</section>",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, raw_html, re.I | re.S):
            text = _clean_block(match.group(1))
            if len(text) >= 120:
                candidates.append(text)
    if candidates:
        return max(candidates, key=len)[:40000].strip()
    body = re.search(r"<body\b[^>]*>(.*?)</body>", raw_html, re.I | re.S)
    return _clean_block(body.group(1) if body else raw_html)[:40000].strip()


def _cache_paths(url: str) -> tuple[Path, Path]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.txt", CACHE_DIR / f"{digest}.json"


def body_for(
    url: str,
    fallback: str = "",
    *,
    cache_days: int = 90,
    timeout: int = 10,
    retries: int = 2,
) -> tuple[str, str]:
    """返回 (正文, 状态)。状态为 full/short/rss-summary/error。"""
    if not url or "news.google.com/rss/articles/" in url:
        return fallback, "rss-summary"
    text_path, meta_path = _cache_paths(url)
    if text_path.exists() and meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            age = (time.time() - meta_path.stat().st_mtime) / 86400
            max_age = 0.25 if meta.get("status") == "error-fallback" else cache_days
            if age <= max_age:
                return text_path.read_text(encoding="utf-8"), meta.get("status", "full")
        except (OSError, json.JSONDecodeError):
            pass
    try:
        raw = fetch_bytes(
            url,
            timeout=timeout,
            retries=retries,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
        text = extract_body(_decode(raw))
        if len(text) >= 240:
            status = "full"
        elif len(text) >= 80:
            status = "short"
        else:
            return fallback, "rss-summary"
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        text_path.write_text(text, encoding="utf-8")
        meta_path.write_text(
            json.dumps({"url": url, "status": status, "chars": len(text)}, ensure_ascii=False),
            encoding="utf-8",
        )
        return text, status
    except Exception:  # noqa: BLE001 - 批量导出时单篇失败使用 RSS 摘要
        if fallback:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            text_path.write_text(fallback, encoding="utf-8")
            meta_path.write_text(
                json.dumps(
                    {"url": url, "status": "error-fallback", "chars": len(fallback)},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        return fallback, "error-fallback" if fallback else "error"


def bodies_for(
    rows: list[dict[str, Any]],
    *,
    workers: int = 6,
    timeout: int = 10,
    retries: int = 2,
) -> list[tuple[str, str]]:
    """并发抓取一页/一批新闻正文，返回与输入顺序一致的结果。"""
    if not rows:
        return []
    results: list[tuple[str, str] | None] = [None] * len(rows)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 10))) as pool:
        future_map = {
            pool.submit(
                body_for,
                row.get("url") or "",
                row.get("summary") or "",
                timeout=timeout,
                retries=retries,
            ): index
            for index, row in enumerate(rows)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                results[index] = future.result()
            except Exception:  # noqa: BLE001
                results[index] = ((rows[index].get("summary") or ""), "error")
    return [item or ("", "error") for item in results]
