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
