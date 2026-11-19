"""Canonical return/union records for Mesa (Estación 2).

The station does not keep a second kg ledger.  A union is only a pending
identity until its attested scale capture is received; the receipt then uses
the normal ``scm_movimiento_inventario_kg`` and ``scm_existencia_manga_kg``
tables.  Origins are kept as a many-to-many audit projection because a union
may contain different lots without inventing a lot or prorating kilograms.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ScmMesaRetornoUnion(db.Model):
    __tablename__ = "scm_mesa_retorno_union"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('PENDIENTE_PESAJE', 'PESADA_PENDIENTE_CONFIRMACION', 'RECIBIDA')",
            name="ck_mesa_retorno_union_estado",
        ),
        db.UniqueConstraint("codigo", "ciclo", name="uq_mesa_retorno_union_code_cycle"),
        db.UniqueConstraint("operation_id", name="uq_mesa_retorno_union_operation"),
        db.UniqueConstraint("captura_token", name="uq_mesa_retorno_union_capture"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(120), nullable=False)
    ciclo = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    articulo_scm_id = db.Column(db.Integer, db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"), nullable=False)
    pieza_color_sku = db.Column(db.String(80), nullable=True)
    color = db.Column(db.String(120), nullable=True)
    zona_hallazgo = db.Column(db.String(64), nullable=True)
    estado = db.Column(db.String(32), nullable=False, default="PENDIENTE_PESAJE", server_default="PENDIENTE_PESAJE")
    unidad_fisica_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=False)
    bruto_kg = db.Column(db.Numeric(15, 3), nullable=True)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=True)
    neto_kg = db.Column(db.Numeric(15, 3), nullable=True)
    captura_token = db.Column(db.String(160), nullable=True)
    captura_base = db.Column(db.String(2), nullable=True)
    captura_at = db.Column(db.DateTime(timezone=True), nullable=True)
    captura_reading_id = db.Column(db.String(160), nullable=True)
    captura_payload_hash = db.Column(db.String(64), nullable=True)
    station_id = db.Column(db.String(80), nullable=False, default="ESTACION-2", server_default="ESTACION-2")
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    movimiento_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_movimiento_inventario_kg.id", ondelete="RESTRICT"), nullable=True)
    existencia_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_existencia_manga_kg.id", ondelete="RESTRICT"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    articulo = db.relationship("ScmArticulo")
    unidad_fisica = db.relationship("ScmUnidadFisicaKg", foreign_keys=[unidad_fisica_id])
    actor = db.relationship("Trabajador")
    origenes = db.relationship("ScmMesaRetornoOrigen", back_populates="union", cascade="all, delete-orphan", lazy="selectin")


class ScmMesaRetornoOrigen(db.Model):
    __tablename__ = "scm_mesa_retorno_origen"
    __table_args__ = (
        db.UniqueConstraint("union_id", "unidad_fisica_id", "ciclo", name="uq_mesa_retorno_origin_unit_cycle"),
        db.UniqueConstraint("retiro_id", "unidad_fisica_id", "ciclo", name="uq_mesa_retorno_origin_withdrawal_cycle"),
        db.CheckConstraint(
            "estado IN ('RESERVADO_UNION', 'VACIADO_TERMINAL')",
            name="ck_mesa_retorno_origin_state",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    union_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_mesa_retorno_union.id", ondelete="CASCADE"), nullable=False)
    ciclo = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    unidad_fisica_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=False)
    retiro_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_retiro_armado_kg.id", ondelete="RESTRICT"), nullable=True)
    lote_id = db.Column(db.String(120), nullable=True)
    lote_snapshot = db.Column(db.String(240), nullable=True)
    ot_date = db.Column(db.String(32), nullable=True)
    ot_shift = db.Column(db.String(32), nullable=True)
    neto_retirado_kg = db.Column(db.Numeric(15, 3), nullable=False)
    pieza_color_sku = db.Column(db.String(80), nullable=True)
    color = db.Column(db.String(120), nullable=True)
    estado = db.Column(db.String(24), nullable=False, default="RESERVADO_UNION", server_default="RESERVADO_UNION")
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    terminal_at = db.Column(db.DateTime(timezone=True), nullable=True)

    union = db.relationship("ScmMesaRetornoUnion", back_populates="origenes")
    unidad_fisica = db.relationship("ScmUnidadFisicaKg")
    retiro = db.relationship("ScmRetiroArmadoKg")
    actor = db.relationship("Trabajador")


class ScmMesaRetornoRecibo(db.Model):
    __tablename__ = "scm_mesa_retorno_recibo"
    __table_args__ = (
        db.UniqueConstraint("captura_token", name="uq_mesa_retorno_receipt_capture"),
        db.UniqueConstraint("operation_id", name="uq_mesa_retorno_receipt_operation"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    union_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_mesa_retorno_union.id", ondelete="RESTRICT"), nullable=False)
    ciclo = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    unidad_fisica_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=False)
    movimiento_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_movimiento_inventario_kg.id", ondelete="RESTRICT"), nullable=False)
    existencia_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_existencia_manga_kg.id", ondelete="RESTRICT"), nullable=False)
    captura_token = db.Column(db.String(160), nullable=False)
    neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    union = db.relationship("ScmMesaRetornoUnion")
    unidad_fisica = db.relationship("ScmUnidadFisicaKg")
    movimiento = db.relationship("ScmMovimientoInventarioKg")
    existencia = db.relationship("ScmExistenciaMangaKg")


__all__ = ["ScmMesaRetornoUnion", "ScmMesaRetornoOrigen", "ScmMesaRetornoRecibo"]
