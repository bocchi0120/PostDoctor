# PostDoctor / Rakuba 設計判断記録

作成: 2026-07-30（claude.ai上での企画〜実運用3週間の会話より抽出）
位置づけ: CLAUDE.mdの「なぜそうなっているか」を補完する経緯資料。
ルールの現在値はCLAUDE.md/READMEが正。矛盾があればそちらを優先。

---

## 1. プロダクトの成り立ち

- 発端は「SNSコンテンツの知名度を上げるアプリ（SEO対策ソフト）が欲しい」という構想。
  X向けには SEO ではなく「アルゴリズム最適化＋運用支援」が本質と整理。
- 4モジュール構想: A投稿分析 / Bトレンド監視 / C競合分析 / D投稿文生成A/Bテスト。
  共通基盤（X API取得→SQLite→分析）の上に載せる設計。実装はA→リプライ営業(reply_scout)の順で進んだ。
- 名称「PostDoctor」= 診察(データ取得)→カルテ(蓄積)→処方箋(改善提案)→経過観察(効果測定)
  という医療メタファーが機能設計の指針。
- Rakuba(@rakuba_ai)は「患者第1号」。PostDoctorは汎用SaaS化を見据えた設計とし、
  Rakuba形式の知識は prediction_data.py に隔離（将来の他形式対応を容易にするため）。
- X APIは従量課金（Basic新規受付終了後）。設計原則:「同じデータを二度読まない」
  （since_id差分取得、SQLite永続化、読み取り上限config、実行ごとのコスト表示）。

## 2. ブランド原則（Rakuba「検証中」の思想）

これらは技術仕様より上位の制約。機能追加時は必ずこの原則に照らす。

1. **数字を絶対に盛らない・捏造しない**。外れも隠さず開示する。
   - AI判定(◎◎/◎/〇/△/×)は本命馬自身の自信度。自信がないレースも×として公開する。
   - 的中率が下がっても「実際に投稿した予想」で判定した数字だけを出す。
2. **自動送信・自動いいね・自動フォローは絶対に実装しない**（X自動化ルール対策 + 品質担保）。
   アプリは「候補の発見と下書き生成」まで。送信は必ず人間が確認して手動。
3. **有料予想屋・エンゲージメント乞い系アカウントを持ち上げない**。
   「検証中」の誠実系ブランドが煽り系を称賛すると同類に見られ、ブランド毀損となる。

## 3. 捏造防止の設計思想（reply_scoutの核）

「プロンプト任せにせず、コードで担保する」が大原則。

- 事実文(fact_sentence)はPython側で実データから決定的に組み立てる。
  LLMには「反応文」のみ生成させ、数値・マーク・着順は書かせない。
- 多重防御: LLM出力に数字・マーク・漢数字着順が混入したら案ごと破棄。
- 予測データにマッチしない候補はLLM呼び出し自体を行わない（コストゼロ・捏造ゼロ）。
- この設計のおかげで、バグが「もっともらしい嘘の文章」ではなく
  「事実文の異常」として即座に発見できた（→4節の時制バグ）。

## 4. 実運用で踏んだバグと対策（三層防御の成立経緯）

1. **時制バグ（未来レースへの結果捏造）**: race_results.csvに枠順未確定の
   プレースホルダ行（全馬chakujun=0）が先行書き込みされるため、
   「行が存在する＝確定」判定だと未来レースに「(結果除外)」が付いた。
   → confirmed_race_keys()を「chakujun>0の行が1件以上」に変更。
   トレードオフ（全頭除外の実在レースを未確定扱い）は安全側に倒すと判断。
   **エラーの非対称性**: 機会損失(軽) vs 捏造・ブランド毀損(致命) → 常に安全側。
2. **時制スキップ**: レース確定後に「前哨戦分析」等のレース前投稿へ返信するのは不自然
   → フレーミング語で検知しスキップ。過検知（過去+未来が混在する投稿）は許容する判断
   （(a)許容を採用。実例がスコアカードに貯まってから近傍判定(b)を再検討）。
3. **鮮度バグ（下書きの陳腐化）**: 予測データ更新後（枠順確定でスコア/本命が変動）に
   古い下書きが残留し、公式投稿と矛盾するリスク。
   実例: ダノンセンチュリー◎→枠順確定後アンパドゥ◎に入れ替わり。
   → 下書きにpredictions.jsonのsha256を保存、ズレたら「再生成推奨」警告+送信時confirm。
4. **古い下書きの残留**: draftsに既存下書きがあるとdraft再実行でスキップされる仕様のため、
   生成ロジック変更・データ更新後は DELETE FROM drafts が必要（README記載済み）。
   sent_replies（送信履歴・反応データ）とはFK無しの別テーブルで、削除の影響なし。
5. **published版問題（最重要・未完了確認）**: 結果発表の的中判定が「投稿時点のスナップショット」
   ではなく最新predictions.jsonを参照し、好走馬に◎が付け替わった発表を生成
   （◎的中率が10/15に見えたが実投稿ベースでは7/15）。投稿直前に人間の照合で発見・阻止。
   → 対策方針: 投稿時にその時点のスナップショットを「published版」として凍結保存し、
   結果判定はそれを参照する。**結果発表は投稿前に必ず実投稿と突合する運用を継続。**
6. **タイムスタンプ混在**: sent_repliesの保存時刻がUTC/JST混在 → DB保存はUTC統一、表示でJST変換に修正済み。
7. **メインレース動的判定**: サマータイム開催でメインが11R→7R等に移動。
   「11R決め打ち」を廃し番組データから動的判定（季節で自動追従）に修正済み。
8. **反応生成の宛先誤読**: 2026-08-03、trusted_authors/specific_terms投入後の再scoutで
   実際に発生。候補投稿「頑張ってや💪楽しみにしていますよ🥳」（第三者が馬を応援する投稿）
   に対し、Claudeが「応援ありがとうございます、励みになります」という反応文を生成——
   候補投稿の応援コメントをRakuba自身へ向けられたものと誤読していた。scout候補は
   キーワード検索で見つかる第三者の独立投稿でRakuba宛メンションは構造上あり得ない
   （`-is:reply`で除外済み）が、`prescriber.SYSTEM_PROMPT`にその前提が無く、Claudeが
   二人称・感謝表現を素朴に自分宛と解釈する穴があった。→ SYSTEM_PROMPTに明記して対策。
   `NUMERIC_MARK_RE`のような機械的な事後チェックは見送り
   （「感謝が自分宛かどうか」は文脈依存で正規表現では誤検知しやすいため、プロンプト側の
   指示のみとする）。
9. **同名馬の複数レース混同**: find_match()が同名の馬(例:ウェイワードアクト)が
   複数レースの予測データに存在する場合、候補投稿の話題と無関係な別レースのデータを
   誤ってciteするバグがあった(2026-08-09、7/16マリーンS予想投稿への下書きが、無関係な
   8/9エルムSの同名馬評価をciteした実例で発覚)。原因は、重複排除(race_key,uma_codeキー)
   のために使っていた「開催日最新優先」ロジックが、実際には「同名馬の別レース判別」にも
   流用されてしまっていたこと(本文がどのレースの話かを一切見ていなかった)。
   → 修正: find_match()の選定を3段階に変更。(1)本文中のレース名・開催場名での
   絞り込みを最優先、(2)手がかりが無ければ投稿日時(候補のcreated_at)に開催日が
   近い方を優先、(3)それでも決まらなければ従来通り開催日最新優先(最終フォールバック)。
   回帰テストは実例(マリーンS/エルムS)の構図をそのままfixture化して固定。
10. **AI判定と個体の順位評価の混同（2026-09-25発覚）**: `_build_fact_sentence()`が
    マッチした馬のpred_rank(1〜N番手、レース内の相対評価)に関わらず一律
    `mark_for_score(その馬自身のscore)`を「◎評価」「△評価」のように個体の評価
    として文面化していた。しかしRakuba側(`ops/x_post.py`のMARK_NOTE「AI判定は
    ◎(本命)自体の自信度です」、`_full_mark(top_score)`が`horses[0]`=本命のscore
    のみから算出)では、AI判定(◎◎/◎/〇/△/×)は**そのレースの本命馬1頭にしか
    定義されないレース単位の指標**であり、2番手以下の馬の「相対評価」とは別概念。
    実例: 2番手評価(pred_rank=2)の馬を評価スコアだけでmark_for_score()にかけて
    「△評価としています」のように書いてしまい、AI判定マークがその馬個体の評価で
    あるかのような誤解を招く文面になっていた。
    → 修正: pred_rank==1(本命)は「本命(1番手評価)」と表記し、AI判定は
    「(このレースのAI判定は◎◎)」のように**別文で**添える。pred_rank>=2は
    「N番手評価」とだけ表記し、AI判定マーク自体を一切使わない（そもそも
    定義されない概念のため）。
    【2026-09-28訂正】当初ここに「5節の改善方針（事実文の『マーク+順位』化）を実現した」
    と書き、§10バックログからも削除していたが、**実態は未完了だった**。置き換えたのは
    マーク部分だけで、直後の`(スコア{horse.score:.2f})`は初回実装(6ae8b17)から一度も
    消えておらず、9/25以降の下書きにも「本命(1番手評価)(スコア0.25)」のように生スコアが
    残っていた（過去のバックアップDBでも7/26以降ほぼ全下書きに生スコアあり）。
    ドキュメントが実装より先に「完了」と書いていたことが原因。完了は11項の修正で実現。
11. **生スコアの残存と、スコア閾値判定による本命の矛盾文（2026-09-28発覚）**:
    (a) 上記の通り事実文に生スコア(スコア0.25/-1.54等)が残っていた。
    (b) `_classify()`が高評価/低評価を**スコア閾値**(0.3)で判定していたため、スコアの
    低い本命が2着だと「本命(1番手評価)としていましたが、結果は2着と、評価を大きく
    上回りました」という自己矛盾した文になっていた（実例: 小倉記念『ジョバンニ』、
    スコア0.25）。生スコアが併記されている間はまだ読めたが、削除すると矛盾が露出する。
    → 修正: (a) 事実文から生スコアを完全削除
    (`prediction_data._build_fact_sentence()`の`prefix`)。(b) `_classify()`を順位ベースに
    変更し、`pred_rank <= HIGH_EVAL_MAX_PRED_RANK(=3)`を高評価とする（事実文の
    「N番手評価」とトーンのカテゴリが同じ基準で揃う）。(c) 頭数を添えて
    「18頭中3番手評価」とする(`HorseRow.field_size`/`_field_size()`)。uma_num==0の行は
    頭数に数えず、**レース内に1頭でもuma_num==0が混在すれば頭数不明**として
    「N番手評価」のみにする（実データで1,129レース中10レースに確定/未確定の混在があり、
    確定分だけ数えると実頭数より少ない数を断定してしまうため）。
    回帰テスト: `tests/reply_scout/test_prediction_data.py`の
    `test_fact_sentence_never_contains_raw_score`、
    `test_low_score_honmei_placing_2nd_is_not_outperformed`（ジョバンニの構図をfixture
    TESTRACE0007として固定）、`test_classify_uses_pred_rank_not_score`、
    `test_fact_sentence_includes_field_size_when_all_uma_num_confirmed`、
    `test_fact_sentence_omits_field_size_when_placeholder_rows_present`(TESTRACE0008)、
    `test_field_size_counts_only_confirmed_uma_num`。

12. **短い馬名の一般名詞・部分文字列への誤マッチ（2026-09-28発覚）**: 候補#9
    (@simazaemonyo)「函館記念ファウストラーゼン応援馬券に…サインもらえた」が、函館9R
    北斗特別の馬『サイン』に誤マッチした。「サイン」は3文字なので短い馬名対策の対象
    だったが、原因は3つ重なっていた。(1) 短い馬名の文脈条件が「レース名**または開催場名**」
    で、「函館記念」の中の「函館」だけで満たされた。(2) 本文の話題である長い馬名
    『ファウストラーゼン』にも一致していたが、`_select_candidate()`(同名馬の別レース判別用)
    が一致の強さを見ずに開催日の近さで『サイン』を選んだ。(3) 馬名を単純な部分文字列で
    照合しており、保存済み候補の短い馬名一致(延べ約40件)の大半が「ジオ」⊂「ラジオNIKKEI」、
    「クラン」⊂「クランフォード」、「ライカ」⊂「ディクタストライカ」等の誤マッチだった。
    → 修正(`prediction_data.py`):
    A. 全馬名で、一致部分の前後がカタカナ(長音・半角カナ含む)なら不一致
       (`_name_at_word_boundary()`)。
    B. 短い馬名(3文字以下)の文脈条件を、レース名(冠名を除いた通称、`_core_race_name()`)
       または開催場名+レース番号(「函館9R」「函館 9R」「函館9Ｒ」「函館9レース」)に強化。
       開催場名だけでは不可。**race_nameが空欄のレースでは短い馬名を一致させない**
       (文脈条件を満たせないため安全側。`_race_specific_context()`)。
    C. 長い馬名の一致がある場合は短い馬名の一致を候補から外してから`_select_candidate()`へ
       (`find_match()`内)。
    実データ(保存済み全候補)で再判定すると、短い馬名の一致は本物6件(エヒト/小倉記念、
    ジュタ/関屋記念)のみ残り、誤マッチは全て消えた。代償として、race_name空欄の函館記念
    (6/28)に出たジュタについて「函館 11R 函館記念 ジュタ」と書いた本物の投稿2件も一致しなく
    なった（race_name空欄は一律不一致とする方針による、受け入れ済みのトレードオフ）。
    回帰テスト(`tests/reply_scout/test_prediction_data.py`):
    `test_find_match_candidate9_common_noun_short_name_loses_to_long_name`、
    `test_find_match_common_noun_short_name_alone_not_matched`、
    `test_find_match_long_name_preferred_over_short_name`、
    `test_find_match_radio_does_not_match_jio`、`test_find_match_cranford_does_not_match_kuran`、
    `test_name_at_word_boundary`、`test_find_match_short_name_accepted_with_venue_and_race_number`
    (表記ゆれ4種)、`test_find_match_short_name_rejected_with_venue_only`、
    `test_find_match_short_name_rejected_when_race_name_blank`、
    `test_find_match_short_name_with_sponsored_race_name`、`test_find_match_short_name_uses_core_race_name`。

13. **反応文が相手の予想との一致・不一致を主張する（2026-09-29発覚）**: 候補#7
    (@naanaashii_1)の案1が「札幌記念、グランディア軸ですね。自分も近い見立てでした」
    だった。反応文を書くClaudeにはRakubaの予測内容を渡していない（捏造防止のため事実は
    fact_sentenceに分離している）ので、相手の印とRakubaの印を比較しようがなく、この種の
    主張は根拠の無い捏造になる。過去の下書き(ユニーク301件)にも「自分も高く見てたので」
    「こちらの見立てとは違う結果」「という見立て納得です」等が計5件あった。
    → 修正(`prescriber.py`): (a) SYSTEM_PROMPTに一致・不一致の主張を禁じる【厳守】制約を
    追加（#7の文面を例示）。(b) NUMERIC_MARK_REと同じ多重防御として`AGREEMENT_CLAIM_RE`を
    追加し、該当した案は`_call_claude()`で破棄（再試行1回→プレースホルダ）。正規表現は
    過去の反応文301件で較正し、主張を含む5件のみ一致・「納得感あります」「こちらも
    気になってました」等の無害な表現は非一致を確認。
    回帰テスト(`tests/reply_scout/test_prescriber.py`): `test_system_prompt_forbids_agreement_claims`、
    `test_agreement_claim_re_detects_claims`、`test_agreement_claim_re_allows_plain_reactions`、
    `test_generate_drafts_discards_candidate7_agreement_claim_and_retries`。

14. **先頭の検索キーワードが日次読み取り上限を使い切っていた（2026-10-05発覚）**:
    `collect_candidates()`は各キーワードの`max_results`を「残り上限全部」にしていたため、
    先頭の「函館記念」(6/28のレース)の検索で毎回50件を使い切り、2番目の「重賞」は
    2026-09-08を最後に一度も検索されていなかった。函館記念での候補も「函館記念も喉鳴りが」
    のような、ついでに触れているだけの投稿が大半だった。
    → 修正(`fetcher.py`): 残り上限をキーワードごとに均等に割り振る。`max_results`の下限が
    10なので検索回数は「残り÷10」回まで（上限50なら5回）、届かなかった枠は後ろの語に回す。
    総量(`daily_read_limit`)は変えていない。あわせて固定語から「函館記念」を外し、直近の
    重賞名を自動で加える方式にした（5節）。
    回帰テスト(`tests/reply_scout/test_fetcher.py`):
    `test_collect_candidates_splits_budget_evenly_across_keywords`、
    `test_collect_candidates_drops_keywords_beyond_query_count`、
    `test_collect_candidates_carries_unused_budget_forward`、
    `test_collect_candidates_respects_already_used_budget`。

15. **多頭数の4〜6番手評価が凡走しても「評価通り」になっていた（2026-10-13発覚）**:
    高評価の閾値が頭数によらず3番手固定(`HIGH_EVAL_MAX_PRED_RANK=3`)だったため、4番手以下は
    すべて低評価扱いで、凡走すると「結果もN着と評価通りでした」になっていた。実例:
    スワンS『ショウナンザナドゥ』18頭中6番手評価→17着、チャレンジC『フィーリウス』
    16頭中6番手→10着（送信済み）、関越S『ディマイザキッド』16頭中4番手→7着（送信済み）。
    上位3分の1に入れていた馬の大敗を「評価通り」と言うのは実態と合わず、外れを軽く見せる
    方向の誤りなので、「外れも隠さず開示する」原則(2節)に反する。
    → 修正(`prediction_data._classify()`/`upper_eval_max_pred_rank()`): 評価を3帯にした。
    高評価=3番手以内（従来通り）、上位評価=4番手〜`max(3, ceil(頭数/3))`番手、低評価=それ以下
    （従来通り）。上位評価の帯だけ次のように扱う。
    - 4着以下で着順が評価順位より下 → 見込み違い系（「結果はN着にとどまりました」）。
    - 4着以下でも着順が評価順位以内 → 順当系のまま（例: 関屋記念『エルトンバローズ』
      14頭中5番手→5着。評価順位どおりの着順を「とどまりました」と書くのは逆に不正確）。
    - 3着以内 → 新カテゴリ「中立系」（「としていて、結果はN着でした」）。的中系にしないのは、
      5番手評価の馬の勝利を「好走しました」と誇らしいトーンで書くと的中の水増しになるため。
      完敗系（「評価を大きく上回りました」）にもしないのは、上位に見ていた馬だから。
      凡走側だけ閾値を広げ、好走側の的中判定は3番手以内のまま、という非対称は意図的。
    - 下限3は小頭数対策（6頭立てで閾値が2になり3番手が低評価扱いになるのを防ぐ）。
      頭数不明(`field_size=None`)は3で、上位評価の帯は無くなり従来通り。
    過去の下書き済み・結果確定の123件に当てた場合、文面が変わるのは凡走側5組・好走側4組
    （馬×レース単位）。7番手以下の「評価通り」（例: 16頭中14番手→4着）は今回の対象外。
    回帰テスト(`tests/reply_scout/test_prediction_data.py`):
    `test_upper_eval_max_pred_rank_is_top_third_with_floor_of_three`、
    `test_classify_upper_eval_band_scales_with_field_size`、
    `test_upper_eval_bad_result_is_not_as_expected_shonan_xanadu`、
    `test_upper_eval_finishing_within_pred_rank_is_as_expected_elton_barows`、
    `test_upper_eval_good_result_uses_neutral_sentence`、
    `test_every_category_has_a_tone_instruction`。

16. **scoutの検索内訳が残らず、保存されなかった理由を後から確認できなかった（2026-10-13発覚）**:
    重賞名の自動追加（14項）後の初回scout（10/13）は50件読んで保存2件だったが、
    `run_scout()`のログ「実際に検索: …」はブラウザ/コンソールに返すだけで、語ごとの読み取り数も
    落とした理由も残っていなかった。コードと状況から「ほぼ全件がいいね5未満」と推定はできたが
    （recent searchは直近7日分のみで、既存候補は8日以上前の投稿＝既知IDはあり得ない）、
    実数は復元できなかった。
    → 修正: `reply_scout.db`に`scout_searches`テーブルを追加（`db.py`の`SCHEMA`、
    `save_scout_searches()`/`get_scout_searches()`）。検索1回につき1行で、クエリ文字列・
    `max_results`・読み取り数・保存数・落とした理由別の件数（既知ID／いいね等不足／同一実行内の
    重複）を持つ。`fetcher.collect_candidates()`は任意引数`stats`に内訳を追記するだけで、
    検索・選別の挙動は変えていない（重複した投稿も従来通り`collected`に入り、
    `insert_candidates()`のON CONFLICTで無視される）。実行ログにも
    「検索語ごとの保存: スワンS 読取10→保存1（いいね等不足9）」の行を追加
    （`orchestrator._search_stat_summary()`）。ログファイルではなくDBにしたのは、既存の
    バックアップ手順に乗り、後からSQLで集計できるため。ダッシュボード表示は未実装。
    回帰テスト: `tests/reply_scout/test_fetcher.py`の
    `test_collect_candidates_records_per_keyword_stats`、
    `test_collect_candidates_stats_do_not_change_results`、`tests/reply_scout/test_db.py`の
    `test_init_db_adds_scout_searches_to_existing_db_without_touching_other_tables`、
    `test_save_and_get_scout_searches_keeps_order_and_separates_runs`、
    `test_save_scout_searches_with_no_stats_writes_nothing`、
    `tests/reply_scout/test_orchestrator.py`の
    `test_run_scout_saves_search_stats_and_logs_saved_counts`、
    `test_run_scout_records_nothing_when_no_search_ran`。
    同日、`race_aliases`に「ＭＢＳ賞スワンステークス」「サウジアラビアロイヤルカップ」を追加
    （10/13は「MBS賞スワンS」「サウジアラビアロイヤルカップ」で検索しており、通称
    「スワンS」「サウジアラビアRC」の投稿を拾えていなかった）。**キーはRakubaの`race_name`と
    完全一致**で引くので、全角英字（ＭＢＳ）はそのまま全角で書く
    （`test_graded_race_keywords_alias_key_must_match_fullwidth_race_name`）。
    見送り（2026-10-13）: 通称の自動生成（〜カップ→C、冠名の自動除去）、counts APIでの
    表記ゆれ確認、`sort_order="relevancy"`、検索語の削減。まず`scout_searches`の実測を
    数回分ためてから判断する。
    2026-10-17: 実測2回（10/16・10/17）とも、読み取り50件のうち保存は3件で、当週末の重賞
    （秋華賞・富士S）は2回とも10件すべてがいいね等不足だった。新しい順では発走前後に集中する
    直近の投稿ばかり読むため、`sort_order="relevancy"`を**当週末の重賞（自動キーワード）だけ**で
    試す（`orchestrator.relevancy_keyword_groups()`、`prediction_data.graded_race_keyword_plan()`、
    `fetcher.collect_candidates(relevancy_groups=)`）。前週末の重賞と固定語（「重賞」等）は
    従来どおりsort_orderを送らない（API既定のrecency）。読み取り上限・1検索語10件・min_likesは
    変えていない。並び順は`scout_searches.sort_order`に残し（列追加前の行はNULL=新しい順）、
    実行ログでは「富士S[関連度順] 読取10→保存…」と出す。比べ方:
    `SELECT COALESCE(sort_order,'recency'), SUM(read_count), SUM(saved_count) FROM scout_searches
    GROUP BY 1`。数回分ためて、保存率が上がらなければ戻す。
    2026-10-24: 4回分（10/16〜10/24、19検索）で比べると、保存率は新しい順9%（156件中14件）に
    対し関連度順36%（44件中16件）、保存分のいいね平均は27.1対77.4、馬名照合できた候補は2件対6件。
    → **全検索語を関連度順に**した。例外は設定で選べる: `config/keywords.json`の
    `"search_sort_order": {"default": "relevancy", "recency_keywords": [...]}`
    （`recency_keywords`に挙げた語＝表記の組のどれかは新しい順。`default`を`recency`にすれば
    全語を従来どおりに戻せる。未知の値はエラー）。`fetcher.sort_order_for()`で組ごとに決め、
    当週末の重賞だけを選んでいた`orchestrator.relevancy_keyword_groups()`と
    `collect_candidates(relevancy_groups=)`は削除。新しい順の検索には従来どおりsort_orderを送らない。
    同時に固定語「競馬予想」を外した（`keywords`は「重賞」のみ）。10/19に一度検索され
    13件読んで保存1件（地方の名古屋9R、Rakubaの対象外）。読み取りの割り振りは従来どおり
    残数を検索語で等分する（例: 自動の重賞2つ＋「重賞」なら16・17・17件）。
    テスト: `test_collect_candidates_sends_relevancy_for_all_groups_by_default`、
    `test_collect_candidates_recency_keywords_send_no_sort_order`、
    `test_collect_candidates_default_recency_sends_no_sort_order`、
    `test_load_config_reads_search_sort_order`、`test_load_config_rejects_unknown_sort_order`。

17. **反応文に、データに無い主張が入り込む（2026-10-13発覚）**: 過去の反応文351件
    （現行DB＋バックアップ、重複除去）を全件確認し、既存の2フィルタ（数字・マーク、一致主張）を
    すり抜ける2種類を見つけた。
    (a) **自分の過去の評価・期待の主張（9件）**: 「ローシャムパークは期待してただけに」
    （札幌記念で16頭中14番手評価だった馬。事実と逆。事実文の対象は別の馬『アドマイヤテラ』）、
    「グランディア軸ですね、気になってた馬なので」など。事実文と同じ馬についてでも、当時×評価の
    『イガッチ』に「こちらも気になってました」と書いた例があった。原因は2つ: Claudeには予測
    データも事実文の対象馬も渡していないこと、見込み違い系のトーン指示が「高く評価していた
    予測が外れたことを…」で、その言い回し自体を誘発していたこと。
    (b) **相手の自己申告の数字の称賛（狭く8件、的中実績まで広げると計42件）**:
    「状況下でのこの数字は立派だと思います」（単勝回収率の自己申告に対して）、
    「この的中率は見習いたいです」など。検証できない成績を褒めることになり、
    有料予想屋を持ち上げない原則（2節）にも反する。
    → 修正(`prescriber.py`): 13項と同じ、プロンプト＋破棄の二重防御。
    - `SELF_STANCE_CLAIM_RE`: 過去形の「期待/注目/評価して(い)た」「推し/狙っ/買ってた」
      「気になってた」「高く見てた」「本命だった」を、**馬を問わず**案ごと破棄する。
      「別の馬の名前が出たときだけ破棄」する案は、9件中4件しか拾えず（「含め」「この馬」の形を
      逃す）、同じ馬でも事実と食い違う例があるので採らなかった。事実と合う言及（例:
      3番手評価で6着の馬に「評価していた馬が振るわず」）も落ちるが、評価は事実文が正確に
      述べるので反応文で繰り返す必要が無い。「注目してます」「期待してしまいます」のような
      現在形・これからの関心は対象外。
    - `NUMBER_PRAISE_RE`: 「的中率/回収率/勝率/複勝率/収支/数字」と称賛語が近くに並ぶ場合だけ
      破棄する。351件に当てて一致は8件。境界例の「回収率更新しっかり記録されてるの偉い」
      （記録する姿勢への言及）と「単勝配当も含めてしっかり当ててるの凄い」は破棄しない。
      的中実績一般の称賛（「的中実績、素直にすごいです」など34件）は破棄に入れない —
      「小倉記念のワンツー的中お見事です」のような、レース名の分かる個別の的中へのお祝いまで
      落としてしまうため。こちらはプロンプトで「通算・連続・自己申告の成績は称賛も評価も
      しない。称賛してよいのはレース名が分かる個別の的中だけ」と禁じるにとどめる。
    - 見込み違い系のトーン指示を「今回の予測が外れたことだけを認める。どの馬をどう評価して
      いたかは書かない」に変更。`SYSTEM_PROMPT`に【厳守】を2項目追加。
    - 破棄フィルタは反応文だけに掛ける（`_reaction_is_acceptable()`）。事実文は
      「…本命(N頭中1番手評価)としていて」と評価を述べるのが役目なので対象外。
    回帰テスト(`tests/reply_scout/test_prescriber.py`):
    `test_self_stance_claim_re_detects_past_stance`（実例9件すべて）、
    `test_self_stance_claim_re_allows_present_tense_and_plain_reactions`、
    `test_number_praise_re_detects_praise_of_claimed_numbers`（実例8件すべて）、
    `test_number_praise_re_allows_non_praise_and_single_race_congratulations`、
    `test_generate_drafts_discards_self_stance_and_number_praise_and_retries`、
    `test_discard_filters_apply_to_reaction_only_not_fact_sentence`、
    `test_fact_sentence_with_stance_wording_is_kept_verbatim`、
    `test_system_prompt_forbids_self_stance_and_number_praise`、
    `test_miss_high_tone_does_not_invite_stating_past_evaluation`。
    実装時点で未送信の下書きは無かった（TOP10は送信済み1・見送り9に処理済み）ため、
    保存を伴う再生成は行っていない。新プロンプトは4候補の試し生成（保存なし）で確認し、
    出力された反応文はすべてフィルタを通過した。
    試し生成の途中で、Claudeの応答が空で返る別件が見つかった（18項）。

18. **Claudeの応答が空になり、2案ともプレースホルダになる（2026-10-13発覚・同日修正）**:
    `_call_claude()`は`max_tokens=300`で呼んでいた。Claude Sonnet 5は`thinking`を指定しないと
    adaptive thinkingで動き、**思考トークンも`max_tokens`に数えられる**ため、思考だけで上限を
    使い切ると`stop_reason=max_tokens`・本文なしで返る。再試行も空だと2案ともプレースホルダに
    なり、しかも文言が「下書き生成はAPIキー設定後に有効化」だったので、キーがあるのに落ちた
    ことが見分けられなかった。上限16000で12回実測した出力トークンは71〜2049（うち2回が300超）。
    投稿に数字が多い候補（回収率の自己申告など）で思考が長くなりやすい。
    → 修正(`prescriber.py`):
    - `MAX_OUTPUT_TOKENS = 16000`。実測の最大2049に対して十分な余裕を取った。`max_tokens`は
      課金額ではなく天井で、実際の課金は使った分だけ（最大だった2049トークンで約$0.02）。
      16000は非ストリーミングでタイムアウトしない範囲の標準的な値。thinkingを無効にする案
      （Sonnet 5では指定可能）は、反応文の質が変わりうるので採らなかった。
    - プレースホルダを原因別に3種類にした: `PLACEHOLDER`（APIキー未設定、従来の文言のまま）、
      `PLACEHOLDER_EMPTY`「空応答のため生成できませんでした」（本文なし・解析不能・案の不足）、
      `PLACEHOLDER_FILTERED`「フィルタで破棄されたため生成できませんでした」（1案でも破棄フィルタに
      該当して足りなくなった場合）。ダッシュボードは下書き本文をそのまま表示するだけで、
      文言による分岐は無い（`dashboard/generator.py`）ので、表示側の変更は不要。
    - 空応答・解析不能・フィルタ破棄があった試行は「空応答: @user 1回目 stop_reason=max_tokens」
      の形でログに出す。serveはコンソールの無いpythonwで動くため、`logging`だけでなく
      `run_draft()`のログ行（CLI出力とダッシュボードの更新結果）にも積む。
    修正後の試し生成（保存なし、4候補×2回＝16案）ではプレースホルダ0・空応答0。一方で
    フィルタ破棄は11回の呼び出しで7案あり、再試行で補えたが頻度は高い（どのフィルタかは
    ログに出していない。10節）。
    回帰テスト(`tests/reply_scout/test_prescriber.py`):
    `test_call_claude_requests_enough_output_tokens_for_thinking`、
    `test_generate_drafts_empty_responses_use_empty_placeholder_and_log_attempts`、
    `test_generate_drafts_recovers_when_only_first_attempt_is_empty`、
    `test_generate_drafts_filtered_reactions_use_filtered_placeholder`、
    `test_generate_drafts_keeps_one_good_reaction_and_fills_the_other`、
    `test_generate_drafts_unparsable_response_is_logged_separately`、
    `test_generate_drafts_without_api_key_keeps_original_placeholder`、
    `test_placeholders_are_distinct_per_cause`、`test_draft_top_candidates_passes_log_through`、
    `tests/reply_scout/test_orchestrator.py`の
    `test_run_draft_appends_generation_problems_to_log`。

19. **古い候補がTOP10に居座り、レース前の投稿に確定結果を返していた（2026-10-16発覚）**:
    `db.load_scoring_candidates()`は`status='未送信'`だけで候補を集め、投稿日時で絞って
    いなかった（docstringの「rankがNULL」も実装と食い違っていた）。速度は取り込み時点の
    いいね数÷経過時間なので古い投稿ほど0に近づくが、具体性(0.25)などは時間に関係なく
    加点されるため、7/15以降のレース名入りの投稿がTOP10の8枠を占めていた。うち8/10投稿の
    札幌記念の候補（レース前の投稿だが「予想」等の語が無く時制防御を素通り）には、
    「結果が楽しみです」＋「結果も4着と評価通りでした」という時制の食い違った下書きが付いていた。
    → 修正: ランキング対象を投稿から`candidate_max_age_days`日以内（既定7日、X recent searchの
    範囲。`fetcher.DEFAULT_CANDIDATE_MAX_AGE_DAYS`、`config/keywords.json`で上書き可）に限る
    （`db.load_scoring_candidates(conn, posted_since)`、`orchestrator.run_scout()`）。行は消さず、
    次のscoutで`set_ranking()`がrankをNULLにするだけ。投稿日時が空・読めない候補は新しさを
    確かめられないので対象外にする（古い投稿に結果を返す方向の誤りを避ける）。
    同時に見つかった分類のずれ: 低評価の帯は好走(3着以内)かどうかだけで分けており、16頭中
    14番手評価→4着（ローシャムパーク）が「評価通り」になっていた。好走でない結果のうち、
    着順が評価順位より上なら中立系（「結果はN着でした」）、同じか下なら順当系にした
    （`prediction_data._classify()`。高評価・上位評価の帯と好走側は変更なし）。
    回帰テスト: `test_load_scoring_candidates_excludes_posts_older_than_cutoff_without_deleting`、
    `test_load_scoring_candidates_cutoff_excludes_missing_or_unreadable_created_at`、
    `test_run_scout_ranks_only_posts_within_max_age_days`、
    `test_low_eval_not_good_result_but_better_than_pred_rank_is_neutral_rosham_park`、
    `test_classify_low_eval_band_compares_finish_with_pred_rank`ほか。
    残り: 既存の未送信下書きは古いロジックのまま（再生成は未実施）。時制防御がレース前の
    投稿を見逃した件（「予想」等の語が無い投稿）は別課題として未対応。

20. **発走後・結果取り込み前のレースに、レース前の言い方で返していた（2026-10-17発覚）**:
    @horsedatacの投稿（10/17 17:18、富士Sでシュガークンが勝った後の「中央重賞4勝目」
    「単勝4160円」）に、17:48生成の下書きが「14頭中8番手評価としています。」と返していた。
    富士Sの`race_results.csv`の行はchakujun=0のプレースホルダだけで未確定（土日の結果は
    月曜昼の一括配信）。時制の判定は確定済みのレースにしか働かず、発走時刻は読んですらいなかった。
    → 修正: `HorseRow.start_time`（predictions.jsonの`race.start_time`。古いスナップショットに
    無い分は`race_info.csv`で補う。本番では欠けていた324レースすべてを補えた）と
    `prediction_data.has_started()`を追加。`find_match(..., now=)`は、未確定で現在時刻が
    発走時刻を過ぎていれば`(None, REASON_RESULT_PENDING)`を返し、`draft_top_candidates()`が
    既存の「時制スキップ」として記録する（新しいステータス値は作らない。ダッシュボードの説明文に
    「発走済みで結果の取り込み待ち」を追加）。比べるのは投稿日時ではなく現在時刻（レース前の
    投稿でも、発走後に「…評価としています」と返すのは不自然なため）。下書きの行は作らないので、
    結果が入った後の実行で再判定され、結果付きの文面で作られる。発走時刻が分からないレースと、
    `now`を省略した呼び出しは判定しない。既存の下書き（drafts.id 461/462）は自動では直らない。
    回帰テスト: `test_find_match_skips_started_race_whose_result_is_pending_sugar_kun`、
    `test_draft_top_candidates_records_result_pending_as_time_skip_and_retries_later`、
    `test_load_all_predictions_reads_start_time_and_backfills_from_race_info`、
    `test_has_started_compares_jst_start_with_now`ほか。
21. **語の判定を通り抜けたレース前の投稿に、確定結果を返していた（2026-10-19発覚）**:
    @karennn_keiba（10/18 00:04「秋華賞はガーネットフレア…明日勝つためのおまじない♪」）と
    @gambler87240107（10/13「秋華賞はモンローウォークは本命」）の下書きが、秋華賞（10/18 15:40発走）の
    結果付きで作られていた。どちらも`PRE_RACE_INDICATORS`の語を含まない（「本命」「明日」は対象外）。
    → 修正: `prediction_data.posted_before_start()`を追加。`find_match()`は、投稿日時(created_at)が
    発走時刻より前で、そのレースが発走済み（確定済み、または`now`時点で発走時刻を過ぎている）なら、
    結果の有無を問わず`(None, REASON_TIME_MISMATCH)`を返す。語による判定はそのまま残し、新規則は
    追加で効かせる。発走時刻・投稿日時が分からなければ適用しない。発走時刻以降の投稿は対象外。
    既存の下書き（上の2候補の各2案）は自動では消えない（`draft_top_candidates()`は下書きのある
    候補を飛ばすため）。回帰テスト:
    `test_find_match_skips_post_written_before_start_of_started_race`、
    `test_find_match_post_written_after_start_still_cites_result`、`test_posted_before_start`ほか。
22. **反応文に置換文字(U+FFFD)が入り、「秋華賞」が「秋華�械」と化けた（2026-10-19発覚）**:
    @SuMat0ra7 の案1。DBの`drafts.draft_text`にU+FFFD（UTF-8でEF BF BD）のまま保存されていた。
    PostDoctor側にはバイト列を復号する箇所が無く（SDKの非ストリーミング応答の`text`を
    そのまま連結・保存・`html.escape`して表示）、Claudeの応答の時点で入っていたと判断した
    （生の応答は保存していないため直接の確認はできない）。
    → 修正: 破棄フィルタ「文字化け」(`prescriber.MOJIBAKE_RE`: U+FFFDと、改行・タブ以外の
    C0/C1制御文字)を`_DISCARD_FILTERS`に追加。該当した案は他のフィルタと同じく案ごと破棄し、
    `discarded_reactions`に記録する。回帰テスト:
    `test_generate_drafts_discards_mojibake_variant_and_records_it`、
    `test_mojibake_re_catches_replacement_and_control_characters`。

23. **過去の実行のTOP10とその照合結果を後から再構成できなかった（2026-10-24発覚）**:
    「直近3回のTOP10のうち、Rakuba予測と照合できた割合」を出そうとしたところ、`candidates.rank`は
    scoutのたびに全件クリアして上書き、`score_factors`も候補ごとに最新の値しか残らないため、
    過去の回のTOPはスコアの計算時刻からの推定でしか出せなかった（10/17の回は3件しか特定できず）。
    照合結果も、下書き生成時に`candidates.prediction_status`へ書くだけで（照合できた候補はNULL）、
    順位づけの時点での判定は残っていなかった。
    → 修正: `ranking_log`テーブルを追加し、`run_scout()`が1回の実行につき順位づけした全件
    （上位10件）を追記する。列は`run_at`（`scout_searches.run_at`と同じ値。検索が無かった回は
    記録時刻）、`rank`、`candidate_id`、`score`、`match_status`（照合できた/時制スキップ/
    予測データ未投入。下書き生成と同じ`find_match()`の判定を`prescriber.prediction_status_for()`で
    文言にする）、`sort_order`（その候補を拾った検索の並び順。`db.candidate_sort_orders()`が
    同じ検索語で保存直後60秒以内の`scout_searches`の行から引く。記録開始前に拾った候補はNULL）。
    追記のみで、既存の行・テーブル・順位・候補の行は変えない。集計例:
    `SELECT run_at, match_status, COUNT(*) FROM ranking_log GROUP BY 1, 2`。
    テスト: `test_run_scout_appends_ranking_log_with_match_status_and_sort_order`、
    `test_save_ranking_log_appends_rows`、`test_candidate_sort_orders_matches_search_run_right_after_fetch`。

## 5. スコアリング・候補選定の判断

- NGワードは2リスト: 煽り系（スコア半減）と勧誘・宣伝系「限定/有料/教える等」（ハード除外）。
  有料予想アカウントには原則返信しない。除外理由はscore_factorsに記録（除外しすぎの検証用）。
- 引用RTで引用主自身の実質コメントが無い「無言乗っかりRT」（`analyzer._is_low_content_quote()`）
  はハード除外。X API v2の引用RTのtextは引用主自身が書いた分のみ（元ツイート本文は含まない）
  ので、そこが実質空＝バズった元ネタに乗っかっているだけでリプライの起点になる発言が無い、
  という判断。config側の語彙リストではなくコード側の構造的な閾値判定とした
  （ng_words/solicitation_wordsのような調整可能な語彙とは性質が違うため）。
- 同一投稿者は最高スコア1件に集約（重複リプはスパム認定リスク）。
- 2026-08-03、著者は別人でも同じ馬について書かれた投稿が複数TOP10を占める新種の重複が
  判明（6件が同一馬・ほぼ同一fact_sentence）。`analyzer._topic_key()`で馬名単位の
  重複排除も追加（レース名ではなく馬名を軸にするのは、find_match()自体が馬名一致のみを
  採用する既存方針と揃えるため）。
- 加点: 馬名×分析語（斤量/適性/ラップ/血統等）の共起 = 考察系投稿の優遇。
  実運用の反応が良いのは「考察系の個人アカウント」。
- 生スコア(-0.14等)は外部に伝わらない → 事実文は「マーク＋順位(18頭中3番手等)」中心へ
  （改善方針。内部値をそのまま外に出さず受け手の言語に翻訳する）。
  2026-09-25、「マーク」と「順位」は別概念と判明し、マーク部分を順位表記に置き換えた
  （4節10項）。ただし生スコアは残ったままで、この時点では未完了だった。
  2026-09-28、生スコアを削除し頭数付き順位表記(「18頭中3番手評価」)に揃えて今回実現
  （4節11項）。
- 攻撃的・他者を見下す投稿者には、下書きが生成されても関わらない（人間の最終判断）。
- 2026-10-05、scoutの検索キーワードを固定リストから「直近の重賞名＋固定語」に変更
  （`prediction_data.graded_race_keywords()`、`orchestrator.scout_keyword_groups()`）。
  - 重賞名の出どころはRakubaの予測ファイル（`grade_code` A/B/C = G1/G2/G3）だけ。JRA等の
    外部日程は参照しない。返信は馬名がRakubaのデータにあることが前提なので、Rakubaが
    予測していないレースの投稿を拾っても下書きにならないため。
  - 対象期間は「今日より前の直近の土曜〜今日以降の最初の火曜」。月〜木は前週末
    （月曜14時以降は結果も確定）、金〜土は前週末＋当週末、日曜は当週末のみ。ただし
    2026-10-24以降は発走済みのレースを除く（下記）ので、実際に残るのは未発走のレースだけ。火曜を
    終端にするのは月曜祝日開催の中止が火曜に代替開催されうるため。
  - 優先順は当週末→前週末、その中でG1→G2→G3、同グレード内は開催日が今日に近い順。
    「当週末」は土〜火ならその週末、水〜金なら次の週末（`current_weekend_start()`）。
    金〜日は、前週末のG1（結果が出て話題が落ち着いた後）より当週末のG2/G3を先にする
    （2026-10-05変更。当初はグレード優先で、土曜に前週末のG1が当週末の重賞より前に来た）。
    最大4レース（`auto_graded_races.max_races`）。検索回数が足りない週は固定語（「重賞」。
    「競馬予想」は2026-10-24に外した）の方を後ろから切り捨てる。
  - `cancelled_races.json`の中止日・場のレースは除外し、代替開催日の新しいキーだけを残す。
    同じ表記は1回の検索にまとめる。
  - 発走済みのレースは除く（2026-10-24）。scout実行時刻(JST)が`kaisai_date`＋`start_time`を
    過ぎたレースは検索語にしない（`prediction_data.has_started()`。`start_time`が予測ファイルに
    無ければ`race_info.csv`で補い、それでも分からないレースは従来どおり残す）。除いた後に
    最大4レースを選ぶので、空いた枠には次のレースが入る。理由: 終わったレースの検索語は
    保存の大半が時制スキップか照合なしで、読み取り枠の無駄になる（10/19の秋華賞は7件保存で
    照合1件）。結果として月〜木は前週末の重賞が全て外れ、固定語「重賞」だけになる。
    試算（10/24時点のRakubaデータ）: 日曜10/25の9時台は「菊花賞25・重賞25」（除く前は
    「菊花賞16・アルテミスS17・重賞17」）、菊花賞発走(15:40)後〜木曜は「重賞50」。
    テスト: `test_graded_race_keywords_drops_races_that_have_started`、
    `test_scout_keyword_groups_drops_started_graded_races`、
    `test_run_scout_passes_current_time_to_keyword_groups`。
  - 表記: X検索は「〜S」と「〜ステークス」を同一視しない（2026-10-05、件数API
    `get_recent_tweets_count`で確認。「スワンS」1,203件・「スワンステークス」308件・
    OR検索1,487件。スプリンターズも同傾向でS表記が約3倍）。そのため「〜ステークス」は
    両表記をOR検索1回にまとめ、全角英数字は半角にそろえる（「ＣＢＣ賞」→「CBC賞」）。
    スポンサー名付きの正式名称（「産経賞オールカマー」等）は自動で削らず、
    `config/keywords.json`の`race_aliases`に手で登録する（「朝日杯フューチュリティ
    ステークス」のように、冠名に見える部分がレース名そのものである例があるため）。
  - 効果測定の基準値（変更前、2026-09-21〜10-05取得分・全て「函館記念」）: 候補34件、
    平均いいね57.6、馬名一致14件（TOP10入り5件・8頭、うちエコロディノス8件）。一致件数は
    全候補を`find_match()`でオフライン照合し直して数える（API不要）。
    **注意: `candidates.prediction_status`から一致件数を数えてはいけない。** 値が入るのは
    下書き段階で一致しなかった候補（「予測データ未投入」「時制スキップ」）だけで、空(NULL)は
    「未下書き」と「一致あり」の両方を含む。2026-10-05、これを「一致なし」と読み違えて
    基準値を「一致0件」と誤報告し、同日訂正した。

## 6. リプライ営業の運用知見（実測ベース）

- **答え合わせが本体**。単発リプはインプ数十止まり、答え合わせで会話が往復した相手は419。
  外れた時こそ自分から答え合わせに戻る（誠実さが最大の差別化）。
- **勝ちパターン**: 大型考察系アカウント(1万+フォロワー) × 発走前の先出し予測 × 結果が伴う
  → インプ2,449の実績（フォロワー54kのkeiba_hip宛、レース前リプ）。
- 見解が割れる相手とは「答え合わせの約束」で締める。勝っても勝ち誇らず、
  相手の理論の当たっていた部分を具体的に拾って立てる。
- 同じレースについて相手ごとに違うことを言わない（発言の整合性は検証される前提）。
- 相手の誤字（関谷記念等）や古い時制（「明日」等）を下書きが引き継ぐことがある → 送信前に人間が直す。
- 適量は1日1〜2件、答え合わせ含め最大4件程度。連投は営業臭が出る。
- 返信の見逃しは機会損失（10日放置の実例）→ 返信検知機能で構造的に解消。
  自分自身のぶら下げ返信はreply_countから除外して「他者からの反応」を正とする。

## 7. Rakuba側の運用サイクル（週次リズム）

- JV-Linkの確定成績は**月曜14時頃に土日分まとめて配信**（週1バッチ、正常仕様）。
  土日当日のレース結果は月曜まで引用不可。PostDoctor側で「未確定」と出ても誤検知ではない。
- パイプラインは月曜15時のタスクスケジューラで自動実行（手動ボタンも保険で残す）。
- 週次フロー: 金曜夜 予想投稿 → 土日朝 スカウト+営業リプ → 日曜夕 手動答え合わせ
  → 月曜 バッチ→結果発表（実投稿と突合してから）→答え合わせリプ → 平日 改善開発。
- 結果発表は冒頭1〜2行にハイライト（重賞的中等）+ ◎的中率/馬券的中率、投稿後は固定ポスト差し替え。

## 8. 環境・アカウントの分離

- 開発者認証（Claude Code = claude.aiのProアカウント）と、アプリ実行時認証
  （PostDoctor専用GmailのConsoleアカウントのANTHROPIC_API_KEY、XのDeveloperキー）は独立。
- PostDoctorはRakubaの backend/output/ を読み取り専用で直接参照（コピー同期しない）。
  .env・認証情報は共有しない。
- VSCode/Claude Codeの起動フォルダ: 日常は各プロジェクト個別、横断作業のみ親(C:\Projects)。
- 教訓: Stripe系決済フォームはブラウザのオートフィルで入力イベントが発火せず
  ボタンが非活性のままになることがある → 全欄手入力で解決（サポート知見として保持）。
- **ダッシュボードのTailscale公開（2026-10-24）**: 日曜朝などPCの前にいない時にスマホから
  操作するため、常駐serveを `--host 0.0.0.0` で起動するよう変更（`serve`の`--host`は既定
  `127.0.0.1`のままで、明示した時だけ外に開く）。Rakubaの7100/3000番と同じ方針で、
  受信ファイアウォール規則 **"PostDoctor: block 8765 except Tailscale"**（TCP 8765、
  Tailscaleの `100.64.0.0/10`・`fd7a:115c:a1e0::/48` 以外を遮断）でLANからの接続を断つ。
  Rakuba側の規則（"Rakuba: block 7100/3000 except Tailscale"）に8765を足さず別規則にしたのは、
  両プロジェクトの疎結合を保ち、片方だけ見直す時に混ざらないようにするため。
  - 規則が必須な理由: 実際に待ち受けるのはvenvが起動する基本Python312の`pythonw.exe`で、
    これにはPrivate/Public全ポート許可の既存規則があり、Wi-FiもPrivate。規則なしで0.0.0.0に
    すると即LANから開ける。Block規則はAllowより優先される。
  - ループバックはWindowsファイアウォールの対象外なので `http://localhost:8765` は従来通り。
  - TailscaleのIPだけで待ち受ける案は不採用（localhostで開けなくなる・ログオン時に
    Tailscale未接続だと起動に失敗する）。
  - 認証は無い。外から押せるのはAPI費用のかかる読み取り（更新/候補収集/反応取得）と
    ローカルDBのステータス変更だけで、Xへの書き込み（投稿・いいね・フォロー）は元々存在しない。

## 9. Rakuba側の固定買い目ルール改定（2026-09-05）

- 発端: 2026-06-28〜8-30の約9週分（bet_pl_fixed_rule.csv/bet_pl_heatmap.csv）を
  全期間通算（週別ROIの単純平均ではなく総購入額/総回収額ベース）で棚卸し。
- **△判定の三連複1点(300円)**: 通算ROI 58.6%、的中率3.7%(n=215, 95%CI 1.9-7.2%)。
  実測hitrateと損益分岐hitrate(6.35%)の二項検定はp=0.124で有意差なし＝統計的には
  「構造的に負のEV」と言い切れる段階ではなかったが、直近3週(8/15-16, 8/22-23,
  8/29-30, n=66)が連続ゼロ的中、前半5週ROI(78.6%)→後半5週ROI(37.1%)という
  悪化傾向も観測（Fisher's exact p=0.723で有意差はまだ無い＝偶然の範囲を否定できず）。
  三連複は「◎〇▲の3頭が的中馬全ての枠を占める」という組合せ論的に難易度の高い
  賭け方であり、自信度の低い△帯にはそもそも不向きという理屈は一貫していた。
- 代替券種の試算（同一レース・同一300円/レース予算での置き換え）: 複勝3頭box
  (100円×3)がROI 77.2%・的中率84.7%と、三連複より的中率・ROIともに明確に改善
  （ワイド69.3%、馬連71.9%、単勝64.1%は複勝ほどではない）。
- 副産物として「◎◎判定×三連複box(◎〇▲)」も同時に集計: ROI 110.2%・的中率11.5%
  (n=61)。損益分岐率10.42%に対し二項検定p=0.679で有意差はまだ無いが、△の三連複
  とは対照的にプラス圏で、直近2週(8/22-23, 8/29-30)がROI257%・318%と好調だった。
- **決定（実装まで完了、2026-09-05）**: ①△判定を「三連複1点300円」→「複勝3頭box
  (◎〇▲各100円、計300円)」に変更。②◎◎判定に「複勝1点500円(既存)」＋「三連複box
  (◎〇▲)300円(新規)」の二段構えを追加(投資額は増える方向)。
  実装は `Rakuba/backend/generate_purchase_list.py`（週次購入チェックリスト生成、
  週間合計投資額の内訳: ◎◎複勝分/◎◎三連複box分/△複勝box分を表示）。
  2026-09-06(日)分の購入リストから新ルールを適用。
- **追記(2026-09-05, 同日中)**: 週次収支トラッキング`Rakuba/backend/calc_fixed_rule_pl.py`
  も上記ルールに合わせて更新。合わせて×tierの扱いを確認: `generate_purchase_list.py`
  （実購入チェックリスト）は当初から×を一度も対象にしておらず、×→複勝500円という
  ルールは`calc_fixed_rule_pl.py`内の**参考シミュレーションのみ**（正式採用された
  実購入ルールではない）だったと確認。△・◎◎の改定に伴っても×の扱いは変更せず、
  従来通り複勝500円の参考シミュレーションとして維持（非公式である旨をスクリプトの
  コメントに明記）。
  - 過去データへの遡及適用は行わない方針とし、`RULE_CUTOVER_DATE = "20260906"`を
    導入。2026-09-06以降のレースのみ新ルールで`bet_pl_fixed_rule.csv`/`.html`に
    集計し、それ以前（〜2026-08-30、旧ルール: ◎◎複勝500円のみ・△三連複1点300円）
    のシミュレーション結果は`reports/bet_pl_fixed_rule_legacy.csv`/`.html`として
    凍結保存（以後このlegacyファイルは再生成しない）。両者を安易に合算しないよう、
    メインレポート側にlegacyへのリンクと適用開始日を明記。
  - `compute_bet()`(1レース1ベット)は`compute_bets()`(1レース複数ベット可、
    ◎◎は複勝＋三連複boxの2本を返す)に変更。回帰テスト`test_calc_fixed_rule_pl.py`
    も新シグネチャに合わせて全面書き換え、7ケースで新ルールの3ティア(◎◎/△/×)を
    それぞれ検証。

## 10. 今後の改善バックログ（会話中に挙がった未実装・検討事項）

- published版スナップショット凍結（4-5節）— 最優先
- 反応文が「自分の見立ても甘かった」「見立てが外れました」のように、Rakuba自身の予想の
  当否を語る表現（4節13項の対象外）。トーン指示でカテゴリ(外れ等)は伝えているので
  方向は概ね合うが、Claudeは実際の評価を知らないため、fact_sentenceと食い違う余地は残る。
  実例が出たら同様の禁止対象に加えるか検討。
- 一般名詞と一致する短い馬名のリスト（「サイン」等をconfigに列挙し、さらに厳しい条件を
  課す案）— 2026-09-28見送り（4節12項）。A+B適用後に残る誤マッチは「テスト北斗特別の
  サイン馬券」のように、そのレース名/レース番号と一般名詞が同じ投稿に並ぶ場合のみで、
  実例は未確認。`trusted_authors`と同じく実例が出てから追加する。
- analyzer側の馬名照合（`_specificity()`/`_topic_key()`/`_has_analytical_signal()`）も
  単純な部分文字列一致のまま（「ジオ」⊂「ラジオ」等で加点・重複排除が誤作動し得る）。
  事実文には影響しないが、`_name_at_word_boundary()`の流用を検討。
- Rakuba側データで重賞のrace_nameが空欄のレースがある（例: 2026-06-28函館11R 函館記念）。
  4節12項の方針で、ここに出た短い馬名は一致しない。Rakuba側でrace_nameが埋まれば解消。
- 事実文の「マーク+順位」化（5節）の残課題（本体は2026-09-28に実現、4節11項）:
  - 高評価の閾値`HIGH_EVAL_MAX_PRED_RANK=3`は頭数によらず固定 — 2026-10-13に、4番手〜
    上位3分の1の帯だけ頭数比例にした（4節15項）。残り: 低評価の馬が4着など惜しい着順でも
    「評価通り」になる（例: 16頭中14番手→4着）点と、6頭立ての3番手を高評価とする点は未対応。
  - 出走取消・競走除外の馬がpredictions側に残っている場合、頭数に含まれてしまう
    可能性がある（未検証）。race_results.csvの確定頭数と突き合わせるかは要検討。
  - 確定/未確定の混在レースは頭数を出さない仕様。Rakuba側が確定頭数を持つように
    なればそれを使う。
- スポンサー名付きの重賞名で、短い馬名の誤一致防止が働かない — 2026-10-05記録、未修正。
  `_core_race_name()`は全角スペースで区切るだけなので、「産経賞オールカマー」
  「関西テレビ放送賞ローズステークス」のように区切りの無い正式名称では通称（オールカマー・
  ローズS）が得られない。投稿で通称だけが書かれていると、`_race_specific_context()`の
  レース名共起が成立せず、3文字以下の馬名は（本物の言及でも）一致しない（「中山11R」の
  ような開催場+レース番号が書かれていれば一致する）。「〜ステークス」を「〜S」と書いた
  投稿も同様。誤った引用を
  生む方向ではなく取りこぼす方向の不具合。scout検索用の`race_aliases`（5節）を共起判定にも
  流用するのが候補だが、`find_match()`の挙動が変わるので下書き再生成が必要になる。
- 反応文のフィルタ破棄の頻度が高い — 2026-10-13記録。4節17項のフィルタ追加後、試し生成
  11回の呼び出しで7案が破棄された（再試行で補えたのでプレースホルダは0）。同日、破棄された
  案ごとに該当フィルタ名（数字・印／一致の主張／自分の評価／数字の称賛、複数該当は全部）と
  反応文の先頭20字をログに出すようにした（`prescriber._DISCARD_FILTERS`/`_describe_discard()`。
  例: 「フィルタで破棄: @user 1回目 自分の評価「Q&A形式で読みやすいです。ローシャムパ…」」。
  破棄の判定と再試行回数は変えていない。回帰テスト:
  `test_discard_reasons_names_every_matching_filter`、
  `test_discard_log_shows_filter_names_and_truncated_head_not_full_text`ほか）。
  2026-10-16: このログ行は常駐serve(pythonw)では出力先が無く捨てられていたため、破棄1案に
  つき1行を`reply_scout.db`の`discarded_reactions`テーブルにも記録するようにした
  （`db.save_discarded_reaction()`、`prescriber._record_discard()`。候補ID・試行回数・応答の
  何案目か・該当フィルタ名のJSON配列・先頭20字・記録日時）。draftsへの列追加にしなかったのは、
  破棄された案はdraftsの行にならず（1候補で複数回・複数案ありうる）、下書き再生成で
  draftsの行は消える運用のため。記録の失敗は生成を止めずlogger.warningのみ。ログ行は従来どおり。
  回帰テスト: `test_discarded_reaction_is_saved_as_one_row_with_head_only`、
  `test_discarded_reaction_with_multiple_filters_saves_every_filter_name`、
  `test_discard_record_failure_does_not_stop_generation`ほか。
  残り: 実測をためてから、プロンプトの調整か再試行回数（現在1回）の見直しを判断する。
  フィルタ名ごとの件数は`SELECT f.value, COUNT(*) FROM discarded_reactions,
  json_each(filters) AS f GROUP BY f.value`で数える。
- run_track / 返信取得への読み取り上限（track_read_limit）
- 時制ミスマッチの近傍判定(b)（実例が貯まってから）
- 1投稿に複数の該当馬(◎〇▲等)が言及されている場合の優先cite選択（4-9節の修正後も残る別課題。
  現状はレース自体は正しく絞り込めても、同一レース内で複数馬が該当するとリスト順による
  決定論的だが本文の文脈(相手の本命かどうか)を考慮しない選択になる。将来的には印
  (◎/〇/▲等)と馬名の本文中での近接性から「相手が本命視している馬」を優先citeする
  改善余地がある）
- 馬名誤字への耐性（「ジョパンニ」問題）/ 挨拶語の時間帯不一致
- 生成時刻と送信時刻のズレ対応（「おはようございます」問題）
- スコアカード(score_review.csv)による配点の実測見直し — 2026-08-03、送信済み・追跡済み
  n=10で一度検証。フォロワー項(follower_norm)とインプレッションのPearson相関は0.555だが、
  これはフォロワー54k・インプ2,463の1件（keiba_hip、6節の「勝ちパターン」実例）に牽引された
  見かけの値で、外れ値に頑健なSpearman順位相関では0.024（ほぼ無相関）。同じ手法で
  velocity_norm（元投稿のいいね増加速度）を見るとSpearman0.612と大幅に頑健。
  → 現時点ではフォロワー項の重み変更は見送り、`reply-scout scorecard`の相関出力
  （`orchestrator.summarize_score_correlations()`）でn>30まで様子見と判断。
  なお同じ検証でspecificity/trustedは分散ゼロ（`config/keywords.json`の
  `specific_terms`/`trusted_authors`が空だったため）と判明し、まずこの2項目を
  実データで埋める対応を優先した（本節時点で対応済み）。
- 「再訪予定」リマインダー（答え合わせの約束管理）
- モジュールB(トレンド監視)・C(競合分析)・SaaS化(BYOK方式から)は未着手
- Rakuba側 `race_results.csv` の `time_sec` 変換誤りの修正 — 2026-09-29見送り。
  `JraVanConsole/Records/SeRecord.cs:39` の `TimeSec => TimeRaw / 10.0` が、JV-Dataの
  走破タイム(「分・秒・1/10秒」の4桁、`1332` = 1分33秒2)を秒に変換せず10で割っているだけで、
  93.2秒が133.2秒として入っている(1分ごとに+40秒ずれる)。`agari_sec`(60秒未満)は正しい。
  修正するとCSVの再出力・再学習が必要で、`race_results.csv` を読むPostDoctor側にも
  影響が及ぶ(データ契約の変更)ため、単独の作業として切り出す。それまでの間、Rakubaの
  `x_post.py` は、この値から計算する `horse_avg_speed_3`(平均走破速度)をコメント候補から
  外している(`_HORSE_ITEMS` のコメント参照)。修正したら重要度順で候補に戻す。
  経緯は `Rakuba/docs/design_decisions.md`「X投稿コメント: レース内の相対比較で根拠を選ぶ」。
