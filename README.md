# web-tmux

ブラウザから `tmux` セッションを操作するための軽量な Web フロントエンドです。  
Python で `tmux -CC` を制御し、WebSocket 経由で端末入出力を中継し、フロントエンドでは `xterm.js` を使ってペインを描画します。

## 主な機能

- `tmux` のウィンドウ一覧、セッション一覧、ペイン一覧をサイドバーに表示
- アクティブなウィンドウやペインの切り替え
- `panes` リストからペインを選ぶと対象ペインへ切り替えたうえで自動ズーム
- `windows` リストからウィンドウを選ぶと、複数ペイン構成ならズームを解除して表示
- 新規ウィンドウ作成
- 新規セッション作成
- ペインの縦分割・横分割
- ブラウザの表示サイズに合わせた `tmux` 側のリサイズ
- ペイン移動やズーム切り替え直後の入力フォーカス復帰を強化
- モバイル表示時の 1 ペイン集中表示
- モバイル向けの仮想キー
  - `Esc`
  - `Tab`
  - `Enter`
  - カーソルキー
  - `Ctrl` トグル
- モバイル向けトップバー操作
  - アクティブペインの半ページスクロール（上 / 下）
  - クリップボードシートでの表示内容コピーと貼り付け送信
- ウィンドウ切り替えや復帰時のスナップショット再描画
- モバイル表示時の日本語入力（IME）対応
- ペイン一覧の自動ズームとウィンドウ切り替え時のズーム解除
- 画面遷移・リロード後の表示乱れを修正

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
- `start.sh` のポート解放は `lsof` または `fuser` があれば実行されます
- ブラウザから `jsdelivr` にアクセスできること
  - `xterm.js` と `xterm-addon-fit` を CDN から読み込みます

## 対応環境

- 動作前提は `macOS` と `Linux` です
- `tmux -CC` と PTY 制御に依存するため、現状のままでは `Windows` ネイティブ動作は想定していません
- ブラウザはモダンブラウザ前提です
- モバイル表示は `visualViewport` と `navigator.clipboard` が使える環境で最も安定します

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

### `install.sh` だけで導入する

`install.sh` 自身に全ソースが埋め込まれているため、`install.sh` 1 本だけあれば任意のディレクトリにサービスを展開できます。リポジトリをクローンしない運用に便利です。

```bash
mkdir web-tmux && cd web-tmux
# install.sh を配置（scp / curl / コピー & ペーストなど任意の方法で）
chmod +x install.sh
./install.sh
```

実行すると次のファイルがカレントディレクトリに生成されます。

```text
server.py
tmux_control.py
layout_parser.py
static/index.html
static/style.css
static/app.js
start.sh
```

そのあとは通常どおり依存関係の導入と起動を行います。

```bash
python3 -m pip install --user websockets
./start.sh
```

ソースを更新したあと `install.sh` を作り直したい場合は、リポジトリ内で次を実行します。

```bash
python3 .pycache/build_install.py
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
  - 切り替え先が複数ペインでズーム中の場合は、自動でズーム解除して全体表示します
- `panes` からアクティブペインを切り替えます
  - 選択したペインに切り替えたあと、自動でズームします
- `+` ボタンで新規ウィンドウを作成します
- `+ new session` で新規セッションを作成します
  - セッション名は `sess-<UNIX時刻>` 形式で自動採番されます
- `⇿` で横分割、`⇕` で縦分割します
- 画面内のペインを直接クリックした場合は、ペイン選択のみ行い自動ズームはしません

### モバイル操作

- 画面幅が狭い場合はアクティブペインのみを全面表示します
- ハンバーガーボタンからサイドバーを開閉できます
- 上部ボタンでアクティブペインを半ページ単位でスクロールできます
- クリップボードボタンからコピー / ペースト送信用シートを開けます
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

- `start.sh` は起動前に既存の `server.py` を停止し、見つけられる範囲で `8765` と `8766` の待受プロセスを終了します
- `start.sh` は `tmux -L webtmux-ctl kill-server` を実行して、制御用 tmux サーバーを作り直します
- サーバーは `127.0.0.1` にのみ bind します
- 認証機構はありません。外部公開する場合は必ずリバースプロキシやアクセス制限を前段に置いてください
- WebSocket URL はページのプロトコルに応じて `ws://` / `wss://` を自動選択します

## ログ

- `server.log`
  - サーバーの起動ログ、接続ログ、スナップショット取得ログ

必要に応じて `tail -f server.log` で動作を確認できます。
