#!/bin/sh
set -e

PORT="${PORT:-8000}"
CERT_DIR="/data/certs"
KEY_FILE="$CERT_DIR/server.key"
CRT_FILE="$CERT_DIR/server.crt"

if [ "${SCHEME:-http}" = "https" ]; then
    mkdir -p "$CERT_DIR"

    if [ ! -f "$KEY_FILE" ] || [ ! -f "$CRT_FILE" ]; then
        echo "[entrypoint] TLS certificate not found. Generating self-signed certificate..."
        openssl req -x509 -newkey rsa:4096 -days 3650 -nodes \
            -keyout "$KEY_FILE" \
            -out "$CRT_FILE" \
            -subj "/CN=${DOMAIN:-localhost}" \
            -addext "subjectAltName=DNS:${DOMAIN:-localhost},IP:127.0.0.1"
        echo "[entrypoint] Self-signed certificate generated: $CRT_FILE"
    else
        echo "[entrypoint] TLS certificate found: $CRT_FILE"
    fi

    # --reload は付けない。ソースコードはバインドマウントされており開発中の
    # 即時反映には便利だが、ファイル変更を検知すると進行中のVM起動処理
    # （rootfsコピー・Firecracker起動待ち等のバックグラウンドタスク）を巻き込んで
    # プロセスを再起動してしまい、サイトが一時的に応答不能になる事故が複数回
    # あったため（本番運用中の編集は docker compose restart/up -d で明示的に反映する）。
    exec uvicorn main:app \
        --host 0.0.0.0 \
        --port "$PORT" \
        --ssl-keyfile "$KEY_FILE" \
        --ssl-certfile "$CRT_FILE"
else
    exec uvicorn main:app \
        --host 0.0.0.0 \
        --port "$PORT"
fi
