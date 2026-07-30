from __future__ import annotations

from datetime import datetime, timedelta, timezone

from postdoctor import config
from postdoctor.config import Account
from postdoctor.dashboard import generator
from postdoctor.reply_scout import db as rs_db


def test_format_utc_as_jst_converts_naive_string_assumed_utc():
    """回帰テスト: sent_at/last_checked_atはUTCで保存されるが、以前は変換せず
    そのまま表示していたため実際の時刻と9時間ズレて見えるバグがあった。"""
    # 2026-07-25 03:26:54 UTC -> 2026-07-25 12:26 JST
    assert generator._format_utc_as_jst("2026-07-25 03:26:54") == "2026-07-25 12:26"


def test_format_utc_as_jst_converts_offset_aware_string():
    # _utc_now_iso()が生成する形式（オフセット明示）
    assert generator._format_utc_as_jst("2026-07-25T03:26:54+00:00") == "2026-07-25 12:26"


def test_format_utc_as_jst_handles_none_and_empty():
    assert generator._format_utc_as_jst(None) is None
    assert generator._format_utc_as_jst("") is None


def test_format_utc_as_jst_crosses_midnight_into_next_day():
    # 2026-07-25 20:00:00 UTC -> 2026-07-26 05:00 JST（日付またぎ）
    assert generator._format_utc_as_jst("2026-07-25 20:00:00") == "2026-07-26 05:00"


def test_elapsed_hours_computes_hours_since_jst_timestamp():
    ten_hours_ago = (datetime.now(generator.JST) - timedelta(hours=10)).isoformat()
    hours = generator._elapsed_hours(ten_hours_ago)
    assert 9.9 < hours < 10.1


def test_format_elapsed_switches_between_minutes_hours_days():
    assert generator._format_elapsed(0.5) == "30分前"
    assert generator._format_elapsed(5) == "5時間前"
    assert generator._format_elapsed(50) == "2日前"


def _account(tmp_path, monkeypatch) -> Account:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return Account(name="testacct", screen_name="testacct", user_id="1")


def _seed_sent_reply(account: Account, candidate_id: str, reply_id: str) -> None:
    with rs_db.connect(account) as conn:
        rs_db.insert_candidates(conn, [
            rs_db.Candidate(
                id=candidate_id, text="元投稿", author_id="a1", author_screen_name="poster",
                created_at="2026-07-21T00:00:00+09:00", likes=1, retweets=0, replies=0, quotes=0,
                keyword="競馬",
            )
        ])
        rs_db.update_status(conn, candidate_id, "送信済み", reply_id=reply_id)


def test_unacknowledged_responses_section_empty_when_no_responses(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch)
    _seed_sent_reply(account, "cand-1", "reply-1")

    assert generator._unacknowledged_responses_section(account) == ""


def test_unacknowledged_responses_section_shows_count_and_overdue_flag(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch)
    _seed_sent_reply(account, "cand-1", "reply-1")
    old_created_at = (datetime.now(generator.JST) - timedelta(hours=72)).isoformat()

    with rs_db.connect(account) as conn:
        rs_db.save_reply_responses(conn, [
            rs_db.ReplyResponse(
                id="resp-1", parent_reply_id="reply-1", author_id="u1", author_username="fan",
                text="質問があります", created_at=old_created_at,
            )
        ])

    section = generator._unacknowledged_responses_section(account)
    assert "未対応の返信 1件" in section
    assert "早めの返信推奨" in section
    assert "@fan" in section
    assert "質問があります" in section


def test_unacknowledged_responses_section_omits_overdue_flag_within_48h(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch)
    _seed_sent_reply(account, "cand-1", "reply-1")
    recent_created_at = (datetime.now(generator.JST) - timedelta(hours=2)).isoformat()

    with rs_db.connect(account) as conn:
        rs_db.save_reply_responses(conn, [
            rs_db.ReplyResponse(
                id="resp-1", parent_reply_id="reply-1", author_id="u1", author_username="fan",
                text="質問があります", created_at=recent_created_at,
            )
        ])

    section = generator._unacknowledged_responses_section(account)
    assert "未対応の返信 1件" in section
    assert "早めの返信推奨" not in section
