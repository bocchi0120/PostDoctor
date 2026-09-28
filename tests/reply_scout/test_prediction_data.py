from __future__ import annotations

import json

import pytest

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


def test_all_race_names_excludes_blank_race_names(pcfg):
    rows = pd.load_all_predictions(pcfg)
    names = pd.all_race_names(rows)
    assert "テストステークス" in names
    assert "関屋記念" in names
    assert "" not in names


def test_confirmed_race_keys(pcfg):
    results = pd.load_race_results(pcfg)
    confirmed = pd.confirmed_race_keys(results)
    assert "TESTRACE0001" in confirmed  # chakujun>0の行がある
    assert "TESTRACE0002" not in confirmed  # 結果行が無い（未確定）
    # TESTRACE0003は行はあるがchakujun>0が1件も無い（全頭除外）。行の存在だけでは
    # 確定と判定しない（詳細はconfirmed_race_keys()のdocstring参照）。
    assert "TESTRACE0003" not in confirmed
    # TESTRACE0004: 枠順未確定(uma_num=0)のプレースホルダ行のみでchakujun>0が無い。
    # 関屋記念で実際に発生したバグ（未来のレースが確定済み扱いされた）の回帰テスト。
    assert "TESTRACE0004" not in confirmed


def _match(pcfg, text, created_at=None):
    rows = pd.load_all_predictions(pcfg)
    results = pd.load_race_results(pcfg)
    confirmed = pd.confirmed_race_keys(results)
    return pd.find_match(text, rows, results, confirmed, created_at=created_at)


def test_find_match_by_horse_name_confirmed_race(pcfg):
    m, reason = _match(pcfg, "テストホースイチが強かった")
    assert m is not None
    assert reason is None
    assert m.horse.uma_code == "H0001"
    assert m.concluded is True
    assert m.chakujun == 2
    assert m.mark == "◎◎"
    assert "2着" in m.fact_sentence
    assert "1番手評価" in m.fact_sentence


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
    # TESTRACE0003はchakujun>0の行が無い(全頭除外)ため未確定扱いになる
    # （個別馬の除外表示自体は test_find_match_individual_exclusion_within_confirmed_race
    # で別途検証する）。
    assert m.concluded is False
    assert m.chakujun is None
    assert "除外" not in m.fact_sentence


def test_find_match_unconfirmed_race_omits_result_clause(pcfg):
    m, reason = _match(pcfg, "テストホースサンに注目")
    assert m is not None
    assert reason is None
    assert m.concluded is False
    assert m.chakujun is None
    assert "結果" not in m.fact_sentence


def test_find_match_individual_exclusion_within_confirmed_race(pcfg):
    """レース自体はchakujun>0の行があり確定済みだが、対象馬個体は出走除外(chakujun=0)
    だったケース。レースが確定している以上、除外の事実は正しく表示されるべき
    （全頭除外で未確定扱いになるTESTRACE0003とは区別する）。"""
    m, reason = _match(pcfg, "テストホースロクに期待していたのに")
    assert m is not None
    assert reason is None
    assert m.concluded is True
    assert m.chakujun == 0
    assert "除外" in m.fact_sentence


def test_find_match_future_race_with_placeholder_row_omits_result_clause(pcfg):
    """関屋記念で実際に発生したバグの回帰テスト: 枠順未確定(uma_num=0)の
    プレースホルダ行がrace_results.csvに先行して書き込まれていても、
    chakujun>0の行が無い限り「レース未確定」とみなし、結果節を一切付けない。"""
    m, reason = _match(pcfg, "テストホースゴに期待しています")
    assert m is not None
    assert reason is None
    assert m.concluded is False
    assert m.chakujun is None
    assert "除外" not in m.fact_sentence
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


def test_fact_sentence_honmei_gets_rank_label_and_separate_ai_judgement(pcfg):
    """pred_rank=1(本命)は「本命(1番手評価)」と表記し、AI判定(◎◎等)は
    レース単位の情報として別文で添える。「◎◎評価」のように個体評価と
    地続きの文面にはしない(AI判定と個体の順位評価の混同を避けるため)。"""
    m, reason = _match(pcfg, "テストホースイチが強かった")
    assert m is not None
    assert "本命(3頭中1番手評価)" in m.fact_sentence
    assert "このレースのAI判定は◎◎" in m.fact_sentence


def test_fact_sentence_non_honmei_uses_rank_only_no_ai_judgement_mark(pcfg):
    """pred_rank>=2の馬は「N番手評価」とだけ表記し、AI判定マーク(◎◎/◎/〇/△/×)は
    一切使わない(AI判定は本命自体の自信度であり、2番手以下には定義されない概念のため)。"""
    m, reason = _match(pcfg, "テストホースニに注目していました")
    assert m is not None
    assert "2番手評価" in m.fact_sentence
    assert "AI判定" not in m.fact_sentence
    for sym in ["◎◎", "◎", "〇", "△", "×"]:
        assert f"を{sym}" not in m.fact_sentence


@pytest.mark.parametrize(
    "text",
    [
        "テストホースイチが強かった",  # 本命・確定済み(的中系)
        "テストホースニに注目していました",  # 2番手・確定済み(見込み違い系)
        "テストホースゴに期待しています",  # 未確定・頭数不明
        "テストホースキュウは惜しかった",  # 低スコアの本命・2着
    ],
)
def test_fact_sentence_never_contains_raw_score(pcfg, text):
    """生スコア(0.25, -1.54等)は受け手に意味が伝わらない内部値なので、
    事実文には一切出さない(順位表記のみ)。"""
    m, reason = _match(pcfg, text)
    assert m is not None
    assert "スコア" not in m.fact_sentence
    assert f"{m.horse.score:.2f}" not in m.fact_sentence


def test_classify_uses_pred_rank_not_score():
    assert pd._classify(1, 2) == pd.CATEGORY_HIT
    assert pd._classify(3, 3) == pd.CATEGORY_HIT
    assert pd._classify(3, 4) == pd.CATEGORY_MISS_HIGH
    assert pd._classify(4, 1) == pd.CATEGORY_MISS_LOW
    assert pd._classify(4, 10) == pd.CATEGORY_AS_EXPECTED


def test_low_score_honmei_placing_2nd_is_not_outperformed(pcfg):
    """ジョバンニ(小倉記念)の実例の回帰テスト: スコアが低い(0.25)本命でも、
    2着なら「評価を大きく上回りました」とは書かない。高評価/低評価は
    score閾値ではなく順位(pred_rank<=3)で判定する。"""
    m, reason = _match(pcfg, "テストホースキュウは惜しかった")
    assert m is not None
    assert m.horse.pred_rank == 1
    assert m.chakujun == 2
    assert m.category == pd.CATEGORY_HIT
    assert "大きく上回" not in m.fact_sentence


def test_fact_sentence_includes_field_size_when_all_uma_num_confirmed(pcfg):
    m, _ = _match(pcfg, "テストホースイチが強かった")
    assert "本命(3頭中1番手評価)" in m.fact_sentence
    m, _ = _match(pcfg, "テストホースニに注目していました")
    assert "3頭中2番手評価" in m.fact_sentence


def test_fact_sentence_omits_field_size_when_placeholder_rows_present(pcfg):
    """uma_num==0(枠順未確定)の行を含むレースは頭数を確定できないので
    「N番手評価」のみ。確定済みの馬だけ数えると実際より少ない頭数を
    断定してしまうため、混在レースも頭数なし扱い。"""
    m, _ = _match(pcfg, "テストホースゴに期待しています")  # 全頭uma_num==0
    assert "本命(1番手評価)" in m.fact_sentence
    assert "頭中" not in m.fact_sentence
    m, _ = _match(pcfg, "テストホースジュウイチに期待")  # 確定/未確定の混在
    assert "本命(1番手評価)" in m.fact_sentence
    assert "頭中" not in m.fact_sentence


def test_field_size_counts_only_confirmed_uma_num():
    assert pd._field_size([{"uma_num": 1}, {"uma_num": 2}, {"uma_num": "3"}]) == 3
    assert pd._field_size([{"uma_num": 1}, {"uma_num": 0}]) is None
    assert pd._field_size([{"uma_num": 0}]) is None
    assert pd._field_size([]) is None


def test_compute_predictions_signature_none_when_dir_missing(tmp_path):
    pcfg = pd.load_prediction_config(str(tmp_path / "does_not_exist"))
    assert pd.compute_predictions_signature(pcfg) is None


def test_compute_predictions_signature_stable_for_unchanged_content(tmp_path):
    (tmp_path / "predictions.json").write_text(json.dumps({"races": []}), encoding="utf-8")
    pcfg = pd.load_prediction_config(str(tmp_path))
    assert pd.compute_predictions_signature(pcfg) == pd.compute_predictions_signature(pcfg)


def test_compute_predictions_signature_changes_when_rolling_file_updates(tmp_path):
    """枠順確定等でpredictions.jsonの内容が変わると指紋も変わる（下書きの陳腐化検知）。"""
    path = tmp_path / "predictions.json"
    path.write_text(json.dumps({"races": [{"race_key": "R1", "horses": []}]}), encoding="utf-8")
    pcfg = pd.load_prediction_config(str(tmp_path))
    before = pd.compute_predictions_signature(pcfg)

    path.write_text(json.dumps({"races": [{"race_key": "R1", "horses": [{"uma_num": 1}]}]}), encoding="utf-8")
    after = pd.compute_predictions_signature(pcfg)

    assert before != after


# 同名馬が複数レースにまたがってヒットするケース(TESTRACE0005=テストマリーンステークス
# 7/16・TESTRACE0006=テストエルムステークス8/9、どちらも「テストナナ」)。
# 実例(2026-08-09): 「ウェイワードアクト」がマリーンS・エルムSの両方に出走登録されており、
# マリーンSの予想投稿への下書きが、無関係な(開催日が新しいだけの)エルムSの事実文で
# 生成された。この回帰テストで固定する。


def test_find_match_disambiguates_by_race_name_when_multiple_races_match(pcfg):
    """本文に一方のレース名が明記されていれば、開催日の新旧に関わらずそちらを選ぶ。"""
    m, reason = _match(pcfg, "テストマリーンステークスのテストナナに注目")
    assert m is not None
    assert reason is None
    assert m.horse.race_key == "TESTRACE0005"
    assert "テストマリーンステークス" in m.fact_sentence


def test_find_match_disambiguates_by_jyo_name_when_multiple_races_match(pcfg):
    """レース名の明記が無くても、開催場名の共起で絞り込める。"""
    m, reason = _match(pcfg, "函館のテストナナが気になる")
    assert m is not None
    assert m.horse.race_key == "TESTRACE0005"


def test_find_match_real_incident_marine_s_not_overridden_by_newer_elm_s(pcfg):
    """実例そのものの回帰テスト: マリーンSの話題の投稿が、開催日が新しいだけの
    無関係なエルムSにすり替わらないこと。"""
    m, reason = _match(
        pcfg, "テストマリーンステークス、テストナナが強かったですね"
    )
    assert m is not None
    assert reason is None
    assert m.horse.race_key == "TESTRACE0005"
    assert m.horse.race_key != "TESTRACE0006"


def test_find_match_falls_back_to_created_at_proximity_without_race_hint(pcfg):
    """本文にレース名/開催場名の手がかりが無い場合、投稿日時に開催日が近い方を選ぶ。"""
    m, reason = _match(pcfg, "テストナナ、次も頑張ってほしい", created_at="2026-07-18T12:00:00+09:00")
    assert m is not None
    # 投稿日時(7/18)はTESTRACE0005(7/16)の方がTESTRACE0006(8/9)より近い
    assert m.horse.race_key == "TESTRACE0005"


def test_find_match_falls_back_to_latest_kaisai_date_without_any_hint(pcfg):
    """本文の手がかりも投稿日時も無い場合のみ、開催日が最も新しいものを選ぶ
    (従来の挙動。最終フォールバックとして維持)。"""
    m, reason = _match(pcfg, "テストナナ、次も頑張ってほしい")
    assert m is not None
    assert m.horse.race_key == "TESTRACE0006"


def test_fact_sentence_numbers_trace_to_fixture_data(pcfg):
    """事実文に現れる数値が、fixtureデータの実際の値と一致することを検証する
    （捏造防止の直接的な回帰テスト）。"""
    m, reason = _match(pcfg, "テストホースイチが強かった")
    assert m is not None
    # 生スコアは事実文に出さない方針のため、順位・頭数・着順の3つを突き合わせる
    assert f"{m.horse.field_size}頭中{m.horse.pred_rank}番手評価" in m.fact_sentence
    assert str(m.chakujun) in m.fact_sentence
