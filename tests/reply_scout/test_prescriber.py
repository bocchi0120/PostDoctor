from __future__ import annotations

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
