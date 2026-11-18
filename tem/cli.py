"""命令列入口。

用法：
  python -m tem run                 # 完整流程：赔率 + 新闻 + 分析 + 预测 + 报告
  python -m tem collect             # 只采集（赔率与新闻）
  python -m tem analyze --limit 100 # 只做新闻分析
  python -m tem forecast            # 只做融合预测与报告
  python -m tem embed               # 只产生 Polymarket 嵌入码
  python -m tem serve               # 启动本地 Dashboard
  python -m tem status              # 显示目前数据状态
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import db, pipeline
from .paths import EXPORT_DIR, REPORT_DIR, ROOT, ensure_dirs
from .zhconv import is_simplified_preferred, neutralize_scope, to_simplified


class _TranscodingStream:
    """把所有终端输出实时转为显示字形（默认简体），程序码本身仍维持原文。"""

    def __init__(self, stream) -> None:
        self._stream = stream

    def write(self, text: str):
        return self._stream.write(to_simplified(neutralize_scope(text)))

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if is_simplified_preferred():
        sys.stdout = _TranscodingStream(sys.stdout)
        sys.stderr = _TranscodingStream(sys.stderr)


def _settings():
    settings = pipeline.get_settings()
    if not settings.llm_api_key:
        print("[warn] 未设置 TEM_LLM_API_KEY，将使用规则式中文情感引擎（可正常运作，但语意细腻度较低）")
    return settings


def cmd_init(_args: argparse.Namespace) -> int:
    db.init_db()
    print(f"数据库已初始化：{db.db_path()}")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    db.init_db()
    settings = _settings()
    pipeline.collect_odds(settings)
    pipeline.collect_news(settings, max_age_days=args.days)
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    db.init_db()
    settings = _settings()
    if not db.latest_ts("odds"):
        print("[warn] 尚无赔率数据，先执行 collect 以提升候选人识别准确度")
    pipeline.run_analysis(settings, limit=args.limit, force=args.force, use_llm=not args.no_llm)
    return 0


def cmd_forecast(args: argparse.Namespace) -> int:
    db.init_db()
    settings = _settings()
    payload = pipeline.run_forecast(settings, window_days=args.window)
    pipeline.write_outputs(settings, payload, embeds=not args.no_embeds)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    db.init_db()
    settings = pipeline.get_settings()
    if not settings.llm_api_key:
        print("[warn] 未设置 TEM_LLM_API_KEY，分析将使用规则式中文情感引擎")
    result = pipeline.run_all(
        settings,
        analyze_limit=args.limit,
        force_analyze=args.force,
        use_llm=not args.no_llm,
        max_age_days=args.days,
        window_days=args.window,
        with_market_data=not args.no_market_data,
    )
    print(json.dumps(result["payload"]["stats"], ensure_ascii=False, indent=2))
    return 0


def cmd_embed(args: argparse.Namespace) -> int:
    db.init_db()
    settings = _settings()
    from .embed import build_embeds, build_gallery_html, build_js_widget
    from .models import build_snapshots

    snapshots = build_snapshots(db.fetch_latest_odds(), name_map=settings.name_map, region_names=settings.region_names())
    if not snapshots:
        print("尚无赔率数据，请先执行 collect")
        return 1
    items = build_embeds(snapshots, settings)
    ensure_dirs()
    (EXPORT_DIR / "polymarket-embeds.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    gallery = EXPORT_DIR / "polymarket-embeds.html"
    gallery.write_text(build_gallery_html(items), encoding="utf-8")
    (EXPORT_DIR / "polymarket-widget.js").write_text(build_js_widget(items), encoding="utf-8")
    print(f"已产生 {len(items)} 组嵌入码：{gallery}")

    if not args.no_widget:
        from .popup import MODULE_JS, build_popup_demo
        from .widget import DEMO_HTML, build_widget_js, collect_widget_data
        from .zhconv import deep_neutralize_scope

        print("[widget] 正在整理候选人赔率、民调、舆情与档案…")
        widget_items = collect_widget_data(
            snapshots,
            settings,
            min_price=args.min_price,
            max_candidates=args.widget_limit,
            allow_network=False,
        )
        # 嵌入元件的資料保留來源字形（繁體），但範圍用語一律輸出為「全島」
        widget_items = deep_neutralize_scope(widget_items)
        widget_path = EXPORT_DIR / "tem-widget.js"
        widget_path.write_text(build_widget_js(widget_items), encoding="utf-8")
        (EXPORT_DIR / "tem-widget-demo.html").write_text(DEMO_HTML, encoding="utf-8")
        (EXPORT_DIR / "tem-widget-data.json").write_text(
            json.dumps(widget_items, ensure_ascii=False), encoding="utf-8"
        )
        (EXPORT_DIR / "tem-candidate-popup.js").write_text(MODULE_JS, encoding="utf-8")
        (EXPORT_DIR / "tem-candidate-popup-demo.html").write_text(
            build_popup_demo(widget_items), encoding="utf-8"
        )
        size_kb = round(widget_path.stat().st_size / 1024, 1)
        print(f"[widget] 已产生 {len(widget_items)} 位候选人卡片：{widget_path}（{size_kb} KB）")
        print(f"[widget] 演示页面：{EXPORT_DIR / 'tem-widget-demo.html'}")
        print(f"[popup]  独立弹窗模块：{EXPORT_DIR / 'tem-candidate-popup.js'}")
        print(f"[popup]  弹窗演示：{EXPORT_DIR / 'tem-candidate-popup-demo.html'}")

    if args.print_all:
        for item in items:
            print("\n" + "=" * 70)
            print(f"# {item['region_name']} — {item.get('leader_zh')}")
            print(item["jsonld"])
            print(item["figure"])
    elif args.region:
        target = next((i for i in items if args.region in (i["region_name"], i["region_code"])), None)
        if not target:
            print(f"找不到县市：{args.region}")
            return 1
        print(target["jsonld"])
        print(target["figure"])
    return 0


def cmd_profiles(args: argparse.Namespace) -> int:
    """抓取 / 更新候选人个人档案（党籍、生日、现职、简历）。"""
    db.init_db()
    settings = pipeline.get_settings()
    from .models import build_snapshots
    from .profile import prefetch, resolve_names, resolve_profile, wikidata_lookup

    snapshots = build_snapshots(db.fetch_latest_odds(), name_map=settings.name_map, region_names=settings.region_names())
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for snap in snapshots.values():
        for cand in snap.candidates:
            key = (cand.name_zh, cand.name_en)
            if key in seen:
                continue
            seen.add(key)
            pairs.append(key)
    if not pairs:
        print("尚无赔率数据，请先执行 collect")
        return 1

    if args.resolve:
        missing = [en for _zh, en in pairs if en not in settings.name_map]
        print(f"[resolve] 用 Wikidata 反查 {len(missing)} 个尚无中文名的候选人（每次约 1 秒）…")
        proposals = resolve_names(missing, throttle=args.throttle)
        ensure_dirs()
        out = EXPORT_DIR / "name-suggestions.json"
        out.write_text(json.dumps(proposals, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[resolve] 命中 {len(proposals)} 个，已写入 {out}")
        print("[resolve] 请人工确认后加入 config/name_map.json，再重跑 profiles")

    stats = prefetch(pairs, force=args.force)
    print(
        f"[profiles] 共 {stats['total']} 位候选人：缓存命中 {stats['hit']}、"
        f"新抓取 {stats['fetched']}、查无资料 {stats['missing']}"
    )

    # 維基百科沒有條目、登記名冊也沒收錄的市場候選人，改用 Wikidata 的 P102 補黨籍
    import time

    fixed: list[str] = []
    for _zh, en in pairs:
        if not en:
            continue
        profile = resolve_profile("", _zh, en, allow_network=False) or {}
        if profile.get("party"):
            continue
        hit = wikidata_lookup(en, _zh, allow_network=True, force=args.force) or {}
        if hit.get("party"):
            fixed.append(f"{en}→{hit.get('zh') or ''}{hit['party']}")
        time.sleep(max(float(args.throttle or 0), 0.8))
    print(f"[profiles] Wikidata 党籍补位 {len(fixed)} 位" + (f"：{'、'.join(fixed)}" if fixed else ""))
    return 0


def cmd_polls(args: argparse.Namespace) -> int:
    """解析 22 县市民调汇总表并写入 config/polls.json。"""
    from .polls import build_polls, save_polls

    print("[polls] 正在解析维基百科民调汇总表…")
    data = build_polls()
    path = save_polls(data)
    regions = data["regions"]
    raw = sum(len(v) for v in data["raw"].values())
    counts = (data.get("meta") or {}).get("counts") or {}
    with_signal = sum(1 for v in regions.values() if v.get("signal"))
    primaries = sum(v.get("primary_polls") or 0 for v in regions.values())
    print(
        f"[polls] 原始 {len(data['raw'])} 个县市、{raw} 笔民调（其中党内初选 {primaries} 笔已排除）"
        f"；其中原文直抓 {counts.get('direct', 0)} 笔；"
        f"可用摘要 {len(regions)} 个县市，其中 {with_signal} 个有对战民调信号"
    )
    for name, item in list(regions.items())[:6]:
        signal = item.get("signal") or {}
        if signal:
            print(
                f"        {name}: {signal['leader']} {signal['leader_pct']}% vs "
                f"{signal['runner_up']} {signal['runner_up_pct']}%（差距 {signal['margin']} 个百分点，"
                f"加權樣本 {item.get('effective_samples')}，最新 {item.get('effective_date')}）"
            )
        else:
            print(f"        {name}: 仅有党内初选民调，不产出对战信号")
    print(f"[polls] 已写入 {path}")
    return 0


def cmd_polldirect(args: argparse.Namespace) -> int:
    """按县市主要候选人定向搜索并抓取民调原文。"""
    from .polldirect import run as run_polldirect

    payload = run_polldirect(days=args.days, limit=args.limit, verbose=True)
    meta = payload["meta"]
    print(
        f"[polldirect] 查询 {meta['query_groups']} 组、发现 {meta['eligible']} 篇、"
        f"抓取 {meta['fetched']} 篇、有效 {meta['unique']} 份；"
        f"覆盖 {meta['regions']}/22 个县市"
    )
    for item in payload["summary"][:8]:
        print(
            f"        {item['region']} {item['pair']}: {item['raw']} "
            f"（{item['polls']} 份，最新 {item['latest']}）"
        )
    if meta.get("coverage"):
        print(f"[polldirect] 新鲜度：{meta['coverage']}")
    print(f"[polldirect] 已写入 {ROOT / 'config' / 'polls_direct.json'}")
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    """生成分析报道（HTML + Markdown）。"""
    from .briefing import generate

    paths = generate(days=args.days)
    print(f"[brief] 已生成分析报道：{paths['html']}")
    print(f"[brief] Markdown 版：{paths['markdown']}（涵盖 {paths['regions']} 个选区）")
    return 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    """把同一件事的多家转载合并成事件，写回 news 表。"""
    from .dedupe import run_dedupe

    db.init_db()
    stats = run_dedupe(days=args.days)
    print(
        f"[dedupe] {stats['articles']} 则新闻 → {stats['events']} 个事件"
        f"（合并 {stats['merged_articles']} 则，最大事件 {stats['largest']} 则）"
    )
    sizes = stats.get("sizes") or {}
    if sizes:
        detail = "、".join(f"{k} 则群 {v} 个" for k, v in sizes.items() if k != "1")
        if detail:
            print(f"[dedupe] 群大小分布：{detail}")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    """用 2018 / 2022 选举回测民调→胜率校准曲线，写入 config/calibration.json。"""
    from .backtest import run_backtest, save_calibration

    print("[backtest] 抓取 2018 / 2022 选前民调并对实际结果回测…")
    data = run_backtest(window_days=args.window)
    path = save_calibration(data)
    metrics = data["metrics"]
    model = data["model"]
    flips = [o for o in data["observations"] if not o["leader_won"]]
    print(
        f"[backtest] 样本 {metrics['n']} 组（其中民调领先者落选 {len(flips)} 组）；"
        f"准确率 {metrics['accuracy'] * 100:.1f}%、"
        f"Brier {metrics['brier']}（基线 {metrics['baseline_brier']}）"
    )
    print(f"[backtest] 拟合：斜率 {model['slope']}、截距 {model['intercept']}")
    print("[backtest] 校准曲线（民调领先幅度 → 胜率）：")
    for margin, prob in model["curve"].items():
        print(f"        {float(margin):5.1f} 个百分点 → {prob * 100:5.1f}%")
    print(f"[backtest] 已写入 {path}")
    return 0


def cmd_depth(args: argparse.Namespace) -> int:
    """抓取 Polymarket 盘口、逐笔成交与持仓集中度。"""
    from .db import fetch_latest_odds, init_db, save_market_depth
    from .marketdeep import collect_market_depth

    init_db()
    latest = fetch_latest_odds()
    per_region: dict[str, int] = {}
    markets: list[dict] = []
    for row in latest:
        code = row["region_code"]
        if not code or not row["clob_token_id"]:
            continue
        if per_region.get(code, 0) >= args.per_region:
            continue
        # fetch_latest_odds 已按 yes_price 由高到低排序，取前幾名即可
        per_region[code] = per_region.get(code, 0) + 1
        markets.append({
            "market_id": row["market_id"],
            "token_id": row["clob_token_id"],
            "region_code": code,
            "candidate_zh": row["candidate_zh"],
            "candidate_en": row["candidate_en"],
            "event_slug": row["event_slug"],
        })
    print(f"[depth] 抓取 {len(markets)} 个盘口（每县市前 {args.per_region} 名）…")
    rows = collect_market_depth(markets, delay=args.delay, hours=args.hours)
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    written = save_market_depth(ts, rows)
    ok = [r for r in rows if not r.get("error")]
    spreads = [r["spread"] for r in ok if r.get("spread") is not None]
    depths = [r.get("depth_usdc") or 0 for r in ok]
    flat = [r for r in ok if (r.get("depth_usdc") or 0) < 3000]
    print(
        f"[depth] 成功 {len(ok)}/{len(rows)} 个；平均价差 "
        f"{(sum(spreads) / len(spreads) * 100 if spreads else 0):.2f} 个百分点、"
        f"平均 5% 内挂单深度 {(sum(depths) / len(depths) if depths else 0):,.0f} USDC"
    )
    if flat:
        print(f"[depth] 其中 {len(flat)} 个盘口挂单不足 3000 USDC，属于极稀薄市场")
    print(f"[depth] 已写入 market_depth 表（{ts}，共 {written} 列）")
    return 0


def cmd_attention(args: argparse.Namespace) -> int:
    """抓取 PTT 聲量與維基百科瀏覽量，寫入 attention 表。"""
    from .attention import collect_attention
    from .db import init_db, save_attention

    settings = _settings()
    init_db()
    print(f"[attention] 抓取 PTT 看板近況與各候選人維基瀏覽量（每頁間隔 {args.delay}s）…")
    payload = collect_attention(
        settings,
        board=args.board,
        ptt_pages=args.pages,
        view_days=args.days,
        delay=args.delay,
        limit_per_region=args.per_region,
    )
    ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    written = save_attention(ts, payload["rows"])
    candidates = [r for r in payload["rows"] if r["entity_type"] == "candidate"]
    ptt_rows = [r for r in candidates if r["source"] == "ptt"]
    wiki_rows = [r for r in candidates if r["source"] == "wikipedia"]
    print(
        f"[attention] 看板 {len(payload['board_items'])} 篇；候選人 PTT {len(ptt_rows)} 位、"
        f"維基瀏覽量 {len(wiki_rows)} 位；共寫入 {written} 列（{ts}）"
    )
    for row in sorted(wiki_rows, key=lambda r: -(r.get("views_7d") or 0))[:8]:
        print(
            f"        {row['entity']}: 近 7 日均 {row.get('views_7d')} 次、"
            f"動能 {row.get('momentum')}、PTT 聲量 "
            f"{next((r['mentions'] for r in ptt_rows if r['entity'] == row['entity']), 0)} 篇"
        )
    return 0


def cmd_baseline(args: argparse.Namespace) -> int:
    """抓取 2018 / 2022 縣市長選舉結果，寫入 config/baseline.json。"""
    from .baseline import build_baseline, save_baseline

    print("[baseline] 正在抓取維基百科 2018 / 2022 縣市長選舉結果…")
    data = build_baseline()
    path = save_baseline(data)
    regions = data["regions"]
    both = sum(1 for v in regions.values() if "2018" in v and "2022" in v)
    print(f"[baseline] {len(regions)} 個縣市，其中 {both} 個同時有 2018 與 2022 資料")
    for name, item in list(regions.items())[:6]:
        y22 = item.get("2022") or {}
        y18 = item.get("2018") or {}
        print(
            f"        {name}: 2018 {y18.get('winner')}（{y18.get('winner_pct')}%）→ "
            f"2022 {y22.get('winner')}（{y22.get('winner_pct')}%，差距 {y22.get('margin')}）"
        )
    print(f"[baseline] 已写入 {path}")
    return 0


def cmd_roster(args: argparse.Namespace) -> int:
    """从维基百科解析 22 县市候选人登记名册并写入 config/roster.json。"""
    from .roster import build_roster, save_roster

    print("[roster] 正在解析维基百科候选人登记表…")
    roster = build_roster()
    path = save_roster(roster)
    total = sum(len(v) for v in roster["regions"].values())
    with_en = sum(1 for v in roster["regions"].values() for e in v if e.get("en"))
    print(f"[roster] {len(roster['regions'])} 个县市、{total} 位候选人，其中 {with_en} 位有英文标签")
    print(f"[roster] 已写入 {path}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path = REPORT_DIR / (args.file or "latest.md")
    if not path.exists():
        print(f"找不到报告：{path}，请先执行 run 或 forecast")
        return 1
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    db.init_db()
    info = db.stats()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print(f"\n项目根目录：{ROOT}")
    print(f"报告目录　：{REPORT_DIR}")
    print(f"导出目录　：{EXPORT_DIR}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    db.init_db()
    from .web import serve

    from .web import DEFAULT_PORT

    # "auto" = 从默认端口开始往后找一个空闲的（port 0 则代表交由系统随机指派）
    port = DEFAULT_PORT if str(args.port).strip().lower() == "auto" else int(args.port)
    serve(
        host=args.host,
        port=port,
        open_browser=not args.no_browser,
        strict=args.strict_port,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tem",
        description="台湾九合一选举 舆情监测 × Polymarket 赔率融合预测系统",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化数据库")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("collect", help="采集 Polymarket 赔率与新闻")
    p.add_argument("--days", type=int, default=7, help="新闻回溯天数（默认 7）")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("analyze", help="对未分析的新闻执行 AI 舆情分析")
    p.add_argument("--limit", type=int, default=60, help="本次最多分析几则（默认 60）")
    p.add_argument("--force", action="store_true", help="重新分析已分析过的新闻")
    p.add_argument("--no-llm", action="store_true", help="强制使用规则式引擎")
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("forecast", help="产生融合预测与报告")
    p.add_argument("--window", type=int, default=7, help="舆情聚合回溯天数（默认 7）")
    p.add_argument("--no-embeds", action="store_true", help="不产生 Polymarket 嵌入码")
    p.set_defaults(func=cmd_forecast)

    p = sub.add_parser("run", help="执行完整流程")
    p.add_argument("--limit", type=int, default=60, help="本次最多分析几则新闻")
    p.add_argument("--days", type=int, default=7, help="新闻回溯天数")
    p.add_argument("--window", type=int, default=7, help="舆情聚合回溯天数")
    p.add_argument("--force", action="store_true", help="重新分析已分析过的新闻")
    p.add_argument("--no-llm", action="store_true", help="强制使用规则式引擎")
    p.add_argument(
        "--no-market-data",
        action="store_true",
        help="跳过民调、关注度与盘口深度采集（只跑赔率与新闻）",
    )
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("embed", help="产生 Polymarket 嵌入码")
    p.add_argument("--region", help="只输出指定县市（例：新北市）")
    p.add_argument("--print-all", action="store_true", help="在终端输出全部嵌入码")
    p.add_argument("--no-widget", action="store_true", help="不产生可嵌入的候选人赔率卡片组件")
    p.add_argument("--widget-limit", type=int, default=60, help="卡片最多包含几位候选人")
    p.add_argument("--min-price", type=float, default=0.01, help="只收录赔率高于此值的候选人")
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("report", help="输出最新报告")
    p.add_argument("--file", help="指定报告档名（默认 latest.md）")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("profiles", help="抓取候选人个人档案（党籍 / 生日 / 现职 / 简历）")
    p.add_argument("--force", action="store_true", help="忽略缓存重新抓取")
    p.add_argument("--resolve", action="store_true", help="用 Wikidata 反查缺中文名的候选人")
    p.add_argument("--throttle", type=float, default=1.0, help="反查时每次请求的间隔秒数")
    p.set_defaults(func=cmd_profiles)

    p = sub.add_parser("roster", help="从维基百科解析 22 县市候选人登记名册")
    p.set_defaults(func=cmd_roster)

    p = sub.add_parser("polls", help="从维基百科解析 22 县市民调汇总表")
    p.set_defaults(func=cmd_polls)

    p = sub.add_parser("polldirect", help="按县市主要候选人直抓民调原文并计算支持度")
    p.add_argument("--days", type=int, default=45, help="回溯天数（默认 45）")
    p.add_argument("--limit", type=int, default=70, help="本次最多抓取几篇文章（默认 70）")
    p.set_defaults(func=cmd_polldirect)

    p = sub.add_parser("baseline", help="抓取 2018 / 2022 县市长选举结果作为基线与回测依据")
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("attention", help="抓取 PTT 声量与维基百科浏览量")
    p.add_argument("--board", default="HatePolitics", help="PTT 看板名称")
    p.add_argument("--pages", type=int, default=2, help="每个关键词翻页数")
    p.add_argument("--days", type=int, default=60, help="维基百科浏览量回溯天数")
    p.add_argument("--per-region", type=int, default=4, help="每个县市抓前几位候选人")
    p.add_argument("--delay", type=float, default=0.35, help="每次请求间隔秒数")
    p.set_defaults(func=cmd_attention)

    p = sub.add_parser("depth", help="抓取 Polymarket 盘口、逐笔成交与持仓集中度")
    p.add_argument("--per-region", type=int, default=3, help="每个县市抓前几位候选人")
    p.add_argument("--hours", type=int, default=24, help="逐笔成交回溯小时数")
    p.add_argument("--delay", type=float, default=0.25, help="每次请求间隔秒数")
    p.set_defaults(func=cmd_depth)

    p = sub.add_parser("backtest", help="用 2018 / 2022 选举回测民调到胜率的校准曲线")
    p.add_argument("--window", type=int, default=90, help="取投票日前几天的民调")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("dedupe", help="把转载新闻合并成事件（舆情以事件为单位计算）")
    p.add_argument("--days", type=int, default=30, help="回溯天数")
    p.set_defaults(func=cmd_dedupe)

    p = sub.add_parser("brief", help="生成选情分析报道（HTML / Markdown）")
    p.add_argument("--days", type=int, default=21, help="舆情回溯天数")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("status", help="显示数据状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("serve", help="启动本地 Dashboard")
    p.add_argument(
        "--port",
        default="auto",
        help="端口；默认 auto（从 8790 起自动寻找空闲端口），也可指定数字",
    )
    p.add_argument("--strict-port", action="store_true", help="指定端口被占用时直接失败，不改用其他端口")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
