import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class ScmOperacion(db.Model):
    __tablename__ = "scm_operacion"
    __table_args__ = (
        db.CheckConstraint(
            "length(request_sha256) = 64",
            name="ck_scm_operacion_request_sha256",
        ),
        db.CheckConstraint(
            "estado_http IS NULL OR estado_http BETWEEN 100 AND 599",
            name="ck_scm_operacion_estado_http",
        ),
    )

    operation_id = db.Column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    endpoint = db.Column(db.String(255), nullable=False)
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_operacion_actor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    request_sha256 = db.Column(db.String(64), nullable=False)
    estado_http = db.Column(db.Integer, nullable=True)
    response_json = db.Column(db.JSON, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )

    actor = db.relationship("Trabajador")
    eventos = db.relationship(
        "ScmEvento",
        back_populates="operacion",
        lazy="selectin",
    )

    def to_dict(self):
        return {
            "operation_id": (
                str(self.operation_id) if self.operation_id else None
            ),
            "endpoint": self.endpoint,
            "actor_id": self.actor_id,
            "request_sha256": self.request_sha256,
            "estado_http": self.estado_http,
            "response_json": self.response_json,
            "created_at": _isoformat(self.created_at),
        }


class ScmEvento(db.Model):
    __tablename__ = "scm_evento"
    __table_args__ = (
        db.Index(
            "ix_scm_evento_aggregate",
            "aggregate_type",
            "aggregate_id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    aggregate_type = db.Column(db.String(64), nullable=False)
    # UUIDs de OP/OF/OE y enteros legacy comparten el mismo journal.
    aggregate_id = db.Column(db.String(64), nullable=False)
    tipo = db.Column(db.String(64), nullable=False)
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_evento_actor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    actor_snapshot = db.Column(db.JSON, nullable=False)
    motivo = db.Column(db.Text, nullable=True)
    before_json = db.Column(db.JSON, nullable=True)
    after_json = db.Column(db.JSON, nullable=True)
    operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_operacion.operation_id",
            name="fk_scm_evento_operacion",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    occurred_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )

    actor = db.relationship("Trabajador")
    operacion = db.relationship(
        "ScmOperacion",
        back_populates="eventos",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "tipo": self.tipo,
            "actor_id": self.actor_id,
            "actor_snapshot": self.actor_snapshot,
            "motivo": self.motivo,
            "before_json": self.before_json,
            "after_json": self.after_json,
            "operation_id": (
                str(self.operation_id) if self.operation_id else None
            ),
            "occurred_at": _isoformat(self.occurred_at),
        }
