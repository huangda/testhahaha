"""端到端流程编排。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable

from . import ai_forecast, analyze, db, forecast, news, polymarket, report
from .config import Settings, load_settings
from .models import build_snapshots
from .paths import CONFIG_DIR, EXPORT_DIR, ensure_dirs


def _log(message: str) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}")


def collect_odds(settings: Settings, *, ts: str | None = None) -> dict[str, Any]:
    ts = ts or db.utcnow_iso()
    rows, events, unmatched = polymarket.collect(settings, ts)
    inserted = db.insert_odds([r.as_dict() for r in rows])
    if unmatched:
        _log(f"Polymarket：{len(unmatched)} 个事件未对应到设置档（可加入 regions.json）")
    _log(f"Polymarket：{len(events)} 个事件、{inserted} 笔候选人市场写入（快照 {ts}）")
    return {"ts": ts, "markets": inserted, "events": len(events), "unmatched": [e.get("slug") for e in unmatched]}


def collect_news(settings: Settings, *, max_age_days: int = 14) -> dict[str, Any]:
    items = news.fetch_all(settings, max_age_days=max_age_days)
    inserted = db.upsert_news([i.as_dict() for i in items])
    _log(f"新闻：抓取 {len(items)} 则、添加 {inserted} 则")
    return {"fetched": len(items), "inserted": inserted}


def _snapshots_from_db(top_n: int = 8):
    rows = db.fetch_latest_odds()
    return build_snapshots(rows, top_n=top_n, name_map=load_settings().name_map, region_names=load_settings().region_names())


def run_analysis(
    settings: Settings,
    *,
    limit: int = 60,
    force: bool = False,
    use_llm: bool = True,
) -> dict[str, Any]:
    snapshots = _snapshots_from_db()
    pending = db.fetch_unanalyzed_news(limit=limit, force=force)
    if not pending:
        _log("分析：没有待处理的新闻")
        return {"analyzed": 0, "engine": "none"}
    results, model = analyze.analyze_news(settings, pending, snapshots, use_llm=use_llm)
    saved = db.save_analyses(results)
    engine = results[0]["engine"] if results else "none"
    _log(f"分析：完成 {saved} 则（引擎 {engine} / {model}）")
    return {"analyzed": saved, "engine": engine, "model": model}


def run_forecast(
    settings: Settings,
    *,
    window_days: int = 30,
    ts: str | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    ts = ts or db.utcnow_iso()
    snapshots = _snapshots_from_db()
    if not snapshots:
        raise RuntimeError("数据库没有赔率数据，请先执行 collect。")

    # 以「事件」為單位聚合輿情，避免同一件事被多家轉載反覆計分
    news_rows = db.fetch_news_since_events(days=window_days)
    analyses = db.fetch_all_analyses()
    states = analyze.aggregate_regions(settings, news_rows, analyses, snapshots)
    cand_sentiment = analyze.candidate_sentiment_index(news_rows, analyses)
    rows, ai_meta = ai_forecast.ai_fuse(
        settings,
        snapshots,
        cand_sentiment,
        states,
        progress_callback=progress_callback,
    )

    db.save_region_state(ts, states)
    db.save_forecasts(ts, rows)
    summaries = forecast.region_summary(rows)
    verdict = analyze.overall_verdict(states, snapshots)

    try:
        from .polls import load_polls

        poll_regions = load_polls().get("regions") or {}
    except Exception:  # noqa: BLE001 - 没有民调档也能产出报告
        poll_regions = {}

    payload = {
        "generated_at": ts,
        "election": settings.election,
        "election_date": settings.election_date,
        "sentiment_weight": settings.sentiment_weight,
        "region_states": states,
        "candidate_sentiment": cand_sentiment,
        "forecasts": rows,
        "forecast_engine": "llm",
        "forecast_ai": ai_meta,
        "region_summaries": summaries,
        "verdict": verdict,
        "polls": poll_regions,
        "markets": {code: snap.as_dict() for code, snap in snapshots.items()},
        "news_window_days": window_days,
    }
    _log(
        f"预测：AI 完成 {len(summaries)} 个选区、{len(rows)} 位候选人的综合胜率"
    )
    return payload


def write_outputs(
    settings: Settings,
    payload: dict[str, Any],
    *,
    news_rows: list[Any] | None = None,
    embeds: bool = True,
    widget: bool = True,
    generate_report: bool = True,
) -> dict[str, str]:
    ensure_dirs()
    snapshots = build_snapshots(db.fetch_latest_odds(), name_map=settings.name_map, region_names=settings.region_names())
    paths: dict[str, str] = {}
    if generate_report:
        news_rows = (
            news_rows
            if news_rows is not None
            else db.fetch_news_since(days=7)
        )
        markdown = report.build_markdown(
            settings,
            generated_at=payload["generated_at"],
            region_summaries=payload["region_summaries"],
            forecast_rows=payload["forecasts"],
            region_states=payload["region_states"],
            candidate_sentiment=payload["candidate_sentiment"],
            news_rows=news_rows,
            verdict=payload["verdict"],
            snapshots=snapshots,
            polls=payload.get("polls") or {},
        )
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        paths = report.write_reports(markdown, payload, stamp)

    if embeds:
        from .embed import build_embeds, build_gallery_html, build_js_widget

        items = build_embeds(snapshots, settings)
        (EXPORT_DIR / "polymarket-embeds.json").write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (EXPORT_DIR / "polymarket-embeds.html").write_text(
            build_gallery_html(items), encoding="utf-8"
        )
        (EXPORT_DIR / "polymarket-widget.js").write_text(
            build_js_widget(items), encoding="utf-8"
        )
        paths["embeds"] = str(EXPORT_DIR / "polymarket-embeds.html")

    if widget:
        try:
            from .popup import MODULE_JS, build_popup_demo
            from .widget import DEMO_HTML, build_widget_js, collect_widget_data

            items = collect_widget_data(
                snapshots,
                settings,
                allow_network=False,
            )
            (EXPORT_DIR / "tem-widget.js").write_text(
                build_widget_js(items, generated_at=payload["generated_at"]), encoding="utf-8"
            )
            (EXPORT_DIR / "tem-widget-demo.html").write_text(DEMO_HTML, encoding="utf-8")
            keyed = EXPORT_DIR / "tem-widget-data.json"
            keyed.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
            (EXPORT_DIR / "tem-candidate-popup.js").write_text(MODULE_JS, encoding="utf-8")
            (EXPORT_DIR / "tem-candidate-popup-demo.html").write_text(
                build_popup_demo(items), encoding="utf-8"
            )
            paths["widget"] = str(EXPORT_DIR / "tem-widget.js")
            paths["popup"] = str(EXPORT_DIR / "tem-candidate-popup.js")
            _log(f"嵌入组件：{len(items)} 位候选人（{paths['widget']}）")
        except Exception as exc:  # noqa: BLE001 - 组件产生失败不应让整条流程失败
            _log(f"嵌入组件产生失败：{exc}")
    if generate_report:
        _log(f"报告：{paths['markdown']}")
    return paths


def run_all(
    settings: Settings,
    *,
    analyze_limit: int = 60,
    force_analyze: bool = False,
    use_llm: bool = True,
    max_age_days: int = 7,
    window_days: int = 7,
    with_market_data: bool = True,
) -> dict[str, Any]:
    _log("=== 开始执行完整流程 ===")
    ts = db.utcnow_iso()
    odds = collect_odds(settings, ts=ts)
    news_stat = collect_news(settings, max_age_days=max_age_days)
    analysis_stat = run_analysis(settings, limit=analyze_limit, force=force_analyze, use_llm=use_llm)
    dedupe_stat: dict[str, Any] = {}
    try:
        from .dedupe import run_dedupe

        dedupe_stat = run_dedupe(days=max(window_days, 30))
        _log(
            f"事件去重：{dedupe_stat['articles']} 则新闻 → {dedupe_stat['events']} 个事件"
            f"（合并 {dedupe_stat['merged_articles']} 则）"
        )
    except Exception as exc:  # noqa: BLE001
        dedupe_stat = {"error": type(exc).__name__}
    market_stat: dict[str, Any] = {}
    if with_market_data:
        market_stat = collect_market_data(settings)
    payload = run_forecast(settings, window_days=window_days, ts=ts)
    payload["stats"] = {
        "odds": odds, "news": news_stat, "analysis": analysis_stat,
        "dedupe": dedupe_stat, **market_stat
    }
    paths = write_outputs(settings, payload)
    # 分析报道放在最後產生，這樣它才吃得到同一輪的最新預測與事件分群
    try:
        from .briefing import generate as generate_brief

        brief = generate_brief(days=max(window_days, 21))
        paths["brief_html"] = brief["html"]
        paths["brief_markdown"] = brief["markdown"]
        _log(f"分析報道：{brief['html']}")
    except Exception as exc:  # noqa: BLE001
        _log(f"分析報道生成失敗：{type(exc).__name__}: {exc}")
    _log("=== 完成 ===")
    return {"payload": payload, "paths": paths}


def refresh_polls(*, days: int = 45, limit: int = 70) -> dict[str, Any]:
    """更新发布方原文，并合并长期民调汇总。"""
    from .polldirect import run as collect_direct_polls
    from .polls import build_polls, save_polls

    direct = collect_direct_polls(days=days, limit=limit, verbose=False)
    polls = build_polls()
    save_polls(polls)
    return {
        "direct_polls": direct["meta"]["unique"],
        "direct_regions": direct["meta"]["regions"],
        "direct_coverage": direct["meta"]["coverage"],
        "merged_regions": len(polls.get("regions") or {}),
        "raw_counts": (polls.get("meta") or {}).get("counts") or {},
    }


def collect_market_data(
    settings: Settings,
    *,
    baseline_days: int = 30,
    per_region: int = 2,
    attention_per_region: int = 3,
) -> dict[str, Any]:
    """補齊基線、民調、注意力與盤口深度；任何一步失敗都不中斷主流程。"""
    out: dict[str, Any] = {}
    ts = db.utcnow_iso()

    try:
        from .pollwatch import collect_media_polls, save_media_polls

        media = collect_media_polls(settings, days=14, delay=0.25)
        save_media_polls(media)
        out["media_polls"] = len(media.get("polls") or [])
    except Exception as exc:  # noqa: BLE001
        out["media_polls"] = f"error:{type(exc).__name__}"

    try:
        out["polls"] = refresh_polls()
    except Exception as exc:  # noqa: BLE001
        out["polls"] = f"error:{type(exc).__name__}"

    try:
        from .backtest import run_backtest, save_calibration

        if not (CONFIG_DIR / "calibration.json").exists():
            calibration = run_backtest()
            save_calibration(calibration)
            out["calibration"] = calibration["metrics"]
        else:
            out["calibration"] = "cached"
    except Exception as exc:  # noqa: BLE001
        out["calibration"] = f"error:{type(exc).__name__}"

    try:
        from .attention import collect_attention

        payload = collect_attention(settings, limit_per_region=attention_per_region, delay=0.25)
        out["attention"] = db.save_attention(ts, payload["rows"])
    except Exception as exc:  # noqa: BLE001
        out["attention"] = f"error:{type(exc).__name__}"

    try:
        from .marketdeep import collect_market_depth

        latest = db.fetch_latest_odds()
        seen: dict[str, int] = {}
        targets: list[dict[str, Any]] = []
        for row in latest:
            code = row["region_code"]
            if not code or not row["clob_token_id"]:
                continue
            if seen.get(code, 0) >= per_region:
                continue
            seen[code] = seen.get(code, 0) + 1
            targets.append({
                "market_id": row["market_id"],
                "token_id": row["clob_token_id"],
                "region_code": code,
                "candidate_zh": row["candidate_zh"],
                "candidate_en": row["candidate_en"],
                "event_slug": row["event_slug"],
            })
        rows = collect_market_depth(targets, delay=0.2)
        out["depth"] = db.save_market_depth(ts, rows)
    except Exception as exc:  # noqa: BLE001
        out["depth"] = f"error:{type(exc).__name__}"

    _log(f"外部訊號：{out}")
    return out


def get_settings() -> Settings:
    return load_settings()
