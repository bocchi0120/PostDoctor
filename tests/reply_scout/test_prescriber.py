from __future__ import annotations

import json
from pathlib import Path

import pytest

from postdoctor.reply_scout import db, prediction_data, prescriber
from postdoctor.reply_scout.db import Candidate
from postdoctor.reply_scout.fetcher import ScoutConfig

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "rakuba_output"


class _FakeBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str):
        self.content = [_FakeBlock(text)]


def _candidate(cid: str, text: str) -> Candidate:
    return Candidate(
        id=cid, text=text, author_id=f"a-{cid}", author_screen_name=f"user{cid}",
        created_at="2026-07-21T00:00:00+09:00", likes=1, retweets=0, replies=0, quotes=0,
        keyword="競馬",
    )


def _cfg() -> ScoutConfig:
    return ScoutConfig(
        keywords=["競馬"], ng_words=[], solicitation_words=[], analysis_terms=[],
        trusted_authors=[], daily_read_limit=50, min_likes=5, min_replies=0,
        top_user_lookup_limit=15, claude_model="claude-sonnet-5",
        draft_top_n=5, rakuba_output_dir=str(FIXTURES_DIR),
    )


def _find_match(text: str):
    pcfg = prediction_data.load_prediction_config(str(FIXTURES_DIR))
    rows = prediction_data.load_all_predictions(pcfg)
    results = prediction_data.load_race_results(pcfg)
    confirmed = prediction_data.confirmed_race_keys(results)
    match, _reason = prediction_data.find_match(text, rows, results, confirmed)
    return match


def test_system_prompt_warns_against_misreading_addressee():
    """rank#4の実例（候補投稿の応援コメントを自分(Rakuba)宛と誤読して
    「応援ありがとうございます」と返した）の回帰防止。候補は第三者の独立投稿であり
    Rakuba宛の言及ではあり得ない、という前提がシステムプロンプトに明記されていること。"""
    assert "第三者の独立した投稿" in prescriber.SYSTEM_PROMPT
    assert "Rakuba自身に" in prescriber.SYSTEM_PROMPT


def test_generate_drafts_sends_system_prompt_to_claude(monkeypatch):
    """SYSTEM_PROMPTの内容が実際にAPI呼び出しのsystemパラメータとして渡ることを確認する
    （定数の中身だけでなく、_call_claude()が実際にそれを使っていることの回帰テスト）。"""
    match = _find_match("テストホースイチが強かった")
    assert match is not None
    captured = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeResponse('{"reactions": ["いい馬ですよね", "気になる存在です"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    candidate = _candidate("c1", "テストホースイチが強かった")
    prescriber.generate_drafts(candidate, match, model="claude-sonnet-5")

    assert captured["system"] == prescriber.SYSTEM_PROMPT
    assert "第三者の独立した投稿" in captured["system"]


def test_generate_drafts_combines_fact_sentence_verbatim(monkeypatch):
    match = _find_match("テストホースイチが強かった")
    assert match is not None

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResponse('{"reactions": ["いい馬ですよね", "気になる存在です"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    candidate = _candidate("c1", "テストホースイチが強かった")
    drafts = prescriber.generate_drafts(candidate, match, model="claude-sonnet-5")

    assert len(drafts) == 2
    for d in drafts:
        assert match.fact_sentence in d
        assert len(d) <= prescriber.MAX_DRAFT_LEN


def test_generate_drafts_discards_variant_with_stray_digit_and_retries(monkeypatch):
    match = _find_match("テストホースイチが強かった")
    assert match is not None
    calls = {"n": 0}

    class _FakeMessages:
        def create(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResponse('{"reactions": ["2着だったんですね", "いい馬です"]}')
            return _FakeResponse('{"reactions": ["応援しています"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    candidate = _candidate("c1", "テストホースイチが強かった")
    drafts = prescriber.generate_drafts(candidate, match, model="claude-sonnet-5")

    assert calls["n"] == 2  # 1件しか残らなかったので再試行が1回走る
    assert len(drafts) == 2
    for d in drafts:
        # fact_sentence以外の部分に数字・マーク・漢数字+着が含まれていないことを検証
        reaction_part = d.replace(match.fact_sentence, "")
        assert not prescriber.NUMERIC_MARK_RE.search(reaction_part)


def test_generate_drafts_kanji_number_is_also_sanitized(monkeypatch):
    match = _find_match("テストホースイチが強かった")
    assert match is not None

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResponse('{"reactions": ["十二着とは驚きました", "普通の感想です"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    candidate = _candidate("c1", "テストホースイチが強かった")
    drafts = prescriber.generate_drafts(candidate, match, model="claude-sonnet-5")
    combined = " ".join(drafts)
    assert "十二着とは驚きました" not in combined


def test_draft_top_candidates_hard_skips_no_match(monkeypatch, conn):
    cfg = _cfg()
    candidates = [
        _candidate("match1", "テストホースイチが強かった"),
        _candidate("nomatch1", "競馬とは関係のない投稿です"),
    ]
    db.insert_candidates(conn, candidates)
    db.set_ranking(conn, [("match1", 0.9, 1), ("nomatch1", 0.8, 2)])

    calls = {"n": 0}

    class _FakeMessages:
        def create(self, **kwargs):
            calls["n"] += 1
            return _FakeResponse('{"reactions": ["いい馬ですね", "注目しています"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())

    count = prescriber.draft_top_candidates(account=None, conn=conn, cfg=cfg)
    assert count == 1
    assert calls["n"] == 1  # nomatch1ではClaudeを一切呼ばない

    ranked_by_id = {r.candidate.id: r for r in db.get_top_candidates(conn)}
    assert ranked_by_id["match1"].drafts
    assert ranked_by_id["nomatch1"].drafts == []
    assert ranked_by_id["nomatch1"].prediction_status == "予測データ未投入"


def test_draft_top_candidates_distinguishes_time_mismatch_status(monkeypatch, conn):
    cfg = _cfg()
    candidates = [
        _candidate("nomatch1", "競馬とは関係のない投稿です"),
        _candidate("timeskip1", "テストホースイチの前哨戦分析、参戦が楽しみです"),
    ]
    db.insert_candidates(conn, candidates)
    db.set_ranking(conn, [("nomatch1", 0.9, 1), ("timeskip1", 0.8, 2)])

    monkeypatch.setattr(prescriber, "_client", lambda: None)  # モックモード、呼び出し不要

    count = prescriber.draft_top_candidates(account=None, conn=conn, cfg=cfg)
    assert count == 0
    ranked_by_id = {r.candidate.id: r for r in db.get_top_candidates(conn)}
    assert ranked_by_id["nomatch1"].prediction_status == db.PREDICTION_STATUS_NO_MATCH
    assert ranked_by_id["timeskip1"].prediction_status == db.PREDICTION_STATUS_TIME_MISMATCH


def test_draft_top_candidates_saves_current_predictions_hash(monkeypatch, conn):
    cfg = _cfg()
    db.insert_candidates(conn, [_candidate("match1", "テストホースイチが強かった")])
    db.set_ranking(conn, [("match1", 0.9, 1)])

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResponse('{"reactions": ["いい馬ですね", "注目しています"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    prescriber.draft_top_candidates(account=None, conn=conn, cfg=cfg)

    expected_hash = prediction_data.compute_predictions_signature(
        prediction_data.load_prediction_config(str(FIXTURES_DIR))
    )
    ranked = db.get_top_candidates(conn)[0]
    assert ranked.predictions_hash == expected_hash
    assert ranked.predictions_hash is not None


def test_draft_top_candidates_excludes_skipped_status(monkeypatch, conn):
    cfg = _cfg()
    candidates = [_candidate("skipped1", "テストホースイチが強かった")]
    db.insert_candidates(conn, candidates)
    db.set_ranking(conn, [("skipped1", 0.9, 1)])
    db.update_status(conn, "skipped1", "見送り")

    calls = {"n": 0}

    class _FakeMessages:
        def create(self, **kwargs):
            calls["n"] += 1
            return _FakeResponse('{"reactions": ["a", "b"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())

    count = prescriber.draft_top_candidates(account=None, conn=conn, cfg=cfg)
    assert count == 0
    assert calls["n"] == 0


# --- 相手の予想との一致・不一致の主張（2026-09-29、候補#7 @naanaashii_1 の実例） ---

CANDIDATE7_REACTION = "札幌記念、グランディア軸ですね。自分も近い見立てでした"


def test_system_prompt_forbids_agreement_claims():
    """反応文を書くClaudeは相手の印とRakubaの印を比較していないので、一致・不一致を
    主張する表現を禁じる制約がシステムプロンプトに明記されていること。"""
    assert "一致・不一致を主張する表現は一切書かない" in prescriber.SYSTEM_PROMPT
    assert "自分も近い見立てでした" in prescriber.SYSTEM_PROMPT


@pytest.mark.parametrize(
    "reaction",
    [
        CANDIDATE7_REACTION,  # 候補#7の案1そのもの
        "グランディア本命、こちらと同じです",
        "うちも似た評価でした",
        "自分も高く見てたので今回は完全に見立て違いでした",  # 過去の下書きの実例
        "こちらも本命にしていました",
        "近い見立てで嬉しいです",
        "同じ評価の方がいて心強いです",
        "見立てが一致しましたね",
        "本命が被りました",
        "こちらとは見立てが違いますが参考になります",
        "こちらの見立てとは違う結果で勉強になります",  # 過去の下書きの実例
        "大外回しが響いたという見立て納得です",  # 過去の下書きの実例
        "納得の本命ですね",
        "同感です",
    ],
)
def test_agreement_claim_re_detects_claims(reaction):
    assert prescriber.AGREEMENT_CLAIM_RE.search(reaction), reaction


@pytest.mark.parametrize(
    "reaction",
    [
        # 過去の下書きに実際にあった、比較を主張していない反応文（誤検知しないこと）
        "先行してからの持続力が持ち味というのは納得感あります。毎日王冠との相性の良さも興味深いです",
        "イガッチの適性の広さ、こちらも気になってました",
        "マイネルメモリーの訃報、自分も驚きました",
        "アイビスまで並べての総括、お疲れさまです。夏場は荒れやすくて自分も苦戦しました",
        "函館と小倉でコース質が違うというのは見落としがちな視点、参考になります",
        "洋芝適性の見立て、参考になります",
        "騎手それぞれの思い入れの馬が違うの、ドラマがあって好きです",
    ],
)
def test_agreement_claim_re_allows_plain_reactions(reaction):
    assert not prescriber.AGREEMENT_CLAIM_RE.search(reaction), reaction


def test_generate_drafts_discards_candidate7_agreement_claim_and_retries(monkeypatch):
    """候補#7の案1が返ってきても下書きに残らず、再試行で補充されること。"""
    match = _find_match("テストホースイチが強かった")
    assert match is not None
    calls = {"n": 0}

    class _FakeMessages:
        def create(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResponse(
                    '{"reactions": ["' + CANDIDATE7_REACTION + '", "いい馬ですよね"]}'
                )
            return _FakeResponse('{"reactions": ["気になる存在です"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    candidate = _candidate("c7", "札幌記念はグランディア軸で勝負")
    drafts = prescriber.generate_drafts(candidate, match, model="claude-sonnet-5")

    assert calls["n"] == 2
    assert len(drafts) == 2
    for d in drafts:
        assert "近い見立て" not in d
        assert not prescriber.AGREEMENT_CLAIM_RE.search(d.replace(match.fact_sentence, ""))


def test_system_prompt_forbids_self_stance_and_number_praise():
    """自分の過去の評価・期待の主張と、相手の自己申告の成績の称賛を禁じる制約が
    システムプロンプトに明記されていること（破棄フィルタとの二重防御）。"""
    assert "自分(Rakuba)がどの馬をどう見ていたかは一切書かない" in prescriber.SYSTEM_PROMPT
    assert "期待してた" in prescriber.SYSTEM_PROMPT
    assert "称賛も評価もしない" in prescriber.SYSTEM_PROMPT
    assert "回収率・的中率・的中続き" in prescriber.SYSTEM_PROMPT
    assert "レース名が分かる個別の的中だけ" in prescriber.SYSTEM_PROMPT


def test_miss_high_tone_does_not_invite_stating_past_evaluation():
    """見込み違い系のトーン指示が「高く評価していた予測が…」だったことが、反応文に
    「高く評価していた」を書かせる原因になっていた。指示文自体が破棄対象の言い回しを
    含まず、どの馬をどう評価していたかは書かないよう明記していること。"""
    tone = prescriber._CATEGORY_TONE["見込み違い系"]
    assert "どの馬をどう評価していたかは書かない" in tone
    assert "今回の予測が外れたことだけ" in tone
    assert "高く評価していた" not in tone


# 過去の反応文351件(2026-10-13集計)にあった、自分の過去の評価・期待を述べる9件すべて。
PAST_SELF_STANCE_REACTIONS = [
    "イガッチの適性の広さ、こちらも気になってました",
    "重賞初挑戦の壁、やっぱり侮れないですね。自分も高く見てたので今回は完全に見立て違いでした",
    "グランディア軸ですね、気になってた馬なので展開含めて見届けたいです",
    "グランディア軸のこの並び、個人的にも気になってました",
    "換金前に踏みとどまれてよかったです、ファウストラーゼン応援馬券とみくちゃんのサインいいですね。"
    "こちらは高く見ていた予測が外れてしまい、素直に受け止めます",
    "みくちゃんサインゲットおめでとうございます！自分はこの馬を高く評価していたのですが"
    "結果はついてこず、言い訳せず反省です",
    "Q&A形式で読みやすいです。アドマイヤテラ含め注目してましたが、結果的に予測は外れてしまいました",
    # 事実と逆の実例: ローシャムパークは札幌記念で下位評価だった
    "Q&A形式で読みやすいです。ローシャムパークは期待してただけに、結果は謙虚に受け止めます",
    "枠順確定後の細かい評価、見応えありました。こちらは評価していた馬が振るわず反省です",
]


@pytest.mark.parametrize(
    "reaction",
    PAST_SELF_STANCE_REACTIONS
    + [
        "ずっと推してたので嬉しいです",
        "前から狙ってた一頭でした",
        "この馬は本命でした",
        "高く買っていただけに残念です",
    ],
)
def test_self_stance_claim_re_detects_past_stance(reaction):
    assert prescriber.SELF_STANCE_CLAIM_RE.search(reaction), reaction
    assert not prescriber._reaction_is_acceptable(reaction)


@pytest.mark.parametrize(
    "reaction",
    [
        # 過去の下書きに実際にあった、現在形・これからの関心（破棄しない）
        "直近の重賞、穴狙いがしっかり馬券になってるの凄いですね。関屋記念も注目してます",
        "七夕賞から府中牝馬Sまで穴狙いが続けて的中してますね、関屋記念も期待してしまいます",
        "数値化しての予想、面白い視点ですね。ファーヴェント注目してみます、当日の走り気になります",
        "横山武史騎手の手応え、クイーンSでどう結果に繋がるか注目してます",
        "横山騎手のコメントに気持ちが伝わってきますね。雪辱なるか注目しています",
        "函館の内容からすると期待したくなりますね。札幌記念でどう動くか注目です",
        "名前好きって理由も競馬の楽しみ方として好きです。ランスオブカオス気になります",
        "七夕賞から続けて結果出されてるの見ていました。関屋記念も楽しみにしています",
        # 過去形でも、自分の評価・期待ではないもの
        "陽くんの足上げ、気になる仕草ですね。今回は見立てが及びませんでした、素直に受け止めます。",
        "各路線組の評価が細かくて勉強になります。今回は自分の見立てが外れた形なので、率直に反省しています",
        "マイネルメモリーの訃報、自分も驚きました",
        # 「〜していたい」は願望で、過去の主張ではない
        "これからも注目していたいです",
    ],
)
def test_self_stance_claim_re_allows_present_tense_and_plain_reactions(reaction):
    assert not prescriber.SELF_STANCE_CLAIM_RE.search(reaction), reaction


# 過去の反応文351件のうち NUMBER_PRAISE_RE に一致した8件すべて。
PAST_NUMBER_PRAISE_REACTIONS = [
    "重賞での的中率えげつないですね、特に穴人気を的中させてるのが説得力あります😳",
    "回収率145.6%は安定してますね、東海Sの予想気になります👀",
    "この的中率は見習いたいです。穴党の視点、勉強になります",
    "キングジョージ超えなるか楽しみです。ハンデ重賞の的中率、正直見習いたいです",
    "高人気馬を続けて的中させてる的中率、見習いたいです。こちらも精進します",
    "この的中率は見習いたいです、東海Sと関屋記念も当たりますように",
    "波乱決着での的中率の高さ、見習いたいです。素直に脱帽です",
    "僅差での涙、積み重なると大きいですよね。状況下でのこの数字は立派だと思います",
]


@pytest.mark.parametrize(
    "reaction", PAST_NUMBER_PRAISE_REACTIONS + ["すごい回収率ですね", "立派な数字だと思います"]
)
def test_number_praise_re_detects_praise_of_claimed_numbers(reaction):
    assert prescriber.NUMBER_PRAISE_RE.search(reaction), reaction


@pytest.mark.parametrize(
    "reaction",
    [
        # 数字の語はあるが、称賛していない（過去の下書きの実例）
        "朝日杯世代の層の厚さが数字に表れてますね、当時の掲示板組が揃って重賞馬になるのは珍しい記録だと思います",
        "追い切りの数字もしっかり出てますね。夏場の巻き返しに期待したいところです",
        "僅差での結果が積み重なると数字の見え方も変わりますよね。状況を冷静に受け止める姿勢、勉強になります",
        "ラップとPCIの数値、見ごたえありました。次走チェック馬の走り楽しみです",
        "回収率まで含めて振り返るの参考になります。当たり外れ含めて見せる姿勢いいですね",
        "回収率の数字が並んでるとつい見入っちゃいます、CBC賞の結果も楽しみにしてます",
        # 境界例: 記録する姿勢・個別の配当への言及は破棄しない（プロンプト側で抑える）
        "回収率更新しっかり記録されてるの偉い、フェアリーSも楽しみですね",
        "単勝配当も含めてしっかり当ててるの凄いです、更新楽しみにしてます",
        # 的中実績の称賛は破棄に入れない（プロンプトでの禁止のみ）
        "ハンデ重賞での的中実績、素直にすごいです。見習いたいです",
        # レース名の分かる個別の的中へのお祝いは許容
        "小倉記念のワンツー的中お見事です、勉強になります",
        "おはようございます☀️ フロムダスク的中とは、その着眼点すごいです。馬券外れは悔しいですね…",
    ],
)
def test_number_praise_re_allows_non_praise_and_single_race_congratulations(reaction):
    assert not prescriber.NUMBER_PRAISE_RE.search(reaction), reaction


def test_generate_drafts_discards_self_stance_and_number_praise_and_retries(monkeypatch):
    """2026-10-13に未送信の下書きに残っていた2例が返ってきても下書きに残らないこと。"""
    match = _find_match("テストホースイチが強かった")
    assert match is not None
    calls = {"n": 0}

    class _FakeMessages:
        def create(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _FakeResponse(json.dumps({"reactions": [
                    "Q&A形式で読みやすいです。ローシャムパークは期待してただけに、結果は謙虚に受け止めます",
                    "僅差での涙、積み重なると大きいですよね。状況下でのこの数字は立派だと思います",
                ]}, ensure_ascii=False))
            return _FakeResponse('{"reactions": ["いい馬ですよね", "気になる存在です"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    candidate = _candidate("c5", "札幌記念のQ&Aをアップしました")
    drafts = prescriber.generate_drafts(candidate, match, model="claude-sonnet-5")

    assert calls["n"] == 2
    assert [d.replace(match.fact_sentence, "").strip() for d in drafts] == [
        "いい馬ですよね", "気になる存在です",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "テストホースイチが強かった",        # 的中系・本命
        "テストホースニに注目していました",  # 見込み違い系
        "テストホースゴに期待しています",    # 未確定
        "テストホースキュウは惜しかった",    # 低スコアの本命
    ],
)
def test_discard_filters_apply_to_reaction_only_not_fact_sentence(monkeypatch, text):
    """破棄フィルタの対象は反応文だけ。事実文は「…本命(N頭中1番手評価)としていて」のように
    評価そのものを述べるのが役目なので、事実文が付いた下書きは破棄されずそのまま残る。"""
    match = _find_match(text)
    assert match is not None
    assert "評価" in match.fact_sentence
    # 事実文は数字を含むので、もし下書き全体にフィルタを掛けていたら必ず破棄される
    assert prescriber.NUMERIC_MARK_RE.search(match.fact_sentence)

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResponse('{"reactions": ["いい馬ですよね", "気になる存在です"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    drafts = prescriber.generate_drafts(_candidate("c1", text), match, model="claude-sonnet-5")
    assert drafts == [
        f"いい馬ですよね {match.fact_sentence}"[: prescriber.MAX_DRAFT_LEN],
        f"気になる存在です {match.fact_sentence}"[: prescriber.MAX_DRAFT_LEN],
    ]


def test_fact_sentence_with_stance_wording_is_kept_verbatim(monkeypatch):
    """事実文が破棄用の正規表現に一致する言い回し（旧テンプレート「…と評価していましたが」）
    であっても、事実文は検査対象ではないので下書きに残る。"""
    from dataclasses import replace

    base = _find_match("テストホースニに注目していました")
    fact = "うちのAIモデルはテストステークスで『テストホースニ』を高く評価していましたが、結果は届きませんでした。"
    assert prescriber.SELF_STANCE_CLAIM_RE.search(fact)
    match = replace(base, fact_sentence=fact)

    class _FakeMessages:
        def create(self, **kwargs):
            return _FakeResponse('{"reactions": ["いい馬ですよね", "気になる存在です"]}')

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    drafts = prescriber.generate_drafts(_candidate("c2", "x"), match, model="claude-sonnet-5")
    assert all(d.endswith(fact) for d in drafts)
    assert prescriber.PLACEHOLDER not in drafts


class _FakeResponseWithStop(_FakeResponse):
    def __init__(self, text: str, stop_reason: str):
        super().__init__(text)
        self.stop_reason = stop_reason


def _patch_responses(monkeypatch, responses):
    """create()が呼ばれるたびにresponsesを順に返す偽クライアントを差し込み、呼び出し記録を返す。"""
    calls = []

    class _FakeMessages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return responses[min(len(calls), len(responses)) - 1]

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(prescriber, "_client", lambda: _FakeClient())
    return calls


def test_call_claude_requests_enough_output_tokens_for_thinking(monkeypatch):
    """max_tokens=300では、adaptive thinkingの思考トークンだけで上限を使い切り、本文が空で
    返ることがあった(2026-10-13、実測で最大2049トークン)。上限に十分な余裕があること。"""
    match = _find_match("テストホースイチが強かった")
    calls = _patch_responses(
        monkeypatch, [_FakeResponse('{"reactions": ["いい馬ですよね", "気になる存在です"]}')]
    )
    prescriber.generate_drafts(_candidate("c1", "x"), match)
    assert calls[0]["max_tokens"] == prescriber.MAX_OUTPUT_TOKENS
    assert prescriber.MAX_OUTPUT_TOKENS >= 8000


def test_generate_drafts_empty_responses_use_empty_placeholder_and_log_attempts(monkeypatch):
    """2回とも本文なし(stop_reason=max_tokens)なら、APIキー未設定とは別の文言で埋め、
    何回目の試行か・stop_reasonをログに残す。"""
    match = _find_match("テストホースイチが強かった")
    calls = _patch_responses(monkeypatch, [_FakeResponseWithStop("", "max_tokens")])
    log: list[str] = []
    drafts = prescriber.generate_drafts(_candidate("c1", "x"), match, log=log)

    assert len(calls) == prescriber.MAX_ATTEMPTS == 2
    assert drafts == [prescriber.PLACEHOLDER_EMPTY, prescriber.PLACEHOLDER_EMPTY]
    assert log[0] == "空応答: @userc1 1回目 stop_reason=max_tokens"
    assert log[1] == "空応答: @userc1 2回目 stop_reason=max_tokens"
    assert log[2].startswith("下書き未生成: @userc1 2案")


def test_generate_drafts_recovers_when_only_first_attempt_is_empty(monkeypatch):
    match = _find_match("テストホースイチが強かった")
    _patch_responses(monkeypatch, [
        _FakeResponseWithStop("", "max_tokens"),
        _FakeResponseWithStop('{"reactions": ["いい馬ですよね", "気になる存在です"]}', "end_turn"),
    ])
    log: list[str] = []
    drafts = prescriber.generate_drafts(_candidate("c1", "x"), match, log=log)

    assert all(match.fact_sentence in d for d in drafts)
    assert not any(p in d for d in drafts for p in prescriber.PLACEHOLDERS)
    assert log == ["空応答: @userc1 1回目 stop_reason=max_tokens"]


def test_generate_drafts_filtered_reactions_use_filtered_placeholder(monkeypatch):
    """返ってきた案がすべて破棄フィルタに該当した場合は「フィルタで破棄」の文言にする。"""
    match = _find_match("テストホースイチが強かった")
    bad = '{"reactions": ["ずっと期待してたので嬉しいです", "この的中率は見習いたいです"]}'
    _patch_responses(monkeypatch, [_FakeResponseWithStop(bad, "end_turn")])
    log: list[str] = []
    drafts = prescriber.generate_drafts(_candidate("c1", "x"), match, log=log)

    assert drafts == [prescriber.PLACEHOLDER_FILTERED, prescriber.PLACEHOLDER_FILTERED]
    assert log[0] == "フィルタで破棄: @userc1 1回目 2案"
    assert log[1] == "フィルタで破棄: @userc1 2回目 2案"
    assert not any(line.startswith("空応答") for line in log)


def test_generate_drafts_keeps_one_good_reaction_and_fills_the_other(monkeypatch):
    match = _find_match("テストホースイチが強かった")
    mixed = '{"reactions": ["いい馬ですよね", "ずっと期待してたので嬉しいです"]}'
    _patch_responses(monkeypatch, [_FakeResponseWithStop(mixed, "end_turn")])
    drafts = prescriber.generate_drafts(_candidate("c1", "x"), match)

    assert drafts[0] == f"いい馬ですよね {match.fact_sentence}"[: prescriber.MAX_DRAFT_LEN]
    assert drafts[1] == prescriber.PLACEHOLDER_FILTERED


def test_generate_drafts_unparsable_response_is_logged_separately(monkeypatch):
    match = _find_match("テストホースイチが強かった")
    _patch_responses(monkeypatch, [_FakeResponseWithStop("すみません、書けません", "end_turn")])
    log: list[str] = []
    drafts = prescriber.generate_drafts(_candidate("c1", "x"), match, log=log)

    assert drafts == [prescriber.PLACEHOLDER_EMPTY, prescriber.PLACEHOLDER_EMPTY]
    assert log[0] == "解析できない応答: @userc1 1回目 stop_reason=end_turn"


def test_generate_drafts_without_api_key_keeps_original_placeholder(monkeypatch):
    """APIキー未設定のときは従来の文言のまま。APIは呼ばず、ログも出さない。"""
    match = _find_match("テストホースイチが強かった")
    monkeypatch.setattr(prescriber, "_client", lambda: None)
    log: list[str] = []
    drafts = prescriber.generate_drafts(_candidate("c1", "x"), match, log=log)

    assert drafts == [prescriber.PLACEHOLDER, prescriber.PLACEHOLDER]
    assert prescriber.PLACEHOLDER == "[下書き生成はAPIキー設定後に有効化]"
    assert log == []


def test_placeholders_are_distinct_per_cause():
    assert len(set(prescriber.PLACEHOLDERS)) == 3
    assert "空応答" in prescriber.PLACEHOLDER_EMPTY
    assert "フィルタで破棄" in prescriber.PLACEHOLDER_FILTERED
    assert "APIキー" in prescriber.PLACEHOLDER


def test_draft_top_candidates_passes_log_through(monkeypatch, conn):
    monkeypatch.setattr(prescriber, "_client", lambda: None)
    db.insert_candidates(conn, [_candidate("a", "テストホースイチが強かった")])
    db.set_ranking(conn, [("a", 0.9, 1)])
    seen = {}

    def _fake_generate(candidate, match, model="m", log=None):
        seen["log"] = log
        log.append("空応答: @usera 1回目 stop_reason=max_tokens")
        return [prescriber.PLACEHOLDER_EMPTY, prescriber.PLACEHOLDER_EMPTY]

    monkeypatch.setattr(prescriber, "generate_drafts", _fake_generate)
    log: list[str] = []
    count = prescriber.draft_top_candidates(account=None, conn=conn, cfg=_cfg(), log=log)

    assert count == 1
    assert seen["log"] is log
    assert log == ["空応答: @usera 1回目 stop_reason=max_tokens"]
