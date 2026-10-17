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


def test_find_match_short_name_rejected_when_race_name_blank(pcfg):
    """race_nameが空欄のレース(TESTRACE0003=中山9R)の短い馬名は、開催場名や
    開催場名+レース番号があっても一致させない(文脈条件を満たせないので安全側)。"""
    for text in ["中山でアオが出走するらしい", "中山9Rでアオが出走するらしい"]:
        m, reason = _match(pcfg, text)
        assert m is None, text
        assert reason == pd.REASON_NO_MATCH


def test_find_match_short_name_accepted_with_race_name(pcfg):
    m, reason = _match(pcfg, "テスト函館特別はジオが本命")
    assert m is not None
    assert m.horse.uma_code == "H0015"


@pytest.mark.parametrize(
    "text", ["函館10Rはジオ", "函館 10R ジオ", "函館10Ｒのジオ", "函館10レースのジオ"]
)
def test_find_match_short_name_accepted_with_venue_and_race_number(pcfg, text):
    """開催場名+レース番号はレースを特定できるので文脈条件を満たす(表記ゆれ込み)。"""
    m, reason = _match(pcfg, text)
    assert m is not None, text
    assert m.horse.uma_code == "H0015"


def test_find_match_short_name_rejected_with_venue_only(pcfg):
    """開催場名だけではレースを特定できない(「函館記念」の「函館」等)。"""
    m, reason = _match(pcfg, "函館記念の日にジオを見た")
    assert m is None
    # レース番号違いも不可
    m, reason = _match(pcfg, "函館11Rのジオ")
    assert m is None


def test_find_match_short_name_uses_core_race_name():
    """「農林水産省賞典　小倉記念」のような冠名付き正式名称でも、投稿側の通称で一致させる。"""
    assert pd._core_race_name("農林水産省賞典　小倉記念") == "小倉記念"
    assert pd._core_race_name("関屋記念") == "関屋記念"
    assert pd._core_race_name("") == ""


def test_find_match_short_name_with_sponsored_race_name(pcfg):
    m, reason = _match(pcfg, "テスト小倉杯はエヒで決まり")
    assert m is not None
    assert m.horse.uma_code == "H0017"


def test_find_match_radio_does_not_match_jio(pcfg):
    """「ジオ」が「ラジオNIKKEI」の一部に一致しない(文脈条件は満たしていても)。"""
    m, reason = _match(pcfg, "テスト函館特別はラジオNIKKEIで観戦")
    assert m is None


def test_find_match_cranford_does_not_match_kuran(pcfg):
    """短い馬名「クラン」が、別馬名「クランフォード」の一部に一致しない。"""
    m, reason = _match(pcfg, "テスト函館特別 S評価 クランフォード")
    assert m is None


def test_name_at_word_boundary():
    assert pd._name_at_word_boundary("ジオ", "ジオ、強い") is True
    assert pd._name_at_word_boundary("ジオ", "ラジオNIKKEI") is False
    assert pd._name_at_word_boundary("クラン", "クランフォード") is False
    assert pd._name_at_word_boundary("クラン", "クランー") is False  # 長音
    assert pd._name_at_word_boundary("ライカ", "ディクタストライカ/ウマ娘") is False
    assert pd._name_at_word_boundary("ジオ", "ｼﾞｵ") is False  # 半角は別表記なので不一致
    assert pd._name_at_word_boundary("ジオ", "ﾗジオ") is False  # 直前が半角カナ
    # 一度でも境界上に現れればTrue
    assert pd._name_at_word_boundary("ジオ", "ラジオとジオ") is True


def test_find_match_candidate9_common_noun_short_name_loses_to_long_name(pcfg):
    """候補#9(@simazaemonyo)の構図の回帰テスト: 「函館記念」の「函館」だけを
    文脈として、一般名詞「サイン」が函館9Rの馬『サイン』に誤マッチし、本文の
    話題である長い馬名の馬より優先されていた。"""
    text = "換金しなくてよかった！函館記念テストラーゼン応援馬券にやっとサインもらえたよー！"
    m, reason = _match(pcfg, text, created_at="2026-08-30T17:30:12+09:00")
    assert m is not None
    assert m.horse.uma_name == "テストラーゼン"


def test_find_match_common_noun_short_name_alone_not_matched(pcfg):
    """長い馬名が無くても、開催場名だけでは『サイン』に一致しない。"""
    m, reason = _match(pcfg, "函館記念の馬券にサインもらえた", created_at="2026-08-30T17:30:12+09:00")
    assert m is None
    # レースを特定できる手がかりがあれば一致する(馬名自体を禁止しているわけではない)
    m, reason = _match(pcfg, "テスト北斗特別はサインが勝った")
    assert m is not None
    assert m.horse.uma_code == "H0014"


def test_find_match_long_name_preferred_over_short_name(pcfg):
    """長い馬名と短い馬名の両方が一致したら、文脈が揃っていても長い方を採用する。"""
    m, reason = _match(pcfg, "テスト北斗特別のサインより、テストラーゼンが気になる")
    assert m is not None
    assert m.horse.uma_name == "テストラーゼン"


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


@pytest.mark.parametrize(
    "field_size, expected",
    [(None, 3), (0, 3), (5, 3), (9, 3), (10, 4), (12, 4), (13, 5), (14, 5), (15, 5), (16, 6), (18, 6)],
)
def test_upper_eval_max_pred_rank_is_top_third_with_floor_of_three(field_size, expected):
    assert pd.upper_eval_max_pred_rank(field_size) == expected


def test_classify_upper_eval_band_scales_with_field_size():
    # 凡走で着順が評価順位より下 -> 見込み違い系
    assert pd._classify(6, 17, 18) == pd.CATEGORY_MISS_HIGH
    assert pd._classify(4, 9, 14) == pd.CATEGORY_MISS_HIGH
    # 凡走でも着順が評価順位以内 -> 順当系(評価通り)
    assert pd._classify(5, 5, 14) == pd.CATEGORY_AS_EXPECTED
    assert pd._classify(6, 4, 18) == pd.CATEGORY_AS_EXPECTED
    # 好走 -> 中立系(的中系にも完敗系にもしない)
    assert pd._classify(5, 1, 16) == pd.CATEGORY_NEUTRAL
    assert pd._classify(4, 3, 18) == pd.CATEGORY_NEUTRAL
    # 上位3分の1より下は従来通り
    assert pd._classify(7, 17, 18) == pd.CATEGORY_AS_EXPECTED
    assert pd._classify(7, 2, 18) == pd.CATEGORY_MISS_LOW
    assert pd._classify(6, 10, 14) == pd.CATEGORY_AS_EXPECTED
    # 3番手以内は頭数によらず従来通り
    assert pd._classify(3, 3, 18) == pd.CATEGORY_HIT
    assert pd._classify(3, 4, 18) == pd.CATEGORY_MISS_HIGH
    # 小頭数は下限3(6頭立ての3番手は高評価のまま、4番手は低評価)
    assert pd._classify(3, 2, 6) == pd.CATEGORY_HIT
    assert pd._classify(4, 6, 6) == pd.CATEGORY_AS_EXPECTED
    # 頭数不明は従来通り3固定
    assert pd._classify(6, 17, None) == pd.CATEGORY_AS_EXPECTED
    assert pd._classify(5, 1, None) == pd.CATEGORY_MISS_LOW


def _horse(name, race_name, field_size, pred_rank):
    return pd.HorseRow(
        race_key="X", kaisai_date="20261012", jyo_name="京都", race_num="11",
        race_name=race_name, distance=1400, pred_rank=pred_rank, uma_num=1,
        uma_code="U", uma_name=name, score=0.0, field_size=field_size,
    )


def test_upper_eval_bad_result_is_not_as_expected_shonan_xanadu():
    """ショウナンザナドゥ(2026-10-12スワンS)の実例の回帰テスト: 18頭中6番手評価で17着を
    「評価通り」と書かない。上位3分の1の評価で着順が評価順位より下なら見込み違い系。"""
    horse = _horse("ショウナンザナドゥ", "ＭＢＳ賞スワンステークス", 18, 6)
    sentence, category = pd._build_fact_sentence(horse, "△", 17)
    assert category == pd.CATEGORY_MISS_HIGH
    assert "18頭中6番手評価としていましたが、結果は17着にとどまりました。" in sentence
    assert "評価通り" not in sentence


def test_upper_eval_finishing_within_pred_rank_is_as_expected_elton_barows():
    """エルトンバローズ(2026-07-26関屋記念)の実例: 14頭中5番手評価で5着は、着順が
    評価順位以内なので凡走扱いにせず「評価通り」のまま。"""
    horse = _horse("エルトンバローズ", "関屋記念", 14, 5)
    sentence, category = pd._build_fact_sentence(horse, "△", 5)
    assert category == pd.CATEGORY_AS_EXPECTED
    assert "14頭中5番手評価としていて、結果も5着と評価通りでした。" in sentence
    assert "とどまりました" not in sentence


def test_upper_eval_good_result_uses_neutral_sentence():
    """4番手〜上位3分の1の評価の馬が好走しても「好走しました」(的中の水増し)とも
    「大きく上回りました」とも書かず、中立の文面にする。"""
    horse = _horse("ワイワイレジェンド", "ＮＳＴ賞", 15, 5)
    sentence, category = pd._build_fact_sentence(horse, "△", 1)
    assert category == pd.CATEGORY_NEUTRAL
    assert "15頭中5番手評価としていて、結果は1着でした。" in sentence
    assert "好走" not in sentence
    assert "上回" not in sentence


def test_every_category_has_a_tone_instruction():
    from postdoctor.reply_scout import prescriber

    for category in (
        pd.CATEGORY_HIT, pd.CATEGORY_MISS_HIGH, pd.CATEGORY_MISS_LOW,
        pd.CATEGORY_AS_EXPECTED, pd.CATEGORY_NEUTRAL,
    ):
        assert prescriber._CATEGORY_TONE.get(category)


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


# --- graded_race_keywords（scoutの検索キーワード用の直近重賞） ---------------------

from datetime import date  # noqa: E402


def _race(race_key, kaisai_date, race_name, grade_code, jyo_name="東京", race_num=11):
    return {
        "race_key": race_key, "kaisai_date": kaisai_date, "jyo_name": jyo_name,
        "race_num": race_num, "race_name": race_name, "grade_code": grade_code, "horses": [],
    }


def _write_predictions(out_dir, filename, races):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / filename).write_text(json.dumps({"races": races}, ensure_ascii=False), encoding="utf-8")


def _graded(tmp_path, today, *, aliases=None, cancelled=None, max_races=4):
    out_dir = tmp_path / "output"
    cancelled_path = None
    if cancelled is not None:
        cancelled_path = tmp_path / "cancelled_races.json"
        cancelled_path.write_text(json.dumps(cancelled, ensure_ascii=False), encoding="utf-8")
    return pd.graded_race_keywords(
        pd.load_prediction_config(str(out_dir)), today,
        cancelled_path=cancelled_path, aliases=aliases or {}, max_races=max_races,
    )


@pytest.mark.parametrize(
    "today, expected",
    [
        (date(2026, 10, 5), (date(2026, 10, 3), date(2026, 10, 6))),    # 月: 前週末のみ
        (date(2026, 10, 6), (date(2026, 10, 3), date(2026, 10, 6))),    # 火: 当日が終端
        (date(2026, 10, 9), (date(2026, 10, 3), date(2026, 10, 13))),   # 金: 前週末＋当週末
        (date(2026, 10, 10), (date(2026, 10, 3), date(2026, 10, 13))),  # 土: 当日は始端にしない
        (date(2026, 10, 11), (date(2026, 10, 10), date(2026, 10, 13))),  # 日: 前日の土曜が始端
    ],
)
def test_graded_race_window_previous_saturday_to_next_tuesday(today, expected):
    assert pd.graded_race_window(today) == expected


def test_graded_race_keywords_filters_grade_and_window(tmp_path):
    _write_predictions(tmp_path / "output", "predictions_20261004.json", [
        _race("R1", "20261004", "毎日王冠", "B"),
        _race("R2", "20261004", "京都大賞典", "B", jyo_name="京都"),
        _race("R3", "20261004", "グリーンチャンネルカップ", "L"),
        _race("R4", "20261004", "大原ステークス", "E"),
    ])
    _write_predictions(tmp_path / "output", "predictions_20260927.json", [
        _race("R5", "20260927", "スプリンターズステークス", "A", jyo_name="中山"),  # 窓の外
    ])
    groups = _graded(tmp_path, date(2026, 10, 5))
    assert groups == [("毎日王冠",), ("京都大賞典",)]


def test_graded_race_keywords_puts_current_weekend_before_previous_g1(tmp_path):
    _write_predictions(tmp_path / "output", "predictions_20261010.json", [
        _race("R1", "20261010", "サンプルG3", "C"),
        _race("R2", "20261010", "サンプルG2", "B"),
    ])
    _write_predictions(tmp_path / "output", "predictions_20261004.json", [
        _race("R3", "20261004", "前週G2", "B"),
        _race("R4", "20261004", "前週G1", "A"),
    ])
    # 金・土は当週末(10/10〜)の重賞が先、前週末(10/3〜4)はG1でもその後ろ
    expected = [("サンプルG2",), ("サンプルG3",), ("前週G1",), ("前週G2",)]
    assert _graded(tmp_path, date(2026, 10, 9)) == expected
    assert _graded(tmp_path, date(2026, 10, 10)) == expected


def test_graded_race_keywords_orders_by_grade_then_closeness_within_weekend(tmp_path):
    _write_predictions(tmp_path / "output", "predictions_20261003.json", [
        _race("R1", "20261003", "土曜G2", "B"),
        _race("R2", "20261003", "土曜G3", "C"),
    ])
    _write_predictions(tmp_path / "output", "predictions_20261004.json", [
        _race("R3", "20261004", "日曜G2", "B"),
        _race("R4", "20261004", "日曜G1", "A"),
    ])
    # 月曜(10/5)は前週末が「当週末」: G1→G2→G3、同じG2なら今日に近い日曜が先
    assert _graded(tmp_path, date(2026, 10, 5)) == [
        ("日曜G1",), ("日曜G2",), ("土曜G2",), ("土曜G3",),
    ]


@pytest.mark.parametrize(
    "today, expected",
    [
        (date(2026, 10, 10), date(2026, 10, 10)),  # 土
        (date(2026, 10, 11), date(2026, 10, 10)),  # 日
        (date(2026, 10, 12), date(2026, 10, 10)),  # 月(祝日開催)
        (date(2026, 10, 13), date(2026, 10, 10)),  # 火(代替開催・月曜分の結果確定)
        (date(2026, 10, 14), date(2026, 10, 17)),  # 水: 次の週末
        (date(2026, 10, 16), date(2026, 10, 17)),  # 金
    ],
)
def test_current_weekend_start(today, expected):
    assert pd.current_weekend_start(today) == expected


def test_graded_race_keywords_caps_at_max_races(tmp_path):
    _write_predictions(tmp_path / "output", "predictions_20261004.json", [
        _race(f"R{i}", "20261004", f"重賞{i}", "C", race_num=i) for i in range(1, 7)
    ])
    assert len(_graded(tmp_path, date(2026, 10, 5), max_races=4)) == 4


def test_graded_race_keywords_adds_s_variant_and_normalizes_width(tmp_path):
    _write_predictions(tmp_path / "output", "predictions_20261004.json", [
        _race("R1", "20261004", "スワンステークス", "B"),
        _race("R2", "20261004", "中京２歳ステークス", "C"),
        _race("R3", "20261004", "ＣＢＣ賞", "C"),
    ])
    groups = _graded(tmp_path, date(2026, 10, 5))
    # X検索は「〜S」と「〜ステークス」を同一視しないので両表記をOR検索する
    assert ("スワンS", "スワンステークス") in groups
    assert ("中京2歳S", "中京2歳ステークス") in groups
    assert ("CBC賞",) in groups


def test_graded_race_keywords_applies_aliases_and_core_name(tmp_path):
    _write_predictions(tmp_path / "output", "predictions_20261004.json", [
        _race("R1", "20261004", "関西テレビ放送賞ローズステークス", "B"),
        _race("R2", "20261004", "農林水産省賞典　新潟記念", "C", jyo_name="新潟"),
    ])
    groups = _graded(
        tmp_path, date(2026, 10, 5),
        aliases={"関西テレビ放送賞ローズステークス": ["ローズS", "ローズステークス"]},
    )
    assert groups == [("ローズS", "ローズステークス"), ("新潟記念",)]


def test_graded_race_keywords_alias_key_must_match_fullwidth_race_name(tmp_path):
    """Rakubaのrace_nameは全角英字(ＭＢＳ)のことがある。race_aliasesのキーはその表記と
    完全一致で引くので、半角(MBS)で書いたキーは効かず、冠名付きの検索語のままになる
    （2026-10-13、「MBS賞スワンS」で検索していて通称「スワンS」の投稿を拾えなかった）。"""
    _write_predictions(tmp_path / "output", "predictions_20261012.json", [
        _race("R1", "20261012", "ＭＢＳ賞スワンステークス", "B", jyo_name="京都"),
        _race("R2", "20261010", "サウジアラビアロイヤルカップ", "C"),
    ])
    aliases = {
        "ＭＢＳ賞スワンステークス": ["スワンS", "スワンステークス"],
        "サウジアラビアロイヤルカップ": ["サウジアラビアRC", "サウジアラビアロイヤルカップ"],
    }
    assert _graded(tmp_path, date(2026, 10, 13), aliases=aliases) == [
        ("スワンS", "スワンステークス"),
        ("サウジアラビアRC", "サウジアラビアロイヤルカップ"),
    ]
    halfwidth_key = {"MBS賞スワンステークス": ["スワンS", "スワンステークス"]}
    assert _graded(tmp_path, date(2026, 10, 13), aliases=halfwidth_key)[0] == (
        "MBS賞スワンS", "MBS賞スワンステークス",
    )


def test_graded_race_keywords_excludes_cancelled_date_keeps_rescheduled(tmp_path):
    """順延前の日付のキー(中止日)は除外し、代替開催日の新しいキーは残す。同名は1つに。"""
    _write_predictions(tmp_path / "output", "predictions_20260921.json", [
        _race("2026092106040711", "20260921", "順延重賞", "B", jyo_name="中山"),
        _race("2026092109040711", "20260921", "別場重賞", "B", jyo_name="阪神"),
    ])
    _write_predictions(tmp_path / "output", "predictions_20260922.json", [
        _race("2026092206040711", "20260922", "順延重賞", "B", jyo_name="中山"),
    ])
    cancelled = [{"kaisai_date": "20260921", "jyo_name": "中山", "race_nums": None,
                  "rescheduled_to": "20260922"}]
    groups = _graded(tmp_path, date(2026, 9, 22), cancelled=cancelled)
    assert groups.count(("順延重賞",)) == 1
    assert ("別場重賞",) in groups

    # 中止記録が無ければ順延前の行も対象だが、同じ表記は重複しない
    assert _graded(tmp_path, date(2026, 9, 22)).count(("順延重賞",)) == 1


def test_graded_race_keywords_cancellation_by_race_num(tmp_path):
    _write_predictions(tmp_path / "output", "predictions_20261004.json", [
        _race("R11", "20261004", "中止重賞", "B", race_num=11),
        _race("R10", "20261004", "残る重賞", "C", race_num=10),
    ])
    cancelled = [{"kaisai_date": "20261004", "jyo_name": "東京", "race_nums": [11]}]
    assert _graded(tmp_path, date(2026, 10, 5), cancelled=cancelled) == [("残る重賞",)]


def test_graded_race_keywords_degrades_on_missing_or_broken_files(tmp_path):
    # 出力ディレクトリ自体が無い
    assert _graded(tmp_path, date(2026, 10, 5)) == []
    # 中止ファイルが壊れていても除外なしで動く
    _write_predictions(tmp_path / "output", "predictions_20261004.json", [
        _race("R1", "20261004", "毎日王冠", "B"),
    ])
    (tmp_path / "output" / "predictions_20261003.json").write_text("{broken", encoding="utf-8")
    broken = tmp_path / "cancelled_races.json"
    broken.write_text("{broken", encoding="utf-8")
    groups = pd.graded_race_keywords(
        pd.load_prediction_config(str(tmp_path / "output")), date(2026, 10, 5),
        cancelled_path=broken, aliases={},
    )
    assert groups == [("毎日王冠",)]


def test_graded_race_keywords_rolling_file_overrides_snapshot(tmp_path):
    """同じrace_keyならrolling(predictions.json)の内容が勝つ（load_all_predictionsと同じ規約）。"""
    _write_predictions(tmp_path / "output", "predictions_20261004.json", [
        _race("R1", "20261004", "旧名称", "B"),
    ])
    _write_predictions(tmp_path / "output", "predictions.json", [
        _race("R1", "20261004", "新名称", "B"),
    ])
    assert _graded(tmp_path, date(2026, 10, 5)) == [("新名称",)]


def test_low_eval_not_good_result_but_better_than_pred_rank_is_neutral_rosham_park():
    """ローシャムパーク(2026-08-16札幌記念)の実例の回帰テスト: 16頭中14番手評価で4着は
    評価を上回っているので「評価通り」と書かず、中立の文面にする。"""
    horse = _horse("ローシャムパーク", "札幌記念", 16, 14)
    sentence, category = pd._build_fact_sentence(horse, "△", 4)
    assert category == pd.CATEGORY_NEUTRAL
    assert "16頭中14番手評価としていて、結果は4着でした。" in sentence
    assert "評価通り" not in sentence


@pytest.mark.parametrize(
    "pred_rank, chakujun, field_size, expected",
    [
        # 低評価・好走でない: 着順が評価順位より上 -> 中立系
        (14, 4, 16, pd.CATEGORY_NEUTRAL),
        (14, 13, 16, pd.CATEGORY_NEUTRAL),
        (7, 4, 18, pd.CATEGORY_NEUTRAL),
        # 低評価・好走でない: 着順が評価順位と同じか下 -> 順当系（低評価が当たった形）
        (14, 14, 16, pd.CATEGORY_AS_EXPECTED),
        (14, 16, 16, pd.CATEGORY_AS_EXPECTED),
        (7, 17, 18, pd.CATEGORY_AS_EXPECTED),
        (4, 4, None, pd.CATEGORY_AS_EXPECTED),  # 頭数不明なら4番手は低評価
        # 低評価・好走 -> 完敗系（変更なし）
        (14, 3, 16, pd.CATEGORY_MISS_LOW),
        (7, 1, 18, pd.CATEGORY_MISS_LOW),
        # 上位評価・高評価の帯は変更なし
        (6, 4, 18, pd.CATEGORY_AS_EXPECTED),
        (6, 17, 18, pd.CATEGORY_MISS_HIGH),
        (5, 1, 16, pd.CATEGORY_NEUTRAL),
        (3, 4, 18, pd.CATEGORY_MISS_HIGH),
        (3, 3, 18, pd.CATEGORY_HIT),
    ],
)
def test_classify_low_eval_band_compares_finish_with_pred_rank(
    pred_rank, chakujun, field_size, expected
):
    assert pd._classify(pred_rank, chakujun, field_size) == expected


# --- 発走済み・結果待ちの判定 ---------------------------------------------------

def _write_rakuba_dir(tmp_path, races, snapshot_races=None, race_info_rows=None, results_rows=None):
    """predictions.json(+スナップショット)・race_info.csv・race_results.csvを持つ仮のRakuba出力。"""
    out = tmp_path / "rakuba_output"
    out.mkdir()
    (out / "predictions.json").write_text(
        json.dumps({"generated_at": "x", "n_races": len(races), "races": races}, ensure_ascii=False),
        encoding="utf-8",
    )
    if snapshot_races is not None:
        (out / "predictions_20260801.json").write_text(
            json.dumps({"races": snapshot_races}, ensure_ascii=False), encoding="utf-8"
        )
    if race_info_rows is not None:
        lines = ["race_key,kaisai_date,start_time,race_name"]
        lines += [f"{k},{d},{t},{n}" for k, d, t, n in race_info_rows]
        (out / "race_info.csv").write_text("﻿" + "\n".join(lines) + "\n", encoding="utf-8")
    lines = ["race_key,uma_code,uma_num,chakujun"]
    lines += [f"{k},{u},{n},{c}" for k, u, n, c in (results_rows or [])]
    (out / "race_results.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pd.load_prediction_config(str(out))


def _started_race(race_key, kaisai_date, start_time=None, horse="シュガークン", pred_rank=8):
    race = {
        "race_key": race_key, "kaisai_date": kaisai_date, "jyo_name": "東京", "race_num": "11",
        "distance": 1600, "race_name": "富士ステークス",
        "horses": [
            {"pred_rank": pred_rank, "uma_num": 13, "uma_code": "U1", "uma_name": horse, "score": 0.1},
            {"pred_rank": 1, "uma_num": 1, "uma_code": "U2", "uma_name": "ベツノウマデス", "score": 0.9},
        ],
    }
    if start_time is not None:
        race["start_time"] = start_time
    return race


def test_load_all_predictions_reads_start_time_and_backfills_from_race_info(tmp_path):
    """predictions.jsonのstart_timeを読み、無いスナップショットはrace_info.csvで補う。
    race_info.csvにも無いレースは""のまま。先頭ゼロ("0955")は壊さない。"""
    pcfg = _write_rakuba_dir(
        tmp_path,
        races=[_started_race("R_NEW", "20261017", start_time="1545")],
        snapshot_races=[
            _started_race("R_OLD", "20260801", horse="フルイウマデス"),
            _started_race("R_UNKNOWN", "20260801", horse="ナゾノウマデス"),
        ],
        race_info_rows=[("R_OLD", "20260801", "0955", "x"), ("R_NEW", "20261017", "9999", "x")],
    )
    starts = {(h.race_key, h.uma_code): h.start_time for h in pd.load_all_predictions(pcfg)}
    assert starts[("R_NEW", "U1")] == "1545"  # predictions.jsonの値が優先
    assert starts[("R_OLD", "U1")] == "0955"
    assert starts[("R_UNKNOWN", "U1")] == ""


def test_load_all_predictions_without_race_info_keeps_empty_start_time(tmp_path):
    pcfg = _write_rakuba_dir(tmp_path, races=[_started_race("R1", "20261017")])
    assert {h.start_time for h in pd.load_all_predictions(pcfg)} == {""}


@pytest.mark.parametrize(
    "kaisai_date, start_time, now, expected",
    [
        ("20261017", "1545", "2026-10-17T15:45:00+09:00", True),   # 発走時刻ちょうどは発走済み
        ("20261017", "1545", "2026-10-17T15:44:59+09:00", False),
        ("20261017", "1545", "2026-10-17T06:45:00+00:00", True),   # UTCで渡しても同じ時刻
        ("20261017", "1545", "2026-10-17T15:50:00", True),          # タイムゾーン無しはJST
        ("20261017", "", "2026-10-18T12:00:00+09:00", False),       # 発走時刻不明は判定しない
        ("20261017", "15:45", "2026-10-18T12:00:00+09:00", False),  # 形式が違えば判定しない
        ("", "1545", "2026-10-18T12:00:00+09:00", False),
    ],
)
def test_has_started_compares_jst_start_with_now(kaisai_date, start_time, now, expected):
    horse = pd.HorseRow(
        race_key="R", kaisai_date=kaisai_date, jyo_name="東京", race_num="11", race_name="x",
        distance=1600, pred_rank=1, uma_num=1, uma_code="U", uma_name="ウマ", score=0.0,
        start_time=start_time,
    )
    assert pd.has_started(horse, pd.datetime.fromisoformat(now)) is expected


def _rows_results(pcfg):
    rows = pd.load_all_predictions(pcfg)
    results = pd.load_race_results(pcfg)
    return rows, results, pd.confirmed_race_keys(results)


_SUGAR_TEXT = "シュガークン号騎乗の吉村騎手は中央重賞4勝目。単勝配当4160円"


def test_find_match_skips_started_race_whose_result_is_pending_sugar_kun(tmp_path):
    """シュガークン(2026-10-17富士S)の実例の回帰テスト: 発走(15:45)後で、結果の行が
    chakujun=0のプレースホルダだけなら、「…評価としています」と返さず結果待ちで見送る。"""
    pcfg = _write_rakuba_dir(
        tmp_path, races=[_started_race("R1", "20261017", start_time="1545")],
        results_rows=[("R1", "U1", 13, 0), ("R1", "U2", 1, 0)],
    )
    rows, results, confirmed = _rows_results(pcfg)
    after = pd.datetime.fromisoformat("2026-10-17T17:48:00+09:00")
    m, reason = pd.find_match(_SUGAR_TEXT, rows, results, confirmed, now=after)
    assert m is None
    assert reason == pd.REASON_RESULT_PENDING


def test_find_match_before_start_or_without_now_keeps_pre_race_sentence(tmp_path):
    pcfg = _write_rakuba_dir(tmp_path, races=[_started_race("R1", "20261017", start_time="1545")])
    rows, results, confirmed = _rows_results(pcfg)
    before = pd.datetime.fromisoformat("2026-10-17T15:00:00+09:00")
    for kwargs in ({"now": before}, {}):
        m, reason = pd.find_match(_SUGAR_TEXT, rows, results, confirmed, **kwargs)
        assert reason is None
        assert m.fact_sentence.endswith("8番手評価としています。")


def test_find_match_started_race_with_unknown_start_time_is_not_skipped(tmp_path):
    pcfg = _write_rakuba_dir(tmp_path, races=[_started_race("R1", "20261017")])
    rows, results, confirmed = _rows_results(pcfg)
    later = pd.datetime.fromisoformat("2026-10-20T12:00:00+09:00")
    m, reason = pd.find_match(_SUGAR_TEXT, rows, results, confirmed, now=later)
    assert reason is None and m is not None


def test_find_match_started_and_confirmed_race_cites_result(tmp_path):
    """結果が入れば（chakujun>0の行がある）、発走済みでも結果付きの文面になる。"""
    pcfg = _write_rakuba_dir(
        tmp_path, races=[_started_race("R1", "20261017", start_time="1545")],
        results_rows=[("R1", "U1", 13, 1), ("R1", "U2", 1, 5)],
    )
    rows, results, confirmed = _rows_results(pcfg)
    later = pd.datetime.fromisoformat("2026-10-19T15:00:00+09:00")
    m, reason = pd.find_match(_SUGAR_TEXT, rows, results, confirmed, now=later)
    assert reason is None
    assert m.chakujun == 1
    assert "結果は1着" in m.fact_sentence



def test_graded_race_keyword_plan_flags_current_weekend_races(tmp_path):
    """当週末の重賞だけTrue。並びと中身はgraded_race_keywords()と同じ。"""
    _write_predictions(tmp_path / "output", "predictions_20261017.json", [
        _race("R1", "20261017", "富士ステークス", "C"),
        _race("R2", "20261018", "秋華賞", "A"),
    ])
    _write_predictions(tmp_path / "output", "predictions_20261012.json", [
        _race("R3", "20261012", "ＭＢＳ賞スワンステークス", "B"),
    ])
    pcfg = pd.load_prediction_config(str(tmp_path / "output"))
    aliases = {"ＭＢＳ賞スワンステークス": ["スワンS", "スワンステークス"]}
    plan = pd.graded_race_keyword_plan(pcfg, date(2026, 10, 17), cancelled_path=None, aliases=aliases)
    assert [current for _v, current in plan] == [True, True, False]
    assert [v for v, _c in plan] == pd.graded_race_keywords(
        pcfg, date(2026, 10, 17), cancelled_path=None, aliases=aliases
    )
    assert plan[2][0] == ("スワンS", "スワンステークス")
