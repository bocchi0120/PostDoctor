"""DB層: リプライ営業候補・下書き・送信済みリプライの永続化。

data/<account>/reply_scout.db に分離して保存する（自分の投稿を扱う
posts.db とは別ファイル）。storage/db.py と同じ connect()/init_db()
パターンを踏襲する。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Iterator

from postdoctor.config import Account

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id                  TEXT PRIMARY KEY,
    text                TEXT NOT NULL,
    author_id           TEXT NOT NULL,
    author_screen_name  TEXT NOT NULL,
    author_followers    INTEGER,
    created_at          TEXT NOT NULL,   -- ISO8601 (JST)
    likes               INTEGER DEFAULT 0,
    retweets            INTEGER DEFAULT 0,
    replies             INTEGER DEFAULT 0,
    quotes              INTEGER DEFAULT 0,
    keyword             TEXT,
    score               REAL,
    rank                INTEGER,         -- 直近scoutでのTOP10順位。対象外はNULL
    status              TEXT DEFAULT '未送信',
    fetched_at          TEXT
);
CREATE TABLE IF NOT EXISTS drafts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id  TEXT NOT NULL,
    variant       INTEGER NOT NULL,
    draft_text    TEXT NOT NULL,
    generated_at  TEXT,
    UNIQUE(candidate_id, variant)
);
CREATE TABLE IF NOT EXISTS sent_replies (
    reply_id        TEXT PRIMARY KEY,
    candidate_id    TEXT NOT NULL,
    sent_at         TEXT,
    impressions     INTEGER DEFAULT 0,
    likes           INTEGER DEFAULT 0,
    retweets        INTEGER DEFAULT 0,
    replies         INTEGER DEFAULT 0,
    last_checked_at TEXT
);
CREATE TABLE IF NOT EXISTS usage_log (
    date  TEXT NOT NULL,
    kind  TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (date, kind)
);
"""


@dataclass(frozen=True)
class Candidate:
    id: str
    text: str
    author_id: str
    author_screen_name: str
    created_at: str
    likes: int
    retweets: int
    replies: int
    quotes: int
    keyword: str
    author_followers: int | None = None


@dataclass(frozen=True)
class RankedCandidate:
    candidate: Candidate
    score: float
    rank: int
    status: str
    drafts: list[str]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def connect(account: Account) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(account.reply_scout_db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()


def known_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM candidates WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {r[0] for r in rows}


def insert_candidates(conn: sqlite3.Connection, candidates: list[Candidate]) -> int:
    count = 0
    for c in candidates:
        cur = conn.execute(
            """
            INSERT INTO candidates
              (id, text, author_id, author_screen_name, author_followers,
               created_at, likes, retweets, replies, quotes, keyword, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(id) DO NOTHING
            """,
            (
                c.id, c.text, c.author_id, c.author_screen_name, c.author_followers,
                c.created_at, c.likes, c.retweets, c.replies, c.quotes, c.keyword,
            ),
        )
        count += cur.rowcount
    conn.commit()
    return count


def update_followers(conn: sqlite3.Connection, followers_by_author: dict[str, int]) -> None:
    for author_id, followers in followers_by_author.items():
        conn.execute(
            "UPDATE candidates SET author_followers=? WHERE author_id=?",
            (followers, author_id),
        )
    conn.commit()


def load_scoring_candidates(conn: sqlite3.Connection) -> list[Candidate]:
    """未ランク付け（rankがNULL、かつstatusが未送信）の候補を返す。"""
    rows = conn.execute(
        """
        SELECT id, text, author_id, author_screen_name, author_followers,
               created_at, likes, retweets, replies, quotes, keyword
        FROM candidates
        WHERE status = '未送信'
        """
    ).fetchall()
    return [
        Candidate(
            id=r[0], text=r[1], author_id=r[2], author_screen_name=r[3],
            author_followers=r[4], created_at=r[5], likes=r[6], retweets=r[7],
            replies=r[8], quotes=r[9], keyword=r[10],
        )
        for r in rows
    ]


def set_ranking(conn: sqlite3.Connection, ranked: list[tuple[str, float, int]]) -> None:
    """ranked: [(candidate_id, score, rank), ...]。全候補のrankを一旦クリアしてから設定する。"""
    conn.execute("UPDATE candidates SET rank=NULL")
    for candidate_id, score, rank in ranked:
        conn.execute(
            "UPDATE candidates SET score=?, rank=? WHERE id=?",
            (score, rank, candidate_id),
        )
    conn.commit()


def save_drafts(conn: sqlite3.Connection, candidate_id: str, texts: list[str]) -> None:
    for variant, text in enumerate(texts, start=1):
        conn.execute(
            """
            INSERT INTO drafts (candidate_id, variant, draft_text, generated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(candidate_id, variant) DO UPDATE SET
              draft_text=excluded.draft_text, generated_at=excluded.generated_at
            """,
            (candidate_id, variant, text),
        )
    conn.commit()


def get_top_candidates(conn: sqlite3.Connection) -> list[RankedCandidate]:
    rows = conn.execute(
        """
        SELECT id, text, author_id, author_screen_name, author_followers,
               created_at, likes, retweets, replies, quotes, keyword,
               score, rank, status
        FROM candidates
        WHERE rank IS NOT NULL
        ORDER BY rank ASC
        """
    ).fetchall()
    result: list[RankedCandidate] = []
    for r in rows:
        candidate = Candidate(
            id=r[0], text=r[1], author_id=r[2], author_screen_name=r[3],
            author_followers=r[4], created_at=r[5], likes=r[6], retweets=r[7],
            replies=r[8], quotes=r[9], keyword=r[10],
        )
        drafts = [
            row[0]
            for row in conn.execute(
                "SELECT draft_text FROM drafts WHERE candidate_id=? ORDER BY variant",
                (candidate.id,),
            ).fetchall()
        ]
        result.append(
            RankedCandidate(
                candidate=candidate, score=r[11] or 0.0, rank=r[12],
                status=r[13], drafts=drafts,
            )
        )
    return result


def update_status(
    conn: sqlite3.Connection, candidate_id: str, status: str, reply_id: str | None = None
) -> None:
    conn.execute("UPDATE candidates SET status=? WHERE id=?", (status, candidate_id))
    if status == "送信済み" and reply_id:
        conn.execute(
            """
            INSERT INTO sent_replies (reply_id, candidate_id, sent_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(reply_id) DO NOTHING
            """,
            (reply_id, candidate_id),
        )
    conn.commit()


def get_sent_replies(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT reply_id FROM sent_replies").fetchall()
    return [r[0] for r in rows]


@dataclass(frozen=True)
class SentReply:
    reply_id: str
    candidate_id: str
    sent_at: str | None
    impressions: int
    likes: int
    retweets: int
    replies: int
    last_checked_at: str | None
    author_screen_name: str | None
    original_text: str | None


def get_sent_reply_details(conn: sqlite3.Connection) -> list[SentReply]:
    """送信済みリプライを、追跡した反応値と元候補の情報つきで新しい順に返す。"""
    rows = conn.execute(
        """
        SELECT sr.reply_id, sr.candidate_id, sr.sent_at, sr.impressions, sr.likes,
               sr.retweets, sr.replies, sr.last_checked_at,
               c.author_screen_name, c.text
        FROM sent_replies sr
        LEFT JOIN candidates c ON c.id = sr.candidate_id
        ORDER BY sr.sent_at DESC
        """
    ).fetchall()
    return [
        SentReply(
            reply_id=r[0], candidate_id=r[1], sent_at=r[2],
            impressions=r[3] or 0, likes=r[4] or 0, retweets=r[5] or 0, replies=r[6] or 0,
            last_checked_at=r[7], author_screen_name=r[8], original_text=r[9],
        )
        for r in rows
    ]


def update_sent_reply_metrics(
    conn: sqlite3.Connection, reply_id: str, impressions: int, likes: int, retweets: int, replies: int
) -> None:
    conn.execute(
        """
        UPDATE sent_replies
        SET impressions=?, likes=?, retweets=?, replies=?, last_checked_at=datetime('now')
        WHERE reply_id=?
        """,
        (impressions, likes, retweets, replies, reply_id),
    )
    conn.commit()


def get_today_usage(conn: sqlite3.Connection, kind: str) -> int:
    row = conn.execute(
        "SELECT count FROM usage_log WHERE date=? AND kind=?", (date.today().isoformat(), kind)
    ).fetchone()
    return row[0] if row else 0


def add_usage(conn: sqlite3.Connection, kind: str, count: int) -> None:
    today = date.today().isoformat()
    conn.execute(
        """
        INSERT INTO usage_log (date, kind, count) VALUES (?, ?, ?)
        ON CONFLICT(date, kind) DO UPDATE SET count = count + excluded.count
        """,
        (today, kind, count),
    )
    conn.commit()
