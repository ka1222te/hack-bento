# PostgreSQL 移行計画

## 背景・目的

HackBento は現在 SQLite（WALモード、aiosqlite 非同期ドライバ）を単一ファイルDBとして使用している。
SQLite は「書き込みはファイル全体で排他」という制約があり、以下の問題が確認されている。

- VM起動処理（`routers/envs.py::start_env`）は IP確保・ステータス更新などで複数回 DB
  書き込みトランザクション（`BEGIN IMMEDIATE`）を発行する。この間、ログイン
  （`POST /api/auth/login`）等の他の書き込みリクエストも同じファイルレベルの排他ロックを
  取り合うため、busy_timeout（10秒）の範囲内で待たされる、またはそれを超えると
  "database is locked" で失敗する。
- 2026-11-03 には、調査目的で本番DBファイルに直接 `sqlite3.connect()` した結果、
  SQLiteが自動でWALチェックポイントを行い `-wal`/`-shm` を削除してしまい、既存の
  アプリ側コネクションとロック状態が同期しなくなり、書き込みが恒久的にロックされる
  事故が発生した（復旧には最終的にVM起動処理の書き込みが busy_timeout 内で偶然成功し
  自然解消するまで待つしかなかった）。
- ログイン認証を読み取り専用セッション化する等の軽量な対策（コミット `904f902`）を
  適用した後も、VM起動中にログインできない問題が再現した。これは SQLite の
  ファイルレベル排他ロックという構造的制約そのものが残っているため、同種の
  軽量パッチでは根治しないことを示している。

PostgreSQL は MVCC（多版型同時実行制御）を採用しており、読み取りは書き込みを
ブロックせず、書き込み同士も行ロック単位で競合するため、「VM起動中の書き込みが
ログインの書き込みを待たせる」という問題が構造的に解消される。

## 移行の全体像

| 項目 | 現状 (SQLite) | 移行後 (PostgreSQL) |
|---|---|---|
| DBエンジン | SQLite (WALモード) | PostgreSQL 16系 |
| 非同期ドライバ | aiosqlite | asyncpg |
| ロック粒度 | ファイル全体（書き込み排他） | 行ロック（MVCC） |
| マイグレーション | 手書き `_migrate`（`PRAGMA table_info` で差分検出） | Alembic |
| デプロイ構成 | SQLiteファイルをボリュームマウント | `docker-compose.yml` に `postgres` サービス追加 |
| バックアップ | ファイルコピー | `pg_dump`/`pg_basebackup` |

## 移行ステップ

### Phase 0: 事前調査・設計確定（このドキュメント作成時点）

- [x] SQLite依存箇所の洗い出し（`database.py` のPRAGMA/BEGIN IMMEDIATEイベント、
      `_migrate` の `PRAGMA table_info` 使用、`models.py` のEnum/制約定義）
- [x] 現行データ規模の確認: `hackbento.db` 本体126,976 bytes（約124KB）+ WAL 8,272 bytes
      + SHM 32,768 bytes。行数は users=7, images=17, environments=230,
      image_collaborators=0, default_kernel_assets=3, default_rootfs_assets=3,
      global_settings=1（2026-11-03時点）。**データ量は極小で、ダンプ/リストアによる
      データ移行自体は容易。移行工数の中心はコード側（database.pyのロック制御・
      マイグレーション機構）とインフラ構成変更に集中する**
- [x] 生SQL・`text(...)` を使っている箇所の全数確認:
      `database.py` の `_ensure_global_settings`（SELECT/INSERT）と `_migrate`
      （`PRAGMA table_info`・`ALTER TABLE ADD COLUMN`・`UPDATE`）のみ。
      `routers/`・`services/` 配下には生SQLなし（全てORM経由）。
      `with_for_update()`（行ロック）は `routers/envs.py:222,275`、
      `routers/attack_machine.py:218,249`、`services/bundle_build.py:257`、
      `services/docker_base_assets.py:130`、`services/rootfs_convert.py:60`、
      `services/watchdog.py:33` で使用。これらはSQLAlchemyの方言抽象化レイヤー
      経由のためPostgreSQL移行後もそのまま動作する想定（概念が「ファイルロックへの
      縮退」から「本来の行ロック」に変わる点が移行の実質的な改善点）
- [ ] ステージング環境（本番と分離した検証用コンテナ）の用意

### Phase 1: インフラ・依存関係の追加（実装済み・2026-11-03）

SQLite/PostgreSQL のどちらも `DATABASE_URL` で選べるようにし、既定（`.env` 未変更）では
SQLiteのまま動作することを確認した方針で実装した。PostgreSQL専用に全面切り替えるのではなく、
**両対応の分岐**にしたのは、本番環境が現在SQLiteで稼働中であり即時のデータ移行や
ダウンタイムを伴う切り替えをまだ実施していないため。

- [x] `docker-compose.yml` に `postgres` サービスを追加。Compose profiles
      （`profiles: ["postgres"]`）で既定では起動しない構成にし、使う場合のみ
      `docker compose --profile postgres up -d` を明示する。他の全サービスと同じ
      `network_mode: host` に統一し、`localhost:5432` で到達できるようにした
- [x] `orchestrator/requirements.txt` に `asyncpg==0.29.0` を追加
- [x] `.env.example` に `DATABASE_URL=postgresql+asyncpg://...`（接続先はlocalhost）
      と `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` の例をコメントで追加。
      SQLite用の既定行はそのまま残し、PostgreSQL行はコメントアウト状態で提示する
      （本番 `.env` は変更していない。SQLiteのまま運用継続中）

### Phase 2: アプリケーションコードの移行（実装済み・2026-11-03）

- [x] `database.py`: `IS_SQLITE = settings.DATABASE_URL.startswith("sqlite")` で
      方言を判定し、`if IS_SQLITE: ... else: ...` で分岐する実装にした
      （Alembicへの完全移行ではなく、既存の `_migrate` 関数を方言分岐で両対応させる
      最小変更の方針を採用）。
  - `_set_sqlite_pragma`/`_do_begin`/`_do_begin_readonly`（PRAGMA・BEGIN IMMEDIATE制御）
    は `IS_SQLITE` ブロック内でのみ定義・登録する。PostgreSQL時は `engine_readonly`/
    `AsyncSessionLocalReadonly` を `engine`/`AsyncSessionLocal` のエイリアスにして
    単一エンジンに統合した（`get_db`/`get_db_readonly` の呼び出し側インターフェースは
    変更せず、戻り値の実体だけ統合する形にしたことで `routers/` 側の広範囲な変更を回避）
  - `_migrate` の `PRAGMA table_info(...)` は `IS_SQLITE` のときのみ使用し、それ以外は
    `information_schema.columns` への問い合わせに置き換えた。DDL文字列中の
    `BOOLEAN NOT NULL DEFAULT 0/1`（SQLite方言リテラル）はPostgreSQL実行時のみ
    `FALSE`/`TRUE` に文字列置換してから実行する
  - `init_db()` に起動時リトライ（PostgreSQL時のみ、3秒間隔で最大10回）を追加。
    `postgres` コンテナのヘルスチェック通過前にorchestratorが接続を試みて失敗する
    起動順序の問題に対応するため
- [x] `models.py`: `Column(Enum(...))` の6箇所（`User.role`/`User.auth_provider`/
      `Image.difficulty`/`Image.visibility`/`ImageCollaborator.role`/
      `Environment.status`）全てに `native_enum=False` を追加。SQLite側の挙動は
      変わらないが、PostgreSQL移行時にネイティブENUM型（`CREATE TYPE`、値の追加・削除に
      `ALTER TYPE ... ADD VALUE` が必要で制約が強い）を作らず、SQLiteと同様にVARCHARとして
      保存されるようにし、将来の値追加（例: `RootfsConversionStatus` 系）の柔軟性を保った
- [ ] Alembic導入は見送り、既存の手書き `_migrate` を両方言対応に拡張する方針にとどめた
      （採用した方針。理由: 現行のテーブル数・マイグレーション件数が少なく、Alembic導入の
      コストに対して得られる恩恵が今は小さいと判断したため。将来マイグレーションが
      複雑化した場合は改めて検討する）
- [ ] 生SQL・`with_for_update()` 等の行ロック処理がPostgreSQLで意図通り動くかの実機検証
      （`docker compose --profile postgres up -d` でPostgreSQL環境を立て、VM起動と
      ログインの同時実行テストを含めて確認する。**まだ実施していない**）
- [x] `get_db`/`get_db_readonly` の二重エンジン構成は、PostgreSQL時のみ
      `engine_readonly = engine` のエイリアス化で統合済み（上記 database.py の変更内容）
- [ ] `services/network.py::allocate_ip` 内の「外部コマンドをdbアクセス前に完了させる」
      処理順序の工夫（コミット `904f902`）は未変更のまま残している。PostgreSQL移行後も
      動作に支障はないため削除は必須ではないが、コメントがSQLite前提のまま陳腐化している
      点は今後の課題として残る

### Phase 3: データ移行

- [ ] 既存の `hackbento.db` から全テーブルのデータをエクスポートし、PostgreSQLへ
      投入するスクリプトを作成（`sqlite3` → CSV/SQL → `psql`/`asyncpg` 等の手段を検討）
- [ ] 外部キー・ユニーク制約を含めたデータ整合性の検証（移行後に件数・主要な
      リレーションの整合性をチェックするスクリプトを用意）
- [ ] ダウンタイムの計画（本番運用中のため、移行作業中は新規ログイン・VM起動を
      止める必要がある。移行作業のメンテナンス時間帯をユーザーに事前告知する）

### Phase 4: 検証・切り替え

- [x] **PostgreSQL実機での動作検証（2026-11-03 実施）**: `docker compose --profile
      postgres up -d postgres` でコンテナを起動し、ヘルスチェック通過を確認。
      orchestratorコンテナ内から `DATABASE_URL` をPostgreSQL用に切り替えた一時
      セッションで `init_db()` を実行し、以下を確認した:
      - 全7テーブル（`users`/`images`/`environments`/`image_collaborators`/
        `default_kernel_assets`/`default_rootfs_assets`/`global_settings`）が
        正常に作成され、`_migrate` のカラム追加処理も `information_schema.columns`
        経由で正しく動作した
      - `role`/`auth_provider` 等のEnum型カラムが `character varying` として
        作成され、`native_enum=False` の効果を確認（ネイティブENUM型は作られない）
      - `init_db()` を2回実行しても冪等（重複作成・エラーなし）
      - `GlobalSetting` の初期値投入も正常動作
- [x] **本題の負荷テスト（今回の問題の直接的な再現テスト）**: テストユーザーを
      作成し、以下の並行処理を検証した:
      1. 「VM起動処理の `allocate_ip` 相当」として、ある行に `with_for_update()`
         で排他行ロックを取得したまま5秒間保持するタスク
      2. 1秒後に別接続から「ログイン相当」の読み取り（`SELECT ... WHERE
         username = ...`）を行うタスク
      結果: ロックを握っている最中でも、ログイン相当の読み取りは **0.008秒**で
      即座に完了した（SQLiteではファイルレベル排他のため待たされていたのに対し、
      PostgreSQLのMVCCにより無関係な行への読み取りは全くブロックされないことを実証）
- [x] **本番切り替え実施（2026-11-03）**: 起動中のVMが無いタイミングで実施した。
      1. 本番 `hackbento.db` をバックアップ（`data/db/backup/hackbento.db.bak.20261103132215`）
      2. PostgreSQL（`hackbento-postgres`コンテナ）にスキーマを作成（`init_db()`実行）
      3. SQLiteの全テーブルをPostgreSQLへ移行。Boolean列（0/1→true/false）・
         DateTime列（ISO文字列→datetimeオブジェクト）の型変換が必要だったため、
         移行スクリプトで対応。シーケンス（自動採番）も移行後の最大IDに合わせて修正
      4. 件数検証: `users`=7, `images`=17, `environments`=230, `image_collaborators`=0,
         `default_kernel_assets`=3, `default_rootfs_assets`=3, `global_settings`=1 の
         全テーブルでSQLite側と完全一致。`images`→`users`（owner_id）等のJOINも正常
      5. PostgreSQLの接続パスワードを検証用の仮値から本番用に変更
      6. 本番 `.env` の `DATABASE_URL` をPostgreSQL用に書き換え、
         `docker compose --profile postgres up -d` で再起動
      7. 起動確認: `Application startup complete`をエラーなく確認。
         `/api/images/`・`/api/auth/me`・`/api/auth/ldap-status`のAPI応答、
         `with_for_update()`（行ロック）の動作を確認
- [ ] ステージング環境での全機能結合テスト（ログイン実操作、VM起動・停止・延長の
      実際のユーザー操作、管理画面、Attack Machine、rootfs変換・Composeバンドル
      ビルドのバックグラウンドワーカー）は、本番の実ユーザーへの影響を避けるため
      まだ実施していない。切り替え後の実運用の中で経過観察する
- [ ] 切り替え後の監視（エラーログ・レスポンスタイムの確認）を継続する

**本番環境は現在PostgreSQLで稼働中**。旧SQLiteファイル（`data/db/hackbento.db`）は
削除しておらずそのまま残っている（過去データの参照用として保持。コード上の
SQLiteサポート自体は下記Phase 5で完全除去済みのため、`DATABASE_URL`をSQlite行に
戻しても現行コードでは動作しない点に注意）。

### Phase 5: 後片付け（実施済み・2026-11-03）

ユーザーの指示により、ロールバック手段としての両対応は維持せず、SQLiteサポートを
完全に除去する方針で実施した。

- [x] SQLite関連コード・依存の完全除去:
  - `orchestrator/database.py`: `IS_SQLITE`分岐・`_set_sqlite_pragma`・`_do_begin`/
    `_do_begin_readonly`・`PRAGMA table_info`呼び出しを全て削除し、PostgreSQL専用の
    単一エンジン構成に書き直した（`engine_readonly`/`AsyncSessionLocalReadonly`は
    `engine`/`AsyncSessionLocal`の単純なエイリアスとして残し、`get_db`/
    `get_db_readonly`の呼び出し側インターフェースは変更していない）
  - `orchestrator/requirements.txt`: `aiosqlite==0.20.0` を削除
  - `orchestrator/config.py`: `DATABASE_URL`のデフォルト値をPostgreSQL用に変更
  - `_migrate`のDDL内`BOOLEAN NOT NULL DEFAULT 0/1`を`FALSE`/`TRUE`に統一
    （方言分岐での文字列置換が不要になったため、DDL自体をPostgreSQL専用の記法に）
- [x] `docker-compose.yml`: `postgres`サービスから`profiles: ["postgres"]`を削除し、
      常時起動の必須サービスに変更。`orchestrator`の`depends_on`に
      `postgres: condition: service_healthy`を追加
- [x] `.env.example`: SQLite用の記述・コメントを削除し、PostgreSQL専用の記述に統一
- [x] CLAUDE.md・README.md・README_jp.md の技術スタック記載を
      `DB: PostgreSQL（asyncpg 非同期）`に更新（SQLiteへの言及を全て削除）
- [x] 旧 `hackbento.db` ファイル・バックアップ（`data/db/backup/`）は削除せず保持
      （過去データの参照用。コード側はPostgreSQL専用になったため、このファイルを
      再度読み込んで動かすことはできない）
- [x] 再ビルド・再起動して動作確認（`aiosqlite`が実際にアンインストールされたことを
      `pip show aiosqlite`で確認。`Application startup complete`をエラーなく確認）

## リスク・注意点

- **ダウンタイムが必要**: データ移行は稼働中のサービスに対して安全に行える
  オンライン移行ではなく、停止を伴う一括移行を想定する
- **`network_mode: host` との整合性**: 既存の全サービスが `network_mode: host`
  で動いているため、PostgreSQLコンテナも同様の構成にするか、通常のブリッジ
  ネットワーク越しに接続するかを決める必要がある
- **Enum型の扱い**: PostgreSQLのネイティブENUM型は値の追加・削除がSQLiteの
  VARCHARベースの運用より制約が強い（`ALTER TYPE ... ADD VALUE` が必要）。
  今後 `RootfsConversionStatus` 等に値を追加する運用が想定されるなら、
  ネイティブENUMではなく `VARCHAR` + アプリ側Enumチェックのままにする方が
  柔軟性を保てる可能性がある
- **バックアップ運用の変更**: 単一ファイルのコピーで済んでいたバックアップが
  `pg_dump` 等のツール運用に変わる

## 現時点でのステータス（2026-11-03更新）

- **Phase 0（調査）・Phase 1（インフラ・依存追加）・Phase 2（アプリコード移行）は実装済み**。
  SQLite/PostgreSQLをどちらも `DATABASE_URL` で選択できる状態になっている。
  本番 `.env` は変更しておらず、SQLiteのまま運用継続中（既存運用への影響なし）
- 再ビルド・再起動して起動確認済み（SQLite運用時、`Application startup complete` を
  エラーなく確認。マイグレーション・GlobalSetting初期化も正常動作）
- **PostgreSQL側の実機検証（Phase 2の一部・Phase 4）はまだ実施していない**。
  `docker compose --profile postgres up -d` でPostgreSQLコンテナを起動し、
  実際に `DATABASE_URL` を切り替えて動作確認するところから次のステップになる
- Phase 3（データ移行）・Phase 5（後片付け・SQLite完全除去）は未着手。
  本番をPostgreSQLへ実際に切り替える場合はダウンタイムが必要で、別途計画する
