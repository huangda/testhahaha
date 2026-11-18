"""本地 Dashboard：標準庫 http.server + 內建 API。"""

from __future__ import annotations

import json
import csv
import copy
import io
import mimetypes
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import (
    db,
    google_trends,
    hotissues,
    hotwords,
    market_movers,
    media,
    newsai,
    newstext,
    observe,
    pipeline,
    ptt_opinion,
    statements,
    tikhub_social,
    topic_graph,
    youtube_opinion,
)
from .config import load_settings
from .dotenv import save_env_values
from .llm import LLMClient
from .models import build_snapshots
from .paths import EXPORT_DIR, STATIC_DIR
from .roster import entry_for, load_roster
from .zhconv import deep_simplify, is_simplified_preferred, to_simplified


def _out(payload):
    """依 TEM_LANG 決定輸出字形（預設簡體）。資料庫一律保留原文。"""
    return deep_simplify(payload) if is_simplified_preferred() else payload

_lock = threading.Lock()
_job: dict[str, Any] = {"running": False, "message": "閒置", "started_at": None, "finished_at": None, "error": None}
_ai_lock = threading.Lock()
_ai_job: dict[str, Any] = {
    "running": False,
    "message": "尚未执行",
    "started_at": None,
    "finished_at": None,
    "error": None,
}
AI_REPORT = EXPORT_DIR / "news_ai_analysis.json"

DEFAULT_PORT = 8790


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _method_payload(settings) -> dict[str, Any]:
    """「方法与数据源」页要用的实时数字：回测校准曲线、模型权重、各来源样本量。

    全部从当前档案与资料库读，避免页面写死与程序脱节。
    """
    from .paths import CONFIG_DIR
    from .ai_forecast import (
        MAX_MARKET_EFFECT_PP,
        MAX_POLL_EFFECT_PP,
        MAX_POLL_PRIORITY_EFFECT_PP,
        MAX_SENTIMENT_EFFECT_PP,
        MAX_TOTAL_EFFECT_PP,
    )
    from .forecast import TOSSUP_MARGIN

    def load(name: str) -> dict[str, Any]:
        path = CONFIG_DIR / name
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    calibration = load("calibration.json")
    polls = load("polls.json")
    media = load("polls_media.json")
    poll_matchups = 0
    poll_primaries = 0
    for region in (polls.get("regions") or {}).values():
        poll_matchups += len(region.get("matchups") or [])
        poll_primaries += int(region.get("primary_polls") or 0)
    try:
        coverage = json.loads((EXPORT_DIR / "poll_coverage.json").read_text(encoding="utf-8"))
        merged_coverage = coverage.get("merged") or {}
        poll_regions = sum(
            1 for item in merged_coverage.values() if int((item or {}).get("polls") or 0) > 0
        )
        poll_data = sum(int((item or {}).get("polls") or 0) for item in merged_coverage.values())
        poll_direct = sum(
            int((item or {}).get("polls") or 0)
            for item in (coverage.get("direct") or {}).values()
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        poll_regions = len(polls.get("regions") or {})
        poll_data = 0
        poll_direct = 0

    try:
        events = db.event_stats(days=30)
    except Exception:  # noqa: BLE001
        events = {}
    try:
        attention_rows = db.fetch_latest_attention()
        attention_entities = len({r["entity"] for r in attention_rows if r["entity_type"] == "candidate"})
    except Exception:  # noqa: BLE001
        attention_entities = 0
    try:
        depth_rows = db.fetch_latest_market_depth()
    except Exception:  # noqa: BLE001
        depth_rows = []

    return {
        "weights": {
            "tossup_margin": TOSSUP_MARGIN,
            "poll_half_life_days": 45,
            "tier": {"direct": 1.05, "institute": 1.0, "media": 0.75, "media_margin": 0.5},
        },
        "forecast_engine": "llm",
        "ai_limits": {
            "market_effect_pp": MAX_MARKET_EFFECT_PP,
            "poll_effect_pp": MAX_POLL_EFFECT_PP,
            "poll_priority_effect_pp": MAX_POLL_PRIORITY_EFFECT_PP,
            "sentiment_effect_pp": MAX_SENTIMENT_EFFECT_PP,
            "total_effect_pp": MAX_TOTAL_EFFECT_PP,
        },
        "calibration": calibration,
        "counts": {
            "poll_regions": poll_regions,
            "poll_data": poll_data,
            "poll_direct": poll_direct,
            "poll_matchups": poll_matchups,
            "poll_primaries": poll_primaries,
            "media_polls": len(media.get("polls") or []),
            "media_window": (media.get("meta") or {}).get("window_days"),
            "events_30d": events.get("events"),
            "articles_30d": events.get("articles"),
            "attention_candidates": attention_entities,
            "depth_markets": len(depth_rows),
        },
    }


def poll_data_payload() -> dict[str, Any]:
    """返回民调明细、原文链接与 22 县市覆盖状态。"""
    from .paths import CONFIG_DIR
    from .polldirect import validate as validate_direct_poll
    from .poll_accuracy import history_reference, load_poll_accuracy
    from .polls import cross_validate_records, poll_bias

    def load_json(name: str) -> dict[str, Any]:
        path = CONFIG_DIR / name
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    polls = load_json("polls.json")
    direct_polls = load_json("polls_direct.json")
    from .pollwatch import load_media_polls

    media_polls = load_media_polls()
    roster = load_roster()

    def norm_name(value: Any) -> str:
        return to_simplified(str(value or "")).replace("臺", "台").replace(" ", "").strip()

    party_by_region: dict[str, dict[str, str]] = {}
    for region_name, entries in (roster.get("regions") or {}).items():
        region_key = norm_name(region_name)
        party_by_region[region_key] = {
            norm_name(entry.get("name")): str(entry.get("party") or "")
            for entry in entries or []
        }
    try:
        coverage = json.loads((EXPORT_DIR / "poll_coverage.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        coverage = {}

    def norm_region(value: str) -> str:
        return str(value or "").replace("臺", "台").replace("縣", "县").strip()

    selected = {
        norm_region(name): value
        for name, value in (polls.get("regions") or {}).items()
        if isinstance(value, dict)
    }
    merged = coverage.get("merged") or {}
    direct = coverage.get("direct") or {}
    coverage_rows: list[dict[str, Any]] = []
    for region_name, item in merged.items():
        if not isinstance(item, dict):
            continue
        region_poll = selected.get(norm_region(region_name)) or {}
        signal = region_poll.get("signal") or {}
        direct_item = direct.get(region_name) or direct.get(norm_region(region_name)) or {}
        coverage_rows.append(
            {
                "region": region_name,
                "status": item.get("status") or "missing",
                "polls": int(item.get("polls") or 0),
                "age_days": item.get("age_days"),
                "latest": item.get("latest") or "",
                "source": item.get("source") or "",
                "has_signal": bool(item.get("has_signal")),
                "direct_polls": int((direct_item or {}).get("polls") or 0),
                "leader": signal.get("leader") or "",
                "margin": signal.get("margin"),
                "matchup": region_poll.get("matchup") or [],
            }
        )
    status_order = {"fresh": 0, "recent": 1, "aging": 2, "stale": 3, "missing": 4}
    coverage_rows.sort(
        key=lambda item: (
            status_order.get(item["status"], 9),
            item.get("age_days") if item.get("age_days") is not None else 99999,
            item["region"],
        )
    )

    records: list[dict[str, Any]] = []
    seen_records: set[tuple[Any, ...]] = set()

    def add_record(record: dict[str, Any]) -> None:
        values = record.get("values") or {}
        if isinstance(values, dict):
            record["values"] = {
                to_simplified(str(key)): value for key, value in values.items()
            }
            value_signature: Any = tuple(
                sorted(
                    (str(key), str(value))
                    for key, value in (record.get("values") or {}).items()
                )
            )
        else:
            value_signature = json.dumps(values, ensure_ascii=False, sort_keys=True)
        record["matchup"] = to_simplified(str(record.get("matchup") or ""))
        record["lead"] = to_simplified(str(record.get("lead") or ""))
        record["chase"] = to_simplified(str(record.get("chase") or ""))
        region_key = norm_name(record.get("region"))
        candidate_names = set((record.get("values") or {}).keys())
        candidate_names.update(
            name for name in (record.get("lead"), record.get("chase")) if name
        )
        record["candidate_parties"] = {
            name: party_by_region.get(region_key, {}).get(norm_name(name), "")
            for name in candidate_names
        }
        record["published_date"] = str(
            record.get("published_date")
            or record.get("article_date")
            or ""
        )
        record["history_reference"] = history_reference(
            " ".join(
                str(record.get(key) or "")
                for key in ("source", "sponsor", "executor", "title")
            )
        )
        record["bias"] = poll_bias(record)
        validation_errors = validate_direct_poll(
            {
                "title": record.get("title") or "",
                "shares": record.get("values") or {},
                "margin": record.get("margin"),
                "sample_size": record.get("sample_size"),
                "article_date": record.get("article_date") or "",
            }
        )
        if validation_errors:
            return
        signature = (
            str(record.get("region") or ""),
            str(record.get("date") or ""),
            str(record.get("source") or ""),
            value_signature,
            str(record.get("lead") or ""),
            str(record.get("chase") or ""),
            str(record.get("margin") or ""),
        )
        if signature in seen_records:
            return
        seen_records.add(signature)
        records.append(record)

    for region_name, rows in (polls.get("raw") or {}).items():
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            if item.get("tier") in {"direct", "media"}:
                continue
            values = item.get("values")
            add_record({
                "region": region_name,
                "date": item.get("date") or "",
                "source": item.get("source") or "",
                "matchup": item.get("matchup") or "",
                "values": values if isinstance(values, dict) else {},
                "kind": item.get("kind") or "",
                "tier": item.get("tier") or "institute",
                "sample_size": item.get("sample_size"),
                "url": item.get("url") or "",
                "title": item.get("title") or "",
                "report_type": item.get("report_type") or "",
                "sponsor": item.get("sponsor") or "",
                "executor": item.get("executor") or "",
                "lead": item.get("lead") or "",
                "chase": item.get("chase") or "",
                "margin": item.get("gap"),
            })
    for item in direct_polls.get("polls") or []:
        if not isinstance(item, dict):
            continue
        if validate_direct_poll(item):
            continue
        values = item.get("shares") if isinstance(item.get("shares"), dict) else {}
        matchup = item.get("matchup") or " ".join(values.keys())
        add_record({
            "region": item.get("region") or "",
            "date": item.get("fieldwork_end") or item.get("article_date") or "",
            "source": item.get("pollster") or item.get("source") or "",
            "matchup": matchup,
            "values": values,
            "kind": item.get("kind") or "",
            "tier": "direct",
            "article_date": item.get("article_date") or "",
            "sample_size": item.get("sample_size"),
            "url": item.get("url") or "",
            "title": item.get("title") or "",
            "report_type": item.get("report_type") or "",
            "sponsor": item.get("sponsor") or "",
            "executor": item.get("executor") or "",
            "lead": item.get("lead") or "",
            "chase": item.get("chase") or "",
            "margin": item.get("margin"),
        })
    for item in media_polls.get("polls") or []:
        if not isinstance(item, dict):
            continue
        values = item.get("values") if isinstance(item.get("values"), dict) else {}
        add_record({
            "region": item.get("region") or "",
            "date": item.get("date") or "",
            "source": item.get("source") or item.get("media") or "",
            "matchup": " ".join(item.get("matchup") or []),
            "values": values,
            "kind": item.get("kind") or "",
            "tier": "media",
            "article_date": item.get("date") or "",
            "sample_size": item.get("sample_size"),
            "url": item.get("url") or "",
            "title": item.get("title") or "",
            "report_type": item.get("report_type") or "headline",
            "sponsor": item.get("sponsor") or "",
            "executor": item.get("executor") or "",
            "lead": item.get("lead") or "",
            "chase": item.get("chase") or "",
            "margin": item.get("gap"),
        })
    records, verification = cross_validate_records(records)
    records.sort(key=lambda item: (item["date"], item["region"], item["source"]), reverse=True)

    # 同一机构、同一调查日、同一对战组合被多家转载时，如果正文抽取结果
    # 冲突，只保留样本更完整、证据质量更高的一条，避免错误转载混入明细。
    consolidated: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        key = (
            norm_region(record.get("region")),
            str(record.get("date") or ""),
            str(record.get("source") or ""),
            tuple(sorted((record.get("values") or {}).keys())),
        )
        old = consolidated.get(key)
        if old is None:
            consolidated[key] = record
            continue
        score = (
            int(record.get("sample_size") or 0),
            float(record.get("evidence_quality") or 0),
            record.get("report_type") == "full",
            record.get("tier") == "direct",
        )
        old_score = (
            int(old.get("sample_size") or 0),
            float(old.get("evidence_quality") or 0),
            old.get("report_type") == "full",
            old.get("tier") == "direct",
        )
        if score > old_score:
            consolidated[key] = record
    records = list(consolidated.values())
    records.sort(key=lambda item: (item["date"], item["region"], item["source"]), reverse=True)

    def count_status(status: str) -> int:
        return sum(1 for item in coverage_rows if item["status"] == status)

    return {
        "updated_at": coverage.get("updated_at") or (polls.get("meta") or {}).get("updated_at") or "",
        "counts": {
            "polls": sum(item["polls"] for item in coverage_rows),
            "direct": sum(int((item or {}).get("polls") or 0) for item in direct.values()),
            "regions_with_data": sum(1 for item in coverage_rows if item["polls"] > 0),
            "regions_total": len(coverage_rows),
            "fresh": count_status("fresh"),
            "recent": count_status("recent"),
            "stale": count_status("stale"),
            "missing": count_status("missing"),
            "media": sum(1 for item in records if item["tier"] == "media"),
            "records": len(records),
            "verification": verification,
        },
        "verification": verification,
        "history_accuracy": load_poll_accuracy(),
        "coverage": coverage_rows,
        "records": records,
    }


def build_state() -> dict[str, Any]:
    """從資料庫即時組出前端所需的完整狀態。"""
    settings = load_settings()
    roster = load_roster()
    odds_rows = db.fetch_latest_odds()
    snapshots = build_snapshots(odds_rows, name_map=settings.name_map, region_names=settings.region_names())
    hidden_forecast_fields = {"confidence", "sufficiency", "market_quality", "prior_prob"}
    latest_forecasts = db.fetch_latest_forecasts()
    forecasts = [
        {key: value for key, value in _row_to_dict(row).items() if key not in hidden_forecast_fields}
        for row in latest_forecasts
    ]
    region_state = [_row_to_dict(r) for r in db.fetch_latest_region_state()]
    news_rows = [dict(r) for r in db.fetch_recent_news(limit=200)]

    for row in news_rows:
        for key in ("regions", "candidates", "topics"):
            value = row.get(key)
            if isinstance(value, str):
                try:
                    row[key] = json.loads(value)
                except json.JSONDecodeError:
                    row[key] = []
    for row in region_state:
        if isinstance(row.get("top_topics"), str):
            try:
                row["top_topics"] = json.loads(row["top_topics"])
            except json.JSONDecodeError:
                row["top_topics"] = []

    summaries = route_region_summary(forecasts)
    stats = db.stats()
    roster_regions = roster.get("regions") or {}
    stats["roster_candidates"] = sum(len(items or []) for items in roster_regions.values())
    try:
        coverage = json.loads((EXPORT_DIR / "poll_coverage.json").read_text(encoding="utf-8"))
        merged = coverage.get("merged") or {}
        direct = coverage.get("direct") or {}
        stats["poll_data"] = sum(int((item or {}).get("polls") or 0) for item in merged.values())
        stats["poll_regions"] = sum(
            1 for item in merged.values() if int((item or {}).get("polls") or 0) > 0
        )
        stats["poll_direct"] = sum(int((item or {}).get("polls") or 0) for item in direct.values())
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        stats.setdefault("poll_data", 0)
        stats.setdefault("poll_regions", 0)
        stats.setdefault("poll_direct", 0)
    try:
        events = db.event_stats(days=30)
        stats.update({"news_events_30d": events.get("events"), "news_articles_30d": events.get("articles")})
    except Exception:  # noqa: BLE001
        pass

    times = db.snapshot_times()
    for summary in summaries:
        region_name = settings.region_names().get(summary["region_code"], summary["region_name"])
        entries = roster_regions.get(region_name) or []
        summary["roster_count"] = len(entries)
        summary["registered"] = entries
        leader = entry_for(roster, region_name, summary.get("leader_zh") or "", summary.get("leader_en") or "")
        summary["leader_number"] = (leader or {}).get("number")
        summary["leader_party"] = (leader or {}).get("party") or ""
    stats["odds_first_ts"] = times[0] if times else None
    stats["odds_last_ts"] = times[-1] if times else None

    from .analyze import overall_verdict

    states = [
        {
            "region_code": row["region_code"],
            "region_name": row["region_name"],
            "article_count": row.get("article_count") or 0,
            "avg_sentiment": row.get("avg_sentiment") or 0.0,
            "signal_count": row.get("signal_count") or 0,
        }
        for row in region_state
    ]

    # 政黨分布：地圖著色、點擊片區的候選人細節、各黨期望席次
    from .parties import build as build_parties
    from .profile import resolve_profile

    def _profile(region_name: str, zh: str, en: str):
        try:
            return resolve_profile(region_name, zh, en, allow_network=False)
        except Exception:  # noqa: BLE001 - 個人檔案查不到不影響版圖
            return None

    try:
        parties = build_parties(forecasts, _profile)
    except Exception:  # noqa: BLE001
        parties = {}

    forecast_ai: dict[str, Any] = {}
    if latest_forecasts:
        try:
            first_components = latest_forecasts[0]["components"]
            if isinstance(first_components, str):
                first_components = json.loads(first_components)
            if isinstance(first_components, dict):
                forecast_ai = {
                    "engine": first_components.get("calculation_engine"),
                    "model": first_components.get("ai_model"),
                    "method": first_components.get("calculation_method"),
                    "usage": first_components.get("ai_usage") or {},
                    "limits": first_components.get("ai_effect_limits_pp") or {},
                }
        except (TypeError, json.JSONDecodeError):
            forecast_ai = {}

    return {
        "election": settings.election,
        "election_date": settings.election_date,
        "llm_enabled": settings.llm_enabled and bool(settings.llm_api_key),
        "llm_settings": {
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "api_key_set": bool(settings.llm_api_key),
            "api_key_hint": (
                f"••••{settings.llm_api_key[-4:]}" if settings.llm_api_key else ""
            ),
            "timeout": settings.llm_timeout,
        },
        "sentiment_weight": settings.sentiment_weight,
        "stats": stats,
        "method": _method_payload(settings),
        "job": dict(_job),
        "markets": {code: snap.as_dict() for code, snap in snapshots.items()},
        "forecasts": forecasts,
        "forecast_engine": forecast_ai.get("engine") or "none",
        "forecast_ai": forecast_ai,
        "region_summaries": summaries,
        "region_states": region_state,
        "verdict": overall_verdict(states, snapshots),
        "news": news_rows,
        "parties": parties,
        "focus_regions": [
            {"code": region.code, "name": region.name, "type": region.type}
            for region in settings.regions
        ],
        "roster": roster,
        "generated_at": db.latest_ts("forecasts"),
    }


def focus_payload(region_code: str, *, news_limit: int = 12) -> dict[str, Any]:
    """按县市加载议员、乡镇市长和近期新闻，避免首页一次载入全量明细。"""
    settings = load_settings()
    region = settings.region_by_name(region_code)
    if region is None or region.is_nationwide:
        return {"error": "找不到指定县市"}

    from . import council as council_mod
    from . import local as local_mod

    council_data = council_mod.load()
    local_data = local_mod.load()
    region_name = region.name
    council = (council_data.get("regions") or {}).get(region_name)
    local = (local_data.get("regions") or {}).get(region_name)
    if council:
        council = copy.deepcopy(council)
        for district in council.get("districts") or []:
            for candidate in district.get("candidates") or []:
                if candidate.get("wiki_title"):
                    candidate["wiki_title"] = urllib.parse.quote(
                        str(candidate["wiki_title"]), safe=""
                    )

    news: list[dict[str, Any]] = []
    wanted = to_simplified(region_name)
    for row in db.fetch_news_since(days=30, limit=3000):
        item = {key: row[key] for key in row.keys()}
        regions = item.get("regions")
        if isinstance(regions, str):
            try:
                regions = json.loads(regions)
            except json.JSONDecodeError:
                regions = []
        if wanted not in [to_simplified(str(name)) for name in (regions or [])]:
            continue
        news.append(
            {
                "id": item.get("id"),
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "published_at": item.get("published_at") or item.get("first_seen") or "",
                "source_name": item.get("source_name") or "",
                "sentiment": item.get("sentiment"),
                "summary": item.get("summary") or "",
            }
        )
        if len(news) >= news_limit:
            break

    return {
        "region": {
            "code": region.code,
            "name": region.name,
            "type": region.type,
        },
        "council": council,
        "local": local,
        "news": news,
    }


def widget_items(limit: int = 80) -> list[dict[str, Any]]:
    """候选人与赔率卡片的即时数据。"""
    snapshots = build_snapshots(db.fetch_latest_odds(), name_map=load_settings().name_map, region_names=load_settings().region_names())
    items: list[dict[str, Any]] = []
    for code, snap in snapshots.items():
        for cand in snap.candidates:
            items.append(
                {
                    "region": code,
                    "region_name": snap.region_name,
                    "candidate_en": cand.name_en,
                    "candidate_zh": cand.name_zh,
                    "image_url": cand.image_url,
                    "price": cand.yes_price,
                    "chg_1d": cand.chg_1d,
                    "chg_1w": cand.chg_1w,
                    "volume": cand.volume,
                    "event_slug": snap.event_slug,
                }
            )
    items.sort(key=lambda i: -(i["price"] or 0))
    return items[:limit]


def route_region_summary(forecasts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .forecast import region_summary

    if not forecasts:
        return []
    return region_summary(forecasts)


def _run_job(options: dict[str, Any]) -> None:
    global _job
    try:
        settings = load_settings()
        db.init_db()
        _job.update({
            "progress": 2,
            "step": "准备",
            "step_status": "初始化本地数据库与任务环境",
        })
        step_errors: list[str] = []
        timings: dict[str, float] = {}
        try:
            _job["observe_cache_cleared"] = observe.clear_cache()
            _job.update({
                "progress": 5,
                "step": "准备",
                "step_status": "外部缓存已清理",
            })
        except Exception as exc:  # noqa: BLE001 - 外部缓存清理失败不阻断主流程
            step_errors.append(f"外部缓存：{type(exc).__name__}")
        days = min(30, max(1, int(options.get("days", 7))))
        window_days = min(30, max(1, int(options.get("window", days))))
        _job["message"] = "正在并发抓取民调、赔率、新闻与候选人言论库……"
        _job.update({
            "progress": 8,
            "step": "抓取数据",
            "step_status": "民调、赔率、新闻、言论库并行抓取中",
        })

        def timed_step(name: str, function):
            started = time.time()
            try:
                return name, function(), None, time.time() - started
            except Exception as exc:  # noqa: BLE001 - 单步失败不阻断其他抓取
                return name, None, f"{type(exc).__name__}: {exc}", time.time() - started

        steps = {
            "polls": lambda: pipeline.refresh_polls(days=45, limit=24),
            "odds": lambda: pipeline.collect_odds(settings),
            "news": lambda: pipeline.collect_news(
                settings, max_age_days=days
            ),
            "statements": lambda: statements.refresh(days=30, limit=80),
        }
        results: dict[str, Any] = {}
        fetch_done = 0
        fetch_labels = {
            "polls": "民调",
            "odds": "赔率",
            "news": "新闻",
            "statements": "候选人言论",
        }
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(timed_step, name, function): name
                for name, function in steps.items()
            }
            for future in as_completed(futures):
                name, value, error, elapsed = future.result()
                timings[name] = round(elapsed, 2)
                results[name] = value
                fetch_done += 1
                _job["step_timings"] = dict(timings)
                _job.update({
                    "progress": min(53, 8 + fetch_done * 11),
                    "step": "抓取数据",
                    "step_status": (
                        f"{fetch_labels.get(name, name)}完成"
                        f"（{fetch_done}/4）"
                    ),
                })
                if error:
                    step_errors.append(f"{fetch_labels.get(name, name)}：{error}")
        poll_stat = results.get("polls") or {"error": "未知民调错误"}
        _job["polls"] = poll_stat
        _job["statements"] = results.get("statements") or {
            "error": "候选人言论库抓取失败"
        }
        _job["message"] = "正在执行新闻舆情分析……"
        _job.update({
            "progress": 58,
            "step": "新闻分析",
            "step_status": "对新增新闻执行舆情分析",
        })
        started = time.time()
        pipeline.run_analysis(
            settings,
            limit=int(options.get("limit", 80)),
            force=bool(options.get("force")),
            use_llm=bool(options.get("use_llm", False)),
        )
        timings["analysis"] = round(time.time() - started, 2)
        _job["step_timings"] = dict(timings)
        _job.update({
            "progress": 70,
            "step": "AI胜率",
            "step_status": "AI正在综合市场、民调与新闻舆情",
        })
        _job["message"] = "正在使用AI计算综合胜率……"
        started = time.time()
        def ai_progress(ratio: float, status: str) -> None:
            _job.update({
                "progress": min(92, 70 + round(22 * max(0.0, min(1.0, ratio)))),
                "step": "AI胜率",
                "step_status": status,
            })

        payload = pipeline.run_forecast(
            settings,
            window_days=window_days,
            progress_callback=ai_progress,
        )
        timings["forecast"] = round(time.time() - started, 2)
        _job["step_timings"] = dict(timings)
        _job.update({
            "progress": 95,
            "step": "生成组件",
            "step_status": "正在生成嵌入组件与数据文件；分析报道需手动单独生成",
        })
        _job["message"] = "正在生成嵌入组件与数据文件……"
        started = time.time()
        pipeline.write_outputs(
            settings,
            payload,
            generate_report=False,
        )
        timings["outputs"] = round(time.time() - started, 2)
        _job = {
            **_job,
            "running": False,
            "message": (
                f"更新完成：直抓民调 {poll_stat.get('direct_polls', 0)} 份、"
                f"覆盖 {poll_stat.get('direct_regions', 0)} 个县市"
                + (f"；部分来源失败：{'、'.join(step_errors)}" if step_errors else "")
                if not poll_stat.get("error") else
                f"更新完成，但民调更新失败：{poll_stat['error']}"
                + (f"；其他来源失败：{'、'.join(step_errors)}" if step_errors else "")
            ),
            "finished_at": db.utcnow_iso(),
            "error": None,
            "step_timings": timings,
            "progress": 100,
            "step": "完成",
            "step_status": "数据、AI胜率与嵌入组件均已更新；分析报道未生成",
        }
    except Exception as exc:  # noqa: BLE001 - 背景工作需吞掉例外並回報
        _job = {
            **_job,
            "running": False,
            "message": f"更新失敗：{exc}",
            "finished_at": db.utcnow_iso(),
            "error": str(exc),
            "progress": 100,
            "step": "失败",
            "step_status": str(exc),
        }


def start_job(options: dict[str, Any]) -> bool:
    global _job
    with _lock:
        if _job.get("running"):
            return False
        _job = {
            "running": True,
            "message": "正在更新民调、赔率、新闻并执行分析……",
            "started_at": db.utcnow_iso(),
            "finished_at": None,
            "error": None,
            "progress": 0,
            "step": "准备",
            "step_status": "正在启动更新任务",
        }
        thread = threading.Thread(target=_run_job, args=(options,), daemon=True)
        thread.start()
        return True


def _row_regions(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return [str(item) for item in value or [] if item]


def _run_news_ai_job(options: dict[str, Any]) -> None:
    global _ai_job
    try:
        settings = load_settings()
        if not settings.llm_api_key:
            raise RuntimeError("尚未配置 AI API Key")
        limit = min(100, max(1, int(options.get("limit", 30))))
        start_date = str(options.get("start") or "")
        end_date = str(options.get("end") or "")
        hours = max(0, int(options.get("hours") or 0))
        region = str(options.get("region") or "")
        force = bool(options.get("force"))
        rows, total = db.fetch_news_page(
            limit=5000,
            offset=0,
            start_date=start_date,
            end_date=end_date,
            hours=hours,
        )
        selected: list[dict[str, Any]] = []
        want = to_simplified(region) if region else ""
        for row in rows:
            data = dict(row)
            if int(data.get("is_rep", 1) or 0) != 1:
                continue
            regions = _row_regions(data.get("regions"))
            if want and want not in {to_simplified(name) for name in regions}:
                continue
            selected.append(data)
            if len(selected) >= limit:
                break
        if not selected:
            raise RuntimeError("所选日期和县市没有待分析的代表新闻")
        _ai_job["message"] = f"正在分析 {len(selected)} 个新闻事件……"

        client = LLMClient(
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout,
        )
        snapshots = build_snapshots(
            db.fetch_latest_odds(),
            name_map=settings.name_map,
            region_names=settings.region_names(),
        )

        def progress(done: int, count: int) -> None:
            _ai_job["message"] = f"已完成 {done}/{count} 个新闻事件"

        report = newsai.analyze_rows(
            settings,
            client,
            selected,
            snapshots,
            batch_size=4,
            workers=2,
            force=force,
            progress=progress,
        )
        report["filters"] = {
            "start": start_date,
            "end": end_date,
            "region": region,
            "hours": hours,
            "candidate_events": total,
            "selected_events": len(selected),
            "limit": limit,
        }
        report["forecast_updated"] = False
        AI_REPORT.parent.mkdir(parents=True, exist_ok=True)
        AI_REPORT.write_text(
            json.dumps(_out(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            _ai_job["message"] = "AI 分析完成，正在刷新融合预测……"
            payload = pipeline.run_forecast(settings, window_days=30)
            pipeline.write_outputs(
                settings,
                payload,
                embeds=False,
                widget=False,
            )
            report["forecast_updated"] = True
            AI_REPORT.write_text(
                json.dumps(_out(report), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - 预测刷新失败不回滚分析结果
            report["forecast_error"] = f"{type(exc).__name__}: {exc}"
        _ai_job = {
            **_ai_job,
            "running": False,
            "message": (
                f"AI 分析完成：{report.get('count', 0)} 个事件，"
                f"{len(report.get('hot_topics') or [])} 个热门议题"
            ),
            "finished_at": db.utcnow_iso(),
            "error": None,
            "report": report,
        }
    except Exception as exc:  # noqa: BLE001
        _ai_job = {
            **_ai_job,
            "running": False,
            "message": f"AI 分析失败：{exc}",
            "finished_at": db.utcnow_iso(),
            "error": str(exc),
        }


def start_news_ai_job(options: dict[str, Any]) -> bool:
    global _ai_job
    with _ai_lock:
        if _ai_job.get("running"):
            return False
        _ai_job = {
            "running": True,
            "message": "正在准备新闻与正文……",
            "started_at": db.utcnow_iso(),
            "finished_at": None,
            "error": None,
        }
        thread = threading.Thread(target=_run_news_ai_job, args=(options,), daemon=True)
        thread.start()
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "TEM/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 覆寫標準介面
        if "/api/" in self.path:
            return
        super().log_message(fmt, *args)

    # ---------- helpers ----------
    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(_out(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_download(
        self,
        body: bytes,
        filename: str,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path) -> None:
        if not path.exists():
            self.send_error(404, "Not Found")
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".html":
            ctype = "text/html"
        if ctype.startswith("text/") or path.suffix in (".js", ".css", ".json"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(data)

    # ---------- routes ----------
    def do_GET(self) -> None:  # noqa: N802 - 標準介面
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            self._send_file(STATIC_DIR / "index.html")
            return

        if route == "/api/state":
            self._send_json(build_state())
            return

        if route == "/api/news/ai":
            report = None
            try:
                report = json.loads(AI_REPORT.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            self._send_json({"job": dict(_ai_job), "report": report})
            return

        if route == "/api/observe/status":
            try:
                self._send_json(observe.status(load_settings()))
            except Exception as exc:  # noqa: BLE001 - 状态接口不能拖垮主页面
                self._send_json(
                    {"ok": False, "source": observe.SOURCE_LABEL, "error": str(exc)},
                    200,
                )
            return

        if route == "/api/observe/insights":
            try:
                hours = max(1, int((query.get("hours") or ["720"])[0]))
                offset = max(0, int((query.get("offset") or ["0"])[0]))
                limit = min(30, max(1, int((query.get("limit") or ["12"])[0])))
                candidate_filter = (query.get("candidate") or [""])[0]
                sort = (query.get("sort") or ["relevance"])[0]
                graph_only = (query.get("graph_only") or [""])[0] == "1"
                self._send_json(
                    observe.get_insights(
                        load_settings(),
                        hours=hours,
                        offset=offset,
                        limit=limit,
                        candidate_filter=candidate_filter,
                        sort=sort,
                        graph_only=graph_only,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 外部补充数据失败时返回结构化降级
                self._send_json({
                    "schema_version": 1,
                    "ok": False,
                    "source": observe.SOURCE_LABEL,
                    "threads": [],
                    "trends": [],
                    "event_pagination": {
                        "offset": 0,
                        "limit": 12,
                        "returned": 0,
                        "loaded": 0,
                        "total": 0,
                        "remaining": 0,
                        "has_more": False,
                        "read_events": 0,
                        "filtered_out": 0,
                        "raw_records": 0,
                        "folded_records": 0,
                        "duplicate_records": 0,
                        "candidate": "",
                        "sort": "relevance",
                        "all_election_events": 0,
                    },
                    "event_facets": {
                        "candidates": [],
                        "selected_candidate": "",
                        "sort": "relevance",
                        "all_election_events": 0,
                    },
                    "counts": {"threads": 0, "trends": 0, "thread_articles": 0},
                    "warnings": [f"{type(exc).__name__}: {exc}"],
                })
            return

        if route == "/api/observe/export":
            try:
                hours = max(1, int((query.get("hours") or ["720"])[0]))
                candidate_filter = (query.get("candidate") or [""])[0]
                sort = (query.get("sort") or ["relevance"])[0]
                report = observe.get_insights(
                    load_settings(),
                    hours=hours,
                    offset=0,
                    limit=2000,
                    candidate_filter=candidate_filter,
                    sort=sort,
                )
                exported_at = time.strftime("%Y-%m-%d %H:%M:%S")
                items: list[dict[str, Any]] = []
                for thread in report.get("threads") or []:
                    if not isinstance(thread, dict):
                        continue
                    coverage = thread.get("coverage") or {}
                    camps = coverage.get("camps") or {}
                    share = coverage.get("share") or {}
                    news_rows = [
                        item for item in (thread.get("news") or [])
                        if isinstance(item, dict)
                    ]
                    news_rows.sort(
                        key=lambda item: str(item.get("published_at") or "")
                    )
                    event_title = (
                        str((news_rows[0] or {}).get("title") or "").strip()
                        if news_rows
                        else ""
                    ) or str(thread.get("title") or "").strip() or "未命名事件"
                    items.append({
                        "事件编号": thread.get("id") or "",
                        "事件标题": event_title,
                        "首次报道": thread.get("first_time") or "",
                        "最近更新": thread.get("last_time") or "",
                        "持续小时": thread.get("duration_hours")
                        if thread.get("duration_hours") is not None
                        else thread.get("hours") or "",
                        "最高事件热度排名": thread.get("best_rank") or "",
                        "标签": " / ".join(
                            str(value) for value in thread.get("tags") or [] if value
                        ),
                        "关联候选人": " / ".join(
                            str(value) for value in thread.get("candidates") or [] if value
                        ),
                        "关联报道数": coverage.get("articles") or 0,
                        "媒体数": coverage.get("media_count") or 0,
                        "蓝营媒体数": camps.get("blue") or 0,
                        "绿营媒体数": camps.get("green") or 0,
                        "其他媒体数": camps.get("other") or 0,
                        "蓝营报道份额": share.get("blue"),
                        "绿营报道份额": share.get("green"),
                        "代表报道日期": "\n".join(
                            str(item.get("published_at") or "") for item in news_rows[:12]
                        ),
                        "代表报道媒体": "\n".join(
                            str(item.get("media_title") or item.get("media") or "")
                            for item in news_rows[:12]
                        ),
                        "代表报道标题": "\n".join(
                            str(item.get("title") or "") for item in news_rows[:12]
                        ),
                        "代表报道链接": "\n".join(
                            str(item.get("url") or "") for item in news_rows[:12]
                        ),
                        "筛选时长小时": hours,
                        "候选人筛选": candidate_filter or "全部候选人",
                        "排序": sort,
                        "数据源": report.get("source") or observe.SOURCE_LABEL,
                        "导出时间": exported_at,
                    })
                if not items:
                    raise ValueError("当前筛选条件下没有可导出的事件")
                stream = io.StringIO()
                writer = csv.DictWriter(
                    stream,
                    fieldnames=list(items[0].keys()),
                    extrasaction="ignore",
                    lineterminator="\r\n",
                )
                writer.writeheader()
                writer.writerows(_out(items))
                body = ("\ufeff" + stream.getvalue()).encode("utf-8")
                stamp = time.strftime("%Y%m%d-%H%M%S")
                self._send_download(
                    body,
                    f"tem-events-{stamp}.csv",
                    "text/csv; charset=utf-8",
                    {"X-TEM-Export-Count": str(len(items))},
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if route == "/api/observe/news-dates":
            try:
                urls = query.get("url") or []
                self._send_json({"dates": observe.article_dates(urls)})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"dates": {}, "error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if route == "/api/observe/thread-detail":
            try:
                thread_id = int((query.get("id") or ["0"])[0])
                self._send_json(observe.get_thread_detail(load_settings(), thread_id))
            except Exception as exc:  # noqa: BLE001 - 事件详情失败不阻断基础事件列表
                self._send_json({
                    "schema_version": 1,
                    "ok": False,
                    "id": (query.get("id") or [""])[0],
                    "source": observe.SOURCE_LABEL,
                    "points": [],
                    "coverage": None,
                    "news": [],
                    "warnings": [f"{type(exc).__name__}: {exc}"],
                })
            return

        if route == "/api/observe/graph-news":
            try:
                source = (query.get("source") or [""])[0]
                target = (query.get("target") or [""])[0]
                limit = min(20, max(1, int((query.get("limit") or ["8"])[0])))
                self._send_json(
                    observe.related_news(
                        load_settings(),
                        source,
                        target,
                        limit=limit,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "news": [],
                    },
                    500,
                )
            return

        if route == "/api/topic-graph":
            try:
                hours = max(1, int((query.get("hours") or ["720"])[0]))
                self._send_json(topic_graph.build_local_graph(hours=hours))
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {
                        "ok": False,
                        "source": "本地新闻库",
                        "graph": {"nodes": [], "edges": [], "groups": []},
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    500,
                )
            return

        if route == "/api/topic-graph/news":
            try:
                hours = max(1, int((query.get("hours") or ["720"])[0]))
                source = (query.get("source") or [""])[0]
                target = (query.get("target") or [""])[0]
                limit = min(20, max(1, int((query.get("limit") or ["8"])[0])))
                self._send_json(
                    topic_graph.related_news(
                        source,
                        target,
                        hours=hours,
                        limit=limit,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "news": [],
                    },
                    500,
                )
            return

        if route == "/api/ptt/opinion":
            try:
                days = int((query.get("days") or ["1"])[0])
                force = (query.get("refresh") or [""])[0] == "1"
                self._send_json(ptt_opinion.get_opinion(days=days, force=force))
            except Exception as exc:  # noqa: BLE001 - PTT 失败不阻断主页面
                self._send_json(
                    {
                        "schema_version": ptt_opinion.SCHEMA_VERSION,
                        "ok": False,
                        "source": "PTT",
                        "error": f"{type(exc).__name__}: {exc}",
                        "candidates": [],
                        "topics": [],
                        "posts": [],
                    },
                    200,
                )
            return

        if route == "/api/youtube/opinion":
            try:
                days = int((query.get("days") or ["1"])[0])
                force = (query.get("refresh") or [""])[0] == "1"
                self._send_json(youtube_opinion.get_opinion(days=days, force=force))
            except Exception as exc:  # noqa: BLE001 - YouTube 失败不影响其他舆情
                self._send_json(
                    {
                        "schema_version": youtube_opinion.SCHEMA_VERSION,
                        "ok": False,
                        "source": "YouTube",
                        "error": f"{type(exc).__name__}: {exc}",
                        "candidates": [],
                        "topics": [],
                        "videos": [],
                        "counts": {},
                    },
                    200,
                )
            return

        if route == "/api/social/tikhub":
            platform = (query.get("platform") or ["threads"])[0]
            hours = int((query.get("hours") or ["24"])[0])
            force = (query.get("refresh") or [""])[0] == "1"
            extend = (query.get("extend") or [""])[0] == "1"
            self._send_json(
                tikhub_social.get_social(
                    platform=platform,
                    hours=hours,
                    force=force,
                    extend=extend,
                )
            )
            return

        if route == "/api/social/tikhub/comments":
            platform = (query.get("platform") or ["threads"])[0]
            post_id = (query.get("post_id") or [""])[0]
            try:
                self._send_json(
                    tikhub_social.get_post_comments(platform, post_id)
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {
                        "ok": False,
                        "platform": platform,
                        "post_id": post_id,
                        "error": f"{type(exc).__name__}: {exc}",
                        "comments": [],
                    },
                    200,
                )
            return

        if route == "/api/youtube/comments":
            from . import youtube_official

            video_id = (query.get("video_id") or query.get("videoid") or [""])[0]
            days = int((query.get("days") or ["1"])[0])
            days = days if days in youtube_opinion.ALLOWED_DAYS else 1
            try:
                group, warnings = youtube_official.video_comments(
                    video_id,
                    days,
                    per_video=50,
                )
                self._send_json(
                    {
                        "ok": True,
                        "source_label": "YouTube Data API v3（官方）",
                        "video_id": video_id,
                        "group": group,
                        "warnings": warnings,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {
                        "ok": False,
                        "video_id": video_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    200,
                )
            return

        if route == "/api/statements":
            try:
                days = int((query.get("days") or ["90"])[0])
                candidate = (query.get("candidate") or [""])[0]
                topic = (query.get("topic") or [""])[0]
                self._send_json(
                    statements.get_payload(
                        load_settings(),
                        days=min(365, max(1, days)),
                        candidate=candidate,
                        topic=topic,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {
                        "schema_version": 2,
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "statements": [],
                        "pairs": [],
                        "candidate_facets": [],
                        "topic_facets": [],
                        "counts": {},
                    },
                    200,
                )
            return

        if route in ("/api/opinion", "/api/opinion-v2"):
            try:
                start_date = (query.get("start") or [""])[0]
                end_date = (query.get("end") or [""])[0]
                hours = max(0, int((query.get("hours") or ["0"])[0]))
                rows, total = db.fetch_news_page(
                    limit=5000,
                    offset=0,
                    start_date=start_date,
                    end_date=end_date,
                    hours=hours,
                )
                selected: list[dict[str, Any]] = []
                rule_rows: list[dict[str, Any]] = []
                analyses: dict[str, dict[str, Any]] = {}
                topic_counts: dict[str, int] = {}
                for row in rows:
                    item = dict(row)
                    if int(item.get("is_rep", 1) or 0) != 1:
                        continue
                    selected.append(item)
                    for key in ("regions", "candidates", "topics"):
                        if isinstance(item.get(key), str):
                            try:
                                item[key] = json.loads(item[key])
                            except json.JSONDecodeError:
                                item[key] = []
                    analyses[str(item["id"])] = {
                        "regions": item.get("regions") or [],
                        "candidates": item.get("candidates") or [],
                        "topics": item.get("topics") or [],
                        "sentiment": item.get("sentiment") or 0,
                        "impact": item.get("impact") or 0,
                    }
                    if item.get("sentiment") is None:
                        rule_rows.append(item)
                    for topic in item.get("topics") or []:
                        name = topic.get("topic") if isinstance(topic, dict) else str(topic)
                        if name:
                            topic_counts[name] = topic_counts.get(name, 0) + 1
                settings = load_settings()
                snapshots = build_snapshots(
                    db.fetch_latest_odds(),
                    name_map=settings.name_map,
                    region_names=settings.region_names(),
                )
                from .analyze import aggregate_regions, analyze_news, candidate_sentiment_index

                if rule_rows:
                    try:
                        rule_results, _ = analyze_news(
                            settings,
                            rule_rows,
                            snapshots,
                            use_llm=False,
                            verbose=False,
                        )
                        db.save_analyses(rule_results)
                        for result in rule_results:
                            analyses[str(result["news_id"])] = {
                                "regions": result.get("regions") or [],
                                "candidates": result.get("candidates") or [],
                                "topics": result.get("topics") or [],
                                "sentiment": result.get("sentiment") or 0,
                                "impact": result.get("impact") or 0,
                            }
                    except Exception as exc:  # noqa: BLE001
                        print(f"[opinion] rule fallback failed: {type(exc).__name__}: {exc}")

                states = aggregate_regions(settings, selected, analyses, snapshots)
                raw_candidates = candidate_sentiment_index(selected, analyses)
                from .forecast import candidate_news_sentiment_scores

                final_sentiment = candidate_news_sentiment_scores(
                    snapshots, raw_candidates, states
                )
                candidates = [
                    {
                        "name_en": name,
                        "name": settings.name_map.get(name, name),
                        "mentions": value.get("mentions") or 0,
                        "articles": value.get("articles") or 0,
                        "positive_articles": value.get("positive_articles") or 0,
                        "negative_articles": value.get("negative_articles") or 0,
                        "news_sentiment": final_sentiment.get(name),
                        "sentiment": value.get("sentiment") or 0,
                    }
                    for name, value in raw_candidates.items()
                ]
                candidates.sort(key=lambda item: (-item["mentions"], -item["articles"]))
                topics = [
                    {"topic": name, "count": count}
                    for name, count in sorted(topic_counts.items(), key=lambda pair: -pair[1])
                ]
                baseline_selected: list[dict[str, Any]] = []
                if hours > 0:
                    baseline_rows = db.fetch_news_hours_window(
                        from_hours_ago=hours * 2,
                        to_hours_ago=hours,
                    )
                else:
                    baseline_rows = []
                    try:
                        start_day = date.fromisoformat(start_date)
                        end_day = date.fromisoformat(end_date)
                        if end_day < start_day:
                            start_day, end_day = end_day, start_day
                        day_count = (end_day - start_day).days + 1
                        baseline_end = start_day - timedelta(days=1)
                        baseline_start = baseline_end - timedelta(days=day_count - 1)
                        baseline_rows, _ = db.fetch_news_page(
                            limit=5000,
                            offset=0,
                            start_date=baseline_start.isoformat(),
                            end_date=baseline_end.isoformat(),
                        )
                    except ValueError:
                        baseline_rows = []

                for row in baseline_rows:
                    item = dict(row)
                    if int(item.get("is_rep", 1) or 0) != 1:
                        continue
                    for key in ("regions", "candidates", "topics"):
                        if isinstance(item.get(key), str):
                            try:
                                item[key] = json.loads(item[key])
                            except json.JSONDecodeError:
                                item[key] = []
                    baseline_selected.append(item)

                hot_word_rows = hotwords.extract(
                    selected,
                    settings,
                    baseline_rows=baseline_selected,
                    limit=100,
                )
                issue_rows = hotissues.extract(selected, limit=60)
                self._send_json({
                    "schema_version": 2,
                    "article_count": len(selected),
                    "total": total,
                    "hours": hours,
                    "start": start_date,
                    "end": end_date,
                    "region_states": states,
                    "candidates": candidates[:80],
                    "topics": topics[:30],
                    "hot_words": hot_word_rows,
                    "hot_issues": issue_rows,
                    "hot_word_baseline_articles": len(baseline_selected),
                    "rule_analyzed_count": len(rule_rows),
                })
            except Exception as exc:  # noqa: BLE001
                self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if route == "/api/news/export":
            export_format = (query.get("format") or ["csv"])[0].lower()
            if export_format not in ("csv", "json"):
                export_format = "csv"
            region = (query.get("region") or [""])[0]
            start_date = (query.get("start") or [""])[0]
            end_date = (query.get("end") or [""])[0]
            hours = max(0, int((query.get("hours") or ["0"])[0]))
            search_q = (query.get("q") or [""])[0].strip()[:100]
            rows, _ = db.fetch_news_page(
                limit=50000,
                offset=0,
                start_date=start_date,
                end_date=end_date,
                hours=hours,
                query_text="",
            )

            def labels(value: Any) -> list[str]:
                if isinstance(value, str):
                    try:
                        value = json.loads(value)
                    except json.JSONDecodeError:
                        value = [value] if value else []
                out: list[str] = []
                for item in value or []:
                    if isinstance(item, dict):
                        label = item.get("name_zh") or item.get("name") or item.get("candidate_zh")
                    else:
                        label = str(item)
                    if label and label not in out:
                        out.append(str(label))
                return out

            want = to_simplified(region) if region else ""
            selected: list[dict[str, Any]] = []
            for row in rows:
                item = {k: row[k] for k in row.keys()}
                item_regions = labels(item.get("regions"))
                if want and want not in {to_simplified(x) for x in item_regions}:
                    continue
                if search_q:
                    haystack = to_simplified(
                        " ".join(
                            str(item.get(key) or "")
                            for key in ("title", "summary", "ai_summary", "source_name")
                        )
                        + " "
                        + " ".join(labels(item.get("candidates")))
                        + " "
                        + " ".join(labels(item.get("topics")))
                    ).casefold()
                    if to_simplified(search_q).casefold() not in haystack:
                        continue
                selected.append({
                    **item,
                    "_regions": item_regions,
                    "_candidates": labels(item.get("candidates")),
                    "_topics": labels(item.get("topics")),
                })
            body_results = newstext.bodies_for(selected, workers=6)
            items: list[dict[str, Any]] = []
            for item, (body, body_status) in zip(selected, body_results):
                items.append({
                    "发布时间": item.get("published_at") or item.get("first_seen") or "",
                    "来源": item.get("source_name") or "",
                    "标题": item.get("title") or "",
                    "链接": item.get("url") or "",
                    "情感": item.get("sentiment"),
                    "影响": item.get("impact"),
                    "关联县市": " / ".join(item["_regions"]),
                    "关联候选人": " / ".join(item["_candidates"]),
                    "主题": " / ".join(item["_topics"]),
                    "摘要": item.get("summary") or "",
                    "AI摘要": item.get("ai_summary") or "",
                    "正文": body,
                    "正文状态": body_status,
                    "媒体数": item.get("cluster_size") or 1,
                })
            stamp = time.strftime("%Y%m%d-%H%M%S")
            if export_format == "json":
                payload = _out({
                    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "filters": {
                        "start": start_date,
                        "end": end_date,
                        "hours": hours,
                        "region": region,
                        "q": search_q,
                    },
                    "count": len(items),
                    "news": items,
                })
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
                self._send_download(
                    body,
                    f"tem-news-{stamp}.json",
                    "application/json; charset=utf-8",
                )
            else:
                stream = io.StringIO()
                fields = [
                    "发布时间", "来源", "标题", "链接", "情感", "影响",
                    "关联县市", "关联候选人", "主题", "摘要", "AI摘要",
                    "正文", "正文状态", "媒体数",
                ]
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for item in _out(items):
                    writer.writerow(item)
                body = ("\ufeff" + stream.getvalue()).encode("utf-8")
                self._send_download(
                    body,
                    f"tem-news-{stamp}.csv",
                    "text/csv; charset=utf-8",
                )
            return

        if route == "/api/news":
            limit = min(500, max(1, int((query.get("limit") or ["100"])[0])))
            offset = max(0, int((query.get("offset") or ["0"])[0]))
            region = (query.get("region") or [""])[0]
            start_date = (query.get("start") or [""])[0]
            end_date = (query.get("end") or [""])[0]
            hours = max(0, int((query.get("hours") or ["0"])[0]))
            search_q = (query.get("q") or [""])[0].strip()[:100]
            media_filter = (query.get("media") or [""])[0].strip()[:120]
            lean_filter = (query.get("lean") or [""])[0].strip().casefold()
            want = to_simplified(region) if region else ""
            if want or search_q or media_filter or lean_filter:
                # 縣市存放在 JSON 欄位中，先按日期取出候選集合，再在輸出層過濾。
                rows, _ = db.fetch_news_page(
                    limit=5000,
                    offset=0,
                    start_date=start_date,
                    end_date=end_date,
                    hours=hours,
                    query_text="",
                )
            else:
                rows, total = db.fetch_news_page(
                    limit=limit,
                    offset=offset,
                    start_date=start_date,
                    end_date=end_date,
                    hours=hours,
                    query_text=search_q,
                )
            out = []
            for row in rows:
                item = {k: row[k] for k in row.keys()}
                for key in ("regions", "candidates", "topics"):
                    if isinstance(item.get(key), str):
                        try:
                            item[key] = json.loads(item[key])
                        except json.JSONDecodeError:
                            item[key] = []
                source_meta = media.classify_source(str(item.get("source_name") or ""))
                item["media_lean"] = source_meta["camp"]
                item["media_lean_label"] = source_meta["label"]
                if want:
                    # 資料庫存的是繁體縣市名，輸出層才轉簡體，比對前要先統一
                    names = {to_simplified(x) for x in (item.get("regions") or [])}
                    if want not in names:
                        continue
                if search_q:
                    haystack = to_simplified(
                        " ".join(
                            str(item.get(key) or "")
                            for key in ("title", "summary", "ai_summary", "source_name")
                        )
                        + " "
                        + str(item.get("candidates") or "")
                        + " "
                        + str(item.get("topics") or "")
                    ).casefold()
                    if to_simplified(search_q).casefold() not in haystack:
                        continue
                if media_filter and to_simplified(
                    str(item.get("source_name") or "")
                ) != to_simplified(media_filter):
                    continue
                if lean_filter and item["media_lean"] != lean_filter:
                    continue
                out.append(item)
            if want or search_q or media_filter or lean_filter:
                total = len(out)
                out = out[offset:offset + limit]
            self._send_json({
                "news": out,
                "region": region,
                "count": len(out),
                "total": total,
                "offset": offset,
                "has_more": offset + len(out) < total,
                "start": start_date,
                "end": end_date,
                "hours": hours,
                "q": search_q,
            })
            return

        if route == "/api/google-trends/daily":
            force = (query.get("refresh") or [""])[0] == "1"
            date = (query.get("date") or [""])[0]
            self._send_json(
                google_trends.daily_election_trends(
                    force=force,
                    date=date,
                )
            )
            return

        if route == "/api/polls":
            self._send_json(poll_data_payload())
            return

        if route == "/api/market-movers":
            force = (query.get("refresh") or [""])[0] == "1"
            candidate_key = (query.get("candidate") or [""])[0]
            region_code = (query.get("region") or [""])[0]
            try:
                window_hours = int((query.get("window") or ["720"])[0])
            except ValueError:
                window_hours = 720
            self._send_json(
                market_movers.get_market_movers(
                    force=force,
                    candidate_key=candidate_key,
                    region_code=region_code,
                    window_hours=window_hours,
                )
            )
            return

        if route == "/api/focus":
            region_code = (query.get("region") or [""])[0]
            self._send_json(focus_payload(region_code))
            return

        if route == "/api/wiki-person":
            name = (query.get("name") or [""])[0].strip()[:40]
            region_name = (query.get("region") or [""])[0].strip()[:30]
            party_name = (query.get("party") or [""])[0].strip()[:30]
            wiki_title = urllib.parse.unquote(
                (query.get("title") or [""])[0].strip()[:160]
            )[:80]
            if not name:
                self._send_json({"found": False, "error": "缺少候选人姓名"}, 400)
                return
            from .profile import search_council_profile

            try:
                profile = search_council_profile(
                    name,
                    region_name,
                    party_name,
                    wiki_title,
                    allow_network=True,
                )
                self._send_json({"found": bool(profile), "profile": profile or {}})
            except Exception as exc:  # noqa: BLE001 - 网络失败时返回可显示结果
                self._send_json(
                    {"found": False, "error": f"维基百科查询失败：{exc}"},
                    503,
                )
            return

        if route == "/api/candidate":
            region = (query.get("region") or [""])[0]
            candidate = (query.get("candidate") or [""])[0]
            region_name = (query.get("region_name") or [""])[0]
            candidate_zh = (query.get("zh") or [""])[0]
            self._send_json(
                self._candidate_detail(
                    region,
                    candidate,
                    region_name=region_name,
                    candidate_zh=candidate_zh,
                )
            )
            return

        if route == "/api/widget-data":
            limit = int((query.get("limit") or ["80"])[0])
            self._send_json({"items": widget_items(limit=limit), "live": True})
            return

        if route == "/api/report":
            from .report import load_latest_report

            payload = load_latest_report()
            if payload is None:
                self._send_json({"error": "尚無報告，請先執行一次完整流程"}, 404)
            else:
                self._send_json(payload)
            return

        # 靜態檔案
        if route.startswith("/exports/"):
            from .paths import EXPORT_DIR

            rel = route[len("/exports/"):]
            if rel and ".." not in rel:
                self._send_file(EXPORT_DIR / rel)
                return

        rel = route.lstrip("/")
        if rel and ".." not in rel and not route.startswith("/api/"):
            self._send_file(STATIC_DIR / rel)
            return

        if route.startswith("/api/"):
            self._send_json({"error": "API endpoint not found"}, 404)
        else:
            self.send_error(404, "Not Found")

    def _candidate_detail(
        self,
        region: str,
        candidate: str,
        *,
        region_name: str = "",
        candidate_zh: str = "",
    ) -> dict[str, Any]:
        if not region or not candidate:
            return {"error": "缺少 region 或 candidate 參數"}
        if not region_name:
            region_name = load_settings().region_names().get(region, region)
        roster = load_roster()
        roster_entry = entry_for(roster, region_name, candidate_zh or candidate, candidate)
        rows = db.fetch_odds_for_candidate(region, candidate)
        latest = rows[-1] if rows else None
        name_zh = (
            (latest["candidate_zh"] if latest else "")
            or candidate_zh
            or (roster_entry or {}).get("name")
            or candidate
        )
        event_slug = (latest["event_slug"] if latest else "") or ""
        market_id = (latest["market_id"] if latest else "") or ""
        market_slug = (latest["market_slug"] if latest else "") or ""
        if not market_slug and event_slug and market_id:
            try:
                from .polymarket import market_slug_for_id

                market_slug = market_slug_for_id(event_slug, market_id)
            except Exception:  # noqa: BLE001 - 嵌入标识补齐失败不应影响个人资料
                market_slug = ""
        profile = None
        try:
            from .profile import resolve_profile

            profile = resolve_profile(
                (latest["region_name"] if latest else "") or region_name,
                name_zh,
                candidate,
                allow_network=bool(latest),
            )
        except Exception:  # noqa: BLE001 - 个人资料抓不到不影响候选人详情
            profile = None
        poll = None
        signals: dict[str, Any] = {}
        try:
            from .forecast import candidate_signals

            signals = candidate_signals(
                (latest["region_name"] if latest else "") or region_name,
                name_zh,
                candidate,
                (latest["market_id"] if latest else None),
            )
            poll = signals.get("poll")
        except Exception:  # noqa: BLE001 - 外部信号抓不到不影响候选人详情
            signals = {}
            poll = None
        return {
            "region": region,
            "candidate_en": candidate,
            "image_url": (latest["image_url"] if latest else "") or "",
            "market_id": market_id,
            "market_slug": market_slug,
            "event_slug": event_slug,
            "candidate_zh": name_zh,
            "region_name": (latest["region_name"] if latest else "") or region_name,
            "roster_number": (roster_entry or {}).get("number"),
            "roster_party": (roster_entry or {}).get("party") or "",
            "roster_experience": (roster_entry or {}).get("experience") or "",
            "official_candidate": bool(roster_entry),
            "meta_volume": (latest["volume"] if latest else None),
            "yes_price": (latest["yes_price"] if latest else None),
            "chg_1d": (latest["chg_1d"] if latest else None),
            "profile": profile,
            "poll": poll,
            "attention": signals.get("attention") or {},
            "depth": signals.get("depth") or {},
            "model": signals.get("model") or {},
        }

    def do_POST(self) -> None:  # noqa: N802 - 標準介面
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            options = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            options = {}

        if parsed.path == "/api/settings/llm":
            try:
                current = load_settings()
                base_url = str(options.get("base_url") or current.llm_base_url).strip().rstrip("/")
                model = str(options.get("model") or current.llm_model).strip()
                api_key = str(options.get("api_key") or "").strip() or current.llm_api_key
                if not base_url.startswith(("http://", "https://")):
                    raise ValueError("Base URL 必须以 http:// 或 https:// 开头")
                if not model:
                    raise ValueError("模型名称不能为空")
                if not api_key:
                    raise ValueError("API Key 不能为空")
                save_env_values({
                    "TEM_LLM_ENABLED": "true",
                    "TEM_LLM_BASE_URL": base_url,
                    "TEM_LLM_MODEL": model,
                    "TEM_LLM_API_KEY": api_key,
                })
                self._send_json({
                    "ok": True,
                    "base_url": base_url,
                    "model": model,
                    "api_key_set": True,
                    "api_key_hint": f"••••{api_key[-4:]}",
                })
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return

        if parsed.path == "/api/settings/llm/test":
            try:
                settings = load_settings()
                if not settings.llm_api_key:
                    raise RuntimeError("尚未配置 API Key")
                client = LLMClient(
                    settings.llm_base_url,
                    settings.llm_api_key,
                    settings.llm_model,
                    min(settings.llm_timeout, 60),
                )
                result = client.complete_json(
                    "只输出 JSON。",
                    '返回 {"ok": true, "message": "连接成功"}',
                    temperature=0,
                    max_tokens=60,
                    json_mode=True,
                )
                self._send_json({
                    "ok": bool(result.get("ok")),
                    "model": settings.llm_model,
                    "message": result.get("message") or "连接成功",
                })
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 502)
            return

        if parsed.path == "/api/news/ai":
            started = start_news_ai_job(options)
            self._send_json(
                {"started": started, "job": dict(_ai_job)},
                status=202 if started else 409,
            )
            return

        if parsed.path == "/api/brief":
            # 即時生成分析報道（吃當前資料庫狀態），完成後由前端開新視窗
            try:
                from .briefing import generate

                days = int(options.get("days") or 21)
                paths = generate(days=days)
                self._send_json({
                    "ok": True,
                    "url": "/exports/report.html?t=" + str(int(time.time())),
                    **paths,
                })
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if parsed.path == "/api/statements/refresh":
            try:
                result = statements.refresh(
                    days=min(365, max(1, int(options.get("days") or 30))),
                    limit=min(500, max(20, int(options.get("limit") or 80))),
                    reset=bool(options.get("reset")),
                )
                self._send_json({"ok": True, **result})
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if parsed.path == "/api/statements/review":
            try:
                result = statements.review_pair(
                    load_settings(),
                    str(options.get("older_id") or ""),
                    str(options.get("newer_id") or ""),
                )
                self._send_json(result)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if parsed.path == "/api/refresh":
            started = start_job(options)
            self._send_json(
                {"started": started, "job": dict(_job)},
                status=202 if started else 409,
            )
            return

        if parsed.path.startswith("/api/"):
            self._send_json({"error": "API endpoint not found"}, 404)
        else:
            self.send_error(404, "Not Found")


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    open_browser: bool = True,
    strict: bool = False,
) -> None:
    db.init_db()
    httpd, actual_port = create_server(
        host, port, search=1 if strict else 30, allow_fallback=not strict
    )
    display_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{display_host}:{actual_port}/"
    if port and actual_port != port:
        print(f"[info] 連接埠 {port} 已被占用，已自動改用 {actual_port}")
    print(f"Dashboard 已啟動：{url}")
    print("按 Ctrl+C 結束")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


class TemHTTPServer(ThreadingHTTPServer):
    """關閉 SO_REUSEADDR：在 Windows 上它會讓已被占用的埠仍然綁定成功，
    導致「自動選埠」失效。關閉後占用中的埠會直接拋出 OSError，才能正確跳過。"""

    allow_reuse_address = False
    daemon_threads = True


def _try_bind(host: str, port: int) -> TemHTTPServer | None:
    try:
        return TemHTTPServer((host, port), Handler)
    except OSError:
        return None


def create_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    *,
    search: int = 30,
    allow_fallback: bool = True,
) -> tuple[TemHTTPServer, int]:
    """建立伺服器，埠被占用時自動往後找，最後退回由系統隨機指派。

    port=0 代表直接請作業系統指派一個空閒埠。
    """
    if port == 0:
        httpd = _try_bind(host, 0)
        if httpd is None:
            raise RuntimeError("無法綁定任何連接埠")
        return httpd, httpd.server_address[1]

    for candidate in range(port, min(port + search, 65536)):
        httpd = _try_bind(host, candidate)
        if httpd is not None:
            return httpd, candidate

    if not allow_fallback:
        raise RuntimeError(f"連接埠 {port} 已被占用（--strict-port 指定不可改用其他埠）")

    httpd = _try_bind(host, 0)
    if httpd is None:
        raise RuntimeError(f"連接埠 {port}–{port + search - 1} 皆被占用，且無法取得系統臨時埠")
    return httpd, httpd.server_address[1]
