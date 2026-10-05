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
        keywords=["競馬"], ng_words=[], solicitation_words=[], analysis_terms=[],
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


def _seed_scored_sent_reply(
    account: Account, candidate_id: str, reply_id: str, velocity_norm: float, impressions: int
) -> None:
    """score_factors + sent_replies(impressions付き)を1件分作る、相関計算テスト用のヘルパー。"""
    _seed_sent_reply(account, candidate_id, reply_id)
    with db.connect(account) as conn:
        db.save_score_factors(
            conn, candidate_id,
            velocity_norm=velocity_norm, follower_norm=0.5, specificity=0.0,
            trusted=0.0, analytical=0.0, ng_penalty_applied=False, final_score=velocity_norm,
        )
        db.update_sent_reply_metrics(conn, reply_id, impressions=impressions, likes=0, retweets=0, replies=0)


def test_summarize_score_correlations_flags_zero_variance_columns(tmp_path, monkeypatch):
    """specificity/trustedのように全件同値(分散ゼロ)の列はSpearman相関が定義できないため、
    NaNを出さず「分散なし」と明記する。velocity_normのように分散がある列は数値を出す。"""
    account = _account(tmp_path, monkeypatch)
    for i, (v, imp) in enumerate([(0.1, 5), (0.5, 20), (0.9, 80)]):
        _seed_scored_sent_reply(account, f"cand-{i}", f"reply-{i}", velocity_norm=v, impressions=imp)

    lines = orchestrator.summarize_score_correlations(account)
    joined = "\n".join(lines)
    assert "specificity: 分散なし" in joined
    assert "trusted: 分散なし" in joined
    assert "velocity_norm: Spearman相関 1.000" in joined


def test_summarize_score_correlations_recommends_review_above_threshold(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch)
    for i in range(31):
        _seed_scored_sent_reply(account, f"cand-{i}", f"reply-{i}", velocity_norm=i / 31, impressions=i + 1)

    lines = orchestrator.summarize_score_correlations(account)
    assert any("30件を超えました" in line for line in lines)


def test_summarize_score_correlations_silent_below_threshold(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch)
    _seed_scored_sent_reply(account, "cand-0", "reply-0", velocity_norm=0.5, impressions=10)

    lines = orchestrator.summarize_score_correlations(account)
    assert not any("30件を超えました" in line for line in lines)


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


def test_scout_keyword_groups_puts_graded_races_before_fixed_keywords(tmp_path):
    import json
    from dataclasses import replace
    from datetime import date

    from postdoctor.reply_scout import prediction_data

    out_dir = tmp_path / "output"
    out_dir.mkdir()
    (out_dir / "predictions_20261004.json").write_text(json.dumps({"races": [
        {"race_key": "R1", "kaisai_date": "20261004", "jyo_name": "東京", "race_num": 11,
         "race_name": "毎日王冠", "grade_code": "B", "horses": []},
        {"race_key": "R2", "kaisai_date": "20261004", "jyo_name": "京都", "race_num": 11,
         "race_name": "スワンステークス", "grade_code": "B", "horses": []},
    ]}, ensure_ascii=False), encoding="utf-8")
    cfg = replace(
        _cfg(daily_read_limit=50), keywords=["重賞", "毎日王冠", "競馬予想"],
        auto_graded_races=True, rakuba_output_dir=str(out_dir),
    )
    pcfg = prediction_data.load_prediction_config(cfg.rakuba_output_dir)

    groups = orchestrator.scout_keyword_groups(cfg, pcfg, date(2026, 10, 5))
    # 重賞名が先、固定語が後。固定語と重複する重賞名は1つにまとめる
    assert groups == [("毎日王冠",), ("スワンS", "スワンステークス"), ("重賞",), ("競馬予想",)]

    disabled = replace(cfg, auto_graded_races=False)
    assert orchestrator.scout_keyword_groups(disabled, pcfg, date(2026, 10, 5)) == [
        ("重賞",), ("毎日王冠",), ("競馬予想",),
    ]


def test_run_scout_logs_planned_and_searched_keywords(tmp_path, monkeypatch):
    account = _account(tmp_path, monkeypatch)
    monkeypatch.setattr(fetcher, "load_config", lambda: _cfg(daily_read_limit=50))
    monkeypatch.setattr(
        orchestrator, "scout_keyword_groups", lambda cfg, pcfg, today: [("毎日王冠",), ("重賞",)]
    )
    seen = {}

    def _fake_collect(account, conn, cfg, keyword_groups):
        seen["groups"] = keyword_groups
        return [], 0, [("毎日王冠", 7), ("重賞", 0)]

    monkeypatch.setattr(fetcher, "collect_candidates", _fake_collect)
    log = orchestrator.run_scout(account)
    assert seen["groups"] == [("毎日王冠",), ("重賞",)]
    assert log[0] == "検索キーワード（優先順）: 毎日王冠, 重賞"
    assert log[1] == "実際に検索: 毎日王冠(7件), 重賞(0件)"
