"""歷史選舉基線：2018 / 2022 縣市長選舉結果。

兩個來源格式不同：
  - 2022：`2022年中華民國直轄市長及縣市長選舉` 條目內，每個縣市章節的
    「選舉結果」小節是一張行內 wikitable（號次 / 候選人 / 政黨 / 得票數 /
    得票率 / 當選標記），表尾另有選舉人數、投票數、投票率。
  - 2018：同一條目只放了候選人彙總模板，明細在各縣市模板
    `Template:2018年中華民國直轄市長選舉/<縣市>`，內容是
    `{{臺灣選舉結果}}`，參數為 c1_name / c1_party / c1_votes / ... 結構化資料。

輸出（config/baseline.json）只保留可用於預測的欄位：當選人與政黨、得票率、
前兩名差距、投票率、選舉人數，以及完整候選人清單。
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from typing import Any

from .http import fetch_json
from .paths import CONFIG_DIR

WIKI_API = "https://zh.wikipedia.org/w/api.php"
PAGE_2022 = "2022年中華民國直轄市長及縣市長選舉"
PAGE_2018 = "2018年中華民國直轄市長及縣市長選舉"
UA = {"User-Agent": "TEM-election-monitor/1.0 (local research tool; python-urllib)"}

_REF = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.S)
_TAG = re.compile(r"<[^>]+>")
_TPL = re.compile(r"\{\{[^{}]*\}\}")
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_CELL_ATTR = re.compile(r"^[^|\[]*=[^|\[]*\|")
_NUM = re.compile(r"-?[\d,]+(?:\.\d+)?")
_PARTY_TPL = re.compile(r"\{\{\s*([A-Za-z][\w./\-]*)")

# 政黨模板 → 顯示名稱（同時作為跨年度比較的識別碼）
PARTY_NAMES = {
    "kmt": "中國國民黨",
    "dpp": "民主進步黨",
    "tpp": "台灣民眾黨",
    "nonp": "無黨籍",
    "pfp": "親民黨",
    "npp": "時代力量",
    "tsu": "台灣團結聯盟",
    "np": "新黨",
    "ssfpp": "小民參政歐巴桑聯盟",
    "trp": "台灣維新",
    "gpt": "綠黨",
    "tpp2": "台灣民眾黨",
}


def _clean(text: str) -> str:
    text = _REF.sub("", text)
    text = _LINK.sub(lambda m: (m.group(2) or m.group(1)), text)
    text = _TPL.sub("", text)
    text = _TAG.sub(" ", text)
    return re.sub(r"\s+", " ", text.replace("'''", "").replace("''", "")).strip(" |")


def _num(text: str) -> float | None:
    text = _clean(text).replace(",", "")
    m = _NUM.search(text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _party(text: str) -> str:
    """從政黨儲存格取出標準政黨名。"""
    for name in _PARTY_TPL.findall(text):
        key = name.lower().replace("/logo2", "").replace("/logo", "")
        if key in PARTY_NAMES:
            return PARTY_NAMES[key]
    clean = _clean(text)
    for alias, full in (("國民黨", "中國國民黨"), ("民進黨", "民主進步黨"), ("民眾黨", "台灣民眾黨")):
        if alias in clean:
            return full
    return clean or "無黨籍"


def _person(text: str) -> str:
    m = _LINK.search(text)
    if m:
        return (m.group(2) or m.group(1)).split(" (")[0].strip()
    return _clean(text)


def _wikitext(page: str) -> str:
    params = {
        "action": "parse", "page": page, "prop": "wikitext",
        "format": "json", "formatversion": "2", "redirects": "1",
    }
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    data = fetch_json(url, timeout=60, retries=2, headers=UA)
    return ((data.get("parse") or {}).get("wikitext")) or ""


def _wikitext_many(titles: list[str]) -> dict[str, str]:
    """一次抓多個模板頁的內容（API 單次上限 50 個標題）。"""
    params = {
        "action": "query", "prop": "revisions", "rvslots": "main",
        "rvprop": "content", "format": "json", "formatversion": "2",
        "redirects": "1", "titles": "|".join(titles),
    }
    url = f"{WIKI_API}?{urllib.parse.urlencode(params)}"
    data = fetch_json(url, timeout=60, retries=2, headers=UA)
    out: dict[str, str] = {}
    for page in (data.get("query") or {}).get("pages") or []:
        content = (((page.get("revisions") or [{}])[0].get("slots") or {}).get("main") or {}).get("content")
        if content:
            out[page.get("title") or ""] = content
    return out


# ---------------------------------------------------------------- 表格工具

def _tables(wikitext: str):
    for chunk in re.split(r"\n\{\|", wikitext):
        if "|}" in chunk:
            yield chunk


def _cell(text: str) -> str:
    text = text.strip()
    text = _CELL_ATTR.sub("", text, count=1)
    return text.strip()


def _rows(table: str, *, clean: bool = True) -> list[list[str]]:
    """把 wikitable 拆成列，每列是儲存格字串清單。

    clean=False 時保留原始儲存格（例如 [[File:Vote1.svg|20px]] 這種
    帶有意義內容但會被屬性剝離規則影響的儲存格）。
    """
    rows: list[list[str]] = []
    cur: list[str] = []
    def cells(line: str) -> list[str]:
        parts = [p.strip() for p in line.split("||")]
        return [_cell(p) for p in parts] if clean else parts

    for raw in table.splitlines():
        line = raw.strip()
        if not line or line.startswith("{|") or line.startswith("|}") or line.startswith("|+"):
            continue
        if line.startswith("|-"):
            if cur:
                rows.append(cur)
                cur = []
            continue
        if line.startswith("!"):
            # 連續多行 ! 屬於同一列，只有 |- 才換列
            cur.extend(cells(line[1:].replace("!!", "||")))
            continue
        if line.startswith("|"):
            cur.extend(cells(line[1:]))
    if cur:
        rows.append(cur)
    return rows


def _regions_of(wikitext: str, level: str = "==="):
    """切出 (縣市名, 章節內容) 序列。"""
    pattern = rf"^{re.escape(level)}\s*([^=\n]+?)\s*{re.escape(level)}\s*$"
    marks = [(m.start(), m.end(), m.group(1)) for m in re.finditer(pattern, wikitext, re.M)]
    out: list[tuple[str, str]] = []
    for idx, (_, end, raw_name) in enumerate(marks):
        stop = marks[idx + 1][0] if idx + 1 < len(marks) else len(wikitext)
        name = _clean(raw_name).split("（")[0].strip()
        if not name.endswith(("市", "縣")):
            continue
        out.append((name, wikitext[end:stop]))
    return out


def _subsection(body: str, title: str) -> str:
    m = re.search(rf"^=+\s*{re.escape(title)}\s*=+\s*$", body, re.M)
    if not m:
        return ""
    rest = body[m.end():]
    nxt = re.search(r"^=+\s*[^=\n]+?\s*=+\s*$", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


# ---------------------------------------------------------------- 2022

def parse_2022(wikitext: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for region, body in _regions_of(wikitext):
        section = _subsection(body, "選舉結果")
        if not section:
            continue
        result = _parse_result_table(section)
        if result:
            out[region] = result
    return out


def _parse_result_table(section: str) -> dict[str, Any] | None:
    for table in _tables(section):
        rows = _rows(table)
        raw_rows = _rows(table, clean=False)
        head_idx = next(
            (i for i, row in enumerate(rows) if "得票率" in " ".join(row) and "候選人" in " ".join(row)),
            None,
        )
        if head_idx is None:
            continue
        rows = rows[head_idx + 1:]
        raw_rows = raw_rows[head_idx + 1:]
        candidates: list[dict[str, Any]] = []
        extra: dict[str, float] = {}
        for row, raw in zip(rows, raw_rows):
            joined = " ".join(row)
            if "選舉人數" in joined or "投票數" in joined or "投票率" in joined:
                cells = [c for c in row if c]
                for i, cell in enumerate(cells):
                    text = _clean(cell)
                    value = _num(cells[i + 1]) if i + 1 < len(cells) else None
                    for label, key in (
                        ("選舉人數", "electorate"), ("投票數", "cast"),
                        ("有效票", "valid"), ("無效票", "invalid"), ("投票率", "turnout"),
                    ):
                        if label in text and value is not None:
                            extra[key] = value
                continue
            if len(row) < 5:
                continue
            name = _person(row[1])
            if not name or len(name) > 24:
                continue
            party = _party(row[2])
            votes = _num(row[3])
            pct = _num(row[4])
            if pct is None:
                continue
            tail = raw[-1] if raw else ""
            candidates.append({
                "name": name, "party": party, "votes": votes,
                "pct": pct, "win": bool("vote1" in tail.lower() or "當選" in tail),
            })
        if len(candidates) < 2:
            continue
        candidates.sort(key=lambda c: c["pct"], reverse=True)
        if not any(c["win"] for c in candidates):
            candidates[0]["win"] = True
        return _summarise(candidates, extra)
    return None


# ---------------------------------------------------------------- 2018

BRANCHES = (
    "2018年中華民國直轄市長選舉",
    "2018年中華民國縣市長選舉",
)


def fetch_2018_templates(regions: list[str]) -> dict[str, str]:
    titles = [f"Template:{branch}/{region}" for branch in BRANCHES for region in regions]
    return _wikitext_many(titles)


def parse_2018_template(content: str) -> dict[str, Any] | None:
    params: dict[str, str] = {}
    for line in content.splitlines():
        m = re.match(r"^\|\s*([A-Za-z_][\w]*)\s*=\s*(.*)$", line.strip())
        if m:
            params[m.group(1).lower()] = m.group(2).strip()
    candidates: list[dict[str, Any]] = []
    idx = 1
    while f"c{idx}_votes" in params or f"c{idx}_name" in params:
        name = _person(params.get(f"c{idx}_name", ""))
        if name:
            candidates.append({
                "name": name,
                "party": _party(params.get(f"c{idx}_party", "")),
                "votes": _num(params.get(f"c{idx}_votes", "")),
                "pct": _num(params.get(f"c{idx}_percentage", "")) or 0.0,
                "win": bool(params.get(f"c{idx}_win", "").strip()),
            })
        idx += 1
    if len(candidates) < 2:
        return None
    extra = {
        "electorate": _num(params.get("total", "")),
        "cast": _num(params.get("count", "")),
        "turnout": _num(params.get("turnout", "")),
    }
    candidates.sort(key=lambda c: c["pct"], reverse=True)
    if not any(c["win"] for c in candidates):
        candidates[0]["win"] = True
    return _summarise(candidates, extra)


# ---------------------------------------------------------------- 共用

def _summarise(candidates: list[dict[str, Any]], extra: dict[str, float]) -> dict[str, Any]:
    winner = candidates[0]
    runner = candidates[1] if len(candidates) > 1 else None
    data: dict[str, Any] = {
        "winner": winner["name"],
        "winner_party": winner["party"],
        "winner_pct": winner["pct"],
        "runner_up": runner["name"] if runner else None,
        "runner_up_party": runner["party"] if runner else None,
        "runner_up_pct": runner["pct"] if runner else None,
        "margin": round(winner["pct"] - (runner["pct"] if runner else 0.0), 2),
        "candidates": candidates,
    }
    for key in ("electorate", "cast", "turnout", "valid", "invalid"):
        if extra.get(key) is not None:
            data[key] = extra[key]
    return data


def build_baseline(*, regions: list[str] | None = None) -> dict[str, Any]:
    from .config import load_settings

    settings = load_settings()
    names = regions or [r.name for r in settings.regions]

    got2022 = parse_2022(_wikitext(PAGE_2022))
    got2018: dict[str, dict[str, Any]] = {}
    for title, content in fetch_2018_templates(names).items():
        region = title.split("/")[-1]
        parsed = parse_2018_template(content)
        if parsed:
            got2018[region] = parsed

    merged: dict[str, dict[str, Any]] = {}
    for name in names:
        entry: dict[str, Any] = {}
        for year, source in (("2018", got2018), ("2022", got2022)):
            hit = source.get(name)
            if hit is None:  # 「臺/台」「連江縣」等寫法差異
                key = name.replace("臺", "台")
                hit = next((v for k, v in source.items() if k.replace("臺", "台") == key), None)
            if hit:
                entry[year] = hit
        if entry:
            merged[name] = entry
    return {
        "meta": {
            "sources": [
                f"https://zh.wikipedia.org/wiki/{urllib.parse.quote(PAGE_2018)}",
                f"https://zh.wikipedia.org/wiki/{urllib.parse.quote(PAGE_2022)}",
            ],
            "note": "2018 / 2022 縣市長選舉結果，用於基線與回測",
            "updated_at": time.strftime("%Y-%m-%d"),
        },
        "regions": merged,
    }


def save_baseline(data: dict[str, Any], path=None) -> str:
    target = path or (CONFIG_DIR / "baseline.json")
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


_CACHE: dict[str, Any] = {}


def load_baseline(path=None) -> dict[str, Any]:
    target = path or (CONFIG_DIR / "baseline.json")
    if not target.exists():
        return {"regions": {}}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"regions": {}}


def region_baseline(region_name: str) -> dict[str, Any] | None:
    """依縣市名取 2018/2022 結果（繁簡與臺/台都會正規化）。"""
    from .zhconv import to_simplified

    if "data" not in _CACHE:
        _CACHE["data"] = load_baseline()
    regions = _CACHE["data"].get("regions") or {}
    want = to_simplified(region_name).replace("臺", "台")
    for name, data in regions.items():
        if to_simplified(name).replace("臺", "台") == want:
            return data
    return None


def swing(region_name: str) -> float | None:
    """當選政黨在 2018→2022 的得票率變化（百分點）。"""
    data = region_baseline(region_name) or {}
    a, b = data.get("2018"), data.get("2022")
    if not a or not b:
        return None
    if a.get("winner_party") != b.get("winner_party"):
        return None
    return round((b.get("winner_pct") or 0.0) - (a.get("winner_pct") or 0.0), 2)
