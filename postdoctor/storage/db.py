"""DB層: アカウント単位の SQLite への永続化。

取得層・分析層は SQL を直接書かず、この層が公開する関数のみを介して
データを読み書きする。DB ファイルは account.db_path (data/<account>/posts.db)
に分離され、since_id もアカウントごとの meta テーブルで管理される。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import pandas as pd

from postdoctor.config import Account

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id            TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,   -- ISO8601 (JST)
    weekday       INTEGER,         -- 0=月 ... 6=日
    hour          INTEGER,         -- 0-23 (JST)
    text          TEXT,
    impressions   INTEGER DEFAULT 0,
    likes         INTEGER DEFAULT 0,
    retweets      INTEGER DEFAULT 0,
    replies       INTEGER DEFAULT 0,
    quotes        INTEGER DEFAULT 0,
    bookmarks     INTEGER DEFAULT 0,
    fetched_at    TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@dataclass(frozen=True)
class PostRecord:
    id: str
    created_at: str
    weekday: int
    hour: int
    text: str
    impressions: int
    likes: int
    retweets: int
    replies: int
    quotes: int
    bookmarks: int


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def connect(account: Account) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(account.db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()


def get_since_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key='since_id'").fetchone()
    return row[0] if row else None


def set_since_id(conn: sqlite3.Connection, since_id: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('since_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (since_id,),
    )
    conn.commit()


def upsert_posts(conn: sqlite3.Connection, posts: list[PostRecord]) -> int:
    # fetched_atは記帳用の内部タイムスタンプ(UTC ISO8601)。created_atとは異なり、
    # fetcher層でJST変換済みではないので混同しないこと（reply_scout/db.pyの
    # _utc_now_iso()と同じ規約）。
    fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for p in posts:
        conn.execute(
            """
            INSERT INTO posts
              (id, created_at, weekday, hour, text,
               impressions, likes, retweets, replies, quotes, bookmarks,
               fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
              impressions=excluded.impressions,
              likes=excluded.likes,
              retweets=excluded.retweets,
              replies=excluded.replies,
              quotes=excluded.quotes,
              bookmarks=excluded.bookmarks,
              fetched_at=excluded.fetched_at
            """,
            (
                p.id, p.created_at, p.weekday, p.hour, p.text,
                p.impressions, p.likes, p.retweets, p.replies, p.quotes, p.bookmarks,
                fetched_at,
            ),
        )
    conn.commit()
    return len(posts)


def load_posts_df(account: Account) -> pd.DataFrame:
    with connect(account) as conn:
        df = pd.read_sql_query("SELECT * FROM posts", conn)
    return df
