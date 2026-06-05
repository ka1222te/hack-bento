from pydantic_settings import BaseSettings
from pydantic import field_validator
from typing import List, Union


class Settings(BaseSettings):
    # アプリ
    APP_TITLE: str = "HackBento"
    SECRET_KEY: str = "changeme-in-production-use-random-256bit-key"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480
    DOMAIN: str = "localhost"
    SCHEME: str = "http"
    PORT: int = 8000

    # DB
    DATABASE_URL: str = "sqlite+aiosqlite:///./hackbento.db"

    # タイムアウト・制限
    DEFAULT_TIMEOUT_MINUTES: int = 60
    MAX_ENVS_PER_USER: int = 2
    MAX_ENVS_TOTAL: int = 20
    TIMEOUT_WARNING_MINUTES: int = 10
    VM_CPU_LIMIT: int = 1
    VM_MEMORY_LIMIT_MB: int = 1024
    VM_DISK_LIMIT_GB: int = 10
    VM_PIDS_LIMIT: int = 256
    UPLOAD_IMAGE_MAX_MB: int = 6144
    UPLOAD_README_MAX_MB: int = 10

    # 認証
    LDAP_ENABLED: bool = False
    LDAP_URI: str = "ldap://ldap.example.com:389"
    LDAP_TOP_DOMAIN: str = "dc=example,dc=com"

    LDAP_USER_FILTER: str = "(uid={username})"
    LDAP_OU_USER: str = "people"
    LDAP_OU_GROUP: str = "groups"

    GOOGLE_OAUTH_ENABLED: bool = False
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    OAUTH_ALLOWED_DOMAINS: Union[List[str], str] = []

    @field_validator("OAUTH_ALLOWED_DOMAINS", mode="before")
    @classmethod
    def parse_domains(cls, v):
        if not v:
            return []
        if isinstance(v, list):
            return v
        return [d.strip() for d in str(v).split(",") if d.strip()]

    # バックエンド
    VM_BACKEND: str = "docker"

    # ---- macvlan ネットワーク設定 ----
    MACVLAN_NETWORK: str = "hackbento-vm"   # docker-compose.yml の networks.vm_net.name と一致させる

    # VM に払い出す IP プール（docker-compose.yml の ip_range 内に収めること）
    IP_POOL_START: str = "192.168.181.200"
    IP_POOL_END: str = "192.168.181.230"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_parse_none_str": "null",
    }


settings = Settings()
