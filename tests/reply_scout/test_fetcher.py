from __future__ import annotations

import json

import pytest

from postdoctor.reply_scout import fetcher


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
    assert cfg.analysis_terms == []


def test_load_config_reads_ng_words_and_analysis_terms(tmp_path, monkeypatch):
    keywords_path = tmp_path / "keywords.json"
    keywords_path.write_text(
        json.dumps({"keywords": ["競馬"], "draft_top_n": 3, "rakuba_output_dir": "C:/x"}),
        encoding="utf-8",
    )
    ng_path = tmp_path / "ng_words.json"
    ng_path.write_text(json.dumps({"ng_words": ["限定"]}), encoding="utf-8")
    analysis_path = tmp_path / "analysis_terms.json"
    analysis_path.write_text(json.dumps({"analysis_terms": ["斤量"]}), encoding="utf-8")

    monkeypatch.setattr(fetcher, "KEYWORDS_PATH", keywords_path)
    monkeypatch.setattr(fetcher, "NG_WORDS_PATH", ng_path)
    monkeypatch.setattr(fetcher, "ANALYSIS_TERMS_PATH", analysis_path)

    cfg = fetcher.load_config()
    assert cfg.draft_top_n == 3
    assert cfg.rakuba_output_dir == "C:/x"
    assert cfg.ng_words == ["限定"]
    assert cfg.analysis_terms == ["斤量"]


def test_load_config_missing_keywords_json_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "KEYWORDS_PATH", tmp_path / "does_not_exist.json")
    with pytest.raises(RuntimeError):
        fetcher.load_config()


def test_load_word_list_degrades_gracefully_on_malformed_json(tmp_path):
    bad_path = tmp_path / "bad.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    assert fetcher._load_word_list(bad_path, "ng_words") == []
