"""PostDoctor CLI エントリポイント。

使い方:
  python main.py fetch     --account rakuba_ai [--all]
  python main.py analyze   --account rakuba_ai [--all] [--metric impressions]
  python main.py prescribe --account rakuba_ai [--all]
  python main.py run       --account rakuba_ai [--all]   # fetch -> analyze(全指標) -> prescribe
  python main.py serve     --account rakuba_ai [--port 8765]  # ダッシュボードをブラウザで配信、更新ボタン付き

  python main.py reply-scout scout  --account rakuba_ai                     # 候補収集+スコアリング
  python main.py reply-scout draft  --account rakuba_ai [--predictions ...] # リプライ下書き生成
  python main.py reply-scout run    --account rakuba_ai [--predictions ...] # scout -> draft
  python main.py reply-scout track  --account rakuba_ai                     # 送信済みリプライの反応追跡
  python main.py reply-scout status --account rakuba_ai --id <tweet_id> --status 送信済み|見送り [--reply-id ...]
"""

from __future__ import annotations

import argparse

from postdoctor.analysis.heatmap import METRICS, add_derived_columns, make_heatmap
from postdoctor.config import Account, get_account, load_accounts
from postdoctor.dashboard.generator import generate_dashboard
from postdoctor.fetcher.fetch import fetch_new_posts
from postdoctor.prescription.generator import generate_prescription
from postdoctor.reply_scout import db as rs_db
from postdoctor.reply_scout import orchestrator as rs_orchestrator
from postdoctor.storage.db import connect, get_since_id, load_posts_df, set_since_id, upsert_posts


def _resolve_accounts(args: argparse.Namespace) -> list[Account]:
    accounts = load_accounts()
    if args.all:
        if not accounts:
            raise SystemExit("config/accounts.yaml にアカウントが設定されていません。")
        return list(accounts.values())
    if not args.account:
        raise SystemExit("--account <名前> または --all を指定してください。")
    if args.account not in accounts:
        available = ", ".join(accounts) or "(なし)"
        raise SystemExit(f"アカウント '{args.account}' は未設定です。設定済み: {available}")
    return [accounts[args.account]]


def cmd_fetch(account: Account) -> None:
    with connect(account) as conn:
        since_id = get_since_id(conn)
        print(f"[{account.name}] 差分取得を開始 (since_id={since_id}) ...")
        result = fetch_new_posts(account, since_id)
        if not result.posts:
            print(f"[{account.name}] 新規投稿はありません。読み取りコスト最小で終了。")
            return
        count = upsert_posts(conn, result.posts)
        if result.newest_id:
            set_since_id(conn, result.newest_id)
        print(f"[{account.name}] {count} 件を保存しました。次回は since_id={result.newest_id} から差分取得します。")


def cmd_analyze(account: Account, metric: str) -> None:
    df = add_derived_columns(load_posts_df(account))
    print(f"[{account.name}] 分析対象: {len(df)} 投稿")
    out = make_heatmap(df, metric, account)
    print(f"[{account.name}] 保存: {out}")


def cmd_prescribe(account: Account) -> None:
    df = add_derived_columns(load_posts_df(account))
    text = generate_prescription(df, account)
    print(text)


def cmd_dashboard(account: Account) -> None:
    df = add_derived_columns(load_posts_df(account))
    text = generate_prescription(df, account)
    out = generate_dashboard(df, account, text)
    print(f"[{account.name}] 保存: {out}")


def cmd_run(account: Account) -> None:
    cmd_fetch(account)
    df = add_derived_columns(load_posts_df(account))
    for metric in METRICS:
        out = make_heatmap(df, metric, account)
        print(f"[{account.name}] 保存: {out}")
    text = generate_prescription(df, account)
    print(text)
    out = generate_dashboard(df, account, text)
    print(f"[{account.name}] 保存: {out}")


def cmd_reply_scout_scout(account: Account) -> None:
    for line in rs_orchestrator.run_scout(account):
        print(f"[{account.name}] reply-scout: {line}")


def cmd_reply_scout_draft(account: Account, predictions_path: str | None) -> None:
    for line in rs_orchestrator.run_draft(account, predictions_path):
        print(f"[{account.name}] reply-scout: {line}")


def cmd_reply_scout_run(account: Account, predictions_path: str | None) -> None:
    for line in rs_orchestrator.run_scout_and_draft(account, predictions_path):
        print(f"[{account.name}] reply-scout: {line}")


def cmd_reply_scout_track(account: Account) -> None:
    for line in rs_orchestrator.run_track(account):
        print(f"[{account.name}] reply-scout track: {line}")


def cmd_reply_scout_status(account: Account, tweet_id: str, status: str, reply_id: str | None) -> None:
    with rs_db.connect(account) as conn:
        rs_db.update_status(conn, tweet_id, status, reply_id)
    print(f"[{account.name}] reply-scout: 候補 {tweet_id} のステータスを '{status}' に更新しました。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="postdoctor")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("fetch", "analyze", "prescribe", "dashboard", "run"):
        p = sub.add_parser(name)
        p.add_argument("--account", help="config/accounts.yaml で定義したアカウント名")
        p.add_argument("--all", action="store_true", help="設定済み全アカウントを対象にする")
        if name == "analyze":
            p.add_argument("--metric", default="impressions", choices=METRICS)

    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--account", required=True, help="config/accounts.yaml で定義したアカウント名")
    serve_p.add_argument("--port", type=int, default=8765)
    serve_p.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")

    reply_scout_p = sub.add_parser("reply-scout")
    rs_sub = reply_scout_p.add_subparsers(dest="rs_action", required=True)

    scout_p = rs_sub.add_parser("scout", help="候補投稿を収集・スコアリングしてTOP10を選定する")
    scout_p.add_argument("--account", required=True)

    draft_p = rs_sub.add_parser("draft", help="TOP10候補にリプライ下書きを2案生成する")
    draft_p.add_argument("--account", required=True)
    draft_p.add_argument("--predictions", default=None, help="Rakuba予測データのJSONパス（省略時は data/<account>/predictions.json）")

    run_p = rs_sub.add_parser("run", help="scout -> draft をまとめて実行する")
    run_p.add_argument("--account", required=True)
    run_p.add_argument("--predictions", default=None)

    track_p = rs_sub.add_parser("track", help="送信済みリプライの反応（いいね等）を追跡する")
    track_p.add_argument("--account", required=True)

    status_p = rs_sub.add_parser("status", help="候補のステータスを更新する")
    status_p.add_argument("--account", required=True)
    status_p.add_argument("--id", dest="tweet_id", required=True, help="対象の投稿ID")
    status_p.add_argument("--status", required=True, choices=["未送信", "送信済み", "見送り"])
    status_p.add_argument("--reply-id", default=None, help="送信済みにする場合、追跡用の自分のリプライID")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        from postdoctor.server import serve

        account = get_account(args.account)
        serve(account, port=args.port, open_browser=not args.no_browser)
        return

    if args.command == "reply-scout":
        account = get_account(args.account)
        if args.rs_action == "scout":
            cmd_reply_scout_scout(account)
        elif args.rs_action == "draft":
            cmd_reply_scout_draft(account, args.predictions)
        elif args.rs_action == "run":
            cmd_reply_scout_run(account, args.predictions)
        elif args.rs_action == "track":
            cmd_reply_scout_track(account)
        elif args.rs_action == "status":
            cmd_reply_scout_status(account, args.tweet_id, args.status, args.reply_id)
        return

    accounts = _resolve_accounts(args)

    for account in accounts:
        if args.command == "fetch":
            cmd_fetch(account)
        elif args.command == "analyze":
            cmd_analyze(account, args.metric)
        elif args.command == "prescribe":
            cmd_prescribe(account)
        elif args.command == "dashboard":
            cmd_dashboard(account)
        elif args.command == "run":
            cmd_run(account)


if __name__ == "__main__":
    main()
