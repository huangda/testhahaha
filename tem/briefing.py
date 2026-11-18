"""分析報道生成器：把系統抓到的一切整理成一篇可讀的選情分析。

和 `report.py` 的差別：`report.py` 產出的是資料表（給人核對數字用的），
這裡產出的是「報道」——有摘要、有逐區解讀、有輿情走向，數字都帶出處與
不確定性說明，可以直接給人看或轉成 PDF。

內容來源全部是系統已經抓到的資料：
  市場賠率、盤口深度、加權民調（含當週新聞民調）、
  回測校準曲線、規則式輿情分析（情感/議題/候選人）、事件去重結果、
  PTT 聲量與維基瀏覽量、候選人檔案、登記名冊。

輸出：
  - `data/exports/report.html`：獨立網頁（深色科技風、可直接列印成 PDF）
  - `data/exports/report.md`  ：同樣內容的 Markdown 版
"""

from __future__ import annotations

import html
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .paths import CONFIG_DIR, EXPORT_DIR, ensure_dirs
from .forecast import TOSSUP_MARGIN  # 膠著判定線與預測模組共用同一個常數

LOCAL = timezone(timedelta(hours=8))


# ---------------------------------------------------------------- 取數

def _load(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _local(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL)


def _json_field(row: Any, key: str) -> Any:
    value = row.get(key) if hasattr(row, "get") else None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return []
    return value or []


def collect_payload(*, days: int = 21) -> dict[str, Any]:
    """把生成報道需要的資料一次抓齊。"""
    from . import db
    from .config import load_settings
    from .forecast import load_context

    settings = load_settings()
    db.init_db()
    ctx = load_context()
    polls = _load("polls.json")
    polls_media = _load("polls_media.json")
    calibration = _load("calibration.json")
    roster = _load("roster.json")

    odds = [dict(r) for r in db.fetch_latest_odds()]
    forecasts = [dict(r) for r in db.fetch_latest_forecasts()]
    region_states = [dict(r) for r in db.fetch_latest_region_state()]
    news = [dict(r) for r in db.fetch_news_since(days=days, limit=5000)]
    opinion_days = 7
    opinion_cutoff = datetime.now(LOCAL) - timedelta(days=opinion_days)
    opinion_news = [
        row for row in news
        if (_local(row.get("published_at") or row.get("first_seen")) or datetime.min.replace(tzinfo=LOCAL))
        >= opinion_cutoff
    ]
    try:
        from .observe import _election_terms, _thread_relevant

        candidate_terms, region_terms = _election_terms(settings)
        opinion_news = [
            row for row in opinion_news
            if _thread_relevant(
                " ".join([
                    str(row.get("title") or ""),
                    str(row.get("summary") or ""),
                    " ".join(str(x) for x in _json_field(row, "candidates")),
                    " ".join(str(x) for x in _json_field(row, "regions")),
                ]),
                candidate_terms,
                region_terms,
            )
        ]
    except Exception:  # noqa: BLE001 - 过滤失败时保留原有新闻
        pass
    attention = {r["entity"]: dict(r) for r in db.fetch_latest_attention()}
    depth = {str(r["market_id"]): dict(r) for r in db.fetch_latest_market_depth()}

    # 逐日輿情走向（以事件代表為單位，避免轉載放大）
    trend: dict[str, dict[str, float]] = {}
    topics: Counter[str] = Counter()
    for row in opinion_news:
        when = _local(row.get("published_at") or row.get("first_seen"))
        if not when:
            continue
        key = when.date().isoformat()
        bucket = trend.setdefault(key, {"count": 0.0, "weighted": 0.0, "weight": 0.0})
        sentiment = row.get("sentiment")
        if sentiment is not None and row.get("is_rep", 1):
            bucket["count"] += 1
            impact = float(row.get("impact") or 0.5)
            bucket["weighted"] += float(sentiment) * impact
            bucket["weight"] += impact
        if row.get("is_rep", 1):
            for topic in _json_field(row, "topics"):
                name = topic.get("topic") if isinstance(topic, dict) else str(topic)
                if name:
                    topics[str(name)] += 1

    trend_rows = []
    for key in sorted(trend):
        bucket = trend[key]
        trend_rows.append({
            "date": key,
            "events": int(bucket["count"]),
            "sentiment": round(bucket["weighted"] / bucket["weight"], 3) if bucket["weight"] else None,
        })

    # 多來源熱點事件（同一件事被最多家媒體報導）
    hot: list[dict[str, Any]] = []
    hot_seen: set[tuple[Any, ...]] = set()
    from .dedupe import bigrams, jaccard, normalize_title
    sources_by_cluster: dict[str, set[str]] = {}
    for row in opinion_news:
        cluster_id = str(row.get("cluster_id") or "")
        source_name = str(row.get("source_name") or "").strip()
        if cluster_id and source_name:
            sources_by_cluster.setdefault(cluster_id, set()).add(source_name)

    for row in sorted(opinion_news, key=lambda r: -(r.get("cluster_size") or 1)):
        size = row.get("cluster_size") or 1
        if not row.get("is_rep", 1):
            continue
        when = _local(row.get("published_at") or row.get("first_seen")) or datetime.now(LOCAL)
        title = row.get("title") or ""
        candidates = []
        for item in _json_field(row, "candidates"):
            if isinstance(item, dict):
                name = item.get("name_zh") or item.get("name")
            else:
                name = str(item)
            if name:
                candidates.append(str(name))
        if "民調" in title and len(set(candidates)) >= 2:
            event_key = ("poll", when.date().isoformat(), tuple(sorted(set(candidates))))
        else:
            event_key = (
                "cluster",
                row.get("cluster_id") or normalize_title(title),
            )
        if event_key in hot_seen:
            continue
        normalized = normalize_title(title)
        if "民調" in title:
            duplicate = False
            for existing in hot:
                if existing.get("date") != when.strftime("%m-%d"):
                    continue
                if "民調" not in str(existing.get("title") or ""):
                    continue
                shared = set(candidates) & set(existing.get("candidates") or [])
                similarity = jaccard(bigrams(normalized), bigrams(normalize_title(existing["title"])))
                if shared and similarity >= 0.18:
                    duplicate = True
                    break
            if duplicate:
                continue
        hot_seen.add(event_key)
        hot.append({
            "title": title,
            "source": row.get("source_name") or "",
            "sources": sorted(sources_by_cluster.get(str(row.get("cluster_id") or ""), set())),
            "date": when.strftime("%m-%d"),
            "size": size,
            "sentiment": row.get("sentiment"),
            "impact": row.get("impact"),
            "candidates": sorted(set(candidates)),
            "regions": [str(x) for x in _json_field(row, "regions")],
            "summary": row.get("ai_summary") or "",
            "url": row.get("url") or "",
        })
        if len(hot) >= 12:
            break

    stats = db.stats()
    try:
        stats.update(db.event_stats(days=days))
    except Exception:  # noqa: BLE001
        pass

    def safe_call(function):
        try:
            return function()
        except Exception as exc:  # noqa: BLE001 - 单项补充失败不阻断报告
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "warnings": [f"{type(exc).__name__}: {exc}"],
            }

    from . import google_trends, observe, ptt_opinion, statements, youtube_opinion

    with ThreadPoolExecutor(max_workers=5) as pool:
        observe_future = pool.submit(
            safe_call,
            lambda: observe.get_insights(settings, hours=168),
        )
        ptt_future = pool.submit(
            safe_call,
            lambda: ptt_opinion.get_opinion(days=7),
        )
        youtube_future = pool.submit(
            safe_call,
            lambda: youtube_opinion.get_opinion(days=7),
        )
        trends_future = pool.submit(
            safe_call,
            lambda: google_trends.daily_election_trends(force=False),
        )
        statements_future = pool.submit(
            safe_call,
            lambda: statements.get_payload(settings, days=30),
        )
        observe_payload = observe_future.result()
        ptt_payload = ptt_future.result()
        youtube_payload = youtube_future.result()
        trends_payload = trends_future.result()
        statements_payload = statements_future.result()

    if not isinstance(observe_payload, dict) or "threads" not in observe_payload:
        observe_payload = {
            "ok": False,
            "threads": [],
            "trends": [],
            "graph": {"nodes": [], "edges": []},
            "counts": {},
            "warnings": [str((observe_payload or {}).get("error") or "外部接口暂不可用")],
        }

    return {
        "generated_at": datetime.now(LOCAL),
        "days": days,
        "opinion_days": opinion_days,
        "settings": settings,
        "stats": stats,
        "odds": odds,
        "forecasts": forecasts,
        "region_states": region_states,
        "news": news,
        "opinion_news": opinion_news,
        "trend": trend_rows,
        "topics": topics.most_common(12),
        "hot_events": hot,
        "attention": attention,
        "depth": depth,
        "polls": polls,
        "polls_media": polls_media,
        "calibration": calibration,
        "roster": roster,
        "context": ctx,
        "observe": observe_payload,
        "ptt": ptt_payload,
        "youtube": youtube_payload,
        "google_trends": trends_payload,
        "statements": statements_payload,
    }


# ---------------------------------------------------------------- 組裝

def _pct(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _num(value: float | None, digits: int = 1, unit: str = "") -> str:
    return "—" if value is None else f"{value:.{digits}f}{unit}"


def _region_key(name: str) -> str:
    return (name or "").replace("臺", "台").strip()


def _candidate_name_key(name: str) -> str:
    from .zhconv import to_simplified

    return to_simplified((name or "").replace("臺", "台")).replace(" ", "").lower()


def _election_countdown(settings) -> int | None:
    try:
        target = date.fromisoformat(settings.election_date)
    except (ValueError, TypeError):
        return None
    return (target - datetime.now(LOCAL).date()).days


def _region_bundle(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把每位候選人的市場 / 民調 / 融合 / 檔案併成逐區資料。"""
    forecasts = payload["forecasts"]
    odds = payload["odds"]
    depth = payload["depth"]
    attention = payload["attention"]
    roster = (payload["roster"] or {}).get("regions") or {}

    by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in forecasts:
        by_region[row["region_code"]].append(row)

    out: list[dict[str, Any]] = []
    for code, rows in by_region.items():
        rows = sorted(rows, key=lambda r: -(r.get("fused_prob") or 0))
        region_name = rows[0].get("region_name") or code
        odds_rows = [o for o in odds if o.get("region_code") == code]
        cand_meta = {o.get("candidate_en"): o for o in odds_rows}
        registered = next(
            (
                entries
                for key, entries in roster.items()
                if _region_key(key) == _region_key(region_name)
            ),
            [],
        )
        candidate_rows = rows
        if registered:
            from .roster import entry_for

            official_rows = [
                row
                for row in rows
                if entry_for(
                    payload.get("roster") or {},
                    region_name,
                    row.get("candidate_zh") or "",
                    row.get("candidate_en") or "",
                )
            ]
            if official_rows:
                candidate_rows = official_rows

        candidates: list[dict[str, Any]] = []
        for row in candidate_rows:
            meta = cand_meta.get(row.get("candidate_en")) or {}
            profile = {}
            try:
                # 用 resolve_profile 而不是 get_profile：它會把「候選人登記名冊」
                # 的黨籍與經歷合併進來，維基頁沒抓到黨籍時才不會留空。
                from .profile import resolve_profile

                profile = resolve_profile(
                    region_name,
                    row.get("candidate_zh") or "",
                    row.get("candidate_en") or "",
                    allow_network=False,
                ) or {}
            except Exception:  # noqa: BLE001
                profile = {}
            marker = depth.get(str(meta.get("market_id"))) or {}
            attn = attention.get(row.get("candidate_zh") or "") or {}
            # 全島盤的「候選人」其實就是政黨名稱，直接沿用以免該行留空
            from .profile import PARTY_COLORS

            cand_name = row.get("candidate_zh") or row.get("candidate_en")
            party = profile.get("party") or (cand_name if cand_name in PARTY_COLORS else "")
            roster_entry = next(
                (entry for entry in registered if entry.get("name") == cand_name),
                {},
            )
            candidates.append({
                "name": cand_name,
                "name_en": row.get("candidate_en"),
                "number": roster_entry.get("number"),
                "party": party,
                "party_color": profile.get("party_color") or PARTY_COLORS.get(party, "#8794ad"),
                "office": profile.get("office") or "",
                "experience": profile.get("experience") or "",
                "birth": profile.get("birth") or "",
                "image": meta.get("image_url") or profile.get("photo") or "",
                "market": row.get("pm_prob"),
                "poll": row.get("poll_share"),
                "fused": row.get("fused_prob"),
                "sentiment": row.get("news_sentiment"),
                "articles": row.get("articles"),
                "leader": bool(row.get("is_leader")),
                "rationale": row.get("rationale") or "",
                "spread_pts": marker.get("spread_pts"),
                "depth_usdc": marker.get("depth_usdc"),
                "flow": marker.get("flow_imbalance"),
                "top_share": marker.get("top_share"),
                "views_7d": (attn.get("wikipedia") or {}).get("views_7d"),
                "momentum": (attn.get("wikipedia") or {}).get("momentum"),
                "ptt": (attn.get("ptt") or {}).get("mentions"),
            })

        out.append({
            "code": code,
            "name": region_name,
            "candidates": candidates,
            "registered": registered,
            "poll_margin": rows[0].get("poll_margin"),
            "poll_leader": rows[0].get("poll_leader"),
            "tossup": (rows[0].get("fused_prob") or 0) - (
                rows[1].get("fused_prob") if len(rows) > 1 else 0
            ) <= TOSSUP_MARGIN,
        })
    out.sort(key=lambda r: -(r["candidates"][0].get("fused") or 0))
    return out


def _narrative(region: dict[str, Any], payload: dict[str, Any]) -> str:
    """用可驗證的事實拼出逐區解讀（每一句都對應資料）。"""
    cands = region["candidates"]
    top = cands[0]
    runner = cands[1] if len(cands) > 1 else None
    parts: list[str] = []

    margin = (top.get("fused") or 0) - (runner.get("fused") if runner else 0)
    if margin <= 0.05:
        parts.append(f"{top['name']}與{runner['name']}幾乎打平，是本次選舉最難判讀的選區之一。")
    elif margin <= TOSSUP_MARGIN:
        parts.append(f"{top['name']}僅領先{runner['name']}{margin * 100:.1f} 個百分點，屬於膠著選區。")
    else:
        parts.append(
            f"模型目前由{top['name']}領先，機率上領先{runner['name']} {margin * 100:.1f} 個百分點。"
            if runner else f"模型目前由{top['name']}領先。"
        )

    pm, fused = top.get("market"), top.get("fused")
    if pm is not None and fused is not None:
        delta = (fused - pm) * 100
        if delta >= 1.5:
            parts.append(f"市場只給 {pm * 100:.1f}%，但民調與新聞輿情把融合機率上修到 {fused * 100:.1f}%。")
        elif delta <= -1.5:
            parts.append(f"市場給到 {pm * 100:.1f}%，是輿情與民調把融合機率下修到 {fused * 100:.1f}%。")

    poll_leader = region.get("poll_leader")
    poll_margin = region.get("poll_margin")
    if poll_leader:
        if poll_leader == top["name"] and (poll_margin or 0) >= 0:
            parts.append(f"民調加權後由{poll_leader}領先 {poll_margin:.1f} 個百分點，方向與市場一致。")
        elif poll_leader == top["name"]:
            parts.append(
                f"民調表面上的領先者是{poll_leader}，但把各方「差距」證據合併後方向反轉"
                f"（{abs(poll_margin or 0):.1f} 個百分點），兩種讀數互相矛盾。"
            )
        else:
            parts.append(
                f"民調加權領先者其實是{poll_leader}（領先 {abs(poll_margin or 0):.1f} 個百分點），"
                f"與市場認定不一致，屬「民調分歧」選區。"
            )

    sent = top.get("sentiment")
    if sent is not None and top.get("articles"):
        tone = "偏正面" if sent > 0.12 else ("偏負面" if sent < -0.12 else "中性")
        parts.append(f"近 {payload['days']} 天與該區相關的事件有 {top['articles']} 則，情感{tone}（{sent:+.2f}）。")

    for cand in cands[:3]:
        if cand.get("depth_usdc") and cand["depth_usdc"] < 3000:
            parts.append(f"{cand['name']}的盤口掛單僅約 {cand['depth_usdc']:,.0f} USDC，賠率容易被少量資金帶動。")
            break

    return "".join(parts)


def _summary_points(regions: list[dict[str, Any]], payload: dict[str, Any]) -> list[str]:
    points: list[str] = []
    tight = [r for r in regions if r.get("tossup")][:3]
    if tight:
        points.append(
            "最膠著：" + "、".join(
                f"{r['name']}（{(r['candidates'][0].get('fused') or 0) * 100:.1f}% vs "
                f"{(r['candidates'][1].get('fused') if len(r['candidates']) > 1 else 0) * 100:.1f}%）"
                for r in tight
            )
        )
    conflict = [
        r for r in regions
        if r.get("poll_leader") and r["poll_leader"] != r["candidates"][0]["name"]
    ][:3]
    if conflict:
        points.append("市場與民調不同調：" + "、".join(r["name"] for r in conflict))

    revision = []
    for r in regions:
        top = r["candidates"][0]
        if top.get("market") is not None and top.get("fused") is not None:
            revision.append((abs(top["fused"] - top["market"]), r, top))
    revision.sort(key=lambda x: -x[0])
    if revision and revision[0][0] >= 0.03:
        _, r, top = revision[0]
        points.append(
            f"模型修正最大：{r['name']} {top['name']}"
            f"（市場 {top['market'] * 100:.1f}% → 融合 {top['fused'] * 100:.1f}%）"
        )

    neg = sorted(
        (r for r in regions if r["candidates"][0].get("sentiment") is not None),
        key=lambda r: r["candidates"][0]["sentiment"],
    )[:3]
    if neg:
        points.append(
            "輿情最負面：" + "、".join(
                f"{r['name']}（{r['candidates'][0]['sentiment']:+.2f}）" for r in neg
            )
        )

    events = payload["stats"].get("events")
    articles = payload["stats"].get("articles")
    if events and articles:
        points.append(f"情報規模：近 {payload['days']} 天 {articles} 則新聞，去重後 {events} 個事件。")
    return points


# ---------------------------------------------------------------- 繪圖

def _trend_svg(trend: list[dict[str, Any]], width: int = 860, height: int = 150) -> str:
    rows = [r for r in trend if r.get("sentiment") is not None]
    if len(rows) < 2:
        return '<div class="muted">輿情樣本不足，無法繪製走向圖。</div>'
    pad_l, pad_r, pad_t, pad_b = 34, 12, 10, 20
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b
    counts = [r["events"] for r in rows]
    max_c = max(counts) or 1
    step = iw / max(1, len(rows) - 1)
    bars = []
    line = []
    for i, row in enumerate(rows):
        x = pad_l + step * i
        h = ih * (row["events"] / max_c)
        bars.append(
            f'<rect x="{x - step * 0.28:.1f}" y="{pad_t + ih - h:.1f}" width="{max(2, step * 0.56):.1f}" '
            f'height="{h:.1f}" rx="2" fill="rgba(76,141,255,.28)"/>'
        )
        y = pad_t + ih * (1 - (row["sentiment"] + 1) / 2)
        line.append(f"{x:.1f},{y:.1f}")
    zero = pad_t + ih / 2
    labels = []
    for i in range(0, len(rows), max(1, len(rows) // 6)):
        x = pad_l + step * i
        labels.append(
            f'<text x="{x:.1f}" y="{height - 6}" font-size="9" fill="#7b8798" text-anchor="middle">'
            f'{rows[i]["date"][5:]}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="輿情走向">'
        f'<line x1="{pad_l}" y1="{zero:.1f}" x2="{width - pad_r}" y2="{zero:.1f}" '
        f'stroke="rgba(255,255,255,.14)" stroke-dasharray="3 3"/>'
        f'<text x="4" y="{pad_t + 8}" font-size="9" fill="#7b8798">正面</text>'
        f'<text x="4" y="{pad_t + ih}" font-size="9" fill="#7b8798">負面</text>'
        f'<text x="4" y="{pad_t + ih / 2 + 3}" font-size="9" fill="#7b8798">0</text>'
        + "".join(bars)
        + f'<polyline points="{" ".join(line)}" fill="none" stroke="#22d3ee" stroke-width="1.6"/>'
        + "".join(labels)
        + "</svg>"
    )


def _bar_pair(market: float | None, fused: float | None) -> str:
    def bar(value: float | None, cls: str) -> str:
        if value is None:
            return ""
        return f'<i class="{cls}" style="width:{max(0.0, min(1.0, value)) * 100:.1f}%"></i>'
    return f'<span class="bar">{bar(market, "m")}{bar(fused, "f")}</span>'


# ---------------------------------------------------------------- 輸出

CSS = """
:root{--bg:#070b12;--panel:#0e1522;--panel2:#121b2b;--line:rgba(255,255,255,.09);
--txt:#e8eef8;--txt2:#b6c2d6;--muted:#8593a8;--dim:#5d6b80;--cyan:#22d3ee;--blue:#4c8dff;
--up:#34d399;--down:#fb7185;--amber:#fbbf24}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font-family:"Microsoft YaHei UI","PingFang SC","Noto Sans SC",system-ui,sans-serif;
line-height:1.75;-webkit-font-smoothing:antialiased}
.wrap{max-width:1120px;margin:0 auto;padding:38px 22px 80px}
a{color:var(--cyan);text-decoration:none}
h1{font-size:29px;margin:0 0 8px;letter-spacing:.4px}
h2{font-size:19px;margin:40px 0 14px;padding-left:11px;border-left:3px solid var(--cyan)}
h3{font-size:15px;margin:22px 0 8px;color:var(--txt)}
.meta{color:var(--muted);font-size:12.5px}
.lede{font-size:14.5px;color:var(--txt2);margin:16px 0 0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));gap:11px;margin:22px 0 6px}
.kpi{background:linear-gradient(160deg,var(--panel2),var(--panel));border:1px solid var(--line);
border-radius:12px;padding:13px 15px}
.kpi .lab{font-size:11px;color:var(--muted);letter-spacing:1.2px}
.kpi .val{font-size:23px;font-weight:650;margin-top:3px;
font-family:ui-monospace,"Cascadia Mono",monospace}
.kpi .hint{font-size:11px;color:var(--dim);margin-top:3px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:16px 18px;margin:13px 0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(320px,100%),1fr));gap:13px}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:11.5px;letter-spacing:.6px}
td.num,th.num{text-align:right;font-family:ui-monospace,monospace}
.tag{display:inline-block;font-size:10.5px;padding:1px 7px;border-radius:99px;
border:1px solid var(--line);color:var(--muted);margin-right:4px;white-space:nowrap}
.tag.hot{color:var(--amber);border-color:rgba(251,191,36,.35)}
.tag.flip{color:var(--down);border-color:rgba(251,113,133,.35)}
.tag.ok{color:var(--up);border-color:rgba(52,211,153,.35)}
.muted{color:var(--muted);font-size:12.5px}
.dim{color:var(--dim)}
.lead{display:flex;align-items:center;gap:11px;flex-wrap:wrap}
.av{position:relative;width:34px;height:34px;border-radius:10px;background:rgba(118,160,255,.12);
display:flex;align-items:center;justify-content:center;font-weight:600;overflow:hidden;flex:0 0 auto;
color:var(--txt-2);font-size:14px;line-height:1}
/* 圖片絕對定位鋪滿整個方塊，首字只在沒有圖片時露出；
   若用一般流式排版，圖片會和首字爭寬度而被擠扁 */
.av img{position:absolute;inset:0;display:block;width:100%;height:100%;object-fit:cover}
.av img.contain{object-fit:contain;background:#0d1424}
.bar{display:block;position:relative;height:7px;border-radius:99px;
background:rgba(255,255,255,.07);margin-top:5px;overflow:hidden}
.bar i{position:absolute;left:0;top:0;bottom:0;border-radius:99px}
.bar i.m{background:rgba(76,141,255,.42)}
.bar i.f{height:3px;top:2px;background:var(--cyan)}
.chart{width:100%;height:auto;display:block}
.note{font-size:12px;color:var(--muted);background:rgba(76,141,255,.06);
border:1px solid var(--line);border-radius:10px;padding:11px 13px}
ul.pts{margin:10px 0 0;padding-left:20px}
ul.pts li{margin:5px 0;font-size:13.5px;color:var(--txt2)}
.cand{display:grid;grid-template-columns:1.6fr .7fr .7fr .7fr;gap:8px;align-items:center;
font-size:12.5px;padding:6px 0;border-bottom:1px solid var(--line)}
.cand,.cand>*{min-width:0}
.cand:last-child{border-bottom:0}
.cand .n{display:flex;align-items:center;gap:8px;min-width:0}
.cand .n span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.maprow{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
.mapbox{position:relative;flex:0 0 400px;max-width:400px;
border:1px solid var(--line);border-radius:12px;overflow:hidden;cursor:grab;
background:radial-gradient(62% 52% at 50% 26%,rgba(76,141,255,.12),transparent 72%),rgba(8,13,24,.5)}
.mapbox.drag{cursor:grabbing}
.mapbox svg{display:block;width:100%;height:auto;user-select:none}
.mapbox svg path{stroke:rgba(4,8,16,.9);stroke-width:1.1;vector-effect:non-scaling-stroke;
cursor:pointer;transition:fill .4s,opacity .2s}
.mapbox svg path:hover{filter:brightness(1.3)}
.mapbox svg path.on{stroke:#fff;stroke-width:2}
.mapbox svg path.dim{opacity:.3}
.mapbox svg text{pointer-events:none;fill:rgba(232,238,252,.82);font-size:11px;font-weight:600;
text-anchor:middle;dominant-baseline:middle;paint-order:stroke;stroke:rgba(3,6,12,.75);stroke-width:2.6px}
.mapctl{position:absolute;top:8px;right:8px;display:flex;flex-direction:column;gap:5px}
.mapctl button{width:26px;height:26px;border-radius:7px;border:1px solid var(--line);
background:rgba(8,13,24,.75);color:var(--txt2);font-size:13px;line-height:1;cursor:pointer}
.mapctl button:hover{border-color:var(--cyan);color:#fff}
.maplegend{position:absolute;left:8px;bottom:8px;display:flex;flex-wrap:wrap;gap:9px;
padding:5px 10px;border-radius:99px;background:rgba(6,10,19,.76);border:1px solid var(--line);
font-size:10.5px;color:var(--muted)}
.maplegend span{display:inline-flex;align-items:center;gap:5px;white-space:nowrap}
.maplegend i{width:9px;height:9px;border-radius:3px;display:inline-block}
.mapside{flex:1 1 320px;min-width:0}
.mapside .prow{display:grid;grid-template-columns:118px 1fr 108px;gap:10px;align-items:center;
margin-bottom:9px;font-size:12px}
.mapside .prow .nm{display:flex;align-items:center;gap:7px;min-width:0}
.mapside .prow .nm i{width:9px;height:9px;border-radius:3px;flex:0 0 auto}
.mapside .prow .nm span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mapside .prow .pb{position:relative;height:6px;border-radius:99px;background:rgba(118,160,255,.1);overflow:hidden}
.mapside .prow .pb i{position:absolute;inset:0 auto 0 0;border-radius:99px}
.mapside .prow .num{text-align:right;font-family:ui-monospace,monospace;color:var(--txt2)}
.mlist{margin-top:12px;border-top:1px solid var(--line);padding-top:9px}
.mlist .row{display:grid;grid-template-columns:20px minmax(84px,1fr) 1fr 56px;gap:9px;align-items:center;
padding:5px 0;font-size:12.5px;border-bottom:1px solid var(--line)}
.mlist .row:last-child{border-bottom:0}
.mlist .row .nm b{font-weight:600}
.mlist .row .nm span{color:var(--muted);font-size:11px;margin-left:6px}
.mlist .row .num{text-align:right;font-family:ui-monospace,monospace}
.mapbox svg,.mlist .row,.mapside .prow{min-width:0}
@media (max-width:620px){
  .mapbox{flex:1 1 100%;max-width:100%}
  .mapside .prow{grid-template-columns:96px 1fr 96px}
}
/* 極窄螢幕（手機）先把民調／融合兩欄收起來，避免表格撐破卡片 */
@media (max-width:520px){
  .cand{grid-template-columns:1.6fr .8fr .8fr .8fr}
  .cand>*:nth-child(3),.cand>*:nth-child(4){display:none}
}
.pchip{display:inline-flex;align-items:center;padding:1px 8px;border-radius:99px;font-size:10.5px;
color:#fff;background:var(--pc,#8794ad);margin-left:6px;white-space:nowrap}
.mhint{font-size:12px;color:var(--muted);margin-top:9px}
.foot{margin-top:46px;padding-top:18px;border-top:1px solid var(--line);color:var(--dim);font-size:11.5px}
@media print{
  :root{--bg:#fff;--panel:#fff;--panel2:#f7f9fc;--txt:#111;--txt2:#333;--muted:#555;--dim:#777;
  --line:rgba(0,0,0,.14)}
  body{background:#fff;color:#111}
  .card,.kpi{break-inside:avoid;page-break-inside:avoid}
  h2{break-after:avoid}
}
"""


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _party_map_section(regions: list[dict[str, Any]], s) -> str:
    """政黨版圖：地圖資料放在 web/static/taiwan-map.js，這裡把它內嵌進來，
    讓報告單檔離線也能縮放與點擊。"""
    from .paths import ROOT

    asset = ROOT / "web" / "static" / "taiwan-map.js"
    try:
        map_js = asset.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not map_js.startswith("window.TW_MAP"):
        return ""

    payload: dict[str, Any] = {}
    for r in regions:
        cands = [c for c in r["candidates"] if (c.get("market") or 0) >= 0.002 or c.get("leader")]
        top = r["candidates"][0]
        runner = r["candidates"][1] if len(r["candidates"]) > 1 else None
        payload[r["code"]] = {
            "name": r["name"],
            "party": top.get("party") or "",
            "color": top.get("party_color") or "#8794da",
            "leader": top.get("name"),
            "prob": top.get("fused"),
            "runner": (runner or {}).get("name") or "",
            "runner_prob": (runner or {}).get("fused"),
            "margin": max(((top.get("fused") or 0) - ((runner or {}).get("fused") or 0)), 0.0),
            "candidates": [
                {
                    "name": c.get("name"),
                    "party": c.get("party") or "",
                    "color": c.get("party_color") or "#8794da",
                    "role": c.get("office") or c.get("experience") or "",
                    "fused": c.get("fused"),
                    "market": c.get("market"),
                }
                for c in cands
            ],
        }

    # 全島政黨分布（席次期望）
    seats: dict[str, dict[str, Any]] = {}
    for code, info in payload.items():
        for c in info["candidates"]:
            key = c["party"] or "无党籍/未标注"
            bucket = seats.setdefault(key, {"party": key, "color": c["color"], "seats": 0.0, "leads": 0, "regions": 0})
            if c.get("fused") is not None:
                bucket["seats"] += max(c["fused"], 0.0)
        party = info["party"] or "无党籍/未标注"
        if party in seats:
            seats[party]["leads"] += 1
        for c in info["candidates"]:
            seats[c["party"] or "无党籍/未标注"]["regions"] = seats[
                c["party"] or "无党籍/未标注"
            ].get("regions", 0)
    for key, bucket in seats.items():
        bucket["regions"] = len([1 for i in payload.values() if any((c["party"] or "无党籍/未标注") == key for c in i["candidates"])])
    party_rows = sorted(seats.values(), key=lambda b: -b["seats"])
    total = max(len(payload), 1)

    rows = "".join(
        '<div class="prow"><div class="nm"><i style="background:{c}"></i><span>{p}</span></div>'
        '<div class="pb"><i style="width:{w:.1f}%;background:{c}"></i></div>'
        '<div class="num">{seats:.1f} 席 · {leads} 区领先</div></div>'.format(
            c=b["color"], p=s(b["party"]), w=min(b["seats"] / total * 100, 100), seats=b["seats"], leads=b["leads"]
        )
        for b in party_rows
    )
    legend = "".join(
        '<span><i style="background:{}"></i>{} {} 区领先</span>'.format(b["color"], s(b["party"]), b["leads"])
        for b in party_rows[:4]
    ) + '<span><i style="background:#39435c"></i>无数据</span>'

    hint = (
        '<div class="mhint">滚轮缩放 · 拖动平移 · 点击片区查看该区候选人与支持度。'
        '颜色＝该区融合概率最高候选人所属政党（蓝＝中国国民党阵营，绿＝民主进步党阵营，'
        '青＝台湾民众党，灰＝无党籍／未标注）。金门、马祖离本岛太远，以下方图框示意。</div>'
    )

    return f"""
<h2>四、政党版图与政党分布</h2>
<div class="card">
  <div class="maprow">
    <div class="mapbox" id="rpMapBox">
      <svg id="rpMap" role="img" aria-label="台湾 22 县市政党版图"></svg>
      <div class="mapctl">
        <button type="button" id="rpIn" title="放大">＋</button>
        <button type="button" id="rpOut" title="缩小">－</button>
        <button type="button" id="rpReset" title="还原">⟲</button>
      </div>
      <div class="maplegend">{legend}</div>
    </div>
    <div class="mapside">
      <b style="font-size:13px">各党候选人规模与期望席次</b>
      <div class="muted" style="margin:4px 0 10px">期望席次＝各县市该党候选人融合概率之和，22 县市上限 22 席。</div>
      {rows}
      <div id="rpDetail"></div>
    </div>
  </div>
  {hint}
</div>
<script>{map_js}</script>
<script>
window.RP_DATA={json.dumps(payload, ensure_ascii=False)};
(function(){{
  var MAP=window.TW_MAP, DATA=window.RP_DATA, svg=document.getElementById('rpMap');
  if(!MAP||!svg) return;
  var box=document.getElementById('rpMapBox'), g, state={{k:1,tx:0,ty:0,drag:null,sel:''}};
  var byCode={{}}; MAP.regions.forEach(function(r){{ if(r.code) byCode[r.code]=r; }});
  svg.setAttribute('viewBox','0 0 '+MAP.width+' '+MAP.height);
  var parts=(MAP.insets||[]).map(function(b){{
    return '<rect x="'+(b.x-4)+'" y="'+(b.y-4)+'" width="'+(b.w+8)+'" height="'+(b.h+8)+'" rx="9" '
      +'fill="rgba(8,13,24,.42)" stroke="rgba(118,160,255,.2)" stroke-dasharray="4 4"></rect>';
  }});
  MAP.regions.forEach(function(r){{
    var info=DATA[r.code]||{{}};
    parts.push('<path d="'+r.d+'" data-code="'+(r.code||'')+'" fill="'+(info.color||'#39435c')+'"></path>');
  }});
  MAP.regions.forEach(function(r){{
    var lb=r.label||[0,0];
    parts.push('<text x="'+lb[0]+'" y="'+lb[1]+'">'+(r.name||'').replace(/[市县]$/,'')+'</text>');
  }});
  svg.innerHTML='<g id="rpZoom">'+parts.join('')+'</g>';
  g=document.getElementById('rpZoom');
  function apply(){{ g.setAttribute('transform','translate('+state.tx.toFixed(2)+','+state.ty.toFixed(2)+') scale('+state.k.toFixed(3)+')'); }}
  function clamp(){{ state.tx=Math.min(0,Math.max(MAP.width-MAP.width*state.k,state.tx));
    state.ty=Math.min(0,Math.max(MAP.height-MAP.height*state.k,state.ty)); }}
  function zoomAt(px,py,f){{ var k2=Math.min(8,Math.max(1,state.k*f)), r=k2/state.k;
    state.tx=px-(px-state.tx)*r; state.ty=py-(py-state.ty)*r; state.k=k2; clamp(); apply(); }}
  function center(){{ return [(MAP.width/2-state.tx)/state.k,(MAP.height/2-state.ty)/state.k]; }}
  function pick(code){{
    state.sel=code;
    Array.prototype.forEach.call(svg.querySelectorAll('path[data-code]'),function(p){{
      p.classList.toggle('on',p.getAttribute('data-code')===code);
      p.classList.toggle('dim',!!code&&p.getAttribute('data-code')!==code);
    }});
    var d=DATA[code], el=document.getElementById('rpDetail');
    if(!d){{ el.innerHTML=''; return; }}
    var html='<div class="mlist"><div style="font-size:13px;font-weight:600;margin-bottom:4px">'+d.name
      +'<span class="pchip" style="--pc:'+d.color+'">'+d.party+'</span>'
      +'<span class="muted" style="font-size:11.5px;margin-left:8px">融合领先 '+d.leader+' '
      +(d.prob==null?'—':(d.prob*100).toFixed(1)+'%')
      +(d.runner?' · 次位 '+d.runner+' '+(d.runner_prob*100).toFixed(1)+'%':'')
      +' · 差距 '+(d.margin*100).toFixed(1)+'%</span></div>';
    d.candidates.forEach(function(c,i){{
      var v=(c.fused==null?c.market:c.fused)||0;
      html+='<div class="row"><div class="num">'+(i+1)+'</div>'
        +'<div class="nm"><b>'+c.name+'</b><span class="pchip" style="--pc:'+c.color+'">'+c.party+'</span>'
        +(c.role?'<span>'+c.role+'</span>':'')+'</div>'
        +'<div class="pb" style="height:6px"><i style="width:'+(v*100).toFixed(1)+'%;background:'+c.color+'"></i></div>'
        +'<div class="num">'+(v*100).toFixed(1)+'%</div></div>';
    }});
    el.innerHTML=html+'</div>';
  }}
  svg.addEventListener('wheel',function(e){{
    e.preventDefault();
    var r=svg.getBoundingClientRect();
    var px=((e.clientX-r.left)/r.width*MAP.width-state.tx)/state.k;
    var py=((e.clientY-r.top)/r.height*MAP.height-state.ty)/state.k;
    zoomAt(px,py,e.deltaY<0?1.16:1/1.16);
  }},{{passive:false}});
  svg.addEventListener('pointerdown',function(e){{
    state.drag={{x:e.clientX,y:e.clientY,tx:state.tx,ty:state.ty,moved:0}};
    box.classList.add('drag'); try{{ svg.setPointerCapture(e.pointerId); }}catch(err){{}}
  }});
  svg.addEventListener('pointermove',function(e){{
    var d=state.drag; if(!d) return;
    var r=svg.getBoundingClientRect(), k=MAP.width/r.width;
    d.moved=Math.max(d.moved,Math.hypot(e.clientX-d.x,e.clientY-d.y));
    state.tx=d.tx+(e.clientX-d.x)*k; state.ty=d.ty+(e.clientY-d.y)*k; clamp(); apply();
  }});
  svg.addEventListener('pointerup',function(e){{
    var d=state.drag; state.drag=null; box.classList.remove('drag');
    if(!d||d.moved>5) return;
    var el=document.elementFromPoint(e.clientX,e.clientY);
    var p=el&&el.closest?el.closest('path[data-code]'):null;
    if(p&&p.getAttribute('data-code')) pick(p.getAttribute('data-code'));
  }});
  svg.addEventListener('dblclick',function(){{ state.k=1;state.tx=0;state.ty=0;apply(); }});
  document.getElementById('rpIn').onclick=function(){{ var c=center(); zoomAt(c[0],c[1],1.3); }};
  document.getElementById('rpOut').onclick=function(){{ var c=center(); zoomAt(c[0],c[1],1/1.3); }};
  document.getElementById('rpReset').onclick=function(){{ state.k=1;state.tx=0;state.ty=0;apply(); }};
}})();
</script>
"""


def build_html(payload: dict[str, Any]) -> str:
    from .zhconv import is_simplified_preferred, neutralize_scope, to_simplified
    from .zhconv import deep_simplify

    settings = payload["settings"]
    regions = _region_bundle(payload)
    days = payload["days"]
    stats = payload["stats"]
    countdown = _election_countdown(settings)
    points = _summary_points(regions, payload)

    def s(text: Any) -> str:
        return _esc(to_simplified(neutralize_scope(str(text if text is not None else ""))))

    nationwide = next((r for r in regions if r["code"] == "TW"), None)
    local = [r for r in regions if r["code"] != "TW"]

    kpis = [
        ("投票日", settings.election_date or "—", f"倒數 {countdown} 天" if countdown and countdown > 0 else "已投票"),
        ("涵蓋選區", f"{len(local)} / 22", "縣市長選舉"),
        ("情報總量", f"{stats.get('news_total', 0)} 則", f"近 {days} 天 {stats.get('articles', 0)} 則 → {stats.get('events', 0)} 個事件"),
        ("已分析", f"{stats.get('news_analyzed', 0)} 則", "规则舆情"),
        ("追蹤市場", f"{stats.get('candidate_markets', 0)} 個", f"{stats.get('odds_events', 0)} 個事件盤"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="lab">{s(a)}</div><div class="val">{s(b)}</div>'
        f'<div class="hint">{s(c)}</div></div>'
        for a, b, c in kpis
    )

    lede = (
        f"本報告以 {stats.get('news_total', 0)} 則新聞、{stats.get('odds_snapshots', 0)} 次 Polymarket 賠率快照、"
        f"{len(payload['polls'].get('raw') or {})} 個縣市的民調彙整與 2018/2022 選舉結果為基礎，"
        f"對 {len(local)} 個縣市長選區給出融合預測、輿情走向與逐區解讀。"
    )
    if nationwide:
        top = nationwide["candidates"][0]
        lede += (
            f" 全島政黨賠率方面，市場與模型都指向{top['name']}"
            f"（市場 {_pct(top.get('market'))}、融合 {_pct(top.get('fused'))}）。"
        )

    # 逐區
    region_cards = []
    for r in local:
        top = r["candidates"][0]
        runner = r["candidates"][1] if len(r["candidates"]) > 1 else None
        tags = []
        if r.get("tossup"):
            tags.append('<span class="tag hot">膠著</span>')
        if r.get("poll_leader") and r["poll_leader"] != top["name"]:
            tags.append('<span class="tag flip">民調分歧</span>')
        if top.get("market") is not None and top.get("fused") is not None and top["fused"] - top["market"] >= 0.05:
            tags.append('<span class="tag ok">模型上修</span>')

        rows_html = []
        shown = [c for c in r["candidates"] if (c.get("market") or 0) >= 0.005 or c["leader"]][:5]
        for c in shown:
            img = (
                '<img src="{}" alt="" loading="lazy" onerror="this.remove()">'.format(_esc(c["image"]))
                if c.get("image") else ""
            )
            avatar = '<span class="av">{}{}</span>'.format(img, s((c["name"] or "?")[:1]))
            # 現職優先；維基沒寫現職時退用名冊的「經歷」，避免這行只剩黨籍
            role = c.get("office") or c.get("experience") or ""
            sub = " · ".join(x for x in (s(c.get("party")), s(role)) if x and x != "—")
            sub_html = '<br><span class="dim" style="font-size:11px">{}</span>'.format(sub) if sub else ""
            rows_html.append(
                '<div class="cand">'
                f'<div class="n">{avatar}<span><b>{s(c["name"])}</b>{sub_html}</span></div>'
                f'<div class="num">{_pct(c.get("market"))}</div>'
                f'<div class="num">{_num(c.get("poll"), 1, "%")}</div>'
                f'<div class="num" style="color:var(--cyan);font-weight:600">{_pct(c.get("fused"))}</div>'
                "</div>"
            )
        hidden = len(r["candidates"]) - len(shown)
        hidden_note = (
            f'<div class="muted" style="margin-top:6px">另有 {hidden} 位登記候選人市場機率低於 0.5%，未列出'
            + (f"（本區名冊共 {len(r['registered'])} 人）" if r.get("registered") else "")
            + "。</div>"
            if hidden > 0 else ""
        )

        region_cards.append(
            '<div class="card">'
            f'<div class="lead"><h3 style="margin:0">{s(r["name"])}</h3>'
            f'<span class="tag">{s(top["name"])} 融合 {_pct(top.get("fused"))}</span>'
            + "".join(tags) + "</div>"
            f'{_bar_pair(top.get("market"), top.get("fused"))}'
            f'<div class="muted" style="margin-top:8px;font-size:12.5px">{s(_narrative(r, payload))}</div>'
            '<div style="margin-top:11px">'
            '<div class="cand" style="color:var(--muted);font-size:11px;border-bottom:1px solid var(--line)">'
            "<div>候選人</div><div class=\"num\">市場</div><div class=\"num\">民調</div>"
            "<div class=\"num\">融合</div></div>"
            + "".join(rows_html) + hidden_note + "</div></div>"
        )

    # 熱點事件
    hot_parts = []
    for e in payload["hot_events"]:
        title = s(e["title"])
        if e.get("url"):
            title = '<a href="{}" target="_blank" rel="noopener">{}</a>'.format(_esc(e["url"]), title)
        summary = s(e.get("summary") or "")
        # 短新聞的摘要常常就是標題本身，重複顯示沒有意義
        norm = lambda t: "".join(ch for ch in t if ch.isalnum())
        if summary and norm(summary) and norm(summary) not in norm(s(e["title"])):
            title += '<br><span class="dim" style="font-size:11.5px">{}</span>'.format(summary[:80])
        sent = "—" if e.get("sentiment") is None else f'{e["sentiment"]:.2f}'
        hot_parts.append(
            '<tr>'
            f'<td style="width:52px" class="num">{e["size"]} 家</td>'
            f'<td>{s(e["date"])}｜{title}</td>'
            f'<td style="width:110px" class="muted">{s(e["source"])}</td>'
            f'<td style="width:64px" class="num">{sent}</td>'
            "</tr>"
        )
    hot_html = "".join(hot_parts) or (
        '<tr><td colspan="4" class="muted">近期沒有多來源共同報導的事件。</td></tr>'
    )

    topics_html = "".join(
        f'<span class="tag">{s(name)} × {count}</span>' for name, count in payload["topics"]
    ) or '<span class="muted">尚無議題標籤。</span>'

    calib = payload["calibration"] or {}
    calib_rows = "".join(
        f'<tr><td class="num">{k}</td><td class="num">{float(v) * 100:.1f}%</td></tr>'
        for k, v in (calib.get("model", {}).get("curve") or {}).items()
    )
    metrics = calib.get("metrics") or {}

    media_note = ""
    media_meta = (payload["polls_media"] or {}).get("meta") or {}
    media_rows = (payload["polls_media"] or {}).get("polls") or []
    if media_rows:
        recent = sorted(media_rows, key=lambda p: p.get("date") or "", reverse=True)[:6]
        media_note = (
            f'<div class="note" style="margin-top:11px">本報告的民調除維基百科彙整表外，'
            f'另含 {len(media_rows)} 筆從當週新聞標題抽取的民調（最近 {media_meta.get("window_days", 14)} 天），'
            f'用以補足彙整表更新落後的問題。最新幾筆：'
            + "；".join(
                f'{s(p.get("date"))} {s(p.get("region"))} {s(p.get("source"))}'
                for p in recent
            )
            + "。</div>"
        )

    return f"""<!doctype html>
<html lang="zh-Hans"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>2026 九合一選情分析報道 · {s(payload['generated_at'].strftime('%Y-%m-%d'))}</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>2026 九合一選舉 · 選情分析報道</h1>
<div class="meta">生成時間 {s(payload['generated_at'].strftime('%Y-%m-%d %H:%M'))}（UTC+8）·
選舉：{s(settings.election)} · 投票日 {s(settings.election_date)}</div>
<p class="lede">{s(lede)}</p>
<div class="kpis">{kpi_html}</div>

<h2>一、本期要點</h2>
<div class="card"><ul class="pts">{"".join(f"<li>{s(p)}</li>" for p in points)}</ul></div>

<h2>二、輿情走向：近 {days} 天</h2>
<div class="card">
  <div class="muted" style="margin-bottom:6px">柱狀為每日事件數，折線為當日加權情感（-1 至 +1，越高越正面）。</div>
  {_trend_svg(payload["trend"])}
  <div style="margin-top:12px"><b style="font-size:13px">熱門議題</b><div style="margin-top:7px">{topics_html}</div></div>
</div>

<h2>三、最多媒体报道的事件</h2>
<div class="card"><table><thead><tr>
<th class="num">媒体数</th><th>事件</th><th>主要来源</th><th class="num">情感</th>
</tr></thead><tbody>{hot_html}</tbody></table>
<div class="muted" style="margin-top:9px">同一件事经事件去重后只计一次，这里列出被最多家媒体报道者，代表议题热度。</div></div>

<h2>四、逐區解讀與候選人選項</h2>
<div class="grid2">{"".join(region_cards)}</div>

<h2>五、方法與資料來源</h2>
<div class="card">
  <div class="muted">融合機率以 Polymarket 市場機率為主參考，加上兩個有上限的相對修正：
  民調（按對戰組合加權，並用歷史回測曲線把領先幅度轉成勝率）、
  以及新聞輿情情感。PTT、維基百科關注度只作背景參考，不改變預測方向。
  新文易數的事件追蹤、媒體覆蓋與議題關係圖，以及 Google Trends 台灣當前
  熱搜 Top 10，只作新聞證據與議題核對，不進入勝率公式。</div>
  <div class="grid2" style="margin-top:13px">
    <div>
      <b style="font-size:13px">民調領先幅度 → 勝率（回測校準）</b>
      <table style="margin-top:6px"><thead><tr><th class="num">領先（百分點）</th><th class="num">勝率</th></tr></thead>
      <tbody>{calib_rows or '<tr><td colspan="2" class="muted">尚未執行回測</td></tr>'}</tbody></table>
      <div class="muted" style="margin-top:7px">以 2018/2022 共 {metrics.get('n', '—')} 組樣本擬合，
      準確率 {metrics.get('accuracy') and f"{metrics['accuracy'] * 100:.1f}%"}，
      Brier {metrics.get('brier', '—')}（基準 {metrics.get('baseline_brier', '—')}）。</div>
    </div>
    <div>
      <b style="font-size:13px">資料來源</b>
      <div class="muted" style="margin-top:7px">
        賠率與盤口：Polymarket Gamma / CLOB / data-api（價差、掛單深度、逐筆成交、持倉集中度）<br>
        新聞：Google 新聞 RSS（繁中／簡中／英文）與中央社、自由時報、ETtoday、Yahoo 等 RSS；近30天新聞去重後做情感、議題、候選人與縣市歸因<br>
        民調：22縣市民調彙整表 + 近14天新聞標題民調抽取<br>
        補充證據：新文易數事件串與媒體覆蓋、PTT政黑板、維基百科瀏覽量、Google Trends台灣當前熱搜<br>
        更新方式：完整抓取後重新計算市場、民調、新聞與模型；外部熱搜與事件關係不參與方向計算
      </div>
    </div>
  </div>
  {media_note}
</div>

<div class="foot">
  本報告由本地監測系統自動生成，資料為公開資訊的程式化整理與模型推算，不構成任何投資或投票建議。
  市場價格為預測市場交易結果，與實際選舉結果可能不同。
  模型輸出精確到小數點不代表同等精確的預測能力。
</div>
</div></body></html>"""


def _finalize(html_text: str) -> str:
    """整篇一起做繁轉簡＋詞彙本地化，避免資料簡體、標題繁體的混雜。"""
    from .zhconv import is_simplified_preferred, localize, neutralize_scope

    if is_simplified_preferred():
        return neutralize_scope(localize(html_text))
    return html_text


def build_markdown(payload: dict[str, Any]) -> str:
    from .zhconv import neutralize_scope, to_simplified

    s = lambda v: neutralize_scope(to_simplified(str(v if v is not None else "")))
    regions = _region_bundle(payload)
    local = [r for r in regions if r["code"] != "TW"]
    settings = payload["settings"]
    lines = [
        "# 2026 九合一選舉 · 選情分析報道",
        "",
        f"- 生成時間：{payload['generated_at'].strftime('%Y-%m-%d %H:%M')}（UTC+8）",
        f"- 投票日：{s(settings.election_date)}（倒數 {_election_countdown(settings)} 天）",
        f"- 情報規模：近 {payload['days']} 天 {payload['stats'].get('articles', 0)} 則新聞，"
        f"去重後 {payload['stats'].get('events', 0)} 個事件",
        "",
        "## 一、本期要點",
        "",
    ]
    lines += [f"- {s(p)}" for p in _summary_points(regions, payload)]
    lines += ["", "## 二、輿情走向", "", "| 日期 | 事件數 | 加權情感 |", "|---|---|---|"]
    for row in payload["trend"]:
        sent = "—" if row["sentiment"] is None else f"{row['sentiment']:+.2f}"
        lines.append(f"| {row['date']} | {row['events']} | {sent} |")
    lines += ["", "熱門議題：" + "、".join(f"{s(n)}（{c}）" for n, c in payload["topics"]), ""]
    lines += ["## 三、逐區解讀", ""]
    for r in local:
        top = r["candidates"][0]
        lines += [
            f"### {s(r['name'])}：{s(top['name'])} 融合 {_pct(top.get('fused'))}"
            f"（市場 {_pct(top.get('market'))}）",
            "",
            s(_narrative(r, payload)),
            "",
            "| 候選人 | 黨籍 | 市場 | 民調 | 融合 |",
            "|---|---|---|---|---|",
        ]
        for c in r["candidates"][:5]:
            lines.append(
                f"| {s(c['name'])} | {s(c.get('party'))} | {_pct(c.get('market'))} | "
                f"{_num(c.get('poll'), 1, '%')} | {_pct(c.get('fused'))} |"
            )
        lines.append("")
    lines += ["## 四、方法與資料來源", "",
              "融合機率以 Polymarket 市場機率為主參考，加入民調（按對戰組合加權並用回測曲線校準）",
              "與新聞輿情兩個有上限的相對修正；關注度不進入勝率。", "",
              "新聞來源包括 Google 新聞繁中、簡中、英文搜尋，以及中央社、自由時報、ETtoday、Yahoo等 RSS。",
              "民調來源包括22縣市民調彙整表與近14天新聞標題抽取。",
              "新文易數的事件追蹤、媒體覆蓋與議題關係圖，以及 Google Trends 台灣當前 Top 10 熱搜，",
              "目前只作新聞證據與議題核對，不進入勝率公式。PTT與維基瀏覽量不改變方向。",
              "新聞的候選人、議題、情感與影響強度由本地規則引擎分析。", "",
              "本報告為公開資訊的程式化整理與模型推算，不構成投資或投票建議。", ""]
    return "\n".join(lines)


def generate(*, days: int = 21) -> dict[str, str]:
    """產生正式 HTML、Word 與 Markdown 分析報道，回傳檔案路徑。"""
    ensure_dirs()
    payload = collect_payload(days=days)
    from .formal_report import (
        build_charts,
        build_docx,
        build_html as build_formal_html,
        build_markdown as build_formal_markdown,
    )
    from . import report_llm

    charts = build_charts(payload, EXPORT_DIR / "report-assets")
    narrative, llm_meta = report_llm.generate(payload, payload["settings"])
    html_text = _finalize(build_formal_html(payload, charts, narrative))
    md_text = _finalize(build_formal_markdown(payload, narrative))
    html_path = EXPORT_DIR / "report.html"
    md_path = EXPORT_DIR / "report.md"
    docx_path = EXPORT_DIR / "report.docx"
    html_path.write_text(html_text, encoding="utf-8")
    md_path.write_text(md_text, encoding="utf-8")
    build_docx(payload, charts, docx_path, narrative)
    stamp = payload["generated_at"].strftime("%Y%m%d-%H%M")
    (EXPORT_DIR / f"report-{stamp}.html").write_text(html_text, encoding="utf-8")
    (EXPORT_DIR / "report-llm-meta.json").write_text(
        json.dumps(llm_meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (EXPORT_DIR / "report-llm-content.json").write_text(
        json.dumps(narrative or {}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    import shutil

    shutil.copy2(docx_path, EXPORT_DIR / f"report-{stamp}.docx")
    return {
        "html": str(html_path),
        "docx": str(docx_path),
        "markdown": str(md_path),
        "llm_used": bool(llm_meta.get("used")),
        "llm_model": llm_meta.get("model"),
        "llm_reason": llm_meta.get("reason"),
        "regions": str(len([r for r in _region_bundle(payload) if r["code"] != "TW"])),
    }
