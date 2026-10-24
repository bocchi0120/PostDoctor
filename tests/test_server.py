from __future__ import annotations

import logging

import pytest

from postdoctor import config, server
from postdoctor.config import Account


@pytest.fixture
def restore_root_logger():
    """configure_file_logging()はroot loggerを差し替えるので、テスト後に元へ戻す。"""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for h in root.handlers:
        if h not in handlers:
            h.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def test_serve_log_path_is_under_gitignored_account_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    account = Account(name="testacct", screen_name="testacct", user_id="1")
    assert server.serve_log_path(account) == tmp_path / "testacct" / "logs" / "serve.log"


def test_configure_file_logging_writes_warnings_and_above_only(tmp_path, restore_root_logger):
    log_path = tmp_path / "logs" / "serve.log"
    handler = server.configure_file_logging(log_path)

    logger = logging.getLogger("postdoctor.reply_scout.prescriber")
    logger.info("情報は出さない")
    logger.warning("フィルタで破棄: @user 1回目 自分の評価「ずっと期待してたので」")
    logger.error("エラーも出す")
    handler.flush()

    text = log_path.read_text(encoding="utf-8")
    assert "情報は出さない" not in text
    assert "WARNING postdoctor.reply_scout.prescriber: フィルタで破棄: @user 1回目" in text
    assert "ERROR postdoctor.reply_scout.prescriber: エラーも出す" in text


def test_configure_file_logging_rotates_at_one_megabyte_keeping_three_backups(
    tmp_path, restore_root_logger
):
    handler = server.configure_file_logging(tmp_path / "serve.log")
    assert handler.maxBytes == 1_000_000
    assert handler.backupCount == 3

    logger = logging.getLogger("postdoctor.test")
    line = "x" * 1000
    for _ in range(5000):  # 約5MB書いて、世代が3つで止まることを確かめる
        logger.warning(line)
    handler.flush()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["serve.log", "serve.log.1", "serve.log.2", "serve.log.3"]


class _FakeServer:
    """ThreadingHTTPServerの代役。待ち受けアドレスを記録し、serve_foreverは即停止する。"""

    bound: tuple | None = None

    def __init__(self, address, handler):
        _FakeServer.bound = address

    def serve_forever(self):
        raise KeyboardInterrupt

    def server_close(self):
        pass


@pytest.fixture
def fake_serve_env(tmp_path, monkeypatch, restore_root_logger):
    """serve()をネットワーク・実データ無しで呼べるようにする。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "ThreadingHTTPServer", _FakeServer)
    monkeypatch.setattr(server, "load_posts_df", lambda account: None)
    monkeypatch.setattr(server, "add_derived_columns", lambda df: df)
    monkeypatch.setattr(server, "generate_prescription", lambda df, account: "")
    monkeypatch.setattr(server, "generate_dashboard", lambda df, account, text: None)
    _FakeServer.bound = None
    return Account(name="testacct", screen_name="testacct", user_id="1")


def test_serve_listens_on_localhost_only_by_default(fake_serve_env):
    server.serve(fake_serve_env, port=8765, open_browser=False)
    assert _FakeServer.bound == ("127.0.0.1", 8765)


def test_serve_binds_given_host_but_still_shows_localhost_url(fake_serve_env, capsys):
    server.serve(fake_serve_env, port=8765, open_browser=False, host="0.0.0.0")
    assert _FakeServer.bound == ("0.0.0.0", 8765)
    out = capsys.readouterr().out
    assert "http://127.0.0.1:8765/" in out
    assert "0.0.0.0" not in out


def test_cli_serve_host_defaults_to_localhost_and_is_passed_through(monkeypatch):
    from postdoctor import cli

    assert cli.build_parser().parse_args(["serve", "--account", "x"]).host == "127.0.0.1"

    calls = []
    monkeypatch.setattr(cli, "get_account", lambda name: name)
    monkeypatch.setattr(server, "serve", lambda account, **kw: calls.append((account, kw)))
    cli.main(["serve", "--account", "x", "--host", "0.0.0.0", "--no-browser"])
    assert calls == [("x", {"port": 8765, "open_browser": False, "host": "0.0.0.0"})]
