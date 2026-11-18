"""离线比较：简单加权方案 vs 现行融合模型（只读，不改线上代码）。

两个部分：

A. 历史回放（2018 / 2022）——只有民调与结果是真实可回测的：
   Polymarket 在 2018 年尚未上线、2022 年也没有这 22 个县市的盘口，
   所以「市场」与「三源加权」无法用历史结果评分。这里改成评测
   民调这条腿上的三种做法：
     S1 民调领先者直接判赢
     S2 民调领先幅度 → 校准曲线（现行做法；用留一法避免自我评分）
     S3 民调 + 结构先验（在位者 / 同党延续，只有 2022 有上届资料）

B. 2026 当前快照——没有结果，无法算准确率，但可以比较三套公式
   给出的概率差异（领先者是否一致、平均偏离多少）：
     M  只做市场归一化
     T  55% 市场 + 30% 民调 + 15% 舆情（缺源时按剩余权重重新归一）
     N  现行融合模型

用法：python -m tools.backtest_compare [--json PATH]
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tem import backtest, baseline, db  # noqa: E402
from tem.config import load_settings, load_settings as _load  # noqa: E402
from tem.web import _row_to_dict  # noqa: E402


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _brier(pairs: list[tuple[float, float]]) -> float:
    return sum((p - y) ** 2 for p, y in pairs) / max(1, len(pairs))


def _acc(pairs: list[tuple[float, float]]) -> float:
    return sum(1 for p, y in pairs if (p >= 0.5) == (y >= 0.5)) / max(1, len(pairs))


# ── A. 历史回放 ───────────────────────────────────────────────────────────

def _prev_prior(row: dict, regions: dict) -> float:
    """2022 用 2018 的结果当结构先验；2018 没有更早资料，先验为 0。"""
    if row["year"] != 2022:
        return 0.0
    block = regions.get(row["region"]) or {}
    prev = block.get("2018") or {}
    cur = block.get("2022") or {}
    if not prev:
        return 0.0
    party = ""
    for cand in cur.get("candidates") or []:
        if cand.get("name") == row["leader"]:
            party = cand.get("party") or ""
            break
    if row["leader"] == prev.get("winner"):
        return 0.6 * 0.45  # 本人在位
    if party and party == prev.get("winner_party"):
        return 0.6 * 0.20  # 同党延续
    return 0.0


def run_historical() -> dict:
    # 直接用已经跑好的 39 组历史观测（config/calibration.json），
    # 避免每次回放都重新抓维基百科，也确保与线上校准曲线用同一份样本。
    data = json.loads((ROOT / "config" / "calibration.json").read_text(encoding="utf-8"))
    rows = data["observations"]
    a0, b0 = data["model"]["slope"], data["model"]["intercept"]
    regions = (baseline.load_baseline() or {}).get("regions") or {}

    # S1：民调领先者直接判赢（每行都预测「领先者赢」= 概率 1）
    s1 = [(1.0, 1.0 if r["leader_won"] else 0.0) for r in rows]

    # S2：校准曲线（留一法：每次用其余样本拟合）
    loo: list[tuple[float, float]] = []
    for i, r in enumerate(rows):
        rest = rows[:i] + rows[i + 1:]
        a, b = backtest.fit_logistic(
            [x["margin"] for x in rest],
            [1.0 if x["leader_won"] else 0.0 for x in rest],
        )
        loo.append((backtest.predict_prob(r["margin"], a, b), 1.0 if r["leader_won"] else 0.0))

    # 现行校准（全样本拟合，仅作对照，属于自我评分）
    s2_full = [(backtest.predict_prob(r["margin"], a0, b0), 1.0 if r["leader_won"] else 0.0) for r in rows]

    # S3：民调 + 结构先验（只评 2022，因为只有 2022 有上一届资料）
    s3: list[tuple[float, float]] = []
    for i, r in enumerate(rows):
        if r["year"] != 2022:
            continue
        rest = [x for j, x in enumerate(rows) if j != i and x["year"] == 2022]
        a, b = backtest.fit_logistic(
            [x["margin"] for x in rest],
            [1.0 if x["leader_won"] else 0.0 for x in rest],
        )
        p = _sigmoid(a * r["margin"] + b + _prev_prior(r, regions))
        s3.append((p, 1.0 if r["leader_won"] else 0.0))

    def block(pairs, *, note=""):
        return {
            "n": len(pairs),
            "accuracy": round(_acc(pairs), 4),
            "brier": round(_brier(pairs), 4),
            "note": note,
        }

    by_year = {}
    for year in (2018, 2022):
        sub = [r for r in rows if r["year"] == year]
        loo_year = [
            (backtest.predict_prob(
                r["margin"],
                *backtest.fit_logistic(
                    [x["margin"] for x in rows if x is not r],
                    [1.0 if x["leader_won"] else 0.0 for x in rows if x is not r],
                ),
            ), 1.0 if r["leader_won"] else 0.0)
            for r in sub
        ]
        by_year[str(year)] = {
            "n": len(sub),
            "S1_leader_accuracy": round(_acc([(1.0, 1.0 if r["leader_won"] else 0.0) for r in sub]), 4),
            "S2_loo_accuracy": round(_acc(loo_year), 4),
            "S2_loo_brier": round(_brier(loo_year), 4),
        }

    return {
        "sample": len(rows),
        "years": sorted({r["year"] for r in rows}),
        "model_full_fit": {"slope": a0, "intercept": b0, "curve": data["model"]["curve"]},
        "S1_leader_only": block(s1, note="民调领先者直接判赢；无概率，Brier 按 0/1 硬判定计算"),
        "S2_calibrated_loo": block(loo, note="民调领先幅度→校准曲线，留一法（不在自己的样本上评分）"),
        "S2_calibrated_insample": block(s2_full, note="全样本拟合，仅作对照（偏乐观）"),
        "S3_poll_plus_prior_2022": block(s3, note="民调 + 在位者/同党先验，仅 2022，留一法"),
        "S2_2022_same_subset": block(
            [(p, y) for (p, y), r in zip(loo, rows) if r["year"] == 2022],
            note="S2 在 2022 这 20 行上的同一子集，用来和 S3 公平对比",
        ),
        "by_year": by_year,
        "rows": rows,
    }


# ── B. 2026 当前快照 ─────────────────────────────────────────────────────

def _poll_probs(rows: list[dict], slope: float, intercept: float) -> dict[str, float]:
    shares = {r["candidate_en"]: float(r["poll_share"]) for r in rows if r.get("poll_share")}
    if len(shares) < 2:
        return {}
    total = sum(shares.values()) or 1.0
    norm = {k: v / total for k, v in shares.items()}
    ranked = sorted(norm.items(), key=lambda kv: -kv[1])
    margin = (ranked[0][1] - ranked[1][1]) * 100
    p_lead = backtest.predict_prob(margin, slope, intercept)
    rest_total = sum(v for _, v in ranked[1:]) or 1.0
    out = {ranked[0][0]: p_lead}
    for name, value in ranked[1:]:
        out[name] = (1 - p_lead) * (value / rest_total)
    return out


def _sent_prob(value: float | None) -> float:
    if value is None:
        return 0.5
    return 0.5 + 0.10 * max(-1.0, min(1.0, float(value)))


def run_current() -> dict:
    settings = load_settings()
    forecasts = [_row_to_dict(r) for r in db.fetch_latest_forecasts()]
    calib = json.loads((ROOT / "config" / "calibration.json").read_text(encoding="utf-8"))["model"]
    a, b = calib["slope"], calib["intercept"]

    states = {r["region_code"]: r for r in db.fetch_latest_region_state()}
    by_region: dict[str, list[dict]] = defaultdict(list)
    for row in forecasts:
        if row["region_code"] == "TW":
            continue
        by_region[row["region_code"]].append(row)

    variants = {
        "70/20/10": (0.70, 0.20, 0.10),
        "55/30/15": (0.55, 0.30, 0.15),
        "40/40/20": (0.40, 0.40, 0.20),
    }
    out = []
    variant_stats: dict[str, dict[str, int]] = {k: {"same_fused": 0, "same_market": 0} for k in variants}
    for code, rows in by_region.items():
        articles = int((states.get(code) or {"article_count": 0})["article_count"] or 0)
        market_raw = {r["candidate_en"]: max(float(r.get("pm_prob") or 0.0), 1e-9) for r in rows}
        m_total = sum(market_raw.values())
        market = {k: v / m_total for k, v in market_raw.items()}

        poll = _poll_probs(rows, a, b)
        use_poll = len(poll) >= 2
        use_sent = articles >= 10
        sent = {r["candidate_en"]: _sent_prob(r.get("news_sentiment")) for r in rows}

        def blend(weights: tuple[float, float, float]) -> dict[str, float]:
            wm, wp, ws = weights
            w = {"m": wm, "p": wp if use_poll else 0.0, "s": ws if use_sent else 0.0}
            wsum = sum(w.values()) or 1.0
            result = {}
            for r in rows:
                en = r["candidate_en"]
                value = w["m"] * market[en]
                if use_poll:
                    value += w["p"] * poll.get(en, 0.0)
                if use_sent:
                    value += w["s"] * sent[en]
                result[en] = value / wsum
            total = sum(result.values()) or 1.0
            return {k: v / total for k, v in result.items()}

        fused = {r["candidate_en"]: float(r.get("fused_prob") or 0.0) for r in rows}
        f_total = sum(fused.values()) or 1.0
        fused = {k: v / f_total for k, v in fused.items()}

        def leader(d):
            return max(d.items(), key=lambda kv: kv[1])

        lm, lf = leader(market), leader(fused)
        trio = blend(variants["55/30/15"])
        lt = leader(trio)
        name = {r["candidate_en"]: r.get("candidate_zh") or r["candidate_en"] for r in rows}
        for vname, weights in variants.items():
            lv = leader(blend(weights))
            if lv[0] == lf[0]:
                variant_stats[vname]["same_fused"] += 1
            if lv[0] == lm[0]:
                variant_stats[vname]["same_market"] += 1
        out.append({
            "region": rows[0].get("region_name") or code,
            "code": code,
            "articles": articles,
            "market_leader": name[lm[0]],
            "market_prob": round(lm[1], 4),
            "trio_leader": name[lt[0]],
            "trio_prob": round(lt[1], 4),
            "fused_leader": name[lf[0]],
            "fused_prob": round(lf[1], 4),
            "market_vs_fused": round(lm[1] - fused[lm[0]], 4),
            "trio_vs_fused": round(lt[1] - fused[lt[0]], 4),
            "max_abs_diff": round(max(abs(market[k] - fused[k]) for k in market), 4),
            "poll_used": use_poll,
            "sent_used": use_sent,
        })

    same_tf = sum(1 for r in out if r["trio_leader"] == r["fused_leader"])
    same_mf = sum(1 for r in out if r["market_leader"] == r["fused_leader"])
    diffs = sorted(out, key=lambda r: -r["max_abs_diff"])
    return {
        "regions": len(out),
        "same_leader_trio_vs_fused": same_tf,
        "same_leader_market_vs_fused": same_mf,
        "mean_max_abs_diff": round(sum(r["max_abs_diff"] for r in out) / max(1, len(out)), 4),
        "variant_leader_agreement": variant_stats,
        "top_divergences": diffs[:8],
        "rows": out,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="比较简单加权与现行模型")
    parser.add_argument("--json", default="data/exports/backtest-compare.json")
    args = parser.parse_args()

    hist = run_historical()
    cur = run_current()
    payload = {"historical": hist, "current": cur}
    path = ROOT / args.json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 62)
    print("A. 历史回放（2018/2022，样本 %d）" % hist["sample"])
    for key, label in (
        ("S1_leader_only", "S1 民调领先者判赢"),
        ("S2_calibrated_loo", "S2 校准曲线（留一法）"),
        ("S2_calibrated_insample", "S2 校准曲线（全样本，偏乐观）"),
        ("S3_poll_plus_prior_2022", "S3 民调+在位先验（仅2022）"),
        ("S2_2022_same_subset", "S2 在 2022 同一子集（对照）"),
    ):
        m = hist[key]
        print("  %-28s n=%-3d 准确率 %.1f%%  Brier %.4f" % (label, m["n"], m["accuracy"] * 100, m["brier"]))
    for year, m in hist["by_year"].items():
        print("    %s: n=%d  S1 %.1f%%  S2 %.1f%% (Brier %.4f)" % (
            year, m["n"], m["S1_leader_accuracy"] * 100, m["S2_loo_accuracy"] * 100, m["S2_loo_brier"]))

    print("=" * 62)
    print("B. 2026 当前快照（%d 个县市）" % cur["regions"])
    print("  三源加权 vs 现行模型：领先者一致 %d/%d" % (cur["same_leader_trio_vs_fused"], cur["regions"]))
    print("  纯市场   vs 现行模型：领先者一致 %d/%d" % (cur["same_leader_market_vs_fused"], cur["regions"]))
    print("  平均最大偏离：%.1f 个百分点" % (cur["mean_max_abs_diff"] * 100))
    print("  权重敏感度（领先者与现行模型一致 / 与纯市场一致）：")
    for name, s in cur["variant_leader_agreement"].items():
        print("    %-10s %2d/%d  vs 市场 %2d/%d" % (
            name, s["same_fused"], cur["regions"], s["same_market"], cur["regions"]))
    print("  偏差最大的县市：")
    for r in cur["top_divergences"]:
        print("    %-6s 市场 %-6s %.1f%% | 三源 %-6s %.1f%% | 现行 %-6s %.1f%% (n=%d)"
              % (r["region"], r["market_leader"], r["market_prob"] * 100,
                 r["trio_leader"], r["trio_prob"] * 100,
                 r["fused_leader"], r["fused_prob"] * 100, r["articles"]))
    print("=" * 62)
    print("结果已写入", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
