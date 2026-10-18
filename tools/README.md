# tools/

## bento-pack
Compose プロジェクトを HackBento 用のバンドル tar にまとめるツール。

本体は `orchestrator/static/tools/bento-pack`（Web サーバが認証不要で配信する静的ファイル）。
取得・実行方法は HackBento の `/docs/rootfs`（`orchestrator/templates/rootfs_help.html`）に記載している。
手元のこのファイルは開発用の控えで、配布・実行時に参照するものではない。

- 要件: docker, docker compose v2（手元で `docker compose up` が通る状態）。root 不要。
- サーバ側と同じ検査（macvlan/ipvlan・固定IP・`image:` 無しの拒否）を手元で行う。
- プロジェクト直下に `overlay/` があれば、その中身がゲスト OS の `/` に重ねられる（フラグ等）。

生成した tar は、HackBento の新規プロジェクト作成で「Compose バンドル」を選んでアップロードする。

## sample-bundle/
最小サンプル（nginx 公開 + redis 内部 + overlay のフラグ）。
