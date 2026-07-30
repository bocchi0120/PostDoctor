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
    def __init__(self, id_, author_id, text, referenced_tweets=None):
        self.id = id_
        self.author_id = author_id
        self.text = text
        self.referenced_tweets = referenced_tweets or []
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
