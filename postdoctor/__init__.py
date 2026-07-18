"""PostDoctor - 投稿分析＆処方箋生成ツール。

4層構成:
  fetcher      取得層  - X API から投稿データを差分取得
  storage      DB層    - SQLite への永続化（アカウント単位）
  analysis     分析層  - 曜日×時間帯ヒートマップ等の集計
  prescription 処方箋生成層 - 分析結果から投稿運用の提言を生成
"""
