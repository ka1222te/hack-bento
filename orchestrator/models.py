from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import (
    Column, String, Integer, Boolean, DateTime, ForeignKey, Text, Enum, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, PyEnum):
    admin = "admin"
    user = "user"


class AuthProvider(str, PyEnum):
    local = "local"
    ldap = "ldap"
    oauth = "oauth"


class EnvStatus(str, PyEnum):
    starting = "starting"
    running = "running"
    stopping = "stopping"
    stopped = "stopped"
    error = "error"


class RootfsConversionStatus(str, PyEnum):
    ready = "ready"
    pending = "pending"
    converting = "converting"
    failed = "failed"


class Difficulty(str, PyEnum):
    none = "none"
    easy = "easy"
    medium = "medium"
    hard = "hard"
    insane = "insane"


class Visibility(str, PyEnum):
    private = "private"      # 自分と管理者のみ
    protected = "protected"  # ログイン済みユーザ全員
    public = "public"        # 全員（ログイン不要）


class CollaboratorRole(str, PyEnum):
    read = "read"            # 閲覧・起動のみ
    read_write = "read_write"  # 閲覧・起動・編集


class Backend(str, PyEnum):
    macvlan = "macvlan"  # Docker コンテナ + macvlan（OCIイメージを保持）
    bridge = "bridge"    # Firecracker microVM + bridge（カーネル・rootfsを保持）


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(128), nullable=True)           # 表示名（任意）
    needs_username_setup = Column(Boolean, default=False, nullable=False)  # 初回ユーザ名設定が必要か
    email = Column(String(256), unique=True, nullable=True)
    hashed_password = Column(String(128), nullable=True)
    role = Column(Enum(UserRole), default=UserRole.user, nullable=False)
    auth_provider = Column(Enum(AuthProvider), default=AuthProvider.local, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login = Column(DateTime, nullable=True)

    environments = relationship("Environment", back_populates="user")
    owned_images = relationship("Image", back_populates="owner", foreign_keys="Image.owner_id")
    collaborations = relationship("ImageCollaborator", back_populates="user")


class Image(Base):
    __tablename__ = "images"

    id = Column(Integer, primary_key=True, index=True)
    # プロジェクト識別: owner_id/slug でパスを構成 (例: alice/cve-2021-4034)
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    name = Column(String(128), nullable=False, index=True)
    slug = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    backend = Column(Enum(Backend), default=Backend.macvlan, nullable=False)
    # macvlan バックエンド: Docker OCI イメージ
    oci_ref = Column(String(512), nullable=False, default="")
    archive_path = Column(String(512), nullable=True)
    # bridge バックエンド: Firecracker ゲストカーネル・rootfs
    kernel_path = Column(String(512), nullable=True)
    rootfs_path = Column(String(512), nullable=True)
    # rootfs としてファイルシステム tar がアップロード/リンクされた場合、ext4への変換が
    # バックグラウンドジョブ（services.rootfs_convert）で行われるまでの状態を管理する
    # （DefaultRootfsAsset と同じカラム構成・RootfsConversionStatus を共有）
    rootfs_conversion_status = Column(String(16), default=RootfsConversionStatus.ready.value, nullable=False)
    rootfs_conversion_error = Column(String(512), nullable=True)
    rootfs_source_archive_path = Column(String(512), nullable=True)
    # 「default」モードで選択された共有デフォルト資産への参照（編集画面の初期表示用）
    default_kernel_asset_id = Column(Integer, ForeignKey("default_kernel_assets.id"), nullable=True)
    default_rootfs_asset_id = Column(Integer, ForeignKey("default_rootfs_assets.id"), nullable=True)
    readme_path = Column(String(512), nullable=True)
    difficulty = Column(Enum(Difficulty), default=Difficulty.medium, nullable=False)
    category = Column(String(64), nullable=True)
    estimated_minutes = Column(Integer, default=60)
    timeout_minutes = Column(Integer, default=60)
    cpu_limit = Column(Integer, default=1)
    memory_limit_mb = Column(Integer, default=1024)
    is_active = Column(Boolean, default=True)
    visibility = Column(Enum(Visibility), default=Visibility.protected, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # owner_id + slug の組み合わせはユニーク
    __table_args__ = (UniqueConstraint("owner_id", "slug", name="uq_image_owner_slug"),)

    owner = relationship("User", back_populates="owned_images", foreign_keys=[owner_id])
    creator = relationship("User", foreign_keys=[created_by])
    environments = relationship("Environment", back_populates="image")
    collaborators = relationship("ImageCollaborator", back_populates="image")


class DefaultKernelAsset(Base):
    """管理者が登録した共有デフォルトゲストカーネル（bridge プロジェクト作成時に選択可能）。"""
    __tablename__ = "default_kernel_assets"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(128), nullable=False)
    file_path = Column(String(512), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class DefaultRootfsAsset(Base):
    """管理者が登録した共有デフォルト rootfs（bridge プロジェクト作成時に選択可能）。

    ext4 イメージを直接アップロードした場合は conversion_status="ready" のまま即利用可能。
    単純なファイルシステム tar をアップロードした場合は "pending" で登録され、
    バックグラウンドジョブ（services.rootfs_convert）が ext4 へ変換して file_path を差し替える。
    """
    __tablename__ = "default_rootfs_assets"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String(128), nullable=False)
    file_path = Column(String(512), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    conversion_status = Column(String(16), default=RootfsConversionStatus.ready.value, nullable=False)
    conversion_error = Column(String(512), nullable=True)
    source_archive_path = Column(String(512), nullable=True)


class ImageCollaborator(Base):
    __tablename__ = "image_collaborators"

    id = Column(Integer, primary_key=True, index=True)
    image_id = Column(Integer, ForeignKey("images.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    role = Column(Enum(CollaboratorRole), default=CollaboratorRole.read, nullable=False)
    added_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("image_id", "user_id", name="uq_collab_image_user"),)

    image = relationship("Image", back_populates="collaborators")
    user = relationship("User", back_populates="collaborations")


class Environment(Base):
    __tablename__ = "environments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    image_id = Column(Integer, ForeignKey("images.id"), nullable=False)
    vm_id = Column(String(128), nullable=True)
    ip_address = Column(String(45), nullable=True)
    status = Column(Enum(EnvStatus), default=EnvStatus.starting, nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    extended_count = Column(Integer, default=0, nullable=False)

    user = relationship("User", back_populates="environments")
    image = relationship("Image", back_populates="environments")
