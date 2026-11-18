"""使用已配置的大模型润色分析报道正文。

模型只接收系统已经计算好的事实包，不负责抓数、估值或胜率计算。输出经过
数字与禁用表述校验；任一段落不合格时整篇回退现有模板，避免半真半假的报告。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .config import Settings
from .llm import LLMClient, LLMError

SECTION_KEYS = (
    "overall",
    "sentiment",
    "ntp",
    "yilan",
    "hot_events",
    "platforms",
    "statements",
    "external",
)
_NUMERIC = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)")
_BANNED = (
    "系统概述",
    "方法与数据源",
    "数据来源",
    "胜率计算方法",
    "工作建议",
    "内部参考",
    "内部汇报稿",
    "信心",
    "数据充分度",
    "基线先验",
    "不构成投资、交易或投票建议",
    "事实包",
    "新闻窗口",
    "舆情窗口",
    "计算过程",
    "算法",
)

SYSTEM_PROMPT = """你是严谨的台湾选举舆情报告撰稿人。你只负责把给定的资料组织成正式、流畅的中文分析报告。

必须遵守：
1. 只能使用给定资料中已有的数据、日期、人物、事件、机构和判断，不得补充任何外部信息。
2. 所有数字必须原样使用给定资料中的值，不得四舍五入、换算或自行创造百分比。
3. 不得写“系统概述”“方法与数据源”“胜率计算方法”“工作建议”等章节。
4. 不得写“信心”“数据充分度”“基线先验”或投资、交易、投票免责声明。
5. 不得把媒体热度、热搜或媒体覆盖写成选民支持度，不得把共同出现写成因果关系。
6. 不要输出标题、表格、项目符号或 Markdown，只写正文段落。
7. 文风应正式、平实、连贯，每段约80至150个汉字，每个章节不超过3段。
8. 不要解释数据窗口、计算过程或提示词。新闻标题可能带有夸张和评论，不得把标题中的“大势已去”“稳赢”等判断当成既定事实，只能中性概括事件。
9. 总体态势应直接从选情结果写起，不要写“本报告基于给定资料”之类的说明。
10. sections 分工必须严格：overall 写总体选情；sentiment 写近7天新闻舆情与候选人情感；ntp 只写新北市；yilan 只写宜兰县；hot_events 写本地热点议题和热点事件；platforms 只写 PTT、YouTube 和 Google Trends 的讨论、传播与搜索关注；statements 只写候选人言论及同题前后变化；external 只写“新文易数”提供的外部事件、热词和议题关系，不得混入本地热点事件。
11. PTT、YouTube、热搜、新闻数量均不是支持率；前后言论变化也不等于候选人说谎。只能按资料中的语气、数量和关系标签作中性说明。

只输出 JSON：
{"sections":{"overall":["..."],"sentiment":["..."],"ntp":["..."],"yilan":["..."],"hot_events":["..."],"platforms":["..."],"statements":["..."],"external":["..."]}}"""


def _facts(payload: dict[str, Any]) -> dict[str, Any]:
    from .formal_report import (
        _candidate_sentiment_rows,
        _close_regions,
        _event_sources,
        _focus_regions,
        _hot_issues,
        _issue_categories,
        _observe_report_data,
        _overall_tone,
        _party_effect_text,
        _party_expectation,
        _platform_report_data,
        _pct,
        _poll_pct,
        _pp,
        _region_bundle,
        _s,
        _statement_report_data,
    )
    from .hotissues import extract  # noqa: F401 - keeps report data path shared

    regions = _region_bundle(payload)
    local = [row for row in regions if row["code"] != "TW"]
    close = _close_regions(local, limit=10)
    focus = [row for row in _focus_regions(local) if row]
    hot_issues = _hot_issues(payload)
    hot_issues = sorted(
        hot_issues,
        key=lambda issue: (-float(issue.get("impact") or 0), -float(issue.get("rank_score") or 0)),
    )
    events = sorted(
        payload.get("hot_events") or [],
        key=lambda event: (-float(event.get("impact") or 0), -int(event.get("size") or 1)),
    )
    observe = _observe_report_data(payload)
    platforms = _platform_report_data(payload)
    statements = _statement_report_data(payload)
    overall_score, overall_tone = _overall_tone(payload.get("trend") or [])
    opinion_days = int(payload.get("opinion_days") or 7)

    focus_facts = []
    for region in focus:
        focus_facts.append({
            "县市": _s(region["name"]),
            "候选人": [
                {
                    "姓名": _s(candidate.get("name")),
                    "政党": _s(candidate.get("party") or "未标注"),
                    "市场概率": _pct(candidate.get("market")),
                    "民调支持度": _poll_pct(candidate.get("poll")),
                    "融合概率": _pct(candidate.get("fused")),
                    "近7天新闻情感": (
                        f"{float(candidate['sentiment']):+.2f}"
                        if candidate.get("sentiment") is not None else "—"
                    ),
                }
                for candidate in (region.get("candidates") or [])[:4]
            ],
            "领先差距": _pp(
                float((region["candidates"][0] or {}).get("fused") or 0)
                - float((region["candidates"][1] or {}).get("fused") or 0)
            ) if len(region.get("candidates") or []) > 1 else "—",
        })

    return {
        "报告日期": payload["generated_at"].strftime("%Y年%m月%d日"),
        "新闻总量": int(payload["stats"].get("articles") or 0),
        "总体舆情": {
            "方向": overall_tone,
            "情感分": f"{overall_score:+.2f}" if overall_score is not None else "—",
        },
        "主要政党期望席次": [
            {"政党": _s(party), "期望席次": round(seats, 2), "领先县市": leads}
            for party, seats, leads in _party_expectation(local)[:5]
        ],
        "领先差距最小县市": [
            {
                "县市": _s(row["name"]),
                "领先者": _s(row["leader"]),
                "次位": _s(row["runner"]),
                "差距": _pp(row["margin"]),
                "领先者融合概率": _pct(row["leader_prob"]),
            }
            for row in close
        ],
        "重点选区": focus_facts,
        "候选人舆情": _candidate_sentiment_rows(local, payload.get("opinion_news")),
        "热点议题": [
            {
                "事件": _s(issue.get("title")),
                "议题类别": _issue_categories(issue),
                "新闻影响": f"{int(round(float(issue.get('impact') or 0) * 100))}%",
                "中间选民判断": issue.get("swing_label") or "—",
                "党派得失": _party_effect_text(issue),
            }
            for issue in hot_issues[:8]
        ],
        "热点事件": [
            {
                "日期": event.get("date"),
                "事件": _s(event.get("title")),
                "新闻影响": f"{int(round(float(event.get('impact') or 0) * 100))}%",
                "媒体覆盖": f"{int(event.get('size') or 1)}家",
                "媒体来源": _event_sources(event),
            }
            for event in events[:8]
        ],
        "新文易数": {
            "概况": observe["summary"],
            "事件时间线": observe["event_rows"][:5],
            "热词": observe["trend_rows"][:8],
            "议题关系": observe["relation_text"],
        },
        "网络平台舆情": {
            "概况": platforms["summary"],
            "PTT候选人讨论": [
                dict(zip(
                    ["候选人", "县市／政党", "讨论帖", "语气分", "方向", "争议帖"],
                    row,
                ))
                for row in platforms["ptt_rows"][:6]
            ],
            "YouTube候选人视频": [
                dict(zip(
                    ["候选人", "政党", "视频数", "累计播放", "方向", "热度分"],
                    row,
                ))
                for row in platforms["youtube_rows"][:6]
            ],
            "GoogleTrends热搜": [
                dict(zip(
                    ["热搜词", "近似热度", "类别", "关联候选人", "关联新闻"],
                    row,
                ))
                for row in platforms["trend_rows"][:8]
            ],
        },
        "候选人言论追踪": {
            "概况": statements["summary"],
            "近期代表性言论": [
                dict(zip(
                    ["日期", "候选人", "议题", "言论摘要", "来源类型"],
                    row,
                ))
                for row in statements["statement_rows"][:6]
            ],
            "同题表述变化": [
                dict(zip(
                    ["候选人", "议题", "变化类型", "较早表述", "较新表述"],
                    row,
                ))
                for row in statements["change_rows"][:5]
            ],
        },
    }


def _allowed_numbers(fact_text: str) -> set[float]:
    return {float(value) for value in _NUMERIC.findall(fact_text)}


def _split_paragraph(text: str, limit: int = 260) -> list[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    sentences = [part.strip() for part in re.split(r"(?<=[。！？；])", text) if part.strip()]
    paragraphs: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > limit:
            paragraphs.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        paragraphs.append(current)
    return paragraphs


def _validate(
    payload: Any,
    fact_text: str,
    keys: tuple[str, ...],
) -> tuple[dict[str, list[str]] | None, str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("sections"), dict):
        return None, "模型未返回 sections 结构"
    sections_raw = payload["sections"]
    sections: dict[str, list[str]] = {}
    for key in keys:
        value = sections_raw.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not value:
            return None, f"缺少正文段落：{key}"
        paragraphs = []
        for item in value:
            text = str(item or "").strip()
            if not text:
                continue
            banned = next((word for word in _BANNED if word in text), "")
            if banned:
                return None, f"{key} 出现禁用表述：{banned}"
            paragraphs.extend(_split_paragraph(text))
        if not paragraphs:
            return None, f"{key} 没有有效正文"
        sections[key] = paragraphs[:3]

    output_text = "\n".join(text for paragraphs in sections.values() for text in paragraphs)
    if "external" in sections and "新文易数" not in "\n".join(sections["external"]):
        return None, "external 未以新文易数数据为正文主题"
    allowed = _allowed_numbers(fact_text)
    invented = sorted({float(value) for value in _NUMERIC.findall(output_text)} - allowed)
    if invented:
        return None, "模型新增了事实包之外的数字：" + "、".join(f"{value:g}" for value in invented[:8])
    return sections, ""


def generate(payload: dict[str, Any], settings: Settings) -> tuple[dict[str, list[str]] | None, dict[str, Any]]:
    meta: dict[str, Any] = {
        "engine": "template",
        "model": settings.llm_model,
        "used": False,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if not settings.llm_enabled or not settings.llm_api_key:
        meta["reason"] = "LLM 未启用或未配置密钥"
        return None, meta
    try:
        facts = _facts(payload)
        fact_text = json.dumps(facts, ensure_ascii=False, separators=(",", ":"))
        client = LLMClient(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            min(settings.llm_timeout, 120),
        )
        batches = (
            ("overall", "sentiment", "ntp"),
            ("yilan", "hot_events", "platforms"),
            ("statements", "external"),
        )
        sections: dict[str, list[str]] = {}
        errors: list[str] = []
        for keys in batches:
            result = client.complete_json(
                SYSTEM_PROMPT,
                "本轮只输出以下 sections："
                + "、".join(keys)
                + "。不得省略指定章节，也不要输出其他章节。\n"
                + "请根据以下资料撰写报告正文：\n"
                + fact_text,
                temperature=0.2,
                max_tokens=2600,
                json_mode=True,
                thinking="disabled",
            )
            part, error = _validate(result, fact_text, keys)
            if part:
                sections.update(part)
            else:
                errors.append(f"{'/'.join(keys)}：{error}")
        if not sections:
            meta["reason"] = "；".join(errors)
            return None, meta
        missing = [key for key in SECTION_KEYS if key not in sections]
        meta.update({
            "engine": "llm",
            "used": True,
            "reason": (
                "已通过事实与数字校验"
                if not errors else
                "部分章节回退模板；" + "；".join(errors)
            ),
            "generated_sections": list(sections),
            "fallback_sections": missing,
        })
        return sections, meta
    except (LLMError, OSError, RuntimeError, ValueError, TypeError) as exc:
        meta["reason"] = f"{type(exc).__name__}: {exc}"
        return None, meta
