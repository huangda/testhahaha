"""候選人言論追蹤：抽取原文、建立時間線並比較同題前後表述。"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import db, newstext, statement_sources
from .dedupe import bigrams, jaccard, normalize_title
from .lexicon import topics as lexicon_topics
from .llm import LLMClient
from .ptt_opinion import _candidate_registry, _match_candidates, _valid_candidate_name
from .paths import DATA_DIR
from .zhconv import to_simplified

DIRECT_SPEECH_VERBS = (
    "表示", "指出", "強調", "强调", "主張", "主张", "承諾", "承诺",
    "保證", "保证", "認為", "认为", "呼籲", "呼吁", "批評", "批评",
    "說", "说", "講", "讲", "宣布", "提出", "要求", "回應", "回应",
    "答覆", "答复", "說明", "说明", "宣稱", "宣称",
)
SOFT_SPEECH_VERBS = (
    "提及", "提到", "談到", "谈到", "談及", "谈及",
    "據報道", "据报道", "消息稱", "消息称",
)
STATEMENT_VERBS = tuple(dict.fromkeys((*DIRECT_SPEECH_VERBS, *SOFT_SPEECH_VERBS)))
SOFT_SPEECH_VERB_SET = set(SOFT_SPEECH_VERBS)
SUPPORT_TERMS = (
    "支持", "贊成", "赞成", "推動", "推动", "主張", "主张", "承諾", "承诺",
    "保證", "保证", "同意", "優先", "优先", "擴大", "扩大", "補助", "补助",
    "興建", "兴建", "會做", "会做", "將推動", "将推动", "應該做", "应该做",
)
OPPOSE_TERMS = (
    "反對", "反对", "不支持", "拒絕", "拒绝", "取消", "停止", "不會", "不会",
    "不應", "不应", "刪減", "删减", "暫緩", "暂缓", "不宜", "沒有必要", "没有必要",
)
CONDITION_RE = re.compile(
    r"[^。！？!?]{0,35}(?:如果|若|只要|除非|前提|在.{0,18}(?:情況下|情况下)|一旦)[^。！？!?]{0,70}"
)
QUOTE_RE = re.compile(r"[「『“](?P<quote>[^」』”]{6,240})[」』”]")
SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")
EXTRACT_CACHE_DIR = DATA_DIR / "cache" / "statement_extractions"
EXTRACT_CACHE_VERSION = "v5"
TOPIC_RULES = (
    ("住房与土地", ("房價", "房价", "住宅", "社宅", "居住", "租屋", "土地", "都更", "房市")),
    ("交通建设", ("交通", "捷運", "捷运", "道路", "鐵路", "铁路", "公車", "公车", "停車", "停车", "機場", "机场")),
    ("经济就业", ("經濟", "经济", "就業", "就业", "薪資", "薪资", "產業", "产业", "投資", "投资", "中小企業", "中小企业")),
    ("社会福利", ("福利", "托育", "長照", "长照", "老人", "幼兒", "幼儿", "補助", "补助", "健保", "低收入")),
    ("教育青年", ("教育", "學生", "学生", "青年", "學校", "学校", "老師", "老师", "大學", "大学")),
    ("能源环境", ("能源", "電力", "电力", "核電", "核电", "再生能源", "減碳", "减碳", "環境", "环境", "污染")),
    ("两岸关系", ("兩岸", "两岸", "中國", "中国", "中共", "統獨", "统独", "國安", "国安", "國防", "国防")),
    ("治安司法", ("治安", "司法", "檢調", "检调", "警察", "犯罪", "詐騙", "诈骗", "廉政", "貪瀆", "贪渎")),
    ("财政预算", ("財政", "财政", "預算", "预算", "稅", "税", "負債", "负债", "財源", "财源")),
    ("地方治理", ("市政", "縣政", "县政", "治理", "行政效率", "公務員", "公务员", "地方建設", "地方建设")),
)
RELATION_LABELS = {
    "consistent": "前后一致",
    "clarification": "补充或澄清",
    "condition_change": "条件变化",
    "policy_change": "政策调整",
    "suspected_inconsistency": "疑似前后不一致",
    "insufficient": "证据不足",
}
RELATION_SEVERITY = {
    "suspected_inconsistency": 4,
    "policy_change": 3,
    "condition_change": 2,
    "clarification": 1,
    "consistent": 0,
    "insufficient": 0,
}


def _analysis_candidates(row: dict[str, Any]) -> list[str]:
    value = row.get("candidates")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    names = []
    for item in value or []:
        if isinstance(item, dict):
            name = item.get("name_zh") or item.get("name")
        else:
            name = str(item)
        if name:
            names.append(to_simplified(str(name)))
    return names


def _topic(text: str) -> str:
    for topic, words in TOPIC_RULES:
        if any(word in text for word in words):
            return topic
    extracted = lexicon_topics(text, limit=1)
    if extracted:
        mapped = {
            "民調選情": "选举与民调",
            "競選造勢": "选举与民调",
            "政見政策": "其他政策",
            "提名徵召": "选举与民调",
            "政黨整合": "政党合作",
            "司法廉能": "治安司法",
            "兩岸國安": "两岸关系",
            "輔選站台": "选举与民调",
            "選務爭議": "选举与民调",
        }
        return mapped.get(extracted[0]["topic"], extracted[0]["topic"])
    return "其他政策"


def _polarity(text: str) -> tuple[int, str]:
    if any(term in text for term in OPPOSE_TERMS):
        return -1, "opposes"
    if any(term in text for term in SUPPORT_TERMS):
        return 1, "supports"
    return 0, "neutral"


def _condition(text: str) -> str:
    match = CONDITION_RE.search(text)
    return match.group(0).strip()[:120] if match else ""


def _clean_sentence(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    value = re.sub(r"^(?:記者|中央社記者|本報記者)[^。]{0,30}[。，,]\s*", "", value)
    return value[:300]


def _statement_id(news_id: str, candidate: str, text: str) -> str:
    raw = f"{news_id}|{candidate}|{normalize_title(text)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _substantive_claim(text: str) -> bool:
    text = _clean_sentence(text)
    if len(re.sub(r"\W", "", text)) < 10:
        return False
    return bool(
        _topic(text) != "其他政策"
        or any(term in text for term in (*SUPPORT_TERMS, *OPPOSE_TERMS))
        or re.search(
            r"(?:會|将|將|推動|推动|改善|提高|降低|增加|減少|减少|建設|建设|"
            r"規劃|规划|檢討|检讨|開放|开放|投入|保障|制定|修正|要求|主張|主张)",
            text,
        )
    )


def _aliases_for_candidate(
    candidate: dict[str, Any],
    aliases: dict[str, list[dict[str, Any]]],
) -> list[str]:
    key = (candidate.get("region"), candidate.get("name"), candidate.get("office"))
    result = []
    for alias, items in aliases.items():
        if any(
            (item.get("region"), item.get("name"), item.get("office")) == key
            for item in items
        ):
            result.append(alias)
    return sorted(result, key=len, reverse=True)


def _extract_from_article(
    row: dict[str, Any],
    body: str,
    aliases: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    text = "\n".join(part for part in (row.get("title") or "", body or "") if part)
    if not text:
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    candidates = _match_candidates(text, aliases)
    for candidate in candidates:
        candidate["_aliases"] = _aliases_for_candidate(candidate, aliases)

    def nearest_candidate_before(
        fragment: str,
        end: int,
        *,
        max_distance: int = 60,
    ) -> tuple[dict[str, Any] | None, int]:
        matches = _match_candidates(fragment[max(0, end - 140):end], aliases)
        nearest: list[tuple[int, dict[str, Any]]] = []
        window_start = max(0, end - 140)
        for candidate in matches:
            positions = [
                fragment.rfind(alias, window_start, end)
                for alias in _aliases_for_candidate(candidate, aliases)
            ]
            valid = [position for position in positions if position >= 0]
            if valid:
                position = max(valid)
                if end - position <= max_distance:
                    nearest.append((position, candidate))
        if not nearest:
            return None, -1
        position, candidate = max(nearest, key=lambda item: item[0])
        return candidate, position

    def append_statement(
        candidate: dict[str, Any],
        claim: str,
        context: str,
        *,
        direct: bool,
        confidence: float,
    ) -> None:
        claim = _clean_sentence(claim).strip("，,：: ")
        key = (candidate["name"], normalize_title(claim))
        if not claim or key in seen or not _substantive_claim(claim):
            return
        seen.add(key)
        polarity, stance = _polarity(claim)
        result.append({
            "id": _statement_id(
                str(row.get("id") or ""),
                candidate["name"],
                claim,
            ),
            "news_id": row.get("id") or "",
            "candidate_name": candidate["name"],
            "region_name": candidate.get("region") or "",
            "party": candidate.get("party") or "",
            "office": candidate.get("office") or "",
            "published_at": row.get("published_at") or row.get("first_seen") or "",
            "topic": _topic(claim),
            "claim": claim,
            "quote_text": _clean_sentence(context)[:500],
            "stance": stance,
            "polarity": polarity,
            "conditions": _condition(context),
            "directness": "direct" if direct else "paraphrase",
            "confidence": confidence,
            "source_name": row.get("source_name") or "",
            "source_title": row.get("title") or "",
            "source_url": row.get("url") or "",
            "extracted_at": db.utcnow_iso(),
        })

    # 直接引語：在整篇正文的引號上下文中，把最近的正式候選人視為說話者。
    for quote_match in QUOTE_RE.finditer(text):
        quote = quote_match.group("quote").strip()
        if not _substantive_claim(quote):
            continue
        before = text[max(0, quote_match.start() - 180):quote_match.start()]
        after = text[quote_match.end():quote_match.end() + 80]
        before_matches = _match_candidates(before, aliases)
        nearest = []
        for candidate in before_matches:
            positions = [
                before.rfind(alias)
                for alias in _aliases_for_candidate(candidate, aliases)
                if before.rfind(alias) >= 0
            ]
            if positions:
                position = max(positions)
                if len(before) - position <= 120:
                    nearest.append((position, candidate))
        if nearest:
            position, candidate = max(nearest, key=lambda item: item[0])
            speaker_part = before[position:]
            has_verb = any(verb in speaker_part for verb in STATEMENT_VERBS)
            has_direct_mark = bool(re.search(r"(?:表示|指出|強調|强调|說|说|認為|认为).{0,15}[：:]?$", speaker_part))
            if has_verb or has_direct_mark or len(speaker_part) <= 55:
                context = before[-130:] + quote + after[:40]
                append_statement(
                    candidate,
                    quote,
                    context,
                    direct=True,
                    confidence=0.9 if (has_verb or has_direct_mark) else 0.76,
                )
        else:
            after_matches = _match_candidates(after, aliases)
            if len(after_matches) == 1:
                append_statement(
                    after_matches[0],
                    quote,
                    before[-80:] + quote + after,
                    direct=True,
                    confidence=0.72,
                )

    # 候選人冒號句式：例如「蔣萬安：支持推動……」、「沈伯洋表示：……」。
    for candidate in candidates:
        for alias in candidate.get("_aliases") or []:
            if len(alias) < 3:
                continue
            pattern = re.compile(
                rf"{re.escape(alias)}\s*(?:[（(][^）)]{{0,24}}[）)])?\s*"
                rf"(?:(?:{'|'.join(map(re.escape, STATEMENT_VERBS))})\s*)?[：:]\s*"
                rf"(?P<claim>[^。！？!?\n；;]{{8,260}})",
                re.I,
            )
            for match in pattern.finditer(text):
                claim = match.group("claim")
                append_statement(
                    candidate,
                    claim,
                    text[max(0, match.start() - 40):match.end() + 30],
                    direct=True,
                    confidence=0.86,
                )

    # 發言動詞句式：將動詞歸給距離它最近、且位於動詞之前的正式候選人。
    verb_pattern = re.compile(
        "|".join(map(re.escape, sorted(STATEMENT_VERBS, key=len, reverse=True)))
    )
    for sentence_match in SENTENCE_RE.finditer(text):
        sentence = _clean_sentence(sentence_match.group(0))
        if len(sentence) < 10 or QUOTE_RE.search(sentence):
            continue
        matched = _match_candidates(sentence, aliases)
        if not matched:
            continue
        for verb_match in verb_pattern.finditer(sentence):
            verb = verb_match.group(0)
            verb_position = verb_match.start()
            owner, owner_position = nearest_candidate_before(
                sentence,
                verb_position,
                max_distance=24,
            )
            if not owner:
                continue
            if owner_position < 0:
                continue
            claim = sentence[verb_match.end():].strip("，,：: ")
            if len(claim) < 8:
                continue
            soft = verb in SOFT_SPEECH_VERB_SET
            append_statement(
                owner,
                claim,
                sentence,
                direct=False,
                confidence=0.48 if soft else 0.62,
            )
        if len(result) >= 24:
            return result
    return result


def _official_account_statements(
    *,
    days: int,
    candidates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """讀取官方頻道 RSS；標題與簡介不冒充完整講話。"""
    result = statement_sources.fetch_youtube_rss_statements(
        days=days,
        candidates=candidates,
    )
    return result.rows, result.warnings


def _extract_cached(
    row: dict[str, Any],
    body: str,
    aliases: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], str]:
    """按新闻编号与正文哈希缓存抽取结果，避免每次全量重跑。"""
    source_id = str(row.get("id") or row.get("url") or "unknown")
    digest = hashlib.sha256(
        (
            f"{EXTRACT_CACHE_VERSION}|{source_id}|{row.get('title') or ''}|{body or ''}"
        ).encode("utf-8")
    ).hexdigest()
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", source_id)[:60]
    path = EXTRACT_CACHE_DIR / f"{safe_id}-{digest[:20]}.json"
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("signature") == digest and isinstance(payload.get("rows"), list):
                return (
                    [item for item in payload["rows"] if isinstance(item, dict)],
                    "hit",
                )
    except (OSError, json.JSONDecodeError):
        pass
    rows = _extract_from_article(row, body, aliases)
    try:
        EXTRACT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"signature": digest, "rows": rows},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    return rows, "miss"


def refresh(
    *,
    days: int = 30,
    limit: int = 80,
    reset: bool = False,
) -> dict[str, Any]:
    """抓取近期候选人有新闻正文并更新言论库。"""
    db.init_db()
    _candidates, aliases = _candidate_registry()
    rows = db.fetch_news_since(days=days, limit=max(limit * 8, 1200))
    selected: list[dict[str, Any]] = []
    for raw in rows:
        row = {key: raw[key] for key in raw.keys()}
        probe = f"{row.get('title') or ''} {row.get('summary') or ''}"
        matched = _match_candidates(probe, aliases)
        if not matched and not _analysis_candidates(row):
            continue
        selected.append(row)
        if len(selected) >= limit:
            break
    bodies = newstext.bodies_for(
        selected,
        workers=8,
        timeout=6,
        retries=1,
    )
    extracted: list[dict[str, Any]] = []
    direct_count = 0
    cache_hits = 0
    cache_misses = 0
    for row, (body, _) in zip(selected, bodies):
        items, cache_state = _extract_cached(row, body, aliases)
        cache_hits += int(cache_state == "hit")
        cache_misses += int(cache_state == "miss")
        direct_count += sum(1 for item in items if item["directness"] == "direct")
        extracted.extend(items)
    source_jobs = {
        "youtube": lambda: statement_sources.fetch_youtube_rss_statements(
            days=days,
            candidates=_candidates,
        ),
        "bulletins": lambda: statement_sources.fetch_bulletin_statements(_candidates),
        "ivod": lambda: statement_sources.fetch_ivod_text_statements(
            _candidates,
            days=days,
        ),
        "directory": lambda: statement_sources.sync_election_directory(_candidates),
    }
    source_results: dict[str, statement_sources.SourceResult] = {}
    source_job_labels = {
        "youtube": "官方YouTube",
        "bulletins": "选举公报",
        "ivod": "立法院IVOD",
        "directory": "选举黄页",
    }
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(function): name
            for name, function in source_jobs.items()
        }
        for future in futures:
            name = futures[future]
            try:
                source_results[name] = future.result()
            except Exception as exc:  # noqa: BLE001 - 單一來源失敗不阻斷既有言論
                source_results[name] = statement_sources.SourceResult(
                    warnings=[f"{name}：{type(exc).__name__}: {exc}"]
                )
    source_warnings: list[str] = []
    for name, result in source_results.items():
        extracted.extend(result.rows)
        source_warnings.extend(
            f"{source_job_labels.get(name, name)}：{warning}"
            for warning in result.warnings
        )
    removed = db.clear_candidate_statements() if reset and extracted else 0
    written = db.save_candidate_statements(extracted)
    counts = db.candidate_statement_counts()
    source_counts = {
        kind: sum(1 for item in extracted if item.get("directness") == kind)
        for kind in statement_sources.SOURCE_LABELS
    }
    return {
        "reset_removed": removed,
        "news_scanned": len(rows),
        "articles_selected": len(selected),
        "statements_written": written,
        "statements": counts["statements"],
        "direct_statements": direct_count,
        "extraction_cache": {
            "hits": cache_hits,
            "misses": cache_misses,
        },
        "source_counts": source_counts,
        "source_stats": {
            name: result.stats for name, result in source_results.items()
        },
        "source_warnings": source_warnings[:20],
        "account_warnings": (
            source_results.get("youtube", statement_sources.SourceResult()).warnings
        ),
        "candidates": counts["candidates"],
        "topics": counts["topics"],
        "latest": counts["latest"],
    }


def _dedupe_statements(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    by_group: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in sorted(rows, key=lambda item: item.get("published_at") or ""):
        key = (row["candidate_name"], row["topic"])
        grams = bigrams(normalize_title(row["claim"]))
        duplicate = False
        for previous in by_group.get(key, []):
            if jaccard(grams, previous["_grams"]) >= 0.88:
                duplicate = True
                if (
                    statement_sources.SOURCE_CONFIDENCE.get(str(row.get("directness")), 0)
                    > statement_sources.SOURCE_CONFIDENCE.get(str(previous.get("directness")), 0)
                ):
                    previous.update(row)
                    previous["_grams"] = grams
                break
        if duplicate:
            continue
        row["_grams"] = grams
        output.append(row)
        by_group.setdefault(key, []).append(row)
    return output


def _local_relation(
    older: dict[str, Any],
    newer: dict[str, Any],
) -> tuple[str, float, str]:
    similarity = jaccard(
        bigrams(normalize_title(older["claim"])),
        bigrams(normalize_title(newer["claim"])),
    )
    older_source = str(older.get("directness") or "")
    newer_source = str(newer.get("directness") or "")
    comparable = {
        older_source,
        newer_source,
    }.issubset(statement_sources.COMPARABLE_SOURCES)
    source_confidence = min(
        statement_sources.SOURCE_CONFIDENCE.get(older_source, 0),
        statement_sources.SOURCE_CONFIDENCE.get(newer_source, 0),
    )
    if not comparable:
        return (
            "insufficient",
            round(min(source_confidence, 0.35), 2),
            "至少一条来自新闻转述或官方频道标题，未达到前后矛盾判断门槛。",
        )
    if similarity >= 0.72:
        return (
            "consistent",
            round(min(0.9, source_confidence + 0.03), 2),
            "前后表述高度相似，核心说法基本一致。",
        )
    if older.get("polarity") and newer.get("polarity") and older["polarity"] != newer["polarity"]:
        return (
            "suspected_inconsistency",
            round(min(0.9, source_confidence), 2),
            "同一议题上的支持或反对方向发生变化，需要核对原文、日期和当时条件。",
        )
    older_conditions = older.get("conditions") or ""
    newer_conditions = newer.get("conditions") or ""
    if bool(older_conditions) != bool(newer_conditions):
        return (
            "condition_change",
            round(min(0.72, source_confidence), 2),
            "一条表述带有前提条件，另一条没有；可能属于条件变化，不宜直接判为矛盾。",
        )
    if older.get("stance") == newer.get("stance") and similarity >= 0.28:
        return (
            "clarification",
            round(min(0.65, source_confidence), 2),
            "方向相同但表述范围有变化，更可能是补充或澄清。",
        )
    return (
        "insufficient",
        round(min(0.35, source_confidence), 2),
        "两条言论虽属同一议题，但文字差异较大，暂不足以判断是否一致。",
    )


def _pairs(statements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in statements:
        groups.setdefault((item["candidate_name"], item["topic"]), []).append(item)
    pairs: list[dict[str, Any]] = []
    for (candidate, topic), group in groups.items():
        if topic == "其他政策":
            continue
        group.sort(key=lambda item: item.get("published_at") or "")
        for index, newer in enumerate(group):
            for older in group[max(0, index - 4):index]:
                if not older.get("published_at") or not newer.get("published_at"):
                    continue
                if older["published_at"] == newer["published_at"]:
                    continue
                relation, confidence, explanation = _local_relation(older, newer)
                if relation in ("insufficient", "consistent"):
                    continue
                pairs.append({
                    "id": f"{older['id']}::{newer['id']}",
                    "candidate": candidate,
                    "topic": topic,
                    "relation": relation,
                    "relation_label": RELATION_LABELS[relation],
                    "severity": RELATION_SEVERITY[relation],
                    "confidence": confidence,
                    "explanation": explanation,
                    "older": {k: v for k, v in older.items() if not k.startswith("_")},
                    "newer": {k: v for k, v in newer.items() if not k.startswith("_")},
                })
    pairs.sort(
        key=lambda item: (
            int(item["severity"]),
            float(item["confidence"]),
            str(item["newer"].get("published_at") or ""),
        ),
        reverse=True,
    )
    return pairs[:80]


def get_payload(
    settings: Any,
    *,
    days: int = 90,
    candidate: str = "",
    topic: str = "",
) -> dict[str, Any]:
    db.init_db()
    rows = [
        {key: row[key] for key in row.keys()}
        for row in db.fetch_candidate_statements(days=days, limit=5000)
    ]
    all_statements = [
        item for item in _dedupe_statements(rows)
        if _valid_candidate_name(str(item.get("candidate_name") or ""))
    ]
    candidate_filter = to_simplified(candidate).strip()
    topic_filter = to_simplified(topic).strip()
    filtered = [
        item for item in all_statements
        if (not candidate_filter or to_simplified(item["candidate_name"]) == candidate_filter)
        and (not topic_filter or to_simplified(item["topic"]) == topic_filter)
    ]
    pairs = _pairs(filtered)
    candidate_counts: dict[str, int] = {}
    topic_counts: dict[str, int] = {}
    for item in all_statements:
        candidate_counts[item["candidate_name"]] = candidate_counts.get(item["candidate_name"], 0) + 1
        topic_counts[item["topic"]] = topic_counts.get(item["topic"], 0) + 1
    return {
        "schema_version": 2,
        "ok": True,
        "days": days,
        "candidate": candidate_filter,
        "topic": topic_filter,
        "llm_available": bool(settings.llm_api_key),
        "counts": {
            "statements": len(filtered),
            "direct_statements": sum(1 for item in filtered if item.get("directness") == "direct"),
            "official_bulletin_statements": sum(
                1 for item in filtered if item.get("directness") == "official_bulletin"
            ),
            "official_transcript_statements": sum(
                1 for item in filtered if item.get("directness") == "official_transcript"
            ),
            "official_account_statements": sum(
                1 for item in filtered if item.get("directness") == "official_account"
            ),
            "paraphrase_statements": sum(
                1 for item in filtered if item.get("directness") == "paraphrase"
            ),
            "candidates": len(candidate_counts),
            "topics": len(topic_counts),
            "changes": len(pairs),
            "suspected_inconsistencies": sum(
                1 for item in pairs if item["relation"] == "suspected_inconsistency"
            ),
        },
        "candidate_facets": [
            {"candidate": name, "count": count}
            for name, count in sorted(candidate_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "topic_facets": [
            {"topic": name, "count": count}
            for name, count in sorted(topic_counts.items(), key=lambda pair: (-pair[1], pair[0]))
        ],
        "source_facets": [
            {
                "kind": kind,
                "label": label,
                "count": sum(
                    1 for item in filtered if item.get("directness") == kind
                ),
                "confidence": statement_sources.SOURCE_CONFIDENCE[kind],
            }
            for kind, label in statement_sources.SOURCE_LABELS.items()
        ],
        "statements": sorted(
            (
                {key: value for key, value in item.items() if not key.startswith("_")}
                for item in filtered
            ),
            key=lambda item: str(item.get("published_at") or ""),
            reverse=True,
        )[:300],
        "pairs": pairs,
        "notes": [
            "选举公报只读取网页文字版；扫描PDF会跳过，不做OCR。",
            "立法院只读取现成文字记录；不下载音视频，也不做语音转写。",
            "官方频道标题和简介不等于完整讲话；新闻转述不单独触发前后矛盾。",
            "前后变化不等于说谎；可能来自条件、时间或政策调整。",
            "所有结论均应回到原文核验。",
        ],
    }


def review_pair(settings: Any, older_id: str, newer_id: str) -> dict[str, Any]:
    older_row = db.fetch_candidate_statement(older_id)
    newer_row = db.fetch_candidate_statement(newer_id)
    if not older_row or not newer_row:
        raise ValueError("言论记录不存在")
    older = {key: older_row[key] for key in older_row.keys()}
    newer = {key: newer_row[key] for key in newer_row.keys()}
    if older["candidate_name"] != newer["candidate_name"]:
        raise ValueError("两条言论不属于同一候选人")
    client = LLMClient(
        settings.llm_base_url,
        settings.llm_api_key,
        settings.llm_model,
        min(settings.llm_timeout, 60),
    )
    if not client.available:
        raise ValueError("尚未配置大模型 API Key")
    payload = {
        "candidate": older["candidate_name"],
        "topic": older["topic"],
        "older": {
            "date": older.get("published_at"),
            "claim": older.get("claim"),
            "quote": older.get("quote_text"),
            "source": older.get("source_name"),
            "url": older.get("source_url"),
            "directness": older.get("directness"),
        },
        "newer": {
            "date": newer.get("published_at"),
            "claim": newer.get("claim"),
            "quote": newer.get("quote_text"),
            "source": newer.get("source_name"),
            "url": newer.get("source_url"),
            "directness": newer.get("directness"),
        },
    }
    result = client.complete_json(
        (
            "你是台湾选举研究的事实核验助手。只依据输入的两条原文判断，"
            "不得补充外部事实，不得把政策调整或条件变化直接称为说谎。"
            "只输出JSON。"
        ),
        (
            "比较同一位候选人在同一议题上的前后表述。relation只能是："
            "consistent、clarification、condition_change、policy_change、"
            "suspected_inconsistency、insufficient。"
            "输出字段：relation、explanation、confidence(0到1)、"
            "older_key_point、newer_key_point。\n"
            + json.dumps(payload, ensure_ascii=False)
        ),
        temperature=0,
        max_tokens=500,
        json_mode=True,
    )
    relation = str(result.get("relation") or "insufficient")
    if relation not in RELATION_LABELS:
        relation = "insufficient"
    return {
        "ok": True,
        "relation": relation,
        "relation_label": RELATION_LABELS[relation],
        "explanation": str(result.get("explanation") or "")[:500],
        "confidence": max(0.0, min(1.0, float(result.get("confidence") or 0))),
        "older_key_point": str(result.get("older_key_point") or "")[:160],
        "newer_key_point": str(result.get("newer_key_point") or "")[:160],
        "model": settings.llm_model,
    }
