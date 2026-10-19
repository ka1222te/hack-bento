"""docker-base rootfs ビルド専用の小さな HTTP サービス。

orchestrator コンテナは Firecracker 脱出時の被害を抑えるため docker.sock を
一切持たない（CLAUDE.md 参照）。docker-base rootfs（Compose バンドル用の
共通土台）の構築だけは Debian コンテナを起動して docker export する必要があり
docker.sock が要るため、この専用コンテナに切り出し、orchestrator からは
HTTP 経由でジョブを依頼する。

ジョブは非同期（POST で開始 → GET でポーリング）。ビルドは数分〜30分程度
かかるため、HTTP リクエスト自体はブロックしない。
"""
import asyncio
import logging
import os
import shutil
import tempfile
import uuid
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("docker-base-builder")

app = FastAPI()

OUTPUT_DIR = "/output"
_BUILD_TIMEOUT_S = 1800
_EXPORT_TIMEOUT_S = 900

# docker-base をビルドできるベースイメージ。orchestrator 側の
# ALLOWED_DOCKER_BASE_IMAGES と同じ内容を保つこと（docker_base_assets.py 参照）。
ALLOWED_BASE_IMAGES = {"debian:bookworm-slim"}

# /export-tar で使える固定のベースイメージ（default_assets.py・attack_machine_assets.py が
# 内部で使う、管理者操作由来の信頼済みイメージのみ。ユーザーが選べる対象ではない）。
ALLOWED_EXPORT_BASE_IMAGES = {"alpine:3.19", "kalilinux/kali-rolling"}

_jobs: dict[str, dict] = {}


class BuildRequest(BaseModel):
    job_id: str
    base_image: str
    resolv_conf: str
    dest_filename: str  # OUTPUT_DIR 配下に書き出すファイル名（パストラバーサル対策で basename のみ許可）
    size_mb: int = 1536


class ExportTarRequest(BaseModel):
    """汎用のtar取り出しジョブ（default rootfs・rootfs変換専用ベース・Attack Machine用）。

    orchestrator はビルドスクリプトを固定の信頼済みコード（管理者操作由来、
    ユーザー入力を含まない）としてのみ送る。docker-base-builder は 127.0.0.1
    限定で orchestrator からしか到達できないため、外部からの不正な
    シェルスクリプト注入はできない。
    """
    job_id: str
    base_image: str
    build_script: str  # コンテナ内で実行するシェルスクリプト（bash -c で渡す）
    dest_filename: str  # OUTPUT_DIR 配下に書き出す tar ファイル名
    # "export": docker export でコンテナ全体を取り出す
    # "cp": コンテナ内の cp_src_path を docker cp で取り出す（すでに tar 済みのファイル）
    method: str = "export"
    cp_src_path: Optional[str] = None
    # "docker run" 本体のタイムアウト（秒）。Attack Machine（Kali）のような大きい
    # イメージは数十分〜2時間程度かかることがあるため、呼び出し側で調整できるようにする。
    build_timeout_s: int = _BUILD_TIMEOUT_S


# docker-base の /sbin/init。orchestrator/services/docker_base_assets.py の
# _DOCKER_BASE_INIT_TEMPLATE と同一内容を保つこと（責務分離のため重複定義している）。
_DOCKER_BASE_INIT_TEMPLATE = r"""#!/bin/sh
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
mount -t proc proc /proc 2>/dev/null || true
mount -t sysfs sysfs /sys 2>/dev/null || true
mount -t devtmpfs devtmpfs /dev 2>/dev/null || true
mkdir -p /dev/pts /dev/shm
mount -t devpts devpts /dev/pts 2>/dev/null || true
mount -t tmpfs tmpfs /dev/shm 2>/dev/null || true
mkdir -p /sys/fs/cgroup
mount -t cgroup2 cgroup2 /sys/fs/cgroup 2>/dev/null || true
ip link set lo up 2>/dev/null || true

is_build=0
if grep -q 'hb_build' /proc/cmdline 2>/dev/null; then is_build=1; fi

start_dockerd() {
    if [ "$1" = "build" ]; then
        dockerd --iptables=false --ip6tables=false --bridge=none --userland-proxy=false >/var/log/dockerd.log 2>&1 &
    else
        dockerd --iptables=true --userland-proxy=false >/var/log/dockerd.log 2>&1 &
    fi
    i=0
    while [ $i -lt 60 ]; do
        if docker info >/dev/null 2>&1; then return 0; fi
        i=$((i+1)); sleep 1
    done
    echo "[hackbento] dockerd が起動しませんでした" >&2
    return 1
}

if [ "$is_build" = "1" ]; then
    mkdir -p /mnt/scratch
    mount /dev/vdb /mnt/scratch 2>/dev/null || true
    STATUS=1
    {
        echo "=== hackbento docker-base build start ==="
        if start_dockerd build; then
            if [ -d /mnt/scratch/app ]; then
                rm -rf /opt/app; mkdir -p /opt/app
                cp -a /mnt/scratch/app/. /opt/app/
            fi
            if [ -f /mnt/scratch/images.tar ]; then
                docker load -i /mnt/scratch/images.tar
            fi
            if [ -d /mnt/scratch/overlay ]; then
                cp -a /mnt/scratch/overlay/. /
            fi
            PROJ="$(cat /mnt/scratch/project_name 2>/dev/null || echo app)"
            if [ -f /opt/app/compose.yml ] || [ -f /opt/app/compose.yaml ] || [ -f /opt/app/docker-compose.yml ] || [ -f /opt/app/docker-compose.yaml ]; then
                docker compose --project-directory /opt/app -p "$PROJ" config --format json > /mnt/scratch/compose.json 2>/mnt/scratch/compose.err && STATUS=0 || STATUS=2
                docker compose --project-directory /opt/app -p "$PROJ" config --images > /mnt/scratch/images.list 2>/dev/null || true
            else
                echo "compose ファイルが /opt/app に見つかりません" > /mnt/scratch/compose.err
                STATUS=3
            fi
            docker images --format '{{.Repository}}:{{.Tag}}' > /mnt/scratch/loaded.list 2>/dev/null || true
        else
            echo "dockerd 起動失敗" > /mnt/scratch/compose.err
            STATUS=4
        fi
        echo "$STATUS" > /mnt/scratch/status
        echo "=== build end status=$STATUS ==="
    } >> /mnt/scratch/build.log 2>&1
    cp /var/log/dockerd.log /mnt/scratch/dockerd.log 2>/dev/null || true
    sync
    umount /mnt/scratch 2>/dev/null || true
    echo o > /proc/sysrq-trigger
else
    printf '__RESOLV_CONF__' > /etc/resolv.conf
    chmod 644 /etc/resolv.conf
    hostname hackbento 2>/dev/null || true
    grep -q hackbento /etc/hosts 2>/dev/null || echo "127.0.0.1 localhost hackbento" >> /etc/hosts
    if start_dockerd; then
        PROJ="$(cat /opt/app/.hb_project 2>/dev/null || echo app)"
        docker compose --project-directory /opt/app -p "$PROJ" up -d --pull never >/var/log/compose.log 2>&1 || true
    fi
    echo "=== HackBento docker-base VM 起動完了 ==="
    while true; do sleep 3600; done
fi
"""

_DOCKER_BASE_BUILD_SCRIPT = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
    ca-certificates curl iproute2 iptables kmod e2fsprogs
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y --no-install-recommends \
    docker-ce docker-ce-cli containerd.io docker-compose-plugin
update-alternatives --set iptables /usr/sbin/iptables-legacy || true
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy || true
mkdir -p /etc/docker
cat > /etc/docker/daemon.json << 'JSON_EOF'
{"storage-driver":"overlay2","features":{"containerd-snapshotter":false}}
JSON_EOF
apt-get clean
rm -rf /var/lib/apt/lists/*
cat > /tmp/hackbento-init << 'INIT_EOF'
__INIT_SCRIPT__
INIT_EOF
chmod +x /tmp/hackbento-init
rm -f /sbin/init
mv /tmp/hackbento-init /sbin/init
tar -cf /rootfs.tar -C / --exclude=./proc --exclude=./sys --exclude=./dev --exclude=./rootfs.tar .
"""


async def _run(cmd: list[str], timeout: int) -> None:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {stderr.decode(errors='replace').strip()}")


async def _run_to_file(cmd: list[str], dest_path: str, timeout: int) -> None:
    with open(dest_path, "wb") as f:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=f, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {stderr.decode(errors='replace').strip()}")


async def _run_convert_to_ext4(tar_path: str, dest_path: str, size_mb: int) -> None:
    """tar を ext4 イメージへ変換する（mkfs.ext4 -d で直接 populate。loop マウント不要）。"""
    workdir = tempfile.mkdtemp(prefix="hackbento-convert-")
    root = os.path.join(workdir, "root")
    os.makedirs(root, exist_ok=True)
    try:
        await _run(["tar", "-xf", tar_path, "-C", root], timeout=600)
        tmp_img = dest_path + ".tmp"
        if os.path.exists(tmp_img):
            os.remove(tmp_img)
        await _run(["dd", "if=/dev/zero", f"of={tmp_img}", "bs=1M", f"count={size_mb}"], timeout=300)
        await _run(["mkfs.ext4", "-F", "-O", "^metadata_csum", "-d", root, tmp_img], timeout=600)
        os.replace(tmp_img, dest_path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def _do_build(job_id: str, base_image: str, resolv_conf: str, dest_path: str, size_mb: int) -> None:
    job = _jobs[job_id]
    tmpdir = tempfile.mkdtemp(prefix="hackbento-docker-base-")
    container_name = f"hackbento-docker-base-build-{job_id}"
    try:
        init_script = _DOCKER_BASE_INIT_TEMPLATE.replace("__RESOLV_CONF__", resolv_conf)
        build_script = _DOCKER_BASE_BUILD_SCRIPT.replace("__INIT_SCRIPT__", init_script)
        try:
            await _run(["docker", "rm", "-f", container_name], timeout=30)
        except Exception:
            pass
        await _run(
            ["docker", "run", "--name", container_name, base_image, "bash", "-c", build_script],
            timeout=_BUILD_TIMEOUT_S,
        )
        tar_path = os.path.join(tmpdir, "rootfs.tar")
        await _run_to_file(["docker", "export", container_name], tar_path, timeout=_EXPORT_TIMEOUT_S)
        await _run_convert_to_ext4(tar_path, dest_path, size_mb)
        job["status"] = "ready"
        logger.info(f"job {job_id}: docker-base rootfs built -> {dest_path}")
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e).strip()[:1024]
        logger.error(f"job {job_id}: failed: {e}")
    finally:
        try:
            await _run(["docker", "rm", "-f", container_name], timeout=30)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)


async def _do_export_tar(
    job_id: str, base_image: str, build_script: str, dest_path: str, method: str,
    cp_src_path: Optional[str], build_timeout_s: int,
) -> None:
    """default rootfs・rootfs変換専用ベース・Attack Machine用 rootfs の共通ビルド処理。

    ベースイメージ上でビルドスクリプトを実行し、結果を tar として dest_path に書き出す。
    ext4 への変換は行わない（orchestrator 側が Firecracker 安全変換 VM で行う）。
    """
    job = _jobs[job_id]
    container_name = f"hackbento-export-build-{job_id}"
    try:
        try:
            await _run(["docker", "rm", "-f", container_name], timeout=30)
        except Exception:
            pass
        await _run(
            ["docker", "run", "--name", container_name, base_image, "sh", "-c", build_script],
            timeout=build_timeout_s,
        )
        if method == "cp":
            if not cp_src_path:
                raise RuntimeError("method='cp' には cp_src_path が必要です")
            await _run(["docker", "cp", f"{container_name}:{cp_src_path}", dest_path], timeout=_EXPORT_TIMEOUT_S)
        else:
            await _run_to_file(["docker", "export", container_name], dest_path, timeout=_EXPORT_TIMEOUT_S)
        job["status"] = "ready"
        logger.info(f"job {job_id}: tar exported -> {dest_path}")
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e).strip()[:1024]
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        logger.error(f"job {job_id}: failed: {e}")
    finally:
        try:
            await _run(["docker", "rm", "-f", container_name], timeout=30)
        except Exception:
            pass


@app.post("/export-tar")
async def start_export_tar(req: ExportTarRequest):
    if req.base_image not in ALLOWED_EXPORT_BASE_IMAGES:
        raise HTTPException(status_code=400, detail=f"許可されていないベースイメージです: {req.base_image}")
    dest_filename = os.path.basename(req.dest_filename)
    if not dest_filename or dest_filename != req.dest_filename:
        raise HTTPException(status_code=400, detail="dest_filename が不正です")
    if req.method not in ("export", "cp"):
        raise HTTPException(status_code=400, detail="method は export または cp を指定してください")
    if req.job_id in _jobs:
        raise HTTPException(status_code=409, detail="同じ job_id のジョブが既に存在します")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    dest_path = os.path.join(OUTPUT_DIR, dest_filename)
    _jobs[req.job_id] = {"status": "running", "error": None}
    asyncio.create_task(
        _do_export_tar(
            req.job_id, req.base_image, req.build_script, dest_path,
            req.method, req.cp_src_path, req.build_timeout_s,
        )
    )
    return {"job_id": req.job_id, "status": "running"}


@app.post("/build")
async def start_build(req: BuildRequest):
    if req.base_image not in ALLOWED_BASE_IMAGES:
        raise HTTPException(status_code=400, detail=f"許可されていないベースイメージです: {req.base_image}")
    dest_filename = os.path.basename(req.dest_filename)
    if not dest_filename or dest_filename != req.dest_filename:
        raise HTTPException(status_code=400, detail="dest_filename が不正です")
    if req.job_id in _jobs:
        raise HTTPException(status_code=409, detail="同じ job_id のジョブが既に存在します")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    dest_path = os.path.join(OUTPUT_DIR, dest_filename)
    _jobs[req.job_id] = {"status": "running", "error": None}
    asyncio.create_task(_do_build(req.job_id, req.base_image, req.resolv_conf, dest_path, req.size_mb))
    return {"job_id": req.job_id, "status": "running"}


@app.get("/build/{job_id}")
async def get_build_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="ジョブが見つかりません")
    return {"job_id": job_id, "status": job["status"], "error": job.get("error")}


@app.delete("/build/{job_id}")
async def delete_build_job(job_id: str):
    """完了済みジョブの記録を破棄する（メモリリーク防止。呼び出し側がポーリング終了後に呼ぶ）。"""
    _jobs.pop(job_id, None)
    return {"ok": True}


@app.get("/health")
async def health():
    return {"ok": True}
