"""回測與機率校準：用 2018 / 2022 的選前民調對照實際結果。

為什麼要做這件事：原本的模型裡所有權重都是拍出來的，輸出的百分比也沒有
機率意義。「民調領先 5 個百分點」到底等於幾成勝率？只有拿歷史選舉回測
才答得出來。

作法：
  1. 2018 / 2022 兩次縣市長選舉，各縣市條目底下都有「民意調查」小節，
     格式與 2026 的民調表相同，可直接沿用同一套解析器。
  2. 對每個縣市，取投票日前 90 天內、最接近投票日的那份民調，比較
     「民調前兩名」與「實際當選人」。
  3. 用 logistic regression 擬合 P(民調領先者當選 | 領先幅度)。
     截距 b 代表民調的系統性偏差，斜率 a 代表領先幅度有多少資訊量。
  4. 用 Brier score 與準確率評估，係數寫入 config/calibration.json，
     供預測模型把民調差距轉成勝率。

樣本數不多（每次選舉 20 幾個縣市），因此用 L2 正則化避免過擬合，
並且只在有足夠歷史樣本時才讓校準曲線介入預測。
"""

from __future__ import annotations

import json
import math
import time
from datetime import date, timedelta
from typing import Any

from .paths import CONFIG_DIR

ELECTIONS = {
    2018: {"page": "2018年中華民國直轄市長及縣市長選舉", "date": "2018-11-24"},
    2022: {"page": "2022年中華民國直轄市長及縣市長選舉", "date": "2022-11-26"},
}
WINDOW_DAYS = 90


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def historical_polls(year: int) -> dict[str, list[dict[str, Any]]]:
    """從歷史選舉條目抽出各縣市的選前民調。"""
    from . import baseline, polls

    info = ELECTIONS[year]
    wikitext = baseline._wikitext(info["page"])
    out: dict[str, list[dict[str, Any]]] = {}
    for region, body in baseline._regions_of(wikitext):
        section = baseline._subsection(body, "民意調查")
        if not section:
            continue
        items: list[dict[str, Any]] = []
        for table in polls._tables(section):
            items.extend(polls._parse_table(table, fallback_year=year))
        if items:
            out[region] = items
    return out


def observations(year: int, *, window_days: int = WINDOW_DAYS) -> list[dict[str, Any]]:
    """回傳 (民調領先幅度, 領先者是否當選) 的觀測。"""
    from . import baseline

    info = ELECTIONS[year]
    election_day = _parse_date(info["date"])
    if year == 2022:
        results = baseline.parse_2022(baseline._wikitext(info["page"]))
    else:
        results = _results_2018()
    polls = historical_polls(year)

    out: list[dict[str, Any]] = []
    for region, items in polls.items():
        result = results.get(region)
        if result is None:
            cached = baseline.region_baseline(region) or {}
            result = cached.get(str(year))
        elif year == 2018:
            result = (result or {}).get("2018")
        if not result:
            continue
        winner = result.get("winner")
        groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for poll in items:
            when = _parse_date(poll.get("date") or "")
            if not when or not election_day:
                continue
            if not (timedelta(0) <= (election_day - when) <= timedelta(days=window_days)):
                continue
            if poll.get("kind") == "primary":
                continue
            groups.setdefault(tuple(poll.get("matchup") or ()), []).append(poll)
        for key, group in groups.items():
            if winner not in key or len(key) < 2:
                continue
            group.sort(key=lambda p: p["date"])
            latest = group[-1]          # 最接近投票日的一份
            values = {k: v for k, v in latest["values"].items() if k in key}
            if len(values) < 2 or winner not in values:
                continue
            ranked = sorted(values.items(), key=lambda kv: -kv[1])
            if ranked[0][0] != winner:
                # 民調領先者不是實際當選人：這是最有價值的「翻盤」樣本
                margin = ranked[0][1] - ranked[1][1]
                leader_won = False
                leader = ranked[0][0]
                runner = ranked[1][0]
            else:
                margin = ranked[0][1] - ranked[1][1]
                leader_won = True
                leader = ranked[0][0]
                runner = ranked[1][0]
            out.append({
                "year": year,
                "region": region,
                "leader": leader,
                "runner_up": runner,
                "margin": round(margin, 2),
                "leader_won": leader_won,
                "winner": winner,
                "date": latest["date"],
                "source": latest.get("source") or "",
                "matchup": list(key),
            })
    return out


def _results_2018() -> dict[str, dict[str, Any]]:
    """2018 的明細在模板頁，這裡重用 baseline 的抓取與解析。"""
    from . import baseline

    regions = list((baseline.load_baseline().get("regions") or {}).keys())
    if not regions:
        from .config import load_settings

        regions = [r.name for r in load_settings().regions]
    out: dict[str, dict[str, Any]] = {}
    for title, content in baseline.fetch_2018_templates(regions).items():
        parsed = baseline.parse_2018_template(content)
        if parsed:
            out[title.split("/")[-1]] = {"2018": parsed}
    return out


# ---------------------------------------------------------------- 擬合

def fit_logistic(
    xs: list[float],
    ys: list[float],
    *,
    l2: float = 0.02,
    lr: float = 0.05,
    epochs: int = 4000,
) -> tuple[float, float]:
    """單特徵 logistic regression（梯度下降），回傳 (斜率, 截距)。"""
    a, b = 0.08, 0.0
    n = max(1, len(xs))
    for _ in range(epochs):
        ga = gb = 0.0
        for x, y in zip(xs, ys):
            p = 1.0 / (1.0 + math.exp(-(a * x + b)))
            err = p - y
            ga += err * x
            gb += err
        a -= lr * (ga / n + l2 * a)
        b -= lr * (gb / n + l2 * b)
    return round(a, 5), round(b, 5)


def predict_prob(margin: float, a: float, b: float) -> float:
    return 1.0 / (1.0 + math.exp(-(a * margin + b)))


def score(rows: list[dict[str, Any]], a: float, b: float) -> dict[str, Any]:
    probs = [predict_prob(r["margin"], a, b) for r in rows]
    labels = [1.0 if r["leader_won"] else 0.0 for r in rows]
    brier = sum((p - y) ** 2 for p, y in zip(probs, labels)) / max(1, len(rows))
    hits = sum(1 for p, y in zip(probs, labels) if (p >= 0.5) == (y >= 0.5))
    base = sum(labels) / max(1, len(labels))
    baseline_brier = sum((base - y) ** 2 for y in labels) / max(1, len(labels))
    return {
        "n": len(rows),
        "brier": round(brier, 4),
        "baseline_brier": round(baseline_brier, 4),
        "skill": round(1 - brier / baseline_brier, 4) if baseline_brier else None,
        "accuracy": round(hits / max(1, len(rows)), 4),
    }


def run_backtest(*, window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for year in ELECTIONS:
        try:
            rows.extend(observations(year, window_days=window_days))
        except Exception as exc:  # noqa: BLE001 - 單一年度失敗仍可回測另一年
            print(f"[backtest] {year} 抽樣失敗：{type(exc).__name__}: {exc}")

    # 同一縣市可能有多個對戰組合，去重時保留差距最小的（最難判斷的那個）
    dedup: dict[tuple[int, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["year"], row["region"])
        if key not in dedup or row["margin"] < dedup[key]["margin"]:
            dedup[key] = row
    sample = sorted(dedup.values(), key=lambda r: (r["year"], r["region"]))

    xs = [r["margin"] for r in sample]
    ys = [1.0 if r["leader_won"] else 0.0 for r in sample]
    a, b = fit_logistic(xs, ys)
    metrics = score(sample, a, b)
    curve = {
        str(m): round(predict_prob(m, a, b), 4)
        for m in (0, 1, 2, 3, 5, 8, 10, 15, 20, 30)
    }
    return {
        "meta": {
            "note": "以 2018/2022 縣市長選舉的選前民調回測，logistic 校準 P(民調領先者當選 | 領先幅度)",
            "window_days": window_days,
            "updated_at": time.strftime("%Y-%m-%d"),
        },
        "model": {"slope": a, "intercept": b, "curve": curve},
        "metrics": metrics,
        "observations": sample,
    }


def save_calibration(data: dict[str, Any], path=None) -> str:
    target = path or (CONFIG_DIR / "calibration.json")
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(target)


_CACHE: dict[str, Any] = {}


def load_calibration(path=None) -> dict[str, Any]:
    target = path or (CONFIG_DIR / "calibration.json")
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def calibrated_prob(margin: float | None) -> float | None:
    """把民調領先幅度（百分點）轉成勝率；沒有校準檔就回傳 None。"""
    if margin is None:
        return None
    if "data" not in _CACHE:
        _CACHE["data"] = load_calibration()
    model = (_CACHE["data"] or {}).get("model") or {}
    a, b = model.get("slope"), model.get("intercept")
    if a is None or b is None:
        return None
    prob = predict_prob(float(margin), float(a), float(b))
    # 歷史樣本只有 39 組，不該給出 100% 或 0% 這種過度自信的數字
    return max(0.03, min(0.97, prob))
