"""オーケストレーション層: scout/draft/track の一連処理をまとめる。

CLI (cli.py) とダッシュボードサーバー (server.py) の両方から呼ばれる。
ログ文字列のリストを返すだけで、出力先（コンソールprint or JSON応答）は
呼び出し側に委ねる。fetcher/db/analyzer/prescriber/prediction_data の詳細
（SQL・API呼び出し・Rakuba出力ファイルの形式等）はここでは知らず、
各層の公開関数のみを呼ぶ。
"""

from __future__ import annotations

import os

from datetime import datetime, timedelta, timezone

from postdoctor.config import Account
from postdoctor.reply_scout import analyzer, db, fetcher, prediction_data, prescriber

REPLY_TRACKING_WINDOW_DAYS = 14


def run_scout(account: Account) -> list[str]:
    cfg = fetcher.load_config()
    pcfg = prediction_data.load_prediction_config(cfg.rakuba_output_dir)
    prediction_rows = prediction_data.load_all_predictions(pcfg)
    horse_names = frozenset(prediction_data.all_horse_names(prediction_rows))
    race_names = frozenset(prediction_data.all_race_names(prediction_rows))

    with db.connect(account) as conn:
        candidates, read_count = fetcher.collect_candidates(account, conn, cfg)
        inserted = db.insert_candidates(conn, candidates) if candidates else 0
        if read_count:
            db.add_usage(conn, "tweet_read", read_count)

        pending = db.load_scoring_candidates(conn)
        prelim = analyzer.preliminary_score(pending, cfg, horse_names, race_names)
        top_author_ids = list({c.author_id for c, _ in prelim[: cfg.top_user_lookup_limit]})
        followers = fetcher.enrich_followers(account, top_author_ids) if top_author_ids else {}
        if followers:
            db.update_followers(conn, followers)
            db.add_usage(conn, "user_read", len(followers))

        pending = db.load_scoring_candidates(conn)
        ranked = analyzer.final_ranking(pending, cfg, horse_names, race_names, top_n=10)
        db.set_ranking(conn, [(c.id, score, rank) for c, score, rank, _breakdown in ranked])
        for c, _score, _rank, breakdown in ranked:
            db.save_score_factors(
                conn, c.id,
                breakdown.velocity_norm, breakdown.follower_norm, breakdown.specificity,
                breakdown.trusted, breakdown.analytical, breakdown.ng_penalty_applied,
                breakdown.final_score,
            )

    tweet_cost = read_count * fetcher.TWEET_READ_COST
    user_cost = len(followers) * fetcher.USER_READ_COST
    return [
        f"新規候補 {inserted}件を保存しました。",
        f"ツイート読み取り {read_count}件（概算${tweet_cost:.3f}） + "
        f"フォロワー取得 {len(followers)}件（概算${user_cost:.3f}）。",
        f"TOP{len(ranked)}件を選定しました。",
    ]


def run_draft(account: Account) -> list[str]:
    cfg = fetcher.load_config()
    with db.connect(account) as conn:
        count = prescriber.draft_top_candidates(account, conn, cfg)
    mode = "本実装" if os.environ.get("ANTHROPIC_API_KEY") else "モック（プレースホルダ）"
    return [f"{count}件の候補に下書きを生成しました（{mode}）。"]


def run_scout_and_draft(account: Account) -> list[str]:
    return run_scout(account) + run_draft(account)


def run_track(account: Account) -> list[str]:
    cfg = fetcher.load_config()
    log: list[str] = []
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
        log.append(f"{len(metrics)}件の送信済みリプライを更新しました（概算${cost:.3f}）。")

        cutoff = (datetime.now(timezone.utc) - timedelta(days=REPLY_TRACKING_WINDOW_DAYS)).isoformat(
            timespec="seconds"
        )
        targets = db.get_trackable_sent_replies(conn, cutoff)
        if not targets:
            log.append("返信検知: 直近14日以内の送信済みリプライがないため対象外です。")
        else:
            # 返信検知の検索読み取りはscoutの候補収集(tweet_read)と同じ
            # daily_read_limit予算を共有する（同じ有料エンドポイントのため）。
            already_read_today = db.get_today_usage(conn, "tweet_read")
            remaining = max(cfg.daily_read_limit - already_read_today, 0)
            if remaining <= 0:
                log.append(
                    f"返信検知: 本日の検索読み取り上限（{cfg.daily_read_limit}件）に"
                    "達しているためスキップしました。"
                )
            else:
                responses, read_count = fetcher.fetch_reply_responses(
                    account, targets, max_read=remaining
                )
                inserted = db.save_reply_responses(conn, responses) if responses else 0
                if read_count:
                    db.add_usage(conn, "tweet_read", read_count)
                search_cost = read_count * fetcher.TWEET_READ_COST
                log.append(
                    f"返信検知: 新規{inserted}件の返信を検出しました"
                    f"（対象{len(targets)}件・読み取り{read_count}件・概算${search_cost:.3f}）。"
                )

    return log


def export_score_review(account: Account) -> str:
    """score_factors + sent_replies + candidates を突き合わせたCSVを書き出す。

    スコアリング要素と実際の反応(いいね・インプレッション等)を人力で見比べ、
    重み付けを見直すための軽量な分析用出力。ダッシュボード連携はしない。
    """
    import csv

    with db.connect(account) as conn:
        rows = db.get_score_review_rows(conn)

    out_path = account.data_dir / "score_review.csv"
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        else:
            f.write("")
    return str(out_path)


SCORE_FACTOR_COLUMNS = ["velocity_norm", "follower_norm", "specificity", "trusted", "analytical"]
CORRELATION_REVIEW_THRESHOLD = 30


def summarize_score_correlations(account: Account) -> list[str]:
    """送信済み・追跡済みの候補について、各スコア要素とインプレッションの
    Spearman順位相関を計算する。follower重み等の配点の妥当性を実測ベースで
    検証するための軽量チェック（scipy未導入のため pandas の
    Series.rank().corr()で順位相関を代用する）。

    分散ゼロの列（現状はconfig未投入で発生しがちなspecificity/trusted等）は
    順位相関が定義できないため計算せず、その旨を明記する（NaN表示を避ける）。
    """
    import pandas as pd

    with db.connect(account) as conn:
        rows = db.get_score_review_rows(conn)

    df = pd.DataFrame(rows)
    sent = (
        df[df["sent_at"].notna() & df["impressions"].notna()]
        if not df.empty
        else df
    )

    lines = [f"スコア妥当性チェック: 送信済み・追跡済み {len(sent)}件を対象に相関を計算しました。"]
    if len(sent) < 2:
        lines.append("対象件数が少なすぎるため相関は計算できません。")
        return lines

    for col in SCORE_FACTOR_COLUMNS:
        if sent[col].nunique(dropna=True) <= 1:
            lines.append(f"  {col}: 分散なし（判定不可）")
            continue
        corr = sent[col].rank().corr(sent["impressions"].rank())
        lines.append(f"  {col}: Spearman相関 {corr:.3f}")

    if len(sent) > CORRELATION_REVIEW_THRESHOLD:
        lines.append(
            f"送信済み・追跡済みが{CORRELATION_REVIEW_THRESHOLD}件を超えました。"
            "フォロワー項の重み再検証をおすすめします。"
        )
    return lines
