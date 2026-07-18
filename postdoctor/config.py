"""アカウント設定と認証情報の解決。

config/accounts.yaml にマルチアカウントを定義し、各アカウントの X API
認証情報は .env から解決する。認証情報を共有したい場合は credential_prefix
を省略すればデフォルト（X_ で始まるキー）が使われ、アカウントごとに別アプリ
の資格情報を使いたい場合は credential_prefix を指定する
（例: RAKUBA_AI_X_CONSUMER_KEY のようなキー名になる）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config" / "accounts.yaml"
DATA_DIR = ROOT_DIR / "data"

load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Credentials:
    consumer_key: str
    consumer_secret: str
    access_token: str
    access_token_secret: str


@dataclass(frozen=True)
class Account:
    name: str
    screen_name: str
    user_id: str
    credential_prefix: str = "X"

    @property
    def data_dir(self) -> Path:
        d = DATA_DIR / self.name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def db_path(self) -> Path:
        return self.data_dir / "posts.db"

    @property
    def reply_scout_db_path(self) -> Path:
        return self.data_dir / "reply_scout.db"

    def credentials(self) -> Credentials:
        prefix = self.credential_prefix
        try:
            return Credentials(
                consumer_key=os.environ[f"{prefix}_CONSUMER_KEY"],
                consumer_secret=os.environ[f"{prefix}_CONSUMER_SECRET"],
                access_token=os.environ[f"{prefix}_ACCESS_TOKEN"],
                access_token_secret=os.environ[f"{prefix}_ACCESS_TOKEN_SECRET"],
            )
        except KeyError as e:
            raise RuntimeError(
                f"アカウント '{self.name}' の認証情報 {e.args[0]} が .env にありません。"
                f" credential_prefix='{prefix}' に対応するキーを設定してください。"
            ) from e


def load_accounts() -> dict[str, Account]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"アカウント設定ファイルが見つかりません: {CONFIG_PATH}")

    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    accounts: dict[str, Account] = {}
    for name, cfg in (raw.get("accounts") or {}).items():
        accounts[name] = Account(
            name=name,
            screen_name=cfg["screen_name"],
            user_id=str(cfg["user_id"]),
            credential_prefix=cfg.get("credential_prefix", "X"),
        )
    return accounts


def get_account(name: str) -> Account:
    accounts = load_accounts()
    if name not in accounts:
        available = ", ".join(accounts) or "(なし)"
        raise RuntimeError(f"アカウント '{name}' は未設定です。設定済み: {available}")
    return accounts[name]
