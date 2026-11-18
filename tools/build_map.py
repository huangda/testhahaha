"""把台湾 22 县市边界（GeoJSON）压成前端用的 SVG 路径。

来源：ronnywang/twgeojson 的 `twcounty2010.2.json`（已简化过一版）。
这里再做两件事：
  1. 拉直折线（Douglas-Peucker），删掉面积过小的离岛，缩小体积；
  2. 等距圆柱投影 + 纬度收敛，直接输出 SVG path 的 `d`。

输出：`web/static/taiwan-map.js`，内容为 `window.TW_MAP = {...}`，
离线可用，便携包不需要再连网。

用法：python -m tools.build_map [--tolerance 0.004] [--min-area 0.0006]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tem.http import fetch_json  # noqa: E402
from tem.paths import DATA_DIR, ensure_dirs  # noqa: E402

SOURCE_URL = "https://raw.githubusercontent.com/ronnywang/twgeojson/master/twcounty2010.2.json"
CACHE = DATA_DIR / "geo" / "twcounty2010.2.json"
OUTPUT = ROOT / "web" / "static" / "taiwan-map.js"
REGIONS_JSON = ROOT / "config" / "regions.json"

# 2010 年的旧名，对到现在的县市名
ALIASES = {"桃園縣": "桃園市"}

# 金门、马祖离本岛很远，按真实位置画会把本岛压得很小，改成左下角图框
INSETS = ["金門縣", "連江縣"]
INSET_BOX = (132.0, 132.0)  # 每个图框的宽、高
INSET_GAP = 14.0
PAD = 16.0

VIEW_W = 1000.0


def region_codes() -> dict[str, str]:
    """config/regions.json 的 {县市名: 区域码}，供前端直接对号，不靠字串比对。"""
    try:
        data = json.loads(REGIONS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for item in data.get("regions") or []:
        name = ALIASES.get(item.get("name") or "", item.get("name") or "")
        if name and item.get("code"):
            out[name] = item["code"]
        for alias in item.get("aliases") or []:
            alias = ALIASES.get(alias, alias)
            if len(alias) in (2, 3) and alias not in out and item.get("code"):
                out[alias] = item["code"]
    return out


def _ring_area(ring: list[list[float]]) -> float:
    area = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2


def _rdp(points: list[list[float]], tol: float) -> list[list[float]]:
    """Douglas-Peucker 拉直。"""
    if len(points) < 3:
        return points
    first, last = points[0], points[-1]
    dx, dy = last[0] - first[0], last[1] - first[1]
    denom = math.hypot(dx, dy)
    worst, index = 0.0, 0
    for i in range(1, len(points) - 1):
        px, py = points[i][0], points[i][1]
        if denom == 0:
            dist = math.hypot(px - first[0], py - first[1])
        else:
            dist = abs(dy * px - dx * py + last[0] * first[1] - last[1] * first[0]) / denom
        if dist > worst:
            worst, index = dist, i
    if worst <= tol:
        return [first, last]
    left = _rdp(points[: index + 1], tol)
    right = _rdp(points[index:], tol)
    return left[:-1] + right


def _rings(geometry: dict) -> list[list[list[float]]]:
    """把 Polygon / MultiPolygon 统一成外环列表（忽略内环，县市边界没有飞地洞）。"""
    kind = geometry.get("type")
    coords = geometry.get("coordinates") or []
    if kind == "Polygon":
        return [coords[0]] if coords else []
    if kind == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def load_source(force: bool = False) -> dict:
    ensure_dirs()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    if CACHE.exists() and not force:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    data = fetch_json(SOURCE_URL, timeout=60, retries=2, headers={"User-Agent": "TEM-election-monitor/1.0"})
    CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def build(tolerance: float, min_area: float) -> dict:
    data = load_source()
    codes = region_codes()

    rings: list[tuple[str, list[list[float]]]] = []
    for feature in data.get("features") or []:
        props = feature.get("properties") or {}
        name = ALIASES.get(props.get("county") or "", props.get("county") or "")
        for ring in _rings(feature.get("geometry") or {}):
            if _ring_area(ring) < min_area:
                continue
            rings.append((name, _rdp(ring, tolerance)))

    main = [(n, ring) for n, ring in rings if n not in INSETS]
    inset_rings = [(n, ring) for n, ring in rings if n in INSETS]
    if not main:
        main, inset_rings = inset_rings, []

    lngs = [pt[0] for _n, ring in main for pt in ring]
    lats = [pt[1] for _n, ring in main for pt in ring]
    lat0 = (min(lats) + max(lats)) / 2
    kx = math.cos(math.radians(lat0))

    def projector(points: list[list[float]], box_x: float, box_y: float, box_w: float, box_h: float):
        """把一组经纬度等比塞进指定方框，回传该组的转换函数。"""
        px = [pt[0] * kx for pt in points]
        py = [-pt[1] for pt in points]
        span_x, span_y = (max(px) - min(px)) or 1e-9, (max(py) - min(py)) or 1e-9
        k = min(box_w / span_x, box_h / span_y)
        ox = box_x + (box_w - span_x * k) / 2
        oy = box_y + (box_h - span_y * k) / 2
        return lambda lng, lat: ((lng * kx - min(px)) * k + ox, (-lat - min(py)) * k + oy)

    all_main_pts = [pt for _n, ring in main for pt in ring]
    body_w = VIEW_W - PAD * 2
    inset_row_h = INSET_BOX[1] + PAD if inset_rings else 0.0
    # 先用量级很大的高度探一次，只为了知道本岛的高宽比
    probe = projector(all_main_pts, 0.0, 0.0, body_w, 1e9)
    probe_y = [probe(pt[0], pt[1])[1] for pt in all_main_pts]
    body_h = max(probe_y) - min(probe_y)
    height = round(body_h + PAD * 2 + inset_row_h, 1)
    main_proj = projector(all_main_pts, PAD, PAD, body_w, body_h)

    regions: dict[str, list[str]] = {}
    labels: dict[str, tuple[float, float, float]] = {}
    inset_boxes: list[dict] = []

    def emit(name: str, ring, to_svg):
        pts = [to_svg(pt[0], pt[1]) for pt in ring]
        if len(pts) < 3:
            return
        d = "M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z"
        regions.setdefault(name, []).append(d)
        # 標籤錨點取「面積最大的那塊」的重心，離島多的縣市才不會標到海上
        if name not in labels or _ring_area(ring) > labels[name][2]:
            cx = sum(p[0] for p in pts[:-1]) / max(len(pts) - 1, 1)
            cy = sum(p[1] for p in pts[:-1]) / max(len(pts) - 1, 1)
            labels[name] = (cx, cy, _ring_area(ring))

    for name, ring in main:
        emit(name, ring, main_proj)

    for i, box_name in enumerate(INSETS):
        box_pts = [pt for n, ring in inset_rings if n == box_name for pt in ring]
        if not box_pts:
            continue
        bx = PAD + i * (INSET_BOX[0] + INSET_GAP)
        by = height - INSET_BOX[1] - PAD
        proj = projector(box_pts, bx, by, *INSET_BOX)
        for name, ring in inset_rings:
            if name == box_name:
                emit(name, ring, proj)
        inset_boxes.append({"name": box_name, "x": round(bx, 1), "y": round(by, 1),
                            "w": INSET_BOX[0], "h": INSET_BOX[1]})

    return {
        "width": VIEW_W,
        "height": height,
        "source": SOURCE_URL,
        "insets": inset_boxes,
        "regions": [
            {
                "name": name,
                "code": codes.get(name, ""),
                "d": "".join(paths),
                "label": [round(labels[name][0], 1), round(labels[name][1], 1)],
            }
            for name, paths in regions.items()
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成台湾县市 SVG 地图")
    parser.add_argument("--tolerance", type=float, default=0.004, help="折线拉直容差（经纬度）")
    parser.add_argument("--min-area", type=float, default=0.0006, dest="min_area", help="忽略小于此面积的离岛")
    parser.add_argument("--force", action="store_true", help="重新下载边界数据")
    args = parser.parse_args()

    if args.force:
        load_source(force=True)

    payload = build(args.tolerance, args.min_area)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(f"window.TW_MAP={body};\n", encoding="utf-8")
    print(f"[map] {len(payload['regions'])} 个县市，{OUTPUT}（{OUTPUT.stat().st_size / 1024:.1f} KB）")
    print("[map] " + "、".join(r["name"] for r in payload["regions"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
