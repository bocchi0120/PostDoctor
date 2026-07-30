from __future__ import annotations

from postdoctor import config
from postdoctor.config import Account
from postdoctor.reply_scout import db, fetcher, orchestrator
from postdoctor.reply_scout.fetcher import ScoutConfig


def _account(tmp_path, monkeypatch) -> Account:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    return Account(name="testacct", screen_name="testacct", user_id="1")


def _cfg(daily_read_limit: int) -> ScoutConfig:
    return ScoutConfig(
        keywords=["競馬"], specific_terms=[], ng_words=[], solicitation_words=[], analysis_terms=[],
        trusted_authors=[], daily_read_limit=daily_read_limit, min_likes=5, min_replies=0,
        top_user_lookup_limit=15, claude_model="claude-sonnet-5",
        draft_top_n=5, rakuba_output_dir="",
    )


def _seed_sent_reply(account: Account, candidate_id: str, reply_id: str) -> None:
    with db.connect(account) as conn:
        db.insert_candidates(conn, [
            db.Candidate(
                id=candidate_id, text="元投稿", author_id="a1", author_screen_name="poster",
                created_at="2026-07-21T00:00:00+09:00", likes=1, retweets=0, replies=0, quotes=0,
                keyword="競馬",
            )
        ])
        db.update_status(conn, candidate_id, "送信済み", reply_id=reply_id)


def test_run_track_skips_reply_detection_when_daily_budget_already_used(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch)
    _seed_sent_reply(account, "cand-1", "reply-1")

    monkeypatch.setattr(fetcher, "load_config", lambda: _cfg(daily_read_limit=10))
    monkeypatch.setattr(fetcher, "fetch_own_reply_metrics", lambda account, ids: {})

    def _boom(*args, **kwargs):
        raise AssertionError("fetch_reply_responses should not be called when budget is exhausted")

    monkeypatch.setattr(fetcher, "fetch_reply_responses", _boom)

    with db.connect(account) as conn:
        db.add_usage(conn, "tweet_read", 10)  # 本日分をscoutが既に使い切った想定

    log = orchestrator.run_track(account)

    assert any("上限" in line and "スキップ" in line for line in log)


def test_run_track_passes_remaining_budget_to_reply_detection(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch)
    _seed_sent_reply(account, "cand-1", "reply-1")

    monkeypatch.setattr(fetcher, "load_config", lambda: _cfg(daily_read_limit=10))
    monkeypatch.setattr(fetcher, "fetch_own_reply_metrics", lambda account, ids: {})

    captured = {}

    def _fake_fetch(account, targets, max_read=None):
        captured["max_read"] = max_read
        return [], 3

    monkeypatch.setattr(fetcher, "fetch_reply_responses", _fake_fetch)

    with db.connect(account) as conn:
        db.add_usage(conn, "tweet_read", 4)  # scoutが今日既に4件消費済み

    log = orchestrator.run_track(account)

    assert captured["max_read"] == 6  # 10 - 4
    with db.connect(account) as conn:
        assert db.get_today_usage(conn, "tweet_read") == 7  # 4(scout) + 3(reply detection)
    assert any("読み取り3件" in line for line in log)
