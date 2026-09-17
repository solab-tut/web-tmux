# web-tmux

[tmux](https://github.com/tmux/tmux) の軽量 Web フロントエンドです。Tailscale Serve 経由のブラウザから tmux セッションを操作でき、モバイルにも対応しています。

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
- 今開いている全セッションの構成（ウィンドウ名・分割レイアウト・各ペインの作業ディレクトリ）を名前を付けて保存し、あとから復帰
- **モバイル対応：** アクティブペイン全画面表示、仮想キーボード（Esc / Ctrl / Tab / Enter / 矢印）、IME 入力対応

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
cp .web-tmux.env.example .web-tmux.env && chmod 600 .web-tmux.env
# .web-tmux.env のTailnet HTTPS OriginとTailscaleログイン名を編集
./server.sh   # サーバーを起動
```

### Linux（Ubuntu 22.04 / 24.04）

```bash
sudo apt install tmux
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux
./setup.sh    # .venv を作成して依存パッケージをインストール
cp .web-tmux.env.example .web-tmux.env && chmod 600 .web-tmux.env
# .web-tmux.env のTailnet HTTPS OriginとTailscaleログイン名を編集
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
cp .web-tmux.env.example .web-tmux.env && chmod 600 .web-tmux.env
# .web-tmux.env のTailnet HTTPS OriginとTailscaleログイン名を編集
./server.sh
```

[Tailscaleの設定](#tailscale-を使ったリモートアクセス)を完了してから、ブラウザでTailnetのHTTPS URLを開きます。localhost経由の直接アクセスは意図的に無効化しています。

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
| **Layouts** | クリックで保存済みレイアウトを復帰、🗑 で削除 |
| **+**（Layouts 行） | 今開いている全セッションを1つの名前にまとめて保存 |

レイアウトは、保存した時点で開いていた全セッションのワークスペース全体のスナップ
ショットです — 各セッションのウィンドウ名、ペインの分割の仕方、各ペインの作業ディレ
クトリを記録します。実行中だったコマンドや画面の内容は保存されないので、復帰すると
同じセッション・同じ枠組みに新しいシェルが並びます。保存時の作業ディレクトリが失わ
れていた場合は `$HOME` にフォールバックします。

復帰は各セッションを保存時の名前で行います。いずれかの名前のセッションが既に動いて
いる場合は、**Replace**（衝突しているものだけ既存を破棄して置き換え）、
**Restore as copy**（衝突しているものだけ空いている名前で並べて復帰）、**Cancel** の
いずれかを選ぶダイアログが出ます — この選択は衝突しているセッション全体に一括で適用
されます。衝突していないセッションはどちらを選んでもそのまま復帰されます。各セッシ
ョンはまず一時的な名前で個別に組み立てられるため、あるセッションの復元に失敗しても
他のセッションや既存のセッションに影響しません。復元できなかったセッションがあれば
サイドバーに一言表示されます。

レイアウトの保存先は `~/.local/share/web-tmux/layouts.json` です（[セキュリティ](#セキュリティ)を参照）。

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
  - **Paste** は貼り付けシートを開きます。入力欄を長押しして貼り付け、**Send to terminal** で送信します

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

`.web-tmux.env` を作成して権限を600に保ち、TailnetのHTTPS OriginとTailscaleログイン名だけを設定します:

```dotenv
WEB_TMUX_ALLOWED_ORIGINS=https://<machine>.<tailnet>.ts.net:8766
WEB_TMUX_TAILSCALE_USERS=you@example.com
```

`server.sh` は権限が正確に600でない場合、`.web-tmux.env` を読み込まず起動を中止します。Originが空の場合や、リモートOriginに対するTailscaleログイン名がない場合も起動を拒否します。localhostや `127.0.0.1` は追加せず、ブラウザからはTailscale Serve経由だけで利用してください。

| 設定名 | 内容 |
|--------|------|
| `WEB_TMUX_ALLOWED_ORIGINS` | 許可するHTTP Originのカンマ区切り完全一致リスト |
| `WEB_TMUX_TAILSCALE_USERS` | 許可する `Tailscale-User-Login` のカンマ区切り完全一致リスト |

`.web-tmux.env` を編集したらサーバーを再起動します:

```bash
./server.sh stop && ./server.sh start
```

続けて両方のポートを `tailscale serve` で公開します:

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

- サーバーは `127.0.0.1` にのみバインドし、許可Originからlocalhostを除外することで、ブラウザアクセスをTailscale Serve経由に限定します。ただしループバック待受はホスト境界であってユーザー境界ではなく、別のローカルUnixユーザーはプロキシヘッダーを偽装できるため、信頼できないローカルユーザーがいない個人用マシンを前提とします。
- HTTPとWebSocketは許可済みHost／Originを必須とし、Tailnet接続では許可済みの `Tailscale-User-Login` も検証します。
- Tailscale IDの検証後、`GET /auth/session` がHttpOnly、SameSite=Strict、HMAC署名付きCookieを自動発行します。モバイルのサスペンド復帰時やサーバー再起動後を含め、WebSocket接続のたびにブラウザがCookieを自動更新します。Tailnet HTTPSではSecure属性も付きます。
- Origin欠落、Host不一致、Cookie不正、未許可ユーザーは、tmux状態を生成する前に拒否します。Tailscale IDヘッダーを持たないタグ付き端末も利用できません。
- WebSocketは最大8接続、1メッセージ64 KiB、入力1メッセージ8 KiBに制限され、圧縮は無効です。操作・入力レートと送信キューにも上限があります。
- CSP、フレーム埋め込み禁止、MIME sniffing防止、Referrer／Permissions PolicyをHTTPレスポンスに付与します。
- 不正なOrigin、ID、メッセージ、レート超過は、端末入力やCookie値を含めず `server.log` に記録します。
- 保存したレイアウトは `~/.local/share/web-tmux/layouts.json` に、モード700のディレクトリ内でモード600のファイルとして書き出されます。作業ディレクトリのパスを含むため機密として扱ってください。ネットワークには出ず、リポジトリにも含まれません。
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
├── layout_store.py       # セッション構成の保存・復元計画・ストア
├── test_security.py      # 認証・検証・制限のテスト
├── test_server.py        # セッションHTTPハンドラとレイアウト復元のテスト
├── test_layout_store.py  # レイアウトの取得・並び順・復元計画・ストアのテスト
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

保存したレイアウトはリポジトリの外、`~/.local/share/web-tmux/layouts.json` に置かれます
（保存先は `WEB_TMUX_STATE_DIR` で変更できます）。

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
