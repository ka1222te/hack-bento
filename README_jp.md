# HackBento

社内研修・脆弱性再現用のオンプレミス環境プラットフォームです。
ユーザがゲスト OS イメージ（カーネル + rootfs）または Compose バンドルをアップロード・登録し、他のユーザがそれを選択して独立した Firecracker microVM 環境として起動できます。TryHackMe ライクな UX を完全オンプレ・内部ネットワーク完結で実現します。

> **本プロジェクトは、社内ネットワーク・プライベート IP 空間でのデプロイを前提として設計されています。
> インターネットに直接公開する用途は想定していません。**

HackBento は **microVM への直接 IP 付与**（Linux ブリッジ経由）を採用することで、ポートマッピングや Bastion ホストを必要とせず、ユーザが払い出された IP に直接 SSH できる設計を実現しています。

![HackBento Overview](assets/overview.png)

## 特徴

- **ワンクリック起動** — プロジェクトを選択してボタンを押すだけで Firecracker microVM が立ち上がり、IP アドレスが即座に払い出される
- **IP 直アクセス** — ユーザは払い出された IP に直接 SSH・curl・nmap できる（Bastion/VPN 不要）
- **Firecracker microVM（KVM必須）** — 全ての環境がカーネルレベルで分離された Firecracker microVM として起動する。物理ネットワーク上の IP は Linux ブリッジ + TAP インターフェース経由で直接付与される（ポートマッピング不要）。CPU・メモリ・プロセス数の上限はホスト側の cgroup v2 委譲ツリーで強制され、TAP anti-spoof（nftables）により、VM 内で root を取られても IP/MAC を書き換えてブリッジに出ることはできない
- **2種類の登録形式**:
  - **OSイメージ**（`rootfs_kind=os`） — ゲストカーネル + rootfs（ext4）。中で Docker を使わない1台構成（カーネル CVE 問題等）向け
  - **Compose バンドル**（`rootfs_kind=compose`） — `tools/hackbento-pack` でパッケージ化した複数コンテナの compose プロジェクトを VM イメージに焼き込み、ゲスト内の dockerd で実行する。VM の IP は1つで、`ports:` で公開したポートがそのまま待ち受ける
- **rootfs の tar→ext4 自動変換** — `docker export` 形式の単純な tar をアップロードすると、バックグラウンドで自動的に ext4 イメージへ変換
- **柔軟なタイムアウト設定** — タイムアウトの候補（分）を環境変数で設定可能。`0` を指定すると無制限として扱える
- **Attack Machine** — 各ユーザが自分専用の Kali Linux + XFCE microVM を起動し、ホーム画面に埋め込まれた GUI（Apache Guacamole / VNC）から直接操作できる
- **3種の認証** — ローカル認証 / LDAP / Google OAuth2 をサポート
- **プロジェクト管理** — GitHub ライクなオーナー/slug URL でプロジェクトを管理・共有。公開設定・コラボレーターにも対応

## ユースケース

- 社内セキュリティ研修（CTF 形式）
- CVE・脆弱性の PoC 再現環境の保存と共有
- フラグを仕込んだ脆弱マシンの作成・配布
- 再現性が保証されたサンドボックス環境の提供

## アーキテクチャ

```
【内部ネットワーク（例: 192.168.181.0/24）】

社内ユーザ端末
  │
  ├─ ブラウザ → HackBento Web UI（ライフサイクル管理）
  │
  └─ ssh user@192.168.181.200  ← IP直打ち（Bastion不要）
                │
                ▼
         Firecracker microVM（bridgeネットワーク経由でIP直付与・KVM必須）

【Webサーバの立ち位置】
  HackBento ──Firecracker REST API──► VM ランタイム
  （管理APIのみ）                       │
                                      ├─ env-001: 192.168.181.200
                                      ├─ env-002: 192.168.181.201
                                      └─ env-003: 192.168.181.202
```

## 技術スタック

| 項目 | 内容 |
|---|---|
| バックエンド | Python 3.12 + FastAPI |
| フロントエンド | Jinja2 テンプレート + バニラ JS |
| DB | SQLite（aiosqlite 非同期）|
| 認証 | JWT（Cookie + Bearer）/ LDAP / Google OAuth2 |
| 環境ランタイム | Firecracker microVM + Linux bridge ネットワーク（KVM必須）|
| GUIアクセス（Attack Machine） | Apache Guacamole（HackBento自身がリバースプロキシ）|
| デプロイ | Docker Compose（network_mode: host）|

## 必要要件

- Linux ホスト
- Docker Engine / Docker Compose
- KVM（`/dev/kvm`、BIOSでSVM/VT-x有効化）— 全環境（Firecracker microVM）に必須
- ホスト上に Linux ブリッジを作成できること（`iproute2`・`netplan`・systemd）

## セットアップ

### 1. リポジトリを取得

```bash
git clone <repository-url> hack-bento
cd hack-bento
```

### 2. セットアップスクリプトを実行

対話式スクリプト `setup.sh`（要 root 権限）が、ネットワーク構成から `.env`/`docker-compose.override.yml` の生成までを自動化します。

```bash
sudo ./setup.sh
```

スクリプトが行うこと:

- `/dev/kvm` の有無と bridge 作成可否をチェック（どちらも必須。使えない場合はエラー終了）
- 物理 NIC・サブネット・ゲートウェイ・DNS の検出/聞き取り
- ホスト OS 側に Linux ブリッジ（`vmbr-hackbento`）を作成して物理 NIC を接続し、ホストIPをブリッジへ移設、`netplan` で永続化（再起動後も維持）
- VM ごとのリソース上限（メモリ・PID数・CPU使用率）を強制するための cgroup v2 委譲ツリー（`/sys/fs/cgroup/hackbento-vms`）と、再起動時に再構成する systemd サービス（`hackbento-vm-cgroup.service`）をセットアップ
- `.env` の生成（`.env.example` からコピー）と `BRIDGE_NAME`・`FC_GUEST_GATEWAY`・`IP_POOL_START`/`IP_POOL_END` の設定
- Firecracker に必要なデバイス・cgroup マウント（`/dev/kvm`・`/dev/net/tun`・`NET_ADMIN`・`cgroup: host`）を含む `docker-compose.override.yml` を生成
- **Attack Machine**（Kali Linux + Guacamole GUI）を有効にするか対話的に確認。有効にした場合は `GUACAMOLE_JSON_SECRET_KEY` を生成し、`docker-compose.override.yml` に `guacd`/`guacamole` サービス定義を追記

> ネットワーク設定を変更したい場合は `docker compose -f docker-compose.yml -f docker-compose.override.yml down` の後に `sudo ./setup.sh` を再実行してください（`docker-compose.override.yml` を再生成します）。cgroup 委譲ツリーだけ再セットアップしたい場合は `sudo ./setup.sh --vm-cgroup-only` が使えます。

### 3. .env を確認・編集

最低限、以下を確認・設定してください。

```bash
# 公開ホスト名とポート
DOMAIN=your-server.example.com
PORT=8000

# JWT署名キー（必ず変更）
SECRET_KEY=$(openssl rand -hex 32)
```

IP プール（`IP_POOL_START`/`IP_POOL_END`）は `setup.sh` が検出したサブネット内のアドレス空間に自動設定されますが、必要に応じて調整してください。範囲外の IP を指定した場合、VM の起動に失敗します。

### 4. 起動

```bash
docker compose -f docker-compose.yml -f docker-compose.override.yml up -d
```

### 5. アクセス

```
http://<DOMAIN>:<PORT>/
```

初期管理者アカウント: `admin` / `admin`（初回起動時に自動作成）

**初回ログイン後すぐにパスワードを変更してください。**
パスワードの変更は右上のユーザアイコン → **ユーザ設定** から行えます（ローカルユーザのみ）。

### LDAP / Google OAuth2 ユーザの初回ログイン

LDAP または Google OAuth2 でログインした場合、初回のみユーザ名設定画面（`/setup-username`）に遷移します。
任意のユーザ名を設定すると通常のホーム画面に移動します。一度設定したユーザ名は変更できません。

## 認証設定

### LDAP（任意）

```bash
LDAP_ENABLED=true
LDAP_URI=ldap://ldap.example.com:389       # LDAP サーバの URI
LDAP_TOP_DOMAIN=dc=example,dc=com          # BaseDN
LDAP_USER_FILTER=(uid={username})          # ユーザ検索フィルタ
LDAP_OU_USER=people                        # ユーザ OU
LDAP_OU_GROUP=groups                       # グループ OU
```

ログイン画面に LDAP 接続状態バッジ（緑=到達可能・赤=不可）が表示されます。

### Google OAuth2（任意）

1. [Google Cloud Console](https://console.cloud.google.com/) で OAuth 2.0 クライアント ID を作成
2. 承認済みリダイレクト URI に以下を登録:
   ```
   http://<DOMAIN>/api/auth/oauth/google/callback        # ポート80の場合
   http://<DOMAIN>:<PORT>/api/auth/oauth/google/callback  # その他のポート（http）
   ```
3. `.env` に設定:

```bash
GOOGLE_OAUTH_ENABLED=true
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
OAUTH_ALLOWED_DOMAINS=example.com  # 空にすると全Googleアカウントを許可
```

## 主な機能

### プロジェクト管理

プロジェクトは全て Firecracker microVM として起動します。新規作成画面のトグルで選ぶ2種類の登録形式（`Image.rootfs_kind`）があります。

#### OSイメージ（`rootfs_kind=os`）

中で Docker を使わない1台構成（カーネル CVE 問題、OS に直接サービスを入れた VM 等）向け。

- ゲストカーネルと rootfs（ext4）をそれぞれ「アップロード」「URLリンク」「管理者が登録した共有デフォルト資産から選択」（`/admin` の「デフォルトVM資産」タブ）のいずれかで用意
- rootfs として `docker export` 形式の単純な tar をアップロードした場合、バックグラウンドで自動的に ext4 イメージへ変換されます（変換中はステータスバッジが表示され、完了まで環境の起動はブロックされます）
- カーネル exploit 問題向けに、プロジェクト別の追加カーネル起動引数（`nokaslr`/`nosmep`/`nosmap`/`kpti=0` 等）を設定できる

#### Compose バンドル（`rootfs_kind=compose`）

中で Docker を動かし、複数コンテナ（Web + DB + LB 等）を1台の VM・1つの IP で提供する場合向け。

- 作成者が手元で `tools/hackbento-pack --bundle <dir>`（root不要）を実行し、compose プロジェクトをバンドル tar にパッケージ化
- 新規作成画面でアップロードすると、バックグラウンドワーカーがネットワーク分離されたビルド用 VM の中でバンドルを VM イメージへ焼き込み、`docker compose config` の出力を検査する（macvlan/ipvlanネット・外部ネット・固定IP・未収録イメージ等は拒否）
- 本番起動時はゲストが dockerd を起動し `docker compose up -d` を実行する

共通のプロジェクト設定:

- `/{owner}/{slug}` 形式の URL でプロジェクトにアクセス
- 公開設定: `public`（全員）/ `protected`（ログイン済み）/ `private`（オーナー・管理者・コラボレーターのみ）
- コラボレーター: `Read`（閲覧・起動）/ `Read-Write`（閲覧・起動・編集）

### 環境管理

- ワンクリックで環境を起動、IP アドレスが即座に払い出される
- タイムアウトの候補（分）は `TIMEOUT_MINUTES_OPTIONS`（カンマ区切り）で設定可能。`0` は無制限を表す特別値で、負値・数値に変換できない値は無視される
- 残り `TIMEOUT_WARNING_MINUTES` 分で警告表示（無制限環境では表示されない）
- 1 ユーザあたり最大 `MAX_ENVS_PER_USER` 環境、システム全体で最大 `MAX_ENVS_TOTAL` 環境（管理者は両方の上限を受けない）
- 各環境にCPU・メモリ・プロセス数の上限を、ホスト側の cgroup v2 委譲ツリーで強制し、悪意あるイメージ/VM によるホストへの影響を軽減
- バックグラウンドの watchdog が30秒ごとに期限切れ・クラッシュした環境を検知して自動的に後始末する

### Attack Machine

`ATTACK_MACHINE_ENABLED=true` の場合、各ユーザが自分専用の Kali Linux + XFCE microVM を（1ユーザにつき同時に1台まで）ホーム画面から起動し、Apache Guacamole による埋め込みGUIから操作できます（Guacamoleへの通信は HackBento 自身がリバースプロキシするため、Guacamoleのポートを外部に公開する必要はありません）。

### 管理画面（`/admin`）

- **起動中の環境**: 全ユーザの環境一覧・強制停止
- **ユーザ管理**: ローカルユーザ追加・有効化/無効化・ロール変更・パスワードリセット・削除
- **プロジェクト管理**: 全プロジェクト一覧・削除
- **デフォルトVM資産**: プロジェクト作成時に選択できる共有のゲストカーネル・rootfsを登録・管理。rootfsはtarアップロード時に自動でext4へ変換される

## 環境変数一覧

| 変数 | デフォルト | 説明 |
|---|---|---|
| `APP_TITLE` | `HackBento` | Web UI に表示されるアプリ名 |
| `DOMAIN` | `localhost` | 公開ホスト名 |
| `SCHEME` | `http` | `http` または `https` |
| `PORT` | `8000` | リッスンポート |
| `SECRET_KEY` | （要変更）| JWT 署名キー |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `480` | JWT の有効期限（分）|
| `DATABASE_URL` | SQLite | DB 接続 URL |
| `DEFAULT_TIMEOUT_MINUTES` | `60` | 環境のデフォルトタイムアウト（分）|
| `TIMEOUT_MINUTES_OPTIONS` | `15,30,45,60,75,90,105,120` | 選択可能なタイムアウト候補（分・カンマ区切り）。`0` は無制限を表す。負値・数値でない値は無視され、空になった場合は既定候補にフォールバック |
| `TIMEOUT_WARNING_MINUTES` | `10` | タイムアウト警告を出す残り時間（分）|
| `MAX_ENVS_PER_USER` | `2` | 1 ユーザあたりの最大同時起動数（管理者は対象外）|
| `MAX_ENVS_TOTAL` | `20` | システム全体の最大同時起動数（管理者は対象外）|
| `VM_CPU_LIMIT` | `1` | 1 環境あたりの CPU 上限（コア）|
| `VM_MEMORY_LIMIT_MB` | `1024` | 1 環境あたりのメモリ上限（MB）|
| `VM_DISK_LIMIT_GB` | `10` | 1 環境あたりのディスク上限（GB）|
| `VM_PIDS_LIMIT` | `256` | 1 環境あたりの最大プロセス数（fork bomb 対策）|
| `UPLOAD_IMAGE_MAX_MB` | `6144` | イメージ/バンドルファイルのアップロードサイズ上限（MB）|
| `UPLOAD_README_MAX_MB` | `10` | README ファイルのアップロードサイズ上限（MB）|
| `BRIDGE_NAME` | `vmbr-hackbento` | TAP を接続するホストブリッジ名（`setup.sh` が作成）|
| `FC_GUEST_GATEWAY` | `192.168.180.1` | ゲストにルーティングするゲートウェイ（ホストのブリッジIP）|
| `FC_GUEST_DNS` | `8.8.8.8,8.8.4.4` | ゲストに設定するDNSサーバ（カンマ区切りで複数指定可）|
| `IP_POOL_START` | `192.168.181.200` | IP プール開始アドレス（subnet 内に収めること）|
| `IP_POOL_END` | `192.168.181.230` | IP プール終了アドレス（subnet 内に収めること）|
| `ATTACK_MACHINE_ENABLED` | `false` | Attack Machine 機能（Kali Linux + Guacamole GUI）の有効化 |
| `ATTACK_MACHINE_CPU_LIMIT` | `2` | Attack Machine の CPU 上限（コア）|
| `ATTACK_MACHINE_MEMORY_LIMIT_MB` | `2048` | Attack Machine のメモリ上限（MB）|
| `ATTACK_MACHINE_TIMEOUT_MINUTES` | `60` | Attack Machine のタイムアウト（分）。`0` は無制限。初回起動時に一度だけ読み込まれ、以後固定される |
| `GUACAMOLE_JSON_SECRET_KEY` | （Attack Machine有効時は要設定）| Guacamole自動ログイントークンの署名に使うAES鍵。`setup.sh`が生成 |
| `LDAP_ENABLED` | `false` | LDAP 認証の有効化 |
| `LDAP_URI` | `ldap://...` | LDAP サーバの URI |
| `LDAP_TOP_DOMAIN` | `dc=example,dc=com` | BaseDN |
| `LDAP_USER_FILTER` | `(uid={username})` | ユーザ検索フィルタ |
| `LDAP_OU_USER` | `people` | ユーザ OU |
| `LDAP_OU_GROUP` | `groups` | グループ OU |
| `GOOGLE_OAUTH_ENABLED` | `false` | Google OAuth2 の有効化 |
| `GOOGLE_CLIENT_ID` | （空）| Google OAuth2 クライアント ID |
| `GOOGLE_CLIENT_SECRET` | （空）| Google OAuth2 クライアントシークレット |
| `OAUTH_ALLOWED_DOMAINS` | （空=全許可）| 許可するメールドメイン（カンマ区切り）|

詳細は `.env.example` を参照してください。

## https 化

`SCHEME=https` に設定すると、uvicorn が直接 TLS を終端します（nginx 等のリバースプロキシは不要）。

```bash
# .env
SCHEME=https
PORT=443
DOMAIN=your-server.example.com
```

起動時に `/data/certs/server.crt` と `/data/certs/server.key` が存在しない場合は自己署名証明書が自動生成されます。
正式な証明書を使う場合はこのパスに配置してください。

Google OAuth2 を使う場合は Google Cloud Console のリダイレクト URI も `https://...` に更新してください。

## やらないこと（理由）

- SSH の仲介・Webターミナル
- 公開鍵の注入（CTF 的に認証情報は問題の一部となるべきであるため）
- VPN 管理（内部ネットワーク完結であるため）
- フラグ提出・正誤判定（スコープ外）

## 想定デプロイ環境・利用上の注意

本プロジェクトは以下の環境でのデプロイを想定して設計されています。

- **社内ネットワーク・プライベート IP 空間**（RFC 1918 アドレス: 10.x.x.x / 172.16-31.x.x / 192.168.x.x）
- **インターネットから隔離されたクローズドネットワーク**

HackBento は意図的に脆弱なゲストイメージを扱うため、公開ネットワークに露出させると第三者による不正アクセスのリスクがあります。

## 貢献

バグや不具合を発見した場合は、[Issue](../../issues) にてご報告いただけると幸いです。

## 免責事項

本ソフトウェアの使用、または使用不能から生じるいかなる損害（データの損失、システムの障害、セキュリティインシデント等を含む）についても、作者は一切の責任を負いません。

本ソフトウェアを使用する場合は、利用者自身の責任において適切な環境・権限のもとで行ってください。

## サードパーティソフトウェア

HackBento 自体は GPL-3.0 で配布していますが、ビルド時・実行時にいくつかのサードパーティ OSS を利用しています。これらは本リポジトリのソースとして同梱・再配布しているのではなく、Docker ビルド時またはコンテナ起動時に各公式の配布元からダウンロード・プルして使用しているもので、それぞれ独自のライセンスが適用されます。

| コンポーネント | ライセンス | 補足 |
|---|---|---|
| [Firecracker](https://github.com/firecracker-microvm/firecracker) | Apache License 2.0 | microVM ランタイム本体。`orchestrator/Dockerfile` で公式リリースバイナリをダウンロードして使用 |
| [Apache Guacamole](https://guacamole.apache.org/)（`guacamole`・`guacd`） | Apache License 2.0 | Attack Machine の埋め込みGUIに使用。公式イメージ `guacamole/guacamole`・`guacamole/guacd` をベースにしている（`docker/guacamole/Dockerfile` 参照） |
| [guacamole-auth-json](https://guacamole.apache.org/doc/gug/json-auth.html) | Apache License 2.0 | 上記イメージに組み込むGuacamole拡張。パスワード不要のトークン自動ログインに使用 |
| Python依存ライブラリ（FastAPI・SQLAlchemy・Jinja2・`ldap3`・Authlib・httpx 等） | 各種（多くはMIT/BSD/Apache-2.0。`ldap3` は LGPL-3.0）| 詳細は `orchestrator/requirements.txt` を参照。再配布する場合は各パッケージのライセンスを個別に確認すること |
| Kali Linux / Debian ベースイメージ（Attack Machine の rootfs） | 各ディストリビューションのライセンスに準拠 | 初回起動時に自動ビルドされるもので、本リポジトリには同梱していない |

Apache License 2.0 は、これらのコンポーネントを HackBento 自身の GPL-3.0 コードと並行して利用・集約する用途において GPL-3.0 と互換性があります。HackBento のソースコードは Firecracker・Guacamole のソースを派生・組み込んだものではありません。ビルド済みの HackBento イメージを再配布する場合は、同梱される各コンポーネントが求めるライセンス表示（Guacamole/Firecracker に同梱される `NOTICE` ファイル等）を保持してください。

## ライセンス

[GNU General Public License v3.0](LICENSE)

Copyright (C) 2026 ka1222te

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
