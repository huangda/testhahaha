"""生成 Markdown / JSON 报告。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .models import MarketSnapshot
from .paths import EXPORT_DIR, REPORT_DIR, ensure_dirs
from .zhconv import deep_simplify, is_simplified_preferred, neutralize_scope, to_simplified

LOCAL_OFFSET_HOURS = 8  # 台北 / 北京时间 UTC+8


def to_local(value: str | None, *, with_date: bool = True) -> str:
    """把数据库中的 UTC 时间字符串转为 UTC+8 显示。"""
    if not value:
        return "—"
    text = value.strip().replace("T", " ")[:19]
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            return text
    dt = dt + timedelta(hours=LOCAL_OFFSET_HOURS)
    return dt.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M")


def _pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.1f}%"


def _delta_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.1f}%"


def _sentiment_tag(value: float | None) -> str:
    if value is None:
        return "—"
    if value > 0.12:
        return f"🟢 {value:+.2f}"
    if value < -0.12:
        return f"🔴 {value:+.2f}"
    return f"⚪ {value:+.2f}"


def _poll_for(polls: dict[str, Any], region_name: str) -> dict[str, Any] | None:
    """民调资料用「台北市」这类写法，需与报告里的「臺北市」对齐。"""
    from .zhconv import to_simplified

    want = to_simplified(region_name).replace("臺", "台")
    for name, data in polls.items():
        if to_simplified(name).replace("臺", "台") == want:
            return data
    return None


def build_markdown(
    settings: Settings,
    *,
    generated_at: str,
    region_summaries: list[dict[str, Any]],
    forecast_rows: list[dict[str, Any]],
    region_states: list[dict[str, Any]],
    candidate_sentiment: dict[str, dict[str, Any]],
    news_rows: list[Any],
    verdict: dict[str, Any],
    snapshots: dict[str, MarketSnapshot],
    polls: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    add = lines.append

    add("# polymarket投注分析指南")
    add("")
    add(f"- 生成时间：{to_local(generated_at)}（UTC+8）")
    add(f"- 选举：{settings.election}（投票日 {settings.election_date}）")
    add(f"- 涵盖：{len([s for s in region_summaries if s['region_code'] != 'TW'])} 个县市 + 全岛政党市场")
    analyzed = sum(1 for r in news_rows if (r.get if isinstance(r, dict) else (lambda k, r=r: r[k]))("sentiment") is not None)
    add(f"- 新闻样本：本地 {len(news_rows)} 则，其中 {analyzed} 则已完成舆情分析")
    add("")

    # 全岛
    nationwide = next((s for s in region_summaries if s["region_code"] == "TW"), None)
    if nationwide:
        add("## 博彩市场政党赔率（全岛）")
        add("")
        add(f"市场领先：**{nationwide['market_leader_zh']}**，融合概率 **{_pct(nationwide['fused_prob'])}**"
            f"（市场 {_pct(nationwide['pm_prob'])}，舆情 {_sentiment_tag(nationwide['sentiment'])}）")
        add("")

    # 县市总表
    add("## 22 县市选情预测总表")
    add("")
    add("| 县市 | 融合领先者 | 融合概率 | 市场概率 | 次高 | 差距 | 舆情情感 | 新闻数 | 备注 |")
    add("|---|---|---|---|---|---|---|---|---|")
    for row in sorted(
        [r for r in region_summaries if r["region_code"] != "TW"],
        key=lambda r: -r["fused_prob"],
    ):
        note = []
        if row["sentiment_override"]:
            note.append("舆情翻转市场")
        if row["tossup"]:
            note.append("胶着")
        add(
            "| {name} | {leader} | {fp} | {pm} | {ru}({rup}) | {margin} | {sent} | {arts} | {note} |".format(
                name=row["region_name"],
                leader=row["leader_zh"],
                fp=_pct(row["fused_prob"]),
                pm=_pct(row["pm_prob"]),
                ru=row["runner_up_zh"] or "—",
                rup=_pct(row["runner_up_prob"]) if row["runner_up_prob"] else "—",
                margin=_pct(row["margin"]),
                sent=_sentiment_tag(row["sentiment"]),
                arts=row["articles"],
                note="、".join(note) or "",
            )
        )
    add("")

    # 胶着选区
    tossups = [r for r in region_summaries if r["tossup"] and r["region_code"] != "TW"]
    if tossups:
        add("## 胶着选区（融合差距 ≤ 12%）")
        add("")
        for row in tossups:
            add(f"- **{row['region_name']}**：{row['leader_zh']} {_pct(row['fused_prob'])} vs "
                f"{row['runner_up_zh']} {_pct(row['runner_up_prob'])}（差距 {_pct(row['margin'])}，"
                f"{row['articles']} 则新闻）")
        add("")

    # 舆情焦点
    add("## 舆情焦点")
    add("")
    add("### 讨论热度最高")
    add("")
    for item in verdict.get("hottest_regions", [])[:5]:
        add(f"- {item['region']}：{item['articles']} 则，情感 {item['sentiment']:+.2f}")
    add("")
    if verdict.get("most_negative"):
        add("### 氛围最负面")
        add("")
        for item in verdict["most_negative"][:5]:
            add(f"- {item['region']}：情感 {item['sentiment']:+.2f}（{item['articles']} 则）")
        add("")
    if verdict.get("most_positive"):
        add("### 氛围最正面")
        add("")
        for item in verdict["most_positive"][:5]:
            add(f"- {item['region']}：情感 {item['sentiment']:+.2f}（{item['articles']} 则）")
        add("")

    # 民调对照
    if polls:
        add("## 民调 × 赔率对照")
        add("")
        add("| 县市 | 民调领先 | 民调均值 | 最新民调 | 市场概率 | 融合概率 | 数据日期 |")
        add("|---|---|---|---|---|---|---|")
        for row in sorted(
            [r for r in region_summaries if r["region_code"] != "TW"],
            key=lambda r: r["region_name"],
        ):
            data = _poll_for(polls, row["region_name"])
            if not data:
                continue
            average = data.get("average") or {}
            latest = data.get("latest") or {}
            if not average:
                continue
            top_name, top_avg = max(average.items(), key=lambda kv: kv[1])
            latest_val = latest.get(top_name)
            add(
                "| {rg} | {who} | {avg:.1f}% | {lat} | {pm} | {fu} | {date} |".format(
                    rg=row["region_name"],
                    who=top_name,
                    avg=top_avg,
                    lat=f"{latest_val:.1f}%" if latest_val is not None else "—",
                    pm=_pct(row["pm_prob"]),
                    fu=_pct(row["fused_prob"]),
                    date=data.get("latest_date") or "—",
                )
            )
        add("")
        add(f"> 民调为县市调查汇总与近14天新闻标题抽取合并结果，按机构、样本量、"
            f"来源层级和时间衰减加权，半衰期45天；不同机构的方法与委托背景不同，仅供参考。")
        add("")

    # 候选人情感
    if candidate_sentiment:
        add("## 候选人声量与情感（前 25）")
        add("")
        add("| 候选人 | 提及数 | 相关新闻 | 情感均值 |")
        add("|---|---|---|---|")
        ranked = sorted(candidate_sentiment.items(), key=lambda kv: -kv[1]["mentions"])[:25]
        for name_en, info in ranked:
            add(f"| {settings.display_name(name_en)} | {info['mentions']} | {info['articles']} | {_sentiment_tag(info['sentiment'])} |")
        add("")

    # 最新新闻
    add("## 最新相关新闻")
    add("")
    for row in news_rows[:30]:
        get = row.get if isinstance(row, dict) else (lambda k, r=row: r[k])
        title = get("title")
        source = get("source_name") or ""
        published = to_local(get("published_at") or get("first_seen"))
        sentiment = get("sentiment")
        regions = get("regions")
        region_label = ""
        if regions:
            try:
                parsed = json.loads(regions) if isinstance(regions, str) else regions
                region_label = "/".join(parsed[:2])
            except (json.JSONDecodeError, TypeError):
                region_label = ""
        tag = f"[{region_label}] " if region_label else ""
        url = get("url") or ""
        link = f"[{title}]({url})" if url else title
        add(f"- {published} {tag}{link} — *{source}* {_sentiment_tag(sentiment)}")
    add("")

    add("## 方法与免责")
    add("")
    add("- **赔率数据**：Polymarket Gamma API（公开、免密钥），每次执行写入一笔快照，用于计算 24 小时与一周变化。")
    add("- **新闻数据**：Google 新闻繁中、简中、英文搜索，以及中央社、自由时报、ETtoday、Yahoo等RSS；"
        "按选举关键词筛选，并用标题相似度合并多家媒体的同一事件。")
    add("- **新闻分析**：使用57个正面词、81个负面词、10个否定词、12条选举句式规则和10类议题关键词，"
        "识别候选人、县市、议题、情感和影响强度。")
    add("- **民调数据**：县市调查汇总与近14天新闻标题民调抽取，按对战组合拆分并加权去重。")
    add("- **补充证据**：新文易数事件时间线和媒体覆盖、PTT政黑板、维基百科浏览量、"
        "Google Trends台湾当前热搜Top 10；这些只用于证据核对，不进入胜率方向。")
    add("- **融合模型**：以Polymarket市场概率为主，结合加权民调和新闻舆情进行有上限的修正；"
        "首页不提供手工调整舆情权重。")
    add("- 本报告为量化舆情参考，非选举结果预测，也不构成任何投资或投票建议。")
    add("")
    return "\n".join(lines)


def write_reports(markdown: str, payload: dict[str, Any], stamp: str) -> dict[str, str]:
    ensure_dirs()
    if is_simplified_preferred():
        markdown = to_simplified(neutralize_scope(markdown))
        payload = deep_simplify(payload)
    md_path = REPORT_DIR / f"report-{stamp}.md"
    json_path = EXPORT_DIR / f"snapshot-{stamp}.json"
    md_path.write_text(markdown, encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_md = REPORT_DIR / "latest.md"
    latest_json = EXPORT_DIR / "latest.json"
    latest_md.write_text(markdown, encoding="utf-8")
    latest_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"markdown": str(md_path), "json": str(json_path)}


def load_latest_report() -> dict[str, Any] | None:
    path = EXPORT_DIR / "latest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
