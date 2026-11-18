"""Polymarket 22县市候选人赔率异动与前后新闻。"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .http import encode_query, fetch_json
from .marketdeep import UA as MARKET_UA
from .paths import DATA_DIR
from .roster import load_roster
from .zhconv import to_simplified

RECOGNITION_THRESHOLD_PP = 0.02
THRESHOLD_PP = 0.05
SIGNIFICANT_THRESHOLD_PP = 0.08
MAJOR_THRESHOLD_PP = 0.10
THIN_MARKET_THRESHOLD_PP = 0.08
THIN_MARKET_MIN_LIQUIDITY = 3000
THIN_MARKET_MIN_VOLUME = 5000
THIN_PERSISTENCE_RATIO = 0.50
CUMULATIVE_WINDOW_HOURS = 24
DEFAULT_WINDOW_HOURS = 720
WINDOW_OPTIONS_HOURS = (24, 72, 168, 360, 720)
NEWS_WINDOW_HOURS = 24
CACHE_SECONDS = 300
SIX_METRO_CODES = ("TPE", "NTPC", "TYN", "TXG", "TNN", "KHH")
HISTORY_CACHE_DIR = DATA_DIR / "cache" / "market_movers"
TAIWAN_TZ = timezone(timedelta(hours=8))
_MEMORY: dict[str, Any] = {"ts": {}, "payloads": {}}


def _name(value: Any) -> str:
    return to_simplified(str(value or "")).replace("臺", "台").strip()


def _when(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _roster_lookup() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_en: dict[str, dict[str, Any]] = {}
    by_zh: dict[str, dict[str, Any]] = {}
    for region, entries in (load_roster().get("regions") or {}).items():
        for entry in entries or []:
            row = {
                "name": _name(entry.get("name")),
                "name_en": str(entry.get("en") or "").strip(),
                "party": _name(entry.get("party")),
                "region": _name(region),
            }
            if row["name_en"]:
                by_en[row["name_en"].casefold()] = row
            if row["name"]:
                by_zh[row["name"]] = row
    return by_en, by_zh


def _market_url(row: Any) -> str:
    event_slug = str(row["event_slug"] or "").strip()
    return f"https://polymarket.com/event/{event_slug}" if event_slug else ""


def _latest_market_rows(rows: list[Any]) -> dict[tuple[str, str], Any]:
    latest: dict[tuple[str, str], Any] = {}
    for row in rows:
        if str(row["region_code"] or "") == "TW":
            continue
        key = (str(row["region_code"] or ""), str(row["candidate_en"] or ""))
        previous = latest.get(key)
        if previous is None or str(row["ts"] or "") > str(previous["ts"] or ""):
            latest[key] = row
    return latest


def _candidate_group_meta(
    row: Any,
    by_en: dict[str, dict[str, Any]] | None = None,
    by_zh: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    by_en = by_en if by_en is not None else _roster_lookup()[0]
    by_zh = by_zh if by_zh is not None else _roster_lookup()[1]
    key = (str(row["region_code"] or ""), str(row["candidate_en"] or ""))
    roster = by_en.get(key[1].casefold()) or by_zh.get(_name(row["candidate_zh"])) or {}
    return {
        "key": f"{key[0]}|{key[1]}",
        "region_code": key[0],
        "region_name": _name(row["region_name"]),
        "candidate_en": key[1],
        "candidate_zh": roster.get("name") or _name(row["candidate_zh"]),
        "party": roster.get("party") or "",
        "market_url": _market_url(row),
        "volume": float(row["volume"] or 0),
        "liquidity": float(row["liquidity"] or 0),
        "market_state": _market_state(
            float(row["volume"] or 0),
            float(row["liquidity"] or 0),
        ),
        "current_price": float(row["yes_price"]) if row["yes_price"] is not None else None,
        "latest_at": str(row["ts"] or ""),
    }


def _candidate_match(text: str, candidate_name: str, candidate_en: str) -> bool:
    haystack = _name(text).casefold()
    name = _name(candidate_name)
    english = str(candidate_en or "").strip().casefold()
    return bool((name and name.casefold() in haystack) or (english and english in haystack))


def _market_is_thin(volume: float, liquidity: float) -> bool:
    return liquidity < THIN_MARKET_MIN_LIQUIDITY or volume < THIN_MARKET_MIN_VOLUME


def _market_state(volume: float, liquidity: float) -> str:
    return (
        "盘口较薄，容易受少量资金影响"
        if _market_is_thin(volume, liquidity)
        else "盘口有一定成交与流动性"
    )


def _classify_event(
    event: dict[str, Any],
    *,
    next_price: float | None,
) -> dict[str, Any]:
    volume = float(event.get("volume") or 0)
    liquidity = float(event.get("liquidity") or 0)
    change = float(event.get("change") or 0)
    magnitude = abs(change)
    thin = _market_is_thin(volume, liquidity)
    event["market_quality"] = "thin" if thin else "normal"
    event["market_state"] = _market_state(volume, liquidity)

    retained_ratio = None
    confirmation = "正常盘口，达到2个百分点识别阈值"
    if thin:
        if next_price is not None and magnitude > 0:
            retained_ratio = (
                (float(next_price) - float(event["previous_price"])) / change
            )
        persisted = retained_ratio is not None and retained_ratio >= THIN_PERSISTENCE_RATIO
        confirmed = magnitude >= THIN_MARKET_THRESHOLD_PP and persisted
        confirmation = (
            "薄盘口，下一价格点至少保留本次变动的一半"
            if confirmed
            else "薄盘口单次跳点，等待下一价格点确认"
        )
    else:
        confirmed = magnitude >= RECOGNITION_THRESHOLD_PP

    recognized = magnitude >= RECOGNITION_THRESHOLD_PP
    if not confirmed:
        level, label = "watch", "待确认异动"
    elif magnitude >= MAJOR_THRESHOLD_PP:
        level, label = "major", "重大异动"
    elif magnitude >= SIGNIFICANT_THRESHOLD_PP:
        level, label = "significant", "显著异动"
    elif magnitude >= THRESHOLD_PP:
        level, label = "normal", "一般异动"
    else:
        level, label = "minor", "小幅异动"

    event["recognized"] = recognized
    event["confirmed"] = confirmed
    event["alert_eligible"] = recognized
    event["alert_level"] = level
    event["alert_label"] = label
    event["confirmation"] = confirmation
    event["retained_ratio"] = (
        round(float(retained_ratio), 3) if retained_ratio is not None else None
    )
    return event


def _snapshot_event(
    previous: Any,
    current: Any,
    next_price: float | None,
) -> dict[str, Any] | None:
    try:
        before = float(previous["yes_price"])
        after = float(current["yes_price"])
    except (TypeError, ValueError):
        return None
    observed = _when(current["ts"])
    previous_at = _when(previous["ts"])
    if not observed or not previous_at:
        return None
    event = {
        "key": f"{current['region_code']}|{current['candidate_en']}",
        "region_code": str(current["region_code"] or ""),
        "region_name": _name(current["region_name"]),
        "candidate_en": str(current["candidate_en"] or ""),
        "candidate_zh": _name(current["candidate_zh"]),
        "previous_price": before,
        "current_price": after,
        "change": after - before,
        "observed_at": observed,
        "previous_at": previous_at,
        "volume": float(current["volume"] or 0),
        "liquidity": float(current["liquidity"] or 0),
        "market_url": _market_url(current),
        "window_hours": round((observed - previous_at).total_seconds() / 3600, 1),
        "data_source": "本地快照",
    }
    return _classify_event(event, next_price=next_price)


def _optional_price(row: Any) -> float | None:
    try:
        value = row["yes_price"]
        return float(value) if value is not None else None
    except (KeyError, TypeError, ValueError):
        return None


def _snapshot_events(
    days: int = 30,
    rows: list[Any] | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> list[dict[str, Any]]:
    by_market: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in (rows if rows is not None else db.fetch_odds_since(days=days)):
        if str(row["region_code"] or "") == "TW":
            continue
        by_market[(str(row["region_code"] or ""), str(row["candidate_en"] or ""))].append(row)

    raw_events = []
    for rows in by_market.values():
        rows.sort(key=lambda item: str(item["ts"] or ""))
        for index, (previous, current) in enumerate(zip(rows, rows[1:])):
            event = _snapshot_event(
                previous,
                current,
                _optional_price(rows[index + 2]) if index + 2 < len(rows) else None,
            )
            if not event or abs(float(event["change"])) < RECOGNITION_THRESHOLD_PP:
                continue
            raw_events.append(event)

        window_start = 0
        for index, current in enumerate(rows):
            observed = _when(current["ts"])
            if index == 0 or not observed:
                continue
            cutoff = observed - timedelta(hours=CUMULATIVE_WINDOW_HOURS)
            while window_start < index:
                window_at = _when(rows[window_start]["ts"])
                if window_at and window_at >= cutoff:
                    break
                window_start += 1
            previous = rows[window_start]
            event = _snapshot_event(
                previous,
                current,
                _optional_price(rows[index + 1]) if index + 1 < len(rows) else None,
            )
            if not event or abs(float(event["change"])) < RECOGNITION_THRESHOLD_PP:
                continue
            raw_events.append(event)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    raw_events = [event for event in raw_events if event["observed_at"] >= cutoff]
    events = sorted(
        _dedupe_events(raw_events),
        key=lambda item: item["observed_at"],
        reverse=True,
    )
    return events


def _history_points(token_id: str) -> list[dict[str, Any]]:
    cache_path = HISTORY_CACHE_DIR / f"{token_id}.json"

    def read_cache() -> list[dict[str, Any]]:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if str(payload.get("token_id") or "") != token_id:
            return []
        return [
            {"t": int(item["t"]), "p": float(item["p"])}
            for item in (payload.get("points") or [])
            if isinstance(item, dict) and item.get("t") is not None and item.get("p") is not None
        ]

    query = encode_query({
        "market": token_id,
        "interval": "1m",
        "fidelity": 60,
    })
    try:
        data = fetch_json(
            f"https://clob.polymarket.com/prices-history?{query}",
            timeout=20,
            retries=2,
            headers=MARKET_UA,
        )
        points = [
            {"t": int(item["t"]), "p": float(item["p"])}
            for item in (data.get("history") or [])
            if isinstance(item, dict) and item.get("t") is not None and item.get("p") is not None
        ]
        if points:
            HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            temp_path = cache_path.with_suffix(".tmp")
            temp_path.write_text(
                json.dumps(
                    {
                        "token_id": token_id,
                        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "points": points,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temp_path.replace(cache_path)
        return points
    except Exception:  # noqa: BLE001 - 官方接口失败时使用最近一份成功数据
        cached = read_cache()
        if cached:
            return cached
        raise


def _history_event(
    row: Any,
    key: tuple[str, str],
    previous: dict[str, Any],
    current: dict[str, Any],
    next_price: float | None,
) -> dict[str, Any]:
    observed = datetime.fromtimestamp(int(current["t"]), tz=timezone.utc)
    previous_at = datetime.fromtimestamp(int(previous["t"]), tz=timezone.utc)
    event = {
        "key": f"{key[0]}|{key[1]}",
        "region_code": key[0],
        "region_name": _name(row["region_name"]),
        "candidate_en": key[1],
        "candidate_zh": _name(row["candidate_zh"]),
        "previous_price": float(previous["p"]),
        "current_price": float(current["p"]),
        "change": float(current["p"]) - float(previous["p"]),
        "observed_at": observed,
        "previous_at": previous_at,
        "volume": float(row["volume"] or 0),
        "liquidity": float(row["liquidity"] or 0),
        "market_url": _market_url(row),
        "window_hours": round((observed - previous_at).total_seconds() / 3600, 1),
        "data_source": "Polymarket官方历史",
    }
    return _classify_event(event, next_price=next_price)


def _official_points_events(
    row: Any,
    key: tuple[str, str],
    points: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    ordered = sorted(points, key=lambda item: int(item["t"]))

    # 单小时异动：达到2个百分点先进入走势图，再按幅度分级。
    for index, (previous, current) in enumerate(zip(ordered, ordered[1:])):
        change = float(current["p"]) - float(previous["p"])
        if abs(change) < RECOGNITION_THRESHOLD_PP:
            continue
        next_price = float(ordered[index + 2]["p"]) if index + 2 < len(ordered) else None
        events.append(_history_event(row, key, previous, current, next_price))

    # 24小时累计异动：捕捉每小时变化不大但持续同向移动的情况。
    window_start = 0
    for index, current in enumerate(ordered):
        if index == 0:
            continue
        cutoff = int(current["t"]) - CUMULATIVE_WINDOW_HOURS * 3600
        while (
            window_start < index
            and int(ordered[window_start]["t"]) < cutoff
        ):
            window_start += 1
        previous = ordered[window_start]
        if previous is current or int(previous["t"]) == int(current["t"]):
            continue
        change = float(current["p"]) - float(previous["p"])
        if abs(change) < RECOGNITION_THRESHOLD_PP:
            continue
        next_price = float(ordered[index + 1]["p"]) if index + 1 < len(ordered) else None
        events.append(_history_event(row, key, previous, current, next_price))

    return events


def _dedupe_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一候选人按台湾日期只保留绝对变化最大的一笔异动。"""
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        local_day = event["observed_at"].astimezone(TAIWAN_TZ).date().isoformat()
        key = (event["key"], local_day)
        previous = deduped.get(key)
        if previous is None or abs(float(event["change"])) > abs(float(previous["change"])):
            deduped[key] = event
    return list(deduped.values())


def _official_history_events(
    fallback_events: list[dict[str, Any]],
    requested_keys: set[str] | None = None,
    rows: list[Any] | None = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    rows = rows if rows is not None else db.fetch_odds_since(days=30)
    latest: dict[tuple[str, str], Any] = {}
    selected_keys: set[tuple[str, str]] = (
        {
            tuple(key.split("|", 1))
            for key in requested_keys
            if "|" in key
        }
        if requested_keys is not None else {
            (event["region_code"], event["candidate_en"])
            for event in fallback_events
        }
    )
    for row in rows:
        if str(row["region_code"] or "") == "TW":
            continue
        key = (str(row["region_code"] or ""), str(row["candidate_en"] or ""))
        previous = latest.get(key)
        if previous is None or str(row["ts"] or "") > str(previous["ts"] or ""):
            latest[key] = row
        if requested_keys is None and (
            abs(float(row["chg_1d"] or 0)) >= RECOGNITION_THRESHOLD_PP
            or abs(float(row["chg_1w"] or 0)) >= RECOGNITION_THRESHOLD_PP
        ):
            selected_keys.add(key)

    targets = [
        row for key, row in latest.items()
        if key in selected_keys and str(row["clob_token_id"] or "").strip()
    ]

    def fetch(row: Any) -> tuple[Any, list[dict[str, Any]], str]:
        try:
            return row, _history_points(str(row["clob_token_id"])), ""
        except Exception as exc:  # noqa: BLE001
            return row, [], f"{type(exc).__name__}: {exc}"

    events: list[dict[str, Any]] = []
    histories: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for future in as_completed([pool.submit(fetch, row) for row in targets]):
            row, points, error = future.result()
            if error or not points:
                continue
            key = (str(row["region_code"] or ""), str(row["candidate_en"] or ""))
            histories[f"{key[0]}|{key[1]}"] = points
            events.extend(_official_points_events(row, key, points))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    official = [
        event for event in _dedupe_events(events)
        if event["observed_at"] >= cutoff
    ]
    covered = {event["key"] for event in official}
    fallback = [
        {**event, "data_source": event.get("data_source") or "本地快照"}
        for event in fallback_events
        if event["key"] not in covered
    ]
    selected = sorted(
        _dedupe_events([*official, *fallback]),
        key=lambda item: item["observed_at"],
        reverse=True,
    )
    # 官方历史不可用时，用本地快照补一条简化价格线。
    snapshot_points: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        when = _when(row["ts"])
        if not when or when < cutoff or row["yes_price"] is None:
            continue
        snapshot_points[f"{row['region_code']}|{row['candidate_en']}"].append({
            "t": int(when.timestamp()),
            "p": float(row["yes_price"]),
        })
    for region_code, candidate_en in selected_keys:
        key = f"{region_code}|{candidate_en}"
        histories.setdefault(
            key,
            sorted(snapshot_points.get(key, []), key=lambda item: item["t"]),
        )
    return selected, histories


def _news_around_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not events:
        return []
    by_en, by_zh = _roster_lookup()
    oldest = min(event["observed_at"] for event in events) - timedelta(hours=NEWS_WINDOW_HOURS)
    newest = max(event["observed_at"] for event in events) + timedelta(hours=NEWS_WINDOW_HOURS)
    rows = [
        dict(row)
        for row in db.fetch_news_between(
            oldest.isoformat(timespec="seconds"),
            newest.isoformat(timespec="seconds"),
            limit=8000,
        )
        if int(row["is_rep"] or 0) == 1
    ]

    prepared = []
    for row in rows:
        published = _when(row.get("published_at") or row.get("first_seen"))
        if not published:
            continue
        candidate_names = []
        for item in _json_list(row.get("candidates")):
            if isinstance(item, dict):
                value = item.get("name_zh") or item.get("name")
            else:
                value = item
            value = _name(value)
            if value.startswith("local:"):
                value = value.split(":", 1)[1]
            if value:
                candidate_names.append(value)
        prepared.append((published, row, candidate_names))

    for event in events:
        roster = by_en.get(event["candidate_en"].casefold()) or by_zh.get(event["candidate_zh"]) or {}
        if roster:
            event["candidate_zh"] = roster.get("name") or event["candidate_zh"]
            event["party"] = roster.get("party") or ""
        else:
            event["party"] = ""
        event["market_state"] = (
            "盘口较薄，容易受少量资金影响"
            if event["liquidity"] < 3000 or event["volume"] < 5000
            else "盘口有一定成交与流动性"
        )
        start = event["observed_at"] - timedelta(hours=NEWS_WINDOW_HOURS)
        end = event["observed_at"] + timedelta(hours=NEWS_WINDOW_HOURS)
        matches = []
        for published, row, candidate_names in prepared:
            if published < start or published >= end:
                continue
            text = f"{row.get('title') or ''} {row.get('summary') or ''} {row.get('ai_summary') or ''}"
            if not (
                _candidate_match(text, event["candidate_zh"], event["candidate_en"])
                or event["candidate_zh"] in candidate_names
            ):
                continue
            matches.append({
                "title": _name(row.get("title")),
                "url": str(row.get("url") or ""),
                "source_name": str(row.get("source_name") or ""),
                "published_at": published.isoformat(timespec="seconds"),
                "sentiment": row.get("sentiment"),
                "impact": row.get("impact"),
                "_published": published,
            })
        before = [item for item in matches if item["_published"] < event["observed_at"]]
        after = [item for item in matches if item["_published"] >= event["observed_at"]]
        before.sort(key=lambda item: item["_published"], reverse=True)
        after.sort(key=lambda item: item["_published"])
        for item in before + after:
            item.pop("_published", None)
        event["news_before"] = before[:3]
        event["news_after"] = after[:3]
        event["news_count"] = len(matches)
        event["observed_at"] = event["observed_at"].isoformat(timespec="seconds")
        event["previous_at"] = event["previous_at"].isoformat(timespec="seconds")

    return events


def _region_groups(latest_rows: dict[tuple[str, str], Any]) -> list[dict[str, Any]]:
    by_en, by_zh = _roster_lookup()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in latest_rows.values():
        grouped[str(row["region_code"] or "")].append(
            _candidate_group_meta(row, by_en, by_zh)
        )

    regions = []
    for region_code, candidates in grouped.items():
        candidates.sort(
            key=lambda item: (
                -(item.get("current_price") or 0),
                item.get("candidate_zh") or item.get("candidate_en") or "",
            )
        )
        regions.append({
            "code": region_code,
            "name": candidates[0]["region_name"] if candidates else region_code,
            "candidates_count": len(candidates),
            "latest_at": max((item.get("latest_at") or "" for item in candidates), default=""),
            "candidates": candidates,
        })

    metro_rank = {code: index for index, code in enumerate(SIX_METRO_CODES)}
    return sorted(
        regions,
        key=lambda item: (
            metro_rank.get(item["code"], len(SIX_METRO_CODES)),
            item["name"],
        ),
    )


def get_market_movers(
    *,
    force: bool = False,
    candidate_key: str = "",
    region_code: str = "",
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> dict[str, Any]:
    now = time.time()
    window_hours = max(1, min(24 * 30, int(window_hours or DEFAULT_WINDOW_HOURS)))
    if not region_code and "|" in candidate_key:
        region_code = candidate_key.split("|", 1)[0]
    cache_key = f"{region_code or '__default__'}:{window_hours}"
    cached = _MEMORY["payloads"].get(cache_key)
    if not force and cached and now - float(_MEMORY["ts"].get(cache_key) or 0) < CACHE_SECONDS:
        return cached
    rows = db.fetch_odds_since(days=31)
    latest_rows = _latest_market_rows(rows)
    regions = _region_groups(latest_rows)
    selected_region = next(
        (item for item in regions if item["code"] == region_code),
        regions[0] if regions else None,
    )
    region_candidates = (selected_region or {}).get("candidates") or []
    selected_keys = {
        f"{item['region_code']}|{item['candidate_en']}"
        for item in region_candidates
    }
    snapshot_events = _snapshot_events(rows=rows, window_hours=window_hours)
    selected_fallback = [event for event in snapshot_events if event["key"] in selected_keys]
    official_events, histories = _official_history_events(
        selected_fallback,
        selected_keys,
        rows=rows,
        window_hours=window_hours,
    )
    events = _news_around_events(official_events)
    event_cutoff = int(time.time() - window_hours * 3600)
    events_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        events_by_key[event["key"]].append(event)

    candidate_groups = []
    for candidate in region_candidates:
        key = candidate["key"]
        chart_events = sorted(
            events_by_key.get(key, []),
            key=lambda item: item["observed_at"],
            reverse=True,
        )
        alerts = [event for event in chart_events if event.get("alert_eligible")]
        observations = [
            event for event in chart_events
            if event.get("recognized") and not event.get("confirmed")
        ]
        history = [
            point for point in histories.get(key, [])
            if int(point["t"]) >= event_cutoff
        ]
        current_price = (
            float(history[-1]["p"])
            if history
            else candidate.get("current_price")
        )
        group = {
            **candidate,
            "current_price": current_price,
            "history": history,
            "events": alerts,
            "chart_events": chart_events,
            "observations": observations,
            "data_source": (
                "Polymarket官方历史"
                if history
                else "本地赔率快照"
            ),
        }
        candidate_groups.append(group)
    candidate_groups.sort(
        key=lambda item: (
            -(item.get("current_price") or 0),
            item.get("candidate_zh") or item.get("candidate_en") or "",
        )
    )

    alerts = [
        event
        for group in candidate_groups
        for event in group["events"]
    ]
    chart_events = [
        event
        for group in candidate_groups
        for event in group["chart_events"]
    ]
    observations = [
        event
        for group in candidate_groups
        for event in group["observations"]
    ]
    confirmed_events = [event for event in alerts if event.get("confirmed")]
    alerts.sort(key=lambda item: item["observed_at"], reverse=True)
    region_options = [
        {
            "code": item["code"],
            "name": item["name"],
            "candidates_count": item["candidates_count"],
            "latest_at": item["latest_at"],
        }
        for item in regions
    ]
    selected_region_payload = (
        {
            "code": selected_region["code"],
            "name": selected_region["name"],
            "candidates_count": selected_region["candidates_count"],
        }
        if selected_region else None
    )
    payload = {
        "ok": True,
        "source": "Polymarket官方历史 + 本地新闻库",
        "threshold_pp": round(RECOGNITION_THRESHOLD_PP * 100, 1),
        "general_threshold_pp": round(THRESHOLD_PP * 100, 1),
        "significant_threshold_pp": round(SIGNIFICANT_THRESHOLD_PP * 100, 1),
        "major_threshold_pp": round(MAJOR_THRESHOLD_PP * 100, 1),
        "thin_market_threshold_pp": round(THIN_MARKET_THRESHOLD_PP * 100, 1),
        "thin_persistence_ratio": THIN_PERSISTENCE_RATIO,
        "window_hours": window_hours,
        "window_options_hours": list(WINDOW_OPTIONS_HOURS),
        "recent_days": round(window_hours / 24, 2),
        "news_window_hours": NEWS_WINDOW_HOURS,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": {
            "candidates": len(candidate_groups),
            "regions": len(regions),
            "selection_events": len(alerts),
            "confirmed_events": len(confirmed_events),
            "observations": len(observations),
            "with_news": sum(1 for event in alerts if event["news_count"]),
        },
        "events": alerts,
        "chart_events": chart_events,
        "observations": observations,
        "regions": region_options,
        "selected_region": selected_region_payload,
        "candidates": candidate_groups,
        "region_candidates": candidate_groups,
        "selected": candidate_groups[0] if candidate_groups else None,
        "usage_note": (
            "所有单小时或24小时累计达到2个百分点的变化都会识别，并按同一候选人"
            "同一天保留最大一笔。2至4.9个百分点为小幅异动，5至7.9为一般异动；"
            "薄盘口2个百分点以上仍会列出，"
            "但达到8个百分点且下一价格点至少保留一半变动后才标记为确认异动。"
        ),
    }
    _MEMORY["ts"][cache_key] = now
    _MEMORY["payloads"][cache_key] = payload
    return payload
