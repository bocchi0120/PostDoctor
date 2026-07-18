"""処方箋層: 候補投稿に対するリプライ下書きをClaude APIで2案生成する。

analysis層(db層が返すランキング済み候補)のみを入力とする。
ANTHROPIC_API_KEY が .env に設定されるまでは generate_drafts() は
プレースホルダを返すモックとして動作する。キーを追加するだけで、
呼び出し側のコードを一切変更せずに本実装へ切り替わる。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from postdoctor.config import Account
from postdoctor.reply_scout import db
from postdoctor.reply_scout.db import Candidate

PLACEHOLDER = "[下書き生成はAPIキー設定後に有効化]"

SYSTEM_PROMPT = """あなたは競馬アカウント「@rakuba_ai」の中の人です。
X上で見つけた他ユーザーの投稿に返信する下書きを作成します。

方針:
- 宣伝臭を出さない。まず相手の投稿内容に具体的に反応する
- Rakubaの予測データが与えられた場合のみ、根拠として一言添える程度に触れる（与えられなければ触れない）
- 絵文字は控えめに、断定的な物言いは避ける（予想が外れた時の信頼低下対策）
- 140字以内
- 必ず次のJSON形式のみで出力する: {"drafts": ["案1", "案2"]}
"""


def _client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=key)


def load_predictions(account: Account, path: str | None = None) -> list[dict]:
    predictions_path = Path(path) if path else account.data_dir / "predictions.json"
    if not predictions_path.exists():
        return []
    return json.loads(predictions_path.read_text(encoding="utf-8"))


def find_relevant_prediction(text: str, predictions: list[dict]) -> dict | None:
    for p in predictions:
        race_name = p.get("race_name", "")
        if race_name and race_name in text:
            return p
    return None


def _build_user_prompt(candidate: Candidate, prediction: dict | None) -> str:
    lines = [f"相手の投稿（@{candidate.author_screen_name}）: {candidate.text}"]
    if prediction:
        lines.append(
            f"参考にできるRakubaの予測データ（{prediction.get('race_name', '')} "
            f"{prediction.get('date', '')}）: {prediction.get('summary', '')}"
        )
    else:
        lines.append("この投稿に対応する予測データはありません。データ引用なしで反応してください。")
    return "\n".join(lines)


def generate_drafts(
    candidate: Candidate, prediction: dict | None, model: str = "claude-sonnet-5"
) -> list[str]:
    client = _client()
    if client is None:
        return [PLACEHOLDER, PLACEHOLDER]

    resp = client.messages.create(
        model=model,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _build_user_prompt(candidate, prediction)}],
    )
    raw_text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    try:
        start = raw_text.index("{")
        end = raw_text.rindex("}") + 1
        parsed = json.loads(raw_text[start:end])
        drafts = [str(d).strip()[:140] for d in parsed.get("drafts", [])]
    except (ValueError, json.JSONDecodeError):
        drafts = []

    while len(drafts) < 2:
        drafts.append(PLACEHOLDER)
    return drafts[:2]


def draft_top_candidates(
    account: Account, conn, model: str = "claude-sonnet-5", predictions_path: str | None = None
) -> int:
    """rank設定済み（TOP10）の候補のうち、まだ下書きがないものに下書きを生成して保存する。"""
    predictions = load_predictions(account, predictions_path)
    count = 0
    for ranked in db.get_top_candidates(conn):
        if ranked.drafts:
            continue
        prediction = find_relevant_prediction(ranked.candidate.text, predictions)
        drafts = generate_drafts(ranked.candidate, prediction, model=model)
        db.save_drafts(conn, ranked.candidate.id, drafts)
        count += 1
    return count
