"""Durable, append-only records for post-weighing label reprints.

These records intentionally do not point at the existing print job as a
mutable work item.  A reprint is a new request and a new station job whose
source payload is frozen at confirmation time.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ScmPostpesajeReprintRequest(db.Model):
    __tablename__ = "scm_postpesaje_reprint_request"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('QUEUED', 'COMPLETED', 'BLOCKED')",
            name="ck_scm_postpesaje_reprint_request_state",
        ),
        db.UniqueConstraint(
            "operation_id", name="uq_scm_postpesaje_reprint_request_operation"
        ),
    )

    request_id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    actor_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
    )
    motivo = db.Column(db.String(500), nullable=False)
    preview_digest = db.Column(db.String(64), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    renderer_version = db.Column(db.String(40), nullable=False)
    estado = db.Column(db.String(20), nullable=False, default="QUEUED")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now()
    )

    actor = db.relationship("Trabajador")
    items = db.relationship(
        "ScmPostpesajeReprintItem",
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="ScmPostpesajeReprintItem.sequence",
    )
    audits = db.relationship(
        "ScmPostpesajeReprintAudit",
        back_populates="request",
        cascade="all, delete-orphan",
        order_by="ScmPostpesajeReprintAudit.created_at",
    )


class ScmPostpesajeReprintItem(db.Model):
    __tablename__ = "scm_postpesaje_reprint_item"
    __table_args__ = (
        db.CheckConstraint("copies > 0", name="ck_scm_postpesaje_reprint_item_copies"),
        db.UniqueConstraint(
            "request_id", "source_label_id", name="uq_scm_postpesaje_reprint_item_source"
        ),
    )

    item_id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_postpesaje_reprint_request.request_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence = db.Column(db.Integer, nullable=False)
    source_label_id = db.Column(Uuid(as_uuid=True), nullable=False)
    manga_id = db.Column(
        db.Integer, db.ForeignKey("scm_manga.id", ondelete="RESTRICT"), nullable=False
    )
    weighing_id = db.Column(
        db.Integer, db.ForeignKey("scm_pesaje_manga.id", ondelete="RESTRICT"), nullable=False
    )
    copies = db.Column(db.Integer, nullable=False)
    source_payload_hash = db.Column(db.String(64), nullable=False)
    source_payload_json = db.Column(db.JSON, nullable=False)
    source_snapshot_json = db.Column(db.JSON, nullable=False)
    source_snapshot_hash = db.Column(db.String(64), nullable=False)

    request = db.relationship("ScmPostpesajeReprintRequest", back_populates="items")
    job = db.relationship(
        "ScmPostpesajeReprintJob",
        back_populates="item",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ScmPostpesajeReprintJob(db.Model):
    __tablename__ = "scm_postpesaje_reprint_job"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('QUEUED', 'CLAIMED', 'ACK_ACCEPTED', 'ACK_NOT_EMITTED', "
            "'ACK_UNCERTAIN', 'BLOCKED_SOURCE')",
            name="ck_scm_postpesaje_reprint_job_state",
        ),
        db.UniqueConstraint("item_id", name="uq_scm_postpesaje_reprint_job_item"),
        db.UniqueConstraint("attempt_id", name="uq_scm_postpesaje_reprint_job_attempt"),
    )

    job_id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    item_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_postpesaje_reprint_item.item_id", ondelete="CASCADE"),
        nullable=False,
    )
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
    )
    renderer_version = db.Column(db.String(40), nullable=False)
    authorized_copies = db.Column(db.Integer, nullable=False)
    source_payload_hash = db.Column(db.String(64), nullable=False)
    source_payload_json = db.Column(db.JSON, nullable=False)
    estado = db.Column(db.String(24), nullable=False, default="QUEUED")
    attempt_id = db.Column(Uuid(as_uuid=True), nullable=True)
    claimed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    acknowledged_at = db.Column(db.DateTime(timezone=True), nullable=True)
    ack_result_json = db.Column(db.JSON, nullable=True)

    item = db.relationship("ScmPostpesajeReprintItem", back_populates="job")


class ScmPostpesajeReprintAudit(db.Model):
    __tablename__ = "scm_postpesaje_reprint_audit"

    audit_id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_postpesaje_reprint_request.request_id", ondelete="RESTRICT"),
        nullable=False,
    )
    job_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_postpesaje_reprint_job.job_id", ondelete="RESTRICT"),
        nullable=True,
    )
    event = db.Column(db.String(32), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True)
    station_id = db.Column(db.String(36), nullable=True)
    details_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now()
    )

    request = db.relationship("ScmPostpesajeReprintRequest", back_populates="audits")
