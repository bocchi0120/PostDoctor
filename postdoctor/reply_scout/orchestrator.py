"""オーケストレーション層: scout/draft/track の一連処理をまとめる。

CLI (cli.py) とダッシュボードサーバー (server.py) の両方から呼ばれる。
ログ文字列のリストを返すだけで、出力先（コンソールprint or JSON応答）は
呼び出し側に委ねる。fetcher/db/analyzer/prescriber の詳細（SQL・API呼び出し等）
はここでは知らず、各層の公開関数のみを呼ぶ。
"""

from __future__ import annotations

import os

from postdoctor.config import Account
from postdoctor.reply_scout import analyzer, db, fetcher, prescriber


def run_scout(account: Account) -> list[str]:
    cfg = fetcher.load_config()
    with db.connect(account) as conn:
        candidates, read_count = fetcher.collect_candidates(account, conn, cfg)
        inserted = db.insert_candidates(conn, candidates) if candidates else 0
        if read_count:
            db.add_usage(conn, "tweet_read", read_count)

        pending = db.load_scoring_candidates(conn)
        prelim = analyzer.preliminary_score(pending, cfg)
        top_author_ids = list({c.author_id for c, _ in prelim[: cfg.top_user_lookup_limit]})
        followers = fetcher.enrich_followers(account, top_author_ids) if top_author_ids else {}
        if followers:
            db.update_followers(conn, followers)
            db.add_usage(conn, "user_read", len(followers))

        pending = db.load_scoring_candidates(conn)
        ranked = analyzer.final_ranking(pending, cfg, top_n=10)
        db.set_ranking(conn, [(c.id, score, rank) for c, score, rank in ranked])

    tweet_cost = read_count * fetcher.TWEET_READ_COST
    user_cost = len(followers) * fetcher.USER_READ_COST
    return [
        f"新規候補 {inserted}件を保存しました。",
        f"ツイート読み取り {read_count}件（概算${tweet_cost:.3f}） + "
        f"フォロワー取得 {len(followers)}件（概算${user_cost:.3f}）。",
        f"TOP{len(ranked)}件を選定しました。",
    ]


def run_draft(account: Account, predictions_path: str | None = None) -> list[str]:
    cfg = fetcher.load_config()
    with db.connect(account) as conn:
        count = prescriber.draft_top_candidates(
            account, conn, model=cfg.claude_model, predictions_path=predictions_path
        )
    mode = "本実装" if os.environ.get("ANTHROPIC_API_KEY") else "モック（プレースホルダ）"
    return [f"{count}件の候補に下書きを生成しました（{mode}）。"]


def run_scout_and_draft(account: Account, predictions_path: str | None = None) -> list[str]:
    return run_scout(account) + run_draft(account, predictions_path)


def run_track(account: Account) -> list[str]:
    with db.connect(account) as conn:
        reply_ids = db.get_sent_replies(conn)
        if not reply_ids:
            return ["追跡対象の送信済みリプライはありません。"]
        metrics = fetcher.fetch_own_reply_metrics(account, reply_ids)
        for reply_id, m in metrics.items():
            db.update_sent_reply_metrics(
                conn, reply_id, m["impressions"], m["likes"], m["retweets"], m["replies"]
            )
    cost = len(metrics) * 0.001
    return [f"{len(metrics)}件の送信済みリプライを更新しました（概算${cost:.3f}）。"]
