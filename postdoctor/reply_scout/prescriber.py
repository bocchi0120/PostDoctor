"""処方箋層: 候補投稿に対するリプライ下書きをClaude APIで2案生成する。

analysis層(db層が返すランキング済み候補)とprediction_data層(Rakubaの実予測データ)
のみを入力とする。

**捏造防止の設計**: Claudeには実データの数値・評価・着順を一切書かせない。
Claudeの役割は候補投稿への短い反応文({"reactions": [...]}）の生成のみに限定し、
数値・スコア・マーク(◎/〇/△/×/▲)を含む「事実文」はprediction_data.find_match()
がPython側で決定的に組み立てたfact_sentenceをそのまま文字列結合する。
Claudeが指示に反して数字/マークを含む反応文を返した場合、その案は多重防御として
破棄する(NUMERIC_MARK_RE参照)。

該当する予測データが見つからない候補は、Claude呼び出し自体を行わずスキップし
（コストゼロ）、candidates.prediction_status='予測データ未投入'として記録する。

Claude drafting is mocked until `ANTHROPIC_API_KEY` is set in .env —
`prescriber._client()` returns `None` and generate_drafts() returns a
placeholder string, same call signature either way.
"""

from __future__ import annotations

import json
import os
import re

from postdoctor.config import Account
from postdoctor.reply_scout import db, prediction_data
from postdoctor.reply_scout.db import Candidate
from postdoctor.reply_scout.fetcher import ScoutConfig
from postdoctor.reply_scout.prediction_data import PredictionMatch

PLACEHOLDER = "[下書き生成はAPIキー設定後に有効化]"
MAX_DRAFT_LEN = 140
MIN_REACTION_LEN = 20
# 半角数字・マークに加え、漢数字+「着」（例:「二着」「十二着」）も事実の混入とみなして弾く。
NUMERIC_MARK_RE = re.compile(r"[0-9◎〇△×▲]|[一二三四五六七八九十]{1,3}着")

SYSTEM_PROMPT = """あなたは競馬アカウント「@rakuba_ai」の中の人です。
X上で見つけた他ユーザーの投稿に返信する、短い反応文の下書きを作成します。

方針:
- 宣伝臭を出さない。まず相手の投稿内容に具体的に反応する
- 絵文字は控えめに、断定的な物言いは避ける
- 【厳守】数値・スコア・評価・マーク(◎/〇/△/×/▲)・着順・「結果」といった、
  データに基づく事実の内容は一切書かないこと。それらは別途システム側で
  正確な実データから付加するので、あなたはそれ以外の自然な反応文だけを書く
- 与えられた「トーン指示」がある場合は必ずそれに従うこと（予測が外れた場合も
  隠さず開示する方針のアカウントなので、外れを茶化したり過度に卑下したりせず、
  素直に受け止めるトーンにする）
- 必ず次のJSON形式のみで出力する: {"reactions": ["案1", "案2"]}
"""

# カテゴリ別のトーン指示。fact_sentence自体はprediction_data側で確定済みの
# 事実文だが、Claudeが書く「反応文」のトーンをカテゴリに応じて変える。
_CATEGORY_TONE = {
    "的中系": "少し誇らしい気持ちは込めつつも、控えめなトーンで。",
    "見込み違い系": "高く評価していた予測が外れたことを、言い訳せず素直に認めるトーンで。",
    "完敗系": "評価を覆されたことを、卑屈にならず謙虚に認めるトーンで。",
    "順当系": "淡々とした、謙虚なトーンで。",
}


def _client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    import anthropic

    return anthropic.Anthropic(api_key=key)


def _reaction_budget(fact_sentence: str) -> int:
    return max(MAX_DRAFT_LEN - len(fact_sentence) - 1, MIN_REACTION_LEN)


def _build_user_prompt(candidate: Candidate, reaction_budget: int, category: str | None) -> str:
    lines = [
        f"相手の投稿（@{candidate.author_screen_name}）: {candidate.text}",
        f"この投稿への短い反応文を{reaction_budget}字以内で2案作成してください。",
    ]
    tone = _CATEGORY_TONE.get(category) if category else None
    if tone:
        lines.append(f"トーン指示: {tone}")
    return "\n".join(lines)


def _call_claude(
    candidate: Candidate, reaction_budget: int, model: str, category: str | None
) -> list[str]:
    client = _client()
    if client is None:
        return [PLACEHOLDER, PLACEHOLDER]

    resp = client.messages.create(
        model=model,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": _build_user_prompt(candidate, reaction_budget, category),
            }
        ],
    )
    raw_text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    try:
        start = raw_text.index("{")
        end = raw_text.rindex("}") + 1
        parsed = json.loads(raw_text[start:end])
        reactions = [str(r).strip() for r in parsed.get("reactions", [])]
    except (ValueError, json.JSONDecodeError):
        reactions = []

    # 多重防御: 数字・マークが混入した案は、文字を削って繋げるのではなく案ごと破棄する
    # （不自然な文になることを避け、確定していない事実が紛れ込む余地を構造的に無くす）。
    return [r for r in reactions if r and not NUMERIC_MARK_RE.search(r)]


def generate_drafts(
    candidate: Candidate, match: PredictionMatch, model: str = "claude-sonnet-5"
) -> list[str]:
    """matchはNoneであってはならない（呼び出し側でNoneならこの関数自体を呼ばないこと）。"""
    reaction_budget = _reaction_budget(match.fact_sentence)
    reactions = _call_claude(candidate, reaction_budget, model, match.category)

    if len(reactions) < 2 and _client() is not None:
        for r in _call_claude(candidate, reaction_budget, model, match.category):
            if r not in reactions:
                reactions.append(r)

    while len(reactions) < 2:
        reactions.append(PLACEHOLDER)
    reactions = reactions[:2]

    drafts = []
    for r in reactions:
        if r == PLACEHOLDER:
            drafts.append(PLACEHOLDER)
        else:
            combined = f"{r[:reaction_budget]} {match.fact_sentence}"
            drafts.append(combined[:MAX_DRAFT_LEN])
    return drafts


def draft_top_candidates(account: Account, conn, cfg: ScoutConfig) -> int:
    """rank設定済み・未送信・上位cfg.draft_top_n件のうち、まだ下書きがないものに
    下書きを生成して保存する。

    Rakuba予測データに一致しない候補は生成をハードスキップし
    （Claude API呼び出しゼロ）、prediction_status='予測データ未投入'として記録する。
    """
    pcfg = prediction_data.load_prediction_config(cfg.rakuba_output_dir)
    predictions = prediction_data.load_all_predictions(pcfg)
    results = prediction_data.load_race_results(pcfg)
    confirmed = prediction_data.confirmed_race_keys(results)
    predictions_hash = prediction_data.compute_predictions_signature(pcfg)

    count = 0
    for ranked in db.get_draftable_candidates(conn, cfg.draft_top_n):
        if ranked.drafts:
            continue
        match, skip_reason = prediction_data.find_match(
            ranked.candidate.text, predictions, results, confirmed
        )
        if match is None:
            status = (
                db.PREDICTION_STATUS_TIME_MISMATCH
                if skip_reason == prediction_data.REASON_TIME_MISMATCH
                else db.PREDICTION_STATUS_NO_MATCH
            )
            db.set_prediction_status(conn, ranked.candidate.id, status)
            continue
        db.clear_prediction_status(conn, ranked.candidate.id)
        drafts = generate_drafts(ranked.candidate, match, model=cfg.claude_model)
        db.save_drafts(conn, ranked.candidate.id, drafts, predictions_hash=predictions_hash)
        count += 1
    return count
