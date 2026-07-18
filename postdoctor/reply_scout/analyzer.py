"""分析層: リプライ候補のスコアリング・ランキング。

db 層が返す Candidate のリストのみを入力とし、DB や API には触れない
（完全ローカル・API消費ゼロ）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from postdoctor.reply_scout.db import Candidate
from postdoctor.reply_scout.fetcher import ScoutConfig

JST = timezone(timedelta(hours=9))


def _velocity(candidate: Candidate, now: datetime) -> float:
    created = datetime.fromisoformat(candidate.created_at)
    hours = max((now - created).total_seconds() / 3600, 0.5)
    return candidate.likes / hours


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms) if terms else False


def _specificity(candidate: Candidate, specific_terms: list[str]) -> float:
    return 1.0 if _contains_any(candidate.text, specific_terms) else 0.0


def _is_hype(candidate: Candidate, hype_terms: list[str]) -> bool:
    return _contains_any(candidate.text, hype_terms)


def _is_trusted(candidate: Candidate, trusted_authors: list[str]) -> bool:
    return candidate.author_screen_name in trusted_authors if trusted_authors else False


def preliminary_score(
    candidates: list[Candidate], cfg: ScoutConfig
) -> list[tuple[Candidate, float]]:
    """フォロワー数取得前の一次スコアリング。上位候補を絞り込むために使う。

    エンゲージメント速度だけで決めると、内容が薄い「バズっただけ」の投稿に
    偏るため、具体的なレース名・馬名を含む情報系投稿を優遇し、
    「限定」「教える」等の煽り系ワードを含む投稿は割り引く。
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
        score = v_norm * 0.5 + specificity * 0.5
        if _is_hype(c, cfg.hype_terms):
            score *= 0.5
        scored.append((c, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def final_ranking(
    candidates: list[Candidate], cfg: ScoutConfig, top_n: int = 10
) -> list[tuple[Candidate, float, int]]:
    """フォロワー数を含めた最終スコアリングでTOP N を選ぶ。

    スコア = エンゲージメント速度0.35 + フォロワー数0.15 + トピック具体性0.30
             + 信頼できる投稿者(公式・メディア等)0.20
    さらに、煽り系ワード（config/keywords.jsonのhype_terms）を含む投稿は
    最終スコアを半減させる。単純ないいね数の多さだけで上位に来ないようにするため。
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
        score = v_norm * 0.35 + f_norm * 0.15 + specificity * 0.30 + trusted * 0.20
        if _is_hype(c, cfg.hype_terms):
            score *= 0.5
        scored.append((c, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)

    top = scored[:top_n]
    return [(c, score, rank) for rank, (c, score) in enumerate(top, start=1)]
