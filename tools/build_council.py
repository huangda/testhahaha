"""建立22县市议员选举资料。

三个来源合并：

1. 中选会「115年直辖市／县市议员选举 政党推荐候选人登记情形汇总表」（PDF，2026-09-07）
   → 每个选区的**应选名额、各党推荐人数、未经政党推荐人数、登记合计**（官方数字）
2. 维基百科「2026年地方公职人员选举 国民党／民进党提名结果列表」
   → 具名提名名单，含任次、派系、性别、备注
3. 维基百科「2026年中華民國直轄市議員及縣市議員選舉」
   → 2022 年各县市议员政党当选席次（基线）

输出：config/council_2026.json

用法：
    python -m tools.build_council            # 用缓存，缺档才下载
    python -m tools.build_council --force    # 全部重抓

依赖：pypdf、beautifulsoup4（只在生成资料时需要，运行时不需要）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pypdf import PdfReader  # noqa: E402

from tem.http import fetch_bytes, fetch_json  # noqa: E402
from tem.paths import CONFIG_DIR, DATA_DIR, ensure_dirs  # noqa: E402

UA = {"User-Agent": "TEM-election-monitor/1.0 (local research tool)"}
WIKI_API = "https://zh.wikipedia.org/w/api.php"
CACHE = DATA_DIR / "cec"

TARGETS = {
    "臺北市": {"code": "TPE", "pdf": "city"},
    "新北市": {"code": "NTPC", "pdf": "city"},
    "桃園市": {"code": "TYN", "pdf": "city"},
    "臺中市": {"code": "TXG", "pdf": "city"},
    "臺南市": {"code": "TNN", "pdf": "city"},
    "高雄市": {"code": "KHH", "pdf": "city"},
    "基隆市": {"code": "KEL", "pdf": "county"},
    "新竹市": {"code": "HSC", "pdf": "county"},
    "嘉義市": {"code": "CYI", "pdf": "county"},
    "新竹縣": {"code": "HSQ", "pdf": "county"},
    "苗栗縣": {"code": "MIA", "pdf": "county"},
    "彰化縣": {"code": "CHA", "pdf": "county"},
    "南投縣": {"code": "NTO", "pdf": "county"},
    "雲林縣": {"code": "YUN", "pdf": "county"},
    "嘉義縣": {"code": "CYQ", "pdf": "county"},
    "屏東縣": {"code": "PIF", "pdf": "county"},
    "宜蘭縣": {"code": "ILA", "pdf": "county"},
    "花蓮縣": {"code": "HUN", "pdf": "county"},
    "臺東縣": {"code": "TTT", "pdf": "county"},
    "澎湖縣": {"code": "PEN", "pdf": "county"},
    "金門縣": {"code": "KIN", "pdf": "county"},
    "連江縣": {"code": "LNN", "pdf": "county"},
}
REGION_PATTERN = "|".join(re.escape(name) for name in TARGETS)

CEC_FILES = {
    "city": (
        "https://web.cec.gov.tw/api/file/7a8eb38b-76c3-41c1-9d54-0dd55951a182.pdf",
        "council_city.pdf",
    ),
    "county": (
        "https://web.cec.gov.tw/api/file/ee74ed96-d621-4ee2-b95f-32b6ff6a3d62.pdf",
        "council_county.pdf",
    ),
}

# 表头顺序（依 PDF 首页文字判读），把数字对回政党
CEC_PARTIES = {
    "city": [
        "中國國民黨", "勞動黨", "民主進步黨", "新黨", "台灣綠黨", "親民黨", "台聯黨",
        "永久和平自然法", "天宙和平統一家庭黨", "時代力量", "社會民主黨", "台灣基進",
        "左翼聯盟", "台灣民眾黨", "小民參政歐巴桑聯盟", "司法改革黨", "愛民黨",
    ],
    "county": [
        "中國國民黨", "勞動黨", "民主進步黨", "新黨", "親民黨", "台聯黨", "台灣進步黨",
        "時代力量", "台灣基進", "台灣民眾黨", "小民參政歐巴桑聯盟", "正神名黨", "更生黨",
    ],
}

NOMINATION_PAGES = [
    ("中國國民黨", "2026年地方公職人員選舉中國國民黨提名結果列表"),
    ("民主進步黨", "2026年地方公職人員選舉民主進步黨提名結果列表"),
]

OVERVIEW_PAGE = "2026年中華民國直轄市議員及縣市議員選舉"

# 2022 当选席次表的栏位顺序（應選名額之后）
BASELINE_COLUMNS = [
    "中國國民黨", "民主進步黨", "台灣民眾黨", "無黨團結聯盟", "時代力量",
    "台灣團結聯盟", "親民黨", "台灣基進", "其他", "無黨籍",
]

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
          "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13,
          "十四": 14, "十五": 15, "十六": 16}


def cn_to_int(text: str) -> int:
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    if text in CN_NUM:
        return CN_NUM[text]
    if text.startswith("二十"):
        return 20 + CN_NUM.get(text[2:], 0)
    if text.startswith("十"):
        return 10 + CN_NUM.get(text[1:], 0)
    return 0


# ── 1. 中选会汇总表 ────────────────────────────────────────────────────────

def load_cec_pdf(kind: str, force: bool) -> Path:
    ensure_dirs()
    CACHE.mkdir(parents=True, exist_ok=True)
    url, name = CEC_FILES[kind]
    path = CACHE / name
    if force or not path.exists() or path.stat().st_size < 1024:
        path.write_bytes(fetch_bytes(url, timeout=90, retries=2, headers=UA))
    return path


def parse_cec(kind: str, force: bool) -> dict[tuple[str, int], dict]:
    reader = PdfReader(str(load_cec_pdf(kind, force)))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    parties = CEC_PARTIES[kind]
    out: dict[tuple[str, int], dict] = {}
    pattern = re.compile(rf"^({REGION_PATTERN})第(\d+)選舉區\s+([\d\s]+)$", re.M)
    for m in pattern.finditer(text):
        region, no, nums = m.group(1), int(m.group(2)), m.group(3).split()
        if len(nums) < len(parties) + 3:
            continue
        counts = [int(x) for x in nums[: len(parties)]]
        rest = [int(x) for x in nums[len(parties):]]
        subtotal, unaffiliated, total = rest[0], rest[1], rest[2]
        seats = rest[3] if len(rest) > 3 else 0
        if sum(counts) != subtotal or subtotal + unaffiliated != total:
            print(f"  [warn] {region}第{no}区 数字校验失败：{nums}")
        out[(region, no)] = {
            "party_counts": {p: c for p, c in zip(parties, counts) if c},
            "recommended": subtotal,
            "unaffiliated": unaffiliated,
            "registered": total,
            "seats": seats,
        }
    return out


# ── 2. 维基提名名单（wikitext） ────────────────────────────────────────────

def wiki_wikitext(page: str, force: bool) -> str:
    ensure_dirs()
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / (re.sub(r"[^\w\u4e00-\u9fff]+", "_", page)[:80] + ".wiki")
    if force or not path.exists() or path.stat().st_size < 5000:
        params = urllib.parse.urlencode(
            {"action": "parse", "page": page, "prop": "wikitext", "format": "json",
             "formatversion": "2", "redirects": "1"}
        )
        data = fetch_json(f"{WIKI_API}?{params}", timeout=60, retries=2, headers=UA)
        path.write_text(((data.get("parse") or {}).get("wikitext")) or "", encoding="utf-8")
        time.sleep(0.5)
    return path.read_text(encoding="utf-8")


HEAD = re.compile(r"^={3,5}\s*(.+?)\s*={3,5}\s*$", re.M)
ATTR_PREFIX = re.compile(r'^\s*((?:[a-zA-Z]+\s*=\s*(?:"[^"]*"|[^\s|]+)\s*)+)\|')
ROWSPAN = re.compile(r'rowspan\s*=\s*"?(\d+)"?')
LINK = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")
TAG = re.compile(r"<[^>]+>")


def split_sections(wikitext: str):
    marks = list(HEAD.finditer(wikitext))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(wikitext)
        yield m.group(1).strip(), wikitext[m.end():end]


def clean(text: str) -> str:
    text = LINK.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)
    text = re.sub(r"-\{([^}]*)\}-", r"\1", text)  # 繁简转换标记 -{杰}-
    text = re.sub(r"-\{([^}|]*)\|([^}]*)\}-", r"\2", text)
    text = text.replace("'''", "").replace("''", "")
    text = TAG.sub("", text)
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    return re.sub(r"\s+", " ", text).strip(" |")


def split_cell(raw: str) -> tuple[str, int]:
    cell = raw.strip()
    rowspan = 1
    m = ATTR_PREFIX.match(cell)
    if m:
        mm = ROWSPAN.search(m.group(1))
        if mm:
            rowspan = int(mm.group(1))
        cell = cell[m.end():]
    return cell, rowspan


def parse_district(body: str, party: str) -> dict:
    seats_est = int((re.search(r"應選席次[：:]\s*(\d+)", body) or [0, 0])[1])
    nom_line = (re.search(r"提名席次[：:]\s*([^\n]*)", body) or [0, ""])[1]
    nom_est = sum(int(x) for x in re.findall(r"\d+", nom_line[:12]))  # 「1+1」＝2

    candidates: list[dict] = []
    pending = 0
    for raw in re.split(r"\n\|-", body):
        if raw.count("\n|") < 2:
            # 少数选区整张表只用一格写「某某報准參選」
            m = LINK.search(raw)
            if m and nom_est >= 1:
                nm = clean(m.group(2) or m.group(1))
                tail = clean(raw[m.end():])
                if nm and nm not in [c["name"] for c in candidates]:
                    candidates.append({
                        "name": nm, "party": party, "terms": 0, "gender": "",
                        "faction": "", "note": tail or "報准參選", "nominated": True,
                        "wiki_title": (m.group(1) or "").strip(),
                    })
            continue
        lines = [ln for ln in raw.split("\n") if ln.startswith("|") and not ln.startswith("||")]
        cells: list[tuple[str, int]] = []
        for ln in lines:
            for piece in re.split(r"\|\|", ln[1:]):
                cells.append(split_cell(piece))
        if not cells:
            continue

        is_y = "{{Y}}" in raw
        y_span = 1
        if is_y:
            for content, span in cells:
                if "{{Y}}" in content:
                    y_span = span
                    break
        if pending > 0:
            nominated = True
            pending -= 1
        elif is_y:
            nominated = True
            pending = max(y_span - 1, 0)
        else:
            nominated = False

        first_value = clean(cells[0][0])
        has_terms_column = bool(re.fullmatch(r"\d+", first_value))
        raw_name = cells[1][0] if has_terms_column and len(cells) > 1 else cells[0][0]
        name = ""
        wiki_title = ""
        m = LINK.search(raw_name)
        if m:
            name = (m.group(2) or m.group(1)).strip()
            wiki_title = (m.group(1) or "").strip()
        if not name:
            name = clean(raw_name)
        name = re.sub(r"<br\s*/?>", " ", name)
        name = re.sub(r"<[^>]+>", "", name).strip()
        name = re.sub(r"\s+", " ", name)
        name = clean(name)
        if not name or len(name) > 30 or re.fullmatch(r"[\d.\s]+", name):
            continue

        terms = 0
        if has_terms_column:
            terms = int(first_value)

        gender = next((clean(c) for c, _ in cells if clean(c) in ("男", "女")), "")
        faction = ""
        for idx, (content, _span) in enumerate(cells):
            if clean(content) in ("男", "女") and idx + 1 < len(cells):
                faction = clean(cells[idx + 1][0])
                break
        note = ""
        for content, _span in reversed(cells):
            text = clean(content)
            if not text or "{{Y}}" in content:
                continue
            if text in (name, str(terms), faction, "男", "女") or re.fullmatch(r"[\d.]+%?", text):
                continue
            note = text
            break
        if "無黨" in note and "結盟" in note:
            party_label = "無黨籍（結盟）"
        else:
            party_label = party
        candidates.append({
            "name": name,
            "party": party_label,
            "terms": terms,
            "gender": gender,
            "faction": faction,
            "note": note,
            "nominated": nominated,
            "wiki_title": wiki_title,
        })

    # 有些选区整张表用「報准參選」一行带过，没有 {{Y}} 标记
    if nom_est > 0 and not any(c["nominated"] for c in candidates):
        for c in candidates[:nom_est]:
            c["nominated"] = True

    return {"seats_est": seats_est, "nom_est": nom_est, "candidates": candidates}


def reclassify(candidates: list[dict], party_counts: dict[str, int]) -> None:
    """把「报准／报备参选」「与某党结盟但以无党籍登记」的人移出政党名单。

    CEC 的「政党推荐人数」只算正式推荐；报准、报备、结盟参选都计入「未经政党推荐」。
    """
    for c in candidates:
        blob = f"{c.get('faction', '')} {c.get('note', '')}"
        if "無黨" in blob:
            c["party"] = "無黨籍（結盟）"
    for party, count in party_counts.items():
        own = [c for c in candidates if c["party"] == party]
        extra = len(own) - count
        if extra <= 0:
            continue
        for c in own:
            if extra <= 0:
                break
            if "報備" in c.get("note", "") or "報准" in c.get("note", ""):
                c["party"] = "無黨籍（報准）"
                extra -= 1


def parse_nominations(force: bool) -> dict[tuple[str, int], dict[str, dict]]:
    out: dict[tuple[str, int], dict[str, dict]] = {}
    for party, page in NOMINATION_PAGES:
        text = wiki_wikitext(page, force)
        for title, body in split_sections(text):
            m = re.match(
                rf"^({REGION_PATTERN})第([一二三四五六七八九十]+)選舉區\s*[(（](.+?)[)）]$",
                title,
            )
            if not m:
                continue
            region, no, area = m.group(1), cn_to_int(m.group(2)), m.group(3)
            parsed = parse_district(body, party)
            bucket = out.setdefault((region, no), {})
            bucket[party] = {**parsed, "area": area}
    return out


# ── 3. 2022 基线 ──────────────────────────────────────────────────────────

def parse_baseline(force: bool) -> dict[str, dict]:
    text = wiki_wikitext(OVERVIEW_PAGE, force)
    out: dict[str, dict] = {}
    for region in TARGETS:
        m = re.search(
            r"^!\s*\[\[[^\]]*\|" + re.escape(region) + r"\]\]\s*\n((?:\|[^\n]*\n)+)",
            text,
            re.M,
        )
        if not m:
            continue
        nums = [int(x) for x in re.findall(r"\|\s*'*(\d+)'*\s*", m.group(1))]
        if len(nums) < 2:
            continue
        total = nums[0]
        seats = dict(zip(BASELINE_COLUMNS, nums[1:]))
        out[region] = {"total": total, "seats": {k: v for k, v in seats.items() if v}}
    return out


# ── 4. 组装 ───────────────────────────────────────────────────────────────

def build(force: bool) -> dict:
    cec = {kind: parse_cec(kind, force) for kind in ("city", "county")}
    nominations = parse_nominations(force)
    baseline = parse_baseline(force)

    regions: dict[str, dict] = {}
    for region, meta in TARGETS.items():
        table = cec[meta["pdf"]]
        districts = []
        for (r, no), info in sorted(table.items()):
            if r != region:
                continue
            wiki = nominations.get((region, no), {})
            cands: list[dict] = []
            for party in ("中國國民黨", "民主進步黨"):
                entry = wiki.get(party) or {}
                picked = [c for c in entry.get("candidates", []) if c["nominated"]]
                expected = info["party_counts"].get(party, 0)
                own = [c for c in picked if c["party"] == party]
                if expected and len(own) != expected:
                    print(f"  [note] {region}第{no}区 {party}: 具名 {len(picked)} vs 官方 {expected}")
                cands.extend({k: v for k, v in c.items() if k != "nominated"} for c in picked)
            reclassify(cands, info["party_counts"])
            area = next((e.get("area") for e in wiki.values() if e.get("area")), "")
            districts.append({
                "no": no,
                "name": f"{region}第{no}選舉區",
                "area": area,
                "seats": info["seats"],
                "registered": info["registered"],
                "recommended": info["recommended"],
                "unaffiliated": info["unaffiliated"],
                "party_counts": info["party_counts"],
                "candidates": cands,
            })
        regions[region] = {
            "code": meta["code"],
            "districts": districts,
            "total_seats": sum(d["seats"] for d in districts),
            "registered_total": sum(d["registered"] for d in districts),
            "baseline_2022": baseline.get(region, {}),
        }

    return {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M"),
            "note": "议员：应选名额与各党推荐人数来自中选会 115 年汇总表（2026-09-07）；"
                    "具名名单来自维基百科国民党／民进党提名结果列表；2022 席次为基线。",
            "sources": {
                "cec_city": CEC_FILES["city"][0],
                "cec_county": CEC_FILES["county"][0],
                "wiki_nomination": [p for _party, p in NOMINATION_PAGES],
                "wiki_overview": OVERVIEW_PAGE,
            },
        },
        "regions": regions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成22县市议员选举资料")
    parser.add_argument("--force", action="store_true", help="重新下载所有来源")
    parser.add_argument("--print", action="store_true", dest="dump", help="打印摘要")
    args = parser.parse_args()

    data = build(args.force)
    out = CONFIG_DIR / "council_2026.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    for region, info in data["regions"].items():
        named = sum(len(d["candidates"]) for d in info["districts"])
        print(f"[council] {region}: {len(info['districts'])} 个选区，应选 {info['total_seats']} 席，"
              f"登记 {info['registered_total']} 人，具名 {named} 人")
    print(f"[council] 已写入 {out}（{out.stat().st_size / 1024:.1f} KB）")

    if args.dump:
        for region, info in data["regions"].items():
            print(f"\n== {region} 上届 {info['baseline_2022']}")
            for d in info["districts"]:
                print(f"  {d['no']:>2}. {d['area'] or '—'} | 应选 {d['seats']} 登记 {d['registered']} "
                      f"| {d['party_counts']} 无党 {d['unaffiliated']}")
                for c in d["candidates"]:
                    print(f"        {c['party']} {c['name']} 任次{c['terms']} {c['faction']} {c['note']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
