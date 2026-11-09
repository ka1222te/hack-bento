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
    DATABASE_URL: str = "postgresql+asyncpg://hackbento:hackbento@localhost:5432/hackbento"

    # タイムアウト・制限
    DEFAULT_TIMEOUT_MINUTES: int = 60
    # VM起動・延長時に選択できるタイムアウト（分）の候補リスト。0 は無制限を表す。
    # 負の値・数値に変換できない値は無視する。空になった場合は既定値にフォールバックする。
    TIMEOUT_MINUTES_OPTIONS: Union[List[int], str] = [15, 30, 45, 60, 75, 90, 105, 120]
    MAX_ENVS_PER_USER: int = 2
    MAX_ENVS_TOTAL: int = 20

    TIMEOUT_WARNING_MINUTES: int = 10
    VM_CPU_LIMIT: int = 1
    VM_MEMORY_LIMIT_MB: int = 1024
    VM_DISK_LIMIT_GB: int = 10
    VM_PIDS_LIMIT: int = 256
    # rootfsのwritableコピー（_copy_rootfs_io_limited）を同時に実行できる本数の上限。
    # 複数VMが同時起動すると、その数だけcpプロセスが並行してホストのページキャッシュに
    # 書き込み、ディスクI/O帯域（VM_IO_LIMIT_MBPS）を奪い合って完了が遅延し、
    # その間キャッシュがホストメモリを圧迫してホスト全体がハングする事故があったため、
    # 同時に走るコピー本数自体を絞る（I/O帯域制限とは別の、同時実行数での制限）。
    VM_ROOTFS_COPY_CONCURRENCY: int = 2
    # 起動待機中/起動中/rootfsコピー中の表示を更新するため、WebUIが環境状態を
    # ポーリングする間隔（秒）。index.htmlの環境一覧全体更新、project.htmlの
    # 起動中監視、navbar.htmlのAttack Machineナビポーリングで共通して使う。
    WEB_POLL_INTERVAL_SECONDS: int = 5
    UPLOAD_IMAGE_MAX_MB: int = 6144
    UPLOAD_README_MAX_MB: int = 10

    # rootfs tar→ext4 自動変換時に追加できる余裕MBの最大値の初回起動時デフォルト値。
    # アプリ初回起動時に GlobalSetting.rootfs_margin_max_mb の初期値として一度だけ投入され、
    # 以後は /admin のデフォルトVMタブから動的に変更する（この値は再起動しないと反映されないため）。
    # プロジェクト作成者は新規作成・編集画面で 0〜この最大値の範囲の余裕MBを選べる
    # （Docker等を含む大きめのrootfsをアップロードする場合、VM起動後にゲスト内で
    # apt install/docker pull等を行うとすぐ容量が尽きるため）。
    ROOTFS_CONVERT_SIZE_MARGIN_MB: int = 256

    # rootfsのwritableコピー・debugfsによるresolv.conf注入・running中のFirecracker VM
    # 自体（ゲスト内のディスクI/O）は、複数VMの同時起動・稼働時にホストのディスクI/O
    # 帯域を奪い合い、他の全リクエスト（ログイン・ページ閲覧等）がイベントループを
    # ブロックされて応答不能になる事故が起きた。VM専用cgroup委譲ツリーのルート
    # （smolvm.py::VM_CGROUP_ROOT、"hackbento-vms"）自体にcgroup v2のio.maxを設定し、
    # その配下の全プロセス（起動処理中のコピー・debugfs、稼働中の全Firecracker VM）が
    # 使うディスク帯域の合計をここで一括制限する。VMごとの個別上限ではなく、
    # ホスト全体で合計この値を超えないようにする設計。0または未設定の場合は制限しない
    # （cgroup委譲ツリーが無い環境ではこの制限自体も無効）。
    VM_IO_LIMIT_MBPS: int = 0

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

    @field_validator("TIMEOUT_MINUTES_OPTIONS", mode="before")
    @classmethod
    def parse_timeout_options(cls, v):
        raw = v if isinstance(v, list) else str(v).split(",")
        options = set()
        for item in raw:
            try:
                n = int(str(item).strip())
            except (TypeError, ValueError):
                continue
            if n < 0:
                continue
            options.add(n)
        return sorted(options) or [15, 30, 45, 60, 75, 90, 105, 120]

    # ---- ネットワーク設定 ----
    BRIDGE_NAME: str = "vmbr-hackbento"               # TAP を接続するホスト bridge 名

    # VM に払い出す IP プール
    IP_POOL_START: str = "192.168.181.200"
    IP_POOL_END: str = "192.168.181.230"

    # VM のデフォルトゲートウェイ
    FC_GUEST_GATEWAY: str = "192.168.180.1"

    # VM に設定する DNS サーバ（カンマ区切りで複数指定可能）
    FC_GUEST_DNS: str = "8.8.8.8,8.8.4.4"

    # ---- Attack Machine ----
    ATTACK_MACHINE_ENABLED: bool = False
    ATTACK_MACHINE_CPU_LIMIT: int = 2
    ATTACK_MACHINE_MEMORY_LIMIT_MB: int = 2048
    # Attack Machine のタイムアウト（分）。0 は無制限を表す。
    # 負の値・数値に変換できない値は無視し、既定値（60分）にフォールバックする。
    ATTACK_MACHINE_TIMEOUT_MINUTES: int = 60

    @field_validator("ATTACK_MACHINE_TIMEOUT_MINUTES", mode="before")
    @classmethod
    def parse_attack_machine_timeout(cls, v):
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            return 60
        return n if n >= 0 else 60

    # ---- docker-base builder ----
    # orchestrator コンテナから見た docker-base-builder への接続先。orchestrator は
    # docker.sock を持たないため、docker-base rootfs のビルドはこのサービスへ委譲する
    # （services/docker_base_assets.py 参照。両コンテナとも network_mode: host）。
    DOCKER_BASE_BUILDER_URL: str = "http://127.0.0.1:8300"

    # ---- Guacamole ----
    # orchestrator コンテナから見た Guacamole (Tomcat) への接続先。
    # main.py のリバースプロキシ (/guacamole/*) がこの URL へ転送する。
    GUACAMOLE_INTERNAL_URL: str = "http://127.0.0.1:8080/guacamole"
    # ブラウザから見た公開パス。HackBento (uvicorn) がこのパス配下を
    # GUACAMOLE_INTERNAL_URL へリバースプロキシする。
    GUACAMOLE_PUBLIC_PATH: str = "/guacamole"
    # openssl rand -hex 16 (AES-128) で生成。HackBento の SECRET_KEY とは別の鍵。
    GUACAMOLE_JSON_SECRET_KEY: str = "changeme-32byte-hex-for-guacamole-json-auth"

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_parse_none_str": "null",
    }


settings = Settings()
