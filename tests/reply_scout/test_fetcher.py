from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from postdoctor.config import Account
from postdoctor.reply_scout import db, fetcher


def test_load_config_defaults_and_new_fields(tmp_path, monkeypatch):
    keywords_path = tmp_path / "keywords.json"
    keywords_path.write_text(json.dumps({"keywords": ["競馬"]}), encoding="utf-8")
    monkeypatch.setattr(fetcher, "KEYWORDS_PATH", keywords_path)
    monkeypatch.setattr(fetcher, "NG_WORDS_PATH", tmp_path / "missing_ng.json")
    monkeypatch.setattr(fetcher, "ANALYSIS_TERMS_PATH", tmp_path / "missing_analysis.json")

    cfg = fetcher.load_config()
    assert cfg.draft_top_n == 5
    assert cfg.rakuba_output_dir == ""
    assert cfg.ng_words == []
    assert cfg.solicitation_words == []
    assert cfg.analysis_terms == []
    assert cfg.auto_graded_races is False
    assert cfg.race_aliases == {}
    assert cfg.max_graded_races == 4


def test_load_config_reads_ng_words_and_analysis_terms(tmp_path, monkeypatch):
    keywords_path = tmp_path / "keywords.json"
    keywords_path.write_text(
        json.dumps({"keywords": ["競馬"], "draft_top_n": 3, "rakuba_output_dir": "C:/x"}),
        encoding="utf-8",
    )
    ng_path = tmp_path / "ng_words.json"
    ng_path.write_text(
        json.dumps({"ng_words": ["いいねで"], "solicitation_words": ["限定"]}), encoding="utf-8"
    )
    analysis_path = tmp_path / "analysis_terms.json"
    analysis_path.write_text(json.dumps({"analysis_terms": ["斤量"]}), encoding="utf-8")

    monkeypatch.setattr(fetcher, "KEYWORDS_PATH", keywords_path)
    monkeypatch.setattr(fetcher, "NG_WORDS_PATH", ng_path)
    monkeypatch.setattr(fetcher, "ANALYSIS_TERMS_PATH", analysis_path)

    cfg = fetcher.load_config()
    assert cfg.draft_top_n == 3
    assert cfg.rakuba_output_dir == "C:/x"
    assert cfg.ng_words == ["いいねで"]
    assert cfg.solicitation_words == ["限定"]
    assert cfg.analysis_terms == ["斤量"]


def test_load_config_missing_keywords_json_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "KEYWORDS_PATH", tmp_path / "does_not_exist.json")
    with pytest.raises(RuntimeError):
        fetcher.load_config()


def test_load_word_list_degrades_gracefully_on_malformed_json(tmp_path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    assert fetcher._load_word_list(bad_path, "ng_words") == []


def _account() -> Account:
    return Account(name="rakuba_ai", screen_name="rakuba_ai", user_id="999")


class _Ref:
    def __init__(self, type_: str, id_: str):
        self.type = type_
        self.id = id_


class _Tweet:
    def __init__(self, id_, author_id, text, referenced_tweets=None, public_metrics=None):
        self.id = id_
        self.author_id = author_id
        self.text = text
        self.referenced_tweets = referenced_tweets or []
        self.public_metrics = public_metrics or {}
        self.created_at = datetime(2026, 7, 28, 1, 0, 0, tzinfo=timezone.utc)


class _User:
    def __init__(self, id_, username):
        self.id = id_
        self.username = username


class _Resp:
    def __init__(self, data, users=None):
        self.data = data
        self.includes = {"users": users or []}


class _FakeClient:
    def __init__(self, responses_by_query: dict[str, _Resp]):
        self._responses_by_query = responses_by_query
        self.queries: list[str] = []

    def search_recent_tweets(self, query, **kwargs):
        self.queries.append(query)
        return self._responses_by_query.get(query, _Resp([]))


def _scout_cfg(**overrides) -> fetcher.ScoutConfig:
    base = dict(
        keywords=["競馬"], ng_words=[], solicitation_words=[], analysis_terms=[],
        trusted_authors=[], daily_read_limit=50, min_likes=0, min_replies=0,
        top_user_lookup_limit=15, claude_model="claude-sonnet-5",
        draft_top_n=5, rakuba_output_dir="",
    )
    base.update(overrides)
    return fetcher.ScoutConfig(**base)


def test_collect_candidates_flags_quote_tweets(tmp_path, monkeypatch):
    from postdoctor import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    account = Account(name="rakuba_ai", screen_name="rakuba_ai", user_id="999")

    quote_tweet = _Tweet(
        "t-quote", author_id="1", text="🥳 https://t.co/x",
        referenced_tweets=[_Ref("quoted", "orig-1")],
    )
    plain_tweet = _Tweet("t-plain", author_id="2", text="普通の投稿です")
    fake_resp = _Resp(
        [quote_tweet, plain_tweet], users=[_User("1", "quoter"), _User("2", "plain_user")]
    )
    fake_client = _FakeClient({"競馬 -is:retweet -is:reply lang:ja": fake_resp})
    monkeypatch.setattr(fetcher, "build_client", lambda account: fake_client)

    with db.connect(account) as conn:
        candidates, read_count, _searched = fetcher.collect_candidates(account, conn, _scout_cfg())

    assert read_count == 2
    by_id = {c.id: c for c in candidates}
    assert by_id["t-quote"].is_quote is True
    assert by_id["t-plain"].is_quote is False


class _CountingClient:
    """max_resultsを記録し、queryごとに指定件数(省略時はmax_results件)の投稿を返す。"""

    def __init__(self, returned_by_keyword: dict[str, int] | None = None):
        self.calls: list[tuple[str, int]] = []
        self._returned = returned_by_keyword or {}

    def search_recent_tweets(self, query, max_results, **kwargs):
        self.calls.append((query, max_results))
        n = max_results
        for kw, count in self._returned.items():
            if query.startswith(kw):
                n = min(count, max_results)
        tweets = [_Tweet(f"{query}-{i}", author_id="1", text="t") for i in range(n)]
        return _Resp(tweets, users=[_User("1", "u")])


def _collect(tmp_path, monkeypatch, client, cfg, groups, already_read=0):
    from postdoctor import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(fetcher, "build_client", lambda account: client)
    with db.connect(_account()) as conn:
        if already_read:
            db.add_usage(conn, "tweet_read", already_read)
        return fetcher.collect_candidates(_account(), conn, cfg, groups)


def test_collect_candidates_splits_budget_evenly_across_keywords(tmp_path, monkeypatch):
    """先頭の1語で日次上限を使い切らない（2026-09-08〜10-05に「重賞」が一度も
    検索されていなかった不具合の回帰テスト）。"""
    client = _CountingClient()
    groups = [("A",), ("B",), ("C",), ("D",), ("E",)]
    _, read_count, searched = _collect(
        tmp_path, monkeypatch, client, _scout_cfg(daily_read_limit=50), groups
    )
    assert [m for _q, m in client.calls] == [10, 10, 10, 10, 10]
    assert read_count == 50
    assert searched == [("A", 10), ("B", 10), ("C", 10), ("D", 10), ("E", 10)]


def test_collect_candidates_drops_keywords_beyond_query_count(tmp_path, monkeypatch):
    """max_resultsの下限が10なので、上限50なら検索は5回まで。後ろの語から切り捨てる。"""
    client = _CountingClient()
    groups = [(k,) for k in "ABCDEFG"]
    _, read_count, searched = _collect(
        tmp_path, monkeypatch, client, _scout_cfg(daily_read_limit=50), groups
    )
    assert [kw for kw, _n in searched] == ["A", "B", "C", "D", "E"]
    assert read_count == 50


def test_collect_candidates_carries_unused_budget_forward(tmp_path, monkeypatch):
    client = _CountingClient({"A ": 3})
    groups = [("A",), ("B",), ("C",)]
    _, read_count, searched = _collect(
        tmp_path, monkeypatch, client, _scout_cfg(daily_read_limit=50), groups
    )
    # A: 50//3=16枠で3件 -> 残り47を2語で 23, 24
    assert [m for _q, m in client.calls] == [16, 23, 24]
    assert searched == [("A", 3), ("B", 23), ("C", 24)]
    assert read_count == 50


def test_collect_candidates_respects_already_used_budget(tmp_path, monkeypatch):
    client = _CountingClient()
    groups = [("A",), ("B",), ("C",)]
    _, read_count, searched = _collect(
        tmp_path, monkeypatch, client, _scout_cfg(daily_read_limit=50), groups, already_read=30
    )
    # 残り20 -> 検索は2回(10件ずつ)まで
    assert [m for _q, m in client.calls] == [10, 10]
    assert read_count == 20
    assert [kw for kw, _n in searched] == ["A", "B"]


def test_collect_candidates_builds_or_query_for_variants(tmp_path, monkeypatch):
    """X検索は「〜S」と「〜ステークス」を同一視しないため、表記の組は1回のOR検索にまとめる。"""
    client = _CountingClient()
    candidates, _, searched = _collect(
        tmp_path, monkeypatch, client, _scout_cfg(daily_read_limit=10),
        [("スワンS", "スワンステークス")],
    )
    assert client.calls[0][0] == "(スワンS OR スワンステークス) -is:retweet -is:reply lang:ja"
    assert searched == [("スワンS", 10)]
    assert {c.keyword for c in candidates} == {"スワンS"}


def test_collect_candidates_defaults_to_config_keywords(tmp_path, monkeypatch):
    client = _CountingClient()
    _collect(tmp_path, monkeypatch, client, _scout_cfg(keywords=["重賞", "競馬予想"]), None)
    assert [q for q, _m in client.calls] == [
        "重賞 -is:retweet -is:reply lang:ja", "競馬予想 -is:retweet -is:reply lang:ja",
    ]


def _stats_scenario(tmp_path, monkeypatch, stats):
    """A: 既知1・いいね不足1・保存1 / B: Aで拾った投稿の重複1・保存1 / C: 0件。"""
    from postdoctor import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    liked = {"like_count": 9}
    suffix = " -is:retweet -is:reply lang:ja"
    client = _FakeClient({
        "A" + suffix: _Resp([
            _Tweet("known-1", "1", "t", public_metrics=liked),
            _Tweet("low-1", "1", "t", public_metrics={"like_count": 4}),
            _Tweet("new-1", "1", "t", public_metrics=liked),
        ], users=[_User("1", "u")]),
        "(B OR Bステークス)" + suffix: _Resp([
            _Tweet("new-1", "1", "t", public_metrics=liked),
            _Tweet("new-2", "1", "t", public_metrics=liked),
        ], users=[_User("1", "u")]),
    })
    monkeypatch.setattr(fetcher, "build_client", lambda account: client)
    cfg = _scout_cfg(daily_read_limit=30, min_likes=5)
    groups = [("A",), ("B", "Bステークス"), ("C",)]
    with db.connect(_account()) as conn:
        db.insert_candidates(conn, [db.Candidate(
            id="known-1", text="t", author_id="1", author_screen_name="u",
            created_at="2026-07-21T00:00:00+09:00", likes=9, retweets=0, replies=0, quotes=0,
            keyword="A",
        )])
        result = fetcher.collect_candidates(_account(), conn, cfg, groups, stats=stats)
        inserted = db.insert_candidates(conn, result[0])
    return result, inserted


def test_collect_candidates_records_per_keyword_stats(tmp_path, monkeypatch):
    """検索語ごとの読み取り・保存・落とした理由を記録する（2026-10-13、50件読んで2件しか
    保存されなかった理由を後から確認できなかったため）。"""
    stats: list[db.SearchStat] = []
    (_candidates, read_count, _searched), inserted = _stats_scenario(tmp_path, monkeypatch, stats)

    suffix = " -is:retweet -is:reply lang:ja"
    assert stats == [
        db.SearchStat("A", "A" + suffix, 10, 3, 1, 1, 1, 0),
        db.SearchStat("B", "(B OR Bステークス)" + suffix, 13, 2, 1, 0, 0, 1),
        db.SearchStat("C", "C" + suffix, 25, 0, 0, 0, 0, 0),
    ]
    # 内訳は実際の読み取り数・保存数と一致する
    assert sum(s.read_count for s in stats) == read_count == 5
    assert sum(s.saved_count for s in stats) == inserted == 2
    for s in stats:
        assert s.read_count == (
            s.saved_count + s.dropped_known + s.dropped_low_engagement + s.dropped_duplicate
        )


def test_collect_candidates_stats_do_not_change_results(tmp_path, monkeypatch):
    """statsは記録だけ。渡しても渡さなくても、候補・読み取り数・検索語は同じ。"""
    with_stats, _ = _stats_scenario(tmp_path / "a", monkeypatch, [])
    (tmp_path / "b").mkdir()
    without_stats, _ = _stats_scenario(tmp_path / "b", monkeypatch, None)
    assert with_stats == without_stats
    assert [c.id for c in with_stats[0]] == ["new-1", "new-1", "new-2"]
    assert with_stats[2] == [("A", 3), ("B", 2), ("C", 0)]


def test_load_config_reads_auto_graded_races_and_aliases(tmp_path, monkeypatch):
    keywords_path = tmp_path / "keywords.json"
    keywords_path.write_text(json.dumps({
        "keywords": ["重賞"],
        "auto_graded_races": {"enabled": True, "grades": ["A", "B"], "max_races": 3},
        "race_aliases": {"産経賞オールカマー": "オールカマー", "スワンステークス": ["スワンS", ""]},
        "rakuba_cancelled_races_path": "C:/x/cancelled_races.json",
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(fetcher, "KEYWORDS_PATH", keywords_path)
    cfg = fetcher.load_config()
    assert cfg.auto_graded_races is True
    assert cfg.graded_grades == ("A", "B")
    assert cfg.max_graded_races == 3
    # 文字列1つでもリストに正規化し、空の表記は捨てる
    assert cfg.race_aliases == {"産経賞オールカマー": ["オールカマー"], "スワンステークス": ["スワンS"]}
    assert cfg.rakuba_cancelled_races_path == "C:/x/cancelled_races.json"


def test_fetch_reply_responses_excludes_self_and_unrelated_conversation_replies(monkeypatch):
    account = _account()
    targets = [db.TrackTarget(reply_id="reply-1", candidate_id="cand-1", sent_at="2026-07-27T00:00:00+00:00")]

    other_reply = _Tweet("resp-1", author_id="111", text="ありがとうございます", referenced_tweets=[_Ref("replied_to", "reply-1")])
    own_followup = _Tweet("resp-2", author_id="999", text="答え合わせです", referenced_tweets=[_Ref("replied_to", "reply-1")])
    unrelated_reply_to_op = _Tweet("resp-3", author_id="222", text="元投稿への無関係な返信", referenced_tweets=[_Ref("replied_to", "cand-1")])

    fake_resp = _Resp([other_reply, own_followup, unrelated_reply_to_op], users=[_User("111", "fan_user")])
    fake_client = _FakeClient({"conversation_id:cand-1": fake_resp})
    monkeypatch.setattr(fetcher, "build_client", lambda account: fake_client)

    responses, read_count = fetcher.fetch_reply_responses(account, targets)

    assert read_count == 3
    assert len(responses) == 1
    assert responses[0].id == "resp-1"
    assert responses[0].parent_reply_id == "reply-1"
    assert responses[0].author_username == "fan_user"


def test_fetch_reply_responses_groups_targets_by_candidate_id(monkeypatch):
    account = _account()
    targets = [
        db.TrackTarget(reply_id="reply-1", candidate_id="cand-1", sent_at="2026-07-27T00:00:00+00:00"),
        db.TrackTarget(reply_id="reply-2", candidate_id="cand-1", sent_at="2026-07-27T00:00:00+00:00"),
        db.TrackTarget(reply_id="reply-3", candidate_id="cand-2", sent_at="2026-07-27T00:00:00+00:00"),
    ]
    fake_client = _FakeClient({})
    monkeypatch.setattr(fetcher, "build_client", lambda account: fake_client)

    fetcher.fetch_reply_responses(account, targets)

    assert sorted(fake_client.queries) == ["conversation_id:cand-1", "conversation_id:cand-2"]


def test_fetch_reply_responses_returns_empty_for_no_targets(monkeypatch):
    account = _account()
    monkeypatch.setattr(fetcher, "build_client", lambda account: (_ for _ in ()).throw(AssertionError("called")))

    responses, read_count = fetcher.fetch_reply_responses(account, [])

    assert responses == []
    assert read_count == 0


def test_fetch_reply_responses_returns_empty_when_max_read_is_zero(monkeypatch):
    account = _account()
    targets = [db.TrackTarget(reply_id="reply-1", candidate_id="cand-1", sent_at="2026-07-27T00:00:00+00:00")]
    monkeypatch.setattr(fetcher, "build_client", lambda account: (_ for _ in ()).throw(AssertionError("called")))

    responses, read_count = fetcher.fetch_reply_responses(account, targets, max_read=0)

    assert responses == []
    assert read_count == 0


def test_fetch_reply_responses_stops_issuing_queries_once_max_read_exhausted(monkeypatch):
    account = _account()
    targets = [
        db.TrackTarget(reply_id="reply-1", candidate_id="cand-1", sent_at="2026-07-27T00:00:00+00:00"),
        db.TrackTarget(reply_id="reply-2", candidate_id="cand-2", sent_at="2026-07-27T00:00:00+00:00"),
    ]
    tweet_a = _Tweet("resp-a", author_id="111", text="a", referenced_tweets=[_Ref("replied_to", "reply-1")])
    tweet_b = _Tweet("resp-b", author_id="111", text="b", referenced_tweets=[_Ref("replied_to", "reply-2")])
    # cand-1の1件読み取りだけでmax_read(=1)を使い切るので、cand-2へは問い合わせないはず
    fake_client = _FakeClient({
        "conversation_id:cand-1": _Resp([tweet_a], users=[_User("111", "u")]),
        "conversation_id:cand-2": _Resp([tweet_b], users=[_User("111", "u")]),
    })
    monkeypatch.setattr(fetcher, "build_client", lambda account: fake_client)

    responses, read_count = fetcher.fetch_reply_responses(account, targets, max_read=1)

    assert read_count == 1
    assert len(fake_client.queries) == 1


def test_load_config_candidate_max_age_days_defaults_to_seven_and_can_be_overridden(
    tmp_path, monkeypatch
):
    keywords_path = tmp_path / "keywords.json"
    monkeypatch.setattr(fetcher, "KEYWORDS_PATH", keywords_path)
    monkeypatch.setattr(fetcher, "NG_WORDS_PATH", tmp_path / "missing_ng.json")
    monkeypatch.setattr(fetcher, "ANALYSIS_TERMS_PATH", tmp_path / "missing_analysis.json")

    keywords_path.write_text(json.dumps({"keywords": ["競馬"]}), encoding="utf-8")
    assert fetcher.load_config().candidate_max_age_days == 7 == fetcher.DEFAULT_CANDIDATE_MAX_AGE_DAYS

    keywords_path.write_text(
        json.dumps({"keywords": ["競馬"], "candidate_max_age_days": 3}), encoding="utf-8"
    )
    assert fetcher.load_config().candidate_max_age_days == 3
