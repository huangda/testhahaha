"""規則式中文情感 / 議題分析（在沒有 LLM 金鑰時的降級引擎）。

設計目標：可解釋、可調參、完全離線。輸出的分數與 LLM 引擎同構，
因此上層聚合與預測邏輯不需區分引擎。
"""

from __future__ import annotations

import re
from typing import Any

# 正向詞：對「選情態勢」偏有利的表述
POSITIVE = [
    "領先", "领先", "看好", "支持度上升", "民調上升", "民调上升", "聲勢", "声势",
    "大勝", "大胜", "穩住", "稳住", "穩固", "稳固", "整合成功", "團結", "团结",
    "勝選", "胜选", "氣勢", "气势", "高民調", "高民调", "壓倒性", "压倒性",
    "聲望", "声望", "人氣", "人气", "力挺", "背書", "背书", "站台", "輔選成功",
    "獲提名", "获提名", "徵召", "征召", "登記參選", "登记参选", "表態參選", "表态参选",
    "勝出", "胜出", "出線", "出线", "擊敗", "击败", "擴大優勢", "扩大优势",
    "支持度最高", "滿意度上升", "满意度上升", "好評", "好评", "認同", "认同",
]

# 負向詞：對候選人 / 選情不利
NEGATIVE = [
    "落後", "落后", "崩盤", "崩盘", "民調下滑", "民调下滑", "支持度下滑", "聲勢下滑",
    "醜聞", "丑闻", "爭議", "争议", "內鬨", "内讧", "內訌", "分裂", "退選", "退选",
    "起訴", "起诉", "貪汙", "贪污", "涉貪", "涉贪", "收賄", "收贿", "弊案", "圖利",
    "抗議", "抗议", "質疑", "质疑", "炮轟", "炮轰", "抨擊", "抨击", "罵", "骂",
    "落敗", "落败", "敗選", "败选", "失守", "翻盤", "翻盘", "告急", "危急",
    "棄保", "弃保", "配票失敗", "配票失败", "民調落後", "民调落后", "重挫", "挫敗",
    "挫败", "失望", "不滿", "不满", "反彈", "反弹", "連署失敗", "联署失败",
    "司法調查", "司法调查", "搜索", "約談", "约谈", "羈押", "羁押", "交保", "判刑",
    "違法", "违法", "造假", "舞弊", "賄選", "贿选", "抹黑", "負面聲量", "负面声量",
]

NEGATIONS = ["不", "未", "沒", "没", "無", "无", "非", "否認", "否认", "澄清"]

TOPIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("提名徵召", ("提名", "徵召", "征召", "初選", "初选", "出線", "出线", "黨內初選")),
    ("民調選情", ("民調", "民调", "支持度", "看好度", "選情", "选情", "聲勢", "声势")),
    ("競選造勢", ("造勢", "造势", "競選", "竞选", "掃街", "扫街", "拜票", "站台", "晚會", "晚会")),
    ("政黨整合", ("藍白", "蓝白", "政黨協商", "政党协商", "整合", "合作", "結盟", "结盟", "分裂")),
    ("司法廉能", ("起訴", "起诉", "貪汙", "贪污", "弊案", "司法", "羈押", "羁押", "賄選", "贿选", "約談", "约谈")),
    ("兩岸國安", ("兩岸", "两岸", "中共", "中國", "中国", "統獨", "统独", "國安", "国安", "國防", "国防", "軍演", "军演")),
    ("政見政策", ("政見", "政见", "政策", "福利", "建設", "建设", "交通", "社宅", "托育", "長照", "长照")),
    ("罷免風波", ("罷免", "罢免", "公投", "連署", "联署")),
    ("輔選站台", ("輔選", "辅选", "站台", "大咖", "總統", "总统", "院長", "院长", "合體", "合体")),
    ("選務爭議", ("選務", "选务", "投票所", "計票", "计票", "棄保", "弃保", "配票")),
]

_SENTENCE_SPLIT = re.compile(r"[。！？!?；;\n]+")

# 針對台灣選舉報導常見句式的樣式規則：(正則, 極性)
# 「A 領先 B 4.6 個百分點」這類句型用詞典無法正確判讀，需以樣式處理。
PATTERN_RULES: list[tuple[str, float]] = [
    (r"(領先|超前|勝過|贏過|勝出)[^。，,；;]{0,10}\d+(\.\d+)?\s*(個)?百分點", 1.0),
    (r"(落後|輸給)[^。，,；;]{0,10}\d+(\.\d+)?\s*(個)?百分點", -1.0),
    (r"看好度[^。，,；;]{0,8}(破|逾|超過|達)\s*[四五五六七八九]成", 1.0),
    (r"看好度[^。，,；;]{0,8}(僅|只有|不到|低於)\s*[一二三四]成", -1.0),
    (r"支持度[^。，,；;]{0,6}(上升|增加|成長|回升)", 1.0),
    (r"支持度[^。，,；;]{0,6}(下降|下滑|減少|大跌)", -1.0),
    (r"(正式|確定|宣布)[^。，,；;]{0,4}(參選|投入選戰|爭取提名|角逐)", 0.5),
    (r"(宣布退選|退出選舉|棄選|不選了)", -1.0),
    (r"(民調第一|支持度第一|居冠|奪冠|聲量第一)", 1.0),
    (r"(民調墊底|敬陪末座|支持度最低|聲量最低)", -1.0),
    (r"(差距|民調)[^。，,；;]{0,6}(縮小|拉近|追平)", 0.4),
    (r"(差距)[^。，,；;]{0,6}(擴大|拉開)", 0.4),
]
_PATTERNS = [(re.compile(p), w) for p, w in PATTERN_RULES]


def _has_negation(text: str, index: int, window: int = 4) -> bool:
    start = max(0, index - window)
    return any(neg in text[start:index] for neg in NEGATIONS)


def pattern_signals(text: str) -> tuple[float, int]:
    """返回选举句式规则的总分和命中次数。"""
    total = 0.0
    hits = 0
    for pattern, weight in _PATTERNS:
        for _ in pattern.finditer(text):
            total += weight
            hits += 1
    return total, hits


def score_text(text: str) -> tuple[float, int, int]:
    """回傳 (情感分數 -1..1, 正向命中數, 負向命中數)。"""
    pos = neg = 0
    for word in POSITIVE:
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            if _has_negation(text, idx):
                neg += 1
            else:
                pos += 1
            start = idx + len(word)
    for word in NEGATIVE:
        start = 0
        while True:
            idx = text.find(word, start)
            if idx < 0:
                break
            if _has_negation(text, idx):
                pos += 1
            else:
                neg += 1
            start = idx + len(word)

    pattern_total, pattern_hits = pattern_signals(text)

    total = pos + neg + pattern_hits
    if total == 0:
        return 0.0, 0, 0
    # 分母加權讓少量命中不會直接給出極端值
    score = (pos - neg + pattern_total) / max(total, 2)
    return max(-1.0, min(1.0, score)), pos, neg


def candidate_sentiments(text: str, candidates: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """針對每位候選人，取「名字附近句子」的情感作為其專屬分數。

    candidates: [(english_name, chinese_name), ...]
    """
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    results: list[dict[str, Any]] = []
    for english, chinese in candidates:
        needles = [n for n in (chinese, english) if n]
        mentions = 0
        scores: list[float] = []
        for sentence in sentences:
            hit = sum(1 for n in needles if n and n in sentence)
            if not hit:
                continue
            mentions += hit
            s, _, _ = score_text(sentence)
            if s != 0:
                scores.append(s)
        if not mentions:
            continue
        sentiment = sum(scores) / len(scores) if scores else 0.0
        results.append(
            {
                "name": english,
                "name_zh": chinese,
                "mentions": mentions,
                "sentiment": round(sentiment, 3),
            }
        )
    return results


def topics(text: str, limit: int = 5) -> list[dict[str, Any]]:
    scored: list[tuple[int, str]] = []
    for topic, keys in TOPIC_RULES:
        score = sum(text.count(k) for k in keys)
        if score:
            scored.append((score, topic))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{"topic": t, "hits": s} for s, t in scored[:limit]]


def impact(text: str, title: str, sentiment: float) -> float:
    """估計新聞對選情的影響力強度 0..1。"""
    weight_words = [
        "獨家", "独家", "震撼", "爆料", "重大", "緊急", "紧急", "確定", "确定",
        "宣布", "正式", "民調", "民调", "翻盤", "翻盘", "醜聞", "丑闻",
    ]
    base = 0.35
    base += 0.15 * sum(1 for w in weight_words if w in text)
    base += min(0.25, len(title) / 200)
    base += 0.2 * abs(sentiment)
    return round(max(0.05, min(1.0, base)), 3)


def summary_snippet(text: str, limit: int = 90) -> str:
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if not sentences:
        return text[:limit]
    best = max(sentences[:6], key=lambda s: abs(score_text(s)[0]) + min(len(s), 60) / 120)
    return best[:limit]


def analyze_item(
    title: str,
    summary: str,
    candidates: list[tuple[str, str]],
) -> dict[str, Any]:
    text = f"{title}。{summary}"
    sentiment, pos, neg = score_text(text)
    return {
        "sentiment": round(sentiment, 3),
        "impact": impact(text, title, sentiment),
        "candidates": candidate_sentiments(text, candidates),
        "topics": [t["topic"] for t in topics(text)],
        "summary": summary_snippet(summary or title),
        "_debug": {"positive_hits": pos, "negative_hits": neg},
    }
