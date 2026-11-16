"""Shared central draft and visit records for in-process quality."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ScmVisitaCalidad(db.Model):
    __tablename__ = "scm_visita_calidad"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('INICIAL', 'PERIODICA')",
            name="ck_scm_visita_calidad_tipo",
        ),
        db.CheckConstraint(
            "estado IN ('DRAFT', 'CONFIRMED')",
            name="ck_scm_visita_calidad_estado",
        ),
        db.CheckConstraint("numero > 0", name="ck_scm_visita_calidad_numero"),
        db.UniqueConstraint(
            "ot_id", "trabajo_ot_id", "tipo", "numero",
            name="uq_scm_visita_calidad_contexto",
        ),
        db.UniqueConstraint(
            "operation_id", name="uq_scm_visita_calidad_operation",
        ),
        db.Index(
            "ix_scm_visita_calidad_ot_estado",
            "ot_id", "estado", "tipo",
        ),
        db.Index(
            "uq_scm_visita_calidad_confirmada_ot_numero",
            "ot_id", "numero", unique=True,
            sqlite_where=db.text("estado = 'CONFIRMED'"),
            postgresql_where=db.text("estado = 'CONFIRMED'"),
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ot_id = db.Column(
        db.Integer,
        db.ForeignKey("registro_diario_produccion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    trabajo_ot_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_trabajo_ot.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tipo = db.Column(db.String(16), nullable=False)
    numero = db.Column(db.Integer, nullable=False)
    estado = db.Column(
        db.String(16), nullable=False, default="DRAFT", server_default="DRAFT",
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    observed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    recorded_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=utc_now, server_default=db.func.now(),
    )
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False,
    )
    updated_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False,
    )
    confirmed_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True,
    )
    operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    payload_json = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")

    ot = db.relationship("RegistroDiarioProduccion")
    trabajo = db.relationship("ScmTrabajoOt")
    created_by = db.relationship("Trabajador", foreign_keys=[created_by_id])
    updated_by = db.relationship("Trabajador", foreign_keys=[updated_by_id])
    confirmed_by = db.relationship("Trabajador", foreign_keys=[confirmed_by_id])
    operation = db.relationship("ScmOperacion")

    def to_dict(self):
        return {
            "id": str(self.id),
            "ot_id": str(self.ot.public_id) if self.ot is not None else None,
            "trabajo_id": str(self.trabajo_ot_id),
            "trabajo_codigo": self.trabajo.codigo if self.trabajo is not None else None,
            "tipo": self.tipo,
            "numero": self.numero,
            "estado": self.estado,
            "version": self.version,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
            "payload": self.payload_json,
        }
