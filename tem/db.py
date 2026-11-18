"""SQLite 儲存層。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from .paths import DATA_DIR, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds (
    ts            TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    event_slug    TEXT NOT NULL,
    market_slug   TEXT,
    region_code   TEXT,
    region_name   TEXT,
    candidate_en  TEXT,
    candidate_zh  TEXT,
    yes_price     REAL,
    best_bid      REAL,
    best_ask      REAL,
    last_trade    REAL,
    volume        REAL,
    liquidity     REAL,
    chg_1d        REAL,
    chg_1w        REAL,
    image_url     TEXT,
    clob_token_id TEXT,
    question      TEXT,
    PRIMARY KEY (ts, market_id)
);
CREATE INDEX IF NOT EXISTS idx_odds_region ON odds(region_code, ts);

CREATE TABLE IF NOT EXISTS news (
    id            TEXT PRIMARY KEY,
    source_id     TEXT,
    source_name   TEXT,
    lang          TEXT,
    title         TEXT NOT NULL,
    url           TEXT,
    published_at  TEXT,
    summary       TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_pub ON news(published_at DESC);

CREATE TABLE IF NOT EXISTS news_analysis (
    news_id       TEXT PRIMARY KEY REFERENCES news(id) ON DELETE CASCADE,
    analyzed_at   TEXT NOT NULL,
    engine        TEXT NOT NULL,
    model         TEXT,
    sentiment     REAL,
    impact        REAL,
    regions       TEXT,
    candidates    TEXT,
    topics        TEXT,
    summary       TEXT
);

CREATE TABLE IF NOT EXISTS candidate_statements (
    id             TEXT PRIMARY KEY,
    news_id        TEXT,
    candidate_name TEXT NOT NULL,
    region_name    TEXT,
    party          TEXT,
    office         TEXT,
    published_at   TEXT,
    topic          TEXT,
    claim          TEXT,
    quote_text     TEXT,
    stance         TEXT,
    polarity       INTEGER,
    conditions     TEXT,
    directness     TEXT,
    confidence     REAL,
    source_name    TEXT,
    source_title   TEXT,
    source_url     TEXT,
    extracted_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_statements_candidate
    ON candidate_statements(candidate_name, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_statements_topic
    ON candidate_statements(topic, published_at DESC);

CREATE TABLE IF NOT EXISTS forecasts (
    ts             TEXT NOT NULL,
    region_code    TEXT NOT NULL,
    region_name    TEXT,
    candidate_en   TEXT NOT NULL,
    candidate_zh   TEXT,
    pm_prob        REAL,
    pm_raw         REAL,
    news_sentiment REAL,
    region_sentiment REAL,
    fused_prob     REAL,
    confidence     REAL,
    articles       INTEGER,
    volume         REAL,
    chg_1d         REAL,
    chg_1w         REAL,
    is_leader      INTEGER,
    rationale      TEXT,
    PRIMARY KEY (ts, region_code, candidate_en)
);

CREATE TABLE IF NOT EXISTS region_state (
    ts             TEXT NOT NULL,
    region_code    TEXT NOT NULL,
    region_name    TEXT,
    article_count  INTEGER,
    avg_sentiment  REAL,
    pos_count      INTEGER,
    neu_count      INTEGER,
    neg_count      INTEGER,
    top_topics     TEXT,
    momentum       REAL,
    signal_count   INTEGER,
    PRIMARY KEY (ts, region_code)
);

CREATE TABLE IF NOT EXISTS attention (
    ts             TEXT NOT NULL,
    entity         TEXT NOT NULL,
    entity_type    TEXT,
    region_code    TEXT,
    source         TEXT NOT NULL,
    mentions       INTEGER,
    score          REAL,
    views_7d       REAL,
    views_30d      REAL,
    momentum       REAL,
    extra          TEXT,
    PRIMARY KEY (ts, entity, source)
);
CREATE INDEX IF NOT EXISTS idx_attention_entity ON attention(entity, ts);

CREATE TABLE IF NOT EXISTS market_depth (
    ts             TEXT NOT NULL,
    market_id      TEXT NOT NULL,
    token_id       TEXT,
    region_code    TEXT,
    candidate_zh   TEXT,
    candidate_en   TEXT,
    event_slug     TEXT,
    best_bid       REAL,
    best_ask       REAL,
    mid            REAL,
    spread         REAL,
    spread_pct     REAL,
    spread_pts     REAL,
    depth_usdc     REAL,
    trades_24h     INTEGER,
    volume_24h     REAL,
    flow_imbalance REAL,
    unique_traders INTEGER,
    largest_usdc   REAL,
    holder_count   INTEGER,
    top_share      REAL,
    hhi            REAL,
    extra          TEXT,
    PRIMARY KEY (ts, market_id)
);
CREATE INDEX IF NOT EXISTS idx_depth_region ON market_depth(region_code, ts);
"""

# 舊版資料庫的欄位遷移（SQLite 無 ADD COLUMN IF NOT EXISTS）
MIGRATIONS = [
    ("region_state", "signal_count", "ALTER TABLE region_state ADD COLUMN signal_count INTEGER"),
    ("forecasts", "region_sentiment", "ALTER TABLE forecasts ADD COLUMN region_sentiment REAL"),
    ("forecasts", "articles", "ALTER TABLE forecasts ADD COLUMN articles INTEGER"),
    ("forecasts", "volume", "ALTER TABLE forecasts ADD COLUMN volume REAL"),
    ("forecasts", "chg_1d", "ALTER TABLE forecasts ADD COLUMN chg_1d REAL"),
    ("forecasts", "chg_1w", "ALTER TABLE forecasts ADD COLUMN chg_1w REAL"),
    ("forecasts", "is_leader", "ALTER TABLE forecasts ADD COLUMN is_leader INTEGER"),
    ("odds", "image_url", "ALTER TABLE odds ADD COLUMN image_url TEXT"),
    ("odds", "clob_token_id", "ALTER TABLE odds ADD COLUMN clob_token_id TEXT"),
    ("odds", "market_slug", "ALTER TABLE odds ADD COLUMN market_slug TEXT"),
    ("forecasts", "poll_prob", "ALTER TABLE forecasts ADD COLUMN poll_prob REAL"),
    ("forecasts", "poll_share", "ALTER TABLE forecasts ADD COLUMN poll_share REAL"),
    ("forecasts", "poll_margin", "ALTER TABLE forecasts ADD COLUMN poll_margin REAL"),
    ("forecasts", "prior_prob", "ALTER TABLE forecasts ADD COLUMN prior_prob REAL"),
    ("forecasts", "sufficiency", "ALTER TABLE forecasts ADD COLUMN sufficiency REAL"),
    ("forecasts", "market_quality", "ALTER TABLE forecasts ADD COLUMN market_quality REAL"),
    ("forecasts", "spread_pts", "ALTER TABLE forecasts ADD COLUMN spread_pts REAL"),
    ("forecasts", "depth_usdc", "ALTER TABLE forecasts ADD COLUMN depth_usdc REAL"),
    ("forecasts", "flow_imbalance", "ALTER TABLE forecasts ADD COLUMN flow_imbalance REAL"),
    ("forecasts", "top_share", "ALTER TABLE forecasts ADD COLUMN top_share REAL"),
    ("forecasts", "views_7d", "ALTER TABLE forecasts ADD COLUMN views_7d REAL"),
    ("forecasts", "momentum", "ALTER TABLE forecasts ADD COLUMN momentum REAL"),
    ("forecasts", "ptt_mentions", "ALTER TABLE forecasts ADD COLUMN ptt_mentions INTEGER"),
    ("forecasts", "components", "ALTER TABLE forecasts ADD COLUMN components TEXT"),
    ("forecasts", "poll_leader", "ALTER TABLE forecasts ADD COLUMN poll_leader TEXT"),
    ("market_depth", "spread_pts", "ALTER TABLE market_depth ADD COLUMN spread_pts REAL"),
    ("news", "cluster_id", "ALTER TABLE news ADD COLUMN cluster_id TEXT"),
    ("news", "cluster_size", "ALTER TABLE news ADD COLUMN cluster_size INTEGER"),
    ("news", "is_rep", "ALTER TABLE news ADD COLUMN is_rep INTEGER DEFAULT 1"),
]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db_path() -> str:
    import os

    return os.environ.get("TEM_DB") or str(DATA_DIR / "election.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.execute(ddl)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def upsert_news(rows: Iterable[dict[str, Any]]) -> int:
    """寫入新聞，回傳本次新增筆數。"""
    now = utcnow_iso()
    inserted = 0
    with connect() as conn:
        for row in rows:
            cur = conn.execute("SELECT id FROM news WHERE id = ?", (row["id"],))
            exists = cur.fetchone() is not None
            if exists:
                conn.execute("UPDATE news SET last_seen = ? WHERE id = ?", (now, row["id"]))
                continue
            conn.execute(
                """
                INSERT INTO news (id, source_id, source_name, lang, title, url,
                                  published_at, summary, first_seen, last_seen)
                VALUES (:id, :source_id, :source_name, :lang, :title, :url,
                        :published_at, :summary, :first_seen, :last_seen)
                """,
                {
                    "id": row["id"],
                    "source_id": row.get("source_id"),
                    "source_name": row.get("source_name"),
                    "lang": row.get("lang"),
                    "title": row["title"],
                    "url": row.get("url"),
                    "published_at": row.get("published_at"),
                    "summary": (row.get("summary") or "")[:2000],
                    "first_seen": now,
                    "last_seen": now,
                },
            )
            inserted += 1
    return inserted


def delete_news_ids(ids: Iterable[str]) -> int:
    """删除新闻及其分析、言论关联记录。"""
    deleted = 0
    with connect() as conn:
        for news_id in dict.fromkeys(str(value) for value in ids if value):
            conn.execute("DELETE FROM candidate_statements WHERE news_id = ?", (news_id,))
            conn.execute("DELETE FROM news_analysis WHERE news_id = ?", (news_id,))
            cur = conn.execute("DELETE FROM news WHERE id = ?", (news_id,))
            deleted += max(0, cur.rowcount)
    return deleted


def save_news_clusters(mapping: dict[str, dict[str, Any]]) -> int:
    """把事件分群結果寫回 news 表。"""
    count = 0
    with connect() as conn:
        for news_id, entry in mapping.items():
            conn.execute(
                "UPDATE news SET cluster_id = ?, cluster_size = ?, is_rep = ? WHERE id = ?",
                (entry["cluster_id"], entry["cluster_size"], entry["is_rep"], news_id),
            )
            count += 1
    return count


def insert_odds(rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with connect() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO odds (ts, market_id, event_slug, market_slug, region_code, region_name,
                    candidate_en, candidate_zh, yes_price, best_bid, best_ask, last_trade,
                    volume, liquidity, chg_1d, chg_1w, image_url, clob_token_id, question)
                VALUES (:ts, :market_id, :event_slug, :market_slug, :region_code, :region_name,
                    :candidate_en, :candidate_zh, :yes_price, :best_bid, :best_ask, :last_trade,
                    :volume, :liquidity, :chg_1d, :chg_1w, :image_url, :clob_token_id, :question)
                """,
                row,
            )
            count += 1
    return count


def save_analyses(rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with connect() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO news_analysis
                    (news_id, analyzed_at, engine, model, sentiment, impact,
                     regions, candidates, topics, summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["news_id"],
                    row.get("analyzed_at") or utcnow_iso(),
                    row["engine"],
                    row.get("model"),
                    row.get("sentiment"),
                    row.get("impact"),
                    _json(row.get("regions") or []),
                    _json(row.get("candidates") or []),
                    _json(row.get("topics") or []),
                    row.get("summary"),
                ),
            )
            count += 1
    return count


def save_candidate_statements(rows: Iterable[dict[str, Any]]) -> int:
    """写入候选人言论记录；同一新闻中的同一句只保留一条。"""
    count = 0
    with connect() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO candidate_statements
                (id, news_id, candidate_name, region_name, party, office,
                 published_at, topic, claim, quote_text, stance, polarity,
                 conditions, directness, confidence, source_name, source_title,
                 source_url, extracted_at)
                VALUES
                (:id, :news_id, :candidate_name, :region_name, :party, :office,
                 :published_at, :topic, :claim, :quote_text, :stance, :polarity,
                 :conditions, :directness, :confidence, :source_name, :source_title,
                 :source_url, :extracted_at)
                """,
                row,
            )
            count += 1
    return count


def fetch_candidate_statements(days: int = 90, limit: int = 5000) -> list[sqlite3.Row]:
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT *
                FROM candidate_statements
                WHERE COALESCE(NULLIF(published_at, ''), extracted_at) >= datetime('now', ?)
                ORDER BY COALESCE(NULLIF(published_at, ''), extracted_at) DESC
                LIMIT ?
                """,
                (f"-{max(1, int(days))} days", max(1, limit)),
            ).fetchall()
        )


def fetch_candidate_statement(statement_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM candidate_statements WHERE id = ?",
            (statement_id,),
        ).fetchone()


def candidate_statement_counts() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS statements,
                   COUNT(DISTINCT candidate_name) AS candidates,
                   COUNT(DISTINCT topic) AS topics,
                   MAX(extracted_at) AS latest
            FROM candidate_statements
            """
        ).fetchone()
    return {
        "statements": int(row["statements"] or 0),
        "candidates": int(row["candidates"] or 0),
        "topics": int(row["topics"] or 0),
        "latest": row["latest"],
    }


def clear_candidate_statements() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM candidate_statements").fetchone()
        conn.execute("DELETE FROM candidate_statements")
    return int(row[0] or 0)


FORECAST_COLUMNS = (
    "region_name", "candidate_zh", "pm_prob", "pm_raw", "news_sentiment", "region_sentiment",
    "fused_prob", "confidence", "articles", "volume", "chg_1d", "chg_1w", "rationale",
    "poll_prob", "poll_share", "poll_margin", "prior_prob", "sufficiency", "market_quality",
    "spread_pts", "depth_usdc", "flow_imbalance", "top_share", "views_7d", "momentum",
    "ptt_mentions", "components",
    "poll_leader",
)


def save_forecasts(ts: str, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with connect() as conn:
        for row in rows:
            payload = dict(row)
            payload["components"] = _json(row.get("components") or {})
            payload["is_leader"] = 1 if row.get("leader") else 0
            payload["ts"] = ts
            payload["region_code"] = row["region_code"]
            payload["candidate_en"] = row["candidate_en"]
            columns = ("ts", "region_code", "candidate_en", "is_leader", *FORECAST_COLUMNS)
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT OR REPLACE INTO forecasts ({', '.join(columns)}) VALUES ({placeholders})",
                tuple(payload.get(col) for col in columns),
            )
            count += 1
    return count


def save_region_state(ts: str, rows: Iterable[dict[str, Any]]) -> int:
    count = 0
    with connect() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO region_state
                    (ts, region_code, region_name, article_count, avg_sentiment,
                     pos_count, neu_count, neg_count, top_topics, momentum, signal_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    row["region_code"],
                    row.get("region_name"),
                    row.get("article_count"),
                    row.get("avg_sentiment"),
                    row.get("pos_count"),
                    row.get("neu_count"),
                    row.get("neg_count"),
                    _json(row.get("top_topics") or []),
                    row.get("momentum"),
                    row.get("signal_count"),
                ),
            )
            count += 1
    return count


def latest_ts(table: str = "odds") -> str | None:
    with connect() as conn:
        row = conn.execute(f"SELECT MAX(ts) AS ts FROM {table}").fetchone()  # noqa: S608 - 表名為內部常數
    return row["ts"] if row and row["ts"] else None


def save_attention(ts: str, rows: Iterable[dict[str, Any]]) -> int:
    """寫入注意力數據（PTT 聲量 / 維基瀏覽量）。"""
    count = 0
    with connect() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO attention
                    (ts, entity, entity_type, region_code, source,
                     mentions, score, views_7d, views_30d, momentum, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    row["entity"],
                    row.get("entity_type"),
                    row.get("region_code"),
                    row["source"],
                    row.get("mentions"),
                    row.get("score"),
                    row.get("views_7d"),
                    row.get("views_30d"),
                    row.get("momentum"),
                    _json(row.get("extra") or {}),
                ),
            )
            count += 1
    return count


def fetch_latest_attention() -> list[sqlite3.Row]:
    """取每個 (entity, source) 各自最新的一列。

    這樣做是為了讓「只抓幾個候選人」的局部更新不會把其他候選人從
    最新視圖裡擠掉（用 MAX(ts) 整批取的話就會如此）。
    """
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT a.* FROM attention a
                JOIN (
                    SELECT entity, source, MAX(ts) AS ts FROM attention GROUP BY entity, source
                ) m ON m.entity = a.entity AND m.source = a.source AND m.ts = a.ts
                ORDER BY a.entity
                """
            ).fetchall()
        )


def save_market_depth(ts: str, rows: Iterable[dict[str, Any]]) -> int:
    """寫入盤口 / 逐筆成交 / 持倉集中度。"""
    count = 0
    with connect() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT OR REPLACE INTO market_depth
                    (ts, market_id, token_id, region_code, candidate_zh, candidate_en, event_slug,
                     best_bid, best_ask, mid, spread, spread_pct, spread_pts, depth_usdc,
                     trades_24h, volume_24h, flow_imbalance, unique_traders, largest_usdc,
                     holder_count, top_share, hhi, extra)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    str(row.get("market_id")),
                    row.get("token_id"),
                    row.get("region_code"),
                    row.get("candidate_zh"),
                    row.get("candidate_en"),
                    row.get("event_slug"),
                    row.get("best_bid"),
                    row.get("best_ask"),
                    row.get("mid"),
                    row.get("spread"),
                    row.get("spread_pct"),
                    row.get("spread_pts"),
                    row.get("depth_usdc"),
                    row.get("trades"),
                    row.get("volume_usdc"),
                    row.get("flow_imbalance"),
                    row.get("unique_traders"),
                    row.get("largest_usdc"),
                    row.get("holder_count"),
                    row.get("top_share"),
                    row.get("hhi"),
                    _json({
                        "top_holders": row.get("top_holders"),
                        "largest_trade": row.get("largest_trade"),
                        "error": row.get("error"),
                    }),
                ),
            )
            count += 1
    return count


def fetch_latest_market_depth() -> list[sqlite3.Row]:
    """每個市場各自最新的一列（同樣避免局部更新擠掉其他市場）。"""
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT d.* FROM market_depth d
                JOIN (
                    SELECT market_id, MAX(ts) AS ts FROM market_depth GROUP BY market_id
                ) m ON m.market_id = d.market_id AND m.ts = d.ts
                ORDER BY d.region_code, d.mid DESC
                """
            ).fetchall()
        )


def attention_history(entity: str, source: str | None = None, limit: int = 240) -> list[sqlite3.Row]:
    with connect() as conn:
        if source:
            return list(
                conn.execute(
                    """
                    SELECT ts, source, mentions, score, views_7d, views_30d, momentum
                    FROM attention WHERE entity = ? AND source = ?
                    ORDER BY ts DESC LIMIT ?
                    """,
                    (entity, source, limit),
                ).fetchall()
            )
        return list(
            conn.execute(
                """
                SELECT ts, source, mentions, score, views_7d, views_30d, momentum
                FROM attention WHERE entity = ?
                ORDER BY ts DESC LIMIT ?
                """,
                (entity, limit),
            ).fetchall()
        )


def snapshot_times() -> list[str]:
    """回傳所有賠率快照的時間點（由舊到新）。"""
    with connect() as conn:
        return [r["ts"] for r in conn.execute("SELECT DISTINCT ts FROM odds ORDER BY ts")]


def fetch_odds_for_candidate(region_code: str, candidate_en: str) -> list[sqlite3.Row]:
    """取得某候选人的赔率快照，最新一笔用于候选人详情。"""
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT ts, yes_price, chg_1d, image_url, clob_token_id, market_id, market_slug,
                       event_slug, volume, liquidity, region_name, candidate_zh
                FROM odds
                WHERE region_code = ? AND candidate_en = ?
                ORDER BY ts ASC
                """,
                (region_code, candidate_en),
            ).fetchall()
        )


def fetch_odds_since(days: int = 30) -> list[sqlite3.Row]:
    """读取近期赔率快照，按候选人和时间正序，供异动检测使用。"""
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM odds
                WHERE julianday(ts) >= julianday('now', ?)
                ORDER BY candidate_en, region_code, ts
                """,
                (f"-{max(1, int(days))} days",),
            ).fetchall()
        )


def fetch_news_between(
    start: str,
    end: str,
    *,
    limit: int = 3000,
) -> list[sqlite3.Row]:
    """读取指定时间区间的新闻及分析字段。"""
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT n.*, a.sentiment, a.impact, a.regions, a.candidates, a.topics,
                       a.summary AS ai_summary, a.engine, a.model
                FROM news n LEFT JOIN news_analysis a ON a.news_id = n.id
                WHERE julianday(COALESCE(n.published_at, n.first_seen)) >= julianday(?)
                  AND julianday(COALESCE(n.published_at, n.first_seen)) < julianday(?)
                ORDER BY COALESCE(n.published_at, n.first_seen) ASC
                LIMIT ?
                """,
                (start, end, max(1, int(limit))),
            ).fetchall()
        )


def fetch_recent_news(limit: int = 200) -> list[sqlite3.Row]:
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT n.*, a.sentiment, a.impact, a.regions, a.candidates, a.topics,
                       a.summary AS ai_summary, a.engine, a.model
                FROM news n LEFT JOIN news_analysis a ON a.news_id = n.id
                ORDER BY COALESCE(n.published_at, n.first_seen) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )


def fetch_news_page(
    *,
    limit: int = 50,
    offset: int = 0,
    start_date: str = "",
    end_date: str = "",
    hours: int = 0,
    query_text: str = "",
) -> tuple[list[sqlite3.Row], int]:
    """按发布日期分页读取新闻，返回当前页与符合条件的总数。"""
    clauses: list[str] = []
    params: list[Any] = []
    date_expr = "date(COALESCE(n.published_at, n.first_seen), '+8 hours')"
    if start_date:
        clauses.append(f"{date_expr} >= ?")
        params.append(start_date)
    if end_date:
        clauses.append(f"{date_expr} <= ?")
        params.append(end_date)
    if hours > 0:
        clauses.append("COALESCE(n.published_at, n.first_seen) >= datetime('now', ?)")
        params.append(f"-{int(hours)} hours")
    if query_text.strip():
        pattern = f"%{query_text.strip()}%"
        clauses.append(
            "(n.title LIKE ? OR COALESCE(n.summary, '') LIKE ? "
            "OR COALESCE(n.source_name, '') LIKE ?)"
        )
        params.extend([pattern, pattern, pattern])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as conn:
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM news n{where}", params).fetchone()[0]
        )
        rows = conn.execute(
            f"""
            SELECT n.*, a.sentiment, a.impact, a.regions, a.candidates, a.topics,
                   a.summary AS ai_summary, a.engine, a.model
            FROM news n LEFT JOIN news_analysis a ON a.news_id = n.id
            {where}
            ORDER BY COALESCE(n.published_at, n.first_seen) DESC
            LIMIT ? OFFSET ?
            """,
            [*params, max(1, limit), max(0, offset)],
        ).fetchall()
    return list(rows), total


def fetch_news_hours_window(
    *,
    from_hours_ago: int,
    to_hours_ago: int,
    limit: int = 5000,
) -> list[sqlite3.Row]:
    """读取一个明确的小时区间，用于计算上一等长时段的热词基线。

    ``from_hours_ago`` 是较早边界，``to_hours_ago`` 是较晚边界。
    例如近 24 小时的上一时段传入 48 和 24。
    """
    start_hours = max(0, int(from_hours_ago))
    end_hours = max(0, int(to_hours_ago))
    if start_hours < end_hours:
        start_hours, end_hours = end_hours, start_hours
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT n.*, a.sentiment, a.impact, a.regions, a.candidates, a.topics,
                       a.summary AS ai_summary, a.engine, a.model
                FROM news n LEFT JOIN news_analysis a ON a.news_id = n.id
                WHERE COALESCE(n.published_at, n.first_seen) >= datetime('now', ?)
                  AND COALESCE(n.published_at, n.first_seen) < datetime('now', ?)
                  AND COALESCE(n.is_rep, 1) = 1
                ORDER BY COALESCE(n.published_at, n.first_seen) DESC
                LIMIT ?
                """,
                (f"-{start_hours} hours", f"-{end_hours} hours", max(1, limit)),
            ).fetchall()
        )


def fetch_news_since(days: int = 30, limit: int = 3000) -> list[sqlite3.Row]:
    """取近 N 天新聞（含分析欄位），用於聚合。

    representatives_only=True 時只回傳每個事件群的代表，等於以「事件」
    為單位計算輿情（避免同一件事被多家轉載反覆計分）。
    """
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT n.*, a.sentiment, a.impact, a.regions, a.candidates, a.topics,
                       a.summary AS ai_summary, a.engine, a.model
                FROM news n LEFT JOIN news_analysis a ON a.news_id = n.id
                WHERE COALESCE(n.published_at, n.first_seen) >= datetime('now', ?)
                ORDER BY COALESCE(n.published_at, n.first_seen) DESC
                LIMIT ?
                """,
                (f"-{int(days)} days", limit),
            ).fetchall()
        )


def fetch_news_since_events(days: int = 30, limit: int = 3000) -> list[sqlite3.Row]:
    """只取事件代表（is_rep=1）的近 N 天新聞，用於輿情聚合。"""
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT n.*, a.sentiment, a.impact, a.regions, a.candidates, a.topics,
                       a.summary AS ai_summary, a.engine, a.model
                FROM news n LEFT JOIN news_analysis a ON a.news_id = n.id
                WHERE COALESCE(n.published_at, n.first_seen) >= datetime('now', ?)
                  AND COALESCE(n.is_rep, 1) = 1
                ORDER BY COALESCE(n.published_at, n.first_seen) DESC
                LIMIT ?
                """,
                (f"-{int(days)} days", limit),
            ).fetchall()
        )


def event_stats(days: int = 30) -> dict[str, Any]:
    """新聞則數 vs 事件數，供前端顯示去重效果。"""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS articles,
                   SUM(CASE WHEN COALESCE(is_rep, 1) = 1 THEN 1 ELSE 0 END) AS events,
                   MAX(COALESCE(cluster_size, 1)) AS largest
            FROM news
            WHERE COALESCE(published_at, first_seen) >= datetime('now', ?)
            """,
            (f"-{int(days)} days",),
        ).fetchone()
    articles = (row["articles"] if row else 0) or 0
    events = (row["events"] if row else 0) or 0
    return {
        "articles": articles,
        "events": events,
        "merged": max(0, articles - events),
        "largest": (row["largest"] if row else 1) or 1,
        "ratio": round(events / articles, 3) if articles else None,
    }


def fetch_all_analyses() -> dict[str, dict[str, Any]]:
    """回傳 {news_id: analysis dict}。"""
    result: dict[str, dict[str, Any]] = {}
    with connect() as conn:
        for row in conn.execute("SELECT * FROM news_analysis"):
            def parse(value: Any) -> Any:
                if not value:
                    return []
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return []

            result[row["news_id"]] = {
                "news_id": row["news_id"],
                "analyzed_at": row["analyzed_at"],
                "engine": row["engine"],
                "model": row["model"],
                "sentiment": row["sentiment"],
                "impact": row["impact"],
                "regions": parse(row["regions"]),
                "candidates": parse(row["candidates"]),
                "topics": parse(row["topics"]),
                "summary": row["summary"],
            }
    return result


def fetch_latest_odds() -> list[sqlite3.Row]:
    """取得最新一次快照的所有賠率列。"""
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM odds
                WHERE ts = (SELECT MAX(ts) FROM odds)
                ORDER BY region_code, yes_price DESC
                """
            ).fetchall()
        )


def fetch_latest_forecasts() -> list[sqlite3.Row]:
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM forecasts
                WHERE ts = (SELECT MAX(ts) FROM forecasts)
                ORDER BY region_code, fused_prob DESC
                """
            ).fetchall()
        )


def fetch_latest_region_state() -> list[sqlite3.Row]:
    with connect() as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM region_state
                WHERE ts = (SELECT MAX(ts) FROM region_state)
                ORDER BY article_count DESC
                """
            ).fetchall()
        )


def fetch_unanalyzed_news(limit: int = 60, force: bool = False) -> list[sqlite3.Row]:
    with connect() as conn:
        if force:
            sql = "SELECT * FROM news ORDER BY COALESCE(published_at, first_seen) DESC LIMIT ?"
        else:
            sql = """
                SELECT n.* FROM news n
                LEFT JOIN news_analysis a ON a.news_id = n.id
                WHERE a.news_id IS NULL
                ORDER BY COALESCE(n.published_at, n.first_seen) DESC
                LIMIT ?
            """
        return list(conn.execute(sql, (limit,)).fetchall())


def stats() -> dict[str, Any]:
    with connect() as conn:
        def one(sql: str) -> Any:
            row = conn.execute(sql).fetchone()
            return row[0] if row else None

        return {
            "news_total": one("SELECT COUNT(*) FROM news"),
            "news_analyzed": one("SELECT COUNT(*) FROM news_analysis"),
            "odds_latest_ts": one("SELECT MAX(ts) FROM odds"),
            "odds_snapshots": one("SELECT COUNT(DISTINCT ts) FROM odds"),
            "forecast_latest_ts": one("SELECT MAX(ts) FROM forecasts"),
            "regions_with_odds": one("SELECT COUNT(DISTINCT region_code) FROM odds"),
            # Polymarket 一档「谁当选」= 1 个 event；底下每位候选人 = 1 个 market
            "odds_events": one("SELECT COUNT(DISTINCT event_slug) FROM odds"),
            "candidate_markets": one(
                "SELECT COUNT(*) FROM odds WHERE ts = (SELECT MAX(ts) FROM odds)"
            ),
            "regions_with_forecast": one("SELECT COUNT(DISTINCT region_code) FROM forecasts"),
        }
