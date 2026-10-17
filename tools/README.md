# tools/

## hackbento-pack
Compose プロジェクトを HackBento 用のバンドル tar にまとめるツール。

```bash
./hackbento-pack --bundle <プロジェクトディレクトリ> [-o 出力.tar] [--name 名前]
```

- 要件: docker, docker compose v2（手元で `docker compose up` が通る状態）。root 不要。
- サーバ側と同じ検査（macvlan/ipvlan・固定IP・`image:` 無しの拒否）を手元で行う。
- プロジェクト直下に `overlay/` があれば、その中身がゲスト OS の `/` に重ねられる（フラグ等）。

生成した tar は、HackBento の新規プロジェクト作成で「Compose バンドル」を選んでアップロードする。

## sample-bundle/
最小サンプル（nginx 公開 + redis 内部 + overlay のフラグ）。
