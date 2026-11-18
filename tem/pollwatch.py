"""民調時效：從最新新聞標題抽取當週民調數字。

為什麼需要這個：維基百科的民調彙整表是人工維護的，通常落後一到兩週甚至幾個月
（V2 時多數縣市最新一筆還停在 1~5 月）。但投票日前六週正是民調最密集的時候，
漏掉這一週等於放棄最關鍵的訊號。

作法：用 Google News RSS 的 `when:` 時間窗，逐縣市查「<縣市>長 民調」，
再從標題抽出「候選人 + 支持度」配對。標題雖然短，但台灣媒體的民調新聞
幾乎一定會把關鍵數字寫進標題（例如「蘇巧慧43.1%領先李四川 差距13.1%」）。

抽取規則刻意保守，寧可漏、不要錯：
  1. 先把「領先/贏/輸/差距 + 數字」這種**差距**片段挖掉，避免把差距誤當支持度。
  2. 再把「候選人 + 數字」配對；候選人與數字之間若出現領先/勝/輸等字樣則丟棄。
  3. 至少要有兩位候選人的支持度、且加總不超過 105%，才收錄。

抽出來的民調標記為 `tier="media"`，權重低於民調機構自己公布的完整表格
（`tier="institute"`）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Iterable

from .http import fetch_text
from .paths import CONFIG_DIR

GNEWS = "https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
UA = {"User-Agent": "TEM-election-monitor/1.0 (local research tool; python-urllib)"}
MEDIA_TIER_WEIGHT = 0.75

# 標題裡常見的民調執行單位；抓不到就用媒體名
POLLSTERS = (
    "戴立安", "信民協會", "TVBS", "匯流", "美麗島", "ETtoday", "聯合報", "中時",
    "年代", "台灣民意基金會", "QuickseeK", "世新", "政大", "中華亞太菁英",
    "台灣指標", "皮爾森", "山水", "趨勢", "艾普羅", "全國公信力", "典通",
)

_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TITLE = re.compile(r"<title>(.*?)</title>", re.S)
_DATE = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)
_SOURCE = re.compile(r"<source[^>]*>(.*?)</source>", re.S)
_LINK = re.compile(r"<link>(.*?)</link>", re.S)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)

# 「8.3個百分點」通常是明確差距；即使中間夾了候選人姓名也應辨識。
_GAP_POINTS = re.compile(
    r"(?:領先|超前|贏|勝|輸|落後|差距|差|拉開|擴大|縮小|緊追|紧追|不到|只差|僅差|仅差|輾壓|碾压)[^\d]{0,12}"
    r"(\d{1,3}(?:\.\d)?)\s*(?:個百分點|百分點|個點|點)"
)
# 「差距9.2%」的百分號在語意上就是百分點，但只接受明確寫「差距／相差」。
_GAP_PERCENT = re.compile(
    r"(?:差距|相差)\s*(\d{1,3}(?:\.\d)?)\s*[%％]"
)
# 「領先童子瑋4.6%」可能是差距，也可能是該候選人支持度；稍後用數值合理性判別。
_GAP_AMBIGUOUS = re.compile(
    r"(?:領先|超前|贏|勝|輸|落後|拉開|擴大|縮小|緊追|紧追|不到|只差|僅差|仅差|輾壓|碾压)[^\d]{0,12}"
    r"(\d{1,3}(?:\.\d)?)\s*[%％]"
)
_VALUE_AFTER = r"[^\d]{0,8}(\d{1,3}(?:\.\d)?)\s*(?:%|％|個百分點|百分點|個點)"
_BAD_BETWEEN = ("領先", "超前", "贏", "勝", "輸", "落後", "差距", "差", "拉開", "擴大", "縮小", "誤差", "範圍")


def normalize(text: str) -> str:
    """全形轉半形、去多餘空白，讓後續比對單純一點。"""
    table = str.maketrans({
        "％": "%", "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
        "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
    })
    text = (text or "").translate(table)
    return re.sub(r"\s+", "", text)


def candidate_index() -> dict[str, set[str]]:
    """縣市 → 候選人姓名集合（名冊 + Polymarket 市場）。"""
    from .roster import load_roster

    out: dict[str, set[str]] = {}
    for region, entries in (load_roster().get("regions") or {}).items():
        names = {e.get("name") for e in entries if e.get("name")}
        if names:
            out.setdefault(region, set()).update(names)
    try:
        from . import db

        for row in db.fetch_latest_odds():
            if row["region_name"] and row["candidate_zh"]:
                out.setdefault(row["region_name"], set()).add(row["candidate_zh"])
    except Exception:  # noqa: BLE001
        pass
    return out


def extract_poll(title: str, index: dict[str, set[str]]) -> dict[str, Any] | None:
    """從單一標題抽出民調；抽不出可用數字就回傳 None。"""
    text = normalize(title)
    looks_like_poll = any(k in text for k in ("民調", "支持度", "選情"))
    if (
        any(
            word in text
            for word in (
                "看好度", "看好", "滿意度", "满意度", "執政滿意",
                "执政满意", "城市治理", "辯論表現", "辩论表现",
                "可能當選", "可能当选", "當選機會", "当选机会",
                "AI民調", "AI民调", "AI模擬", "AI模拟",
            )
        )
        and not any(word in text for word in ("支持度", "支持率"))
    ):
        return None
    names = {name for group in index.values() for name in group if len(name or "") >= 2}
    # 1) 先抓「候選人 + 支持度」配對（數字前若出現差距字樣就不是支持度）
    found: dict[str, float] = {}
    for name in names:
        pos = 0
        while True:
            i = text.find(name, pos)
            if i < 0:
                break
            pos = i + len(name)
            m = re.match(_VALUE_AFTER, text[i + len(name):])
            if not m:
                continue
            between = text[i + len(name): i + len(name) + m.start(1)]
            if any(bad in between for bad in _BAD_BETWEEN):
                continue
            value = float(m.group(1))
            if 0 < value <= 100:
                found.setdefault(name, value)

    # 2) 差距片段（可能是「領先/贏/差距 + 數字」）
    gap = None
    gap_pos = -1
    gm = _GAP_POINTS.search(text) or _GAP_PERCENT.search(text)
    if gm is None:
        ambiguous = _GAP_AMBIGUOUS.search(text)
        if ambiguous and len(found) >= 2:
            ambiguous_value = float(ambiguous.group(1))
            echoed_name = next(
                (name for name, value in found.items() if abs(value - ambiguous_value) < 0.05),
                "",
            )
            leader_value = max(
                (value for name, value in found.items() if name != echoed_name),
                default=max(found.values()),
            )
            # 若把该数字当支持度，总和明显高于当差距反推后的总和，则保留为支持度。
            support_total = leader_value + ambiguous_value
            gap_total = leader_value + max(0.0, leader_value - ambiguous_value)
            gm = ambiguous if gap_total > support_total else None
        elif ambiguous and len(found) < 2:
            gm = ambiguous
    if gm:
        gap = float(gm.group(1))
        gap_pos = gm.start()
    if not looks_like_poll and gap is None:
        return None

    mentioned = sorted((n for n in names if n in text), key=lambda n: text.find(n))

    # 3) 若某位候選人的「支持度」正好等於標題陳述的差距，那其實是差距不是支持度。
    #    例如「謝國樑支持度42.9%領先童子瑋4.6%」：4.6 是差距，童子瑋實際約 38.3%。
    if gap is not None and len(found) >= 2:
        echoed = [n for n, v in found.items() if abs(v - gap) < 0.05]
        if echoed:
            for name in echoed:
                found.pop(name, None)
            if len(found) == 1:
                leader, value = max(found.items(), key=lambda kv: kv[1])
                others = [n for n in mentioned if n != leader]
                if others and value - gap > 0:
                    found[others[0]] = round(value - gap, 1)

    # 只有一位候選人被抓到支持度、但標題給了差距 → 用「領先者支持度 - 差距」補出對手
    elif len(found) == 1 and gap:
        leader, value = next(iter(found.items()))
        if 0 < gap < value:
            others = [n for n in mentioned if n != leader]
            if others:
                runner = others[0]
                found[runner] = round(value - gap, 1)

    # 完全沒有支持度、但有點名兩位候選人 + 差距 → 只記差距（後續用校準曲線換勝率）
    if len(found) < 2 and gap and len(mentioned) >= 2 and gap_pos >= 0:
        before = [n for n in mentioned if text.find(n) < gap_pos]
        after = [n for n in mentioned if text.find(n) > gap_pos]
        if before and after and before[0] != after[0]:
            return {
                "region": _pick_region(before[0], index, text),
                "values": {},
                "lead": before[0],
                "chase": after[0],
                "gap": gap,
                "pollster": next((p for p in POLLSTERS if p.lower() in text.lower()), ""),
            }

    if len(found) < 2 or sum(found.values()) > 105:
        return None

    # 有雙邊支持度時，差距直接由支持度回推，比標題裡的「差距」字樣可靠
    ranked = sorted(found.items(), key=lambda kv: -kv[1])
    computed_gap = round(ranked[0][1] - ranked[1][1], 1)

    regions = {region for region, group in index.items() if group & set(found)}
    if len(regions) != 1:
        # 多個縣市的候選人同時出現時，必須靠標題點名縣市才敢歸屬
        hint = {r for r in regions if r.replace("臺", "台") in text.replace("臺", "台")}
        if len(hint) != 1:
            return None
        regions = hint
    region = next(iter(regions))

    pollster = next((p for p in POLLSTERS if p.lower() in text.lower()), "")
    return {
        "region": region,
        "values": found,
        "matchup": tuple(sorted(found)),
        "pollster": pollster,
        "gap": computed_gap,
    }


def _pick_region(candidate: str, index: dict[str, set[str]], text: str) -> str:
    hits = [region for region, group in index.items() if candidate in group]
    if len(hits) == 1:
        return hits[0]
    named = [r for r in hits if r.replace("臺", "台") in text.replace("臺", "台")]
    return (named or hits or [""])[0]


def fetch_titles(query: str, *, timeout: int = 30) -> list[dict[str, Any]]:
    url = GNEWS.format(q=urllib.parse.quote(query))
    xml = fetch_text(url, timeout=timeout, retries=2, headers=UA)
    out: list[dict[str, Any]] = []
    for block in _ITEM.findall(xml):
        def pick(pattern) -> str:
            m = pattern.search(block)
            if not m:
                return ""
            value = m.group(1)
            inner = _CDATA.search(value)
            return (inner.group(1) if inner else value).strip()

        title = pick(_TITLE)
        if not title:
            continue
        published = pick(_DATE)
        try:
            when = datetime.strptime(published, "%a, %d %b %Y %H:%M:%S %Z").replace(
                tzinfo=timezone.utc
            ).astimezone().date().isoformat()
        except ValueError:
            when = ""
        out.append({
            "title": title,
            "date": when,
            "source": re.sub(r"<[^>]+>", "", pick(_SOURCE)) or "",
            "url": pick(_LINK),
        })
    return out


def queries_for(regions: Iterable[str], *, days: int = 14) -> list[str]:
    out = [f"縣市長 民調 when:{days}d", f"九合一 民調 when:{days}d"]
    for region in regions:
        base = region.replace("臺", "台")
        out.append(f"{base}長 民調 when:{days}d")
    return out


def collect_media_polls(
    settings,
    *,
    days: int = 14,
    delay: float = 0.3,
    per_query: int = 100,
) -> dict[str, Any]:
    """逐查詢抓 Google News，抽民調，回傳可直接併入民調訊號的資料。"""
    index = candidate_index()
    regions = [r.name for r in settings.regions]
    seen: set[tuple[str, str, str]] = set()
    polls: list[dict[str, Any]] = []
    stats = {"queries": 0, "items": 0, "extracted": 0, "skipped": 0}

    for query in queries_for(regions, days=days):
        stats["queries"] += 1
        try:
            items = fetch_titles(query)
        except Exception:  # noqa: BLE001 - 單一查詢失敗不影響其他
            time.sleep(delay)
            continue
        stats["items"] += len(items)
        for item in items[:per_query]:
            parsed = extract_poll(item["title"], index)
            if not parsed:
                stats["skipped"] += 1
                continue
            headline = normalize(item["title"])[:40]
            key = (parsed["region"], item["date"], headline)
            if key in seen:
                continue
            seen.add(key)
            stats["extracted"] += 1
            polls.append({
                "region": parsed["region"],
                "source": parsed["pollster"] or item["source"] or "媒體報導",
                "media": item["source"],
                "date": item["date"],
                "values": parsed.get("values") or {},
                "lead": parsed.get("lead"),
                "chase": parsed.get("chase"),
                "matchup": list(parsed.get("matchup") or ()),
                "gap": parsed["gap"],
                "title": item["title"][:120],
                "url": item["url"],
                "kind": "matchup",
                "tier": "media",
            })
        time.sleep(delay)
    polls.sort(key=lambda p: (p["region"], p["date"]))
    return {
        "meta": {
            "note": "從新聞標題抽取的當週民調（tier=media，權重低於機構公布表）",
            "window_days": days,
            "updated_at": time.strftime("%Y-%m-%d"),
            "stats": stats,
        },
        "polls": polls,
    }


def save_media_polls(data: dict[str, Any], path=None) -> str:
    target = path or (CONFIG_DIR / "polls_media.json")
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


_CACHE: dict[str, Any] = {}


def load_media_polls(path=None) -> dict[str, Any]:
    target = path or (CONFIG_DIR / "polls_media.json")
    if not target.exists():
        return {"polls": []}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"polls": []}
    index = candidate_index()
    sanitized: list[dict[str, Any]] = []
    for item in data.get("polls") or []:
        parsed = extract_poll(str(item.get("title") or ""), index)
        if not parsed:
            continue
        values = dict(parsed.get("values") or {})
        if len(values) < 2 or sum(values.values()) > 105:
            continue
        if len({round(float(value), 1) for value in values.values()}) < 2:
            continue
        sanitized.append(
            {
                **item,
                "region": parsed.get("region") or item.get("region") or "",
                "source": parsed.get("pollster") or item.get("source") or "",
                "values": values,
                "matchup": list(parsed.get("matchup") or values.keys()),
                "lead": parsed.get("lead") or "",
                "chase": parsed.get("chase") or "",
                "gap": parsed.get("gap"),
            }
        )
    return {"meta": data.get("meta") or {}, "polls": sanitized}


def media_polls_for(region_name: str) -> list[dict[str, Any]]:
    """取某縣市的新聞民調，並轉成 polls.summarise 吃的格式。"""
    if "data" not in _CACHE:
        _CACHE["data"] = load_media_polls()
    from .zhconv import to_simplified

    want = to_simplified(region_name or "").replace("臺", "台")
    out: list[dict[str, Any]] = []
    for item in (_CACHE["data"] or {}).get("polls") or []:
        if to_simplified(item.get("region") or "").replace("臺", "台") != want:
            continue
        matchup = list(item.get("matchup") or [])
        if len(matchup) < 2 and item.get("lead") and item.get("chase"):
            matchup = sorted([item["lead"], item["chase"]])
        out.append({
            "source": item.get("source") or "",
            "date": item.get("date") or "",
            "values": dict(item.get("values") or {}),
            "matchup": tuple(matchup),
            "kind": "matchup",
            "tier": "media",
            "gap": item.get("gap"),
            "lead": item.get("lead"),
            "title": item.get("title") or "",
        })
    return out
