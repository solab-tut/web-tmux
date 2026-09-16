# web-tmux

[tmux](https://github.com/tmux/tmux) の軽量 Web フロントエンドです。ブラウザから tmux セッションを操作できます。モバイルにも対応しており、同じマシン上、または Tailscale Serve 経由で利用できます。

```
Browser (xterm.js)  ←─WebSocket─→  server.py  ←─PTY─→  tmux -CC
```

[English](README.md)

## 主な機能

- セッション・ウィンドウ・ペインをサイドバーにリアルタイム表示
- セッション・ウィンドウのインライン切り替え・リネーム・削除
- 新規ウィンドウ・セッション作成、ペインの縦横分割
- ペインズームの切り替え（サイドバーのペインを選択してズーム、再選択で解除）
- ブラウザのビューポートに合わせた端末の自動リサイズ
- カラーテーマ切り替え：Dark（デフォルト）/ Light / Nord
- フォントサイズ調整（11〜18 px）、設定は `localStorage` に保存
- **モバイル対応：** アクティブペイン全画面表示、仮想キーボード（Esc / Ctrl / Tab / Enter / 矢印）、クリップボードシート、スクロールボタン、IME 入力対応

## 必要環境

- Python 3.10 以上
- tmux
- モダンブラウザ（Chrome / Safari / Firefox）

Python 環境は `setup.sh` が自動で構築します。既に [uv](https://github.com/astral-sh/uv) があれば `uv.lock` から `uv sync --frozen` で同期し、なければ既存の Python 3.10+ と `venv` + `pip` を使用します。`setup.sh` 自体がuvをダウンロードしたり、`curl | sh` を実行したりすることはありません。

ブラウザ端末のアセットは `static/vendor/` に同梱しているため、実行時に
CDN 接続は不要です。

## インストール

### macOS

```bash
brew install tmux
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux
./setup.sh    # .venv を作成して依存パッケージをインストール
./server.sh   # サーバーを起動
```

### Linux（Ubuntu 22.04 / 24.04）

```bash
sudo apt install tmux
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux
./setup.sh    # .venv を作成して依存パッケージをインストール
./server.sh   # サーバーを起動
```

### Linux（Ubuntu 20.04）

Ubuntu 20.04 のデフォルト Python は 3.8 です。先にPython 3.10+をインストールするか、[公式手順](https://docs.astral.sh/uv/getting-started/installation/)でuvを別途導入してください。Pythonを手動でインストールする場合:

```bash
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install tmux python3.10 python3.10-venv
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux
./setup.sh
./server.sh
```

ブラウザで **http://127.0.0.1:8766/** を開きます。

### 起動・停止オプション

**tmux セッション名を変更する**（デフォルト: `web`）:

```bash
TMUX_SESSION=my-session ./server.sh
```

**起動・停止:**

```bash
./server.sh        # 起動（起動中なら再起動）
./server.sh start  # 同上
./server.sh stop   # 停止のみ（再起動しない）
```

`server.sh` は、PIDファイル、作業ディレクトリ、コマンドラインが一致する自身のプロセスだけを停止します。無関係なPIDや8765/8766のポート競合を検出した場合は、プロセスをkillせず起動を中止します。

## 使い方

### サイドバー操作

| セクション | 操作 |
|-----------|------|
| **Sessions** | クリックで切り替え、✏ でリネーム、🗑 で削除 |
| **Windows** | クリックで切り替え（ズーム中のウィンドウは自動解除） |
| **Panes** | アクティブなペインをクリック → ズームのオン／オフ切り替え、別のペインをクリック → そのペインへズーム |
| **+**（Windows 行） | 現在のセッションに新規ウィンドウを追加 |
| **+**（Sessions 行） | 新規セッションを作成 |
| **⇿ / ⇕** | アクティブペインを横分割 / 縦分割 |

### テーマとフォントサイズ

トップバー右端の **◑**（テーマ）または **Aa**（フォントサイズ）アイコンをクリックするとドロップダウンが開きます。設定は `localStorage` に保存され次回訪問時も維持されます。

| テーマ | 説明 |
|--------|------|
| Dark   | VS Code Dark 風ダークテーマ（デフォルト） |
| Light  | 明るい背景のライトテーマ |
| Nord   | Nordic カラーパレットのダークテーマ |

フォントサイズ：11 / 12 / 13 / 14 / 16 / 18 px

### モバイル操作

画面幅 768 px 以下の場合:

- アクティブなペインのみ全画面表示されます
- **☰** ボタンでサイドバーを開閉できます
- **下部ツールバー** — 仮想キー：`Esc`、`Ctrl`、`Tab`、`Enter`、矢印キー
  - `Ctrl` トグルを有効にすると次の 1 文字に Ctrl 修飾を適用します
- **右上ボタン** — 半ページスクロール、クリップボードシート（ビューポートのテキストをコピー、テキストを端末に貼り付け）

## Tailscale を使ったリモートアクセス

[Tailscale Serve](https://tailscale.com/kb/1312/serve) を使うと、web-tmux を Tailnet 内のデバイスから HTTPS で利用できます。web-tmux は短命なセッションCookieを発行する前に、ブラウザのOriginと `Tailscale-User-Login` ヘッダーを検証します。

### 仕組み

web-tmux は 2 つのローカルポートを使用します:

| ポート | 用途 |
|--------|------|
| 8766 | 静的ファイル（HTTP） |
| 8765 | WebSocket 端末 I/O |

両方を `tailscale serve` で公開する必要があります。ページが HTTPS で配信されると、ブラウザは WebSocket 接続を自動的に `wss://` へ切り替えます。

### 設定

初回のみ、許可するOriginとTailscaleユーザーを設定してからサーバーを起動します。値はカンマ区切りの完全一致リストで、URL末尾に `/` は付けません:

```bash
cp .web-tmux.env.example .web-tmux.env
chmod 600 .web-tmux.env
# .web-tmux.env 内のHTTPS OriginとTailscaleログイン名を編集
```

このファイルはgit管理外です。`server.sh` は権限が正確に600でない場合、設定を読み込まず起動を中止します。非ローカルOriginは、対応するTailscaleログイン名が明示的に許可されていない限り拒否されます。ローカル利用だけなら、既定で `http://127.0.0.1:8766` と `http://localhost:8766` が許可されるため、このファイルは不要です。

| 設定名 | 内容 |
|--------|------|
| `WEB_TMUX_ALLOWED_ORIGINS` | 許可するHTTP Originのカンマ区切り完全一致リスト |
| `WEB_TMUX_TAILSCALE_USERS` | 許可する `Tailscale-User-Login` のカンマ区切り完全一致リスト |

```bash
tailscale serve --bg --https=8766 http://127.0.0.1:8766
tailscale serve --bg --https=8765 http://127.0.0.1:8765
```

> **Linux の場合：** `tailscale serve` には `sudo` が必要です。macOS では通常不要です。

以下の URL でアクセスできます:

```
https://<machine-name>.<tailnet>.ts.net:8766/
```

### 確認

```bash
tailscale serve status
```

以下のような出力が得られれば正常です:

```
https://<machine-name>.<tailnet>.ts.net:8765/ (tailnet only)
|-- / proxy http://127.0.0.1:8765

https://<machine-name>.<tailnet>.ts.net:8766/ (tailnet only)
|-- / proxy http://127.0.0.1:8766
```

### 停止

```bash
tailscale serve --https=8766 off
tailscale serve --https=8765 off
```

> **Linux の場合：** こちらのコマンドにも `sudo` が必要です。

### Tailscale Funnel による外部公開

**web-tmux では Funnel を有効にしないでください。** 本アプリは本人所有の非公開Tailnet専用です。インターネット公開と複数ユーザー向け認可はセキュリティモデルの対象外です。

## セキュリティ

- サーバーは `127.0.0.1` にのみバインドし、リモート接続はTailscale Serve経由に限定します。
- HTTPとWebSocketは許可済みHost／Originを必須とし、Tailnet接続では許可済みの `Tailscale-User-Login` も検証します。
- `GET /auth/session` がHttpOnly、SameSite=Strict、HMAC署名付きCookieを発行します。Cookieは8時間またはサーバー再起動で失効し、フロントエンドが自動更新します。Tailnet HTTPSではSecure属性も付きます。
- Origin欠落、Host不一致、Cookie不正、未許可ユーザーは、tmux状態を生成する前のWebSocketハンドシェイクで拒否します。Tailscale IDヘッダーを持たないタグ付き端末も利用できません。
- WebSocketは最大8接続、1メッセージ64 KiB、入力1メッセージ8 KiBに制限され、圧縮は無効です。操作・入力レートと送信キューにも上限があります。
- CSP、フレーム埋め込み禁止、MIME sniffing防止、Referrer／Permissions PolicyをHTTPレスポンスに付与します。
- 不正なOrigin、ID、メッセージ、レート超過は、端末入力やCookie値を含めず `server.log` に記録します。
- `tailscale serve status` が上記2ポートのtailnet-only公開だけになっていることを維持してください。

## サードパーティのブラウザアセット

- `xterm@5.3.0`
- `xterm-addon-fit@0.8.0`
- `xterm-addon-unicode11@0.4.0`

これらのアセットは MIT ライセンスで配布されています。ライセンスファイルは
`static/vendor/` 内の各 vendored ファイルの近くに同梱しています。

## ファイル構成

```
web-tmux/
├── server.py             # HTTP + WebSocket サーバー
├── web_security.py       # Origin、Host、ID、署名Cookieの検証
├── tmux_control.py       # tmux -CC 制御モードラッパー
├── layout_parser.py      # tmux レイアウト文字列パーサー
├── test_security.py      # 認証・検証・制限のテスト
├── pyproject.toml        # Pythonプロジェクトと固定依存関係
├── uv.lock               # uv用ロックファイル
├── requirements.txt      # venv + pip用固定依存関係
├── .web-tmux.env.example # Tailnet設定例
├── setup.sh              # 初回環境構築スクリプト（.venvを作成）
├── server.sh             # サーバーの起動・停止
└── static/
    ├── index.html
    ├── style.css
    ├── app.js
    └── vendor/          # 同梱した xterm.js 実行時アセットとライセンス
```

## テスト

`setup.sh` 実行後、プロジェクトの仮想環境でテストを実行します:

```bash
.venv/bin/python -m unittest -v
```

## ログ

```bash
tail -f server.log
```

起動時に `server.log` は新しく作り直され、権限600になります。拒否理由は記録しますが、端末内容、入力データ、Cookie値は記録しません。
