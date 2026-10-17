# HackBento 改修プラン — Firecracker 一本化と Compose バンドル導入

本書は、以下の意思決定に基づく実装計画である。実装はこの計画に沿って段階的に進める。

**確定した方針**
- Compose バンドルの ext4 組み立ては **ビルド用 VM の中**で行う（信用できない入力をホスト上で展開しない）。
- macvlan バックエンドは **完全削除**（コードから一掃し、Firecracker 一本に絞る）。
- セキュリティ強化（jailer、TAP の IP/MAC 制限）は **compose 土台と並行〜早期**に入れる。

**設計の背骨（全フェーズ共通の原則）**
> プロジェクト作成者の設定ミスも、VM 内で root を取った参加者も、**ホスト側（orchestrator / nftables / cgroup / ビルド用 VM / jailer）で封じ込める**。README や「お願い」で守らない。信用できない入力（アップロードされた tar / rootfs）は、ホスト上で直接展開・解釈しない。

---

## 全体像

```
現状                              目標
┌─────────────┐                 ┌───────────────────────────────┐
│ macvlan or  │                 │ Firecracker 一本               │
│ bridge      │   ──────────►   │  ├─ OS イメージ(ext4/tar)       │
│ (二者択一)   │                 │  ├─ Compose バンドル(tar)        │
└─────────────┘                 │  │    └─ ビルド用VMで ext4 化    │
                                 │  └─ jailer + TAP制限 で封じ込め  │
                                 └───────────────────────────────┘
```

プロジェクト作成者から見た最終的な体験:
1. 手元で `docker compose up` して動作確認
2. `hackbento-pack --bundle .` でバンドル tar を作る（root 不要）
3. HackBento で「Compose バンドル」としてアップロード
4. サーバがビルド用 VM で ext4 を組み立て、`ready` になったら起動可能

---

## フェーズ構成（実装順）

各フェーズは独立してデプロイ可能な単位にする。前フェーズが動く状態を壊さずに積み上げる。

| # | フェーズ | 目的 | 依存 |
|---|---|---|---|
| 0 | 準備・退避 | 現状把握、テスト基盤、ロールバック手段 | なし |
| 1 | macvlan 完全削除 | Firecracker 一本化、コード単純化 | 0 |
| 2 | TAP の IP/MAC 制限（nftaband） | なりすまし・踏み台化を封じる | 1 |
| 3 | jailer による Firecracker 封じ込め | VM 脱出時の被害極小化 | 1 |
| 4 | ビルド用 VM 基盤 | 信用できない入力を VM 内で処理する土台 | 1 |
| 5 | docker-base.ext4 の作成 | compose を載せる共通土台 | 1 |
| 6 | Compose バンドル取り込み | バンドル → ext4 組み立てパイプライン | 4,5 |
| 7 | UI（入力形式の分岐・状態表示） | 作成者の導線 | 6 |
| 8 | hackbento-pack とドキュメントページ | 作成者のツールと手順 | 6 |
| 9 | 堅牢化・運用（残りの守りの穴） | クラッシュ検知・起動引数・容量制御 等 | 全般 |

セキュリティ（2,3）を土台（4〜6）と並行で進められるよう、1 の直後に 2・3 を着手する。

---

## フェーズ 0: 準備・退避

**目的**: 大規模改修の前に、戻れる状態と確かめる手段を用意する。

- [ ] 現状の `git` ブランチを切る（例: `feature/firecracker-only`）。各フェーズを個別コミットに。
- [ ] bridge モードで現在正常に VM が起動することを手で確認（基準点）。
- [ ] 既存の DB に macvlan プロジェクト / 環境が存在するか確認（`Image.backend == 'macvlan'` の件数）。完全削除するため、対象データの扱い（論理削除 or マイグレーションで除去）を決める。
- [ ] 最小の動作確認手順を文書化（provision 済みの小さな rootfs を1つ起動 → SSH 到達確認）。これを各フェーズ後の回帰確認に使う。

**成果物**: 作業ブランチ、基準点の確認メモ。

---

## フェーズ 1: macvlan 完全削除 ✅ 完了（commit 9d75f31）

**目的**: `VM_BACKEND` 分岐を廃し、Firecracker 専用に単純化する。コードの見通しを良くしてから機能追加する。

**実施メモ**: `backend`/`oci_ref`/`archive_path` はマイグレーションで drop せず、後方互換の未使用カラムとして温存した（既存 DB の NOT NULL 制約を壊さないため。model 側は `backend` を plain String・既定 `bridge` に変更し enum を撤去）。既存 DB は macvlan プロジェクト0件・起動中環境0件を確認済み。

**変更箇所**
- [ ] `services/smolvm.py`
  - `_start_docker` / `_stop_docker` / `docker_load` / `_get_macvlan_container_ids` / `_cleanup_orphaned_docker_vms` を削除。
  - `start_vm` / `stop_vm` の `VM_BACKEND` 分岐を削除し、Firecracker 経路に直結（[smolvm.py:823-840](../orchestrator/services/smolvm.py#L823)）。
  - `cleanup_orphaned_vms` の macvlan 分岐を削除。
- [ ] `config.py`: `VM_BACKEND` / `MACVLAN_NETWORK` を削除（または `VM_BACKEND` は撤去）。
- [ ] `models.py`: `Backend` enum と `Image.backend` の扱いを決める。
  - 推奨: `backend` カラムは **残さず削除**（マイグレーションで drop）。全プロジェクトが Firecracker 前提になるため。
  - 既存 macvlan プロジェクトはマイグレーション時に削除 or 無効化。
- [ ] `routers/images.py`: `host_backend` フィルタを削除。
- [ ] `routers/envs.py`: backend ミスマッチの 400 チェック（`envs.py:97` 付近）を削除。
- [ ] `routers/admin.py`: デフォルト資産まわりで backend 前提があれば整理。
- [ ] `services/network.py`: macvlan 実使用 IP 確認（`allocate_ip` の macvlan 分岐）を削除し、bridge 経路のみに。
- [ ] `setup.sh`: バックエンド選択の対話・macvlan 用 `docker-compose.override.yml` 生成経路・`ATTACK_MACHINE_ENABLED=false` 強制を削除。bridge セットアップのみに単純化。
- [ ] テンプレート: `new_project.html` / `edit_project.html` の backend 選択 UI を削除。
- [ ] `CLAUDE.md`: アーキテクチャ記述を Firecracker 一本に更新（ドキュメントの整合）。

**マイグレーション注意**
- SQLite は `ALTER TABLE DROP COLUMN` の対応がバージョン依存。`database.py` の既存マイグレーション方式に合わせる。難しければ当面カラムは残し、未使用化に留める判断も可。

**検証**: フェーズ 0 の回帰手順で VM 起動を確認。macvlan 関連の import / 設定が残っていないか grep。

---

## フェーズ 2: TAP の IP/MAC 制限（nftables）✅ 実装完了（要実機検証）

**目的**: VM が割り当て外の IP/MAC を名乗る・ARP/DHCP なりすまし・社内への無差別アクセスを、ホスト側で封じる。compose で特権コンテナや dind を許す前提なので、ここは早期に必須。

**実施メモ**:
- VM ごとに bridge family の専用テーブル `hb_<id8>` を作成（`prerouting` フック, priority -300）。`iif != tap accept` で自 TAP 以外は素通し、`ether saddr`/`arp saddr ip`/`ip saddr` が割り当て値と異なるフレームを drop。テーブル単位で `nft delete table` でき後始末が単純。
- `create_tap(vm_id, ip)` にシグネチャ変更（MAC は `ip_to_mac` で導出。smolvm の `_ip_to_mac` と同一規則を network.py に複製し循環 import を回避）。`delete_tap` が anti-spoof テーブルも除去。
- `cleanup_orphaned_vms` に `cleanup_orphaned_antispoof` を追加（起動時に残留 `hb_*` テーブルを一掃）。
- Dockerfile に `nftables` を追加。nft 不在/失敗時は警告にとどめ VM 起動はブロックしない（多層防御の方針）。
- nft ルールのグラマは host の `nft -c` でパース検証済み（netlink 実行は root=コンテナ内 NET_ADMIN で行われる）。
- `iif "name"`（挿入時に ifindex 解決・TAP 不在だと失敗）を `iifname "name"`（パケット毎の名前照合）へ変更。TAP 作成前後の順序依存と ifindex 再利用レースを回避。

**実機検証済み（コンテナ内・commit で反映）**:
- Phase 1/2 を載せた orchestrator イメージを再ビルド（`nftables` 入り確認）→ 起動ログ正常（macvlan 経路なし・IP プール検証 OK・lifespan の anti-spoof sweep がエラーなく実行）。`/health` OK、nft がコンテナ内で動作（NET_ADMIN）。
- `create_tap(vm_id, ip)` → TAP 作成＋`bridge hb_<id8>` テーブル生成（規則内容を確認）→ `delete_tap` で TAP・テーブルとも除去を確認。
- `cleanup_orphaned_antispoof`: 複数の残留テーブルを作り、active 指定したものだけ残し他を削除／空 active で全削除、を確認。
- **未実施（実 VM が要る範囲）**: 実ゲストから spoof フレームを送って実際に drop されること・正規 IP での SSH 到達の確認（API ログインに管理者資格が要るため、サービス層で代替検証した）。
- （未対応・将来）VM→外向きの許可リスト化、VM 間通信ポリシーは F 項として別途検討。

**設計**
- TAP ごとに nftables（bridge family）で ingress フィルタを適用。
  - 送信元 MAC ≠ `_ip_to_mac(ip)` → drop
  - ARP sender-ip ≠ 割り当て IP → drop
  - IPv4 送信元 ≠ 割り当て IP → drop
  - DHCP サーバ応答・不要な IPv6 RA → drop（必要に応じ）
- （任意・強め）VM → 外向きを許可リスト化: ゲートウェイ・DNS など必要な宛先のみ許可。社内への横移動を抑止。CTF 設計により調整。

**変更箇所**
- [ ] `services/network.py`: `create_tap` でルール投入、`delete_tap` で削除（[network.py:293-316](../orchestrator/services/network.py#L293)）。VM ごとに一意な nft table/chain 名（vm_id 由来）。
- [ ] `cleanup_orphaned_vms`: 孤立した nft ルールの掃除を追加。
- [ ] `orchestrator/Dockerfile`: `nftables` パッケージ追加。
- [ ] `setup.sh` / `docker-compose.override.yml`: ホスト nft を操作できる前提（`NET_ADMIN` は既にある）を確認。

**検証**: VM 内から `ip addr add` で別 IP を付与 → 外部へ到達しないことを確認。正規 IP での SSH は通ることを確認。

---

## フェーズ 3: Firecracker 脱出時の被害極小化（jailer は不採用 → docker.sock 隔離）✅ 実装・実機検証完了

**結論の変更**: jailer は**この環境では採用しない**。jailer v1.16 は起動時に必ず mount namespace を `unshare(CLONE_NEWNS)` し、これに `CAP_SYS_ADMIN` が必要。orchestrator コンテナは `CAP_SYS_ADMIN` を持たず（実機で `Failed to unshare into new mount namespace: EPERM` を確認）、付与すると `cgroup: host` + host networking と相まってコンテナがほぼホスト root 相当になり、jailer の保護目的（ホストへの被害抑制）を上回って逆効果。ユーザ判断で「jailer 見送り・docker.sock 隔離を優先」を選択。

**代わりに実施した被害極小化**:
- **docker.sock マウントを撤去**（`docker-compose.yml`）。orchestrator はホスト Docker デーモン（＝実質ホスト root）へ到達できなくなり、Firecracker 脱出や orchestrator 侵害時の被害が大幅に縮小。実機で「コンテナ内から `docker ps` 不可・`/var/run/docker.sock` 不在」を確認。
- docker.sock の唯一の用途は**デフォルト/Attack Machine rootfs の初回自動ビルド**（`firecracker_setup._build_default_rootfs`/`_build_attack_rootfs`）。資産が既に存在すれば未使用のため、定常運用でマウント不要。`_docker_available()` ガードを追加し、ソケット不在時はビルドのみ明確なメッセージで失敗（VM 起動には影響しない）。初回だけマウントを有効化して資産生成後に外す運用を compose にコメントで明記。
- **Firecracker の既定 seccomp フィルタは有効**（`--no-seccomp` を付けていない）。jailer 無しでも FC プロセスのシステムコールは制限される。これが FC 脱出に対する実効的な緩和。
- 実機検証: docker.sock 無しで orchestrator が正常起動（資産存在のため build 未実行・エラーなし）・`/health` OK・Phase2 anti-spoof と VM 起動パスに退行なし。

**将来（Phase 4-6 で解消）**: rootfs ビルドはビルド用 VM へ移行し、docker.sock を恒久的に不要化する。それまでは初回のみ一時マウントで対応。

---

## （旧）フェーズ 3: jailer による Firecracker 封じ込め（不採用・記録として保持）

**目的**: 万一 VM から脱出されても、Firecracker プロセスの権限を最小化し、ホスト（特に docker.sock）への到達を断つ。

**設計**
- Firecracker 直接起動を、公式 `jailer` 経由の起動に置き換える。
  - 専用の低権限 uid/gid、chroot（`<base>/firecracker/<vm_id>/root`）、cgroup、seccomp。
  - chroot 内に kernel（ハードリンク/バインド）、writable rootfs、API ソケットを配置。
  - ファイル所有者を jailer ユーザに合わせる。

**変更箇所**
- [ ] `services/smolvm.py`: `_start_firecracker` を jailer 起動へ改修（[smolvm.py:464](../orchestrator/services/smolvm.py#L464)）。socket パス・rootfs コピー先（`_prepare_writable_rootfs`）・cgroup（`_create_vm_cgroup` と jailer cgroup の役割整理）を chroot 前提に修正。
- [ ] `_stop_firecracker` / `cleanup_orphaned_vms`: chroot ディレクトリの掃除を追加。
- [ ] `orchestrator/Dockerfile` / `entrypoint.sh`: `jailer` バイナリ、jailer 用ユーザ作成。
- [ ] ホスト設定（推奨・別途）: `kvm_amd nested=0`、SMT 無効化等は運用ドキュメントに記載。docker.sock マウントの要否を再検討（不要なら外す）。

**検証**: 通常起動が成功すること。jailer ユーザから docker.sock が見えない（アクセス不可）ことを確認。

---

## フェーズ 4: ビルド用 VM 基盤

**目的**: 信用できない入力（tar）を、ホスト上ではなくネットワークなしの使い捨て Firecracker VM 内で展開・組み立てる共通基盤を作る。フェーズ 6 の前提。

**設計**
- `_start_firecracker` を汎用化し、以下を可能にする:
  - 追加ドライブ（ドライブ2 = バンドル/作業用）を `/drives/scratch` として attach。
  - TAP / network-interface を **省略**（ネットワークなし）。
  - boot_args に「ビルドモード」フラグを渡す（または init がドライブ2の存在で判定）。
  - 完了検知: VM が自力で電源断（init が `reboot`）するのを、Firecracker プロセス終了で検知。タイムアウト（例 15分）と cgroup 制限を付与。
- ビルド I/O は `run_in_executor` でブロッキングを退避（既存方針踏襲）。

**変更箇所**
- [ ] `services/smolvm.py`: 起動処理を「本番起動」と「ビルド起動」で共用できるよう引数化（drives リスト・network 有無・boot_args・終了待ち）。
- [ ] 新規 `services/image_build.py`（または既存 `rootfs_convert.py` を拡張）: ビルド用 VM の起動・完了待ち・結果回収を担うワーカー。

**検証**: ドライブ2に小さな tar を入れた VM を起動し、init が中身を読めること・電源断を検知できることを確認。

---

## フェーズ 5: docker-base.ext4 の作成

**目的**: compose を載せる共通土台 rootfs を1つ用意する。管理者が「デフォルト VM 資産」として登録し、全 compose バンドルで共有する。

**docker-base の中身**
- Debian + systemd（`/sbin/init`）
- docker-ce / containerd / docker compose plugin
- `iptables` を **legacy に固定**（ゲストカーネル 6.1 に nftables が無いため。[確認済み]）
- `/etc/docker/daemon.json`: `{"storage-driver":"overlay2","features":{"containerd-snapshotter":false}}`
- `hackbento-compose.service`:
  - 通常起動時: `docker compose --project-directory /opt/app -p <固定名> up -d --pull never`
  - ビルドモード時（ドライブ2あり）: `docker load` → compose 設定の正規化出力 → overlay 展開 → 電源断

**変更箇所**
- [ ] 新規 `tools/build-docker-base.sh`: docker-base.ext4 をホストで作る手順スクリプト（管理者用、再現可能に）。
- [ ] `models.py`: `DefaultRootfsAsset` に `kind`（`os` / `docker-base`）を追加。
- [ ] `routers/admin.py` / `admin.html`: デフォルト資産タブで docker-base を登録・一覧・削除できるように。
- [ ] `firecracker_setup.py`: 初回起動時に docker-base を自動構築 or 案内（`ensure_default_vm_assets` の拡張。失敗してもアプリ起動はブロックしない既存方針を踏襲）。

**検証**: docker-base から VM を起動し、中で `docker version` / `docker compose version` が通ること。legacy iptables で `docker run -p` が機能すること。

---

## フェーズ 6: Compose バンドル取り込み

**目的**: バンドル tar を受け取り、ビルド用 VM で ext4 を組み立て、`ready` にするパイプライン。

**バンドル仕様（format 1）**
```
my-ctf.bundle.tar(.zst)
 ├─ hackbento-bundle.json   {"format":1,"project_name":"my-ctf"}
 ├─ app/                    compose プロジェクト（相対マウント元含む）
 ├─ images.tar              docker save $(docker compose config --images)
 └─ overlay/                （任意）ゲストOSの / に重ねるファイル（フラグ・設定ミス）
```

**処理フロー**
```
アップロード
 │ ① 受付検査（ホスト・tarメタデータのみ、展開しない）
 ▼
pending ─► ② ビルド用VM(フェーズ4)で組み立て
 │           - docker-base をコピー・resize2fs(上限 VM_DISK_LIMIT_GB)
 │           - ドライブ2=バンドル で起動
 │           - init: app→/opt/app, docker load, overlay 展開, compose 正規化出力, 電源断
 ▼
③ 結果判定（ホスト・VMが書き出したcompose設定JSONにルール適用）
 ▼  ready / failed(理由)
```

**① 受付検査（展開せずメタデータで）**
- 圧縮サイズ・展開後合計サイズに上限。
- `hackbento-bundle.json` の存在と `format` 検証（バンドル判定の目印。tar 先頭に配置）。
- `app/` `images.tar` の存在。
- `app/` `overlay/` 内に絶対パス・`../`・外向き symlink・デバイスファイルが無いか（tar エントリ名/型の検査）。

**③ 結果判定（compose 正規化出力に対して）**
| 内容 | 判定 |
|---|---|
| macvlan/ipvlan ネットワーク、`ipv4_address`/`ipv6_address`、`external` ネットワーク | 拒否 |
| 参照イメージが images.tar に不在（かつ対応する build 成果物も無い） | 拒否（不足イメージ名を表示） |
| `privileged` / `network_mode: host` / docker.sock マウント | 許可（影響は VM 内に閉じる） |
| overlay が `/sbin/init`・起動サービスを上書き | 拒否 or 警告 |
| 公開ポート一覧 | 保存（UI 表示用） |

**変更箇所**
- [ ] `services/firecracker_setup.py`: `_detect_archive_kind` に `compose-bundle` 判定を追加（`hackbento-bundle.json` 目印）。[firecracker_setup.py:194](../orchestrator/services/firecracker_setup.py#L194)
- [ ] 新規 `services/bundle_build.py`（or `image_build.py`）: 受付検査・ビルド用 VM 呼び出し・結果判定。
- [ ] `services/rootfs_convert.py`: 既存変換ワーカーと状態機械（`RootfsConversionStatus`）を共有。pending→converting→ready/failed。
- [ ] `models.py`: `Image` に `rootfs_kind`(`os`/`compose`)、`base_rootfs_asset_id`、`compose_summary`(JSON)。
- [ ] zstd 対応が必要なら受付検査・展開を拡張（現 `tarfile` は zstd 非対応）。

**（並行）既存 OS イメージ経路の tar 展開もビルド用 VM に寄せる**
- [ ] `convert_tar_to_ext4`（ホスト上 `tar -xf`、[firecracker_setup.py:159-190](../orchestrator/services/firecracker_setup.py#L159)）を、ビルド用 VM 経由に移行。信用できない tar をホストで展開しない方針に統一。移行までの暫定として、`--no-absolute-names` 等の防御を先に入れてもよい。

**検証**: web+db+lb の3コンテナ構成バンドルを ready にし、起動 → ポートごとに各コンテナへ到達することを確認。overlay のフラグが VM の `/root` に存在することを確認。

---

## フェーズ 7: UI（入力形式の分岐・状態表示）

**目的**: 作成者の導線を「OS イメージ」と「Compose バンドル」に分ける。

**変更箇所**
- [ ] `new_project.html`: 「登録する形式」ラジオ（OS イメージ / Compose バンドル）。選択で入力欄を出し分け。
- [ ] Compose バンドル側: バンドルアップロード欄。カーネル・docker-base は自動選択（詳細設定で変更可）。
- [ ] `edit_project.html` / `project.html`: 状態バッジ（pending/converting/ready/failed）とポーリング自動更新（既存の仕組みを流用）。`compose_summary` から公開ポート・サービス一覧を表示。
- [ ] 形式と中身の不一致時の案内メッセージ（例: docker save の tar → バンドル化を案内）。
- [ ] `static/js/main.js`: フォーム出し分け・ポーリング。
- [ ] （任意）アップロード前のブラウザ側事前判定（tar 先頭数 KB の `hackbento-bundle.json` 検出。非圧縮時のみ）。

**検証**: 両形式で新規作成 → 失敗時の理由表示 → 成功時の起動まで一通り。

---

## フェーズ 8: hackbento-pack とドキュメントページ

**目的**: 作成者が手元でバンドル（または ext4）を作れるツールと、手順ページを提供。

**ツール `tools/hackbento-pack`（bash）**
- `hackbento-pack --bundle <dir>`: root 不要。`docker compose config` 検査 → `docker compose build/pull` → `docker save $(... --images)` → `app/`/`images.tar`/`overlay/`/`hackbento-bundle.json` を tar 化。`project_name` 固定。
- `hackbento-pack <dir>`（ext4 直作成・要 root・loop）: docker-base コピー → resize → chroot で docker-base の dockerd により `docker load`（バージョン一致）→ overlay 展開 → e2fsck。上級者向け。
- 検査ルールはフェーズ 6 ③ と一致させる（手元で先に気づける）。

**ドキュメントページ**
- [ ] プロジェクト作成画面に「rootfs の作り方」ページ/モーダルを追加。
  - OS イメージ（docker export → init 自作）の手順
  - Compose バンドル（`hackbento-pack --bundle`）の手順
  - overlay でフラグをゲスト OS に置く方法
  - カーネル CVE 問題の作り方（独自カーネル + 起動引数、フェーズ 9）
- [ ] `tools/` にサンプルバンドル（web+db+lb）と README。

**検証**: 作成者視点で、手元 compose → pack → アップロード → 起動が通ること。

---

## フェーズ 9: 堅牢化・運用（残りの守りの穴）

既出の「守りの穴」のうち未対応分を仕上げる。優先度順。

- [ ] **クラッシュ検知**: watchdog に Firecracker プロセス/コンテナの生存確認を追加。死亡時は停止・IP/枠解放。カーネル CVE 問題（意図的 panic）で特に重要。
- [ ] **プロジェクト別の起動引数**: `Image` に追加。`ip=`/`init=`/`console=` 等は上書き不可にホスト側で制限。カーネル exploit 問題用（nokaslr 等）。
- [ ] **プロジェクト別 CPU/メモリ上書き**: 現在ホスト共通。compose/Windows 代替等で不足するため、Image 単位で上限内上書き。
- [ ] **容量制御**: rootfs サイズ上限（展開後）、起動前のホスト空き容量確認、reflink での高速コピー検討。
- [ ] **cgroup memory.max の余裕**: ゲストメモリ + オーバーヘッド（+64MB 等）に。Firecracker 自体の OOM を防ぐ。
- [ ] **起動後ヘルスチェック**: 公開ポートの応答確認を状態表示に反映（任意）。
- [ ] **I/O・帯域制限**: TAP に tc、cgroup io コントローラ委譲（任意）。

---

## 横断的な確認事項（実装中に随時）

- マイグレーションは `database.py` の既存方式に従い、各フェーズで前方互換を保つ。
- 各フェーズ後にフェーズ 0 の回帰手順を実行。
- `cleanup_orphaned_vms` は毎フェーズで「増えたリソース種別（nft ルール、jailer chroot、ビルド用 VM、追加ドライブ）」の掃除を追補する。
- ドキュメント（`CLAUDE.md`）を各フェーズの実態に合わせて更新。

---

## 追加の懸念（見直しで洗い出した要練り込み事項）

初版の見直しで抽出した、各フェーズに織り込むべき追加論点。番号は該当フェーズ。

### A. ビルド用 VM と本番 VM の資源競合（フェーズ 4・6・9）
ビルド用 VM も Firecracker VM なので、放置すると本番環境と枠を奪い合う。
- [ ] **IP プールを消費させない**: ビルド用 VM はネットワークなし（TAP/NIC 省略）で起動するので `allocate_ip` を呼ばない設計にする。`cleanup_orphaned_vms` が「DB に無い VM」として誤検知しないよう、ビルド用 VM を別管理（`_fc_pids` とは別のレジストリ or ラベル）にする。
- [ ] **同時起動枠（`MAX_ENVS_TOTAL`/`MAX_ENVS_PER_USER`）と分離**: ビルドは Environment レコードを作らない。ただしビルド用 VM の同時実行数に独自上限（例: 同時2件）を設け、cgroup もビルド専用ツリーに入れて本番 VM のメモリ/CPU を圧迫しないようにする。
- [ ] **ビルドの直列化/キュー**: 既存の `rootfs_convert` ワーカーが `with_for_update()` で1件ずつ claim する方式を踏襲し、ビルド用 VM も同時多発しないようにする。

### B. Attack Machine と Firecracker 一本化の相互作用（フェーズ 1・3・5）
Attack Machine は既に Firecracker 専用機能だが、改修の影響を受ける。
- [ ] **jailer 化の影響（フェーズ 3）**: Attack Machine VM も `_start_firecracker` 経由で起動する。jailer 化すると chroot・所有者前提が変わるため、Attack Machine 経路も同時に検証する（VNC ポート 5901 への Guacamole からの到達性）。
- [ ] **Guacamole リバースプロキシとの整合（フェーズ 3）**: jailer で network namespace を分ける設計にすると、ホスト上の guacd から VM の VNC へ到達できなくなる恐れ。ネットワークは分離せず TAP のみ使う（フェーズ 2 の nft で守る）方針を明記し、Attack Machine の VNC 到達性を壊さないことを確認。
- [ ] **ビルド基盤の流用（フェーズ 5）**: `_build_attack_rootfs`（Kali ビルド）は現状独自実装。ビルド用 VM 基盤（フェーズ 4）に寄せられるか検討。少なくとも、docker-base ビルドと Attack rootfs ビルドで資源競合しないようにする。
- [ ] macvlan 削除後も `ATTACK_MACHINE_ENABLED` は bridge 前提のまま有効（削除対象は「macvlan 時に false 強制」する分岐のみ）。

### C. アップロードとディスクの実際的な上限（フェーズ 6・9）
- [ ] **バンドルは現行の単一ファイル上限（`UPLOAD_IMAGE_MAX_MB=6144`）を超えやすい**。`images.tar` を含むため。バンドル用に別の上限を設けるか、上限を見直す。アップロードはストリーミングでサイズ検査済み（[images.py:374](../orchestrator/services/../routers/images.py#L374)）だが、**展開後サイズ**の上限は別途必要。
- [ ] **ホスト全体のディスク予算**: 「docker-base サイズ + 各 ext4（組み立て済み）+ VM ごとの writable コピー + ビルド作業領域 + アップロード原本」が積み上がる。起動前・ビルド前にホスト空き容量を確認し、不足時は明示エラー。
- [ ] **アップロード原本の後始末**: ビルド成功後に原本 tar を削除する（現 `rootfs_source_archive_path` の掃除方式を踏襲）。失敗時の原本の扱いも決める。
- [ ] **writable rootfs コピーのコスト**: compose/overlay で rootfs が数 GB になると、VM 起動のたびの全体コピーが重い。reflink（`cp --reflink=auto`、ファイルシステムが対応する場合）や qcow2 バッキング相当の検討（フェーズ 9）。

### D. ビルドの再現性と失敗時の観測性（フェーズ 4・6）
- [ ] **ビルド用 VM はネットワークなし**＝ビルド中にイメージ pull 不可（意図どおり）。よって `images.tar` に全イメージが揃っている必要がある。受付検査で「compose 参照イメージ ⊆ images.tar」を**ビルド前に**確認し、不足を早期に失敗させる（VM を起動せずに弾ける）。
- [ ] **ビルドログの回収**: ビルド用 VM の中で `docker load`/`compose config` が失敗した場合の原因を、ドライブ2に書き出したログとして回収し `rootfs_conversion_error` に格納。ネットワークなしなので外部送信は不可、ドライブ経由が唯一の手段。
- [ ] **ビルド用 VM のハング対策**: Firecracker プロセスのハング時に無期限待ちしないよう、全体タイムアウト＋`_terminate_pid` の二重化（既存の `_fc_api` 方針と同じ思想）。

### E. バンドル検査の取りこぼし（フェーズ 6・7）
- [ ] **compose の正規化は docker-base の compose で行う**（バージョン差を吸収）。ホスト側の Python で compose を独自パースしない（書式差で誤判定する）。VM が出力した正規化 JSON にのみルールを当てる。
- [ ] **`build:` のみのサービス**: images.tar に該当イメージが無い場合、VM 内でビルドしようとしてネットワークなしで失敗する。受付/判定で「build のみ かつ 対応イメージ無し」を検出して拒否。
- [ ] **compose のプロジェクト名固定**: 手元ディレクトリ名と `/opt/app` でプロジェクト名がずれ、`build:` サービスのイメージ名解決が狂う問題（既出）。`hackbento-pack` が `name:` を固定で埋め、起動サービスも `-p <固定名>` と `--pull never` を使う。両者の名前を一致させる。
- [ ] **zstd 等の圧縮**: 現 `tarfile` は zstd 非対応（[firecracker_setup.py:194](../orchestrator/services/firecracker_setup.py#L194)）。許可する圧縮形式を明示し、受付検査もそれに対応させる。

### F. ネットワーク設計の細部（フェーズ 2）
- [ ] **ゲートウェイ/DNS への到達は許可リストに含める**: VM→外向きを絞る場合、`FC_GUEST_GATEWAY`・`FC_GUEST_DNS` への到達は残す。絞りすぎて正規の名前解決や SSH 応答経路を壊さない。
- [ ] **VM 間通信のポリシー**: 同一ブリッジ上の VM 同士が直接通信できてよいか（CTF 的に他人の環境へ到達させない方が安全）。nft で VM 間を遮断するか、少なくとも検討結果を明記。
- [ ] **nft ルールのアトミック適用と掃除**: VM ごとに独立した table を使い、起動失敗時・停止時・孤立時に確実に削除（フェーズ 1 のクリーンアップ追補と連動）。

### G. 設定・ドキュメントの同期（全フェーズ）
- [ ] `.env.example`・`config.py`・`setup.sh`・`CLAUDE.md` の4点を、macvlan 削除と新規設定（docker-base 資産、ビルド用 VM 上限、起動引数など）に合わせて同時更新する。片方だけ更新して乖離させない。
- [ ] 初期管理者/既存ユーザへの移行手順（macvlan プロジェクトが消えることの告知）をドキュメント化。

---

## リスクと留意点

- **docker-base とゲストカーネルの整合**: カーネル CVE 問題で独自カーネルを使う場合も、docker-base は legacy iptables に揃えておけば広く動く。nftables 前提ツールを使いたい場合のみ nftables 入りカーネルを別途用意。
- **ビルド用 VM の所要時間**: compose の規模次第で数分かかる。UI の進捗表示と妥当なタイムアウトが必要。
- **jailer 導入の影響範囲**: パス・所有者・cgroup の前提が広く変わる。フェーズ 3 は単独で十分に検証してから統合する。
- **既存データ**: macvlan 完全削除に伴う既存プロジェクトの扱いは、フェーズ 0 で確定してから着手する。
```
