# HackBento

社内研修・脆弱性再現用のオンプレミス環境プラットフォーム。  
Docker イメージをアップロード・登録し、ユーザが独立したコンテナ環境を即座に起動できる TryHackMe ライクなシステムです。完全オンプレ・内部ネットワーク完結で動作します。

## 特徴

- **ワンクリック起動** — Docker イメージを選択してボタンを押すだけで環境が立ち上がり、IP アドレスが払い出される
- **IP 直アクセス** — ユーザは払い出された IP に直接 SSH・curl・nmap できる（Bastion/VPN 不要）
- **macvlan ネットワーク** — コンテナに物理ネットワーク上の IP を直接付与（ポートマッピング不要）
- **タイムアウト管理** — 一定時間で環境を自動削除、延長ボタンで 60 分リセット
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
  └─ ssh user@192.168.181.200  ← IP直打ち（Bastion不要）
                │
                ▼
         Dockerコンテナ or microVM
         （macvlan経由でIPを直付与）

【Webサーバの立ち位置】
  HackBento ──Docker API──► コンテナランタイム
  （管理APIのみ）              │
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
| コンテナ基盤 | Docker + macvlan ネットワーク |
| デプロイ | Docker Compose（network_mode: host）|

## 必要要件

- Linux ホスト（物理サーバ推奨）
- Docker Engine / Docker Compose
- macvlan ドライバが使える NIC
- Python 3.12+（コンテナ内で完結するため不要）

> KVM が利用可能な場合は将来的に SmolVM + Firecracker バックエンドに切り替えてカーネルレベル分離を実現できます。

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

# macvlan IPプール（docker-compose.yml の ip_range 内に収めること）
IP_POOL_START=192.168.181.200
IP_POOL_END=192.168.181.230
```

### 3. macvlan ネットワークの設定

`docker-compose.yml` の `parent` をホストの実際の NIC 名に変更します。

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
        - subnet: 192.168.176.0/20
          gateway: 192.168.180.1
          ip_range: 192.168.181.192/27
```

### 4. 起動

```bash
docker compose up -d
```

### 5. アクセス

```
http://<DOMAIN>:<PORT>/
```

初期管理者アカウント: `admin` / `admin`（**初回ログイン後すぐにパスワードを変更してください**）

## 認証設定

### LDAP

```bash
LDAP_ENABLED=true
LDAP_URI=ldap://ldap.example.com:389
LDAP_TOP_DOMAIN=dc=example,dc=com
LDAP_BIND_PASSWORD=your-admin-password
LDAP_USER_FILTER=(uid={username})
LDAP_MAIL_DOMAIN=example.com
LDAP_OU_USER=people
LDAP_OU_GROUP=groups
```

### Google OAuth2

1. [Google Cloud Console](https://console.cloud.google.com/) で OAuth 2.0 クライアント ID を作成
2. 承認済みリダイレクト URI に以下を登録:
   ```
   http://<DOMAIN>/api/auth/oauth/google/callback        # ポート80の場合
   http://<DOMAIN>:<PORT>/api/auth/oauth/google/callback  # その他のポート(http)
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

### 管理画面（`/admin`）

- **起動中の環境**: 全ユーザの環境一覧・強制停止
- **ユーザ管理**: ローカルユーザ追加・有効化/無効化・ロール変更・パスワードリセット・削除
- **プロジェクト管理**: 全プロジェクト一覧・削除

## 環境変数一覧

| 変数 | デフォルト | 説明 |
|---|---|---|
| `DOMAIN` | `localhost` | 公開ホスト名 |
| `SCHEME` | `http` | `http` または `https` |
| `PORT` | `8000` | リッスンポート |
| `SECRET_KEY` | （要変更）| JWT 署名キー |
| `DATABASE_URL` | SQLite | DB 接続 URL |
| `DEFAULT_TIMEOUT_MINUTES` | `60` | 環境のデフォルトタイムアウト（分）|
| `MAX_ENVS_PER_USER` | `2` | 1 ユーザあたりの最大同時起動数 |
| `MAX_ENVS_TOTAL` | `20` | システム全体の最大同時起動数 |
| `VM_CPU_LIMIT` | `1` | 1 環境あたりの CPU 上限（コア）|
| `VM_MEMORY_LIMIT_MB` | `1024` | 1 環境あたりのメモリ上限（MB）|
| `VM_BACKEND` | `docker` | `docker` または `firecracker` |
| `MACVLAN_NETWORK` | `hackbento-vm` | macvlan ネットワーク名 |
| `IP_POOL_START` | `192.168.181.200` | IP プール開始アドレス |
| `IP_POOL_END` | `192.168.181.230` | IP プール終了アドレス |
| `LDAP_ENABLED` | `false` | LDAP 認証の有効化 |
| `GOOGLE_OAUTH_ENABLED` | `false` | Google OAuth2 の有効化 |
| `OAUTH_ALLOWED_DOMAINS` | （空=全許可）| 許可するメールドメイン（カンマ区切り）|

詳細は `.env.example` を参照してください。

## https 化

`SCHEME=https` にする場合は以下も必要です。

1. nginx 等でTLS終端してアプリにリバースプロキシ
2. Google Cloud Console のリダイレクト URI を `https://...` に更新

## やらないこと（今後の展望）

- SSH の仲介・Webターミナル（ユーザが IP に直接 SSH）
- 公開鍵の注入（CTF 的に認証情報は問題の一部であるべき）
- VPN 管理（内部ネットワーク完結）
- フラグ提出・正誤判定（スコープ外）

## ライセンス

[GNU General Public License v3.0](LICENSE)

Copyright (C) 2026 ka1222te

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
