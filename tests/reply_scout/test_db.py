from __future__ import annotations

import sqlite3
from datetime import datetime

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


def test_save_drafts_round_trips_predictions_hash(conn):
    db.insert_candidates(conn, [_candidate("h")])
    db.set_ranking(conn, [("h", 0.9, 1)])
    db.save_drafts(conn, "h", ["下書き1", "下書き2"], predictions_hash="hash-v1")

    ranked = db.get_top_candidates(conn)[0]
    assert ranked.predictions_hash == "hash-v1"

    # 再生成で指紋が変わった場合、上書き保存で新しい指紋に更新される
    db.save_drafts(conn, "h", ["下書き1改", "下書き2改"], predictions_hash="hash-v2")
    ranked = db.get_top_candidates(conn)[0]
    assert ranked.predictions_hash == "hash-v2"


def test_save_drafts_without_predictions_hash_defaults_to_none(conn):
    db.insert_candidates(conn, [_candidate("i")])
    db.set_ranking(conn, [("i", 0.9, 1)])
    db.save_drafts(conn, "i", ["下書き1", "下書き2"])

    ranked = db.get_top_candidates(conn)[0]
    assert ranked.predictions_hash is None


def test_update_status_stores_sent_at_as_utc_iso8601_with_offset(conn):
    """回帰テスト: sent_at/last_checked_atがオフセット無しのUTC文字列で保存されていた
    せいで、ダッシュボード側がJSTと誤認して表示し、実際の送信時刻と9時間ズレて
    見えるバグがあった。以後はオフセット明示のISO8601(UTC)で保存する。"""
    db.insert_candidates(conn, [_candidate("ts1")])
    db.update_status(conn, "ts1", "送信済み", reply_id="reply-ts1")

    row = conn.execute(
        "SELECT sent_at FROM sent_replies WHERE reply_id='reply-ts1'"
    ).fetchone()
    sent_at = row[0]
    dt = datetime.fromisoformat(sent_at)
    assert dt.tzinfo is not None  # オフセットが明示されていること
    assert dt.utcoffset().total_seconds() == 0  # UTCであること


def test_update_sent_reply_metrics_stores_last_checked_at_as_utc_iso8601(conn):
    db.insert_candidates(conn, [_candidate("ts2")])
    db.update_status(conn, "ts2", "送信済み", reply_id="reply-ts2")
    db.update_sent_reply_metrics(conn, "reply-ts2", impressions=10, likes=1, retweets=0, replies=0)

    details = db.get_sent_reply_details(conn)
    last_checked_at = next(d.last_checked_at for d in details if d.reply_id == "reply-ts2")
    dt = datetime.fromisoformat(last_checked_at)
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_get_trackable_sent_replies_filters_by_cutoff(conn):
    db.insert_candidates(conn, [_candidate("t1"), _candidate("t2")])
    db.update_status(conn, "t1", "送信済み", reply_id="reply-t1")
    db.update_status(conn, "t2", "送信済み", reply_id="reply-t2")
    conn.execute("UPDATE sent_replies SET sent_at='2020-01-01T00:00:00+00:00' WHERE reply_id='reply-t1'")
    conn.execute("UPDATE sent_replies SET sent_at='2030-01-01T00:00:00+00:00' WHERE reply_id='reply-t2'")
    conn.commit()

    targets = db.get_trackable_sent_replies(conn, cutoff_iso="2025-01-01T00:00:00+00:00")
    ids = {t.reply_id for t in targets}
    assert ids == {"reply-t2"}


def test_save_reply_responses_is_idempotent_on_id(conn):
    db.insert_candidates(conn, [_candidate("r1")])
    db.update_status(conn, "r1", "送信済み", reply_id="reply-r1")
    response = db.ReplyResponse(
        id="resp-1", parent_reply_id="reply-r1", author_id="u1", author_username="someone",
        text="ありがとうございます", created_at="2026-07-28T10:00:00+09:00",
    )

    inserted_first = db.save_reply_responses(conn, [response])
    inserted_second = db.save_reply_responses(conn, [response])

    assert inserted_first == 1
    assert inserted_second == 0
    count = conn.execute("SELECT COUNT(*) FROM reply_responses").fetchone()[0]
    assert count == 1


def test_unacknowledged_responses_round_trip_and_acknowledge(conn):
    db.insert_candidates(conn, [_candidate("r2")])
    db.update_status(conn, "r2", "送信済み", reply_id="reply-r2")
    db.save_reply_responses(conn, [
        db.ReplyResponse(
            id="resp-2", parent_reply_id="reply-r2", author_id="u2", author_username="fan",
            text="いつも見てます", created_at="2026-07-28T10:00:00+09:00",
        )
    ])

    unacked = db.get_unacknowledged_responses(conn)
    assert len(unacked) == 1
    assert unacked[0].response.id == "resp-2"
    assert unacked[0].original_author_screen_name == "userr2"
    assert unacked[0].acknowledged is False

    db.acknowledge_response(conn, "resp-2")
    assert db.get_unacknowledged_responses(conn) == []


def test_get_sent_reply_details_other_reply_count_excludes_own_raw_replies_field(conn):
    """other_reply_countはreply_responses(他者からの返信のみ保存)からの集計であり、
    sr.replies(自分自身のぶら下げ返信も含むX APIの生reply_count)とは独立していること。"""
    db.insert_candidates(conn, [_candidate("r3")])
    db.update_status(conn, "r3", "送信済み", reply_id="reply-r3")
    db.update_sent_reply_metrics(conn, "reply-r3", impressions=100, likes=5, retweets=0, replies=3)
    db.save_reply_responses(conn, [
        db.ReplyResponse(
            id="resp-3a", parent_reply_id="reply-r3", author_id="u3", author_username="fan1",
            text="質問です", created_at="2026-07-28T10:00:00+09:00",
        )
    ])

    details = db.get_sent_reply_details(conn)
    detail = next(d for d in details if d.reply_id == "reply-r3")
    assert detail.replies == 3  # 生のreply_count(自分含む)はそのまま残る
    assert detail.other_reply_count == 1  # 表示用の「他者からの返信数」は別集計


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
