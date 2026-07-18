"""リプライ営業支援 (reply_scout) - 競馬系の伸びている投稿を発見し、
Rakubaの予測データを根拠にしたリプライ下書きを生成する新モジュール。

4層構成（既存の fetcher/storage/analysis/prescription と同じ考え方）:
  fetcher     取得層   - X API v2 recent search で候補投稿を収集
  db          DB層     - アカウント単位の SQLite (reply_scout.db) への永続化
  analyzer    分析層   - スコアリング・ランキング（API消費ゼロ）
  prescriber  処方箋層 - Claude API でリプライ下書きを2案生成

送信・いいね・フォローの自動化機能は一切実装しない。送信は必ず人間が
ダッシュボードまたはXアプリから手動で行う。
"""
