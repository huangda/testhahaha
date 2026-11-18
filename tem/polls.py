"""民意調查 v2：加權滾動平均 + 日期補正。

v1 的問題：
  1. 只認「YYYY年M月D日」，像「11月9日至12日」這種省略年份的調查區間會整筆被
     丟進 source 欄位，導致日期錯亂（history 裡出現 source="1月19日至21日"）。
  2. 只用最近 5 筆做無權重算術平均，新舊民調、機構品質一視同仁。

v2 的做法：
  - 年份缺漏時，優先用同一列引註（<ref>）裡的 date=YYYY-MM-DD 補年份，
    再退回「同一張表最近一次出現的年份」。
  - 機構權重（SOURCE_WEIGHTS）× 時間衰減（半衰期 45 天）做加權平均。
  - 另外輸出 poll_signal：加權後的領先者、前兩名差距、正規化支持度。
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
from datetime import date
from typing import Any

from .http import fetch_json
from .paths import CONFIG_DIR, EXPORT_DIR

WIKI_API = "https://zh.wikipedia.org/w/api.php"
PAGE = "2026年中華民國直轄市長及縣市長選舉民意調查"
UA = {"User-Agent": "TEM-election-monitor/1.0 (local research tool; python-urllib)"}

_REF = re.compile(r"<ref[^>]*>.*?</ref>|<ref[^>]*/>", re.S)
_TAG = re.compile(r"<[^>]+>")
_EXT_LINK = re.compile(r"\[(?:https?://\S+)\s*([^\]]*)\]")
_LINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_CELL_ATTR = re.compile(r"^[^|\[]*=[^|\[]*\|")
_PCT = re.compile(r"^[\s']*(\d{1,3}(?:\.\d+)?)\s*[%％]")
_IMGSIZE = re.compile(r"\d+\s*x\s*\d+\s*px")

_YEAR_TEXT = re.compile(r"(\d{4})\s*年")
_MONTH_TEXT = re.compile(r"(\d{1,2})\s*月")
_DAY_TEXT = re.compile(r"(\d{1,2})\s*[日號]")
_REF_DATE_ANY = re.compile(r"(access-?date|date)\s*=\s*(\d{4})-(\d{1,2})-(\d{1,2})", re.I)
_SKIP_HEADER = ("民調來源", "調查日期", "完成日期", "未表態", "來源", "日期", "其他", "差距", "領先")
_NOT_CANDIDATE = re.compile(r"px|表態|差距|領先|其他|^[\d\s]")

# 機構權重：以長期民調品質與選舉預測準確度粗略排序，可用 TEM_POLL_WEIGHTS 覆寫
SOURCE_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("TVBS", 1.15), ("聯合報", 1.10), ("中央社", 1.10), ("台灣民意基金會", 1.10),
    ("政大", 1.10), ("匯流", 1.05), ("美麗島", 1.05), ("年代", 1.00),
    ("世新", 1.00), ("艾普羅", 1.00), ("全國公信力", 1.00), ("典通", 1.00),
    ("ETtoday", 1.00), ("中時", 0.95), ("自由時報", 0.95), ("三立", 0.95),
    ("風傳媒", 0.95), ("中華亞太菁英", 0.95), ("台灣指標", 1.00),
    ("鉅聞", 1.00), ("巨聞", 1.00), ("巨闻", 1.00), ("菱傳", 1.00), ("菱传", 1.00),
    ("皮爾森", 1.00), ("皮尔森", 1.00), ("東森", 1.00), ("东森", 1.00),
    ("QuickseeK", 0.85), ("網路溫度計", 0.85), ("DailyView", 0.85),
    ("Yahoo", 0.60), ("網路投票", 0.50), ("線上投票", 0.50),
)
HALF_LIFE_DAYS = 45.0
# 資料來源層級：機構公布的完整表格 > 從新聞標題抽出的數字
TIER_WEIGHTS = {"direct": 1.05, "institute": 1.0, "media": 0.75, "media_margin": 0.5}
BLUE_SPONSORS = (
    "中國國民黨", "国民党", "國民黨", "藍營", "蓝营", "侯友宜競選", "蔣萬安競選",
    "謝國樑競選", "柯志恩競選",
)
GREEN_SPONSORS = (
    "民主進步黨", "民进党", "民進黨", "綠營", "绿营", "賴清德競選", "沈伯洋競選",
    "蘇巧慧競選", "陳其邁競選",
)
BLUE_MEDIA = ("聯合報", "联合报", "中國時報", "中国时报", "中天", "中視", "中视", "TVBS")
GREEN_MEDIA = ("自由時報", "自由时报", "三立", "民視", "民视", "新頭殼", "新头壳", "放言", "芋傳媒", "芋传媒")
_PARTY_LOOKUP: dict[tuple[str, str], str] = {}
_PARTY_TPL = re.compile(r"\{\{\s*([A-Za-z][\w./\-]*)")
PARTY_LABELS = {
    "kmt": "中國國民黨", "dpp": "民主進步黨", "tpp": "台灣民眾黨", "nonp": "無黨籍",
    "pfp": "親民黨", "npp": "時代力量", "tsu": "台灣團結聯盟", "np": "新黨",
}


def _party_of(header_cell: str) -> str:
    for name in _PARTY_TPL.findall(header_cell):
        key = name.lower().replace("/logo2", "").replace("/logo", "")
        if key in PARTY_LABELS:
            return PARTY_LABELS[key]
    return ""


def _clean(text: str) -> str:
    text = _REF.sub("", text)
    text = _EXT_LINK.sub(lambda m: m.group(1), text)
    text = _LINK.sub(lambda m: (m.group(2) or m.group(1)), text)
    text = _TAG.sub("", text)
    text = _TEMPLATE.sub("", text)
    text = _IMGSIZE.sub("", text)
    return re.sub(r"\s+", " ", text.replace("'''", "").replace("''", "")).strip(" |")


def _cell(text: str) -> str:
    return _clean(_CELL_ATTR.sub("", text.strip(), count=1))


def _tables(wikitext: str):
    for chunk in re.split(r"\n\{\|", wikitext):
        if "|}" in chunk:
            yield chunk


def _rows(table: str) -> list[list[str]]:
    rows: list[list[str]] = []
    cur: list[str] = []
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
            cur.extend(line[1:].replace("!!", "||").split("||"))
            continue
        if line.startswith("|"):
            cur.extend(line[1:].split("||"))
    if cur:
        rows.append(cur)
    return rows


def fetch_wikitext(page: str = PAGE) -> str:
    params = {
        "action": "parse", "page": page, "prop": "wikitext",
        "format": "json", "formatversion": "2", "redirects": "1",
    }
    data = fetch_json(f"{WIKI_API}?{urllib.parse.urlencode(params)}", timeout=45, retries=2, headers=UA)
    return ((data.get("parse") or {}).get("wikitext")) or ""


# ---------------------------------------------------------------- 日期

def _ref_dates(raw: str) -> tuple[date | None, date | None]:
    """回傳同一列引註裡的 (發布日期, 存取日期)。"""
    pub = acc = None
    for m in _REF_DATE_ANY.finditer(raw):
        try:
            value = date(int(m.group(2)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            continue
        if m.group(1).lower().startswith("access"):
            acc = value if acc is None else max(acc, value)
        else:
            pub = value if pub is None else max(pub, value)
    return pub, acc


def _parse_date(
    text: str,
    *,
    pub: date | None = None,
    acc: date | None = None,
    fallback_year: int | None = None,
) -> tuple[str, int]:
    """解析調查區間，取最後一天。年份缺漏時用引註日期回推（不得晚於引註日）。"""
    months = _MONTH_TEXT.findall(text)
    if not months:
        return "", 0
    month = int(months[-1])
    days = _DAY_TEXT.findall(text)
    day = int(days[-1]) if days else 1
    years = _YEAR_TEXT.findall(text)

    def valid(year: int) -> date | None:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    if years:
        year = int(years[-1])
    else:
        ceiling = acc or pub
        year = 0
        if ceiling:
            for candidate in (ceiling.year, ceiling.year - 1, (fallback_year or 0)):
                when = valid(candidate) if candidate else None
                if when and when <= ceiling:
                    year = candidate
                    break
            if not year:
                year = ceiling.year
        elif fallback_year:
            year = fallback_year
        else:
            return "", 0
    when = valid(year)
    if not when:
        return "", 0
    return when.isoformat(), year


# ---------------------------------------------------------------- 解析

def _candidate_columns(header_row: list[str]) -> list[dict[str, str]]:
    cols: list[dict[str, str]] = []
    for cell in header_row:
        name = _cell(cell)
        if any(skip in name for skip in _SKIP_HEADER):
            continue
        if not name or _NOT_CANDIDATE.search(name) or not 2 <= len(name) <= 8:
            continue
        cols.append({"name": name, "party": _party_of(cell)})
    return cols


def _parse_table(table: str, *, fallback_year: int | None = None) -> list[dict[str, Any]]:
    rows = _rows(table)
    head_idx = next(
        (i for i, r in enumerate(rows) if any("來源" in c for c in r) and any("日" in c for c in r)),
        None,
    )
    if head_idx is None:
        return []
    columns = _candidate_columns(rows[head_idx])
    if not columns:
        return []
    names = [c["name"] for c in columns]
    parties = {c["party"] for c in columns if c["party"]}
    # 同一政黨候選人互比 = 黨內初選民調，不是對戰民調
    kind = "primary" if len(columns) > 1 and len(parties) == 1 else "matchup"
    preamble = " ".join(" ".join(r) for r in rows[:head_idx])
    if "初選" in preamble or "初选" in preamble:
        kind = "primary"

    polls: list[dict[str, Any]] = []
    source = ""
    date = ""
    year = fallback_year or 0
    for row in rows[head_idx + 1:]:
        head: list[str] = []
        values: list[str] = []
        for cell in row:
            text = _cell(cell)
            if _PCT.match(text) or text in ("-", "–", "—", ""):
                values.append(text)
            else:
                head.append(cell.strip())
        pub = acc = None
        date_text = ""
        for raw in head:
            p, a = _ref_dates(raw)
            pub = pub or p
            acc = acc or a
            text = _cell(raw)
            if any(k in text for k in ("月", "年", "日")):
                date_text = f"{date_text} {text}".strip()
        for raw in head:
            text = _cell(raw)
            if not text:
                continue
            if any(k in text for k in ("月", "年", "日")):
                parsed, got_year = _parse_date(
                    date_text or text, pub=pub, acc=acc, fallback_year=year or fallback_year
                )
                if parsed:
                    date = parsed
                    year = got_year or year
                continue
            if len(text) > 1 and not text.replace(".", "").isdigit():
                source = text[:28]
        if not values:
            continue
        picked: dict[str, float] = {}
        filled: list[dict[str, str]] = []
        for idx, text in enumerate(values):
            if idx >= len(names):
                break
            m = _PCT.match(text)
            if not m:
                continue
            try:
                picked[names[idx]] = float(m.group(1))
                filled.append(columns[idx])
            except ValueError:
                continue
        if picked and date:
            row_kind = kind
            row_parties = {c["party"] for c in filled if c["party"]}
            if len(filled) > 1 and len(row_parties) == 1:
                row_kind = "primary"  # 這一列只問了同一個黨的候選人
            if "初選" in source or "初选" in source:
                row_kind = "primary"
            if source.strip() in PARTY_LABELS.values():
                row_kind = "primary"  # 來源直接寫政黨名，通常是該黨初選
            if sum(picked.values()) > 100.5:
                row_kind = "primary"  # 支持度加總破百，只可能是黨內互比
            polls.append({
                "source": source, "date": date, "values": picked,
                "ref_year": (acc or pub).year if (acc or pub) else year,
                "kind": row_kind,
                # 同一張表的候選人組合 = 一個對戰組合，不同組合不能混在一起平均
                "matchup": tuple(sorted(picked)),
            })
    return polls


def parse_polls(wikitext: str) -> dict[str, list[dict[str, Any]]]:
    """回傳 {縣市: [ {source, date, values} ... ]}。"""
    sections = re.split(r"\n==+\s*([^=\n]+?)\s*==+\s*\n", wikitext)
    out: dict[str, list[dict[str, Any]]] = {}
    for i in range(1, len(sections) - 1, 2):
        region = _clean(sections[i])
        if not region.endswith(("市", "縣")):
            continue
        polls: list[dict[str, Any]] = []
        for table in _tables(sections[i + 1]):
            polls.extend(_parse_table(table))
        if polls:
            out.setdefault(region, []).extend(polls)
    return out


# ---------------------------------------------------------------- 加權

def source_weight(source: str) -> float:
    base = 0.95
    for keyword, weight in SOURCE_WEIGHTS:
        if keyword and keyword.lower() in (source or "").lower():
            base = weight
            break
    try:
        from .poll_accuracy import history_reference

        history = history_reference(source)
        if history:
            base *= float(history.get("weight_multiplier") or 1.0)
    except (ImportError, OSError, ValueError, TypeError):
        pass
    return base


def tier_weight(tier: str | None) -> float:
    return TIER_WEIGHTS.get(tier or "institute", 0.95)


def poll_bias(poll: dict[str, Any]) -> dict[str, Any]:
    """识别阵营倾向，并结合“结果是否有利于委托方”修正权重。"""
    from .roster import load_roster

    def norm(value: Any) -> str:
        return str(value or "").replace("臺", "台").replace(" ", "").strip()

    region_key = norm(poll.get("_region") or poll.get("region"))
    if not _PARTY_LOOKUP:
        for region, entries in (load_roster().get("regions") or {}).items():
            for entry in entries or []:
                _PARTY_LOOKUP[(norm(region), norm(entry.get("name")))] = str(entry.get("party") or "")

    values = poll.get("values") if isinstance(poll.get("values"), dict) else {}
    leader = (
        max(values, key=values.get)
        if values
        else str(poll.get("lead") or "")
    )
    leader_party = _PARTY_LOOKUP.get((region_key, norm(leader)), "")
    leader_camp = ""
    if "國民黨" in leader_party or "国民党" in leader_party:
        leader_camp = "blue"
    elif "民主進步黨" in leader_party or "民进党" in leader_party:
        leader_camp = "green"

    sponsor = " ".join(
        str(poll.get(key) or "")
        for key in ("sponsor", "pollster", "executor")
    )
    title = str(poll.get("title") or "")
    source = str(poll.get("source") or "")
    camp = ""
    kind = ""
    if any(token in sponsor for token in BLUE_SPONSORS):
        camp, kind = "blue", "party"
    elif any(token in sponsor for token in GREEN_SPONSORS):
        camp, kind = "green", "party"
    elif any(token in sponsor for token in ("競辦", "竞办", "競選總部", "竞选总部", "內參", "内参")):
        for (lookup_region, lookup_name), lookup_party in _PARTY_LOOKUP.items():
            if lookup_region == region_key and lookup_name and lookup_name in sponsor:
                if "國民黨" in lookup_party or "国民党" in lookup_party:
                    camp, kind = "blue", "party"
                    break
                if "民主進步黨" in lookup_party or "民进党" in lookup_party:
                    camp, kind = "green", "party"
                    break
    if not camp and any(token in title for token in ("藍營民調", "蓝营民调", "國民黨民調", "国民党民调", "藍營內參", "蓝营内参")):
        camp, kind = "blue", "party"
    elif not camp and any(token in title for token in ("綠營民調", "绿营民调", "民進黨民調", "民进党民调", "綠營內參", "绿营内参")):
        camp, kind = "green", "party"

    if not camp and any(token in source for token in BLUE_MEDIA):
        camp, kind = "blue", "media"
    elif not camp and any(token in source for token in GREEN_MEDIA):
        camp, kind = "green", "media"
    if camp:
        executor = str(poll.get("executor") or "")
        independent_executor = bool(executor) and not any(
            token in executor for token in (*BLUE_MEDIA, *GREEN_MEDIA)
        )
        side = "蓝" if camp == "blue" else "绿"
        if leader_camp and leader_camp != camp:
            outcome = "against"
            outcome_label = f"反阵营结果：{side}营民调由对手阵营候选人领先"
        elif leader_camp == camp:
            outcome = "favor"
            outcome_label = f"同阵营有利结果：{side}营民调由本阵营候选人领先"
        elif leader_camp:
            outcome = "third"
            outcome_label = f"{side}营民调由第三阵营候选人领先"
        else:
            outcome = "unknown"
            outcome_label = f"{side}营民调，领先者阵营未识别"
        if kind == "party":
            weight = {
                "against": 1.00,
                "favor": 0.30,
                "third": 0.60,
                "unknown": 0.55,
            }[outcome]
            label = f"政党或竞选团队委托；{outcome_label}"
        else:
            weight = {
                "against": 1.05,
                "favor": 0.70,
                "third": 0.88,
                "unknown": 0.82,
            }[outcome]
            label = (
                f"偏{side}媒体转载并附独立执行；{outcome_label}"
                if independent_executor
                else f"偏{side}媒体公布；{outcome_label}"
            )
        return {
            "camp": camp,
            "label": label,
            "outcome": outcome,
            "leader": leader,
            "leader_party": leader_party,
            "weight": weight,
        }
    return {
        "camp": "unknown",
        "label": "未识别明确阵营委托",
        "outcome": "unknown",
        "leader": leader,
        "leader_party": leader_party,
        "weight": 1.0,
    }


def _age_days(date_iso: str, as_of: str) -> float | None:
    from datetime import date

    try:
        d = date.fromisoformat(date_iso[:10])
        ref = date.fromisoformat(as_of[:10])
    except ValueError:
        return None
    return max(0.0, (ref - d).days)


def weighted_average(
    items: list[dict[str, Any]],
    *,
    as_of: str | None = None,
    half_life: float = HALF_LIFE_DAYS,
    max_age_days: int = 400,
    include_primary: bool = False,
) -> dict[str, Any]:
    """機構權重 × 時間衰減的加權平均。"""
    as_of = as_of or time.strftime("%Y-%m-%d")
    dated = [
        p for p in items
        if (
            p.get("date")
            and not p.get("_exclude_from_model")
            and (include_primary or p.get("kind") != "primary")
        )
    ]
    dated.sort(key=lambda p: p["date"])
    kept = [p for p in dated if (a := _age_days(p["date"], as_of)) is None or a <= max_age_days]
    if not kept:
        kept = dated[-5:]
    if not kept:
        return {}

    weights: dict[str, float] = {}
    sums: dict[str, float] = {}
    decays: list[float] = []
    bias_rows: list[dict[str, Any]] = []
    for poll in kept:
        age = _age_days(poll["date"], as_of)
        decay = 0.5 ** (age / half_life) if age is not None else 1.0
        decays.append(decay)
        weight = (
            source_weight(poll.get("source", ""))
            * tier_weight(poll.get("tier"))
            * float(poll_bias(poll)["weight"])
            * (
                min(1.0, math.sqrt(float(poll["sample_size"]) / 1000))
                if poll.get("sample_size") else 1.0
            )
            * float(poll.get("evidence_quality") or 1.0)
            * float(poll.get("verification_weight_factor") or 1.0)
            * max(decay, 1e-4)
        )
        bias_rows.append(poll_bias(poll))
        weights[poll["date"]] = weight
        for name, value in poll["values"].items():
            sums[name] = sums.get(name, 0.0) + value * weight
            weights.setdefault(name + "\u0000w", 0.0)
            weights[name + "\u0000w"] += weight

    avg: dict[str, float] = {}
    for name in sums:
        total_w = weights.get(name + "\u0000w", 0.0)
        if total_w > 0:
            avg[name] = round(sums[name] / total_w, 1)
    avg = dict(sorted(avg.items(), key=lambda kv: kv[1], reverse=True))
    return {
        "weighted": avg,
        "effective_samples": round(sum(v for k, v in weights.items() if not k.endswith("\u0000w")), 2),
        "poll_count": len(kept),
        # freshness：時間衰減後的平均新鮮度（1=全部剛發布，0=全部很舊）
        "freshness": round(sum(decays) / len(decays), 3) if decays else 0.0,
        "age_days": round(_age_days(kept[-1]["date"], as_of) or 0),
        "latest_date": kept[-1]["date"],
        "oldest_date": kept[0]["date"],
        "latest_source": kept[-1].get("source") or "",
        "bias": {
            "camps": sorted({row["camp"] for row in bias_rows}),
            "labels": sorted({row["label"] for row in bias_rows}),
            "minimum_weight": min((row["weight"] for row in bias_rows), default=1.0),
        },
        "as_of": as_of,
    }


def summarise(
    polls: dict[str, list[dict[str, Any]]],
    *,
    window: int = 5,
    as_of: str | None = None,
) -> dict[str, dict[str, Any]]:
    """每個縣市「按對戰組合」分別彙整，再挑出代表性組合當摘要。"""
    out: dict[str, dict[str, Any]] = {}

    def is_candidate(name: str) -> bool:
        return bool(name) and 2 <= len(name) <= 8 and not _NOT_CANDIDATE.search(name)

    for region, items in polls.items():
        all_dated = [p for p in items if p.get("date")]
        for p in all_dated:
            p["_region"] = region
            p["values"] = {k: v for k, v in p["values"].items() if is_candidate(k)}
        # 只保留「有支持度」或「有差距且點名兩位候選人」的民調
        all_dated = [
            p for p in all_dated
            if (
                not p.get("_exclude_from_model")
                and (p["values"] or (p.get("gap") and len(p.get("matchup") or []) >= 2))
            )
        ]
        if not all_dated:
            continue

        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        margin_only: list[dict[str, Any]] = []
        for poll in all_dated:
            if len(poll.get("values") or {}) >= 2:
                groups.setdefault(tuple(poll.get("matchup") or ()), []).append(poll)
            elif poll.get("gap") and len(poll.get("matchup") or []) >= 2:
                # 只有「領先 X 個百分點」的標題：沒有支持度，但差距本身有用
                margin_only.append(poll)

        matchups: list[dict[str, Any]] = []
        for key, group in groups.items():
            matchup_polls = [p for p in group if p.get("kind") != "primary"]
            if len(matchup_polls) < 1:
                continue
            matchup_polls.sort(key=lambda p: p["date"])
            recent = matchup_polls[-window:]
            avg: dict[str, float] = {}
            for name in recent[-1]["values"]:
                vals = [p["values"][name] for p in matchup_polls if name in p["values"]]
                if vals:
                    avg[name] = round(sum(vals) / len(vals), 1)
            weighted = weighted_average(matchup_polls, as_of=as_of)
            item: dict[str, Any] = {
                "matchup": list(key),
                "latest_date": recent[-1]["date"],
                "latest_source": recent[-1].get("source") or "",
                "latest": dict(recent[-1]["values"]),
                "average": avg,
                "weighted": weighted.get("weighted") or avg,
                "effective_samples": weighted.get("effective_samples"),
                "freshness": weighted.get("freshness"),
                "age_days": weighted.get("age_days"),
                "effective_date": weighted.get("latest_date"),
                "oldest_date": weighted.get("oldest_date"),
                "poll_count": weighted.get("poll_count") or len(recent),
                "bias": weighted.get("bias") or {},
                "history": [
                    {"date": p["date"], "source": p.get("source") or "", "values": p["values"]}
                    for p in recent
                ],
            }
            signal = poll_signal(item)
            # 把「只有差距」的民調也併進差距估計（不影響顯示用的支持度均值）
            keyset = set(key)
            evidence = [
                p for p in margin_only
                if set(p.get("matchup") or []) <= keyset and len(p.get("matchup") or []) >= 2
            ]
            if evidence and signal:
                as_of_day = as_of or time.strftime("%Y-%m-%d")
                rows_w: list[tuple[float, float]] = []
                values_margin = signal.get("margin")
                values_weight = float(item.get("effective_samples") or 0.0)
                leader = signal.get("leader")
                if values_margin is not None and values_weight > 0:
                    rows_w.append((float(values_margin), values_weight))
                for p in evidence:
                    age = _age_days(p.get("date") or "", as_of_day)
                    decay = 0.5 ** ((age if age is not None else 0) / HALF_LIFE_DAYS)
                    # 差距要帶方向：若這筆民調的領先者不是同一人，差距算負的
                    signed = abs(float(p.get("gap") or 0))
                    if leader and p.get("lead") and p["lead"] != leader:
                        signed = -signed
                    rows_w.append((
                        signed,
                        source_weight(p.get("source", "")) * tier_weight(p.get("tier")) * max(decay, 1e-4),
                    ))
                total_w = sum(w for _, w in rows_w)
                if total_w > 0:
                    blended = sum(v * w for v, w in rows_w) / total_w
                    item["margin_override"] = round(blended, 1)
                    item["margin_evidence"] = len(evidence)
                    signal["margin"] = round(blended, 1)
                    signal["margin_evidence"] = len(evidence)
            if signal:
                item["signal"] = signal
            matchups.append(item)
        if not matchups:
            continue
        # 代表性組合：先看最新日期與新鮮度，再看證據量。
        # 不能讓歷史樣本很多的舊組合壓過本週剛公布的直抓民調。
        matchups.sort(
            key=lambda m: (
                m.get("effective_date") or "",
                m.get("freshness") or 0,
                m.get("effective_samples") or 0,
            ),
            reverse=True,
        )
        entry = dict(matchups[0])
        entry["poll_total"] = len(all_dated)
        entry["matchup_polls"] = sum(m["poll_count"] for m in matchups)
        entry["primary_polls"] = len(all_dated) - sum(
            m["poll_count"] for m in matchups
        )
        entry["has_matchup"] = any(m.get("signal") for m in matchups)
        entry["matchups"] = [
            {k: v for k, v in m.items() if k != "history"} for m in matchups
        ]
        out[region] = entry
    return out


def poll_signal(entry: dict[str, Any]) -> dict[str, Any] | None:
    """由加權平均導出領先者、前兩名差距與正規化支持度。"""
    avg = entry.get("weighted") or entry.get("average") or {}
    ranked = sorted(((k, v) for k, v in avg.items() if v > 0), key=lambda kv: kv[1], reverse=True)
    if len(ranked) < 2:
        return None
    total = sum(v for _, v in ranked) or 1.0
    leader, leader_pct = ranked[0]
    second, second_pct = ranked[1]
    return {
        "leader": leader,
        "leader_pct": round(leader_pct, 1),
        "runner_up": second,
        "runner_up_pct": round(second_pct, 1),
        "margin": round(leader_pct - second_pct, 1),
        "shares": {name: round(v / total, 4) for name, v in ranked},
        "weighted": {name: round(v, 1) for name, v in ranked},
        "date": entry.get("effective_date") or entry.get("latest_date") or "",
        "sources": entry.get("poll_count"),
        "bias": entry.get("bias") or {},
    }


def build_polls(as_of: str | None = None) -> dict[str, Any]:
    """維基百科民調彙整 + 新聞民調，合併後再彙整。"""
    wiki_error = ""
    try:
        polls = parse_polls(fetch_wikitext())
    except Exception as exc:  # noqa: BLE001 - 外站失败时沿用上次完整汇总
        wiki_error = type(exc).__name__
        cached = load_polls()
        polls = {
            region: [
                dict(item) for item in items
                if (item.get("tier") or "institute") == "institute"
            ]
            for region, items in (cached.get("raw") or {}).items()
        }
        polls = {region: items for region, items in polls.items() if items}
        if not polls:
            raise
    institute_raw = sum(len(v) for v in polls.values())
    media_total = 0
    direct_total = 0
    try:
        from .pollwatch import media_polls_for

        for region in list(polls.keys()):
            extra = media_polls_for(region)
            if extra:
                polls[region].extend(extra)
                media_total += len(extra)
    except Exception:  # noqa: BLE001 - 沒有新聞民調檔就只跑機構表
        pass
    try:
        direct_data = json.loads((CONFIG_DIR / "polls_direct.json").read_text(encoding="utf-8"))
        try:
            from .polldirect import validate as validate_direct_poll
        except ImportError:
            validate_direct_poll = None
        for record in direct_data.get("polls") or []:
            if validate_direct_poll and validate_direct_poll(record):
                continue
            region = record.get("region") or ""
            region = _matching_region_key(polls, region) or region
            values = {
                name: float(value)
                for name, value in (record.get("shares") or {}).items()
                if name and value
            }
            poll_date = (
                record.get("fieldwork_end")
                or record.get("fieldwork_start")
                or record.get("article_date")
                or ""
            )
            if not region or len(values) < 2 or not poll_date:
                continue
            converted = {
                "source": record.get("pollster") or record.get("source") or "",
                "date": poll_date,
                "values": values,
                "kind": "public",
                "tier": "direct",
                "matchup": tuple(sorted(values)),
                "sample_size": record.get("sample_size"),
                "evidence_quality": record.get("evidence_quality") or 1.0,
                "report_type": record.get("report_type") or "",
                "executor": record.get("executor") or "",
                "url": record.get("url") or "",
                "title": record.get("title") or "",
            }
            # 维基表与直抓稿可能同时收录同一份民调；同日、同数字只留直抓版。
            signature = (
                poll_date,
                tuple(sorted((name, round(value, 1)) for name, value in values.items())),
            )
            duplicate = next(
                (
                    item for item in polls.get(region, [])
                    if item.get("date") == signature[0]
                    and tuple(sorted(
                        (name, round(float(value), 1))
                        for name, value in (item.get("values") or {}).items()
                    )) == signature[1]
                ),
                None,
            )
            if duplicate is not None:
                polls[region].remove(duplicate)
            polls.setdefault(region, []).append(converted)
            direct_total += 1
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    for poll in [p for group in polls.values() for p in group]:
        poll.setdefault("tier", "institute")
    polls = _merge_region_keys(polls)
    polls = _dedupe_poll_sources(polls)
    polls, verification = cross_validate_polls(polls)
    return {
        "meta": {
            "source": f"https://zh.wikipedia.org/wiki/{urllib.parse.quote(PAGE)}",
            "note": (
                "各縣市長民調彙整；weighted 為機構權重×样本量×來源層級×時間衰減"
                f"（半衰期 {HALF_LIFE_DAYS:.0f} 天）"
            ),
            "counts": {
                "institute": institute_raw,
                "media": media_total,
                "direct": direct_total,
            },
            "verification": verification,
            "wiki_fetch_error": wiki_error,
            "updated_at": time.strftime("%Y-%m-%d"),
        },
        "regions": summarise(polls, as_of=as_of),
        "raw": polls,
    }


def _region_key(name: str) -> str:
    from .zhconv import to_simplified

    return to_simplified(name or "").replace("臺", "台").replace(" ", "")


def _matching_region_key(
    regions: dict[str, list[dict[str, Any]]],
    name: str,
) -> str:
    want = _region_key(name)
    return next((key for key in regions if _region_key(key) == want), "")


def _merge_region_keys(
    regions: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    merged: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    for name, items in regions.items():
        key = _region_key(name)
        label = labels.setdefault(key, name)
        merged.setdefault(label, []).extend(items)
    return merged


def _dedupe_poll_sources(
    regions: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """合并同一份民调的官网表格、原文直抓与多家新闻转述。"""
    priority = {"direct": 4, "institute": 3, "media_margin": 2, "media": 1}
    out: dict[str, list[dict[str, Any]]] = {}
    for region, rows in regions.items():
        direct = [
            row for row in rows
            if row.get("tier") == "direct" and row.get("date") and row.get("values")
        ]
        kept: list[dict[str, Any]] = []
        exact: dict[tuple, dict[str, Any]] = {}
        for row in rows:
            values = row.get("values") or {}
            if not values:
                continue
            numeric = [float(value) for value in values.values()]
            if max(numeric) < 15 or (min(numeric) < 10 and max(numeric) > 30):
                continue
            signature = tuple(sorted((name, round(float(value), 1)) for name, value in values.items()))
            copied = False
            for item in direct:
                if item is row:
                    continue
                other = item.get("values") or {}
                if (row.get("tier") or "") == "direct":
                    continue
                same_names = set(other) == set(values)
                echoed = sum(
                    1 for name, value in values.items()
                    if abs(float(value) - float(other.get(name, -999))) <= 0.1
                )
                other_signature = tuple(sorted(
                    (name, round(float(value), 1)) for name, value in other.items()
                ))
                if other_signature != signature and not (same_names and echoed >= 1):
                    continue
                try:
                    age = abs(
                        (date.fromisoformat(row["date"][:10])
                         - date.fromisoformat(item["date"][:10])).days
                    )
                except (KeyError, ValueError, TypeError):
                    continue
                if age <= 14:
                    copied = True
                    break
            if copied:
                continue
            key = (row.get("date") or "", signature)
            old = exact.get(key)
            if old is None or priority.get(row.get("tier") or "", 0) > priority.get(old.get("tier") or "", 0):
                exact[key] = row
        kept.extend(exact.values())
        out[region] = kept
    return out


VERIFICATION_STATUS_LABELS = {
    "consistent": "多源一致",
    "discrepancy": "存在差异",
    "conflict": "数据冲突",
    "original_only": "原始报告",
    "single_source": "单一来源",
    "unverified": "未交叉核验",
}


def _verification_name(value: Any) -> str:
    from .zhconv import to_simplified

    return to_simplified(str(value or "")).replace("臺", "台").replace(" ", "").strip()


def _verification_date(record: dict[str, Any]) -> date | None:
    value = (
        record.get("fieldwork_end")
        or record.get("date")
        or record.get("article_date")
        or record.get("published_date")
        or ""
    )
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _verification_pollster(record: dict[str, Any]) -> str:
    from .zhconv import to_simplified

    known = [
        name
        for name, _weight in SOURCE_WEIGHTS
    ] + ["循证", "循證", "锋灿", "鋒燦", "年代", "趋势", "趨勢", "大地"]

    def canonical(value: str) -> str:
        text = to_simplified(str(value or "")).replace("臺", "台")
        text = re.sub(r"\s+", "", text)
        for token in (
            "股份有限公司", "有限公司", "民意調查", "民意调查",
            "民調", "民调", "公司",
        ):
            text = text.replace(token, "")
        return text.casefold()

    combined = " ".join(
        str(record.get(key) or "")
        for key in ("pollster", "executor", "sponsor", "source", "title")
    )
    for name in known:
        if name and name.casefold() in combined.casefold():
            return canonical(name)
    for key in ("pollster", "executor", "sponsor", "source"):
        value = canonical(record.get(key) or "")
        if value:
            return value
    return canonical(str(record.get("title") or "")[:24])


def _verification_values(record: dict[str, Any]) -> dict[str, float]:
    raw = record.get("values")
    if not isinstance(raw, dict):
        raw = record.get("shares")
    if not isinstance(raw, dict):
        return {}
    values = {}
    for name, value in raw.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        key = _verification_name(name)
        if key and 0 < number <= 100:
            values[key] = number
    return values


def _verification_quality(record: dict[str, Any]) -> float:
    tier = str(record.get("tier") or "institute")
    quality = {
        "direct": 4.2,
        "institute": 3.0,
        "media_margin": 2.3,
        "media": 1.5,
    }.get(tier, 1.8)
    report_type = str(record.get("report_type") or "")
    if report_type == "full":
        quality += 0.5
    elif report_type == "partial":
        quality += 0.2
    if record.get("sample_size"):
        quality += 0.15
    if record.get("url"):
        quality += 0.1
    return round(quality, 3)


def _verification_source_key(record: dict[str, Any]) -> str:
    url = str(record.get("url") or "").strip()
    if url:
        host = urllib.parse.urlparse(url).netloc.casefold().removeprefix("www.")
        if host and "news.google.com" not in host:
            return host
    source = _verification_name(record.get("source") or record.get("pollster") or "")
    title = _verification_name(record.get("title") or "")[:48]
    return f"{source}|{title}" if source or title else ""


def _annotate_verification(
    record: dict[str, Any],
    *,
    status: str,
    note: str,
    source_count: int,
    diff_pp: float | None = None,
    factor: float = 1.0,
) -> None:
    record["verification_status"] = status
    record["verification_label"] = VERIFICATION_STATUS_LABELS.get(status, status)
    record["verification_note"] = note
    record["verification_sources"] = source_count
    record["verification_diff_pp"] = round(diff_pp, 1) if diff_pp is not None else None
    record["verification_weight_factor"] = factor
    record["_exclude_from_model"] = status in {"conflict", "discarded"}


def cross_validate_records(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按同一民调事件比对多来源支持度，并标注可信度。"""
    grouped: dict[tuple[str, tuple[str, ...], str], list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for record in records:
        values = _verification_values(record)
        when = _verification_date(record)
        pair = tuple(sorted(values))
        region = _verification_name(
            record.get("region") or record.get("_region") or ""
        )
        pollster = _verification_pollster(record)
        if len(values) < 2 or when is None or not region or not pollster:
            _annotate_verification(
                record,
                status="unverified",
                note="缺少调查日期、机构或完整对战数字，无法交叉核验",
                source_count=0,
                factor=0.45,
            )
            ungrouped.append(record)
            continue
        grouped.setdefault((region, pair, pollster), []).append(record)

    kept: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    status_counts: dict[str, int] = {}
    for event_records in grouped.values():
        event_records.sort(key=lambda item: _verification_date(item) or date.min)
        clusters: list[list[dict[str, Any]]] = []
        for record in event_records:
            record_date = _verification_date(record)
            if (
                clusters
                and record_date is not None
                and (record_date - (_verification_date(clusters[-1][0]) or record_date)).days <= 5
            ):
                clusters[-1].append(record)
            else:
                clusters.append([record])

        for cluster in clusters:
            anchor = max(cluster, key=_verification_quality)
            anchor_values = _verification_values(anchor)
            source_count = len({
                key for key in (_verification_source_key(item) for item in cluster)
                if key
            })
            anchor_quality = _verification_quality(anchor)
            for record in cluster:
                values = _verification_values(record)
                diff = max(
                    (
                        abs(values.get(name, 0.0) - anchor_values.get(name, 0.0))
                        for name in set(values) | set(anchor_values)
                    ),
                    default=0.0,
                )
                if record is anchor:
                    if source_count >= 2:
                        status, note, factor = (
                            "consistent",
                            f"已与 {source_count} 个来源交叉核验，采用最高质量报告",
                            1.0,
                        )
                    elif anchor_quality >= 4.0:
                        status, note, factor = (
                            "original_only",
                            "有原始报告和完整调查资料，但未找到独立来源复核",
                            1.0,
                        )
                    elif anchor_quality >= 3.0:
                        status, note, factor = (
                            "single_source",
                            "仅一个可识别来源，降低模型权重",
                            0.6,
                        )
                    else:
                        status, note, factor = (
                            "unverified",
                            "缺少高质量原始来源，降低模型权重",
                            0.45,
                        )
                elif diff > 8:
                    status, note, factor = (
                        "discarded",
                        f"与原始报告相差 {diff:.1f} 个百分点，超过8点，已舍弃",
                        0.0,
                    )
                elif diff > 5:
                    status, note, factor = (
                        "conflict",
                        f"与原始报告相差 {diff:.1f} 个百分点，排除出胜率计算",
                        0.0,
                    )
                elif diff > 2:
                    status, note, factor = (
                        "discrepancy",
                        f"与原始报告相差 {diff:.1f} 个百分点，保留但降低权重",
                        0.8,
                    )
                else:
                    status, note, factor = (
                        "consistent",
                        f"与原始报告相差 {diff:.1f} 个百分点，多源结果一致",
                        1.0,
                    )

                if status == "discarded":
                    _annotate_verification(
                        record,
                        status=status,
                        note=note,
                        source_count=source_count,
                        diff_pp=diff,
                        factor=0.0,
                    )
                    discarded.append(record)
                    continue
                _annotate_verification(
                    record,
                    status=status,
                    note=note,
                    source_count=source_count,
                    diff_pp=diff,
                    factor=factor,
                )
                status_counts[status] = status_counts.get(status, 0) + 1
                kept.append(record)

    for record in ungrouped:
        status = str(record.get("verification_status") or "unverified")
        status_counts[status] = status_counts.get(status, 0) + 1
        kept.append(record)

    return kept, {
        "verified": status_counts.get("consistent", 0),
        "discrepancy": status_counts.get("discrepancy", 0),
        "conflict": status_counts.get("conflict", 0),
        "original_only": status_counts.get("original_only", 0),
        "single_source": status_counts.get("single_source", 0),
        "unverified": status_counts.get("unverified", 0),
        "discarded": len(discarded),
        "status_counts": status_counts,
    }


def cross_validate_polls(
    polls: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    for region, rows in polls.items():
        for record in rows:
            record.setdefault("_region", region)
    flat = [record for rows in polls.values() for record in rows]
    _kept, summary = cross_validate_records(flat)
    discarded = {
        id(record)
        for record in flat
        if record.get("verification_status") == "discarded"
    }
    cleaned = {
        region: [record for record in rows if id(record) not in discarded]
        for region, rows in polls.items()
    }
    return {region: rows for region, rows in cleaned.items() if rows}, summary


def _write_coverage(data: dict[str, Any]) -> None:
    """同时保留「本轮原文直抓」与「合并长期汇总」的 22 县市覆盖状态。"""
    coverage_path = EXPORT_DIR / "poll_coverage.json"
    direct: dict[str, Any] = {}
    try:
        previous = json.loads(coverage_path.read_text(encoding="utf-8"))
        direct = previous.get("direct") or (
            previous if all(isinstance(v, dict) for v in previous.values()) else {}
        )
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    names: list[str] = []
    try:
        roster = json.loads((CONFIG_DIR / "roster.json").read_text(encoding="utf-8"))
        names.extend((roster.get("regions") or {}).keys())
    except (OSError, json.JSONDecodeError):
        pass
    try:
        regions_cfg = json.loads((CONFIG_DIR / "regions.json").read_text(encoding="utf-8"))
        names.extend(
            item.get("name") for item in (regions_cfg.get("regions") or [])
            if item.get("name")
        )
    except (OSError, json.JSONDecodeError):
        pass
    names.extend((data.get("raw") or {}).keys())
    uniq: dict[str, str] = {}
    for name in names:
        uniq.setdefault(_region_key(name), name)

    summaries = data.get("regions") or {}
    raw = data.get("raw") or {}
    summary_by_key = {_region_key(name): item for name, item in summaries.items()}
    raw_by_key = {_region_key(name): items for name, items in raw.items()}
    today = date.today()
    merged: dict[str, Any] = {}
    for key, label in sorted(uniq.items(), key=lambda item: item[1]):
        item = summary_by_key.get(key) or {}
        rows = raw_by_key.get(key) or []
        latest = item.get("effective_date") or ""
        source = item.get("latest_source") or ""
        if not latest:
            dated = sorted(
                (
                    row.get("date") or ""
                    for row in rows
                    if row.get("date") and (row.get("values") or row.get("gap"))
                ),
                reverse=True,
            )
            latest = dated[0] if dated else ""
            if dated:
                source = next(
                    (row.get("source") or "" for row in rows if row.get("date") == latest),
                    "",
                )
        try:
            age = (today - date.fromisoformat(latest[:10])).days
        except (ValueError, TypeError):
            age = None
        if latest and age is not None and age <= 14:
            status = "fresh"
        elif latest and age is not None and age <= 30:
            status = "recent"
        elif latest and age is not None and age <= 60:
            status = "aging"
        elif latest:
            status = "stale"
        else:
            status = "missing"
        merged[label] = {
            "status": status,
            "has_signal": bool(item.get("signal")),
            "polls": item.get("poll_count") or len(rows),
            "age_days": age,
            "latest": latest,
            "source": source,
        }
    export = {
        "note": "direct=本轮发布方原文直抓；merged=原文与长期汇总合并后的县市民调状态",
        "updated_at": time.strftime("%Y-%m-%d %H:%M"),
        "direct": direct,
        "merged": merged,
    }
    coverage_path.write_text(json.dumps(export, ensure_ascii=False, indent=2), encoding="utf-8")


def save_polls(data: dict[str, Any], path=None) -> str:
    target = path or (CONFIG_DIR / "polls.json")
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_coverage(data)
    return str(target)


_CACHE: dict[str, Any] = {"path": "", "mtime": 0, "data": {"regions": {}}}


def load_polls(path=None) -> dict[str, Any]:
    target = path or (CONFIG_DIR / "polls.json")
    if not target.exists():
        return {"regions": {}}
    stamp = target.stat().st_mtime_ns
    if _CACHE.get("path") == str(target) and _CACHE.get("mtime") == stamp:
        return _CACHE.get("data") or {"regions": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"regions": {}}
    _CACHE.update({"path": str(target), "mtime": stamp, "data": data})
    return data


def _match_region(regions: dict[str, Any], region_name: str) -> dict[str, Any] | None:
    from .zhconv import to_simplified

    want = to_simplified(region_name).replace("臺", "台")
    for name, data in regions.items():
        if to_simplified(name).replace("臺", "台") == want:
            return data
    return None


def region_polls(region_name: str) -> dict[str, Any] | None:
    return _match_region(load_polls().get("regions") or {}, region_name)


def candidate_poll(region_name: str, candidate_zh: str) -> dict[str, Any] | None:
    """取某候選人在該縣市民調中的最新值、均值與加權值。"""
    data = region_polls(region_name)
    if not data or not candidate_zh:
        return None
    latest = data.get("latest") or {}
    average = data.get("average") or {}
    weighted = data.get("weighted") or {}
    name = candidate_zh
    if name not in latest and name not in average and name not in weighted:
        # 允許「黃敏惠 / 黄敏惠」這類異體字差異
        alt = name.replace("黃", "黄")
        for key in (latest, average, weighted):
            hit = next((k for k in key if k.replace("黃", "黄") == alt), None)
            if hit:
                name = hit
                break
        else:
            return None
    return {
        "latest": latest.get(name),
        "average": average.get(name),
        "weighted": weighted.get(name),
        "date": data.get("effective_date") or data.get("latest_date") or "",
        "oldest_date": data.get("oldest_date") or "",
        "source": data.get("latest_source") or "",
        "poll_count": data.get("poll_count") or 0,
        "poll_total": data.get("poll_total") or 0,
        "signal": data.get("signal"),
    }


def region_signal(region_name: str) -> dict[str, Any] | None:
    data = region_polls(region_name)
    return (data or {}).get("signal")


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def matchup_signal(
    region_name: str,
    candidate_names: list[str],
    *,
    market_probs: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    """挑出與市場候選人組合最相符的民調對戰組合。

    同一個縣市常有「A－B」與「A－C」兩組民調，混在一起平均會產生
    誰都沒真的被問到的人造支持度，因此必須按組合分別取用。
    market_probs 有給時，會額外偏好「民調前兩名 = 市場前兩名」的組合，
    避免選到把黨內對手排進來的表格。
    """
    data = region_polls(region_name)
    if not data:
        return None
    want = {_norm(name) for name in candidate_names if name}
    market_top2: set[str] = set()
    if market_probs:
        ranked = sorted(market_probs.items(), key=lambda kv: -kv[1])[:2]
        market_top2 = {_norm(n) for n, _ in ranked}
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in data.get("matchups") or []:
        names = {_norm(n) for n in item.get("matchup") or []}
        if len(names & want) < 2:
            continue
        # 以重疊數為主、組合相似度為輔；再看樣本數與新鮮度
        score = len(names & want) + _jaccard(names, want)
        if market_top2:
            poll_top2 = [
                _norm(n) for n, _ in
                sorted((item.get("weighted") or {}).items(), key=lambda kv: -kv[1])[:2]
            ]
            score += 0.8 * len(market_top2 & set(poll_top2))
        signal = item.get("signal")
        if not signal:
            continue
        sources = float(item.get("effective_samples") or 0)
        rank = (score, min(sources, 5.0), item.get("effective_date") or "")
        if best is None or rank > best_score:
            best, best_score = {**signal, **{
                "matchup": item.get("matchup"),
                "effective_samples": item.get("effective_samples"),
                "freshness": item.get("freshness"),
                "effective_date": item.get("effective_date"),
                "poll_count": item.get("poll_count"),
                "weighted": item.get("weighted"),
            }}, rank
    return best


def _norm(name: str) -> str:
    from .zhconv import to_simplified

    return to_simplified(name or "").replace("臺", "台").replace("黃", "黄").strip()


def candidate_matchup(region_name: str, candidate_zh: str) -> dict[str, Any] | None:
    """找出包含該候選人、且樣本最多的對戰組合（用於單一候選人彈窗）。"""
    data = region_polls(region_name)
    if not data or not candidate_zh:
        return None
    want = _norm(candidate_zh)
    best: dict[str, Any] | None = None
    best_rank: tuple[str, float] = ("", 0.0)
    for item in data.get("matchups") or []:
        if not item.get("signal"):
            continue
        names = {_norm(n) for n in item.get("matchup") or []}
        if want not in names:
            continue
        # 彈窗給人看的，優先顯示最新那份民調，其次才看樣本多寡
        rank = (item.get("effective_date") or "", float(item.get("effective_samples") or 0))
        if rank > best_rank:
            best, best_rank = item, rank
    return best
