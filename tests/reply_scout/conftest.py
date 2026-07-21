from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from postdoctor.reply_scout import db, prediction_data

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "rakuba_output"


@pytest.fixture
def pcfg() -> prediction_data.PredictionConfig:
    return prediction_data.load_prediction_config(str(FIXTURES_DIR))


@pytest.fixture
def conn():
    """スキーマ初期化済みのインメモリSQLite接続（実プロジェクトのdata/には触れない）。"""
    connection = sqlite3.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()
