"""Modelos declarativos SQLAlchemy 2.0 para todas as tabelas do PostgreSQL."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Classe base para todos os modelos declarativos ORM do SQLAlchemy 2.0."""
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "user_id"),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'viewer')",
            name="ck_organization_members_role",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default="owner")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    __table_args__ = (
        CheckConstraint("kind IN ('creator', 'video')", name="ck_prompt_templates_kind"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class PromptLastUsed(Base):
    __tablename__ = "prompt_last_used"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "kind"),
        CheckConstraint("kind IN ('creator', 'video')", name="ck_prompt_last_used_kind"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Creator(Base):
    __tablename__ = "creators"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "run_id", "creator_id"),
        CheckConstraint("status IN ('approved', 'rejected')", name="ck_creators_status"),
        CheckConstraint(
            "voice_status IN ('legacy', 'candidates_ready', 'selected', 'failed')",
            name="ck_creators_voice_status",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    run_id: Mapped[str] = mapped_column(Text)
    creator_id: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False, autoincrement=True)
    image_uri: Mapped[Optional[str]] = mapped_column(Text)
    voice_ref: Mapped[Optional[str]] = mapped_column(Text)
    voice_preview_uri: Mapped[Optional[str]] = mapped_column(Text)
    angles: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=func.text("'[]'::jsonb")
    )
    voice_reroll_count: Mapped[Optional[int]] = mapped_column(Integer)
    voice_spec: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=func.text("'{}'::jsonb")
    )
    voice_provider: Mapped[Optional[str]] = mapped_column(Text)
    voice_design_model: Mapped[Optional[str]] = mapped_column(Text)
    voice_tts_model: Mapped[Optional[str]] = mapped_column(Text)
    voice_design_hash: Mapped[Optional[str]] = mapped_column(Text)
    voice_selected_candidate: Mapped[Optional[str]] = mapped_column(Text)
    voice_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="legacy"
    )
    voice_design_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=func.text("'{}'::jsonb")
    )
    creator_prompt: Mapped[Optional[str]] = mapped_column(Text)
    video_prompt: Mapped[Optional[str]] = mapped_column(Text)
    offer: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class RunFeedback(Base):
    __tablename__ = "run_feedback"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "run_id"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    run_id: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False, autoincrement=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "id"),
        UniqueConstraint("organization_id", "storage_key", name="uq_artifacts_organization_storage_key"),
    )

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    id: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[Optional[str]] = mapped_column(Text)
    creator_id: Mapped[Optional[str]] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    storage_backend: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(Text)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger)
    sha256: Mapped[Optional[str]] = mapped_column(Text)
    source_uri: Mapped[Optional[str]] = mapped_column(Text)
    retention_class: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=func.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "id"),
        CheckConstraint(
            "phase IN ('running', 'editing', 'awaiting', 'review', 'done', 'error', 'cancelled')",
            name="ck_runs_phase",
        ),
        CheckConstraint(
            "batch_size IS NULL OR batch_size >= 0",
            name="ck_runs_batch_size",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    id: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False, autoincrement=True)
    offer: Mapped[Optional[str]] = mapped_column(Text)
    platform: Mapped[Optional[str]] = mapped_column(Text)
    batch_size: Mapped[Optional[int]] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=func.text("'{}'::jsonb")
    )
    state: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=func.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class RunItem(Base):
    __tablename__ = "run_items"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "run_id", "item_id"),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    run_id: Mapped[str] = mapped_column(Text)
    item_id: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(BigInteger, nullable=False, autoincrement=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry', 'succeeded', 'failed', 'cancelled')",
            name="ck_jobs_status",
        ),
        CheckConstraint("attempt >= 0", name="ck_jobs_attempt"),
        CheckConstraint("max_attempts > 0", name="ck_jobs_max_attempts"),
    )

    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=func.text("'{}'::jsonb")
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class RunGate(Base):
    __tablename__ = "run_gates"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "id"),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "organization_id",
            "run_id",
            "gate_type",
            "version",
            name="uq_run_gates_version",
        ),
        CheckConstraint(
            "status IN ('pending', 'resolved', 'cancelled')",
            name="ck_run_gates_status",
        ),
        CheckConstraint("version > 0", name="ck_run_gates_version"),
    )

    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    gate_type: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    resolution: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "seq"),
        ForeignKeyConstraint(
            ["organization_id", "run_id"],
            ["runs.organization_id", "runs.id"],
            ondelete="CASCADE",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    seq: Mapped[int] = mapped_column(BigInteger, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class Outbox(Base):
    __tablename__ = "outbox"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "id"),
        CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed')",
            name="ck_outbox_status",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    id: Mapped[int] = mapped_column(BigInteger, autoincrement=True)
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    message_key: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    worker_id: Mapped[Optional[str]] = mapped_column(Text)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ProviderQuota(Base):
    __tablename__ = "provider_quotas"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "provider"),
        CheckConstraint("limit_units >= 0", name="ck_provider_quotas_limit"),
        CheckConstraint(
            "used_units >= 0 AND used_units <= limit_units",
            name="ck_provider_quotas_usage",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    provider: Mapped[str] = mapped_column(Text)
    limit_units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_units: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class EffectLedger(Base):
    __tablename__ = "external_effects"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "effect_key"),
        CheckConstraint("units > 0", name="ck_external_effects_units"),
        CheckConstraint(
            "status IN ('reserved', 'succeeded', 'uncertain', 'failed')",
            name="ck_external_effects_status",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    effect_key: Mapped[str] = mapped_column(Text)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class LegacyImportBatch(Base):
    __tablename__ = "legacy_import_batches"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "id"),
    )

    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    id: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class LegacyImportEntry(Base):
    __tablename__ = "legacy_import_entries"
    __table_args__ = (
        PrimaryKeyConstraint("organization_id", "batch_id", "source_type", "source_id"),
        ForeignKeyConstraint(
            ["organization_id", "batch_id"],
            ["legacy_import_batches.organization_id", "legacy_import_batches.id"],
            ondelete="CASCADE",
        ),
    )

    organization_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    batch_id: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(Text)
    source_id: Mapped[str] = mapped_column(Text)
    target_id: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
