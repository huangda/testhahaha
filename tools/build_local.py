"""建立22县市乡镇市长 / 里长基础资料。

来源：

1. 中选会「115年鄉鎮市長選舉候選人登記彙總表」（PDF，2026-09-07）
   → 宜兰县 12 个乡镇市、每位候选人的姓名、推荐政党、登记日期
2. 中选会「115年直轄市山地原住民區長選舉候選人登記彙總表」（PDF）
   → 新北市乌来区（新北唯一的民选区长）
3. 维基百科各乡镇市／区条目的「区划」栏
   → 每个乡镇市／区有几个村里（＝里长应选名额）
4. 维基百科「2026年中華民國村里長選舉」
   → 新北市 1,032 里、宜兰县 233 村里（总数校验）

里长候选人名册要在选举公报（投票前约两周）才会完整公布，
目前只做到「每区里数 + 全县市总数」，页面会明确标注。

输出：config/local_2026.json

用法：
    python -m tools.build_local [--force] [--print]
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

PDFS = {
    "town_city": (
        "https://web.cec.gov.tw/api/file/a7b4f3d3-dad3-4e61-9036-2cd21cf29d92.pdf",
        "town_city.pdf",
    ),
    "town_county": (
        "https://web.cec.gov.tw/api/file/1278f66e-1d15-4ecf-aeeb-9ea5cba61f00.pdf",
        "town_county.pdf",
    ),
}

# 已核实村里数的重点县市清单（新北 29 区、宜兰 12 乡镇市）
TOWNS = {
    "新北市": [
        "板橋區", "三重區", "中和區", "永和區", "新莊區", "新店區", "樹林區", "鶯歌區",
        "三峽區", "淡水區", "汐止區", "瑞芳區", "土城區", "蘆洲區", "五股區", "泰山區",
        "林口區", "深坑區", "石碇區", "坪林區", "三芝區", "石門區", "八里區", "平溪區",
        "雙溪區", "貢寮區", "金山區", "萬里區", "烏來區",
    ],
    "宜蘭縣": [
        "宜蘭市", "羅東鎮", "蘇澳鎮", "頭城鎮", "礁溪鄉", "員山鄉",
        "壯圍鄉", "五結鄉", "冬山鄉", "三星鄉", "大同鄉", "南澳鄉",
    ],
}

VILLAGE_TOTAL = {"新北市": 1032, "宜蘭縣": 233}  # 2026 村里长选举应选名额
REGION_CODES = {
    "臺北市": "TPE", "新北市": "NTPC", "桃園市": "TYN", "臺中市": "TXG",
    "臺南市": "TNN", "高雄市": "KHH", "基隆市": "KEL", "新竹市": "HSC",
    "嘉義市": "CYI", "新竹縣": "HSQ", "苗栗縣": "MIA", "彰化縣": "CHA",
    "南投縣": "NTO", "雲林縣": "YUN", "嘉義縣": "CYQ", "屏東縣": "PIF",
    "宜蘭縣": "ILA", "花蓮縣": "HUN", "臺東縣": "TTT", "澎湖縣": "PEN",
    "金門縣": "KIN", "連江縣": "LNN",
}
MUNICIPALITIES = {"臺北市", "新北市", "桃園市", "臺中市", "臺南市", "高雄市"}
PROVINCIAL_CITIES = {"基隆市", "新竹市", "嘉義市"}


def load_pdf(kind: str, force: bool) -> str:
    ensure_dirs()
    CACHE.mkdir(parents=True, exist_ok=True)
    url, name = PDFS[kind]
    path = CACHE / name
    if force or not path.exists() or path.stat().st_size < 1024:
        path.write_bytes(fetch_bytes(url, timeout=90, retries=2, headers=UA))
    reader = PdfReader(str(path))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def parse_mayors(text: str, region: str) -> dict[str, list[dict]]:
    """从登记汇总表取出该县市每个乡镇市的候选人。"""
    out: dict[str, list[dict]] = {}
    pattern = re.compile(
        rf"^{region}(\S+?)\s+(\d{{3}}/\d{{2}}/\d{{2}})\s+(\S+)\s*(.*)$", re.M
    )
    for m in pattern.finditer(text):
        town, date, name, party = m.group(1), m.group(2), m.group(3), m.group(4).strip()
        if town in {"選舉區", "製表日期"} or region not in REGION_CODES:
            continue
        out.setdefault(town, []).append({
            "name": name,
            "party": "無黨籍" if party in ("無", "") else party,
            "date": date,
        })
    return out


# ── 村里数 ────────────────────────────────────────────────────────────────

def wiki_batch(titles: list[str]) -> dict[str, str]:
    """批量取 wikitext，回传 {实际标题: 内容}。"""
    out: dict[str, str] = {}
    for i in range(0, len(titles), 10):
        chunk = titles[i: i + 10]
        params = urllib.parse.urlencode({
            "action": "query", "format": "json", "formatversion": "2",
            "prop": "revisions", "rvprop": "content", "rvslots": "main",
            "titles": "|".join(chunk), "redirects": "1",
        })
        data = fetch_json(f"{WIKI_API}?{params}", timeout=60, retries=2, headers=UA)
        pages = (data.get("query") or {}).get("pages") or []
        for page in pages:
            if page.get("missing"):
                continue
            revs = page.get("revisions") or []
            if revs:
                out[page.get("title", "")] = (
                    (revs[0].get("slots") or {}).get("main", {}).get("content") or ""
                )
        time.sleep(0.4)
    return out


def village_counts(region: str, force: bool) -> dict[str, int]:
    ensure_dirs()
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"villages_{region}.json"
    if path.exists() and not force:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass

    names = TOWNS[region]
    # 部分条目带消歧义后缀，两种写法都试
    cands = names + [f"{n} (台灣)" for n in names]
    pages = wiki_batch(cands)
    counts: dict[str, int] = {}
    for name in names:
        # 「板橋區」「大同鄉」这类条目是消歧义页，要挑真正有「區劃」栏的那一版
        pool = [pages.get(k) or "" for k in (name, f"{name} (台灣)")]
        pool += [v for k, v in pages.items() if k.startswith(name) and v not in pool]
        text = next(
            (t for t in pool if len(t) > 800 and re.search(r"(?m)^\|\s*區劃\s*=", t)),
            "",
        )
        m = re.search(r"(?m)^\|\s*區劃\s*=\s*([^|\n]+)", text)
        if not m:
            continue
        seg = m.group(1)
        n = re.search(r"([\d,]+)\s*\[\[\s*(?:村里|里|村)", seg)
        if not n:
            n = re.search(r"([\d,]+)\s*(?:里|村)", seg)
        if n:
            counts[name] = int(n.group(1).replace(",", ""))
    path.write_text(json.dumps(counts, ensure_ascii=False), encoding="utf-8")
    return counts


def build(force: bool) -> dict:
    city_text = load_pdf("town_city", force)
    county_text = load_pdf("town_county", force)

    regions: dict[str, dict] = {}
    for region in REGION_CODES:
        if region in MUNICIPALITIES:
            text = county_text
        elif region in PROVINCIAL_CITIES:
            text = ""
        else:
            text = city_text
        mayors = parse_mayors(text, region)
        counts = village_counts(region, force) if region in TOWNS else {}
        town_names = TOWNS.get(region) or sorted(mayors)
        towns = []
        for name in town_names:
            cands = mayors.get(name, [])
            towns.append({
                "name": name,
                "villages": counts.get(name, 0),
                "candidates": cands,
                "parties": {
                    p: sum(1 for c in cands if c["party"] == p)
                    for p in sorted({c["party"] for c in cands})
                },
            })
        regions[region] = {
            "code": REGION_CODES[region],
            "towns": towns,
            "mayor_seats": sum(1 for t in towns if t["villages"] or t["candidates"]),
            "mayor_candidates": sum(len(t["candidates"]) for t in towns),
            "villages_total": VILLAGE_TOTAL.get(region, 0),
            "villages_counted": sum(t["villages"] for t in towns),
        }

    return {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M"),
            "note": "乡镇市长：中选会 115 年候选人登记汇总表（姓名/政党/登记日期，2026-09-07）。"
                    "新北、宜兰已有每区里数；其余县市先列乡镇市区与乡镇市长候选人，村里数待后续官方资料补齐。",
            "sources": {
                "town_city": PDFS["town_city"][0],
                "town_county": PDFS["town_county"][0],
                "villages": "zh.wikipedia 各乡镇市区条目「区划」栏 + 2026年中華民國村里長選舉",
            },
        },
        "regions": regions,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成22县市乡镇市长与里长基础资料")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--print", action="store_true", dest="dump")
    args = parser.parse_args()

    data = build(args.force)
    out = CONFIG_DIR / "local_2026.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    for region, info in data["regions"].items():
        towns = [t for t in info["towns"] if t["candidates"]]
        print(f"[local] {region}: 乡镇市长选区 {len(towns)}，候选人 {info['mayor_candidates']} 人；"
              f"里数 {info['villages_counted']}/{info['villages_total']}")
        if args.dump:
            for t in info["towns"]:
                head = "、".join(f"{c['name']}({c['party']})" for c in t["candidates"]) or "无登记"
                print(f"   {t['name']:<5} {t['villages']:>4} 里  {head}")
    print(f"[local] 已写入 {out}（{out.stat().st_size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
