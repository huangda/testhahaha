"""轻量新闻热词提取，用于舆情词云。"""

from __future__ import annotations

import re
import math
from collections import Counter, defaultdict
from typing import Any, Iterable

from .config import Settings
from .lexicon import TOPIC_RULES
from .zhconv import to_simplified

_HAN_RUN = re.compile(r"[\u4e00-\u9fff]{2,40}")
_PARTY_TERMS = ("中国国民党", "民主进步党", "台湾民众党", "国民党", "民进党", "民众党", "无党籍")
_SEED_TERMS = (
    "民调", "支持度", "领先", "落后", "差距", "胶着", "黄金交叉",
    "竞选", "造势", "扫街", "拜票", "辩论", "政见", "政策", "提名",
    "征召", "整合", "蓝白", "罢免", "投票率",
    "司法", "搜索", "约谈", "交保", "起诉", "贿选", "争议", "丑闻",
    "台风", "停班停课", "两岸", "中国", "美国", "国安", "防务",
    "长照", "健保", "社宅", "交通", "经济", "就业", "青年",
    "洋流", "沉默螺旋", "中间选民", "政党轮替",
)
_MEANINGS = {
    "中国国民党": "台湾主要在野党之一，通常归入蓝色阵营。",
    "国民党": "中国国民党的简称，通常归入蓝色阵营。",
    "民主进步党": "台湾执政党，通常归入绿色阵营。",
    "民进党": "民主进步党的简称，通常归入绿色阵营。",
    "台湾民众党": "台湾第三势力政党，新闻中常简称民众党。",
    "民众党": "台湾民众党的简称。",
    "无党籍": "没有加入政党、以无党身份参选的候选人。",
    "民调": "民意调查的简称，通过抽样访问估计选民支持度；词云统计的是相关新闻热度，不是民调本身。",
    "支持度": "受访者表示支持某位候选人或政党的比例。",
    "领先": "候选中民调、赔率或声量暂时高于其他竞争者。",
    "落后": "候选中民调、赔率或声量暂时低于其他竞争者。",
    "差距": "两名主要竞争者之间支持度或市场概率的差额。",
    "胶着": "主要候选人的支持度或市场概率非常接近，胜负仍不明朗。",
    "黄金交叉": "民调语境中指两名候选人的支持度曲线发生交叉，原先落后者转为领先。",
    "投票率": "具有投票资格者实际投票的比例。",
    "竞选": "候选人投入选举、争取选民支持的活动。",
    "造势": "通过集会、晚会或动员活动凝聚支持者声势。",
    "扫街": "候选人沿街拜访、接触选民的传统竞选活动。",
    "拜票": "向选民请托支持、争取选票。",
    "辩论": "候选人就政策与选情公开交锋、陈述立场的活动。",
    "政见": "候选人提出的政策主张与当选后的施政承诺。",
    "政策": "政府或候选人针对公共议题提出的处理方案。",
    "提名": "政党正式决定由某人代表参选。",
    "征召": "政党不经初选，直接邀请特定人选代表参选。",
    "整合": "政党或阵营协调人选、资源与支持者，避免分裂。",
    "蓝白": "中国国民党与台湾民众党之间的合作、协商或竞争关系。",
    "罢免": "依法发起程序，要求选民投票决定是否提前解除民选公职。",
    "司法": "与检调、法院、起诉、羁押等法律程序有关的新闻。",
    "搜索": "司法语境中的搜查行动（如检调搜索），并非网络搜索。",
    "约谈": "检调或司法机关通知相关人员到场说明。",
    "交保": "涉案人缴纳保证金后暂时解除羁押或限制。",
    "起诉": "检察机关正式对被告提起刑事诉讼。",
    "贿选": "以金钱、利益或其他不正当方式影响投票行为。",
    "争议": "新闻中出现质疑、反对或不同立场交锋的事件。",
    "丑闻": "涉及违法、失德或严重损害公共信任的事件。",
    "台风": "影响台湾天气、活动安排与地方治理的自然灾害议题。",
    "停班停课": "地方政府因台风等灾害宣布停止上班上课。",
    "两岸": "台湾与中国大陆之间的政治、经济与社会关系。",
    "中国": "新闻语境中通常指中国大陆，也可能出现在两岸关系议题中。",
    "美国": "与美国政策、外交或台海关系有关的新闻语境。",
    "国安": "国家安全、防务及两岸安全政策相关议题。",
    "防务": "军事、国防预算与安全政策相关议题。",
    "长照": "长期照顾，涉及老人及失能者的照护政策。",
    "健保": "台湾全民健康保险制度及相关政策。",
    "社宅": "社会住宅，政府提供或补贴的居住政策。",
    "交通": "道路、轨道、公共运输与地方交通建设议题。",
    "经济": "就业、产业、投资与民生经济议题。",
    "就业": "工作机会、劳动市场与青年就业议题。",
    "青年": "年轻选民、青年政策与青年参政相关议题。",
    "洋流": "近期新闻中的网络化政治称呼，主要围绕沈伯洋相关的支持者与舆论动员；具体指代以关联报道为准。",
    "沉默螺旋": "一种传播学概念：人们若认为自己的意见属于少数，可能更不愿意公开表达。",
    "中间选民": "尚未固定支持某一政党、可能左右选举结果的选民。",
    "政党轮替": "不同政党通过选举取得或失去执政地位。",
    "提名征召": "政党提名或直接征召候选人参加选举的相关流程。",
    "民调选情": "围绕支持度、看好度、差距与选情变化的议题。",
    "竞选造势": "集会、扫街、拜票等争取选民支持的竞选活动。",
    "政党整合": "政党之间的协商、合作、结盟或分裂。",
    "司法廉能": "涉及检调搜索、起诉、羁押、交保、贿选等法治与廉政议题。",
    "两岸国安": "两岸关系、国家安全、防务与军事议题。",
    "政见政策": "候选人的政策主张、福利与地方建设承诺。",
    "罢免风波": "罢免、公投或联署程序引发的政治争议。",
    "辅选站台": "政治人物为候选人站台、助讲或动员支持者。",
    "选务争议": "投票、计票、弃保或配票等选举程序争议。",
}
_STOP = {
    "记者", "报导", "报道", "新闻", "今天", "昨天", "明天", "目前", "已经",
    "表示", "指出", "认为", "强调", "说明", "进行", "相关", "这个", "那个",
    "我们", "他们", "可以", "没有", "不是", "因为", "所以", "但是", "以及",
    "同时", "对于", "如何", "什么", "一个", "两个", "多位", "外界", "民众",
    "民众党", "国民党", "民进党", "中央社", "自由时报", "中国时报", "联合报",
    "政治", "地方", "选举", "大选", "九合一",
}
_BAD_EDGE = ("的", "了", "是", "在", "将", "会", "与", "和", "及", "对", "把", "被", "为")


def _valid(term: str) -> bool:
    if len(term) < 2 or term in _STOP:
        return False
    if any(term.startswith(x) or term.endswith(x) for x in _BAD_EDGE):
        return False
    if any(term == x or term in x for x in _STOP if len(x) >= 3):
        return False
    if re.search(r"\d", term):
        return False
    return True


def _doc_terms(text: str) -> set[str]:
    terms: set[str] = set()
    for run in _HAN_RUN.findall(text):
        for size in range(2, min(6, len(run)) + 1):
            for index in range(0, len(run) - size + 1):
                term = run[index:index + size]
                if _valid(term):
                    terms.add(term)
    return terms


def extract(
    rows: Iterable[dict[str, Any]],
    settings: Settings,
    *,
    baseline_rows: Iterable[dict[str, Any]] = (),
    limit: int = 40,
) -> list[dict[str, Any]]:
    rows = list(rows)
    baseline_rows = list(baseline_rows)
    doc_freq: Counter[str] = Counter()
    weighted_freq: Counter[str] = Counter()
    sentiment_num: dict[str, float] = defaultdict(float)
    sentiment_weight: dict[str, float] = defaultdict(float)
    impact_sum: dict[str, float] = defaultdict(float)
    evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    evidence_titles: dict[str, set[str]] = defaultdict(set)

    candidate_terms = {to_simplified(zh) for zh in settings.name_map.values() if zh}
    known = set(candidate_terms)
    known.update(_PARTY_TERMS)
    known.update(_SEED_TERMS)
    known.update(topic for topic, _keys in TOPIC_RULES)

    def terms_for(row: dict[str, Any]) -> set[str]:
        title = to_simplified(row.get("title") or "")
        summary = to_simplified(row.get("summary") or "")
        text = f"{title}\n{summary}"
        terms = set()
        terms.update(term for term in known if term in text)
        for item in row.get("topics") or []:
            topic = item.get("topic") if isinstance(item, dict) else str(item)
            if topic and _valid(to_simplified(topic)):
                terms.add(to_simplified(topic))
        return terms

    for row in rows:
        impact = float(row.get("impact") or 0.3)
        recency_weight = 0.5 + impact
        terms = terms_for(row)
        title = to_simplified(row.get("title") or "").strip()
        summary = to_simplified(row.get("ai_summary") or row.get("summary") or "").strip()
        title_key = title[:100]
        for term in terms:
            doc_freq[term] += 1
            weighted_freq[term] += recency_weight
            sentiment_num[term] += float(row.get("sentiment") or 0) * recency_weight
            sentiment_weight[term] += recency_weight
            impact_sum[term] += impact
            if (
                title
                and len(evidence[term]) < 4
                and title_key not in evidence_titles[term]
            ):
                evidence_titles[term].add(title_key)
                url = str(row.get("url") or "").strip()
                if not re.match(r"^https?://", url, re.IGNORECASE):
                    url = ""
                evidence[term].append({
                    "title": title[:180],
                    "summary": summary[:220],
                    "source": to_simplified(row.get("source_name") or ""),
                    "published_at": row.get("published_at") or row.get("first_seen") or "",
                    "url": url,
                    "impact": round(impact, 3),
                })

    baseline_freq: Counter[str] = Counter()
    for row in baseline_rows:
        baseline_freq.update(terms_for(row))

    current_total = len(rows)
    baseline_total = len(baseline_rows)
    max_weight = max(weighted_freq.values(), default=1.0) or 1.0

    def heat_metrics(term: str, count: int, weight: float) -> tuple[float, float]:
        current_rate = (count + 0.5) / (current_total + 8)
        if baseline_total:
            baseline_rate = (baseline_freq[term] + 0.5) / (baseline_total + 8)
            change_ratio = current_rate / baseline_rate
            rise_signal = min(
                1.0,
                math.log1p(max(0.0, change_ratio - 1.0)) / math.log(8.0),
            )
        else:
            change_ratio = 1.0
            rise_signal = 0.0
        absolute_signal = min(1.0, weight / max_weight)
        heat = 0.7 * rise_signal + 0.3 * absolute_signal
        return heat, change_ratio

    result = []
    for term, count in doc_freq.items():
        if count < 2:
            continue
        if term not in known and any(
            term in protected or protected in term
            for protected in known if protected and protected != term
        ):
            continue
        length_bonus = min(1.35, 0.9 + len(term) * 0.08)
        weight = weighted_freq[term] * length_bonus
        heat, change_ratio = heat_metrics(term, count, weight)
        meaning = _MEANINGS.get(term)
        if not meaning and term in candidate_terms:
            meaning = "候选人姓名，用于统计其在本时段新闻中被提及的次数、情感倾向与影响强度。"
        if not meaning:
            meaning = "系统从新闻标题与议题分析中识别的选举关键词；具体含义请以下列原始报道为准。"
        result.append({
            "word": term,
            "count": count,
            "baseline_count": baseline_freq[term],
            "change_ratio": round(change_ratio, 2),
            "heat": round(heat, 4),
            "weight": round(weight, 3),
            "sentiment": round(
                sentiment_num[term] / (sentiment_weight[term] or 1.0), 3
            ),
            "impact": round(impact_sum[term] / count, 3),
            "meaning": meaning,
            "evidence": evidence[term],
        })
    result.sort(key=lambda item: (-len(item["word"]), -item["weight"], item["word"]))
    kept: list[dict[str, Any]] = []
    for item in result:
        term = item["word"]
        covered = any(
            term != other["word"]
            and term in other["word"]
            and other["count"] >= item["count"] * 0.9
            for other in result
        )
        if not covered:
            kept.append(item)
    kept.sort(key=lambda item: (
        -item["heat"], -item["weight"], -item["count"], item["word"]
    ))
    return kept[:limit]
