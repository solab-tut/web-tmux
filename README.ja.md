# web-tmux

[tmux](https://github.com/tmux/tmux) の軽量 Web フロントエンドです。ブラウザからtmux セッションを操作できます。モバイルにも対応しており、ローカルネットワーク内や Tailscale 経由で安全に利用できます。

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
- Python パッケージ [`websockets`](https://pypi.org/project/websockets/)
- モダンブラウザ（Chrome / Safari / Firefox）
- jsDelivr CDN からの xterm.js 読み込みのためインターネット接続

## インストール

### macOS

```bash
# 必要であれば依存関係をインストール
brew install tmux

# リポジトリを取得
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux

# Python パッケージをインストール
python3 -m pip install websockets

# サーバーを起動
./start.sh
```

### Linux（Debian / Ubuntu）

```bash
# 依存関係をインストール
sudo apt install tmux python3 python3-pip

# リポジトリを取得
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux

# Python パッケージをインストール
python3 -m pip install websockets

# サーバーを起動
./start.sh
```

ブラウザで **http://127.0.0.1:8766/** を開きます。

### 起動オプション

**tmux セッション名を変更する**（デフォルト: `web`）:

```bash
TMUX_SESSION=my-session ./start.sh
```

`start.sh` は起動前に既存のサーバープロセスを停止するため、再実行は常に安全です。

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

### 端末内ショートカット

web-tmux は **Ctrl+A** をクライアント側のプレフィックスとして使用します（tmux 自身のプレフィックスキーとは独立しています）:

| ショートカット | 動作 |
|--------------|------|
| `Ctrl+A s` | セッションリストにフォーカス |
| `Ctrl+A w` | ウィンドウリストにフォーカス |
| `Ctrl+A q` | ペインリストにフォーカス |

リストにフォーカスした後は矢印キーで移動、Enter で選択、Escape で端末に戻ります。

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

[Tailscale Serve](https://tailscale.com/kb/1312/serve) を使うと、web-tmux を Tailnet 内のデバイスから HTTPS で安全に利用できます。追加の認証設定は不要で、Tailscale のデバイス認証がアクセス制御として機能します。

### 仕組み

web-tmux は 2 つのローカルポートを使用します:

| ポート | 用途 |
|--------|------|
| 8766 | 静的ファイル（HTTP） |
| 8765 | WebSocket 端末 I/O |

両方を `tailscale serve` で公開する必要があります。ページが HTTPS で配信されると、ブラウザは WebSocket 接続を自動的に `wss://` へ切り替えます。

### 設定

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

> **注意：** この節の内容は未確認です。

Tailnet 外からアクセスする場合は `tailscale serve` の代わりに `tailscale funnel` を使用します。web-tmux には**認証機構がない**ため、Funnel を有効にする前に HTTP Basic 認証などを備えたリバースプロキシを前段に置くことを強く推奨します。

## セキュリティ

- サーバーは `127.0.0.1` にのみバインドし、ネットワークから直接到達できません。
- **認証機構はありません。** リモートアクセスには Tailscale Serve（Tailnet 限定）またはリバースプロキシを利用してください。
- WebSocket URL はページのプロトコルに応じて `ws://`（HTTP）/ `wss://`（HTTPS）を自動選択します。

## ファイル構成

```
web-tmux/
├── server.py          # HTTP + WebSocket サーバー
├── tmux_control.py    # tmux -CC 制御モードラッパー
├── layout_parser.py   # tmux レイアウト文字列パーサー
├── start.sh           # 起動・再起動スクリプト
└── static/
    ├── index.html
    ├── style.css
    └── app.js
```

## ログ

```bash
tail -f server.log
```
