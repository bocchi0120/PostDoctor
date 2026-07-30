from __future__ import annotations

from datetime import timedelta

from postdoctor.reply_scout import analyzer
from postdoctor.reply_scout.db import Candidate
from postdoctor.reply_scout.fetcher import ScoutConfig


def _cfg(**overrides) -> ScoutConfig:
    base = dict(
        keywords=["競馬"],
        specific_terms=[],
        ng_words=["いいねで", "リポストで"],
        solicitation_words=["限定公開", "限定"],
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
    hype = _candidate("h", "いいねでリポストでお願いします！", likes=100)
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
        "bait", "いいねでリポストでお願いします、必勝情報！", likes=500
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
    hype = _candidate("h", "いいねでリポストでお願いします！", likes=50)
    plain = _candidate("p", "今日のレースについて", likes=50)
    scored = analyzer.preliminary_score([hype, plain], cfg, frozenset())
    by_id = {c.id: score for c, score in scored}
    assert by_id["h"] < by_id["p"]


def test_solicitation_words_hard_excluded_from_final_ranking():
    """問題3(B): 有料予想の売り込み等(solicitation_words)はスコア半減ではなく
    ランキングから完全除外されるべき(ng_wordsの煽り語とは異なる扱い)。"""
    cfg = _cfg()
    solicit = _candidate("s", "今だけ限定で有料予想を教えます", likes=1000)
    plain = _candidate("p", "普通の投稿です", likes=1)

    ranked = analyzer.final_ranking([solicit, plain], cfg, frozenset(), top_n=10)
    ids = [c.id for c, _score, _rank, _b in ranked]
    assert "s" not in ids
    assert "p" in ids


def test_solicitation_words_excluded_from_preliminary_score():
    cfg = _cfg()
    solicit = _candidate("s", "今だけ限定で有料予想を教えます", likes=1000)
    plain = _candidate("p", "普通の投稿です", likes=1)

    scored = analyzer.preliminary_score([solicit, plain], cfg, frozenset())
    ids = [c.id for c, _score in scored]
    assert "s" not in ids
    assert "p" in ids


def test_final_ranking_dedups_by_author():
    """問題2: 同一著者の投稿が複数枠を占めないよう著者単位で重複排除し、
    次点の別著者候補で枠を埋める(top_nのスロット数は減らさない)。"""
    cfg = _cfg()
    same_author_high = Candidate(
        id="a1", text="同じ人の投稿1", author_id="dup-author",
        author_screen_name="dup", created_at=analyzer.datetime.now(analyzer.JST).isoformat(),
        likes=100, retweets=0, replies=0, quotes=0, keyword="競馬", author_followers=100,
    )
    same_author_low = Candidate(
        id="a2", text="同じ人の投稿2", author_id="dup-author",
        author_screen_name="dup", created_at=analyzer.datetime.now(analyzer.JST).isoformat(),
        likes=90, retweets=0, replies=0, quotes=0, keyword="競馬", author_followers=100,
    )
    other = _candidate("o", "別の人の投稿", likes=1)

    ranked = analyzer.final_ranking(
        [same_author_high, same_author_low, other], cfg, frozenset(), top_n=2
    )
    ids = [c.id for c, _score, _rank, _b in ranked]
    assert ids == ["a1", "o"]
