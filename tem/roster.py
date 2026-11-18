"""候選人登記名冊：從維基百科條目解析 22 縣市長參選人清單。

來源條目：`2026年中華民國地方公職人員選舉` 的「直轄市、縣市長」段落，
內含兩張 wikitable（直轄市長 / 縣市長），每列為：
縣市、抽籤號次、參選人、政黨、經歷。

這份名冊解決兩件事：
  1. 補齊 Polymarket 英文拼音名 → 中文名的對照（透過 Wikidata 英文標籤比對）。
  2. 提供完整正式參選人、抽籤號次、黨籍與簡短經歷，作為頁面與報告的可靠底稿。

輸出 `config/roster.json`。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any

from .http import fetch_json
from .paths import CONFIG_DIR
from .zhconv import to_simplified

WIKI_API = "https://zh.wikipedia.org/w/api.php"
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
PAGE = "2026年中華民國地方公職人員選舉"
UA = {"User-Agent": "TEM-election-monitor/1.0 (local research tool; python-urllib)"}

_REGION_HEAD = re.compile(r'^!\s*(?:rowspan="\d+"\s*\|\s*)?(.+?)\s*$')
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_REF = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.S)
_NUMBER_CELL = re.compile(r'^\|\s*(?:align="center"\s*\|\s*)?(\d+)\s*$')


def _clean(text: str) -> str:
    text = _REF.sub("", text)
    text = _LINK.sub(lambda m: (m.group(2) or m.group(1)), text)
    text = text.replace("'''", "").replace("''", "")
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip().strip("|").strip()


def fetch_wikitext(page: str = PAGE) -> str:
    params = {
        "action": "parse",
        "page": page,
        "prop": "wikitext",
        "format": "json",
        "formatversion": "2",
        "redirects": "1",
    }
    data = fetch_json(f"{WIKI_API}?{urllib.parse.urlencode(params)}", timeout=40, retries=2, headers=UA)
    return ((data.get("parse") or {}).get("wikitext")) or ""


def parse_roster(wikitext: str) -> dict[str, list[dict[str, Any]]]:
    """回傳 {縣市: [{number, name, party, experience}, ...]}。"""
    # 只看「直轄市、縣市長」這一節，避免把選舉日程表、政黨表也算進來
    start = wikitext.find("直轄市、縣市長")
    if start >= 0:
        end = wikitext.find("直轄市、縣市議員", start + 1)
        wikitext = wikitext[start:end if end > start else len(wikitext)]

    regions: dict[str, list[dict[str, Any]]] = {}
    current: str | None = None
    pending_number: int | None = None

    for raw in wikitext.splitlines():
        line = raw.strip()
        if not line or line.startswith("|+") or line.startswith("{|") or line == "|}":
            continue
        if line == "|-" or line.startswith("!"):
            if line.startswith("!"):
                # 可能是「表頭」也可能是「縣市列」
                head = line[1:].strip()
                if head.startswith(("直轄市", "縣市", "參選人", "政黨", "經歷")):
                    continue
                name = _clean(_REGION_HEAD.sub(r"\1", line))
                if name and 2 <= len(name) <= 5 and name.endswith(("市", "縣")) and "||" not in name:
                    current = name
                    pending_number = None
            continue
        if line.startswith("|") and current:
            number_match = _NUMBER_CELL.match(line)
            if number_match:
                pending_number = int(number_match.group(1))
                continue
            cells = [_clean(c) for c in line[1:].split("||")]
            while len(cells) < 3:
                cells.append("")
            cand, party, experience = cells[0], cells[1], cells[2]
            if not cand:
                continue
            regions.setdefault(current, []).append(
                {
                    "number": pending_number,
                    "name": cand,
                    "party": party,
                    "experience": experience,
                }
            )
            pending_number = None

    if len(regions) != 22:
        raise ValueError(f"候選人名冊應有 22 個縣市，實際解析到 {len(regions)} 個")
    for region, items in regions.items():
        numbers = [item.get("number") for item in items]
        if numbers != list(range(1, len(items) + 1)):
            raise ValueError(f"{region} 的候選人號次不連續：{numbers}")
    return regions


def _wikibase_ids(titles: list[str]) -> dict[str, str]:
    """批次取 zh.wikipedia 條目對應的 Wikidata Q id。"""
    out: dict[str, str] = {}
    for i in range(0, len(titles), 20):
        chunk = titles[i:i + 20]
        params = urllib.parse.urlencode(
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "prop": "pageprops",
                "titles": "|".join(chunk),
                "redirects": "1",
            }
        )
        try:
            data = fetch_json(f"{WIKI_API}?{params}", timeout=30, retries=2, headers=UA)
        except Exception:  # noqa: BLE001
            continue
        query = data.get("query") or {}
        pages = {p.get("title"): p for p in (query.get("pages") or [])}
        out.update(
            {
                title: (page.get("pageprops") or {}).get("wikibase_item")
                for title, page in pages.items()
                if (page.get("pageprops") or {}).get("wikibase_item")
            }
        )
        time.sleep(1.0)
    return out


def _english_labels(qids: list[str]) -> dict[str, str]:
    """批次取 Wikidata 的英文標籤，用來對上 Polymarket 的拼音名。"""
    out: dict[str, str] = {}
    for i in range(0, len(qids), 40):
        chunk = qids[i:i + 40]
        params = urllib.parse.urlencode(
            {
                "action": "wbgetentities",
                "ids": "|".join(chunk),
                "props": "labels",
                "languages": "en",
                "format": "json",
            }
        )
        try:
            data = fetch_json(f"{WIKIDATA_API}?{params}", timeout=30, retries=2, headers=UA)
        except Exception:  # noqa: BLE001
            continue
        for qid, entity in (data.get("entities") or {}).items():
            label = ((entity.get("labels") or {}).get("en") or {}).get("value")
            if label:
                out[qid] = label
        time.sleep(1.0)
    return out


def _norm(name: str) -> str:
    return re.sub(r"[\s\-'’·.]", "", (name or "").lower())


def build_roster(*, resolve_english: bool = True) -> dict[str, Any]:
    """抓取並解析名冊，盡量補上 Polymarket 的英文拼音名。"""
    regions = parse_roster(fetch_wikitext())
    entries = [e for items in regions.values() for e in items]

    if resolve_english and entries:
        ids = _wikibase_ids([e["name"] for e in entries])
        labels = _english_labels(list(ids.values()))
        for entry in entries:
            qid = ids.get(entry["name"])
            entry["wikidata"] = qid or ""
            entry["en"] = labels.get(qid, "") if qid else ""

    # 維基表格更新後，部分新增候選人可能暫時沒有 Wikidata 條目。
    # 按「縣市＋姓名」沿用前一版已核對的英文名，避免 Polymarket 匹配退化。
    previous = load_roster().get("regions") or {}
    for region, items in regions.items():
        old_items = previous.get(region) or []
        old_by_name = {item.get("name"): item for item in old_items}
        for entry in items:
            old = old_by_name.get(entry.get("name")) or {}
            for key in ("wikidata", "en"):
                if not entry.get(key) and old.get(key):
                    entry[key] = old[key]

    return {
        "meta": {
            "source": f"https://zh.wikipedia.org/wiki/{urllib.parse.quote(PAGE)}",
            "official_source": "https://info.cec.gov.tw/vote2026/",
            "number_source": "2026年中華民國地方公職人員選舉條目所載抽籤號次",
            "number_updated_at": time.strftime("%Y-%m-%d"),
            "note": (
                "22縣市長正式登記參選人與抽籤號次；正式候選人名單及號次以中央選舉委員會公告為準。"
                " en 為 Wikidata 英文標籤，用於對接 Polymarket 拼音名。"
            ),
            "regions": len(regions),
            "candidates": len(entries),
            "updated_at": time.strftime("%Y-%m-%d"),
        },
        "regions": regions,
    }


def save_roster(roster: dict[str, Any], path=None) -> str:
    target = path or (CONFIG_DIR / "roster.json")
    target.write_text(json.dumps(roster, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


def load_roster(path=None) -> dict[str, Any]:
    target = path or (CONFIG_DIR / "roster.json")
    if not target.exists():
        return {"regions": {}}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"regions": {}}


def _lookup_key(value: str) -> str:
    """簡繁、臺台與空白差異不影響名冊查詢。"""
    return to_simplified((value or "").replace("臺", "台")).strip().lower()


def entry_for(
    roster: dict[str, Any],
    region_name: str,
    name_zh: str = "",
    name_en: str = "",
) -> dict[str, Any] | None:
    """依縣市 + 中文名或英文名找出名冊條目。"""
    regions = roster.get("regions") or {}
    region_key = _lookup_key(region_name)
    items = next(
        (entries for key, entries in regions.items() if _lookup_key(key) == region_key),
        [],
    )
    if name_zh:
        zh_key = _lookup_key(name_zh)
        for item in items:
            if _lookup_key(item.get("name") or "") == zh_key:
                return item
        # 名冊上有人的名字特別長（例如「黃宏成台灣阿成世界偉人財神總統」），
        # 市場只用其中一段，這裡允許「市場名是名冊名開頭」的比對；
        # 只取最短的那個命中，避免短名誤配到別人。
        prefix_hits = [
            item for item in items
            if len(name_zh) >= 3 and _lookup_key(item.get("name") or "").startswith(zh_key)
        ]
        if prefix_hits:
            return min(prefix_hits, key=lambda i: len(i.get("name") or ""))
    if name_en:
        key = _norm(name_en)
        for item in items:
            if _norm(item.get("en") or "") == key:
                return item
    return None


def _nrm(name: str) -> str:
    """羅馬拼音正規化：統一大小寫、去掉分隔符與常見拼寫差異。"""
    s = re.sub(r"[\s\-'’·.()]", "", (name or "").lower())
    for a, b in (("ts", "tz"), ("hs", "sh"), ("ch", "j"), ("k", "g"), ("ph", "f"), ("ü", "u")):
        s = s.replace(a, b)
    return s


def match_candidates(
    snapshots: dict[str, Any],
    roster: dict[str, Any],
    name_map: dict[str, str],
) -> dict[str, Any]:
    """把 Polymarket 候選人對到名冊條目。

    依序嘗試：
      1. config/name_map.json 的中文名
      2. 名冊的 Wikidata 英文標籤（含羅馬拼音正規化後比對）
      3. 該縣市只剩一組配對時直接認列
    """
    regions_cfg = roster.get("regions") or {}
    matched: dict[str, dict[str, dict[str, Any]]] = {}
    unmatched: list[dict[str, str]] = []

    for code, snap in snapshots.items():
        entries = list(regions_cfg.get(snap.region_name) or [])
        if not entries:
            continue
        used: set[int] = set()
        pairs: dict[str, dict[str, Any]] = {}

        # 1) 中文名
        for cand in snap.candidates:
            for i, entry in enumerate(entries):
                if i in used:
                    continue
                if cand.name_zh and cand.name_zh == entry.get("name"):
                    pairs[cand.name_en] = entry
                    used.add(i)
                    break

        # 2) 英文標籤
        for cand in snap.candidates:
            if cand.name_en in pairs:
                continue
            key = _nrm(cand.name_en)
            if not key:
                continue
            for i, entry in enumerate(entries):
                if i in used or not entry.get("en"):
                    continue
                if _nrm(entry["en"]) == key:
                    pairs[cand.name_en] = entry
                    used.add(i)
                    break

        # 3) 唯一剩餘配對
        left_c = [c for c in snap.candidates if c.name_en not in pairs]
        left_e = [e for i, e in enumerate(entries) if i not in used]
        if len(left_c) == 1 and len(left_e) == 1:
            pairs[left_c[0].name_en] = left_e[0]
            used.add(entries.index(left_e[0]))
            left_c, left_e = [], []

        matched[code] = pairs
        for cand in left_c:
            unmatched.append(
                {
                    "region": snap.region_name,
                    "candidate_en": cand.name_en,
                    "candidate_zh": cand.name_zh,
                    "roster_left": "、".join(e.get("name", "") for e in left_e),
                }
            )
    return {"matched": matched, "unmatched": unmatched}
