from __future__ import annotations

from datetime import timedelta

from postdoctor.reply_scout import analyzer
from postdoctor.reply_scout.db import Candidate
from postdoctor.reply_scout.fetcher import ScoutConfig


def _cfg(**overrides) -> ScoutConfig:
    base = dict(
        keywords=["競馬"],
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


def _candidate(
    cid: str, text: str, likes: int, hours_ago: float = 1.0, is_quote: bool = False
) -> Candidate:
    created = analyzer.datetime.now(analyzer.JST) - timedelta(hours=hours_ago)
    return Candidate(
        id=cid, text=text, author_id=f"a-{cid}", author_screen_name=f"user{cid}",
        created_at=created.isoformat(), likes=likes, retweets=0, replies=0, quotes=0,
        keyword="競馬", author_followers=100, is_quote=is_quote,
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


def test_specificity_matches_horse_name_or_race_name_from_rakuba_data():
    """specificityはconfigの語彙リストではなく、prediction_dataが返す実在の
    馬名・レース名集合を直接参照する（旧specific_termsの再設計後の挙動）。"""
    horse_names = frozenset({"テストホース"})
    race_names = frozenset({"函館記念"})
    by_horse = _candidate("h", "テストホースが今回は狙えそう", 10)
    by_race = _candidate("r", "函館記念は荒れそうですね", 10)
    neither = _candidate("n", "今週も競馬を楽しみましょう", 10)

    assert analyzer._specificity(by_horse, horse_names, race_names) == 1.0
    assert analyzer._specificity(by_race, horse_names, race_names) == 1.0
    assert analyzer._specificity(neither, horse_names, race_names) == 0.0


def test_specificity_ignores_names_shorter_than_min_length():
    short_name = frozenset({"一"})
    candidate = _candidate("s", "一番人気はどれかな", 10)
    assert analyzer._specificity(candidate, short_name, frozenset()) == 0.0


def test_is_low_content_quote_true_only_for_quote_with_no_real_comment():
    silent_quote = _candidate(
        "sq", "🔥🥳 https://t.co/abc123", 10, is_quote=True
    )
    commented_quote = _candidate(
        "cq", "これは絶対に来る、狙い目だと思う https://t.co/abc123", 10, is_quote=True
    )
    plain_post = _candidate("p", "🔥🥳", 10, is_quote=False)

    assert analyzer._is_low_content_quote(silent_quote) is True
    assert analyzer._is_low_content_quote(commented_quote) is False
    assert analyzer._is_low_content_quote(plain_post) is False


def test_low_content_quotes_hard_excluded_from_final_ranking_and_preliminary_score():
    silent_quote = _candidate("sq", "🔥🥳 https://t.co/abc123", likes=1000, is_quote=True)
    plain = _candidate("p", "普通の投稿です", likes=1)
    cfg = _cfg()

    ranked = analyzer.final_ranking([silent_quote, plain], cfg, frozenset(), top_n=10)
    ids = [c.id for c, _score, _rank, _b in ranked]
    assert "sq" not in ids
    assert "p" in ids

    scored = analyzer.preliminary_score([silent_quote, plain], cfg, frozenset())
    ids2 = [c.id for c, _score in scored]
    assert "sq" not in ids2
    assert "p" in ids2


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


def test_topic_key_prefers_longest_match_and_none_when_no_match():
    horse_names = frozenset({"テスト", "テストホース"})
    both = _candidate("b", "テストホースが今回は狙えそう", 10)
    neither = _candidate("n", "今週も競馬を楽しみましょう", 10)

    assert analyzer._topic_key(both, horse_names) == "テストホース"
    assert analyzer._topic_key(neither, horse_names) is None


def test_final_ranking_dedups_by_topic_across_different_authors():
    """新種の重複: 著者は別人でも同じ馬について書かれた投稿が複数あると、
    fact_sentenceがほぼ同一のリプライを別々のアカウントに送ることになるため、
    著者dedupとは別に馬名(トピック)単位でも重複排除する。"""
    cfg = _cfg()
    horse_names = frozenset({"テストホース"})
    same_topic_high = _candidate("t1", "テストホースが強かった、期待できる", likes=100)
    same_topic_low = _candidate("t2", "テストホースについて一言、注目している", likes=50)
    same_topic_lowest = _candidate("t3", "テストホースの話題、気になるところ", likes=10)
    other_topic = _candidate("o", "別の話題の投稿", likes=1)

    ranked = analyzer.final_ranking(
        [same_topic_high, same_topic_low, same_topic_lowest, other_topic],
        cfg, horse_names, top_n=2,
    )
    ids = [c.id for c, _score, _rank, _b in ranked]
    assert ids == ["t1", "o"]
