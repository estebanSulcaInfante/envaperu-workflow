"""add isolated postpesaje reprint request and station job ledger

Revision ID: fd1a2b3c4d50
Revises: fc3d4e5f6071
"""

from alembic import op
import sqlalchemy as sa


revision = "fd1a2b3c4d50"
down_revision = "fc3d4e5f6071"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_postpesaje_reprint_request",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("station_id", sa.String(36), nullable=False),
        sa.Column("motivo", sa.String(500), nullable=False),
        sa.Column("preview_digest", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("renderer_version", sa.String(40), nullable=False),
        sa.Column("estado", sa.String(20), nullable=False, server_default="QUEUED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["station_id"], ["estacion_pesaje.station_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("request_id"),
        sa.UniqueConstraint("operation_id", name="uq_scm_postpesaje_reprint_request_operation"),
        sa.CheckConstraint("estado IN ('QUEUED', 'COMPLETED', 'BLOCKED')", name="ck_scm_postpesaje_reprint_request_state"),
    )
    op.create_table(
        "scm_postpesaje_reprint_item",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_label_id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("weighing_id", sa.Integer(), nullable=False),
        sa.Column("copies", sa.Integer(), nullable=False),
        sa.Column("source_payload_hash", sa.String(64), nullable=False),
        sa.Column("source_payload_json", sa.JSON(), nullable=False),
        sa.Column("source_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["scm_postpesaje_reprint_request.request_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["weighing_id"], ["scm_pesaje_manga.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("item_id"),
        sa.UniqueConstraint("request_id", "source_label_id", name="uq_scm_postpesaje_reprint_item_source"),
        sa.CheckConstraint("copies > 0", name="ck_scm_postpesaje_reprint_item_copies"),
    )
    op.create_table(
        "scm_postpesaje_reprint_job",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("station_id", sa.String(36), nullable=False),
        sa.Column("renderer_version", sa.String(40), nullable=False),
        sa.Column("authorized_copies", sa.Integer(), nullable=False),
        sa.Column("source_payload_hash", sa.String(64), nullable=False),
        sa.Column("source_payload_json", sa.JSON(), nullable=False),
        sa.Column("estado", sa.String(24), nullable=False, server_default="QUEUED"),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ack_result_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["item_id"], ["scm_postpesaje_reprint_item.item_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["station_id"], ["estacion_pesaje.station_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint("item_id", name="uq_scm_postpesaje_reprint_job_item"),
        sa.UniqueConstraint("attempt_id", name="uq_scm_postpesaje_reprint_job_attempt"),
        sa.CheckConstraint("estado IN ('QUEUED', 'CLAIMED', 'ACK_ACCEPTED', 'ACK_NOT_EMITTED', 'ACK_UNCERTAIN', 'BLOCKED_SOURCE')", name="ck_scm_postpesaje_reprint_job_state"),
    )
    op.create_table(
        "scm_postpesaje_reprint_audit",
        sa.Column("audit_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("station_id", sa.String(36), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["request_id"], ["scm_postpesaje_reprint_request.request_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["scm_postpesaje_reprint_job.job_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("audit_id"),
    )
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT 'MANGA_ETIQUETA_POST_REIMPRIMIR', 'Solicitar reimpresion postpesaje', true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad WHERE codigo = 'MANGA_ETIQUETA_POST_REIMPRIMIR'
        )
    """))


def downgrade():
    op.drop_table("scm_postpesaje_reprint_audit")
    op.drop_table("scm_postpesaje_reprint_job")
    op.drop_table("scm_postpesaje_reprint_item")
    op.drop_table("scm_postpesaje_reprint_request")
    op.execute(sa.text("DELETE FROM scm_capacidad WHERE codigo = 'MANGA_ETIQUETA_POST_REIMPRIMIR'"))
