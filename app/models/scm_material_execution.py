"""Requerimientos, reservas y movimientos de material para una corrida de OF."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ScmRequerimientoMaterial(db.Model):
    __tablename__ = "scm_requerimiento_material"
    __table_args__ = (
        db.CheckConstraint("cantidad_plan_kg > 0", name="ck_scm_req_material_cantidad"),
        db.CheckConstraint(
            "tipo_componente IN ('MATERIA_PRIMA', 'COLORANTE', 'ADITIVO')",
            name="ck_scm_req_material_tipo",
        ),
        db.UniqueConstraint(
            "corrida_fabricacion_id", "material_id",
            name="uq_scm_req_material_corrida_material",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    corrida_fabricacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_corrida_fabricacion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    material_id = db.Column(
        db.Integer, db.ForeignKey("scm_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tipo_componente = db.Column(db.String(20), nullable=False)
    cantidad_plan_kg = db.Column(db.Numeric(15, 3), nullable=False)
    receta_revision_id = db.Column(
        db.Integer, db.ForeignKey("receta_color_maestra.id", ondelete="RESTRICT"),
        nullable=False,
    )
    calculo_snapshot_json = db.Column(db.JSON, nullable=False)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    corrida = db.relationship("ScmCorridaFabricacion")
    material = db.relationship("ScmMaterial")
    reservas = db.relationship(
        "ScmReservaMaterial", back_populates="requerimiento",
        cascade="all, delete-orphan", lazy="selectin",
    )


class ScmReservaMaterial(db.Model):
    __tablename__ = "scm_reserva_material"
    __table_args__ = (
        db.CheckConstraint("cantidad_kg > 0", name="ck_scm_reserva_material_cantidad"),
        db.CheckConstraint(
            "emitida_neta_kg >= 0 AND cantidad_consumida_kg >= 0 AND "
            "emitida_neta_kg + cantidad_consumida_kg <= cantidad_kg",
            name="ck_scm_reserva_material_emitida",
        ),
        db.CheckConstraint(
            "estado IN ('ACTIVA', 'LIBERADA')", name="ck_scm_reserva_material_estado",
        ),
        db.UniqueConstraint(
            "requerimiento_id", "saldo_material_id",
            name="uq_scm_reserva_material_fuente",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requerimiento_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_requerimiento_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    saldo_material_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_saldo_material_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_kg = db.Column(db.Numeric(15, 3), nullable=False)
    emitida_neta_kg = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0",
    )
    cantidad_consumida_kg = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0",
    )
    estado = db.Column(
        db.String(16), nullable=False, default="ACTIVA", server_default="ACTIVA",
    )
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    requerimiento = db.relationship("ScmRequerimientoMaterial", back_populates="reservas")
    saldo = db.relationship("ScmSaldoMaterialInventario")
    emisiones = db.relationship(
        "ScmEmisionMaterial", back_populates="reserva",
        cascade="all, delete-orphan", lazy="selectin",
    )


class ScmEmisionMaterial(db.Model):
    __tablename__ = "scm_emision_material"
    __table_args__ = (
        db.CheckConstraint("cantidad_kg > 0", name="ck_scm_emision_material_cantidad"),
        db.CheckConstraint(
            "cantidad_devuelta_kg >= 0 AND cantidad_consumida_kg >= 0 AND "
            "cantidad_devuelta_kg + cantidad_consumida_kg <= cantidad_kg",
            name="ck_scm_emision_material_devuelta",
        ),
        db.UniqueConstraint("operation_id", name="uq_scm_emision_material_operation"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reserva_id = db.Column(
        Uuid(as_uuid=True), db.ForeignKey("scm_reserva_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    saldo_destino_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_saldo_material_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_kg = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_devuelta_kg = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0",
    )
    cantidad_consumida_kg = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0",
    )
    motivo = db.Column(db.String(240), nullable=False)
    actor_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False,
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    reserva = db.relationship("ScmReservaMaterial", back_populates="emisiones")
    saldo_destino = db.relationship("ScmSaldoMaterialInventario")
    devoluciones = db.relationship(
        "ScmDevolucionMaterial", back_populates="emision",
        cascade="all, delete-orphan", lazy="selectin",
    )


class ScmDevolucionMaterial(db.Model):
    __tablename__ = "scm_devolucion_material"
    __table_args__ = (
        db.CheckConstraint("cantidad_kg > 0", name="ck_scm_devolucion_material_cantidad"),
        db.UniqueConstraint("operation_id", name="uq_scm_devolucion_material_operation"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    emision_id = db.Column(
        Uuid(as_uuid=True), db.ForeignKey("scm_emision_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_kg = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    actor_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False,
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    emision = db.relationship("ScmEmisionMaterial", back_populates="devoluciones")


class ScmLotePremezcla(db.Model):
    __tablename__ = "scm_lote_premezcla"
    __table_args__ = (
        db.CheckConstraint("cantidad_kg > 0", name="ck_scm_lote_premezcla_cantidad"),
        db.CheckConstraint(
            "genealogia_tipo IN ('EXACTA', 'CONJUNTO_CANDIDATOS')",
            name="ck_scm_lote_premezcla_genealogia",
        ),
        db.CheckConstraint(
            "estado IN ('DISPONIBLE_MAQUINA', 'CONSUMIDO_MAQUINA', 'ANULADO')",
            name="ck_scm_lote_premezcla_estado",
        ),
        db.UniqueConstraint("codigo", name="uq_scm_lote_premezcla_codigo"),
        db.UniqueConstraint("operation_id", name="uq_scm_lote_premezcla_operation"),
        db.UniqueConstraint(
            "corrida_fabricacion_id", "secuencia",
            name="uq_scm_lote_premezcla_corrida_secuencia",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(64), nullable=False)
    corrida_fabricacion_id = db.Column(
        Uuid(as_uuid=True), db.ForeignKey("scm_corrida_fabricacion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    secuencia = db.Column(db.Integer, nullable=False)
    cantidad_kg = db.Column(db.Numeric(15, 3), nullable=False)
    genealogia_tipo = db.Column(db.String(32), nullable=False, default="EXACTA", server_default="EXACTA")
    estado = db.Column(db.String(24), nullable=False, default="DISPONIBLE_MAQUINA", server_default="DISPONIBLE_MAQUINA")
    ubicacion_codigo = db.Column(db.String(40), nullable=False, default="PREPARACION_PRODUCCION", server_default="PREPARACION_PRODUCCION")
    motivo = db.Column(db.String(240), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    corrida = db.relationship("ScmCorridaFabricacion", back_populates="corrida_premezclas")
    inputs = db.relationship(
        "ScmLotePremezclaInput", back_populates="lote", cascade="all, delete-orphan",
        lazy="selectin",
    )


class ScmLotePremezclaInput(db.Model):
    __tablename__ = "scm_lote_premezcla_input"
    __table_args__ = (
        db.CheckConstraint("cantidad_kg > 0", name="ck_scm_lote_premezcla_input_cantidad"),
        db.UniqueConstraint("lote_premezcla_id", "emision_id", name="uq_scm_lote_premezcla_input_emision"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lote_premezcla_id = db.Column(
        Uuid(as_uuid=True), db.ForeignKey("scm_lote_premezcla.id", ondelete="CASCADE"), nullable=False,
    )
    emision_id = db.Column(
        Uuid(as_uuid=True), db.ForeignKey("scm_emision_material.id", ondelete="RESTRICT"), nullable=False,
    )
    cantidad_kg = db.Column(db.Numeric(15, 3), nullable=False)

    lote = db.relationship("ScmLotePremezcla", back_populates="inputs")
    emision = db.relationship("ScmEmisionMaterial")
