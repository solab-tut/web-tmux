# web-tmux

ブラウザから `tmux` セッションを操作するための軽量な Web フロントエンドです。  
Python で `tmux -CC` を制御し、WebSocket 経由で端末入出力を中継し、フロントエンドでは `xterm.js` を使ってペインを描画します。

## 主な機能

- `tmux` のウィンドウ一覧、セッション一覧、ペイン一覧をサイドバーに表示
- アクティブなウィンドウやペインの切り替え
- 新規ウィンドウ作成
- 新規セッション作成
- ペインの縦分割・横分割
- ブラウザの表示サイズに合わせた `tmux` 側のリサイズ
- モバイル表示時の 1 ペイン集中表示
- モバイル向けの仮想キー
  - `Esc`
  - `Tab`
  - `Enter`
  - カーソルキー
  - `Ctrl` トグル
- ウィンドウ切り替えや復帰時のスナップショット再描画

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
- ブラウザから `jsdelivr` にアクセスできること
  - `xterm.js` と `xterm-addon-fit` を CDN から読み込みます

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

起動に成功すると、次のアドレスが表示されます。

```text
HTTP  http://127.0.0.1:8766/
WS    ws://127.0.0.1:8765/
```

ブラウザで `http://127.0.0.1:8766/` を開いて利用します。

## 利用方法

### 基本操作

- サイドバーの `sessions` からセッションを切り替えます
- `windows` からウィンドウを切り替えます
- `panes` からアクティブペインを切り替えます
- `+` ボタンで新規ウィンドウを作成します
- `+ new session` で新規セッションを作成します
- `⇿` で横分割、`⇕` で縦分割します

### モバイル操作

- 画面幅が狭い場合はアクティブペインのみを全面表示します
- ハンバーガーボタンからサイドバーを開閉できます
- 下部の仮想キーから `Esc`、`Tab`、`Enter`、カーソル移動を送れます
- `Ctrl` を有効にすると、次の 1 文字に対して Ctrl 修飾を適用します

### セッション名の変更

デフォルトでは `TMUX_SESSION=web` を操作対象にします。  
別名にしたい場合は起動前に環境変数を設定してください。

```bash
TMUX_SESSION=my-session ./start.sh
```

## ファイル構成

- `server.py`
  - HTTP サーバーと WebSocket サーバー本体
- `tmux_control.py`
  - `tmux -CC` 制御ラッパー
- `layout_parser.py`
  - `tmux` のレイアウト文字列解析
- `static/index.html`
  - 画面骨格
- `static/style.css`
  - レイアウトとスタイル
- `static/app.js`
  - クライアント側ロジック
- `start.sh`
  - 起動用スクリプト

## 注意点

- `start.sh` は起動前に既存の `server.py` を停止し、`8765` と `8766` の待受プロセスを終了します
- `start.sh` は `tmux -L webtmux-ctl kill-server` を実行して、制御用 tmux サーバーを作り直します
- サーバーは `127.0.0.1` にのみ bind します
- 認証機構はありません。外部公開する場合は必ずリバースプロキシやアクセス制限を前段に置いてください
- WebSocket URL はページのプロトコルに応じて `ws://` / `wss://` を自動選択します

## ログ

- `server.log`
  - サーバーの起動ログ、接続ログ、スナップショット取得ログ

必要に応じて `tail -f server.log` で動作を確認できます。
