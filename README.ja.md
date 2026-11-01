# web-tmux

ノートPCでも、タブレットでも、スマホでも。ブラウザからいつもの tmux を操作できます。接続は [Tailscale](https://tailscale.com) の中だけで完結します。

[English](README.md)

<p>
  <img src="docs/images/screenshot-desktop.png" alt="デスクトップのブラウザで動く web-tmux：サイドバーに3つのセッションと、現在のセッションのウィンドウ・ペインが並び、端末は vim・テスト実行・git log の3ペインに分割されている" width="68%">
  <img src="docs/images/screenshot-phone.png" alt="スマホで動く web-tmux：vim のペインをズームして全画面表示し、下部に esc・tab・ctrl・enter・矢印キーが並ぶ" width="26%">
</p>

- **セッション・ウィンドウ・ペインをサイドバーで一覧**：切り替え、分割、リネーム、ズーム、レイアウト全体の保存と復帰
- **スマホやタブレットでも使いやすく**：タップ1つでペインを全画面にズーム、Esc / Tab / Shift+Tab / Ctrl / 矢印の仮想キー、日本語 IME 入力
- **コーディングエージェントを見守る**：Claude Code・Codex・Hermes Agent のペインが、作業中か、あなたの応答待ちか、完了したかを表示
- **他のマシンの tmux も** SSH 経由で同じ画面から。そのマシンを Tailnet に入れる必要はありません
- **安全な設計**：入れるのはあなたの Tailnet ログインだけ。インターネットにも LAN にも公開しません

## クイックスタート

tmux を動かすマシンに、tmux・git・Python 3.10 以上（または [uv](https://github.com/astral-sh/uv)）と、[Tailscale](https://tailscale.com/download)（インストールして接続済み）が必要です。Tailscale の[管理画面](https://login.tailscale.com/admin/dns)で MagicDNS と HTTPS 証明書を有効にしておいてください（無効なら setup が知らせます）。

```bash
curl -fsSL https://raw.githubusercontent.com/solab-tut/web-tmux/main/install.sh | bash
```

最新リリースを `~/web-tmux` にインストールし、`./setup.sh` を実行します。`setup.sh` は次のことを行います。

1. `.venv` に Python 環境を作ります。
2. `tailscale status` からこのマシンの Tailnet 上の名前とあなたのログイン名を読み取って表示し、確認のうえ `.web-tmux.env` を書き出します。
3. web-tmux を Tailnet に公開する `tailscale serve` コマンドを、実行するか尋ねます（Linux では `sudo` 付き）。

あとは web-tmux を起動し、表示されたアドレスを Tailnet 上のどの端末からでも開くだけです。

```bash
cd ~/web-tmux && ./server.sh
# Open  https://<machine>.<tailnet>.ts.net:8766/
```

インストーラーはパッケージを追加せず、`sudo` も使いません（`sudo` を使うのは、Linux で任意の `tailscale serve` を実行するときだけで、実行前に必ず確認します）。足りないツールがあれば、その名前と入れ方を表示して止まります。先にスクリプトを読みたい場合や、git で直接入れたい場合はこちら:

```bash
curl -fsSLO https://raw.githubusercontent.com/solab-tut/web-tmux/main/install.sh
less install.sh && bash install.sh
# または
git clone https://github.com/solab-tut/web-tmux.git && cd web-tmux && ./setup.sh
```

**前提ツールの入れ方**

| システム | コマンド |
|----------|----------|
| macOS | `brew install tmux git python` と [Tailscale アプリ](https://tailscale.com/download/mac) |
| Ubuntu 22.04 以降 | `sudo apt install tmux git python3 python3-venv` と [Linux 版 Tailscale](https://tailscale.com/download/linux) |
| Ubuntu 20.04 | 標準の Python が 3.8 のため、[uv](https://docs.astral.sh/uv/getting-started/installation/) を入れるか、`deadsnakes` PPA から Python 3.10（`python3.10 python3.10-venv`）を入れてください |

**更新：** 同じ `curl … | bash` をもう一度実行し（自分で clone した場合は `git pull && ./setup.sh`）、`./server.sh restart` で再起動します。`.web-tmux.env` はそのまま残ります。

## なぜ Tailscale なのか

web-tmux は、開いた人にシェルを渡すツールです。だから直接つながる口は作りません。サーバーは `127.0.0.1` でだけ待ち受け、[Tailscale Serve](https://tailscale.com/kb/1312/serve) が Tailnet に HTTPS で届けます。誰からのアクセスかは Tailscale が web-tmux に伝え、web-tmux は `.web-tmux.env` に書かれたログインだけを通します。パスワードを決める必要も、ルーターのポートを開ける必要もなく、自宅でも職場でも、モバイル回線のスマホからでも同じアドレスで使えます。`http://127.0.0.1:8766` を直接開いても、意図的に拒否します。

```
Browser (xterm.js)  ←─HTTPS / WebSocket─→  tailscale serve  ─→  server.py  ←─PTY─→  tmux -CC
```

## ネットワーク構成

![web-tmuxのネットワーク構成：Tailscale経由でブラウザから接続し、サーバー自身のtmuxはローカルPTY、LAN内の別端末や外部サーバーのtmuxはSSHで操作](docs/images/network.ja.svg)

ファイアウォールで保護された閉じたLAN内に、web-tmuxサーバーを配置する構成例です。

1. Tailscaleに参加している操作端末のブラウザから、**Tailscale Serve** 経由（HTTPS / WebSocket）でweb-tmuxにアクセスします。
2. web-tmuxから、**サーバーホスト自身のtmux** はローカルPTY経由で、**LAN内の別端末のtmux** やファイアウォールの外にある **外部のSSHサーバー**（VPSなど）のtmuxはSSH経由で操作できます。
3. LAN内でTailscaleに参加するのはサーバーホスト1台のみです。ほかのLAN内端末や外部サーバーへのTailscale導入は不要で、ブラウザは各端末に直接接続せず、web-tmuxだけに接続します。

SSH接続先の設定は[他のホストの tmux（SSH）](#他のホストの-tmuxssh)を参照してください。

## 主な機能

- セッション・ウィンドウ・ペインをサイドバーにリアルタイム表示
- セッション・ウィンドウのインライン切り替え・リネーム・削除
- 新規ウィンドウ・セッション作成、ペインの縦横分割
- ペインズームの切り替え（サイドバーのペインを選択してズーム、再選択で解除）
- ブラウザのビューポートに合わせた端末の自動リサイズ
- カラーテーマ切り替え：Dark（デフォルト）/ Light / Nord
- フォントサイズ調整（11〜18 px）、設定は `localStorage` に保存
- 今開いている全セッションの構成（ウィンドウ名・分割レイアウト・各ペインの作業ディレクトリ）を名前を付けて保存し、あとから復帰
- 閲覧専用のファイルビューア：ペインの作業ディレクトリから辿ってテキストファイルを表示し、Markdown はプレビュー表示。画像（PNG / JPEG / GIF / WebP / AVIF / BMP / ICO / SVG）も表示でき、Markdown 内で同じホストの画像を参照していればそれも表示します（トップバーのフォルダアイコン、またはサイドバーのペインのフォルダボタン）。SSH 接続先でも使えます。テキストは 200 KiB まで、画像は 4 MiB までで、開けるのは通常ファイルのみ
- Claude Code / Codex / Hermes Agent が動いているペインの状態をサイドバーにアイコンで表示：プロンプト（`❯_`）＝待機中、青の回転矢印＝思考中（Claude Code・Hermes がバックグラウンドで動かしているサブエージェントが残っている間も含む）、橙の警告（点滅）＝承認待ち・選択待ち（Hermes のモデル選択など）、緑のチェック＝処理が終わった（未確認。そのペインにキーを打つか、そのウィンドウを表示したあとで別のウィンドウに移ると消えます。クリックやスクロールでは消えません。表示中のウィンドウでも同じなので、タブを裏にしていても、席を外していても気づけます）。承認・選択待ちか完了・未確認のエージェントがあると、☰ ボタンが橙か緑になり（動作中のエージェントがあれば右上で青い矢印が回ります）、タブのタイトル（`⚠1 ✓2 …`）とアイコンにも出ます。サイドバーを閉じているときや、別のタブを見ているときのためです。ウィンドウ行とセッション行には、その中で一番急ぐ状態を表示します。接続できる他の SSH ホストのエージェントも、そのホストのセッション行に表示し、☰ ボタンとタブの件数にも含めます。サーバーがペインの画面下部を読み、各エージェントの表示（`esc to interrupt`、`Do you want to proceed?` など）で判定するので、設定は不要です。今見ているホストは約 2 秒ごと、他のホストはブラウザが接続している間だけ 10 秒ごとに読みます。エージェントが表示の文言を変えると判定が外れることがあります
- **モバイル対応：** ペインの全画面ズーム、仮想キーボード（Esc / Tab / Shift+Tab / Ctrl / Enter / 矢印）、IME 入力対応

## サーバーの起動と停止

web-tmux のディレクトリ（クイックスタートなら `~/web-tmux`）で実行します。

**tmux セッション名を変更する**（デフォルト: `web`）:

```bash
TMUX_SESSION=my-session ./server.sh
```

**起動・停止:**

```bash
./server.sh        # 起動（起動中なら再起動）
./server.sh start  # 同上
./server.sh restart # 明示的に再起動
./server.sh stop   # 停止のみ（再起動しない）
```

`server.sh` は、このチェックアウトのサーバーと確認できるプロセスだけを停止します。Linuxでは、PIDファイルが失われたり古くなった場合も、`/proc` の所有者・作業ディレクトリ・起動引数を確認して停止できます。無関係なPIDは停止せず、別のチェックアウトやアプリが8766を使用している場合は、そのプロセスをkillせず起動を中止します。

## 使い方

### サイドバー操作

| セクション | 操作 |
|-----------|------|
| **Sessions** | クリックで切り替え、✏ でリネーム、🗑 で削除 |
| **Windows** | クリックで切り替え（ズーム中のウィンドウは自動解除） |
| **Panes** | アクティブなペインをクリック → ズームのオン／オフ切り替え、別のペインをクリック → そのペインへズーム |
| **❯_ / ↻ / ✓ / ⚠**（行頭のアイコン） | AI エージェントの状態：待機中／思考中／完了・未確認／承認・選択待ち。マウスを乗せるとエージェント名と状態を表示 |
| **+**（Windows 行） | 現在のセッションに新規ウィンドウを追加 |
| **+**（Sessions 行） | 新規セッションを作成（ssh ホストがない場合。ある場合は各ホスト行の **+**） |
| **ホスト行**（ssh ホスト設定時） | クリックでそのホストのセッションを折り畳み／展開 |

Windows と Panes は現在のセッションの中身なので、`ホスト › セッション` の見出しを付けた1つの枠にまとめて表示します。
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
**Restore as copy**（衝突しているものだけ空いている名前で並べて復帰）、
**Skip existing**（衝突しているものは無視し、それ以外だけ復帰）、**Cancel** のいずれか
を選ぶダイアログが出ます — この選択は衝突しているセッション全体に一括で適用されます。
衝突していないセッションはいずれの復帰方法でもそのまま復帰されます。各セッシ
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

### マウスを使うアプリからのコピー

マウス操作をアプリ側で受け取るもの（Claude Code のフルスクリーン表示、`mouse=a` の vim、tmux のコピーモード）は、ブラウザの選択ではなく tmux のペーストバッファにコピーします。web-tmux は新しいペーストバッファを、フォーカスのあるブラウザの OS クリップボードへ転送します。Chrome / Edge ではそのまま書き込まれます。ブラウザが書き込みを拒否した場合（Safari、Firefox）は、右下に **Copy N characters** ボタンが 10 秒間表示されるので、クリックするとコピーが完了します。128 KiB を超えるバッファは転送しません。

### モバイル操作

画面幅 768 px 以下の場合:

- 分割されたウィンドウはそのまま表示されます。サイドバーでペインをタップすると全画面にズームし、もう一度タップすると戻ります
- **☰** ボタンでサイドバーを開閉できます
- トップバーの **Paste** アイコンは貼り付けシートを開きます。入力欄を長押しして貼り付け、**Send to terminal** で送信します

**下部ツールバー**（仮想キー：`esc`、`tab`、`⇧tab`（Shift+Tab）、`ctrl`、`enter`、矢印キー）は、画面幅に関係なくタッチ操作に対応した端末で自動表示されます。この判定にはタッチ入力の有無を使うため、デスクトップ表示のユーザーエージェントを使う iPad / iPadOS も対象になります。トップバーのキーボードアイコンから次の表示モードを選択でき、設定は次回訪問時も維持されます。

- **Auto** — タッチ端末または画面幅 768 px 以下で表示（デフォルト）
- **On** — 常に表示
- **Off** — 常に非表示

`ctrl` トグルを有効にすると次の 1 文字に Ctrl 修飾を適用します。なお、この設定が切り替えるのは web-tmux の仮想キーツールバーであり、iOS / iPadOS 自体のソフトキーボードではありません。

iOS / iPadOS では、ソフトキーボードの上に鍵・カード・位置情報アイコンの AutoFill バー（入力アクセサリビュー）が出ます。これはパスワード欄に限った表示ではなく、フォーム要素にフォーカスが当たっていれば常に出るもので、xterm.js の隠し `<textarea>` にフォーカスするターミナルも対象になります。**ページ側からこれを消す方法はありません** — 上書きできるのはネイティブの `WKWebView` ホストが持つ `inputAccessoryView` だけで、端末設定にも無効化する項目はありません。`autocomplete="off"` は `static/app.js` で設定済みですが WebKit は無視します。実機（iOS 26）で `<form>` 囲み、`input type` 各種、`contenteditable`、`inputmode` 各種、画面外配置など 12 通りを試しましたが、どれでもバーは出たままでした。

iOS がタブを破棄したあと、Safari はそのタブを履歴ナビゲーションとして復元します。このとき WebKit は、ページの読み込みが完了していて保留中の要求が何も無くても「読み込み中」表示を残し続けます。そこで復元されたタブは、接続が確立した時点で通常のナビゲーションとして自分を読み込み直します（Service Worker がキャッシュから返すのでネットワークは使いません）。`static/app.js` の `RENAVIGATE_RESTORED_TABS = false` にすれば、読み込み表示は残りますが復元されたタブをそのまま使えます。

## 他のホストの tmux（SSH）

web-tmux は、他のマシンで動いている tmux にも接続できます。tailnet に参加していない LAN 内のマシンも、tailnet ノードを `ProxyJump` の踏み台にすれば対象にできます。ブラウザが話す相手はこれまでどおりこのサーバーだけで、サーバーが `ssh <alias> tmux -CC` を実行します。リモートの tmux はローカルと同じようにサイドバーに並び、ウィンドウ、ペイン、スクロールバック、レイアウト保存がそのまま使えます。ブラウザ、このサーバー、ssh 接続のどれが切れても、セッションはリモート側で動き続けます。

**1. 接続方法をサーバーユーザーの `~/.ssh/config` に書く。** 認証は次のどれでも使えます。

- パスフレーズなしの鍵（最も簡単。専用鍵を推奨）
- ssh-agent に登録した鍵（`server.sh` は、ログインシェルや `.web-tmux.env` に設定した `SSH_AUTH_SOCK` をそのまま渡します）
- パスフレーズ付きの鍵、またはパスワード（ブラウザで入力します。後述の「サインイン」を参照）

```
Host gw-home                      # LAN 内の tailnet ノード
  HostName gw-home.tailnet-name.ts.net
  User me
  IdentityFile ~/.ssh/web-tmux_ed25519
  IdentitiesOnly yes

Host lan-box                      # LAN のみ（tailnet 外）
  HostName 192.168.1.20
  User me
  ProxyJump gw-home
  IdentityFile ~/.ssh/web-tmux_ed25519
  IdentitiesOnly yes
```

**2. 一度手動で接続し**、両方のホップを `known_hosts` に登録します。web-tmux は未知のホスト鍵を受け入れない（`StrictHostKeyChecking=yes`）ため、未登録だと「到達不能」として扱われます。

```bash
ssh lan-box tmux -V
```

**3. `.web-tmux.env` にエイリアスを列挙して**再起動します。

```bash
WEB_TMUX_SSH_HOSTS=lan-box,mac-mini:/opt/homebrew/bin/tmux
```

使えるのはここに列挙したエイリアスだけです。ブラウザはこの中から選ぶだけで、任意のホストを指定することはできません。`alias:/path` は、非対話の ssh ログインの PATH に tmux が無い場合（macOS の Homebrew など）に tmux のパスを指定します。ブラウザからのサインインには、このマシンに OpenSSH 8.4 以上が必要です。

**使い方。** ホストを設定すると、セッション一覧がホストごとにまとまります。各ホストには名前の後ろに状態ドット、そのホストに新規セッションを作る **+**、到達不能なホストを再確認する **↻** が付きます。他ホストのセッションを選ぶと、サーバーの control client がそのホストに移ります。すべてのブラウザが同じ client を見ているので、開いている全ブラウザが一緒に移ります。リモートにいる間、トップバーには `server → alias` と表示されます。接続が切れて再接続できない場合は、ローカルのセッションに戻り、その旨を表示します。レイアウト保存では、応答したすべてのホストのセッションを記録します（応答しないホストは除外し、どのホストを除外したかを表示します）。復元では各セッションを元のホストに再構築します。別ホストへ自動で切り替えることはなく、到達できないホストのセッションは個別に失敗として報告します。

**サインイン。** パスフレーズやパスワードが必要なホストは、一覧に 🔑 が表示されます。🔑 をクリックするか、そのホストの **+** やセッションを選ぶと、操作したブラウザにだけ ssh 自身のプロンプト（`Enter passphrase for key …`、`me@lan-box's password:`）が表示されます。ProxyJump の踏み台でも必要なら、ホップごとに順に聞かれます。入力した文字列は WebSocket から `SSH_ASKPASS` 経由でそのまま ssh に渡され、保存もログ出力もされません。Cancel でサインインを中止できます。サインイン後の共有接続は最後の利用から 8 時間維持されるので、その間は再入力不要です（web-tmux は停止・再起動時に ssh 接続を閉じます。強制終了などで残った接続も、次の起動時に閉じます）。セッション一覧の背景更新がプロンプトを出すことはなく、パスワードが必要なホストは誰かがサインインするまで 🔑 のままです。

**切断。** 接続中のリモートホストには、切れたリンクの形の切断ボタンも付きます。押すとそのホストへの共有 ssh 接続を閉じます。control client がそのホストにいる場合は、先に全ブラウザをローカルのセッションに戻します。ホスト上の tmux セッションはそのまま動き続けます。切断したホストは灰色のドットになり、リンクの形の接続ボタンを押して接続するまで（必要ならパスフレーズやパスワードを聞きます）背景での確認も行いません。切断状態は web-tmux を再起動すると解除されます。

**リモート側の要件：** tmux 3.2 以上（3.2/3.3 では prefix キーは tmux のバインドとして扱われず、そのままペインに送られます）、POSIX 系のログインシェル（sh/bash/zsh。fish は不可）、UTF-8 が使える環境。接続時にリモート tmux サーバーのグローバルオプション `window-size latest` と `history-limit 50000` を設定します（ローカルと同じ）。

ホストへの ssh 呼び出しは 1 本の接続を共有します（`ControlMaster`、10 分間維持）。ソケットとホストごとの ssh ログは `~/.local/share/web-tmux/ssh/` に置かれます。

## 手動セットアップ

ここに書いたことは `./setup.sh` がすべて行います。この節では、手で設定する場合やあとから変更する場合のために、その中身を説明します。`./setup.sh` は何度でも実行でき、既存の `.web-tmux.env` には手を付けません。`./setup.sh --yes` を付けると、質問にすべて「はい」と答えます（スクリプトからの実行用）。

### Python 環境

[uv](https://github.com/astral-sh/uv) があれば、`setup.sh` は `uv.lock` から `uv sync --frozen` で同期します。なければ既存の Python 3.10+ と `venv` + `pip`、`requirements.txt` を使います。uv 自体をダウンロードすることはありません。ブラウザ端末のアセットは `static/vendor/` に同梱しているため、実行時に CDN 接続は不要です。

### `.web-tmux.env`

`setup.sh` は `tailscale status` から、このマシンの Tailnet 上の HTTPS アドレスと、マシンの所有者のログイン名を読み取ってこのファイルを書きます。手で書く場合は `.web-tmux.env.example` をコピーし、権限を600に保って次を設定します:

```dotenv
WEB_TMUX_ALLOWED_ORIGINS=https://<machine>.<tailnet>.ts.net:8766
WEB_TMUX_TAILSCALE_USERS=you@example.com
```

`server.sh` は権限が正確に600でない場合、`.web-tmux.env` を読み込まず起動を中止します。`WEB_TMUX_*` の設定はこのファイルからだけ読み、シェルの環境変数にあるものは無視するので、別のチェックアウト用に export した設定が混ざることはありません。Originが空の場合や、リモートOriginに対するTailscaleログイン名がない場合も起動を拒否します。localhostや `127.0.0.1` は追加せず、ブラウザからはTailscale Serve経由だけで利用してください。

| 設定名 | 内容 |
|--------|------|
| `WEB_TMUX_ALLOWED_ORIGINS` | 許可するHTTP Originのカンマ区切り完全一致リスト |
| `WEB_TMUX_TAILSCALE_USERS` | 許可する `Tailscale-User-Login` のカンマ区切り完全一致リスト |
| `WEB_TMUX_SSH_HOSTS` | 任意：他のマシンの tmux（[他のホストの tmux](#他のホストの-tmuxssh)を参照） |
| `WEB_TMUX_STATE_DIR` | 任意：保存したレイアウトの置き場所（デフォルト `~/.local/share/web-tmux`） |

別の人も使えるようにするには、その人のログイン名を `WEB_TMUX_TAILSCALE_USERS` に追加します。[タグ付き](https://tailscale.com/kb/1068/tags)のマシンでは所有者を判別できないため、`setup.sh` が尋ねます。`.web-tmux.env` を編集したらサーバーを再起動します:

```bash
./server.sh restart
```

### `tailscale serve`

web-tmux が使うローカルポートは `127.0.0.1:8766` の1つです。ページとそのファイルは HTTP で、端末は `/ws` の WebSocket で、同じポートから配信します。このポートを `tailscale serve` で Tailnet に HTTPS 公開すると、ブラウザは WebSocket に `wss://` で接続します。web-tmux は署名付きセッションCookieを発行する前に、ブラウザのOriginと `Tailscale-User-Login` ヘッダーを検証します。

```bash
tailscale serve --bg --https=8766 http://127.0.0.1:8766
```

> **Linux の場合：** `tailscale serve` には `sudo` が必要です（ユーザーを tailscaled のオペレーターにしていれば不要）。macOS では通常不要です。

`./server.sh` は起動時に `tailscale serve status` を読み（これには `sudo` は不要です）、この転送がなければ警告します。自分で確かめる場合:

```bash
tailscale serve status
```

```
https://<machine-name>.<tailnet>.ts.net:8766/ (tailnet only)
|-- / proxy http://127.0.0.1:8766
```

公開をやめるには:

```bash
tailscale serve --https=8766 off
```

### うまくいかないとき

- **「this address or Tailscale user is not allowed here」と表示される。** 理由は `server.log` に出ています。`host not allowed` は、開いたアドレスが `WEB_TMUX_ALLOWED_ORIGINS` にないことを、`tailscale user not allowed` は、あなたのログイン名が `WEB_TMUX_TAILSCALE_USERS` にないことを示します。`missing tailscale identity` は、リクエストが `tailscale serve` を通っていない（またはユーザーを持たないタグ付きデバイスから来た）ことを示します。
- **ページがまったく表示されない。** `./server.sh` が `server started` と表示したか、`tailscale serve status` に上の転送があるかを確認してください。
- **ページは出るが端末がつながらない。** 一度リロードしてください。更新前から開いていたタブは、古いページのまま動いていることがあります。
- `curl -I` に応答がない：web-tmux は `GET` しか受け付けません。`curl -s -o /dev/null -w '%{http_code}\n' <URL>` を使ってください。

### 2ポート構成からの更新

以前のバージョンは WebSocket をポート 8765 でも配信していました。更新したら、その転送を削除してください。残っている間は `./server.sh` が起動時に知らせます:

```bash
tailscale serve --https=8765 off
```

古いページを開いたままのタブは端末につながりません。一度リロードしてください。

### Tailscale Funnel による外部公開

**web-tmux では Funnel を有効にしないでください。** 本アプリは本人所有の非公開Tailnet専用です。インターネット公開と複数ユーザー向け認可はセキュリティモデルの対象外です。

## セキュリティ

- サーバーは `127.0.0.1` にのみバインドし、許可Originからlocalhostを除外することで、ブラウザアクセスをTailscale Serve経由に限定します。ただしループバック待受はホスト境界であってユーザー境界ではなく、別のローカルUnixユーザーはプロキシヘッダーを偽装できるため、信頼できないローカルユーザーがいない個人用マシンを前提とします。
- HTTPとWebSocketは許可済みHost／Originを必須とし、Tailnet接続では許可済みの `Tailscale-User-Login` も検証します。
- Tailscale IDの検証後、`GET /auth/session` がHttpOnly、SameSite=Strict、HMAC署名付きCookieを自動発行します（有効期限30日）。ページは初回ロード時に取得し、接続が安定してから更新し、ハンドシェイクが拒否されたとき（署名鍵が作り直されるサーバー再起動後など）に再取得します。モバイルのスリープ復帰を含む再接続ではCookieを再利用し、HTTPリクエストを一切出しません。Tailnet HTTPSではSecure属性も付きます。
- Origin欠落、Host不一致、Cookie不正、未許可ユーザーは、tmux状態を生成する前に拒否します。Tailscale IDヘッダーを持たないタグ付き端末も利用できません。
- WebSocketは最大8接続、1メッセージ64 KiB、入力1メッセージ8 KiBに制限され、圧縮は無効です。操作・入力レートと送信キューにも上限があります。
- CSP、フレーム埋め込み禁止、MIME sniffing防止、Referrer／Permissions PolicyをHTTPレスポンスに付与します。
- Service Worker（`static/sw.js`）がアプリ本体をキャッシュし、ブラウザが破棄済みタブを作り直すときに自分で出すドキュメント要求に応答します。この読み込みがネットワークを待つことはありません。`/auth/session` には一切触れず、キャッシュにはアプリ本体しか入りません（端末の内容はWebSocket側にしか流れず、そちらの認証はそのままです）。認可が外れたブラウザでもキャッシュされた画面は開けますが、接続はできません。
- `no-store` はCookieを発行する応答だけに付けます。アプリ本体は毎回再検証、`/vendor/` 配下のバージョン付きファイルはキャッシュ可能にしてあり、ブラウザがネットワークに戻らずページを復元できます（いずれも端末の内容や秘密情報を含みません）。
- 不正なOrigin、ID、メッセージ、レート超過は、端末入力やCookie値を含めず `server.log` に記録します。再接続時にはページが接続状態の短い診断行（`client ...`）も記録します。端末の内容は含みません。不要なら `static/app.js` の `CLIENT_DIAGNOSTICS = false` で止められます。
- 保存したレイアウトは `~/.local/share/web-tmux/layouts.json` に、モード700のディレクトリ内でモード600のファイルとして書き出されます。作業ディレクトリのパスを含むため機密として扱ってください。ネットワークには出ず、リポジトリにも含まれません。
- `tailscale serve status` が上記のtailnet-only公開1つだけになっていることを維持してください。
- `WEB_TMUX_SSH_HOSTS` を設定した場合、web-tmux に入れた人（許可済みTailscale ID、または盗まれたセッションCookie）は、列挙した全ホストと途中の踏み台ホストでシェルを得られます。tmux 自体が任意のコマンドを実行できるため `command=` による鍵の制限は効きません。ただし各リモートの `authorized_keys` に `restrict,pty,from="<このサーバーのtailnet IP>"` を付ければ、鍵の他所での利用やフォワーディングを防げます。ssh は agent／X11 フォワーディングを無効にし、設定済みのフォワーディングもすべて解除して実行します。ssh ソケットのディレクトリはモード700です。サインイン画面で入力したパスフレーズやパスワードは、認証済みの WebSocket からモード600のソケットを経て ssh に渡るだけで、ディスクにもログにも残りません。プロンプトに答えられるのはサインインを始めたブラウザだけです。ssh は制御端末なしで、サインイン以外のプロンプトをすべて拒否する askpass 付きで動きます。リモートのエラー文は（短く1行にして）ログに残しますがブラウザには送らず、端末の内容はログに残しません。

## サードパーティのブラウザアセット

- `xterm@5.3.0`
- `xterm-addon-fit@0.8.0`
- `xterm-addon-unicode11@0.4.0`
- `markdown-it@14.1.0`（同梱の依存パッケージのライセンスは、BSD-2-Clause の
  `entities` を含め、同じ場所の `THIRD_PARTY_LICENSES` にあります）

これらのアセットは MIT ライセンスで配布されています。ライセンスファイルは
`static/vendor/` 内の各 vendored ファイルの近くに同梱しています。

## ファイル構成

```
web-tmux/
├── server.py             # HTTP + WebSocket サーバー
├── web_security.py       # Origin、Host、ID、署名Cookieの検証
├── tmux_control.py       # tmux -CC 制御モードラッパー
├── askpass.py            # ssh のパスフレーズ／パスワードをブラウザで答える仕組み
├── tmux_hosts.py         # ローカル／ssh ホスト：tmux の argv、ssh マスター、ホスト一覧
├── layout_parser.py      # tmux レイアウト文字列パーサー
├── layout_store.py       # セッション構成の保存・復元計画・ストア
├── agent_status.py       # ペインの画面から AI エージェントの状態を判定
├── tests/                # ユニットテスト（.venv/bin/python -m unittest）
├── pyproject.toml        # Pythonプロジェクトと固定依存関係
├── uv.lock               # uv用ロックファイル
├── requirements.txt      # venv + pip用固定依存関係
├── .web-tmux.env.example # Tailnet設定例
├── tailscale_setup.py    # setup.sh と server.sh のために Tailscale の情報を読む
├── install.sh            # 1コマンドでのインストール・更新（clone して setup.sh を実行）
├── setup.sh              # Python 環境、.web-tmux.env、tailscale serve の設定
├── server.sh             # サーバーの起動・停止
└── static/
    ├── index.html
    ├── style.css
    ├── app.js
    ├── sw.js             # 復元されたタブへアプリ本体をオフラインで返す
    └── vendor/          # 同梱した xterm.js 実行時アセットとライセンス
```

保存したレイアウトはリポジトリの外、`~/.local/share/web-tmux/layouts.json` に置かれます
（保存先は `.web-tmux.env` の `WEB_TMUX_STATE_DIR` で変更できます）。

## テスト

`setup.sh` 実行後、プロジェクトの仮想環境でテストを実行します:

```bash
.venv/bin/python -m unittest -v
.venv/bin/python -m unittest tests.test_server   # 1モジュールだけ
```

テストは `tests/` にまとめてあり、web-tmux の実行には使いません。

## ログ

```bash
tail -f server.log
```

起動時に `server.log` は新しく作り直され、権限600になります。拒否理由は記録しますが、端末内容、入力データ、Cookie値は記録しません。
