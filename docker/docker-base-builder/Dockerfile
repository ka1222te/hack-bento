# docker-base rootfs（Compose バンドル用の共通土台）ビルド専用コンテナ。
# /var/run/docker.sock を持つのはこのコンテナだけで、orchestrator からは
# 完全に分離する（CLAUDE.md の「Firecracker脱出時の被害を抑える」方針）。
FROM python:3.12-slim

# Debian trixie の docker.io パッケージには docker CLI 本体（/usr/bin/docker）が
# 含まれていない（dockerd/docker-proxy のみ）ため、Docker 公式リポジトリから
# docker-ce-cli を導入する。dockerd 自体は不要（ホストの docker.sock を使う）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl e2fsprogs tar gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && . /etc/os-release \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && apt-get purge -y gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi "uvicorn[standard]"

WORKDIR /app
COPY server.py .

EXPOSE 8300
CMD ["uvicorn", "server:app", "--host", "127.0.0.1", "--port", "8300"]
