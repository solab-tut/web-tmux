# web-tmux

ブラウザから `tmux` セッションを操作するための軽量な Web フロントエンドです。  
Python で `tmux -CC` を制御し、WebSocket 経由で端末入出力を中継し、フロントエンドでは `xterm.js` を使ってペインを描画します。

## 主な機能

- `tmux` のウィンドウ一覧、セッション一覧、ペイン一覧をサイドバーに表示
- アクティブなウィンドウやペインの切り替え
- セッション・ウィンドウのリネームとインライン削除（最終ウィンドウ削除ガード付き）
- `panes` リストからペインを選ぶと対象ペインへ切り替えたうえで自動ズーム
- `windows` リストからウィンドウを選ぶと、複数ペイン構成ならズームを解除して表示
- 新規ウィンドウ・新規セッション作成
- ペインの縦分割・横分割
- ブラウザの表示サイズに合わせた `tmux` 側のリサイズ
- **カラーテーマ切り替え（Dark / Light / Nord）** — トップバーのアイコンから選択、`localStorage` に保存
- ページタイトルとトップバーへのホスト名表示
- ウィンドウ・ペイン切り替え後のスナップショット再描画（scrollback バッファを保持）
- モバイル表示時の 1 ペイン集中表示
- モバイル向けの仮想キー（`Esc` / `Tab` / `Enter` / カーソルキー / `Ctrl` トグル）
- モバイル向けトップバー操作（半ページスクロール・クリップボードシート）
- モバイル表示時の日本語入力（IME）対応

## 動作概要

構成は次の 3 層です。

1. `server.py`
   - 静的ファイルを `127.0.0.1:8766` で配信します。
   - WebSocket を `127.0.0.1:8765` で待ち受けます。
2. `tmux_control.py`
   - `tmux -CC attach-session` を PTY 上で起動し、制御モードの通知とコマンド応答を扱います。
3. `static/`
   - ブラウザ上で `xterm.js` により端末を表示します。
   - レイアウト変更、入力送信、スナップショット復元を処理します。

## 前提条件

- Python 3.10 以上
- `tmux`
- `pip` で `websockets` を導入できること
- `start.sh` を使う場合は `bash` が使えること
- ブラウザから `jsdelivr.net` にアクセスできること（`xterm.js` を CDN から読み込みます）

## 対応環境

- macOS / Linux
- `tmux -CC` と PTY 制御に依存するため、Windows ネイティブ動作は想定していません
- ブラウザはモダンブラウザ前提です

## 設置方法

### 1. リポジトリを配置

```bash
git clone git@github.com:solab-tut/web-tmux.git
cd web-tmux
```

### 2. Python 依存関係を導入

```bash
python3 -m pip install websockets
```

### 3. サーバーを起動

```bash
./start.sh
```

起動に成功すると次のアドレスが表示されます。

```
HTTP  http://127.0.0.1:8766/
WS    ws://127.0.0.1:8765/
```

ブラウザで `http://127.0.0.1:8766/` を開いて利用します。

操作対象のセッション名（デフォルト: `web`）を変更したい場合は環境変数で指定します。

```bash
TMUX_SESSION=my-session ./start.sh
```

## 利用方法

### 基本操作

- サイドバーの `sessions` からセッションを切り替えます
- `windows` からウィンドウを切り替えます
- `panes` からアクティブペインを切り替えます（選択後に自動ズーム）
- `+` ボタンで新規ウィンドウ、`+ new session` で新規セッションを作成します
- `⇿` で横分割、`⇕` で縦分割します
- セッション・ウィンドウ名はサイドバー項目の編集アイコンからリネームできます
- 削除はサイドバー項目のゴミ箱アイコン → インライン確認ボタンで実行します

### カラーテーマ

トップバー右端の半円アイコンをクリックするとテーマメニューが開きます。

| テーマ | 概要 |
|--------|------|
| Dark   | VS Code Dark ベースのダークテーマ（デフォルト）|
| Light  | 明るい背景のライトテーマ |
| Nord   | Nordic カラーパレットのダークテーマ |

選択したテーマはブラウザの `localStorage` に保存され、次回以降も維持されます。

### モバイル操作

- 画面幅が狭い場合はアクティブペインのみを全面表示します
- ハンバーガーボタンからサイドバーを開閉できます
- 上部ボタンでアクティブペインを半ページ単位でスクロールできます
- クリップボードボタンからコピー / ペースト送信用シートを開けます
- 下部の仮想キーから `Esc`、`Tab`、`Enter`、カーソル移動を送れます
- `Ctrl` を有効にすると、次の 1 文字に対して Ctrl 修飾を適用します

## Tailscale を使った VPN 内利用

[Tailscale](https://tailscale.com/) の **Tailscale Serve** を使うと、ローカルで動く web-tmux を Tailnet 内の任意のデバイスから HTTPS で安全に利用できます。追加のパスワード設定は不要で、Tailscale のデバイス認証がそのまま認証層として機能します。

### 仕組み

web-tmux は HTTP（ポート 8766）と WebSocket（ポート 8765）の 2 ポートを `127.0.0.1` でリッスンします。Tailscale Serve でそれぞれを Tailnet 上の HTTPS エンドポイントとして公開します。

フロントエンドはページのプロトコルが `https://` であることを検出すると、WebSocket 接続を自動的に `wss://` に切り替えます。

### 設定手順

```bash
# HTTP フロントエンドを Tailnet 内に HTTPS で公開
tailscale serve https / http://127.0.0.1:8766

# WebSocket を同ホスト名・ポート 8765 で公開
tailscale serve https:8765 / http://127.0.0.1:8765
```

設定後、Tailscale が割り当てるホスト名（`https://<machine-name>.your-tailnet.ts.net/`）でアクセスできます。ホスト名は `tailscale status` または Tailscale 管理画面で確認できます。

### 確認

```bash
tailscale serve status
```

以下のような出力が得られれば正常です。

```
https://<machine-name>.<tailnet>.ts.net:443 /   → http://127.0.0.1:8766
https://<machine-name>.<tailnet>.ts.net:8765 /  → http://127.0.0.1:8765
```

### 停止

Tailscale Serve の公開をやめる場合は次のコマンドを実行します。

```bash
tailscale serve https / off
tailscale serve https:8765 / off
```

### 注意

- Tailscale Serve による公開範囲は Tailnet 内のデバイスのみです（インターネット公開ではありません）
- インターネットへの公開が必要な場合は **Tailscale Funnel** を使いますが、その場合は web-tmux の前段に Basic 認証などを設けることを強く推奨します

## ファイル構成

```
web-tmux/
├── server.py          # HTTP / WebSocket サーバー
├── tmux_control.py    # tmux -CC 制御ラッパー
├── layout_parser.py   # tmux レイアウト文字列解析
├── start.sh           # 起動スクリプト
└── static/
    ├── index.html     # HTML 骨格
    ├── style.css      # レイアウト・テーマ定義
    └── app.js         # クライアント側ロジック
```

## 注意点

- `start.sh` は起動前に既存の `server.py` プロセスを停止し、ポート 8765・8766 を解放してから再起動します
- サーバーは `127.0.0.1` にのみ bind します
- **認証機構はありません。** 外部公開する場合は Tailscale Serve / Funnel、またはリバースプロキシによるアクセス制限を前段に置いてください
- WebSocket URL はページのプロトコルに応じて `ws://` / `wss://` を自動選択します

## ログ

サーバーの起動・接続・スナップショット取得ログは `server.log` に出力されます。

```bash
tail -f server.log
```
