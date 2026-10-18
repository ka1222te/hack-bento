# tools/

## hackbento-pack
Compose プロジェクトを HackBento 用のバンドル tar にまとめるツール。

本体は `orchestrator/static/tools/hackbento-pack`（Web サーバが認証不要で配信する静的ファイル）。
利用者はクローン不要で、HackBento サーバから直接取得して実行できる:

```bash
curl -fsSL https://<domain>/static/tools/hackbento-pack -o hackbento-pack
chmod +x hackbento-pack
./hackbento-pack --bundle <プロジェクトディレクトリ> [-o 出力.tar] [--name 名前]
```

手元にファイルを残さず実行する場合:

```bash
curl -fsSL https://<domain>/static/tools/hackbento-pack | bash -s -- --bundle <プロジェクトディレクトリ>
```

- 要件: docker, docker compose v2（手元で `docker compose up` が通る状態）。root 不要。
- サーバ側と同じ検査（macvlan/ipvlan・固定IP・`image:` 無しの拒否）を手元で行う。
- プロジェクト直下に `overlay/` があれば、その中身がゲスト OS の `/` に重ねられる（フラグ等）。

生成した tar は、HackBento の新規プロジェクト作成で「Compose バンドル」を選んでアップロードする。

## sample-bundle/
最小サンプル（nginx 公開 + redis 内部 + overlay のフラグ）。
