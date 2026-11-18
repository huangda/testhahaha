"""極簡 HTTP 客戶端（僅標準庫，含重試與 UA）。"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body


def _decode(raw: bytes, encoding: str | None) -> bytes:
    if encoding in (None, "identity"):
        return raw
    if encoding == "gzip":
        try:
            return gzip.decompress(raw)
        except OSError:
            return raw
    if encoding == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
            except zlib.error:
                return raw
    return raw


def fetch_bytes(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    headers: dict[str, str] | None = None,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": DEFAULT_UA, "Accept-Encoding": "gzip, deflate", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return _decode(raw, resp.headers.get("Content-Encoding"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read()[:400].decode("utf-8", "replace")
            except Exception:  # pragma: no cover - 讀取失敗不影響流程
                pass
            if exc.code in (400, 404, 422):
                raise HttpError(exc.code, url, body) from exc
            if exc.code == 429:
                # 被限流：退避久一點再試（維基媒體對匿名請求相當敏感）
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait = float(retry_after) if (retry_after or "").replace(".", "", 1).isdigit() else 4.0 * (attempt + 1)
                time.sleep(min(wait, 20.0))
            last_error = HttpError(exc.code, url, body)
        except Exception as exc:  # noqa: BLE001 - 網路層任何錯誤都重試
            last_error = exc
        if attempt < retries - 1:
            time.sleep(0.8 * (2**attempt) + random.random() * 0.3)
    raise RuntimeError(f"請求失敗：{url}（{last_error}）")


def fetch_text(url: str, **kwargs: Any) -> str:
    return fetch_bytes(url, **kwargs).decode("utf-8", "replace")


def fetch_json(url: str, **kwargs: Any) -> Any:
    return json.loads(fetch_bytes(url, **kwargs).decode("utf-8", "replace"))


def encode_query(params: dict[str, Any]) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 60,
    retries: int = 2,
) -> Any:
    data = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "User-Agent": DEFAULT_UA,
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = _decode(resp.read(), resp.headers.get("Content-Encoding"))
                return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read()[:600].decode("utf-8", "replace")
            except Exception:  # pragma: no cover
                pass
            last_error = HttpError(exc.code, url, body)
            if exc.code in (400, 401, 403, 404, 422):
                raise last_error
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        if attempt < retries - 1:
            time.sleep(1.0 * (2**attempt))
    raise RuntimeError(f"POST 失敗：{url}（{last_error}）")
