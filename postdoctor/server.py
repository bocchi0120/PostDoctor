"""ローカルサーバー: ダッシュボードをブラウザで配信し、「更新」ボタンから
パイプライン（fetch -> analyze -> prescribe -> dashboard）を再実行できるようにする。

4層（fetcher/storage/analysis/prescription）と dashboard 層を呼び出すだけの
薄いオーケストレーション層。cli.py の `run` と同じ処理を、ボタン起点で
繰り返し呼べるようにしたもの。

reply_scout（リプライ営業支援）の候補収集・下書き生成・反応追跡も同様に
ボタンから実行できる（/api/reply_scout/refresh, /api/reply_scout/track）。
ただし送信そのものは行わない（自動送信機能は実装しない）。
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from postdoctor.analysis.heatmap import METRICS, add_derived_columns, make_heatmap
from postdoctor.config import Account
from postdoctor.dashboard.generator import generate_dashboard
from postdoctor.fetcher.fetch import fetch_new_posts
from postdoctor.prescription.generator import generate_prescription
from postdoctor.reply_scout import db as rs_db
from postdoctor.reply_scout import orchestrator as rs_orchestrator
from postdoctor.storage.db import connect, get_since_id, load_posts_df, set_since_id, upsert_posts

VALID_REPLY_SCOUT_STATUSES = {"未送信", "送信済み", "見送り"}


def _run_pipeline(account: Account) -> list[str]:
    log: list[str] = []
    with connect(account) as conn:
        since_id = get_since_id(conn)
        result = fetch_new_posts(account, since_id)
        if result.posts:
            count = upsert_posts(conn, result.posts)
            if result.newest_id:
                set_since_id(conn, result.newest_id)
            log.append(f"{count}件の新規投稿を取得しました。")
        else:
            log.append("新規投稿はありませんでした。")

    df = add_derived_columns(load_posts_df(account))
    for metric in METRICS:
        make_heatmap(df, metric, account)
    text = generate_prescription(df, account)
    generate_dashboard(df, account, text)
    log.append(f"分析対象 {len(df)}件でダッシュボードを再生成しました。")
    return log


def _regenerate_dashboard(account: Account) -> None:
    df = add_derived_columns(load_posts_df(account))
    text = generate_prescription(df, account)
    generate_dashboard(df, account, text)


def _run_reply_scout_refresh(account: Account) -> list[str]:
    log = rs_orchestrator.run_scout_and_draft(account)
    _regenerate_dashboard(account)
    return log


def _run_reply_scout_track(account: Account) -> list[str]:
    log = rs_orchestrator.run_track(account)
    _regenerate_dashboard(account)
    return log


def _make_handler(account: Account) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            print(f"[{account.name}] {self.address_string()} - {format % args}")

        def do_GET(self) -> None:
            if self.path not in ("/", ""):
                self.send_response(404)
                self.end_headers()
                return
            body = (account.data_dir / "dashboard.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _write_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self.path == "/api/refresh":
                try:
                    log = _run_pipeline(account)
                    self._write_json(200, {"ok": True, "log": log})
                except Exception as e:
                    self._write_json(500, {"ok": False, "error": str(e)})
                return

            if self.path == "/api/reply_scout/refresh":
                try:
                    log = _run_reply_scout_refresh(account)
                    self._write_json(200, {"ok": True, "log": log})
                except Exception as e:
                    self._write_json(500, {"ok": False, "error": str(e)})
                return

            if self.path == "/api/reply_scout/track":
                try:
                    log = _run_reply_scout_track(account)
                    self._write_json(200, {"ok": True, "log": log})
                except Exception as e:
                    self._write_json(500, {"ok": False, "error": str(e)})
                return

            if self.path == "/api/reply_scout/ack":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    response_id = str(body.get("response_id", "")).strip()
                    if not response_id:
                        self._write_json(400, {"ok": False, "error": "response_id が不正です。"})
                        return
                    with rs_db.connect(account) as conn:
                        rs_db.acknowledge_response(conn, response_id)
                    _regenerate_dashboard(account)
                    self._write_json(200, {"ok": True})
                except Exception as e:
                    self._write_json(500, {"ok": False, "error": str(e)})
                return

            if self.path == "/api/reply_scout/status":
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    body = json.loads(self.rfile.read(length) or b"{}")
                    candidate_id = str(body.get("candidate_id", "")).strip()
                    reply_status = body.get("status")
                    reply_id = body.get("reply_id") or None
                    if not candidate_id or reply_status not in VALID_REPLY_SCOUT_STATUSES:
                        self._write_json(400, {"ok": False, "error": "candidate_id または status が不正です。"})
                        return
                    with rs_db.connect(account) as conn:
                        rs_db.update_status(conn, candidate_id, reply_status, reply_id)
                    _regenerate_dashboard(account)
                    self._write_json(200, {"ok": True})
                except Exception as e:
                    self._write_json(500, {"ok": False, "error": str(e)})
                return

            self.send_response(404)
            self.end_headers()

    return Handler


def serve(account: Account, port: int = 8765, open_browser: bool = True) -> None:
    df = add_derived_columns(load_posts_df(account))
    text = generate_prescription(df, account)
    generate_dashboard(df, account, text)

    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(account))
    url = f"http://127.0.0.1:{port}/"
    print(f"[{account.name}] {url} でダッシュボードを配信中... (Ctrl+C で停止)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n停止しました。")
    finally:
        server.server_close()
