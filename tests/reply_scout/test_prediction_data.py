from __future__ import annotations

from postdoctor.reply_scout import prediction_data as pd


def test_mark_for_score_boundaries():
    assert pd.mark_for_score(0.95) == "◎◎"
    assert pd.mark_for_score(0.9) == "◎◎"
    assert pd.mark_for_score(0.89) == "◎"
    assert pd.mark_for_score(0.6) == "◎"
    assert pd.mark_for_score(0.59) == "〇"
    assert pd.mark_for_score(0.3) == "〇"
    assert pd.mark_for_score(0.29) == "△"
    assert pd.mark_for_score(-0.2) == "△"
    assert pd.mark_for_score(-0.21) == "×"


def test_load_all_predictions_merges_and_excludes_malformed(pcfg):
    rows = pd.load_all_predictions(pcfg)
    codes = {r.uma_code for r in rows}
    # 不正なファイル名（predictions_0620.json, predictions_20260627_20260628.json）は除外される
    assert "DECOY" not in codes
    assert "DECOY2" not in codes
    # 正規のレースは全て読み込まれる
    assert {"H0001", "H0002", "H0003", "H0004"} <= codes


def test_load_all_predictions_rolling_wins_over_snapshot(pcfg):
    rows = pd.load_all_predictions(pcfg)
    h0001 = next(r for r in rows if r.uma_code == "H0001")
    # snapshot(predictions_20260630.json)はscore=0.10だが、rolling(predictions.json)の
    # score=0.95が優先されるはず
    assert h0001.score == 0.95


def test_confirmed_race_keys(pcfg):
    results = pd.load_race_results(pcfg)
    confirmed = pd.confirmed_race_keys(results)
    assert "TESTRACE0001" in confirmed  # chakujun>0の行がある
    assert "TESTRACE0003" in confirmed  # chakujun=0(全除外)でもレース自体は確定済み
    assert "TESTRACE0002" not in confirmed  # 結果行が無い（未確定）


def _match(pcfg, text):
    rows = pd.load_all_predictions(pcfg)
    results = pd.load_race_results(pcfg)
    confirmed = pd.confirmed_race_keys(results)
    return pd.find_match(text, rows, results, confirmed)


def test_find_match_by_horse_name_confirmed_race(pcfg):
    m, reason = _match(pcfg, "テストホースイチが強かった")
    assert m is not None
    assert reason is None
    assert m.horse.uma_code == "H0001"
    assert m.concluded is True
    assert m.chakujun == 2
    assert m.mark == "◎◎"
    assert "2着" in m.fact_sentence
    assert "0.95" in m.fact_sentence


def test_find_match_race_name_only_no_longer_matches(pcfg):
    """レース名だけの言及からの「本命採用」フォールバックは廃止済み。
    馬名そのものへの言及が無ければマッチしない。"""
    m, reason = _match(pcfg, "今日のテストステークス、楽しみですね")
    assert m is None
    assert reason == pd.REASON_NO_MATCH


def test_find_match_skips_pre_race_framing_after_race_concluded(pcfg):
    """レース確定後に「前哨戦分析」等のレース前フレーミング投稿へ返信すると
    時制が噛み合わないため、ハードスキップする。"""
    m, reason = _match(pcfg, "テストホースイチの前哨戦分析、参戦が楽しみです")
    assert m is None
    assert reason == pd.REASON_TIME_MISMATCH


def test_find_match_short_name_without_context_is_rejected(pcfg):
    # 「アオ」は3文字以下なので、レース名/開催場名との共起が無ければ採用しない
    m, reason = _match(pcfg, "アオという単語だけの投稿")
    assert m is None
    assert reason == pd.REASON_NO_MATCH


def test_find_match_short_name_with_context_is_accepted(pcfg):
    m, reason = _match(pcfg, "中山でアオが出走するらしい")
    assert m is not None
    assert reason is None
    assert m.horse.uma_code == "H0004"
    assert m.concluded is True
    assert m.chakujun == 0
    assert "除外" in m.fact_sentence


def test_find_match_unconfirmed_race_omits_result_clause(pcfg):
    m, reason = _match(pcfg, "テストホースサンに注目")
    assert m is not None
    assert reason is None
    assert m.concluded is False
    assert m.chakujun is None
    assert "結果" not in m.fact_sentence


def test_find_match_no_match_returns_none(pcfg):
    m, reason = _match(pcfg, "競馬とは関係のない投稿です")
    assert m is None
    assert reason == pd.REASON_NO_MATCH


def test_find_match_category_hit_when_high_eval_and_good_result(pcfg):
    # H0001: score=0.95(高評価), chakujun=2(好走) -> 的中系
    m, reason = _match(pcfg, "テストホースイチが強かった")
    assert m is not None
    assert m.category == pd.CATEGORY_HIT
    assert "好走" in m.fact_sentence


def test_find_match_category_miss_high_when_high_eval_and_bad_result(pcfg):
    # H0002: score=0.55(高評価), chakujun=5(凡走) -> 見込み違い系
    m, reason = _match(pcfg, "テストホースニに注目していました")
    assert m is not None
    assert m.category == pd.CATEGORY_MISS_HIGH
    assert "5着" in m.fact_sentence


def test_fact_sentence_numbers_trace_to_fixture_data(pcfg):
    """事実文に現れる数値が、fixtureデータの実際の値と一致することを検証する
    （捏造防止の直接的な回帰テスト）。"""
    m, reason = _match(pcfg, "テストホースイチが強かった")
    assert m is not None
    expected_score_str = f"{m.horse.score:.2f}"
    assert expected_score_str in m.fact_sentence
    assert str(m.chakujun) in m.fact_sentence
