import base64
import hashlib
import hmac
import json
import logging
import time

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from config import settings

logger = logging.getLogger(__name__)

GUAC_TOKEN_TTL_SECONDS = 300

_NULL_IV = b"\x00" * 16


def _aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """guacamole-auth-json が要求する形式（HMAC-SHA256署名を先頭に付与しゼロIVのAES-CBCで暗号化）でエンコードする。"""
    signature = hmac.new(key, plaintext, hashlib.sha256).digest()
    signed = signature + plaintext
    padder = padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(signed) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(_NULL_IV)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def build_guac_token(
    username: str,
    connection_name: str,
    protocol: str,
    hostname: str,
    port: int,
    guac_password: str,
    expires_seconds: int = GUAC_TOKEN_TTL_SECONDS,
) -> str:
    """guacamole-auth-json 用の暗号化済みトークン（base64）を生成する。"""
    key_hex = settings.GUACAMOLE_JSON_SECRET_KEY
    key = bytes.fromhex(key_hex)
    if len(key) not in (16, 24, 32):
        raise ValueError("GUACAMOLE_JSON_SECRET_KEY は16/24/32バイト(hex)のAESキーである必要があります")

    payload = {
        "username": username,
        "expires": int((time.time() + expires_seconds) * 1000),
        "connections": {
            connection_name: {
                "protocol": protocol,
                "parameters": {
                    "hostname": hostname,
                    "port": str(port),
                    "password": guac_password,
                    "ignore-cert": "true",
                },
            }
        },
    }

    encrypted = _aes_encrypt(key, json.dumps(payload).encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


async def build_guac_iframe_url(
    username: str,
    connection_name: str,
    protocol: str,
    hostname: str,
    port: int,
    guac_password: str,
) -> str:
    """JSON認証トークンをGuacamoleに交換し、埋め込み用のクライアントURL（HackBento経由の公開パス）を返す。"""
    token = build_guac_token(username, connection_name, protocol, hostname, port, guac_password)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.GUACAMOLE_INTERNAL_URL}/api/tokens",
            data={"data": token},
        )
        resp.raise_for_status()
        auth_token = resp.json()["authToken"]

    client_id = base64.b64encode(f"{connection_name}\0c\0json".encode("utf-8")).decode("ascii")
    public_path = settings.GUACAMOLE_PUBLIC_PATH.rstrip("/")
    return f"{public_path}/#/client/{client_id}?token={auth_token}"
