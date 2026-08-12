"""Kardex normalizado por articulo SCM para el piloto de planificacion."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ScmUbicacionInventario(db.Model):
    __tablename__ = "scm_ubicacion_inventario"
    __table_args__ = (
        db.UniqueConstraint("codigo", name="uq_scm_ubicacion_inventario_codigo"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    almacen_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_almacen.id", ondelete="RESTRICT"),
        nullable=True,
    )
    parent_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=True,
    )
    tipo = db.Column(db.String(24), nullable=True)
    permite_saldo_libre = db.Column(
        db.Boolean, nullable=False, default=True, server_default=db.true()
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    codigo = db.Column(db.String(40), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    clases_articulo_json = db.Column(
        db.JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    activo = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )

    almacen = db.relationship("ScmAlmacen", back_populates="ubicaciones")
    parent = db.relationship("ScmUbicacionInventario", remote_side=[id])

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "nombre": self.nombre,
            "activo": self.activo,
            "clases_articulo": list(self.clases_articulo_json or []),
            "almacen_id": str(self.almacen_id) if self.almacen_id else None,
            "tipo": self.tipo,
            "parent_id": self.parent_id,
            "permite_saldo_libre": self.permite_saldo_libre,
            "version": self.version,
        }


class ScmSaldoInventario(db.Model):
    __tablename__ = "scm_saldo_inventario"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_fisica >= 0 AND cantidad_reservada >= 0 "
            "AND cantidad_no_disponible >= 0 "
            "AND cantidad_reservada + cantidad_no_disponible "
            "<= cantidad_fisica",
            name="ck_scm_saldo_inventario_cantidades",
        ),
        db.UniqueConstraint(
            "articulo_scm_id",
            "ubicacion_id",
            name="uq_scm_saldo_inventario_articulo_ubicacion",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    articulo_scm_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ubicacion_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_fisica = db.Column(
        db.Numeric(15, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    cantidad_reservada = db.Column(
        db.Numeric(15, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    cantidad_no_disponible = db.Column(
        db.Numeric(15, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=db.func.now(),
    )

    articulo = db.relationship("ScmArticulo")
    ubicacion = db.relationship("ScmUbicacionInventario")


class ScmMovimientoInventario(db.Model):
    __tablename__ = "scm_movimiento_inventario"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('SALDO_INICIAL', 'INGRESO_PRODUCCION', "
            "'AJUSTE_POSITIVO', 'AJUSTE_NEGATIVO', 'CONSUMO', "
            "'TRASLADO_SALIDA', 'TRASLADO_ENTRADA', "
            "'RETORNO_SALIDA', 'RETORNO_ENTRADA')",
            name="ck_scm_movimiento_inventario_tipo",
        ),
        db.CheckConstraint(
            "cantidad_delta <> 0 AND saldo_fisico_resultante >= 0",
            name="ck_scm_movimiento_inventario_cantidad",
        ),
        db.UniqueConstraint(
            "operation_id",
            name="uq_scm_movimiento_inventario_operation",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    saldo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_saldo_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tipo = db.Column(db.String(32), nullable=False)
    cantidad_delta = db.Column(db.Numeric(15, 3), nullable=False)
    saldo_fisico_resultante = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    referencia_tipo = db.Column(db.String(40), nullable=True)
    referencia_id = db.Column(db.String(100), nullable=True)
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )

    saldo = db.relationship("ScmSaldoInventario")
    actor = db.relationship("Trabajador")


class ScmSaldoMaterialInventario(db.Model):
    __tablename__ = "scm_saldo_material_inventario"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_fisica_kg >= 0 AND cantidad_reservada_kg >= 0 "
            "AND cantidad_no_disponible_kg >= 0 "
            "AND cantidad_reservada_kg + cantidad_no_disponible_kg "
            "<= cantidad_fisica_kg",
            name="ck_scm_saldo_material_cantidades",
        ),
        db.UniqueConstraint(
            "material_id", "ubicacion_id",
            name="uq_scm_saldo_material_ubicacion",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    material_id = db.Column(
        db.Integer, db.ForeignKey("scm_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ubicacion_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_fisica_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    cantidad_reservada_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    cantidad_no_disponible_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )

    material = db.relationship("ScmMaterial")
    ubicacion = db.relationship("ScmUbicacionInventario")


class ScmMovimientoMaterialInventario(db.Model):
    __tablename__ = "scm_movimiento_material_inventario"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('SALDO_INICIAL', 'AJUSTE_POSITIVO', 'AJUSTE_NEGATIVO', "
            "'RESERVA', 'LIBERACION_RESERVA', 'EMISION', 'DEVOLUCION', "
            "'CONSUMO', 'INGRESO_MOLIENDA')",
            name="ck_scm_movimiento_material_tipo",
        ),
        db.CheckConstraint(
            "cantidad_delta_kg <> 0 AND saldo_fisico_resultante_kg >= 0",
            name="ck_scm_movimiento_material_cantidad",
        ),
        db.UniqueConstraint(
            "operation_id", name="uq_scm_movimiento_material_operation",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    saldo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_saldo_material_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tipo = db.Column(db.String(32), nullable=False)
    cantidad_delta_kg = db.Column(db.Numeric(15, 3), nullable=False)
    saldo_fisico_resultante_kg = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    referencia_tipo = db.Column(db.String(40), nullable=True)
    referencia_id = db.Column(db.String(100), nullable=True)
    actor_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    saldo = db.relationship("ScmSaldoMaterialInventario")
    actor = db.relationship("Trabajador")


class ScmLoteAperturaInventario(db.Model):
    """Corte inicial versionado que solo afecta Kardex al ser aprobado."""

    __tablename__ = "scm_lote_apertura_inventario"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('BORRADOR', 'PENDIENTE_APROBACION', "
            "'APLICADO', 'RECHAZADO')",
            name="ck_scm_lote_apertura_estado",
        ),
        db.UniqueConstraint("codigo", name="uq_scm_lote_apertura_codigo"),
        db.UniqueConstraint(
            "create_operation_id",
            name="uq_scm_lote_apertura_create_operation",
        ),
        db.UniqueConstraint(
            "approval_operation_id",
            name="uq_scm_lote_apertura_approval_operation",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(40), nullable=False)
    fecha_corte = db.Column(db.Date, nullable=False)
    motivo = db.Column(db.String(500), nullable=False)
    estado = db.Column(
        db.String(28), nullable=False, default="BORRADOR",
        server_default="BORRADOR",
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    creado_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    creado_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    enviado_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    enviado_at = db.Column(db.DateTime(timezone=True), nullable=True)
    resuelto_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    resuelto_at = db.Column(db.DateTime(timezone=True), nullable=True)
    motivo_resolucion = db.Column(db.String(500), nullable=True)
    create_operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    approval_operation_id = db.Column(Uuid(as_uuid=True), nullable=True)

    lineas = db.relationship(
        "ScmLoteAperturaLinea", back_populates="lote",
        cascade="all, delete-orphan", order_by="ScmLoteAperturaLinea.id",
    )
    creado_por = db.relationship("Trabajador", foreign_keys=[creado_por_id])
    enviado_por = db.relationship("Trabajador", foreign_keys=[enviado_por_id])
    resuelto_por = db.relationship("Trabajador", foreign_keys=[resuelto_por_id])


class ScmLoteAperturaLinea(db.Model):
    __tablename__ = "scm_lote_apertura_linea"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad > 0", name="ck_scm_lote_apertura_linea_cantidad",
        ),
        db.CheckConstraint(
            "estado_calidad IN ('LIBERADO', 'PENDIENTE')",
            name="ck_scm_lote_apertura_linea_calidad",
        ),
        db.CheckConstraint(
            "(articulo_scm_id IS NOT NULL) <> (material_scm_id IS NOT NULL)",
            name="ck_scm_lote_apertura_linea_item_unico",
        ),
        db.UniqueConstraint(
            "lote_id", "articulo_scm_id", "ubicacion_codigo",
            name="uq_scm_lote_apertura_linea_fuente",
        ),
        db.UniqueConstraint(
            "lote_id", "material_scm_id", "ubicacion_codigo",
            name="uq_scm_lote_apertura_linea_material_fuente",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    lote_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_lote_apertura_inventario.id", ondelete="CASCADE"),
        nullable=False,
    )
    articulo_scm_id = db.Column(
        db.Integer, db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"),
        nullable=True,
    )
    material_scm_id = db.Column(
        db.Integer, db.ForeignKey("scm_material.id", ondelete="RESTRICT"),
        nullable=True,
    )
    ubicacion_codigo = db.Column(db.String(40), nullable=False)
    ubicacion_nombre = db.Column(db.String(120), nullable=False)
    cantidad = db.Column(db.Numeric(15, 3), nullable=False)
    estado_calidad = db.Column(
        db.String(16), nullable=False, default="LIBERADO",
        server_default="LIBERADO",
    )
    observacion = db.Column(db.String(500), nullable=True)
    movimiento_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_movimiento_inventario.id", ondelete="RESTRICT"),
        nullable=True,
    )
    movimiento_material_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_movimiento_material_inventario.id", ondelete="RESTRICT"),
        nullable=True,
    )

    lote = db.relationship("ScmLoteAperturaInventario", back_populates="lineas")
    articulo = db.relationship("ScmArticulo")
    material = db.relationship("ScmMaterial")
    movimiento = db.relationship("ScmMovimientoInventario")
    movimiento_material = db.relationship("ScmMovimientoMaterialInventario")


class ScmReservaInventario(db.Model):
    __tablename__ = "scm_reserva_inventario"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad > 0",
            name="ck_scm_reserva_inventario_cantidad",
        ),
        db.CheckConstraint(
            "estado IN ('RESERVADA', 'CONSUMIDA', 'LIBERADA')",
            name="ck_scm_reserva_inventario_estado",
        ),
        db.UniqueConstraint(
            "plan_produccion_id",
            "saldo_id",
            "orden_produccion_linea_id",
            "uso",
            name="uq_scm_reserva_inventario_plan_fuente",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_produccion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_plan_produccion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    orden_produccion_linea_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_produccion_linea.id", ondelete="RESTRICT"),
        nullable=False,
    )
    saldo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_saldo_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    articulo_scm_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"),
        nullable=False,
    )
    uso = db.Column(db.String(40), nullable=False)
    cantidad = db.Column(db.Numeric(15, 3), nullable=False)
    estado = db.Column(
        db.String(20),
        nullable=False,
        default="RESERVADA",
        server_default="RESERVADA",
    )
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )

    saldo = db.relationship("ScmSaldoInventario")
    articulo = db.relationship("ScmArticulo")
    actor = db.relationship("Trabajador")
    orden_produccion_linea = db.relationship(
        "ScmOrdenProduccionLinea",
        back_populates="reservas_inventario",
    )
