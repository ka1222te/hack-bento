# HackBento

社内研修・脆弱性再現用のオンプレミス環境プラットフォーム。  
Docker イメージをアップロード・登録し、ユーザが独立したコンテナ環境を即座に起動できるシステムです。完全オンプレ・内部ネットワーク完結で動作します。

> **本プロジェクトは、社内ネットワーク・プライベート IP 空間でのデプロイを前提として設計されています。
> インターネットに直接公開する用途は想定していません。**

HackBento は **macvlan によるコンテナへの直接 IP 付与** を採用することで、ポートマッピングや Bastion ホストを必要とせず、ユーザが払い出された IP に直接 SSH できる設計を実現しています。

![HackBento Overview](assets/overview.png)

## 特徴

- **ワンクリック起動** — Docker イメージを選択してボタンを押すだけで環境が立ち上がり、IP アドレスが払い出される
- **IP 直アクセス** — ユーザは払い出された IP に直接 SSH・curl・nmap できる（Bastion/VPN 不要）。ただし、立ち上げる Docker イメージが SSH・curl・nmap に対応している必要があります。
- **macvlan ネットワーク** — コンテナに物理ネットワーク上の IP を直接付与（ポートマッピング不要）
- **タイムアウト管理** — 一定時間で環境を自動削除、延長ボタンで時間リセット
- **3種の認証** — ローカル認証 / LDAP / Google OAuth2 をサポート
- **プロジェクト管理** — GitHub ライクなオーナー/slug URL でプロジェクトを管理・共有

## ユースケース

- 社内セキュリティ研修（CTF 形式）
- CVE・脆弱性の PoC 再現環境の保存と共有
- フラグを仕込んだ脆弱マシンの作成・配布
- 再現性が保証されたサンドボックス環境の提供

## アーキテクチャ

```
【内部ネットワーク】

社内ユーザ端末
  │
  ├─ ブラウザ → HackBento Web UI（ライフサイクル管理）
  │
  └─ ssh user@192.168.180.200  ← IP直打ち（Bastion不要）
                │
                ▼
         Dockerコンテナ or microVM
         （macvlan経由でIPを直付与）

【Webサーバの立ち位置】
  HackBento ──Docker API──► コンテナランタイム
  （管理APIのみ）              │
                              ├─ env-001: 192.168.180.200
                              ├─ env-002: 192.168.180.201
                              └─ env-003: 192.168.180.202
```

## 技術スタック

| 項目 | 内容 |
|---|---|
| バックエンド | Python 3.12 + FastAPI |
| フロントエンド | Jinja2 テンプレート + バニラ JS |
| DB | SQLite（aiosqlite 非同期）|
| 認証 | JWT（Cookie + Bearer）/ LDAP / Google OAuth2 |
| コンテナ基盤 | Docker + macvlan ネットワーク |
| デプロイ | Docker Compose（network_mode: host）|

## 必要要件

- Linux ホスト
- Docker Engine / Docker Compose
- macvlan ドライバが使える NIC

> KVM が利用可能な環境に対して、将来的に SmolVM + Firecracker バックエンドに切り替えてカーネルレベルでの分離ができるよう実装予定。

## セットアップ

### 1. リポジトリを取得

```bash
git clone <repository-url> hack-bento
cd hack-bento
```

### 2. 環境設定ファイルを作成

```bash
cp .env.example .env
```

`.env` を編集して最低限以下を設定してください。

```bash
# 公開ホスト名とポート
DOMAIN=your-server.example.com
PORT=8000

# JWT署名キー（必ず変更）
SECRET_KEY=$(openssl rand -hex 32)

# macvlan IPプール
# IP_POOL_START / IP_POOL_END は docker-compose.yml の networks.vm_net で定義した
# subnet のアドレス空間内に収める必要があります。
# 範囲外の IP を指定した場合、Docker デーモンが docker run 時にエラーを返し
# コンテナの起動に失敗します（HackBento 側での事前検証は行っていません）。
IP_POOL_START=192.168.180.200
IP_POOL_END=192.168.180.230
```

### 3. macvlan ネットワークの設定

`docker-compose.yml` の `parent` をホストの実際の NIC 名に変更します。また、macvlanのサブネットとゲートウェイの設定を行います。

```bash
ip link show  # NIC名を確認
```

```yaml
# docker-compose.yml
networks:
  vm_net:
    driver: macvlan
    driver_opts:
      parent: eth0  # ← 実際のNIC名に変更
    ipam:
      config:
        - subnet: 192.168.176.0/20 # ← ホストOSで設定されているサブネットに変更
          gateway: 192.168.180.1 # ← ホストOSで設定されているデフォルトゲートウェイに変更
```

macvlan の設定（`parent`・`subnet`・`gateway`）を間違えた場合は、ネットワークを作り直す必要があります。

```bash
# ネットワークの再作成
docker compose down
docker network rm hackbento-vm 2>/dev/null || true
# docker-compose.yml を修正してから再起動
docker compose up -d
```

### 4. 起動

```bash
docker compose up -d
```

### 5. アクセス

```
http://<DOMAIN>:<PORT>/
```

初期管理者アカウント: `admin` / `admin`

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

- Docker イメージ（`.tar` / `.tar.gz` / `.tgz` / `.tar.zst`）をアップロード、または Docker Hub URL を指定して登録
- `/{owner}/{slug}` 形式の URL でプロジェクトにアクセス
- 公開設定: `public`（全員）/ `protected`（ログイン済み）/ `private`（限定）
- コラボレーター: `Read`（閲覧・起動）/ `Read-Write`（閲覧・起動・編集）

### 環境管理

- ワンクリックで環境を起動、IP アドレスが即座に払い出される
- デフォルト 60 分でタイムアウト、「延長する（+60分）」で延長可能
- 残り 10 分で警告表示
- 1 ユーザあたり最大 2 環境、システム全体で最大 20 環境（設定変更可能）
- 各コンテナにディスク上限・プロセス数上限・swap 無効化を適用し、悪意あるイメージによるホストへの影響を軽減

### 管理画面（`/admin`）

- **起動中の環境**: 全ユーザの環境一覧・強制停止
- **ユーザ管理**: ローカルユーザ追加・有効化/無効化・ロール変更・パスワードリセット・削除
- **プロジェクト管理**: 全プロジェクト一覧・削除

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
| `TIMEOUT_WARNING_MINUTES` | `10` | タイムアウト警告を出す残り時間（分）|
| `MAX_ENVS_PER_USER` | `2` | 1 ユーザあたりの最大同時起動数 |
| `MAX_ENVS_TOTAL` | `20` | システム全体の最大同時起動数 |
| `VM_CPU_LIMIT` | `1` | 1 環境あたりの CPU 上限（コア）|
| `VM_MEMORY_LIMIT_MB` | `1024` | 1 環境あたりのメモリ上限（MB）|
| `VM_DISK_LIMIT_GB` | `10` | 1 環境あたりのディスク上限（GB）|
| `VM_PIDS_LIMIT` | `256` | 1 環境あたりの最大プロセス数（fork bomb 対策）|
| `VM_BACKEND` | `docker` | `docker` または `firecracker` |
| `MACVLAN_NETWORK` | `hackbento-vm` | macvlan ネットワーク名 |
| `IP_POOL_START` | `192.168.181.200` | IP プール開始アドレス（subnet 内に収めること）|
| `IP_POOL_END` | `192.168.181.230` | IP プール終了アドレス（subnet 内に収めること）|
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

## 貢献

バグや不具合を発見した場合は、[Issue](../../issues) にてご報告いただけると幸いです。

## 免責事項

本ソフトウェアの使用、または使用不能から生じるいかなる損害（データの損失、システムの障害、セキュリティインシデント等を含む）についても、作者は一切の責任を負いません。

本ソフトウェアを使用する場合は、利用者自身の責任において適切な環境・権限のもとで行ってください。

## ライセンス

[GNU General Public License v3.0](LICENSE)

Copyright (C) 2026 ka1222te

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
