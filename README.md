# PostDoctor

X（旧Twitter）の投稿実績から「いつ投稿すると伸びるか」を可視化し、
処方箋（おすすめ投稿枠・運用提言）を自動生成するツール。
複数アカウントを横断管理できる汎用構成。

従量課金対策として **since_id による差分取得** を採用し、同じ投稿を二度読みません。

## アーキテクチャ（4層構成）

```
postdoctor/
  config.py       アカウント設定・認証情報の解決
  fetcher/        取得層  … X API から投稿＋メトリクスを差分取得
  storage/        DB層    … アカウント単位の SQLite への永続化
  analysis/       分析層  … 曜日×時間帯ヒートマップ等の集計（API消費ゼロ）
  prescription/   処方箋生成層 … 分析結果から投稿運用の提言（Markdown）を生成
  dashboard/      ヒートマップ・処方箋を1画面にまとめたHTMLダッシュボードを生成
  reply_scout/    リプライ営業支援 … 競馬系の伸びている投稿を発見し、リプライ下書きを生成（下記参照）
  cli.py          CLI エントリポイント
  server.py       ダッシュボードをブラウザで配信し「更新」ボタンから再実行するローカルサーバー
```

各層は下位層の内部実装（SQL・API呼び出し等）を知らず、公開関数のみを介して連携します。

## セットアップ（Windows / PowerShell）

```powershell
cd PostDoctor
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## .env の作成

developer.x.com のアプリ設定から取得して `.env` に記載（`.env.example` を参照）:

```
X_CONSUMER_KEY=xxxx
X_CONSUMER_SECRET=xxxx
X_ACCESS_TOKEN=xxxx
X_ACCESS_TOKEN_SECRET=xxxx
```

- Access Token は「Read and Write」権限で発行すること
- アカウントごとに別アプリの認証情報を使いたい場合は、`config/accounts.yaml` で
  `credential_prefix` を指定し、`.env` に `<PREFIX>_CONSUMER_KEY` 等を追加する
- `reply_scout`（リプライ営業支援）のリプライ下書き生成には `ANTHROPIC_API_KEY` が必要。
  未設定の場合は下書き生成が自動的にプレースホルダを返すモック動作になるため、
  他の機能（fetch/analyze/prescribe/dashboard等）には影響しない

## マルチアカウント設定

`config/accounts.yaml` に対象アカウントを追加します:

```yaml
accounts:
  rakuba_ai:
    screen_name: rakuba_ai
    user_id: "2074065705956454401"
  another_account:
    screen_name: another_account
    user_id: "1234567890"
    credential_prefix: ANOTHER   # .env に ANOTHER_CONSUMER_KEY 等を用意
```

データは `data/<account名>/` 以下に分離して保存されます
（`posts.db`, `heatmap_<metric>.png`, `prescription.md`）。

## 使い方

```powershell
# 1. 投稿データを取得（初回は直近100件、以降は差分のみ）
python main.py fetch --account rakuba_ai

# 2. 分析（API消費ゼロ）
python main.py analyze --account rakuba_ai                            # インプレッション
python main.py analyze --account rakuba_ai --metric engagement_rate   # エンゲージメント率

# 3. 処方箋を生成（おすすめ投稿枠・運用提言）
python main.py prescribe --account rakuba_ai

# まとめて実行（fetch -> 全指標analyze -> prescribe）
python main.py run --account rakuba_ai

# 全アカウントに対して実行
python main.py run --all

# ダッシュボード（ヒートマップ＋処方箋を1画面にまとめたHTML）を生成
python main.py dashboard --account rakuba_ai

# ダッシュボードをブラウザで配信（「更新」ボタンから再実行できる）
python main.py serve --account rakuba_ai
```

出力: `data/<account>/heatmap_<metric>.png` ＋ `data/<account>/prescription.md` ＋ `data/<account>/dashboard.html`

## リプライ営業支援 (reply_scout)

新規アカウントの露出を増やすため、競馬関連の伸びている投稿を発見し、
Rakubaの予測データを根拠にしたリプライ下書きを生成する機能。
**送信は必ず人間が手動で行う。自動送信・自動いいね・自動フォロー機能は一切実装していない。**

```powershell
# 1. 候補投稿を収集してスコアリング（TOP10を選定）
python main.py reply-scout scout --account rakuba_ai

# 2. リプライ下書きを2案生成（ANTHROPIC_API_KEY未設定時はプレースホルダ）
python main.py reply-scout draft --account rakuba_ai

# まとめて実行（scout -> draft）
python main.py reply-scout run --account rakuba_ai

# 送信済みリプライの反応（いいね等）を後日追跡
python main.py reply-scout track --account rakuba_ai

# ステータス手動更新（ダッシュボードのボタンからも可能）
python main.py reply-scout status --account rakuba_ai --id <tweet_id> --status 送信済み|見送り
```

- 検索キーワード・日次読み取り上限・フォロワー取得上限などは `config/keywords.json` で編集する
- スコアは「エンゲージメント速度＋フォロワー数」だけでなく、以下も加味する
  （**`specific_terms`・`hype_terms`・`trusted_authors` は初期状態では空なので、
  効かせるには自分で値を追加すること**）:
  - `specific_terms`: レース名・馬名など具体的な語を含む投稿を優遇（例: `["函館記念", "○○賞", "馬名A"]`）
  - `hype_terms`: 「限定」「教える」等の煽り系ワードを含む投稿はスコアを半減（初期値は最低限のサンプルのみ）
  - `trusted_authors`: メディア公式・信頼できるアカウントのスクリーンネームを優遇
- Rakubaの予測データは `data/<account>/predictions.json`
  （`[{"race_name": "...", "date": "...", "summary": "..."}]`）に手動で配置すると、
  レース名が一致する候補への下書きに根拠として引用される
- ダッシュボード（`python main.py serve`）の「リプライ営業」タブから、CLIを使わず
  全て操作できる:
  - **🔍 候補を更新**（scout + draft）／**📈 反応を追跡**（track）ボタン
    ※どちらもAPIコストが発生するため、確認ダイアログで概算コストを表示してから実行する
  - 各候補の **送信済みにする** / **見送り** ボタンでステータスを記録
    （送信済みにする際、リプライのURL/IDを入力すると反応追跡の対象になる）
  - 上記CLIコマンドも引き続き使用可能（`--predictions` でファイルを差し替えたい場合など）

### コスト目安

- 候補検索（ツイート読み取り）: $0.005/件、デフォルト上限50件/日
- 投稿者フォロワー数取得: $0.010/件、上位候補（デフォルト15件）のみに限定
- 送信済みリプライの反応追跡: 自分の投稿の読み取りのため $0.001/件
- 例: 毎日50件検索 + 上位15件のフォロワー取得 → 1日あたり最大 $0.40 程度

## ダッシュボードの自動起動（Windowsスタートアップ）

```powershell
.\scripts\install_startup_task.ps1 rakuba_ai    # 登録（管理者権限不要）
.\scripts\uninstall_startup_task.ps1 rakuba_ai  # 解除
```

現在のユーザーの Windows スタートアップフォルダにショートカットを作成し、
ログオン時に `pythonw.exe`（コンソール非表示）で `serve --no-browser` を自動起動します。
起動後は `http://127.0.0.1:8765/` にアクセスすればダッシュボードを閲覧・更新できます。
（このマシンでは Task Scheduler への登録が権限不足で拒否されたため、スタートアップフォルダ方式を採用）

## コスト目安

- 自分の投稿読み取り: $0.001/件 → 週1回100件取得しても月 $0.4 程度
- 分析・処方箋生成は全てローカル SQLite に対して行うため追加コストなし

## 運用のコツ

- **週1回**（月曜朝など）`python main.py run --account <name>` を回すだけで十分
- インプレッションは投稿直後より数日後の方が確定値に近いので、
  週1バッチはコスト面でも精度面でも合理的
- タスクスケジューラで自動化する場合は venv の python.exe をフルパス指定

## 次のステップ

- 予想データから投稿文を自動生成（Claude API）＋型別の効果測定
- トレンド監視＋便乗アラート
- 競合アカウント週次分析
