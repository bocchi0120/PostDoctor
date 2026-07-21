from __future__ import annotations

import sqlite3

from postdoctor.reply_scout import db
from postdoctor.reply_scout.db import Candidate


def _candidate(cid: str) -> Candidate:
    return Candidate(
        id=cid, text=f"text-{cid}", author_id=f"a-{cid}", author_screen_name=f"user{cid}",
        created_at="2026-07-21T00:00:00+09:00", likes=1, retweets=0, replies=0, quotes=0,
        keyword="競馬",
    )


def test_init_db_migration_is_idempotent(conn):
    # conftestのconnフィクスチャで既に一度init_db済み。もう一度呼んでも例外にならない。
    db.init_db(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(candidates)").fetchall()}
    assert "prediction_status" in cols


def test_get_draftable_candidates_excludes_skipped_sent_and_over_limit(conn):
    candidates = [_candidate(c) for c in ("a", "b", "c", "d")]
    db.insert_candidates(conn, candidates)
    db.set_ranking(conn, [("a", 0.9, 1), ("b", 0.8, 2), ("c", 0.7, 3), ("d", 0.6, 4)])
    db.update_status(conn, "b", "見送り")
    db.update_status(conn, "c", "送信済み")

    draftable = db.get_draftable_candidates(conn, limit=3)
    ids = {r.candidate.id for r in draftable}
    assert ids == {"a"}  # b=見送り, c=送信済み, d=rank超過(4>3) で全て除外


def test_mark_and_clear_prediction_status(conn):
    db.insert_candidates(conn, [_candidate("x")])
    db.set_ranking(conn, [("x", 0.5, 1)])

    db.mark_prediction_unavailable(conn, "x")
    ranked = db.get_top_candidates(conn)[0]
    assert ranked.prediction_status == "予測データ未投入"

    db.clear_prediction_status(conn, "x")
    ranked = db.get_top_candidates(conn)[0]
    assert ranked.prediction_status is None


def test_save_and_read_score_factors(conn):
    db.insert_candidates(conn, [_candidate("y")])
    db.save_score_factors(conn, "y", 0.5, 0.3, 1.0, 0.0, 1.0, False, 0.42)

    rows = db.get_score_review_rows(conn)
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "y"
    assert rows[0]["final_score"] == 0.42
    assert rows[0]["ng_penalty_applied"] == 0

    # 上書き保存（ON CONFLICT DO UPDATE）が効くことも確認
    db.save_score_factors(conn, "y", 0.6, 0.3, 1.0, 0.0, 1.0, True, 0.99)
    rows = db.get_score_review_rows(conn)
    assert len(rows) == 1
    assert rows[0]["final_score"] == 0.99
    assert rows[0]["ng_penalty_applied"] == 1


def test_delete_from_drafts_does_not_touch_sent_replies(conn):
    """DELETE FROM draftsがsent_repliesに影響しないことのテスト（外部キー制約が
    存在しないことの直接的な回帰テスト）。"""
    db.insert_candidates(conn, [_candidate("z")])
    db.save_drafts(conn, "z", ["下書き1", "下書き2"])
    db.update_status(conn, "z", "送信済み", reply_id="reply-z")

    conn.execute("DELETE FROM drafts")
    conn.commit()

    drafts_left = conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]
    sent_replies_left = conn.execute("SELECT COUNT(*) FROM sent_replies").fetchone()[0]
    assert drafts_left == 0
    assert sent_replies_left == 1
