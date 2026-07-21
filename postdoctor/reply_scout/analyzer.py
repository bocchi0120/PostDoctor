"""分析層: リプライ候補のスコアリング・ランキング。

db 層が返す Candidate のリストのみを入力とし、DB や API には触れない
（完全ローカル・API消費ゼロ）。既知馬名の集合（horse_names）は
prediction_data.all_horse_names() から呼び出し側が渡す — このファイル自体は
Rakubaの予測データの形式を一切知らない。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from postdoctor.reply_scout.db import Candidate
from postdoctor.reply_scout.fetcher import ScoutConfig

JST = timezone(timedelta(hours=9))


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


def _specificity(candidate: Candidate, specific_terms: list[str]) -> float:
    return 1.0 if _contains_any(candidate.text, specific_terms) else 0.0


def _is_ng(candidate: Candidate, ng_words: list[str]) -> bool:
    return _contains_any(candidate.text, ng_words)


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
    candidates: list[Candidate], cfg: ScoutConfig, horse_names: frozenset[str] = frozenset()
) -> list[tuple[Candidate, float]]:
    """フォロワー数取得前の一次スコアリング。上位候補を絞り込むために使う。

    エンゲージメント速度だけで決めると、内容が薄い「バズっただけ」の投稿に
    偏るため、具体的なレース名・馬名を含む情報系投稿や、馬名+分析語の共起を
    含む考察系投稿を優遇し、config/ng_words.jsonの煽り系ワードを含む投稿は
    割り引く。
    """
    if not candidates:
        return []
    now = datetime.now(JST)
    velocities = [_velocity(c, now) for c in candidates]
    vmax = max(velocities) or 1.0

    scored = []
    for c, v in zip(candidates, velocities):
        v_norm = v / vmax
        specificity = _specificity(c, cfg.specific_terms)
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
    top_n: int = 10,
) -> list[tuple[Candidate, float, int, ScoreBreakdown]]:
    """フォロワー数を含めた最終スコアリングでTOP N を選ぶ。

    スコア = エンゲージメント速度0.30 + フォロワー数0.10 + トピック具体性0.25
             + 信頼できる投稿者(公式・メディア等)0.15 + 考察性(馬名+分析語の共起)0.20
    さらに、config/ng_words.jsonの煽り系ワードを含む投稿は最終スコアを半減させる。
    単純ないいね数の多さや煽り文句だけで上位に来ないようにするため。

    各候補のスコア内訳(ScoreBreakdown)も併せて返す
    （db.save_score_factors()での実運用ログ用）。
    """
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
        specificity = _specificity(c, cfg.specific_terms)
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

    top = scored[:top_n]
    return [
        (c, score, rank, breakdown) for rank, (c, score, breakdown) in enumerate(top, start=1)
    ]
