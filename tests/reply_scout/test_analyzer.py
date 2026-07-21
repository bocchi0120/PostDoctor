from __future__ import annotations

from datetime import timedelta

from postdoctor.reply_scout import analyzer
from postdoctor.reply_scout.db import Candidate
from postdoctor.reply_scout.fetcher import ScoutConfig


def _cfg(**overrides) -> ScoutConfig:
    base = dict(
        keywords=["競馬"],
        specific_terms=[],
        ng_words=["限定公開", "いいねで", "リポストで"],
        analysis_terms=["斤量", "適性"],
        trusted_authors=[],
        daily_read_limit=50,
        min_likes=5,
        min_replies=0,
        top_user_lookup_limit=15,
        claude_model="claude-sonnet-5",
        draft_top_n=5,
        rakuba_output_dir="",
    )
    base.update(overrides)
    return ScoutConfig(**base)


def _candidate(cid: str, text: str, likes: int, hours_ago: float = 1.0) -> Candidate:
    created = analyzer.datetime.now(analyzer.JST) - timedelta(hours=hours_ago)
    return Candidate(
        id=cid, text=text, author_id=f"a-{cid}", author_screen_name=f"user{cid}",
        created_at=created.isoformat(), likes=likes, retweets=0, replies=0, quotes=0,
        keyword="競馬", author_followers=100,
    )


def test_has_analytical_signal_requires_both_horse_name_and_term():
    cfg = _cfg()
    horse_names = frozenset({"テストホース"})
    with_both = _candidate("1", "テストホースの斤量が軽いのが気になる", 10)
    only_term = _candidate("2", "斤量の話をしていた", 10)
    only_name = _candidate("3", "テストホースが好き", 10)

    assert analyzer._has_analytical_signal(with_both, horse_names, cfg.analysis_terms) is True
    assert analyzer._has_analytical_signal(only_term, horse_names, cfg.analysis_terms) is False
    assert analyzer._has_analytical_signal(only_name, horse_names, cfg.analysis_terms) is False


def test_ng_words_halve_score_in_final_ranking():
    cfg = _cfg()
    hype = _candidate("h", "限定公開の情報です！いいねで教えます", likes=100)
    plain = _candidate("p", "普通の投稿です", likes=100)

    ranked = analyzer.final_ranking([hype, plain], cfg, frozenset(), top_n=10)
    by_id = {c.id: (score, breakdown) for c, score, _rank, breakdown in ranked}
    assert by_id["h"][1].ng_penalty_applied is True
    assert by_id["p"][1].ng_penalty_applied is False
    # 同条件（likes等）ならng該当の方がスコアは低くなる
    assert by_id["h"][0] < by_id["p"][0]


def test_analytical_post_outranks_hype_post_despite_fewer_likes():
    """問題3の受け入れテスト: 煽り投稿(いいね数が多い)より、
    考察系投稿(馬名+分析語の共起、いいね数は少ない)が上位に来るべき。"""
    cfg = _cfg()
    horse_names = frozenset({"テストホース"})
    hype_bait = _candidate(
        "bait", "限定公開！いいねでリポストで教えます、必勝情報！", likes=500
    )
    analytical = _candidate(
        "smart", "テストホースは斤量的にも適性的にも今回は狙えそう", likes=20
    )

    ranked = analyzer.final_ranking([hype_bait, analytical], cfg, horse_names, top_n=10)
    order = [c.id for c, _score, _rank, _b in ranked]
    assert order[0] == "smart"
    assert order[1] == "bait"


def test_preliminary_score_also_applies_ng_penalty():
    cfg = _cfg()
    hype = _candidate("h", "限定公開！絶対当たる", likes=50)
    plain = _candidate("p", "今日のレースについて", likes=50)
    scored = analyzer.preliminary_score([hype, plain], cfg, frozenset())
    by_id = {c.id: score for c, score in scored}
    assert by_id["h"] < by_id["p"]
