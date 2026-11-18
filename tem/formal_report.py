"""正式汇报版分析报道：Word/HTML 共用同一套文字、表格和图表。"""

from __future__ import annotations

import html
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from docx import Document
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Inches, Mm, Pt, RGBColor

from .briefing import (
    _candidate_name_key,
    _election_countdown,
    _narrative,
    _region_bundle,
    _summary_points,
)
from .zhconv import neutralize_scope, to_simplified

FONT_REGULAR = r"C:\Windows\Fonts\msyh.ttc"
FONT_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
BODY_FONT = "FangSong"
HEADING_FONT = "SimHei"
INK = "111827"
MUTED = "5B6472"
NAVY = "17365D"
LIGHT_BLUE = "EAF1F8"
LIGHT_GRAY = "F3F5F7"
BORDER = "D9D9D9"
BLUE = "2F6FB3"
GREEN = "2C8C5A"
CYAN = "168A9A"
RED = "B64A5A"
AMBER = "B7791F"


def _s(value: Any) -> str:
    return neutralize_scope(to_simplified(str(value if value is not None else "")))


def _party_color(party: Any) -> str:
    value = _s(party)
    if "国民党" in value:
        return BLUE
    if "民进党" in value or "民主进步党" in value:
        return GREEN
    if "民众党" in value:
        return AMBER
    return "6B7280"


def _party_class(party: Any) -> str:
    color = _party_color(party)
    return {
        BLUE: "party-blue",
        GREEN: "party-green",
        AMBER: "party-amber",
    }.get(color, "party-other")


def _colored_cell(text: Any, party: Any = "", *, bold: bool = False) -> dict[str, Any]:
    return {
        "text": str(text if text is not None else ""),
        "color": _party_color(party),
        "class": _party_class(party),
        "bold": bold,
    }


def _narrative_paragraphs(
    narrative: dict[str, list[str]] | None,
    key: str,
    fallback: str | list[str],
) -> list[str]:
    if narrative and isinstance(narrative.get(key), list):
        values = [str(item).strip() for item in narrative[key] if str(item).strip()]
        if values:
            return values
    if isinstance(fallback, list):
        return [str(item) for item in fallback if str(item).strip()]
    return [fallback] if fallback else []


def _json_value(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value) * 100:.{digits}f}%"


def _pp(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value) * 100:.{digits}f} 个百分点"


def _poll_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    number = float(value)
    if abs(number) <= 1.0:
        number *= 100
    return f"{number:.{digits}f}%"


def _issue_categories(issue: dict[str, Any]) -> str:
    values = []
    for item in (issue.get("issue_types") or []) + (issue.get("topics") or []):
        text = _s(item).strip()
        if len(text) >= 2 and re.search(r"[\u4e00-\u9fff]", text) and text not in values:
            values.append(text)
    return "、".join(values[:2]) or "综合议题"


def _party_effect_text(issue: dict[str, Any]) -> str:
    effects = issue.get("party_effects") or []
    labels = []
    for effect in effects:
        party = _s(effect.get("party") or "")
        direction = _s(effect.get("direction") or "")
        if party and direction:
            labels.append(f"{party}{direction}")
    return "；".join(labels) or _s(issue.get("party_effect_note") or "未同时涉及蓝绿候选人")


def _font(size: int, bold: bool = False):
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(path, size=size)
    except OSError:
        return ImageFont.truetype(r"C:\Windows\Fonts\simhei.ttf", size=size)


def _focus_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = ("新北", "宜兰")
    return [
        next((region for region in regions if key in _s(region["name"])), None)
        for key in wanted
    ]


def _close_regions(regions: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    rows = []
    for region in regions:
        candidates = region.get("candidates") or []
        if len(candidates) < 2:
            continue
        margin = float(candidates[0].get("fused") or 0) - float(candidates[1].get("fused") or 0)
        rows.append({
            "name": region["name"],
            "leader": candidates[0].get("name") or "",
            "leader_party": candidates[0].get("party") or "",
            "runner": candidates[1].get("name") or "",
            "runner_party": candidates[1].get("party") or "",
            "margin": max(0.0, margin),
            "leader_prob": candidates[0].get("fused"),
            "runner_prob": candidates[1].get("fused"),
        })
    rows.sort(key=lambda row: row["margin"])
    return rows[:limit]


def _party_expectation(regions: list[dict[str, Any]]) -> list[tuple[str, float, int]]:
    seats: dict[str, float] = {}
    leads: dict[str, int] = {}
    for region in regions:
        candidates = region.get("candidates") or []
        if not candidates:
            continue
        top = candidates[0]
        party = top.get("party") or "无党籍／未标注"
        leads[party] = leads.get(party, 0) + 1
        for candidate in candidates:
            key = candidate.get("party") or "无党籍／未标注"
            seats[key] = seats.get(key, 0.0) + max(0.0, float(candidate.get("fused") or 0))
    return sorted(
        ((party, value, leads.get(party, 0)) for party, value in seats.items()),
        key=lambda row: (-row[1], row[0]),
    )


def _candidate_sentiment_rows(
    regions: list[dict[str, Any]],
    news_rows: list[dict[str, Any]] | None = None,
    limit: int = 10,
) -> list[list[str]]:
    rows: list[tuple[int, list[str]]] = []
    if news_rows is not None:
        aggregate: dict[str, dict[str, Any]] = {}
        for row in news_rows:
            if int(row.get("is_rep", 1) or 0) != 1:
                continue
            candidates = _json_value(row.get("candidates"))
            regions_value = _json_value(row.get("regions"))
            region_name = _s((regions_value or ["全岛"])[0])
            impact = max(0.1, float(row.get("impact") or 0.3))
            for candidate in candidates:
                if not isinstance(candidate, dict) or candidate.get("sentiment") is None:
                    continue
                name = _s(candidate.get("name_zh") or candidate.get("name") or "")
                if not name:
                    continue
                item = aggregate.setdefault(
                    name,
                    {"region": region_name, "weighted": 0.0, "weight": 0.0, "articles": 0},
                )
                item["weighted"] += float(candidate.get("sentiment") or 0) * impact
                item["weight"] += impact
                item["articles"] += 1
        for name, item in aggregate.items():
            score = item["weighted"] / (item["weight"] or 1)
            tone = "偏正面" if score > 0.12 else ("偏负面" if score < -0.12 else "中性")
            rows.append((
                int(item["articles"]),
                [item["region"], name, str(int(item["articles"])), f"{score:+.2f}", tone],
            ))
    else:
        for region in regions:
            candidates = region.get("candidates") or []
            if not candidates:
                continue
            candidate = candidates[0]
            score = candidate.get("sentiment")
            if score is None:
                continue
            tone = "偏正面" if score > 0.12 else ("偏负面" if score < -0.12 else "中性")
            rows.append((
                int(candidate.get("articles") or 0),
                [
                    _s(region.get("name")),
                    _s(candidate.get("name")),
                    str(int(candidate.get("articles") or 0)),
                    f"{float(score):+.2f}",
                    tone,
                ],
            ))
    rows.sort(key=lambda item: -item[0])
    return [row for _, row in rows[:limit]]


def _overall_tone(trend: list[dict[str, Any]]) -> tuple[float | None, str]:
    weighted = 0.0
    weight = 0.0
    for row in trend:
        sentiment = row.get("sentiment")
        if sentiment is None:
            continue
        events = max(1, int(row.get("events") or 0))
        weighted += float(sentiment) * events
        weight += events
    if not weight:
        return None, "暂无足够舆情样本"
    score = weighted / weight
    tone = "总体偏正面" if score > 0.08 else ("总体偏负面" if score < -0.08 else "总体接近中性")
    return score, tone


def _hot_issues(payload: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        from .hotissues import extract

        return extract(payload.get("opinion_news") or payload.get("news") or [], limit=40)
    except Exception:  # noqa: BLE001
        return []


def _hot_summary(payload: dict[str, Any], issues: list[dict[str, Any]]) -> str:
    ranked = sorted(
        issues,
        key=lambda item: (-float(item.get("impact") or 0), -float(item.get("rank_score") or 0)),
    )
    categories = Counter(_issue_categories(issue) for issue in ranked)
    category_text = "、".join(name for name, _ in categories.most_common(4))
    event_titles = [str(event.get("title") or "").strip() for event in (payload.get("hot_events") or [])]
    event_titles = [title for title in event_titles if title][:3]
    parts = [f"近{payload.get('opinion_days', 7)}天舆情主要集中于{category_text or '综合选举议题'}。"]
    if event_titles:
        parts.append("近期较受关注的具体事件包括" + "；".join(event_titles) + "。")
    parts.append(
        "总体看，民调变化、竞选活动与政策攻防构成主要新闻框架，住房、就业、交通、育儿、长者照顾、"
        "灾害治理和地方廉政是较容易触及中间选民的生活议题。相关报道数量反映传播强度，"
        "并不直接等同于支持率变化。"
    )
    return "".join(parts)


def _event_sources(event: dict[str, Any]) -> str:
    sources = [_s(item).strip() for item in (event.get("sources") or []) if str(item).strip()]
    if not sources and event.get("source"):
        sources = [_s(event.get("source"))]
    return "、".join(dict.fromkeys(sources)) or "—"


def _observe_report_data(payload: dict[str, Any]) -> dict[str, Any]:
    payload_observe = payload.get("observe") or {}
    threads = payload_observe.get("threads") or []
    trends = payload_observe.get("trends") or []
    graph = payload_observe.get("graph") or {}
    event_rows: list[list[str]] = []
    for thread in threads[:8]:
        news = thread.get("news") or []
        title = str((news[0] or {}).get("title") or thread.get("title") or "未命名事件")
        coverage = thread.get("coverage") or {}
        first_time = _s(str(thread.get("first_time") or "")[:16].replace("T", " "))
        last_time = _s(str(thread.get("last_time") or "")[:16].replace("T", " "))
        reports = []
        for item in news[:4]:
            report_title = str(item.get("title") or "").strip()
            if report_title:
                reports.append(_s(report_title))
        event_rows.append([
            _s(title),
            f"{first_time} 至 {last_time}" if first_time or last_time else "—",
            f"{int(coverage.get('articles') or 0)} 篇 / {int(coverage.get('media_count') or 0)} 家",
            "；".join(reports[:2]) or "—",
        ])
    trend_rows: list[list[str]] = []
    candidate_names = {
        _s(name)
        for name in ((payload.get("settings").name_map if payload.get("settings") else {}) or {}).values()
        if name
    }
    entertainment = ("金钟", "金钟奖", "金马", "艺人", "综艺", "红毯", "演唱会", "胡瓜", "陈汉典")
    election_terms = ("选举", "九合一", "市长", "县长", "议员", "民调", "候选人", "提名", "政党")
    for trend in trends[:8]:
        related_items = [str(item.get("tag") or "") for item in (trend.get("related") or [])[:3]]
        related = "、".join(related_items)
        tag_text = _s(trend.get("tag") or "")
        if (
            tag_text in candidate_names
            and any(word in related or word in tag_text for word in entertainment)
            and not any(word in related or word in tag_text for word in election_terms)
        ):
            continue
        trend_rows.append([
            _s(trend.get("tag") or ""),
            str(int(trend.get("count") or 0)),
            f"{float(trend.get('burst') or 0):+.1f}",
            _s(related) or "—",
        ])
    names = {
        str(node.get("id") or node.get("tag") or ""): str(node.get("tag") or node.get("id") or "")
        for node in (graph.get("nodes") or [])
    }
    edges = sorted(graph.get("edges") or [], key=lambda edge: -int(edge.get("value") or 0))
    relations = [
        f"{_s(names.get(str(edge.get('source')), edge.get('source')))}—{_s(names.get(str(edge.get('target')), edge.get('target')))}"
        for edge in edges[:5]
        if edge.get("source") and edge.get("target")
    ]
    event_titles = [row[0] for row in event_rows[:3]]
    summary = (
        f"新文易数近7天筛选出{len(threads)}个台湾选举相关事件、{len(trends)}个当前热词，"
        f"议题关系图包含{len(graph.get('nodes') or [])}个节点和{len(edges)}条关系。"
    )
    if event_titles:
        summary += "近期代表事件包括" + "；".join(event_titles) + "。"
    relation_text = (
        "同批新闻中关联较强的议题组合为" + "、".join(relations) + "。"
        if relations else "本次抓取未形成稳定的议题关系组合。"
    )
    return {
        "ok": bool(payload_observe.get("ok")),
        "summary": summary,
        "relation_text": relation_text,
        "event_rows": event_rows,
        "trend_rows": trend_rows,
        "warnings": payload_observe.get("warnings") or [],
    }


def _tone_label(value: Any) -> str:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "中性"
    return "偏正面" if score > 0.12 else ("偏负面" if score < -0.12 else "中性")


def _platform_report_data(payload: dict[str, Any]) -> dict[str, Any]:
    ptt = payload.get("ptt") or {}
    youtube = payload.get("youtube") or {}
    trends = payload.get("google_trends") or {}

    ptt_counts = ptt.get("counts") or {}
    ptt_candidates = ptt.get("candidates") or []
    ptt_rows = [
        [
            _s(row.get("name")),
            " · ".join(
                part for part in (
                    _s(row.get("region") or ""),
                    _s(row.get("party") or ""),
                ) if part
            ) or "未标注",
            str(int(row.get("posts") or 0)),
            f"{float(row.get('tone') or 0):+.2f}",
            _tone_label(row.get("tone")),
            str(int(row.get("controversial_posts") or 0)),
        ]
        for row in ptt_candidates[:6]
    ]

    youtube_counts = youtube.get("counts") or {}
    youtube_candidates = youtube.get("candidates") or []
    youtube_rows = [
        [
            _s(row.get("name")),
            _s(row.get("party") or "未标注"),
            str(int(row.get("videos") or 0)),
            f"{int(row.get('views') or 0):,}",
            _tone_label(row.get("tone")),
            f"{float(row.get('heat') or 0):.0f}",
        ]
        for row in youtube_candidates[:6]
    ]

    trend_items = [
        item for item in (trends.get("items") or [])
        if item.get("focus")
    ][:8]
    trend_rows = []
    for item in trend_items:
        candidates = "、".join(
            _s(candidate.get("name") or "")
            for candidate in (item.get("candidates") or [])[:2]
            if candidate.get("name")
        )
        related_news = (item.get("news") or [{}])[0].get("title") or ""
        trend_rows.append([
            _s(item.get("term")),
            _s(item.get("traffic_label") or "—"),
            _s(item.get("category") or "其他"),
            candidates or "—",
            _s(related_news) or "—",
        ])

    parts: list[str] = []
    if ptt_counts.get("posts"):
        leader = _s((ptt_candidates[0] or {}).get("name") or "") if ptt_candidates else ""
        parts.append(
            f"近7天PTT共读取{int(ptt_counts.get('posts') or 0)}篇选举相关帖子，"
            f"其中{int(ptt_counts.get('candidate_posts') or 0)}篇点名候选人"
            + (f"，讨论量最高的是{leader}" if leader else "")
            + f"，偏正面{int(ptt_counts.get('positive_posts') or 0)}篇、"
            f"偏负面{int(ptt_counts.get('negative_posts') or 0)}篇。"
        )
    if youtube_counts.get("videos"):
        leader = _s((youtube_candidates[0] or {}).get("name") or "") if youtube_candidates else ""
        parts.append(
            f"YouTube近7天合并后得到{int(youtube_counts.get('videos') or 0)}个相关视频，"
            f"涉及{int(youtube_counts.get('channels') or 0)}个频道，累计播放"
            f"{int(youtube_counts.get('views') or 0):,}次"
            + (f"，候选人视频数量最多的是{leader}" if leader else "")
            + "。"
        )
    if trend_items:
        terms = "、".join(_s(item.get("term")) for item in trend_items[:4])
        parts.append(
            f"Google Trends台湾当前热搜中，政治、选举及社会民生相关条目包括{terms}。"
        )
    if not parts:
        parts.append("本次没有取得可用的PTT、YouTube或Google Trends补充数据。")
    parts.append(
        "上述平台数据用于观察网络讨论和搜索关注，不直接等同于选民支持率，也不改变综合胜率的计算口径。"
    )
    return {
        "summary": "".join(parts),
        "ptt_counts": ptt_counts,
        "ptt_rows": ptt_rows,
        "youtube_counts": youtube_counts,
        "youtube_rows": youtube_rows,
        "trend_rows": trend_rows,
        "warnings": [
            *((ptt.get("warnings") or [])[:1]),
            *((youtube.get("warnings") or [])[:1]),
        ],
    }


def _statement_report_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("statements") or {}
    counts = data.get("counts") or {}
    source_labels = {
        "official_bulletin": "选举公报",
        "official_transcript": "立法院逐字稿",
        "official_account": "官方账号",
        "direct": "新闻直接引语",
        "paraphrase": "新闻转述",
    }
    statements = sorted(
        data.get("statements") or [],
        key=lambda item: str(item.get("published_at") or ""),
        reverse=True,
    )
    statement_rows = [
        [
            _s(str(item.get("published_at") or "")[:10]),
            _s(item.get("candidate_name")),
            _s(item.get("topic") or "其他政策"),
            _s(item.get("claim") or ""),
            source_labels.get(str(item.get("directness") or ""), "其他来源"),
        ]
        for item in statements[:8]
    ]
    pairs = sorted(
        data.get("pairs") or [],
        key=lambda item: (
            -int(item.get("severity") or 0),
            -float(item.get("confidence") or 0),
            str((item.get("newer") or {}).get("published_at") or ""),
        ),
    )
    change_rows = [
        [
            _s(item.get("candidate")),
            _s(item.get("topic") or ""),
            _s(item.get("relation_label") or ""),
            _s((item.get("older") or {}).get("claim") or "")[:80],
            _s((item.get("newer") or {}).get("claim") or "")[:80],
        ]
        for item in pairs[:6]
    ]
    top_candidates = "、".join(
        f"{_s(item.get('candidate'))}（{int(item.get('count') or 0)}条）"
        for item in (data.get("candidate_facets") or [])[:3]
    )
    top_topics = "、".join(
        f"{_s(item.get('topic'))}（{int(item.get('count') or 0)}条）"
        for item in (data.get("topic_facets") or [])[:3]
    )
    if counts.get("statements"):
        official_count = (
            int(counts.get("official_bulletin_statements") or 0)
            + int(counts.get("official_transcript_statements") or 0)
            + int(counts.get("official_account_statements") or 0)
        )
        summary = (
            f"近30天候选人言论库共整理{int(counts.get('statements') or 0)}条可核验言论，"
            f"其中新闻直接引语{int(counts.get('direct_statements') or 0)}条、"
            f"官方文字资料{official_count}条、"
            f"新闻转述{int(counts.get('paraphrase_statements') or 0)}条。"
        )
        if top_candidates:
            summary += f"言论数量较多的候选人包括{top_candidates}；"
        if top_topics:
            summary += f"主要政策主题包括{top_topics}。"
        summary += (
            f"系统识别出{int(counts.get('changes') or 0)}组同题前后表述变化，"
            f"其中{int(counts.get('suspected_inconsistencies') or 0)}组列为疑似前后不一致。"
            "前后变化也可能来自条件、时间或政策调整，结论应以原文和完整语境为准。"
        )
    else:
        summary = "近30天暂未取得可核验的候选人言论记录，本部分不据空白数据作判断。"
    return {
        "summary": summary,
        "statement_rows": statement_rows,
        "change_rows": change_rows,
        "counts": counts,
    }


def build_charts(payload: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    regions = _region_bundle(payload)
    local = [row for row in regions if row["code"] != "TW"]
    focus = _focus_regions(local)
    close = _close_regions(local, limit=10)
    topics = payload.get("topics") or []
    paths = {
        "close": output_dir / "chart-close-regions.png",
        "focus": output_dir / "chart-focus.png",
        "topics": output_dir / "chart-topics.png",
    }

    # 图1：领先差距最小的县市
    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 35), "领先差距最小的十个县市", font=_font(40, True), fill=f"#{INK}")
    draw.text((80, 88), "数值为融合概率前两名之差；越短表示竞争越接近", font=_font(22), fill=f"#{MUTED}")
    for legend_index, (label, color) in enumerate((("国民党", BLUE), ("民进党", GREEN), ("其他", "6B7280"))):
        legend_x = 1160 + legend_index * 125
        draw.rounded_rectangle((legend_x, 72, legend_x + 22, 94), radius=4, fill=f"#{color}")
        draw.text((legend_x + 30, 83), label, font=_font(17), fill=f"#{MUTED}", anchor="lm")
    if close:
        left, right, top = 420, 1450, 155
        max_margin = max(row["margin"] for row in close) or 1
        for index, row in enumerate(close):
            y = top + index * 66
            color = _party_color(row.get("leader_party"))
            draw.text((80, y + 18), row["name"], font=_font(23, True), fill=f"#{INK}", anchor="lm")
            draw.rounded_rectangle((left, y, right, y + 34), radius=10, fill="#EEF2F6")
            end = left + (right - left) * row["margin"] / max_margin
            draw.rounded_rectangle((left, y, max(left + 4, end), y + 34), radius=10, fill=f"#{color}")
            text = f"{_pp(row['margin'])} · {row['leader']} {_pct(row['leader_prob'])}"
            draw.text((min(end + 14, right - 360), y + 17), text, font=_font(19), fill=f"#{color}", anchor="lm")
    image.save(paths["close"], quality=95)

    # 图2：新北与宜兰候选人融合概率
    image = Image.new("RGB", (1600, 780), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 38), "新北市与宜兰县候选人融合概率", font=_font(40, True), fill=f"#{INK}")
    draw.text((80, 92), "柱形长度表示模型输出的胜出概率，候选顺序按概率由高到低", font=_font(22), fill=f"#{MUTED}")
    for panel_index, region in enumerate(focus):
        base_x = 80 + panel_index * 770
        if not region:
            continue
        draw.text((base_x, 150), region["name"], font=_font(31, True), fill=f"#{INK}")
        candidates = [c for c in region["candidates"] if (c.get("fused") or 0) >= 0.005][:4]
        for index, candidate in enumerate(candidates):
            y = 225 + index * 112
            value = max(0.0, min(1.0, float(candidate.get("fused") or 0)))
            party = candidate.get("party") or ""
            color = _party_color(party)
            draw.text((base_x + 175, y), _s(candidate.get("name") or ""), font=_font(23), fill=f"#{color}", anchor="rm")
            bar_left, bar_right = base_x + 195, base_x + 690
            draw.rounded_rectangle((bar_left, y - 16, bar_right, y + 16), radius=10, fill="#EDF1F5")
            end = bar_left + (bar_right - bar_left) * value
            draw.rounded_rectangle((bar_left, y - 16, max(bar_left + 4, end), y + 16), radius=10, fill=f"#{color}")
            draw.text((min(bar_right + 5, end + 14), y), f"{value * 100:.1f}%", font=_font(19, True), fill=f"#{color}", anchor="lm")
    image.save(paths["focus"], quality=95)

    # 图3：议题类别中的高频事件
    image = Image.new("RGB", (1600, 760), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 38), "近期高频议题类别", font=_font(40, True), fill=f"#{INK}")
    draw.text((80, 92), "统计去重代表事件中的议题标签出现次数，不等同于选民支持率", font=_font(22), fill=f"#{MUTED}")
    rows = [(str(topic), int(count)) for topic, count in topics[:8] if count]
    max_count = max((count for _, count in rows), default=1)
    for index, (topic, count) in enumerate(rows):
        y = 165 + index * 70
        draw.text((100, y + 17), topic, font=_font(22), fill=f"#{INK}", anchor="lm")
        draw.rounded_rectangle((420, y, 1440, y + 34), radius=9, fill="#EEF2F6")
        end = 420 + 1020 * count / max_count
        draw.rounded_rectangle((420, y, max(424, end), y + 34), radius=9, fill=f"#{CYAN}")
        draw.text((min(1450, end + 12), y + 17), str(count), font=_font(19, True), fill=f"#{INK}", anchor="lm")
    if not rows:
        draw.text((800, 365), "暂无可用议题标签", font=_font(28), fill=f"#{MUTED}", anchor="mm")
    image.save(paths["topics"], quality=95)
    return paths


def _set_font(run, name: str, size: float, *, bold: bool = False, color: str = INK):
    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:ascii"), name)
    run._element.get_or_add_rPr().rFonts.set(qn("w:hAnsi"), name)
    run.font.size = Pt(size)
    run.bold = bold
    run.font.color.rgb = RGBColor.from_string(color)


def _shade(cell, fill: str):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = tc_pr.find(qn("w:shd"))
    if shd is None:
        shd = OxmlElement("w:shd")
        tc_pr.append(shd)
    shd.set(qn("w:fill"), fill)


def _cell_border(cell):
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = tc_pr.first_child_found_in("w:tcBorders")
    if borders is None:
        borders = OxmlElement("w:tcBorders")
        tc_pr.append(borders)
    for edge in ("top", "left", "bottom", "right"):
        node = borders.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            borders.append(node)
        node.set(qn("w:val"), "single")
        node.set(qn("w:sz"), "5")
        node.set(qn("w:space"), "0")
        node.set(qn("w:color"), BORDER)


def _cell_text(cell, text: str, *, bold=False, color=INK, size=9.2, align=WD_ALIGN_PARAGRAPH.LEFT):
    cell.text = ""
    paragraph = cell.paragraphs[0]
    paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.12
    run = paragraph.add_run(str(text))
    _set_font(run, "Microsoft YaHei", size, bold=bold, color=color)
    cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER


def _repeat_header(row):
    tr_pr = row._tr.get_or_add_trPr()
    node = OxmlElement("w:tblHeader")
    node.set(qn("w:val"), "true")
    tr_pr.append(node)


def _add_table(doc, headers, rows, widths, *, font_size=8.8, first_col_bold=False):
    table = doc.add_table(rows=1, cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    table.style = "Table Grid"
    _repeat_header(table.rows[0])
    for index, header in enumerate(headers):
        cell = table.rows[0].cells[index]
        cell.width = widths[index]
        _cell_text(cell, header, bold=True, color="FFFFFF", size=font_size, align=WD_ALIGN_PARAGRAPH.CENTER)
        _shade(cell, NAVY)
        _cell_border(cell)
    for row_index, values in enumerate(rows):
        row = table.add_row()
        for index, value in enumerate(values):
            cell = row.cells[index]
            cell.width = widths[index]
            align = WD_ALIGN_PARAGRAPH.LEFT if index == 0 else WD_ALIGN_PARAGRAPH.CENTER
            if isinstance(value, dict):
                text = value.get("text") or ""
                color = value.get("color") or INK
                bold = bool(value.get("bold")) or (first_col_bold and index == 0)
            else:
                text = value
                color = INK
                bold = first_col_bold and index == 0
            _cell_text(cell, text, bold=bold, color=color, size=font_size, align=align)
            _shade(cell, "FFFFFF" if row_index % 2 == 0 else LIGHT_BLUE)
            _cell_border(cell)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return table


def _add_caption(doc, text: str):
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(2)
    paragraph.paragraph_format.space_after = Pt(8)
    run = paragraph.add_run(text)
    _set_font(run, BODY_FONT, 8.5, color=MUTED)


def _add_picture(doc, path: Path, width, alt: str):
    shape = doc.add_picture(str(path), width=width)
    doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    shape._inline.docPr.set("title", alt)
    shape._inline.docPr.set("descr", alt)


def _add_body(doc, text: str, *, bold_lead: str | None = None):
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    paragraph.paragraph_format.first_line_indent = Cm(0.74)
    paragraph.paragraph_format.line_spacing = 1.45
    paragraph.paragraph_format.space_after = Pt(5)
    if bold_lead and text.startswith(bold_lead):
        lead = paragraph.add_run(bold_lead)
        _set_font(lead, BODY_FONT, 11.2, bold=True)
        text = text[len(bold_lead):]
    run = paragraph.add_run(text)
    _set_font(run, BODY_FONT, 11.2, color=MUTED if not bold_lead else INK)
    return paragraph


def _add_heading(doc, text: str, level: int = 1, *, page_break: bool = False):
    paragraph = doc.add_paragraph(style="Heading 1" if level == 1 else "Heading 2")
    paragraph.paragraph_format.keep_with_next = True
    paragraph.paragraph_format.page_break_before = page_break
    paragraph.paragraph_format.space_before = Pt(14 if level == 1 else 9)
    paragraph.paragraph_format.space_after = Pt(6 if level == 1 else 4)
    run = paragraph.add_run(text)
    _set_font(run, HEADING_FONT, 15.5 if level == 1 else 12.7, bold=True)
    return paragraph


def _add_page_number(paragraph):
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    prefix = paragraph.add_run("— ")
    _set_font(prefix, BODY_FONT, 9, color=MUTED)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    prefix._r.extend((begin, instr, separate, text, end))
    suffix = paragraph.add_run(" —")
    _set_font(suffix, BODY_FONT, 9, color=MUTED)


def _setup_doc(doc: Document):
    section = doc.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.top_margin = Cm(2.9)
    section.bottom_margin = Cm(2.4)
    section.left_margin = Cm(2.7)
    section.right_margin = Cm(2.5)
    section.header_distance = Cm(1.1)
    section.footer_distance = Cm(1.1)
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), BODY_FONT)
    normal.font.size = Pt(11.2)
    normal.font.color.rgb = RGBColor.from_string(INK)
    normal.paragraph_format.line_spacing = 1.45
    normal.paragraph_format.space_after = Pt(5)
    for name, size in (("Title", 21), ("Heading 1", 15.5), ("Heading 2", 12.7)):
        style = doc.styles[name]
        style.font.name = HEADING_FONT
        style._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), HEADING_FONT)
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.italic = False
        style.font.color.rgb = RGBColor.from_string(INK)
    header = section.header.paragraphs[0]
    header.alignment = WD_ALIGN_PARAGRAPH.LEFT
    run = header.add_run("内部")
    _set_font(run, BODY_FONT, 8.5, color=MUTED)
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = footer.add_run("polymarket投注分析指南  |  ")
    _set_font(run, BODY_FONT, 8, color=MUTED)
    _add_page_number(footer)


def _candidate_rows(
    region: dict[str, Any],
    limit: int = 5,
    *,
    colored: bool = False,
) -> list[list[Any]]:
    rows = []
    market_candidates = region.get("candidates") or []
    registered = (region.get("registered") or [])[:limit]
    if registered:
        for entry in registered:
            name_key = _candidate_name_key(entry.get("name") or "")
            candidate = next(
                (
                    item
                    for item in market_candidates
                    if (
                        _candidate_name_key(item.get("name") or "") == name_key
                        or (
                            len(name_key) >= 3
                            and _candidate_name_key(item.get("name") or "").startswith(name_key)
                        )
                        or (
                            len(name_key) >= 3
                            and name_key.startswith(
                                _candidate_name_key(item.get("name") or "")
                            )
                        )
                    )
                ),
                {},
            )
            party = _s(entry.get("party") or "—")
            fused = _pct(candidate.get("fused"))
            rows.append([
                str(entry.get("number") or "—"),
                _colored_cell(_s(entry.get("name") or "—"), party) if colored else _s(entry.get("name") or "—"),
                _colored_cell(party, party) if colored else party,
                _pct(candidate.get("market")),
                _poll_pct(candidate.get("poll")),
                _colored_cell(fused, party, bold=True) if colored else fused,
            ])
        return rows

    for candidate in market_candidates[:limit]:
        party = _s(candidate.get("party") or "—")
        fused = _pct(candidate.get("fused"))
        rows.append([
            str(candidate.get("number") or "—"),
            _colored_cell(_s(candidate.get("name")), party) if colored else _s(candidate.get("name")),
            _colored_cell(party, party) if colored else party,
            _pct(candidate.get("market")),
            _poll_pct(candidate.get("poll")),
            _colored_cell(fused, party, bold=True) if colored else fused,
        ])
    return rows


def build_docx(
    payload: dict[str, Any],
    charts: dict[str, Path],
    output: Path,
    narrative: dict[str, list[str]] | None = None,
) -> Path:
    settings = payload["settings"]
    regions = _region_bundle(payload)
    local = [row for row in regions if row["code"] != "TW"]
    close = _close_regions(local, limit=10)
    focus = _focus_regions(local)
    focus_map = {row["name"]: row for row in focus if row}
    ntp = next((row for row in local if "新北" in _s(row["name"])), None)
    ilan = next((row for row in local if "宜兰" in _s(row["name"])), None)
    party_rows = _party_expectation(local)
    hot_issues = _hot_issues(payload)
    observe_data = _observe_report_data(payload)
    platform_data = _platform_report_data(payload)
    statement_data = _statement_report_data(payload)
    opinion_days = int(payload.get("opinion_days") or 7)
    candidate_sentiment = _candidate_sentiment_rows(local, payload.get("opinion_news"))
    overall_sentiment, overall_tone = _overall_tone(payload.get("trend") or [])
    countdown = _election_countdown(settings)
    generated = payload["generated_at"]
    stats = payload["stats"]

    doc = Document()
    _setup_doc(doc)
    doc.core_properties.title = "polymarket投注分析指南"
    doc.core_properties.subject = "选情分析报告"
    doc.core_properties.author = "选举舆情监测系统"
    doc.core_properties.keywords = "台湾, 九合一, 舆情监测, 选情分析, 新北, 宜兰"

    title = doc.add_paragraph(style="Title")
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_before = Pt(24)
    title.paragraph_format.space_after = Pt(8)
    run = title.add_run("polymarket投注分析指南")
    _set_font(run, HEADING_FONT, 21, bold=True)

    _add_table(
        doc,
        ["项目", "说明"],
        [
            ("数据范围", f"22个县市；新闻舆情采用近{opinion_days}天数据，报告汇集近{payload['days']}天材料"),
            ("分析范围", "县市长选情、新闻舆情、热点议题；新北与宜兰为重点观察地区"),
        ],
        [Inches(1.25), Inches(5.2)],
        font_size=9.0,
        first_col_bold=True,
    )

    _add_body(
        doc,
        f"截至{generated.strftime('%Y年%m月%d日')}，系统已汇集近{payload['days']}天{stats.get('articles', 0)}则新闻。"
        "经舆情分析、融合市场赔率、民调信息等，做出以下分析报告。",
    )

    if party_rows:
        top_parties = "；".join(f"{party}约{seats:.2f}席" for party, seats, _ in party_rows[:4])
        _add_body(
            doc,
            f"从22个县市的融合概率加总看，{top_parties}。该数值是各县市胜选概率的期望加总，"
            "不是全岛政党得票率，也不代表已经确定取得相应席次。",
            bold_lead="核心判断。",
        )

    _add_heading(doc, "一 总体态势", 1, page_break=True)
    overall_fallback = (
        "当前最接近的选区包括" + "、".join(row["name"] for row in close[:5]) + "。"
        "这些选区的融合概率差距较小，市场价格或单篇新闻变化就可能改变领先顺序，应提高复核频率。"
    )
    for paragraph in _narrative_paragraphs(narrative, "overall", overall_fallback):
        _add_body(doc, paragraph)
    _add_picture(doc, charts["close"], Inches(6.25), "领先差距最小的十个县市柱状图")
    _add_caption(doc, "图1  领先差距最小的十个县市")
    overall_rows = [
        [
            row["name"],
            _colored_cell(row["leader"], row.get("leader_party")),
            _colored_cell(row["runner"], row.get("runner_party")),
            _pp(row["margin"]),
            _colored_cell(_pct(row["leader_prob"]), row.get("leader_party"), bold=True),
        ]
        for row in close[:8]
    ]
    _add_table(
        doc,
        ["县市", "融合领先者", "次位候选人", "领先差距", "领先者概率"],
        overall_rows,
        [Inches(0.92), Inches(1.2), Inches(1.2), Inches(1.05), Inches(1.0)],
        font_size=8.6,
        first_col_bold=True,
    )
    _add_body(
        doc,
        "上述差距来自市场、民调和舆情的综合结果，不是单一民调数字。差距越小，结论越容易受到未表态选民、"
        "少量市场交易和突发新闻影响。",
    )

    _add_heading(doc, "二 舆情分析", 1)
    sentiment_fallback = (
        f"近{opinion_days}天新闻舆情{overall_tone}"
        + (f"，按每日事件数量加权的情感分为{overall_sentiment:+.2f}。" if overall_sentiment is not None else "。")
        + "该分数只表示公开报道对相关候选人及选区的总体语气，不代表选民支持率。"
    )
    for paragraph in _narrative_paragraphs(narrative, "sentiment", sentiment_fallback):
        _add_body(doc, paragraph)
    if candidate_sentiment:
        _add_body(doc, "从重点候选人的新闻覆盖看，近期相关事件量与情感方向如下：")
        _add_table(
            doc,
            ["县市", "领先候选人", f"近{opinion_days}天相关事件", "情感分", "方向"],
            candidate_sentiment,
            [Inches(0.9), Inches(1.25), Inches(0.9), Inches(0.85), Inches(0.9)],
            font_size=8.3,
            first_col_bold=True,
        )
    _add_picture(doc, charts["topics"], Inches(6.25), "近期高频议题类别柱状图")
    _add_caption(doc, "图2  近期高频议题类别")
    if hot_issues:
        issue_rows = []
        ranked_issues = sorted(
            hot_issues,
            key=lambda issue: (-float(issue.get("impact") or 0), -float(issue.get("rank_score") or 0)),
        )
        for issue in ranked_issues[:6]:
            issue_rows.append([
                _s(issue.get("title")),
                _issue_categories(issue),
                f"{int(round(float(issue.get('impact') or 0) * 100))}%",
                issue.get("swing_label") or "—",
                _party_effect_text(issue),
            ])
        _add_table(
            doc,
            ["具体事件", "议题类别", "新闻影响", "中间选民判断", "党派得失"],
            issue_rows,
            [Inches(2.15), Inches(0.95), Inches(0.62), Inches(1.1), Inches(1.45)],
            font_size=7.6,
            first_col_bold=True,
        )
    _add_body(
        doc,
        "高频议题集中在民调选情、竞选活动、政策承诺、政党整合、廉政司法和灾害治理。"
        "能被中间选民直接感知的题目，主要是就业、住房、交通、育儿、长者照顾和地方治理质量。"
        "这些题目的新闻热度只能说明讨论强度，是否改变投票意向仍需结合正式民调。"
        "同一事件同时涉及蓝绿候选人时，系统才给出党派相对得失，情感差距不足6个百分点时不强行判断。",
    )

    def add_focus(region: dict[str, Any] | None, heading: str, chart_added: bool = False):
        if not region:
            return
        _add_heading(doc, heading, 1)
        key = "ntp" if "新北" in _s(region.get("name")) else "yilan"
        for paragraph in _narrative_paragraphs(narrative, key, _narrative(region, payload)):
            _add_body(doc, paragraph)
        _add_table(
            doc,
            ["号次", "候选人", "政党", "市场概率", "民调支持度", "融合概率"],
            _candidate_rows(region, colored=True),
            [Inches(0.48), Inches(1.05), Inches(1.18), Inches(0.9), Inches(1.0), Inches(0.9)],
            font_size=8.6,
            first_col_bold=False,
        )
        if chart_added:
            _add_picture(doc, charts["focus"], Inches(5.8), "新北市与宜兰县候选人融合概率对比图")
            _add_caption(doc, "图3  新北市与宜兰县候选人融合概率")

    add_focus(ntp, "三 新北市选情")
    add_focus(ilan, "四 宜兰县选情", chart_added=True)

    _add_heading(doc, "五 热点事件追踪", 1)
    for paragraph in _narrative_paragraphs(narrative, "hot_events", _hot_summary(payload, hot_issues)):
        _add_body(doc, paragraph)
    event_rows = []
    ranked_events = sorted(
        payload.get("hot_events") or [],
        key=lambda event: (-float(event.get("impact") or 0), -int(event.get("size") or 1)),
    )
    for event in ranked_events[:6]:
        event_rows.append([
            _s(event.get("date")),
            _s(event.get("title")),
            f"{int(round(float(event.get('impact') or 0) * 100))}%",
            f"{int(event.get('size') or 1)}家",
            _event_sources(event),
        ])
    if event_rows:
        _add_table(
            doc,
            ["日期", "事件", "新闻影响", "媒体覆盖", "主要来源"],
            event_rows,
            [Inches(0.58), Inches(2.55), Inches(0.65), Inches(0.62), Inches(1.55)],
            font_size=7.8,
            first_col_bold=True,
        )

    _add_heading(doc, "六 网络平台舆情补充", 1)
    platform_fallback = platform_data["summary"]
    for paragraph in _narrative_paragraphs(narrative, "platforms", platform_fallback):
        _add_body(doc, paragraph)
    if platform_data["ptt_rows"]:
        _add_body(doc, "PTT近7天候选人讨论情况如下：")
        _add_table(
            doc,
            ["候选人", "县市／政党", "讨论帖", "语气分", "方向", "争议帖"],
            platform_data["ptt_rows"],
            [Inches(1.0), Inches(1.75), Inches(0.7), Inches(0.68), Inches(0.72), Inches(0.7)],
            font_size=7.8,
            first_col_bold=True,
        )
    if platform_data["youtube_rows"]:
        _add_body(doc, "YouTube近7天候选人视频传播情况如下：")
        _add_table(
            doc,
            ["候选人", "政党", "视频", "累计播放", "方向", "热度"],
            platform_data["youtube_rows"],
            [Inches(1.05), Inches(1.35), Inches(0.65), Inches(0.95), Inches(0.75), Inches(0.65)],
            font_size=7.8,
            first_col_bold=True,
        )
    if platform_data["trend_rows"]:
        _add_body(doc, "Google Trends台湾当前政治、选举及社会民生热搜如下：")
        _add_table(
            doc,
            ["热搜词", "近似热度", "类别", "关联候选人", "关联新闻"],
            platform_data["trend_rows"],
            [Inches(1.15), Inches(0.7), Inches(0.75), Inches(1.0), Inches(1.8)],
            font_size=7.6,
            first_col_bold=True,
        )

    _add_heading(doc, "七 候选人言论追踪", 1)
    statement_fallback = statement_data["summary"]
    for paragraph in _narrative_paragraphs(narrative, "statements", statement_fallback):
        _add_body(doc, paragraph)
    if statement_data["statement_rows"]:
        _add_body(doc, "近期较具代表性的候选人言论如下：")
        _add_table(
            doc,
            ["日期", "候选人", "议题", "言论摘要", "来源类型"],
            statement_data["statement_rows"],
            [Inches(0.62), Inches(0.82), Inches(0.82), Inches(2.95), Inches(0.9)],
            font_size=7.3,
            first_col_bold=True,
        )
    if statement_data["change_rows"]:
        _add_body(doc, "同题表述出现变化、需要回到原文核验的项目如下：")
        _add_table(
            doc,
            ["候选人", "议题", "变化类型", "较早表述", "较新表述"],
            statement_data["change_rows"],
            [Inches(0.72), Inches(0.82), Inches(0.88), Inches(1.85), Inches(1.85)],
            font_size=7.2,
            first_col_bold=True,
        )

    _add_heading(doc, "八 新文易数舆情补充", 1, page_break=True)
    external_fallback = observe_data["summary"] + observe_data["relation_text"]
    for paragraph in _narrative_paragraphs(narrative, "external", external_fallback):
        _add_body(doc, paragraph)
    if observe_data["event_rows"]:
        _add_body(doc, "新文易数近7天事件及媒体覆盖情况如下：")
        _add_table(
            doc,
            ["选举相关事件", "事件时间范围", "报道覆盖", "代表报道"],
            observe_data["event_rows"],
            [Inches(1.9), Inches(1.05), Inches(0.8), Inches(2.35)],
            font_size=7.4,
            first_col_bold=True,
        )
    if observe_data["trend_rows"]:
        _add_body(doc, "当前新闻热词榜及关联标签如下：")
        _add_table(
            doc,
            ["热词", "新闻量", "爆发值", "关联标签"],
            observe_data["trend_rows"],
            [Inches(1.15), Inches(0.7), Inches(0.7), Inches(3.3)],
            font_size=8.0,
            first_col_bold=True,
        )
    if observe_data["warnings"]:
        _add_body(doc, "外部接口本次存在更新提示：" + "；".join(map(str, observe_data["warnings"][:2])))
    _add_body(doc, "以上数据用于补充事件覆盖、热词和议题关系，媒体篇数及热度不代表民调支持率，也不进入胜率公式。")

    _add_heading(doc, "附录 22县市选情预测总表", 1, page_break=True)
    appendix_rows = []
    for region in sorted(local, key=lambda row: -float((row["candidates"][0].get("fused") or 0))):
        top = region["candidates"][0]
        runner = region["candidates"][1] if len(region["candidates"]) > 1 else None
        margin = float(top.get("fused") or 0) - float((runner or {}).get("fused") or 0)
        appendix_rows.append([
            _s(region["name"]),
            _colored_cell(_s(top.get("name")), top.get("party")),
            _colored_cell(_s(top.get("party") or "—"), top.get("party")),
            _colored_cell(_pct(top.get("fused")), top.get("party"), bold=True),
            _pct(top.get("market")),
            _poll_pct(top.get("poll")),
            _pp(margin),
        ])
    _add_table(
        doc,
        ["县市", "领先候选人", "政党", "融合概率", "市场概率", "民调支持度", "领先差距"],
        appendix_rows,
        [Inches(0.76), Inches(1.05), Inches(1.08), Inches(0.78), Inches(0.78), Inches(0.88), Inches(0.9)],
        font_size=7.8,
        first_col_bold=True,
    )

    _add_heading(doc, "说明事项", 1)
    _add_body(doc, "本报告为内部研判材料，所载概率为系统估计。")
    _add_body(doc, "以上报告，请审阅。")
    output.parent.mkdir(parents=True, exist_ok=True)
    doc.save(output)
    return output


def _html_table(headers: list[str], rows: list[list[Any]], classes: str = "") -> str:
    def render_cell(cell: Any) -> str:
        if isinstance(cell, dict):
            value = html.escape(str(cell.get("text") or ""))
            css_class = html.escape(str(cell.get("class") or ""))
            color = html.escape(str(cell.get("color") or ""))
            weight = "700" if cell.get("bold") else "600"
            return f'<td class="{css_class}" style="color:#{color};font-weight:{weight}">{value}</td>'
        return f"<td>{html.escape(str(cell))}</td>"

    return (
        f'<table class="{classes}"><thead><tr>'
        + "".join(f"<th>{html.escape(str(item))}</th>" for item in headers)
        + "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(render_cell(cell) for cell in row) + "</tr>" for row in rows)
        + "</tbody></table>"
    )


def build_html(
    payload: dict[str, Any],
    charts: dict[str, Path],
    narrative: dict[str, list[str]] | None = None,
) -> str:
    settings = payload["settings"]
    regions = _region_bundle(payload)
    local = [row for row in regions if row["code"] != "TW"]
    close = _close_regions(local, limit=10)
    ntp = next((row for row in local if "新北" in _s(row["name"])), None)
    ilan = next((row for row in local if "宜兰" in _s(row["name"])), None)
    party_rows = _party_expectation(local)
    hot_issues = _hot_issues(payload)
    observe_data = _observe_report_data(payload)
    platform_data = _platform_report_data(payload)
    statement_data = _statement_report_data(payload)
    opinion_days = int(payload.get("opinion_days") or 7)
    candidate_sentiment = _candidate_sentiment_rows(local, payload.get("opinion_news"))
    overall_sentiment, overall_tone = _overall_tone(payload.get("trend") or [])
    points = _summary_points(regions, payload)
    countdown = _election_countdown(settings)
    stats = payload["stats"]
    generated = payload["generated_at"]

    def region_html(region: dict[str, Any] | None, heading: str, key: str) -> str:
        if not region:
            return ""
        table = _html_table(
            ["号次", "候选人", "政党", "市场概率", "民调支持度", "融合概率"],
            _candidate_rows(region, colored=True),
            "data",
        )
        paragraphs = _narrative_paragraphs(narrative, key, _narrative(region, payload))
        text = "".join(f"<p>{html.escape(_s(paragraph))}</p>" for paragraph in paragraphs)
        return f'<h2>{html.escape(heading)}</h2>{text}{table}'

    overview_rows = [
        [
            row["name"],
            _colored_cell(row["leader"], row.get("leader_party")),
            _colored_cell(row["runner"], row.get("runner_party")),
            _pp(row["margin"]),
            _colored_cell(_pct(row["leader_prob"]), row.get("leader_party"), bold=True),
        ]
        for row in close[:8]
    ]
    issue_rows = []
    ranked_issues = sorted(
        hot_issues,
        key=lambda issue: (-float(issue.get("impact") or 0), -float(issue.get("rank_score") or 0)),
    )
    for issue in ranked_issues[:8]:
        issue_rows.append([
            issue.get("title") or "",
            _issue_categories(issue),
            f"{int(round(float(issue.get('impact') or 0) * 100))}%",
            issue.get("swing_label") or "—",
            _party_effect_text(issue),
        ])
    ranked_events = sorted(
        payload.get("hot_events") or [],
        key=lambda event: (-float(event.get("impact") or 0), -int(event.get("size") or 1)),
    )
    event_rows = [
        [
            event.get("date"),
            event.get("title"),
            f"{int(round(float(event.get('impact') or 0) * 100))}%",
            f"{int(event.get('size') or 1)}家",
            _event_sources(event),
        ]
        for event in ranked_events[:10]
    ]
    appendix_rows = []
    for region in sorted(local, key=lambda row: -float(row["candidates"][0].get("fused") or 0)):
        top = region["candidates"][0]
        runner = region["candidates"][1] if len(region["candidates"]) > 1 else {}
        margin = float(top.get("fused") or 0) - float(runner.get("fused") or 0)
        appendix_rows.append([
            region["name"],
            _colored_cell(top.get("name"), top.get("party")),
            _colored_cell(top.get("party") or "—", top.get("party")),
            _colored_cell(_pct(top.get("fused")), top.get("party"), bold=True),
            _pct(top.get("market")),
            _poll_pct(top.get("poll")),
            _pp(margin),
        ])
    party_text = "；".join(f"{party}约{seats:.2f}席" for party, seats, _ in party_rows[:4])
    close_names = "、".join(row["name"] for row in close[:5])
    chart_url = lambda key: f"report-assets/{charts[key].name}"
    points_html = "".join(f"<li>{html.escape(_s(point))}</li>" for point in points)
    overall_fallback = (
        f"当前领先差距最小的县市为{close_names}。这些选区对单次民调、短期市场和突发新闻更敏感，"
        f"应提高复核频率。{party_text}；该数值是各县市融合概率的期望加总，不是全岛政党得票率。"
    )
    sentiment_fallback = (
        f"近{opinion_days}天新闻舆情{overall_tone}"
        + (f"，按每日事件数量加权的情感分为{overall_sentiment:+.2f}。" if overall_sentiment is not None else "。")
        + "该分数只表示公开报道对相关候选人及选区的总体语气，不代表选民支持率。"
    )
    hot_fallback = _hot_summary(payload, hot_issues)
    external_fallback = observe_data["summary"] + observe_data["relation_text"]
    platform_fallback = platform_data["summary"]
    statement_fallback = statement_data["summary"]

    def html_paragraphs(key: str, fallback: str) -> str:
        return "".join(
            f"<p>{html.escape(_s(paragraph))}</p>"
            for paragraph in _narrative_paragraphs(narrative, key, fallback)
        )

    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>polymarket投注分析指南</title>
<style>
:root{{--ink:#111827;--muted:#5b6472;--line:#d9dee7;--soft:#f4f7fa;--blue:#2f6fb3;--navy:#17365d;--teal:#168a9a}}
*{{box-sizing:border-box}}
body{{margin:0;background:#e9edf2;color:var(--ink);font-family:"FangSong","STFangsong","Microsoft YaHei",serif;line-height:1.75}}
.toolbar{{max-width:960px;margin:18px auto 0;display:flex;justify-content:flex-end;gap:10px}}
.toolbar a,.toolbar button{{border:1px solid #b8c4d2;background:#fff;color:#17365d;border-radius:6px;padding:8px 14px;text-decoration:none;font:14px "Microsoft YaHei",sans-serif;cursor:pointer}}
.paper{{width:min(960px,calc(100% - 28px));margin:14px auto 36px;background:#fff;padding:54px 64px 48px;box-shadow:0 8px 32px rgba(18,35,55,.1)}}
.mark{{text-align:left;color:var(--muted);font:12px "Microsoft YaHei",sans-serif;letter-spacing:.18em}}
h1{{text-align:center;font:700 28px/1.45 "SimHei","Microsoft YaHei",sans-serif;margin:18px 0 6px}}
h2{{font:700 18px/1.5 "SimHei","Microsoft YaHei",sans-serif;color:#000;margin:30px 0 12px;break-after:avoid}}
h3{{font:700 14px/1.5 "SimHei","Microsoft YaHei",sans-serif;color:#000;margin:20px 0 8px;break-after:avoid}}
p{{text-align:justify;text-indent:2em;margin:0 0 9px;font-size:15px}}
ul{{margin:6px 0 14px 24px;padding:0}}li{{margin:4px 0;font-size:14.5px}}
table{{width:100%;border-collapse:collapse;margin:10px 0 15px;font:12px "Microsoft YaHei",sans-serif;break-inside:avoid}}
th,td{{border:1px solid var(--line);padding:7px 8px;vertical-align:middle;text-align:center}}
th{{background:var(--navy);color:white;font-weight:700}}tbody tr:nth-child(even){{background:#f4f8fc}}
td:first-child{{text-align:left;font-weight:600;color:#24344b}}
.party-blue{{color:#2f6fb3!important}}.party-green{{color:#2c8c5a!important}}
.party-amber{{color:#b7791f!important}}.party-other{{color:#6b7280!important}}
.meta{{margin:12px 0 18px}}
.download{{display:inline-block;background:var(--navy);color:white;text-decoration:none;border-radius:6px;padding:8px 14px;font:14px "Microsoft YaHei",sans-serif}}
figure{{margin:18px 0;text-align:center;break-inside:avoid}}
figure img{{max-width:100%;height:auto}}figcaption{{font:12px "Microsoft YaHei",sans-serif;color:var(--muted);margin-top:5px}}
.muted{{color:var(--muted);font:12px "Microsoft YaHei",sans-serif;text-indent:0}}
@media(max-width:720px){{.paper{{padding:34px 20px}}h1{{font-size:23px}}p{{font-size:14px}}}}
@media print{{
@page{{size:A4;margin:20mm 18mm}}body{{background:white}}.toolbar{{display:none}}.paper{{width:auto;margin:0;padding:0;box-shadow:none}}
h2{{break-after:avoid}}table,figure{{break-inside:avoid}}p{{orphans:3;widows:3}}
}}
</style>
</head>
<body>
<div class="toolbar"><a href="/exports/report.docx" download>下载Word版</a><button onclick="window.print()">打印/另存PDF</button></div>
<main class="paper">
<div class="mark">内部</div>
<h1>polymarket投注分析指南</h1>
<p>截至{generated.strftime('%Y年%m月%d日')}，系统已汇集近{payload['days']}天{stats.get('articles',0)}则新闻。
经舆情分析、融合市场赔率、民调信息等，做出以下分析报告。</p>
<h2>一 总体态势</h2>
{html_paragraphs('overall', overall_fallback)}
<figure><img src="{chart_url('close')}" alt="领先差距最小的十个县市"><figcaption>图1　领先差距最小的十个县市</figcaption></figure>
{_html_table(['县市','融合领先者','次位候选人','领先差距','领先者概率'], overview_rows, 'data')}
<h2>二 舆情分析</h2>
{html_paragraphs('sentiment', sentiment_fallback)}
{_html_table(['县市','领先候选人','相关事件','情感分','方向'], candidate_sentiment, 'data') if candidate_sentiment else ''}
<figure><img src="{chart_url('topics')}" alt="近期高频议题类别"><figcaption>图2　近期高频议题类别</figcaption></figure>
{_html_table(['具体事件','议题类别','新闻影响','中间选民判断','党派得失'], issue_rows, 'data') if issue_rows else '<p class="muted">暂无可用热点议题。</p>'}
<p>当前议题主要集中在民调选情、竞选活动、政策承诺、政党整合、廉政司法和灾害治理。能直接触及中间选民的题目主要是就业、住房、交通、育儿、长者照顾和地方治理质量。新闻热度表示讨论强度，是否改变投票意向仍需正式民调验证。</p>
{region_html(ntp, "三 新北市选情", "ntp")}
{region_html(ilan, "四 宜兰县选情", "yilan")}
<figure><img src="{chart_url('focus')}" alt="新北市与宜兰县候选人融合概率"><figcaption>图3　新北市与宜兰县候选人融合概率</figcaption></figure>
<h2>五 热点事件追踪</h2>
{html_paragraphs('hot_events', hot_fallback)}
{_html_table(['日期','事件','新闻影响','媒体覆盖','主要来源'], event_rows, 'data') if event_rows else '<p class="muted">暂无多来源热点事件。</p>'}
<h2>六 网络平台舆情补充</h2>
{html_paragraphs('platforms', platform_fallback)}
{('<h3>PTT近7天候选人讨论</h3>' + _html_table(['候选人','县市／政党','讨论帖','语气分','方向','争议帖'], platform_data['ptt_rows'], 'data')) if platform_data['ptt_rows'] else ''}
{('<h3>YouTube近7天候选人视频传播</h3>' + _html_table(['候选人','政党','视频','累计播放','方向','热度'], platform_data['youtube_rows'], 'data')) if platform_data['youtube_rows'] else ''}
{('<h3>Google Trends台湾当前热搜</h3>' + _html_table(['热搜词','近似热度','类别','关联候选人','关联新闻'], platform_data['trend_rows'], 'data')) if platform_data['trend_rows'] else ''}
<h2>七 候选人言论追踪</h2>
{html_paragraphs('statements', statement_fallback)}
{('<h3>近期代表性言论</h3>' + _html_table(['日期','候选人','议题','言论摘要','来源类型'], statement_data['statement_rows'], 'data')) if statement_data['statement_rows'] else ''}
{('<h3>同题表述变化</h3>' + _html_table(['候选人','议题','变化类型','较早表述','较新表述'], statement_data['change_rows'], 'data')) if statement_data['change_rows'] else ''}
<h2>八 新文易数舆情补充</h2>
{html_paragraphs('external', external_fallback)}
{_html_table(['选举相关事件','事件时间范围','报道覆盖','代表报道'], observe_data['event_rows'], 'data') if observe_data['event_rows'] else ''}
{_html_table(['热词','新闻量','爆发值','关联标签'], observe_data['trend_rows'], 'data') if observe_data['trend_rows'] else ''}
{('<p class="muted">' + html.escape('；'.join(map(str, observe_data['warnings'][:2]))) + '</p>') if observe_data['warnings'] else ''}
<p class="muted">以上数据用于补充事件覆盖、热词和议题关系，媒体篇数及热度不代表民调支持率，也不进入胜率公式。</p>
<h2>附录 22县市选情预测总表</h2>
{_html_table(['县市','领先候选人','政党','融合概率','市场概率','民调支持度','领先差距'], appendix_rows, 'data')}
<h2>说明事项</h2>
<p>本报告为内部研判材料，所载概率为系统估计。</p>
<p class="muted">数据生成时间：{generated.strftime('%Y-%m-%d %H:%M')}（UTC+8）　新闻窗口：近{payload['days']}天</p>
</main>
</body>
</html>"""


def _md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |")
    return "\n".join(lines)


def build_markdown(
    payload: dict[str, Any],
    narrative: dict[str, list[str]] | None = None,
) -> str:
    settings = payload["settings"]
    regions = _region_bundle(payload)
    local = [row for row in regions if row["code"] != "TW"]
    close = _close_regions(local, limit=10)
    ntp = next((row for row in local if "新北" in _s(row["name"])), None)
    ilan = next((row for row in local if "宜兰" in _s(row["name"])), None)
    opinion_days = int(payload.get("opinion_days") or 7)
    candidate_sentiment = _candidate_sentiment_rows(local, payload.get("opinion_news"))
    overall_sentiment, overall_tone = _overall_tone(payload.get("trend") or [])
    hot_issues = _hot_issues(payload)
    observe_data = _observe_report_data(payload)
    platform_data = _platform_report_data(payload)
    statement_data = _statement_report_data(payload)
    party_rows = _party_expectation(local)
    stats = payload["stats"]
    generated = payload["generated_at"]
    countdown = _election_countdown(settings)
    close_names = "、".join(row["name"] for row in close[:5])
    party_text = "；".join(f"{party}约{seats:.2f}席" for party, seats, _ in party_rows[:4])
    appendix_rows = []
    for region in sorted(local, key=lambda row: -float(row["candidates"][0].get("fused") or 0)):
        top = region["candidates"][0]
        runner = region["candidates"][1] if len(region["candidates"]) > 1 else {}
        margin = float(top.get("fused") or 0) - float(runner.get("fused") or 0)
        appendix_rows.append([
            region["name"], top.get("name"), top.get("party") or "—",
            _pct(top.get("fused")), _pct(top.get("market")), _poll_pct(top.get("poll")), _pp(margin),
        ])
    ranked_issues = sorted(
        hot_issues,
        key=lambda issue: (-float(issue.get("impact") or 0), -float(issue.get("rank_score") or 0)),
    )
    issue_rows = [
        [
            issue.get("title") or "",
            _issue_categories(issue),
            f"{int(round(float(issue.get('impact') or 0) * 100))}%",
            issue.get("swing_label") or "—",
            _party_effect_text(issue),
        ]
        for issue in ranked_issues[:8]
    ]
    ranked_events = sorted(
        payload.get("hot_events") or [],
        key=lambda event: (-float(event.get("impact") or 0), -int(event.get("size") or 1)),
    )
    event_rows = [
        [
            event.get("date"),
            event.get("title"),
            f"{int(round(float(event.get('impact') or 0) * 100))}%",
            f"{int(event.get('size') or 1)}家",
            _event_sources(event),
        ]
        for event in ranked_events[:10]
    ]
    overall_fallback = (
        f"当前领先差距最小的县市为{close_names}。这些选区对单次民调、短期市场和突发新闻更敏感，"
        f"应提高复核频率。{party_text}。该数值是各县市融合概率的期望加总，不是全岛政党得票率。"
    )
    sentiment_fallback = (
        f"近{opinion_days}天新闻舆情{overall_tone}"
        + (f"，按每日事件数量加权的情感分为{overall_sentiment:+.2f}。" if overall_sentiment is not None else "。")
        + "该分数只表示公开报道对相关候选人及选区的总体语气，不代表选民支持率。"
    )
    hot_fallback = _hot_summary(payload, hot_issues)
    external_fallback = observe_data["summary"] + observe_data["relation_text"]
    platform_fallback = platform_data["summary"]
    statement_fallback = statement_data["summary"]
    paragraph_text = lambda key, fallback: "\n\n".join(_narrative_paragraphs(narrative, key, fallback))
    lines = [
        "内部",
        "",
        "# polymarket投注分析指南",
        "",
        f"截至{generated.strftime('%Y年%m月%d日')}，系统已汇集近{payload['days']}天{stats.get('articles',0)}则新闻。",
        "经舆情分析、融合市场赔率、民调信息等，做出以下分析报告。",
        "",
        "## 一 总体态势",
        "",
        paragraph_text("overall", overall_fallback),
        "",
        "![图1 领先差距最小的十个县市](report-assets/chart-close-regions.png)",
        "",
        _md_table(
            ["县市", "融合领先者", "次位候选人", "领先差距", "领先者概率"],
            [[row["name"], row["leader"], row["runner"], _pp(row["margin"]), _pct(row["leader_prob"])] for row in close[:8]],
        ),
        "",
        "## 二 舆情分析",
        "",
        paragraph_text("sentiment", sentiment_fallback),
        "",
        _md_table(["县市", "领先候选人", "选区相关事件", "情感分", "方向"], candidate_sentiment),
        "",
        "![图2 近期高频议题类别](report-assets/chart-topics.png)",
        "",
        _md_table(["具体事件", "议题类别", "新闻影响", "中间选民判断", "党派得失"], issue_rows),
        "",
        "## 三 新北市选情",
        "",
        paragraph_text("ntp", _s(_narrative(ntp, payload))) if ntp else "",
        "",
        _md_table(["号次", "候选人", "政党", "市场概率", "民调支持度", "融合概率"], _candidate_rows(ntp)) if ntp else "",
        "",
        "## 四 宜兰县选情",
        "",
        paragraph_text("yilan", _s(_narrative(ilan, payload))) if ilan else "",
        "",
        _md_table(["号次", "候选人", "政党", "市场概率", "民调支持度", "融合概率"], _candidate_rows(ilan)) if ilan else "",
        "",
        "![图3 新北市与宜兰县候选人融合概率](report-assets/chart-focus.png)",
        "",
        "## 五 热点事件追踪",
        "",
        paragraph_text("hot_events", hot_fallback),
        "",
        _md_table(["日期", "事件", "新闻影响", "媒体覆盖", "主要来源"], event_rows),
        "",
        "## 六 网络平台舆情补充",
        "",
        paragraph_text("platforms", platform_fallback),
        "",
        "### PTT近7天候选人讨论",
        "",
        _md_table(["候选人", "县市／政党", "讨论帖", "语气分", "方向", "争议帖"], platform_data["ptt_rows"]) if platform_data["ptt_rows"] else "暂无PTT候选人讨论数据。",
        "",
        "### YouTube近7天候选人视频传播",
        "",
        _md_table(["候选人", "政党", "视频", "累计播放", "方向", "热度"], platform_data["youtube_rows"]) if platform_data["youtube_rows"] else "暂无YouTube候选人视频数据。",
        "",
        "### Google Trends台湾当前热搜",
        "",
        _md_table(["热搜词", "近似热度", "类别", "关联候选人", "关联新闻"], platform_data["trend_rows"]) if platform_data["trend_rows"] else "暂无可用Google Trends数据。",
        "",
        "## 七 候选人言论追踪",
        "",
        paragraph_text("statements", statement_fallback),
        "",
        "### 近期代表性言论",
        "",
        _md_table(["日期", "候选人", "议题", "言论摘要", "来源类型"], statement_data["statement_rows"]) if statement_data["statement_rows"] else "暂无候选人言论记录。",
        "",
        "### 同题表述变化",
        "",
        _md_table(["候选人", "议题", "变化类型", "较早表述", "较新表述"], statement_data["change_rows"]) if statement_data["change_rows"] else "暂无需要列出的同题表述变化。",
        "",
        "## 八 新文易数舆情补充",
        "",
        paragraph_text("external", external_fallback),
        "",
        _md_table(["选举相关事件", "事件时间范围", "报道覆盖", "代表报道"], observe_data["event_rows"]) if observe_data["event_rows"] else "",
        "",
        _md_table(["热词", "新闻量", "爆发值", "关联标签"], observe_data["trend_rows"]) if observe_data["trend_rows"] else "",
        "",
        "以上数据用于补充事件覆盖、热词和议题关系，媒体篇数及热度不代表民调支持率，也不进入胜率公式。",
        "",
        "## 附录 22县市选情预测总表",
        "",
        _md_table(["县市", "领先候选人", "政党", "融合概率", "市场概率", "民调支持度", "领先差距"], appendix_rows),
        "",
        "## 说明事项",
        "",
        "本报告为内部研判材料，所载概率为系统估计。",
    ]
    return "\n".join(lines)
