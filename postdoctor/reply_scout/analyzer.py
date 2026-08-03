"""分析層: リプライ候補のスコアリング・ランキング。

db 層が返す Candidate のリストのみを入力とし、DB や API には触れない
（完全ローカル・API消費ゼロ）。既知馬名の集合（horse_names）は
prediction_data.all_horse_names() から呼び出し側が渡す — このファイル自体は
Rakubaの予測データの形式を一切知らない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from postdoctor.reply_scout.db import Candidate
from postdoctor.reply_scout.fetcher import ScoutConfig

JST = timezone(timedelta(hours=9))

_URL_RE = re.compile(r"https?://\S+")
_NON_WORD_RE = re.compile(r"[\s　\U0001F000-\U0001FFFF☀-➿]+")
MIN_QUOTE_COMMENT_LEN = 10  # これ未満なら引用主自身の実質コメントが無い「無言引用RT」とみなす


def _is_low_content_quote(candidate: Candidate) -> bool:
    """引用RTで、引用主自身の実質コメントがほぼ無い「無言乗っかりRT」かどうか。

    X API v2の引用RTのtextは引用主が自分で書いた文言のみ(元ツイート本文は含まない)。
    そこからt.coリンクと絵文字・空白を除いた残りが短すぎれば、引用主自身の意見・分析が
    無い投稿と判断する（バズった元ネタに乗っかっているだけでリプライの起点になる発言が無い）。
    """
    if not candidate.is_quote:
        return False
    stripped = _NON_WORD_RE.sub("", _URL_RE.sub("", candidate.text))
    return len(stripped) < MIN_QUOTE_COMMENT_LEN


@dataclass(frozen=True)
class ScoreBreakdown:
    velocity_norm: float
    follower_norm: float
    specificity: float
    trusted: float
    analytical: float
    ng_penalty_applied: bool
    final_score: float


def _velocity(candidate: Candidate, now: datetime) -> float:
    created = datetime.fromisoformat(candidate.created_at)
    hours = max((now - created).total_seconds() / 3600, 0.5)
    return candidate.likes / hours


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms) if terms else False


MIN_NAME_LENGTH = 2  # これ未満の馬名・レース名は誤マッチしやすいため具体性判定に使わない


def _specificity(
    candidate: Candidate, horse_names: frozenset[str], race_names: frozenset[str]
) -> float:
    """実在の馬名またはレース名を名指ししているかどうか（Rakubaの実データ基準）。

    config側の固定語彙リストではなく、prediction_data.all_horse_names()/
    all_race_names() が返す実データを直接参照する。馬名・レース名は列挙可能な
    固有名詞集合であり、静的な語彙リストで持つと陳腐化するため。
    analytical（馬名+分析語の共起）とは独立の判定で、馬名だけの言及でも1.0になる
    （考察語が伴えばanalyticalも別途加点される）。
    """
    text = candidate.text
    names = (n for n in (*horse_names, *race_names) if len(n) >= MIN_NAME_LENGTH)
    return 1.0 if any(name in text for name in names) else 0.0


def _topic_key(candidate: Candidate, horse_names: frozenset[str]) -> str | None:
    """候補本文が言及している既知の馬名を1つ返す（同一トピックの重複排除キー）。

    複数の馬名が該当する場合は最長一致（短い馬名ほど誤マッチしやすいため）を採用する。
    該当が無ければNone（トピック不明。この場合は重複排除の対象外とする — 無理に
    「トピック無し」同士をグルーピングすると無関係な一般投稿まで過剰に間引いてしまうため）。
    レース名ではなく馬名を軸にするのは、prediction_data.find_match()自体が馬名一致のみを
    採用する（レース名だけの一致からの本命採用フォールバックは廃止済み）方針と揃えるため。
    """
    matches = [n for n in horse_names if len(n) >= MIN_NAME_LENGTH and n in candidate.text]
    if not matches:
        return None
    return max(matches, key=len)


def _is_ng(candidate: Candidate, ng_words: list[str]) -> bool:
    return _contains_any(candidate.text, ng_words)


def _is_solicitation(candidate: Candidate, solicitation_words: list[str]) -> bool:
    """有料級・限定情報の売り込みなど、そもそもリプライ対象にすべきでない投稿の判定。

    ng_words（煽り系、スコア半減のみ）とは別の、ハード除外用の語彙リスト
    （config/ng_words.jsonのsolicitation_words）。「いいねで」等のエンゲージ
    ボーナス狙いの煽り文句は内容自体は許容できる場合もあるため半減に留める一方、
    「限定」「有料」「教える」等の情報商材・有料予想の売り込みは常に除外する。
    """
    return _contains_any(candidate.text, solicitation_words)


def _is_trusted(candidate: Candidate, trusted_authors: list[str]) -> bool:
    return candidate.author_screen_name in trusted_authors if trusted_authors else False


def _has_analytical_signal(
    candidate: Candidate, horse_names: frozenset[str], analysis_terms: list[str]
) -> bool:
    """馬名と分析語（斤量/適性/ラップ/血統/ローテ等）の「共起」のみを評価する。

    分析語だけの一般論トーク（馬名への言及がない）はボーナス対象外。
    """
    if not analysis_terms or not horse_names:
        return False
    text = candidate.text
    return _contains_any(text, analysis_terms) and any(name in text for name in horse_names)


def preliminary_score(
    candidates: list[Candidate],
    cfg: ScoutConfig,
    horse_names: frozenset[str] = frozenset(),
    race_names: frozenset[str] = frozenset(),
) -> list[tuple[Candidate, float]]:
    """フォロワー数取得前の一次スコアリング。上位候補を絞り込むために使う。

    エンゲージメント速度だけで決めると、内容が薄い「バズっただけ」の投稿に
    偏るため、具体的なレース名・馬名を含む情報系投稿や、馬名+分析語の共起を
    含む考察系投稿を優遇し、config/ng_words.jsonの煽り系ワードを含む投稿は
    割り引く。solicitation_words（有料予想の売り込み等）や、引用主自身の
    実質コメントが無い無言引用RT（_is_low_content_quote）に該当する投稿は
    フォロワー数取得の対象にする価値もないため、この時点で除外する。
    """
    candidates = [
        c for c in candidates
        if not _is_solicitation(c, cfg.solicitation_words) and not _is_low_content_quote(c)
    ]
    if not candidates:
        return []
    now = datetime.now(JST)
    velocities = [_velocity(c, now) for c in candidates]
    vmax = max(velocities) or 1.0

    scored = []
    for c, v in zip(candidates, velocities):
        v_norm = v / vmax
        specificity = _specificity(c, horse_names, race_names)
        analytical = 1.0 if _has_analytical_signal(c, horse_names, cfg.analysis_terms) else 0.0
        score = v_norm * 0.4 + specificity * 0.3 + analytical * 0.3
        if _is_ng(c, cfg.ng_words):
            score *= 0.5
        scored.append((c, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def final_ranking(
    candidates: list[Candidate],
    cfg: ScoutConfig,
    horse_names: frozenset[str] = frozenset(),
    race_names: frozenset[str] = frozenset(),
    top_n: int = 10,
) -> list[tuple[Candidate, float, int, ScoreBreakdown]]:
    """フォロワー数を含めた最終スコアリングでTOP N を選ぶ。

    スコア = エンゲージメント速度0.30 + フォロワー数0.10 + トピック具体性0.25
             + 信頼できる投稿者(公式・メディア等)0.15 + 考察性(馬名+分析語の共起)0.20
    さらに、config/ng_words.jsonの煽り系ワードを含む投稿は最終スコアを半減させる。
    単純ないいね数の多さや煽り文句だけで上位に来ないようにするため。
    solicitation_words（有料予想の売り込み等）や、引用主自身の実質コメントが無い
    無言引用RT（_is_low_content_quote）に該当する投稿はスコアに関わらずランキングから
    完全に除外する（半減では上位に残り得るため、ハード除外）。

    top_n選出時、同一著者(author_id)の投稿が複数の枠を占めないよう著者単位で
    重複排除する（1著者につき最上位1件のみ採用）。加えて、著者が別人でも
    同じ馬(_topic_key)について書かれた投稿は同様に1件のみ採用する（別々の投稿者に
    ほぼ同一のfact_sentenceを送ることになり、話題の偏りと不自然さの両方を招くため）。
    除外分はどちらの場合も次点の別著者/別トピックの候補で埋め、top_nのスロット数
    自体は減らさない。トピック不明（既知の馬名が本文に無い）候補はトピック単位の
    重複排除の対象外。

    各候補のスコア内訳(ScoreBreakdown)も併せて返す
    （db.save_score_factors()での実運用ログ用）。
    """
    candidates = [
        c for c in candidates
        if not _is_solicitation(c, cfg.solicitation_words) and not _is_low_content_quote(c)
    ]
    if not candidates:
        return []
    now = datetime.now(JST)
    velocities = [_velocity(c, now) for c in candidates]
    vmax = max(velocities) or 1.0
    followers = [c.author_followers or 0 for c in candidates]
    fmax = max(followers) or 1

    scored = []
    for c, v, f in zip(candidates, velocities, followers):
        v_norm = v / vmax
        f_norm = f / fmax
        specificity = _specificity(c, horse_names, race_names)
        trusted = 1.0 if _is_trusted(c, cfg.trusted_authors) else 0.0
        analytical = 1.0 if _has_analytical_signal(c, horse_names, cfg.analysis_terms) else 0.0
        score = v_norm * 0.30 + f_norm * 0.10 + specificity * 0.25 + trusted * 0.15 + analytical * 0.20
        ng_hit = _is_ng(c, cfg.ng_words)
        if ng_hit:
            score *= 0.5
        breakdown = ScoreBreakdown(
            velocity_norm=v_norm, follower_norm=f_norm, specificity=specificity,
            trusted=trusted, analytical=analytical, ng_penalty_applied=ng_hit,
            final_score=score,
        )
        scored.append((c, score, breakdown))
    scored.sort(key=lambda triple: triple[1], reverse=True)

    top = []
    seen_authors: set[str] = set()
    seen_topics: set[str] = set()
    for c, score, breakdown in scored:
        if c.author_id in seen_authors:
            continue
        topic = _topic_key(c, horse_names)
        if topic is not None and topic in seen_topics:
            continue
        seen_authors.add(c.author_id)
        if topic is not None:
            seen_topics.add(topic)
        top.append((c, score, breakdown))
        if len(top) >= top_n:
            break
    return [
        (c, score, rank, breakdown) for rank, (c, score, breakdown) in enumerate(top, start=1)
    ]
