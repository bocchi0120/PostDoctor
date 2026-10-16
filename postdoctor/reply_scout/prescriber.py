"""処方箋層: 候補投稿に対するリプライ下書きをClaude APIで2案生成する。

analysis層(db層が返すランキング済み候補)とprediction_data層(Rakubaの実予測データ)
のみを入力とする。

**捏造防止の設計**: Claudeには実データの数値・評価・着順を一切書かせない。
Claudeの役割は候補投稿への短い反応文({"reactions": [...]}）の生成のみに限定し、
数値・スコア・マーク(◎/〇/△/×/▲)を含む「事実文」はprediction_data.find_match()
がPython側で決定的に組み立てたfact_sentenceをそのまま文字列結合する。
Claudeが指示に反して数字/マークを含む反応文を返した場合、その案は多重防御として
破棄する(NUMERIC_MARK_RE参照)。相手の予想とRakubaの予想の一致・不一致を主張する
反応文も、Claudeはそもそも比較する材料を持たないので同様に破棄する(AGREEMENT_CLAIM_RE)。
同じ理由で、自分の過去の評価・期待を述べる反応文(SELF_STANCE_CLAIM_RE)と、相手が書いた
検証できない数字を称賛する反応文(NUMBER_PRAISE_RE)も破棄する。

該当する予測データが見つからない候補は、Claude呼び出し自体を行わずスキップし
（コストゼロ）、candidates.prediction_status='予測データ未投入'として記録する。

Claude drafting is mocked until `ANTHROPIC_API_KEY` is set in .env —
`prescriber._client()` returns `None` and generate_drafts() returns a
placeholder string, same call signature either way. Placeholders differ by cause
(no API key / empty response / discarded by filters); see PLACEHOLDERS.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from postdoctor.config import Account
from postdoctor.reply_scout import db, prediction_data
from postdoctor.reply_scout.db import Candidate
from postdoctor.reply_scout.fetcher import ScoutConfig
from postdoctor.reply_scout.prediction_data import PredictionMatch

logger = logging.getLogger(__name__)

# 下書きを生成できなかったときに、反応文+事実文の代わりに保存する文言。原因別に分ける
# （以前は全部「APIキー設定後に有効化」で、キーがあるのに空応答で落ちた場合も区別できなかった）。
PLACEHOLDER = "[下書き生成はAPIキー設定後に有効化]"  # APIキー未設定
PLACEHOLDER_EMPTY = "[空応答のため生成できませんでした]"  # 本文なし・解析不能・案の不足
PLACEHOLDER_FILTERED = "[フィルタで破棄されたため生成できませんでした]"  # 破棄フィルタに該当
PLACEHOLDERS = (PLACEHOLDER, PLACEHOLDER_EMPTY, PLACEHOLDER_FILTERED)

# 出力トークンの上限。Claude Sonnet 5はthinkingを指定しないとadaptive thinkingで動き、
# 思考トークンもこの上限に数えられる。以前の300では思考だけで使い切り、
# stop_reason=max_tokens・本文なしで返ることがあった（2026-10-13、12回の実測で出力は
# 71〜2049トークン、うち2回が300超）。上限は課金額ではなく天井なので、非ストリーミングで
# タイムアウトしない範囲で余裕を持たせる。
MAX_OUTPUT_TOKENS = 16000
MAX_ATTEMPTS = 2  # 2案そろわなければ1回だけ再試行する
MAX_DRAFT_LEN = 140
MIN_REACTION_LEN = 20
# 半角数字・マークに加え、漢数字+「着」（例:「二着」「十二着」）も事実の混入とみなして弾く。
NUMERIC_MARK_RE = re.compile(r"[0-9◎〇△×▲]|[一二三四五六七八九十]{1,3}着")

# 相手の予想とうちの予想の一致・不一致を主張する表現。反応文を書くClaudeは相手の印と
# Rakubaの印を比較していない(Rakubaの予測データ自体を渡していない)ので、「自分も近い
# 見立てでした」のような主張は根拠の無い捏造になる（2026-09-29、候補#7
# @naanaashii_1 の案1で実際に発生）。NUMERIC_MARK_REと同じく、該当した案は破棄する。
_VIEW_WORDS = r"(?:見立て|評価|予想|印|本命|軸|狙い|結論|見解|意見|考え)"
_SELF_WORDS = r"(?:自分|こちら|うち|私|僕|俺|当方)"
AGREEMENT_CLAIM_RE = re.compile(
    "|".join(
        [
            # 自分も近い見立て / こちらと同じ / うちも似た
            _SELF_WORDS + r"(?:も|と)[^。、！!？?]{0,6}(?:近い|近く|同じ|一緒|似た|似て|同様)",
            # 自分も高く見てた / こちらも本命 / うちも狙ってた
            _SELF_WORDS + r"も[^。、！!？?]{0,4}(?:高く|低く|評価し|推し|狙っ|本命|軸に|買っ|重視)",
            # 近い見立て / 同じ評価 / 似た予想
            r"(?:近い|同じ|似た|同様の)" + _VIEW_WORDS,
            # 見立てが一致 / 評価が被る / 本命が重なる / 見解が分かれる
            _VIEW_WORDS + r"(?:が|も|は)?(?:近い|同じ|一緒|似て|一致|被|かぶ|重な|分かれ|割れ)",
            # こちらとは見立てが違う / 見立てとは違う
            r"(?:とは|と)" + _VIEW_WORDS + r"(?:が|は)?(?:違|異な)",
            _VIEW_WORDS + r"(?:とは|と)(?:違|異な)",
            # 見立て納得 / 納得の本命
            _VIEW_WORDS + r"(?:に|は|も)?納得",
            r"納得の" + _VIEW_WORDS,
            r"意見が合|同意見|同感|同じく",
        ]
    )
)

# 自分(Rakuba)が過去にその馬をどう見ていたかを述べる表現。Claudeには予測データを渡して
# いないので、「期待してた」「注目してました」は対象がどの馬であっても根拠が無い
# （2026-10-13、札幌記念で14番手評価だった『ローシャムパーク』について「期待してただけに」と
# 書いた実例。事実文の馬と同じ馬についてでも、×評価の馬に「気になってました」と書いた
# 実例がある）。評価は事実文が正確に述べるので、反応文では馬を問わず案ごと破棄する。
# 「注目してます」「期待してしまいます」のような現在形・これからの関心は対象外。
_PAST = r"(?:た(?!い)|まし)"
SELF_STANCE_CLAIM_RE = re.compile(
    "|".join(
        [
            # 期待してた / 注目してました / 評価していた
            r"(?:期待|注目|警戒|マーク|重視|応援|評価|本命視)して(?:い|お)?" + _PAST,
            # 推してた / 狙ってました / 買っていた
            r"(?:推し|狙っ|買っ|追っ)て(?:い)?" + _PAST,
            r"気になって(?:い)?" + _PAST,
            # 高く見てた / 高く評価していました
            r"高く(?:見|買っ|評価し)て(?:い)?" + _PAST,
            # 本命だった / 期待でした
            r"(?:期待|注目|本命|狙い|推し)(?:だった|でした)",
        ]
    )
)

# 相手が書いた通算の数字(回収率・的中率など)を称賛する表現。自己申告の成績は検証できず、
# 褒めると有料予想屋を持ち上げない原則(design_decisions.md 2節)にも反する（2026-10-13、
# 単勝回収率の自己申告に「この数字は立派だと思います」と書いた実例）。数字を指す語と
# 称賛語が近くに並ぶ場合だけを破棄する。的中実績一般の称賛は、レース名の分かる個別の
# 的中へのお祝いまで落としてしまうので破棄には入れず、プロンプトでの禁止にとどめる。
_NUMBER_WORDS = r"(?:的中率|回収率|勝率|複勝率|収支|数字)"
_PRAISE_WORDS = (
    r"(?:すご|凄|立派|えげつな|見事|さすが|流石|素晴らし|優秀|安定|見習|圧巻|脱帽|驚異|羨まし)"
)
NUMBER_PRAISE_RE = re.compile(
    _NUMBER_WORDS + r"[^。！!？?]{0,15}" + _PRAISE_WORDS
    + "|" + _PRAISE_WORDS + r"[^。！!？?、]{0,4}" + _NUMBER_WORDS
)

# 反応文に適用する破棄フィルタ。事実文(fact_sentence)には適用しない
# (事実文は「…1番手評価としていて」のように評価そのものを述べるのが役目)。
# 名前は実行ログに出す（どのフィルタで破棄が多いかを後から数えるため）。
_DISCARD_FILTERS = (
    ("数字・印", NUMERIC_MARK_RE),
    ("一致の主張", AGREEMENT_CLAIM_RE),
    ("自分の評価", SELF_STANCE_CLAIM_RE),
    ("数字の称賛", NUMBER_PRAISE_RE),
)
_DISCARD_RES = tuple(regex for _name, regex in _DISCARD_FILTERS)
LOG_REACTION_HEAD_LEN = 20  # ログ・DBに残す反応文の先頭文字数（全文は長いので残さない）


def _discard_reasons(reaction: str) -> list[str]:
    """反応文が該当した破棄フィルタの名前（複数に該当すれば全部）。"""
    return [name for name, regex in _DISCARD_FILTERS if regex.search(reaction)]


def _describe_discard(reaction: str) -> str:
    """「自分の評価・数字の称賛「ずっと期待してたので嬉しいです、この的中…」」。"""
    head = reaction[:LOG_REACTION_HEAD_LEN] + ("…" if len(reaction) > LOG_REACTION_HEAD_LEN else "")
    return f"{'・'.join(_discard_reasons(reaction))}「{head}」"


def _reaction_is_acceptable(reaction: str) -> bool:
    return bool(reaction) and not any(regex.search(reaction) for regex in _DISCARD_RES)


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
- 候補の投稿はキーワード検索で見つかった第三者の独立した投稿であり、
  Rakuba(@rakuba_ai)宛のメンションやRakuba自身への言及は構造上あり得ない。
  投稿内の応援・感謝・二人称表現は、その投稿が話題にしている対象
  （馬・騎手・引用元の記事等）に向けたものであり、絶対にRakuba自身に
  向けられたものとして解釈しないこと
- 【厳守】相手の予想・印・本命とRakubaの予想が「近い」「同じ」「一致」「被る」
  「違う」「納得」など、一致・不一致を主張する表現は一切書かないこと
  （例: 「自分も近い見立てでした」「同じ本命です」「こちらとは見立てが違いますが」
  「その印は納得です」）。あなたにはRakubaの予想内容が渡されておらず比較できない
  ので、そうした主張は事実と食い違う恐れがある。Rakubaの評価はシステム側が
  別途正確に付加する
- 【厳守】自分(Rakuba)がどの馬をどう見ていたかは一切書かないこと。「期待してた」
  「注目してました」「推してた」「狙ってた」「高く評価していた」「気になってた馬」
  のような、過去の期待・注目・評価を述べる表現は、どの馬についても使わない。
  あなたにはRakubaの予想内容が渡されていないので、事実と逆になる恐れがある
  （例: 低く評価していた馬に「期待してただけに」と書いてしまう）。どの馬をどう評価
  していたかは、システム側が別途正確に付加する
- 【厳守】相手が書いた通算・連続・自己申告の成績（回収率・的中率・的中続き・
  的中実績・収支などの数字）は、称賛も評価もしないこと（例: 「この数字は立派」
  「すごい回収率」「この的中率は見習いたい」「的中続きですごい」）。こちらでは
  検証できない。称賛してよいのは、レース名が分かる個別の的中だけ
  （例: 「小倉記念の的中お見事です」）。それ以外は、分析の視点や内容に反応する
- 必ず次のJSON形式のみで出力する: {"reactions": ["案1", "案2"]}
"""

# カテゴリ別のトーン指示。fact_sentence自体はprediction_data側で確定済みの
# 事実文だが、Claudeが書く「反応文」のトーンをカテゴリに応じて変える。
_CATEGORY_TONE = {
    "的中系": "少し誇らしい気持ちは込めつつも、控えめなトーンで。",
    "見込み違い系": (
        "今回の予測が外れたことだけを、言い訳せず素直に認めるトーンで。"
        "どの馬をどう評価していたかは書かないこと。"
    ),
    "完敗系": "評価を覆されたことを、卑屈にならず謙虚に認めるトーンで。",
    "順当系": "淡々とした、謙虚なトーンで。",
    "中立系": "淡々としたトーンで。予測が当たったとも外れたとも受け取れる言い方はしないこと。",
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


@dataclass(frozen=True)
class _Discard:
    variant: int   # 応答の何案目か(1始まり)
    reaction: str  # 破棄した反応文（全文。ログ・DBには先頭だけ出す）


@dataclass(frozen=True)
class _CallResult:
    reactions: list[str]      # フィルタを通過した案
    discarded: tuple[_Discard, ...] = ()  # 破棄した案
    problem: str | None = None  # 案を1つも読み取れなかった理由: "empty" / "unparsable"
    stop_reason: str | None = None


def _call_claude(
    candidate: Candidate, reaction_budget: int, model: str, category: str | None
) -> _CallResult:
    """Claudeを1回呼び、反応文の案を返す。呼び出し側で_client()がNoneでないことを確認すること。"""
    resp = _client().messages.create(
        model=model,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": _build_user_prompt(candidate, reaction_budget, category),
            }
        ],
    )
    stop_reason = getattr(resp, "stop_reason", None)
    raw_text = "".join(
        block.text for block in resp.content if getattr(block, "type", "") == "text"
    )
    if not raw_text.strip():
        return _CallResult([], problem="empty", stop_reason=stop_reason)
    try:
        start = raw_text.index("{")
        end = raw_text.rindex("}") + 1
        parsed = json.loads(raw_text[start:end])
        reactions = [str(r).strip() for r in parsed.get("reactions", [])]
    except (ValueError, json.JSONDecodeError, AttributeError):
        return _CallResult([], problem="unparsable", stop_reason=stop_reason)

    # 多重防御: 数字・マーク、相手の予想との一致・不一致の主張、自分の過去の評価の主張、
    # 相手の数字の称賛が混入した案は、文字を削って繋げるのではなく案ごと破棄する
    # （不自然な文になることを避け、確定していない事実が紛れ込む余地を構造的に無くす）。
    numbered = [(i, r) for i, r in enumerate(reactions, start=1) if r]
    kept = [r for _i, r in numbered if _reaction_is_acceptable(r)]
    discarded = tuple(_Discard(i, r) for i, r in numbered if not _reaction_is_acceptable(r))
    return _CallResult(kept, discarded=discarded, stop_reason=stop_reason)


def _report(log: list[str] | None, line: str) -> None:
    """生成時の異常を残す。serveはコンソールの無いpythonwで動くので、loggingだけでなく
    呼び出し元が表示するログ行(orchestrator.run_draft()の戻り値)にも積む。"""
    logger.warning(line)
    if log is not None:
        log.append(line)


def _record_discard(conn, candidate_id: str, attempt: int, discard: _Discard) -> None:
    """破棄した案をDB(discarded_reactions)に残す。ログ行はpythonwのserveでは捨てられるため。
    記録は集計用の副産物なので、書き込みに失敗しても下書き生成は止めずwarningだけ残す。"""
    if conn is None:
        return
    try:
        db.save_discarded_reaction(
            conn, candidate_id, attempt, discard.variant,
            _discard_reasons(discard.reaction), discard.reaction[:LOG_REACTION_HEAD_LEN],
        )
    except Exception as e:
        logger.warning(f"破棄の記録に失敗: {candidate_id} {attempt}回目 {discard.variant}案目 {e!r}")


def generate_drafts(
    candidate: Candidate,
    match: PredictionMatch,
    model: str = "claude-sonnet-5",
    log: list[str] | None = None,
    conn=None,
) -> list[str]:
    """matchはNoneであってはならない（呼び出し側でNoneならこの関数自体を呼ばないこと）。

    2案そろわなければ1回だけ再試行し、それでも足りない分は原因別のプレースホルダで埋める。
    空応答・破棄があった試行は、何回目か・stop_reasonとともにlogへ追記する。
    connを渡すと、破棄した案をdiscarded_reactionsにも記録する。
    """
    if _client() is None:
        return [PLACEHOLDER, PLACEHOLDER]

    reaction_budget = _reaction_budget(match.fact_sentence)
    who = f"@{candidate.author_screen_name}"
    reactions: list[str] = []
    discarded = 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = _call_claude(candidate, reaction_budget, model, match.category)
        if result.problem == "empty":
            _report(log, f"空応答: {who} {attempt}回目 stop_reason={result.stop_reason}")
        elif result.problem == "unparsable":
            _report(
                log, f"解析できない応答: {who} {attempt}回目 stop_reason={result.stop_reason}"
            )
        for discard in result.discarded:
            _report(log, f"フィルタで破棄: {who} {attempt}回目 {_describe_discard(discard.reaction)}")
            _record_discard(conn, candidate.id, attempt, discard)
        discarded += len(result.discarded)
        for r in result.reactions:
            if r not in reactions:
                reactions.append(r)
        if len(reactions) >= 2:
            break

    if len(reactions) < 2:
        filler = PLACEHOLDER_FILTERED if discarded else PLACEHOLDER_EMPTY
        _report(log, f"下書き未生成: {who} {2 - len(reactions)}案 {filler}")
        reactions += [filler] * (2 - len(reactions))
    reactions = reactions[:2]

    drafts = []
    for r in reactions:
        if r in PLACEHOLDERS:
            drafts.append(r)
        else:
            combined = f"{r[:reaction_budget]} {match.fact_sentence}"
            drafts.append(combined[:MAX_DRAFT_LEN])
    return drafts

def draft_top_candidates(
    account: Account, conn, cfg: ScoutConfig, log: list[str] | None = None
) -> int:
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
            ranked.candidate.text, predictions, results, confirmed,
            created_at=ranked.candidate.created_at,
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
        drafts = generate_drafts(
            ranked.candidate, match, model=cfg.claude_model, log=log, conn=conn
        )
        db.save_drafts(conn, ranked.candidate.id, drafts, predictions_hash=predictions_hash)
        count += 1
    return count
