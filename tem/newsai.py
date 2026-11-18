"""新闻流手动 AI 分析：热门议题、党派影响与候选人影响。"""

from __future__ import annotations

import json
import hashlib
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Callable, Iterable

from . import db, lexicon, newstext
from .config import Settings
from .llm import LLMClient
from .models import MarketSnapshot
from .paths import DATA_DIR
from .zhconv import to_simplified

SYSTEM_PROMPT = """你是专精台湾地方选举的舆情分析师。请只依据新闻原文分析，不得补充原文没有的事实，不得预测谁当选。

评分规则：
1. sentiment 表示对某个候选人、政党或议题的有利程度，范围 -1 到 1。
2. impact 表示这条新闻造成影响的强度，范围 0 到 1，与正负方向无关。
3. 同一事件可以同时对甲有利、对乙不利，不要假设一方增加多少，另一方就等量减少。
4. 区分直接当事人、同党外溢、对立阵营相对受益和旁观者；同党外溢必须明显弱于直接当事人。
5. 热门议题名称控制在 4 至 12 个简体汉字。
6. 候选人只能从给定名单中选择；无法对应的候选人不要输出。
7. topics、party_effects、candidate_effects 的每一项都必须提供 evidence，且 evidence 必须是输入正文中的原文连续片段，不得改写、概括或拼接；没有原文依据的项目不要输出。
8. 只输出 JSON，不要 Markdown。"""

USER_TEMPLATE = """请分析下面这条新闻对选情的影响。

【允许的候选人：简体中文名 → 英文名】
{candidates}

【允许的政党】
中国国民党、民主进步党、台湾民众党、无党籍、其他

【新闻标题】
{title}

【新闻来源】
{source}

【新闻正文或摘要】
{body}

严格输出以下 JSON：
{{
  "event_type": "事件类型，例如民调选情、司法争议、政策政见、竞选造势、政党整合",
  "overall_sentiment": -0.0,
  "overall_impact": 0.0,
  "topics": [
    {{"topic": "议题名", "sentiment": 0.0, "impact": 0.0, "evidence": "输入正文中的原文原句"}}
  ],
  "party_effects": [
    {{"party": "中国国民党", "sentiment": 0.0, "impact": 0.0, "reason": "判断理由", "evidence": "原文依据"}}
  ],
  "candidate_effects": [
    {{"candidate": "候选人中文名", "role": "直接当事人或同党外溢或对立阵营相对受益", "sentiment": 0.0, "impact": 0.0, "reason": "判断理由", "evidence": "原文依据"}}
  ],
  "summary": "60 字以内的事件影响摘要",
  "confidence": 0.0
}}"""

BATCH_USER_TEMPLATE = """请逐条分析以下 {count} 条新闻。每条新闻都包含 id、标题、来源、相关选区候选人和正文。
不要把不同新闻的影响混在一起；每条新闻独立给出下列字段。

【新闻数据】
{items}

只输出一个 JSON 对象：
{{
  "results": [
    {{
      "news_id": "输入时的 id",
      "event_type": "事件类型",
      "overall_sentiment": 0.0,
      "overall_impact": 0.0,
      "topics": [{{"topic": "议题名", "sentiment": 0.0, "impact": 0.0, "evidence": "输入正文中的原文原句"}}],
      "party_effects": [{{"party": "中国国民党", "sentiment": 0.0, "impact": 0.0, "reason": "理由", "evidence": "原文依据"}}],
      "candidate_effects": [{{"candidate": "候选人中文名", "role": "直接当事人或同党外溢或对立阵营相对受益", "sentiment": 0.0, "impact": 0.0, "reason": "理由", "evidence": "原文依据"}}],
      "summary": "60 字以内摘要",
      "confidence": 0.0
    }}
  ]
}}"""

AI_CACHE_DIR = DATA_DIR / "cache" / "news_ai"
CACHE_VERSION = "v3-validated-effects"

PARTY_ALIASES = {
    "国民党": "中国国民党",
    "中国国民党": "中国国民党",
    "民进党": "民主进步党",
    "民主进步党": "民主进步党",
    "民众党": "台湾民众党",
    "台湾民众党": "台湾民众党",
    "无党籍": "无党籍",
    "其他": "其他",
}
EVENT_TYPES = {
    "民调选情", "司法争议", "政策政见", "竞选造势", "政党整合",
    "提名征召", "两岸国安", "选务争议", "其他",
}

_JSON_DECODER = json.JSONDecoder()


def _num(value: Any, lo: float, hi: float) -> float:
    try:
        return round(max(lo, min(hi, float(value))), 3)
    except (TypeError, ValueError):
        return 0.0


def _candidate_map(settings: Settings) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for en, zh in settings.name_map.items():
        out[to_simplified(zh)] = (en, zh)
        out[en.lower()] = (en, zh)
    return out


def _parse_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _compact_body(title: str, body: str, limit: int = 3200) -> str:
    lines = []
    seen = set()
    for raw in (body or "").splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        key = re.sub(r"\W+", "", line)
        if not line or key in seen or (line and line in title):
            continue
        seen.add(key)
        lines.append(line)
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    head = int(limit * 0.82)
    tail = limit - head
    return text[:head] + "\n……\n" + text[-tail:]


def _cache_file(news_id: str, model: str):
    safe_model = re.sub(r"[^\w.-]+", "_", model)
    return AI_CACHE_DIR / f"{news_id}-{safe_model}.json"


def _load_cached(path, content_hash: str) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("content_hash") == content_hash and isinstance(data.get("analysis"), dict):
            return data["analysis"]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return None


def _save_cached(path, content_hash: str, analysis: dict[str, Any]) -> None:
    AI_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"content_hash": content_hash, "analysis": analysis}, ensure_ascii=False),
        encoding="utf-8",
    )


def _candidate_names(value: Any) -> list[str]:
    out: list[str] = []
    for item in _parse_list(value):
        name = item.get("name_zh") or item.get("name") if isinstance(item, dict) else str(item)
        if name and str(name) not in out:
            out.append(str(name))
    return out


def _regions_for(settings: Settings, detected: list[Any], fallback: Any) -> list[str]:
    if detected:
        return [region.name for region in detected[:3]]
    names = [
        item.get("name") or item.get("name_zh") if isinstance(item, dict) else str(item)
        for item in _parse_list(fallback)
    ]
    return [name for name in names if name][:3] or ["全岛"]


def _evidence_key(value: Any) -> str:
    return re.sub(r"[\W_]+", "", to_simplified(str(value or ""))).lower()


def _grounded(evidence: Any, source: str) -> bool:
    needle = _evidence_key(evidence)
    return len(needle) >= 5 and needle in _evidence_key(source)


def _normalise(
    payload: dict[str, Any],
    *,
    candidates: dict[str, tuple[str, str]],
    region_names: list[str],
    source_text: str,
    body_status: str,
    title: str,
    summary: str,
    source: str,
) -> dict[str, Any]:
    topic_rows = []
    discarded = 0
    grounding_source = f"{title}\n{source_text}"
    for item in payload.get("topics") or []:
        if isinstance(item, str):
            item = {"topic": item}
        if (
            not isinstance(item, dict)
            or not item.get("topic")
            or not _grounded(item.get("evidence"), grounding_source)
        ):
            discarded += 1
            continue
        topic_rows.append({
            "topic": str(item["topic"])[:20],
            "sentiment": _num(item.get("sentiment"), -1, 1),
            "impact": _num(item.get("impact"), 0, 1),
            "evidence": str(item.get("evidence") or "")[:240],
        })

    party_rows = []
    for item in payload.get("party_effects") or []:
        if not isinstance(item, dict):
            continue
        party = PARTY_ALIASES.get(str(item.get("party") or "").strip())
        if not party or not _grounded(item.get("evidence"), grounding_source):
            discarded += 1
            continue
        party_rows.append({
            "party": party,
            "sentiment": _num(item.get("sentiment"), -1, 1),
            "impact": _num(item.get("impact"), 0, 1),
            "reason": str(item.get("reason") or "")[:240],
            "evidence": str(item.get("evidence") or "")[:240],
        })

    candidate_rows = []
    seen: set[str] = set()
    for item in payload.get("candidate_effects") or []:
        if not isinstance(item, dict):
            continue
        raw_name = str(item.get("candidate") or item.get("name") or "").strip()
        hit = candidates.get(to_simplified(raw_name)) or candidates.get(raw_name.lower())
        if not hit or hit[0] in seen or not _grounded(item.get("evidence"), grounding_source):
            discarded += 1
            continue
        en, zh = hit
        seen.add(en)
        candidate_rows.append({
            "name": en,
            "name_zh": zh,
            "role": str(item.get("role") or "")[:80],
            "sentiment": _num(item.get("sentiment"), -1, 1),
            "impact": _num(item.get("impact"), 0, 1),
            "reason": str(item.get("reason") or "")[:240],
            "evidence": str(item.get("evidence") or "")[:240],
        })

    source_kind = "full-text" if body_status in ("full", "short") else "summary"
    effect_pool = party_rows + candidate_rows
    if not effect_pool:
        effect_pool = topic_rows
    effect_weight = sum(0.5 + float(item.get("impact") or 0) for item in effect_pool)
    if effect_weight:
        overall_sentiment = sum(
            float(item.get("sentiment") or 0) * (0.5 + float(item.get("impact") or 0))
            for item in effect_pool
        ) / effect_weight
        overall_impact = max(float(item.get("impact") or 0) for item in effect_pool)
    else:
        overall_sentiment = 0.0
        overall_impact = 0.0
    confidence = _num(payload.get("confidence"), 0, 1)
    if source_kind == "summary":
        overall_impact = min(overall_impact, 0.55)
        confidence = min(confidence, 0.60)
    return {
        "event_type": (
            str(payload.get("event_type"))
            if str(payload.get("event_type")) in EVENT_TYPES else "其他"
        ),
        "overall_sentiment": round(max(-1.0, min(1.0, overall_sentiment)), 3),
        "overall_impact": overall_impact,
        "topics": topic_rows[:8],
        "party_effects": party_rows[:8],
        "candidate_effects": candidate_rows[:30],
        "regions": region_names,
        "summary": "；".join(item["topic"] for item in topic_rows[:3])[:240],
        "confidence": confidence,
        "title": title,
        "source": source,
        "rss_summary": summary,
        "grounding": {
            "checked": True,
            "source": source_kind,
            "discarded": discarded,
            "accepted": len(topic_rows) + len(party_rows) + len(candidate_rows),
            "score_source": "validated-effects",
        },
    }


def _fallback(row: dict[str, Any], body: str, region_names: list[str], candidates: list[tuple[str, str]]) -> dict[str, Any]:
    result = lexicon.analyze_item(row.get("title") or "", body or row.get("summary") or "", candidates)
    return {
        "event_type": "规则式回退",
        "overall_sentiment": result["sentiment"],
        "overall_impact": result["impact"],
        "topics": [{"topic": topic, "sentiment": result["sentiment"], "impact": result["impact"]} for topic in result["topics"]],
        "party_effects": [],
        "candidate_effects": [
            {
                "name": item["name"], "name_zh": item["name_zh"], "role": "直接提及",
                "sentiment": item["sentiment"], "impact": result["impact"],
                "reason": "LLM 调用失败，使用规则式引擎", "evidence": "",
            }
            for item in result["candidates"]
        ],
        "regions": region_names,
        "summary": result["summary"],
        "confidence": 0.25,
        "title": row.get("title") or "",
        "source": row.get("source_name") or "",
        "rss_summary": row.get("summary") or "",
        "grounding": {
            "checked": False,
            "source": "fallback",
            "discarded": 0,
            "accepted": 0,
        },
        "error": "llm-fallback",
    }


def aggregate(items: Iterable[dict[str, Any]]) -> dict[str, Any]:
    topic_acc: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0.0, "sentiment": 0.0, "impact": 0.0, "weight": 0.0})
    party_acc: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0.0, "sentiment": 0.0, "impact": 0.0, "weight": 0.0})
    cand_acc: dict[str, dict[str, Any]] = {}
    for item in items:
        overall_impact = float(item.get("overall_impact") or 0)
        weight = 0.5 + overall_impact
        for topic in item.get("topics") or []:
            name = topic["topic"]
            acc = topic_acc[name]
            acc["count"] += 1
            acc["sentiment"] += float(topic.get("sentiment") or 0)
            acc["impact"] += float(topic.get("impact") or 0)
            acc["weight"] += weight
        for effect in item.get("party_effects") or []:
            name = effect["party"]
            effect_weight = 0.5 + float(effect.get("impact") or 0)
            acc = party_acc[name]
            acc["count"] += 1
            acc["sentiment"] += float(effect.get("sentiment") or 0) * effect_weight
            acc["impact"] += float(effect.get("impact") or 0) * effect_weight
            acc["weight"] += effect_weight
        for effect in item.get("candidate_effects") or []:
            name = effect["name"]
            acc = cand_acc.setdefault(name, {
                "name": name, "name_zh": effect["name_zh"], "count": 0,
                "sentiment_num": 0.0, "impact_num": 0.0, "weight": 0.0,
                "roles": defaultdict(int),
            })
            effect_weight = 0.5 + float(effect.get("impact") or 0)
            acc["count"] += 1
            acc["sentiment_num"] += float(effect.get("sentiment") or 0) * effect_weight
            acc["impact_num"] += float(effect.get("impact") or 0) * effect_weight
            acc["weight"] += effect_weight
            acc["roles"][effect.get("role") or "其他"] += 1

    topics = [
        {
            "topic": name,
            "count": int(acc["count"]),
            "sentiment": round(acc["sentiment"] / acc["count"], 3),
            "impact": round(acc["impact"] / acc["count"], 3),
        }
        for name, acc in topic_acc.items()
    ]
    parties = [
        {
            "party": name,
            "count": int(acc["count"]),
            "sentiment": round(acc["sentiment"] / acc["weight"], 3),
            "impact": round(acc["impact"] / acc["weight"], 3),
        }
        for name, acc in party_acc.items()
    ]
    candidates = [
        {
            "name": acc["name"],
            "name_zh": acc["name_zh"],
            "count": acc["count"],
            "sentiment": round(acc["sentiment_num"] / acc["weight"], 3),
            "impact": round(acc["impact_num"] / acc["weight"], 3),
            "roles": dict(acc["roles"]),
        }
        for acc in cand_acc.values()
    ]
    topics.sort(key=lambda row: (-row["count"], -row["impact"]))
    parties.sort(key=lambda row: (-row["impact"], -row["count"]))
    candidates.sort(key=lambda row: (-row["impact"], -row["sentiment"]))
    return {"hot_topics": topics, "party_effects": parties, "candidate_effects": candidates}


def analyze_rows(
    settings: Settings,
    client: LLMClient,
    rows: list[dict[str, Any]],
    snapshots: dict[str, MarketSnapshot],
    *,
    batch_size: int = 4,
    workers: int = 2,
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    from .analyze import _candidate_pairs
    from .news import build_candidate_index, detect_regions

    candidate_map = _candidate_map(settings)
    candidate_index = build_candidate_index(snapshots)
    body_results = newstext.bodies_for(rows, workers=6)
    prepared: list[dict[str, Any]] = []
    results: list[dict[str, Any] | None] = [None] * len(rows)
    cached_count = 0
    for index, row in enumerate(rows):
        body, body_status = body_results[index]
        text = f"{row.get('title') or ''} {body or row.get('summary') or ''}"
        detected = detect_regions(text, settings, candidate_index)
        regions = _regions_for(settings, detected, row.get("regions"))
        pairs = _candidate_pairs(settings, detected, snapshots)
        if not pairs:
            pairs = [
                (en, zh) for en, zh in settings.name_map.items()
                if zh and zh in text
            ][:12]
        compact = _compact_body(row.get("title") or "", body or row.get("summary") or "", 3200)
        content_hash = hashlib.sha256(
            f"{CACHE_VERSION}\n{row.get('title') or ''}\n{compact}".encode("utf-8")
        ).hexdigest()
        cache_path = _cache_file(str(row.get("id") or index), client.model)
        cached = None if force else _load_cached(cache_path, content_hash)
        if cached:
            cached.update({
                "news_id": row.get("id"),
                "url": row.get("url") or "",
                "published_at": row.get("published_at") or row.get("first_seen") or "",
                "body_status": body_status,
                "cached": True,
            })
            results[index] = cached
            cached_count += 1
        prepared.append({
            "index": index,
            "row": row,
            "body": compact,
            "body_status": body_status,
            "regions": regions,
            "pairs": pairs[:12],
            "content_hash": content_hash,
            "cache_path": cache_path,
        })

    pending = [item for item in prepared if results[item["index"]] is None]
    batches = [
        pending[offset:offset + max(1, min(batch_size, 6))]
        for offset in range(0, len(pending), max(1, min(batch_size, 6)))
    ]
    completed = cached_count

    def run_batch(batch: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        payload = None
        last_error: Exception | None = None
        for body_limit in (3200, 2200):
            inputs = []
            for item in batch:
                row = item["row"]
                inputs.append({
                    "id": str(row.get("id") or ""),
                    "title": row.get("title") or "",
                    "source": row.get("source_name") or "",
                    "regions": item["regions"],
                    "candidates": {zh: en for en, zh in item["pairs"]},
                    "body": (item["body"] or "（无正文和摘要）")[:body_limit],
                })
            try:
                payload = client.complete_json(
                    SYSTEM_PROMPT,
                    BATCH_USER_TEMPLATE.format(
                        count=len(inputs),
                        items=json.dumps(inputs, ensure_ascii=False),
                    ),
                    temperature=0.0,
                    max_tokens=min(4600, 700 + 850 * len(inputs)),
                    json_mode=True,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if payload is None:
            raise RuntimeError(f"批次分析失败：{last_error}")
        by_id = {
            str(item.get("news_id")): item
            for item in payload.get("results") or []
            if isinstance(item, dict)
        }
        output = []
        for item in batch:
            row = item["row"]
            raw = by_id.get(str(row.get("id") or ""))
            try:
                if not raw:
                    raise ValueError("LLM 未返回该条结果")
                result = _normalise(
                    raw,
                    candidates=candidate_map,
                    region_names=item["regions"],
                    source_text=item["body"],
                    body_status=item["body_status"],
                    title=row.get("title") or "",
                    summary=row.get("summary") or "",
                    source=row.get("source_name") or "",
                )
                if int((result.get("grounding") or {}).get("accepted") or 0) <= 0:
                    raise ValueError("没有通过原文证据校验的议题或影响")
                result["cached"] = False
                _save_cached(item["cache_path"], item["content_hash"], result)
            except Exception as exc:  # noqa: BLE001
                result = _fallback(row, item["body"], item["regions"], item["pairs"])
                result["error_detail"] = f"{type(exc).__name__}: {exc}"
            result["news_id"] = row.get("id")
            result["url"] = row.get("url") or ""
            result["published_at"] = row.get("published_at") or row.get("first_seen") or ""
            result["body_status"] = item["body_status"]
            output.append((item, result))
        return output

    if progress and cached_count:
        progress(cached_count, len(rows))
    if batches:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, 3))) as pool:
            future_batches = {pool.submit(run_batch, batch): batch for batch in batches}
            for future in as_completed(future_batches):
                batch = future_batches[future]
                try:
                    batch_results = future.result()
                except Exception as exc:  # noqa: BLE001
                    batch_results = []
                    for item in batch:
                        row = item["row"]
                        result = _fallback(
                            row,
                            item["body"],
                            item["regions"],
                            item["pairs"],
                        )
                        result["error_detail"] = f"{type(exc).__name__}: {exc}"
                        result["news_id"] = row.get("id")
                        result["url"] = row.get("url") or ""
                        result["published_at"] = row.get("published_at") or row.get("first_seen") or ""
                        result["body_status"] = item["body_status"]
                        batch_results.append((item, result))
                for item, result in batch_results:
                    results[item["index"]] = result
                completed += len(batch_results)
                if progress:
                    progress(min(completed, len(rows)), len(rows))

    items = [item for item in results if item]
    analysis_rows = []
    for item in items:
        candidates = [
            {
                "name": effect["name"],
                "name_zh": effect["name_zh"],
                "mentions": 1,
                "sentiment": effect["sentiment"],
            }
            for effect in item.get("candidate_effects") or []
        ]
        analysis_rows.append({
            "news_id": item["news_id"],
            "analyzed_at": db.utcnow_iso(),
            "engine": "lexicon" if item.get("error") else "llm",
            "model": "rule-based-zh-v1" if item.get("error") else client.model,
            "sentiment": item["overall_sentiment"],
            "impact": item["overall_impact"],
            "regions": item["regions"],
            "candidates": candidates,
            "topics": [topic["topic"] for topic in item.get("topics") or []],
            "summary": item["summary"],
        })
    if analysis_rows:
        db.save_analyses(analysis_rows)
    aggregate_result = aggregate(items)
    return {
        "model": client.model,
        "analyzed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(items),
        "cached": cached_count,
        "llm_batches": len(batches),
        "token_mode": "event-reps + content-hash cache + 4-item batches + compact bodies",
        "items": items,
        **aggregate_result,
    }
