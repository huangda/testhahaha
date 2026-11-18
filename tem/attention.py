"""注意力數據：PTT 看板聲量 + 維基百科瀏覽量。

兩個免費、免金鑰、與新聞不同性質的領先訊號：

  1. **PTT**（`ptt.cc`）：政黑板搜尋結果帶「推文數」（爆 / 數字 / X1…），
     可以同時當聲量與粗略輿情方向。標題比新聞更口語、更快。
  2. **維基百科每日瀏覽量**（Wikimedia REST）：某人條目被查看的次數，
     是「主動搜尋關注度」的代理指標，可以細到單日、單一候選人。

兩者都以「候選人」為實體聚合，寫入 `attention` 表；relative 指標
（momentum = 近 7 日均 / 前 30 日均）比絕對量更能反映事件衝擊。
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from .http import fetch_json, fetch_text

PTT_BASE = "https://www.ptt.cc"
PTT_BOARD = "HatePolitics"
PAGEVIEWS = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "zh.wikipedia/all-access/user/{title}/daily/{start}/{end}"
)
UA = {"User-Agent": "TEM-election-monitor/1.0 (local research tool; python-urllib)"}
PTT_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36",
    "Cookie": "over18=1",
}

_TITLE_RE = re.compile(r'<div class="title">\s*<a href="([^"]+)">([^<]*)</a>')
_NREC_RE = re.compile(r'<div class="nrec"><span class="hl f\d">([^<]*)</span>')
_DATE_RE = re.compile(r'<div class="date">([^<]*)</div>')


def nrec_score(text: str) -> int:
    """PTT 推文數 → 分數。爆=100，X1..X9 = -10×n，數字直接計分。"""
    text = (text or "").strip()
    if not text:
        return 0
    if text.startswith("爆"):
        return 100
    if text.upper().startswith("X"):
        m = re.search(r"X(\d+)", text.upper())
        return -10 * int(m.group(1)) if m else -10
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else 0


def parse_ptt_listing(html: str) -> list[dict[str, Any]]:
    titles = _TITLE_RE.findall(html)
    nrecs = _NREC_RE.findall(html)
    dates = _DATE_RE.findall(html)
    out: list[dict[str, Any]] = []
    for idx, (href, title) in enumerate(titles):
        nrec = nrecs[idx] if idx < len(nrecs) else ""
        out.append({
            "url": PTT_BASE + href,
            "title": title.strip(),
            "nrec": nrec.strip(),
            "score": nrec_score(nrec),
            "date": (dates[idx].strip() if idx < len(dates) else ""),
        })
    return out


def ptt_search(name: str, *, board: str = PTT_BOARD, pages: int = 2, delay: float = 0.6) -> list[dict[str, Any]]:
    """在 PTT 看板搜尋某關鍵字，回傳最近的貼文清單。"""
    import urllib.parse

    out: list[dict[str, Any]] = []
    url = f"{PTT_BASE}/bbs/{board}/search?q={urllib.parse.quote(name)}"
    for _ in range(max(1, pages)):
        try:
            html = fetch_text(url, timeout=12, retries=2, headers=PTT_UA)
        except Exception:  # noqa: BLE001 - 單頁失敗不影響整體
            break
        items = parse_ptt_listing(html)
        for item in items:
            if name in item["title"]:
                item["matched"] = name
                out.append(item)
        prev = re.search(r'<a class="btn wide" href="([^"]+)">‹ 上頁</a>', html)
        if not prev:
            break
        url = PTT_BASE + prev.group(1).replace("&amp;", "&")
        time.sleep(delay)
    return out


def ptt_board_recent(*, board: str = PTT_BOARD, pages: int = 6, delay: float = 0.6) -> list[dict[str, Any]]:
    """抓看板最近幾頁，作為整體討論熱度。"""
    out: list[dict[str, Any]] = []
    url = f"{PTT_BASE}/bbs/{board}/index.html"
    for _ in range(max(1, pages)):
        try:
            html = fetch_text(url, timeout=12, retries=2, headers=PTT_UA)
        except Exception:  # noqa: BLE001
            break
        out.extend(parse_ptt_listing(html))
        prev = re.search(r'<a class="btn wide" href="([^"]+)">‹ 上頁</a>', html)
        if not prev:
            break
        url = PTT_BASE + prev.group(1).replace("&amp;", "&")
        time.sleep(delay)
    return out


def pageviews(title: str, *, days: int = 60) -> list[dict[str, Any]]:
    """維基百科每日瀏覽量。"""
    import urllib.parse

    end = date.today()
    start = end - timedelta(days=days)
    url = PAGEVIEWS.format(
        title=urllib.parse.quote(title.replace(" ", "_")),
        start=start.strftime("%Y%m%d"),
        end=end.strftime("%Y%m%d"),
    )
    data = fetch_json(url, timeout=30, retries=2, headers=UA)
    out: list[dict[str, Any]] = []
    for item in data.get("items") or []:
        stamp = item.get("timestamp") or ""
        out.append({
            "date": f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}",
            "views": int(item.get("views") or 0),
        })
    return out


def pageview_metrics(series: list[dict[str, Any]]) -> dict[str, Any]:
    """近 7 日均、近 30 日均與 momentum（7 日均 / 前 30 日均）。"""
    if not series:
        return {}
    views = [p["views"] for p in series]
    recent = views[-7:]
    base = views[-37:-7] or views[:-7] or views
    avg7 = sum(recent) / len(recent)
    avg30 = sum(base) / len(base) if base else 0.0
    peak = max(views)
    peak_date = series[views.index(peak)]["date"]
    return {
        "views_7d": round(avg7, 1),
        "views_30d": round(avg30, 1),
        "views_total": sum(views),
        "momentum": round(avg7 / avg30, 3) if avg30 else None,
        "peak": peak,
        "peak_date": peak_date,
    }


def collect_attention(
    settings,
    *,
    regions: Iterable[str] | None = None,
    board: str = PTT_BOARD,
    ptt_pages: int = 2,
    view_days: int = 60,
    delay: float = 0.35,
    limit_per_region: int = 4,
) -> dict[str, Any]:
    """抓取 PTT 聲量與維基瀏覽量，回傳可直接寫庫的列。"""
    from .profile import get_profile
    from .roster import load_roster

    roster = load_roster().get("regions") or {}
    wanted = set(regions or roster.keys())

    # 優先抓「市場呼聲高」的候選人，名冊順序只是備援
    ranked: dict[str, list[str]] = {}
    try:
        from . import db

        for row in db.fetch_latest_odds():
            code = row["region_name"] or row["region_code"]
            name = row["candidate_zh"]
            if not code or not name:
                continue
            names = ranked.setdefault(code, [])
            if name not in names:
                names.append(name)
    except Exception:  # noqa: BLE001
        ranked = {}

    rows: list[dict[str, Any]] = []
    board_items = ptt_board_recent(board=board, pages=ptt_pages)
    board_score = sum(i["score"] for i in board_items)
    rows.append({
        "entity": f"board:{board}",
        "entity_type": "board",
        "region_code": None,
        "source": "ptt",
        "mentions": len(board_items),
        "score": board_score,
        "extra": {"hot": sorted(board_items, key=lambda i: -i["score"])[:8]},
    })
    time.sleep(delay)

    for region, entries in roster.items():
        if region not in wanted:
            continue
        names: list[str] = list(ranked.get(region) or [])
        for entry in entries:
            name = (entry.get("name") or entry.get("zh") or "").strip()
            if name and name not in names:
                names.append(name)
        # 只取前幾個以免請求過多
        for name in names[:limit_per_region]:
            try:
                profile = get_profile(name, allow_network=False) or {}
            except Exception:  # noqa: BLE001
                profile = {}
            posts = ptt_search(name, board=board, pages=ptt_pages, delay=delay)
            pos = sum(1 for p in posts if p["score"] > 0)
            neg = sum(1 for p in posts if p["score"] < 0)
            rows.append({
                "entity": name,
                "entity_type": "candidate",
                "region_code": None,
                "source": "ptt",
                "mentions": len(posts),
                "score": sum(p["score"] for p in posts),
                "extra": {
                    "positive": pos, "negative": neg,
                    "hot": sorted(posts, key=lambda p: -p["score"])[:5],
                },
            })
            title = (profile or {}).get("title") or name
            try:
                series = pageviews(title, days=view_days)
            except Exception:  # noqa: BLE001 - 沒有條目或限流就略過
                series = []
            metrics = pageview_metrics(series)
            if metrics:
                rows.append({
                    "entity": name,
                    "entity_type": "candidate",
                    "region_code": None,
                    "source": "wikipedia",
                    "mentions": None,
                    "score": None,
                    "views_7d": metrics["views_7d"],
                    "views_30d": metrics["views_30d"],
                    "momentum": metrics["momentum"],
                    "extra": {"title": title, "peak": metrics["peak"], "peak_date": metrics["peak_date"],
                              "views_total": metrics["views_total"],
                              "series": series[-30:]},
                })
            time.sleep(delay)
    return {"rows": rows, "board_items": board_items, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
