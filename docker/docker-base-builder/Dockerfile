# docker-base rootfs（Compose バンドル用の共通土台）ビルド専用コンテナ。
# /var/run/docker.sock を持つのはこのコンテナだけで、orchestrator からは
# 完全に分離する（CLAUDE.md の「Firecracker脱出時の被害を抑える」方針）。
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        docker.io e2fsprogs tar curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

WORKDIR /app
COPY server.py .

EXPOSE 8300
CMD ["uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8300"]
