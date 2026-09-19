"""Sublibro protegido de piezas/WIP expresado en kg netos medidos."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _decimal(value):
    return format(Decimal(value).quantize(Decimal("0.001")), "f") if value is not None else None


class ScmSaldoInventarioKg(db.Model):
    __tablename__ = "scm_saldo_inventario_kg"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_fisica_kg >= 0 AND cantidad_reservada_kg >= 0 "
            "AND cantidad_no_disponible_kg >= 0 AND "
            "cantidad_reservada_kg + cantidad_no_disponible_kg <= cantidad_fisica_kg",
            name="ck_scm_saldo_inventario_kg_cantidades",
        ),
        db.CheckConstraint(
            "atributo_proceso IN ('PROCESO', 'TERMINADA', 'MIXTA')",
            name="ck_scm_saldo_inventario_kg_atributo_proceso",
        ),
        db.UniqueConstraint(
            "articulo_scm_id", "ubicacion_id",
            name="uq_scm_saldo_inventario_kg_articulo_ubicacion",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    articulo_scm_id = db.Column(db.Integer, db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"), nullable=False)
    ubicacion_id = db.Column(db.Integer, db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"), nullable=False)
    cantidad_fisica_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    cantidad_reservada_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    cantidad_no_disponible_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    cantidad_retirada_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    atributo_proceso = db.Column(db.String(16), nullable=False, default="PROCESO", server_default="PROCESO")
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    articulo = db.relationship("ScmArticulo")
    ubicacion = db.relationship("ScmUbicacionInventario")
    movimientos = db.relationship("ScmMovimientoInventarioKg", back_populates="saldo")

    @property
    def unidad(self):
        return "KG"

    @property
    def cantidad_libre_kg(self):
        return Decimal(self.cantidad_fisica_kg) - Decimal(self.cantidad_reservada_kg) - Decimal(self.cantidad_no_disponible_kg)

    def to_dict(self):
        return {
            "id": str(self.id),
            "articulo_scm_id": self.articulo_scm_id,
            "articulo": {
                "id": self.articulo.id,
                "codigo": self.articulo.codigo,
                "nombre": self.articulo.nombre,
                "clase": self.articulo.clase,
                "unidad": "KG",
                "unidad_inventario": "KG",
            },
            "ubicacion": self.ubicacion.to_dict(),
            "cantidad_fisica": _decimal(self.cantidad_fisica_kg),
            "cantidad_reservada": _decimal(self.cantidad_reservada_kg),
            "cantidad_no_disponible": _decimal(self.cantidad_no_disponible_kg),
            "cantidad_retirada": _decimal(self.cantidad_retirada_kg),
            "cantidad_libre": _decimal(self.cantidad_libre_kg),
            "cantidad_medida_kg": _decimal(self.cantidad_fisica_kg),
            "cantidad_disponible_kg": _decimal(self.cantidad_libre_kg),
            "cantidad_comprometida_kg": _decimal(self.cantidad_reservada_kg),
            "atributo_proceso": self.atributo_proceso,
            "unidad": "KG",
            "unidad_inventario": "KG",
            "version": self.version,
            "updated_at": _iso(self.updated_at),
        }


class ScmMovimientoInventarioKg(db.Model):
    __tablename__ = "scm_movimiento_inventario_kg"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('INGRESO_PRODUCCION', 'AJUSTE_POSITIVO', 'AJUSTE_NEGATIVO', "
            "'TRASLADO_SALIDA', 'TRASLADO_ENTRADA', 'RETIRO_ARMADO', 'RETORNO_ENTRADA')",
            name="ck_scm_movimiento_inventario_kg_tipo",
        ),
        db.CheckConstraint(
            "cantidad_delta_kg <> 0 AND saldo_fisico_resultante_kg >= 0",
            name="ck_scm_movimiento_inventario_kg_cantidad",
        ),
        db.UniqueConstraint("operation_id", name="uq_scm_movimiento_inventario_kg_operation"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    saldo_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_saldo_inventario_kg.id", ondelete="RESTRICT"), nullable=False)
    tipo = db.Column(db.String(32), nullable=False)
    cantidad_delta_kg = db.Column(db.Numeric(15, 3), nullable=False)
    saldo_fisico_resultante_kg = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    referencia_tipo = db.Column(db.String(40), nullable=True)
    referencia_id = db.Column(db.String(100), nullable=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    pesaje_public_id = db.Column(Uuid(as_uuid=True), nullable=True)
    correccion_aplicada_public_id = db.Column(Uuid(as_uuid=True), nullable=True)
    projection_sha256 = db.Column(db.String(64), nullable=False)
    peso_neto_snapshot_kg = db.Column(db.Numeric(15, 3), nullable=False)
    pesada_at_snapshot = db.Column(db.DateTime(timezone=True), nullable=False)
    fuente_tipo = db.Column(db.String(32), nullable=False, default="KG001", server_default="KG001")
    atributo_proceso = db.Column(db.String(16), nullable=False, default="PROCESO", server_default="PROCESO")
    medicion_unidad_kg_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_medicion_unidad_kg.id", ondelete="RESTRICT"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    saldo = db.relationship("ScmSaldoInventarioKg", back_populates="movimientos")
    actor = db.relationship("Trabajador")

    def to_dict(self):
        return {
            "id": str(self.id), "tipo": self.tipo,
            "cantidad_delta": _decimal(self.cantidad_delta_kg),
            "saldo_fisico_resultante": _decimal(self.saldo_fisico_resultante_kg),
            "unidad": "KG", "unidad_inventario": "KG",
            "motivo": self.motivo, "referencia_tipo": self.referencia_tipo,
            "referencia_id": self.referencia_id, "actor_id": self.actor_id,
            "operation_id": str(self.operation_id),
            "pesaje_public_id": str(self.pesaje_public_id) if self.pesaje_public_id else None,
            "correccion_aplicada_public_id": str(self.correccion_aplicada_public_id) if self.correccion_aplicada_public_id else None,
            "projection_sha256": self.projection_sha256,
            "atributo_proceso": self.atributo_proceso,
            "peso_neto_snapshot_kg": _decimal(self.peso_neto_snapshot_kg),
            "created_at": _iso(self.created_at),
        }


class ScmExistenciaMangaKg(db.Model):
    __tablename__ = "scm_existencia_manga_kg"
    __table_args__ = (
        db.CheckConstraint(
            "estado_logistico IN ('EN_PRODUCCION', 'DISPONIBLE_PRODUCCION', 'RECIBIDA_ALMACEN', 'REVERSADA')",
            name="ck_scm_existencia_manga_kg_logistica",
        ),
        db.CheckConstraint(
            "estado_calidad IN ('SIN_CONTROL', 'PENDIENTE', 'LIBERADA', 'BLOQUEADA', 'RECHAZADA')",
            name="ck_scm_existencia_manga_kg_calidad",
        ),
        db.CheckConstraint("cantidad_fisica_kg > 0 AND cantidad_reservada_kg >= 0 AND cantidad_reservada_kg <= cantidad_fisica_kg", name="ck_scm_existencia_manga_kg_cantidad"),
        db.CheckConstraint("atributo_proceso IN ('PROCESO', 'TERMINADA')", name="ck_scm_existencia_manga_kg_atributo_proceso"),
        db.UniqueConstraint("manga_id", name="uq_scm_existencia_manga_kg_manga"),
        db.UniqueConstraint("movimiento_ingreso_id", name="uq_scm_existencia_manga_kg_movimiento"),
        db.UniqueConstraint("operation_id", name="uq_scm_existencia_manga_kg_operation"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manga_id = db.Column(db.Integer, db.ForeignKey("scm_manga.id", ondelete="RESTRICT"), nullable=True)
    sesion_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_sesion_recepcion_manga.id", ondelete="RESTRICT"), nullable=True)
    etiqueta_resuelta_id = db.Column(db.Integer, db.ForeignKey("scm_etiqueta_manga.id", ondelete="RESTRICT"), nullable=True)
    unidad_fisica_kg_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=True)
    articulo_scm_id = db.Column(db.Integer, db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"), nullable=False)
    saldo_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_saldo_inventario_kg.id", ondelete="RESTRICT"), nullable=False)
    ubicacion_id = db.Column(db.Integer, db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"), nullable=False)
    movimiento_ingreso_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_movimiento_inventario_kg.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    resuelta_por = db.Column(db.String(24), nullable=False)
    estado_logistico = db.Column(db.String(32), nullable=False, default="RECIBIDA_ALMACEN", server_default="RECIBIDA_ALMACEN")
    estado_calidad = db.Column(db.String(20), nullable=False, default="SIN_CONTROL", server_default="SIN_CONTROL")
    cantidad_fisica_kg = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_reservada_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    peso_neto_snapshot_kg = db.Column(db.Numeric(15, 3), nullable=False)
    pesaje_public_id = db.Column(Uuid(as_uuid=True), nullable=True)
    correccion_aplicada_public_id = db.Column(Uuid(as_uuid=True), nullable=True)
    projection_sha256 = db.Column(db.String(64), nullable=True)
    pesada_at_snapshot = db.Column(db.DateTime(timezone=True), nullable=False)
    recibida_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    recibida_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    origen_tipo = db.Column(db.String(16), nullable=False, default="PRODUCCION", server_default="PRODUCCION")
    atributo_proceso = db.Column(db.String(16), nullable=False, default="PROCESO", server_default="PROCESO")
    calidad_actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True)
    calidad_at = db.Column(db.DateTime(timezone=True), nullable=True)
    calidad_motivo = db.Column(db.String(500), nullable=True)
    calidad_evidencia = db.Column(db.String(500), nullable=True)

    manga = db.relationship("ScmManga")
    sesion = db.relationship("ScmSesionRecepcionManga")
    etiqueta = db.relationship("ScmEtiquetaManga")
    articulo = db.relationship("ScmArticulo")
    saldo = db.relationship("ScmSaldoInventarioKg")
    ubicacion = db.relationship("ScmUbicacionInventario")
    movimiento_ingreso = db.relationship("ScmMovimientoInventarioKg")
    unidad_fisica_kg = db.relationship(
        "ScmUnidadFisicaKg",
        foreign_keys=[unidad_fisica_kg_id],
        uselist=False,
        post_update=True,
    )
    recibida_por = db.relationship("Trabajador", foreign_keys=[recibida_por_id])
    calidad_actor = db.relationship("Trabajador", foreign_keys=[calidad_actor_id])

    @property
    def cantidad_libre_kg(self):
        if self.estado_calidad not in {"LIBERADA", "SIN_CONTROL"} or self.estado_logistico not in {"DISPONIBLE_PRODUCCION", "RECIBIDA_ALMACEN"}:
            return Decimal("0")
        return Decimal(self.cantidad_fisica_kg) - Decimal(self.cantidad_reservada_kg)

    def to_dict(self):
        return {
            "id": str(self.id), "manga_id": str(self.manga.public_id) if self.manga else None,
            "manga_codigo": self.manga.codigo if self.manga else None, "sesion_id": str(self.sesion_id) if self.sesion_id else None,
            "etiqueta_id": str(self.etiqueta.public_id) if self.etiqueta else None,
            "articulo": {"id": self.articulo.id, "codigo": self.articulo.codigo, "nombre": self.articulo.nombre, "clase": self.articulo.clase, "unidad": "KG", "unidad_inventario": "KG"},
            "ubicacion": self.ubicacion.to_dict(), "unidad": "KG", "unidad_inventario": "KG",
            "estado_logistico": self.estado_logistico, "estado_calidad": self.estado_calidad,
            "cantidad_fisica": _decimal(self.cantidad_fisica_kg), "cantidad_reservada": _decimal(self.cantidad_reservada_kg),
            "cantidad_no_disponible": _decimal(self.cantidad_fisica_kg if self.estado_calidad not in {"LIBERADA", "SIN_CONTROL"} else 0),
            "cantidad_libre": _decimal(self.cantidad_libre_kg), "peso_neto_snapshot_kg": _decimal(self.peso_neto_snapshot_kg),
            "pesaje_public_id": str(self.pesaje_public_id) if self.pesaje_public_id else None, "correccion_aplicada_public_id": str(self.correccion_aplicada_public_id) if self.correccion_aplicada_public_id else None,
            "projection_sha256": self.projection_sha256, "pesada_at": _iso(self.pesada_at_snapshot),
            "recibida_por_id": self.recibida_por_id, "recibida_at": _iso(self.recibida_at), "version": self.version,
            "unidad_fisica_kg_id": str(self.unidad_fisica_kg_id) if self.unidad_fisica_kg_id else None,
            "origen_tipo": self.origen_tipo,
            "atributo_proceso": self.atributo_proceso,
            "calidad_actor_id": self.calidad_actor_id, "calidad_at": _iso(self.calidad_at),
            "calidad_motivo": self.calidad_motivo, "calidad_evidencia": self.calidad_evidencia,
        }


class ScmUnidadFisicaKg(db.Model):
    """Canonical identity for one measured physical KG object."""
    __tablename__ = "scm_unidad_fisica_kg"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('ACTIVA', 'HISTORICA')",
            name="ck_scm_unidad_fisica_kg_estado",
        ),
        db.CheckConstraint(
            "estado_logistico IN ('EN_PRODUCCION', 'DISPONIBLE_PRODUCCION', 'RECIBIDA_ALMACEN', 'ALMACENADA_CONTROLADA', 'RESERVADA', 'RETIRADA_ARMADO', 'REPESADA_PENDIENTE_RECEPCION', 'PENDIENTE_CALIDAD', 'PENDIENTE_VERIFICACION', 'REVERSADA')",
            name="ck_scm_unidad_fisica_kg_logistica",
        ),
        db.CheckConstraint("(kg_entregado IS NULL OR kg_entregado > 0) AND (kg_verificados IS NULL OR kg_verificados > 0)", name="ck_scm_unidad_fisica_kg_kg"),
        db.UniqueConstraint("public_id", name="uq_scm_unidad_fisica_kg_public_id"),
        db.UniqueConstraint("codigo", name="uq_scm_unidad_fisica_kg_codigo"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    public_id = db.Column(Uuid(as_uuid=True), nullable=False, default=uuid.uuid4)
    codigo = db.Column(db.String(120), nullable=False)
    articulo_scm_id = db.Column(db.Integer, db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"), nullable=False)
    existencia_manga_kg_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_existencia_manga_kg.id", ondelete="RESTRICT"), nullable=True)
    unidad_padre_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=True)
    unidad_raiz_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=True)
    division_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_division_unidad_kg.id", ondelete="RESTRICT"), nullable=True)
    intencion = db.Column(db.String(16), nullable=True)
    atributo_proceso = db.Column(db.String(16), nullable=False, default="PROCESO", server_default="PROCESO")
    estado = db.Column(db.String(16), nullable=False, default="ACTIVA", server_default="ACTIVA")
    estado_logistico = db.Column(db.String(40), nullable=False, default="RECIBIDA_ALMACEN", server_default="RECIBIDA_ALMACEN")
    estado_calidad = db.Column(db.String(20), nullable=False, default="SIN_CONTROL", server_default="SIN_CONTROL")
    saldo_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_saldo_inventario_kg.id", ondelete="RESTRICT"), nullable=True)
    ubicacion_id = db.Column(db.Integer, db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"), nullable=True)
    recepcion_vigente_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_existencia_manga_kg.id", ondelete="RESTRICT"), nullable=True)
    medicion_vigente_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_medicion_unidad_kg.id", ondelete="RESTRICT"), nullable=True)
    modo_lectura = db.Column(db.String(32), nullable=True)
    tara_contexto_json = db.Column(db.JSON, nullable=True)
    kg_entregado = db.Column(db.Numeric(15, 3), nullable=True)
    kg_verificados = db.Column(db.Numeric(15, 3), nullable=True)
    kg_verificados_at = db.Column(db.DateTime(timezone=True), nullable=True)
    almacen_responsable_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_almacen.id", ondelete="RESTRICT"), nullable=True)
    tenedor_fisico_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    articulo = db.relationship("ScmArticulo")
    ubicacion = db.relationship("ScmUbicacionInventario")
    saldo = db.relationship("ScmSaldoInventarioKg")
    padre = db.relationship("ScmUnidadFisicaKg", remote_side=[id], foreign_keys=[unidad_padre_id], backref="hijas")
    raiz = db.relationship("ScmUnidadFisicaKg", remote_side=[id], foreign_keys=[unidad_raiz_id])

    def to_dict(self):
        return {
            "id": str(self.id), "public_id": str(self.public_id), "codigo": self.codigo,
            "articulo": {"id": self.articulo.id, "codigo": self.articulo.codigo, "nombre": self.articulo.nombre, "clase": self.articulo.clase} if self.articulo else None,
            "root_id": str(self.unidad_raiz_id or self.id), "parent_id": str(self.unidad_padre_id) if self.unidad_padre_id else None,
            "division_id": str(self.division_id) if self.division_id else None, "intencion": self.intencion,
            "estado": self.estado, "estado_logistico": self.estado_logistico, "estado_calidad": self.estado_calidad,
            "atributo_proceso": self.atributo_proceso,
            "kg_entregado": _decimal(self.kg_entregado), "kg_verificados": _decimal(self.kg_verificados),
            "kg_verificados_at": _iso(self.kg_verificados_at),
            "medicion_vigente_id": str(self.medicion_vigente_id) if self.medicion_vigente_id else None,
            "modo_lectura": self.modo_lectura,
            "tara_contexto": self.tara_contexto_json,
            "saldo_id": str(self.saldo_id) if self.saldo_id else None,
            "ubicacion_id": self.ubicacion_id, "ubicacion": self.ubicacion.to_dict() if self.ubicacion else None,
            "almacen_responsable_id": str(self.almacen_responsable_id) if self.almacen_responsable_id else None,
            "tenedor_fisico_id": self.tenedor_fisico_id, "version": self.version,
        }


class ScmReservaUnidadKg(db.Model):
    __tablename__ = "scm_reserva_unidad_kg"
    __table_args__ = (db.CheckConstraint("estado IN ('ACTIVA', 'RETIRADA', 'LIBERADA')", name="ck_scm_reserva_unidad_kg_estado"),)
    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    unidad_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=False)
    cantidad_snapshot_kg = db.Column(db.Numeric(15, 3), nullable=False)
    documento_destino_tipo = db.Column(db.String(32), nullable=True)
    documento_destino_id = db.Column(db.String(120), nullable=True)
    motivo_operativo = db.Column(db.String(500), nullable=True)
    estado = db.Column(db.String(16), nullable=False, default="ACTIVA", server_default="ACTIVA")
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)
    unidad = db.relationship("ScmUnidadFisicaKg", foreign_keys=[unidad_id])


class ScmRetiroArmadoKg(db.Model):
    __tablename__ = "scm_retiro_armado_kg"
    __table_args__ = (db.CheckConstraint("estado IN ('ABIERTO', 'RETORNADO_TOTAL', 'CONCILIACION_PENDIENTE')", name="ck_scm_retiro_armado_kg_estado"), db.UniqueConstraint("operation_id", name="uq_scm_retiro_armado_kg_operation"))
    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(40), nullable=False, unique=True)
    documento_destino_tipo = db.Column(db.String(32), nullable=True)
    documento_destino_id = db.Column(db.String(120), nullable=True)
    almacen_responsable_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_almacen.id", ondelete="RESTRICT"), nullable=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    tenedor_fisico_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True)
    motivo_operativo = db.Column(db.String(500), nullable=True)
    estado = db.Column(db.String(32), nullable=False, default="ABIERTO", server_default="ABIERTO")
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())
    items = db.relationship("ScmRetiroArmadoKgItem", back_populates="retiro", cascade="all, delete-orphan", lazy="selectin")


class ScmRetiroArmadoKgItem(db.Model):
    __tablename__ = "scm_retiro_armado_kg_item"
    __table_args__ = (db.UniqueConstraint("retiro_id", "unidad_id", name="uq_scm_retiro_armado_kg_item_unit"),)
    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    retiro_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_retiro_armado_kg.id", ondelete="CASCADE"), nullable=False)
    unidad_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=False)
    reserva_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_reserva_unidad_kg.id", ondelete="RESTRICT"), nullable=False)
    neto_entregado_kg = db.Column(db.Numeric(15, 3), nullable=False)
    movimiento_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_movimiento_inventario_kg.id", ondelete="RESTRICT"), nullable=True)
    retiro = db.relationship("ScmRetiroArmadoKg", back_populates="items")
    unidad = db.relationship("ScmUnidadFisicaKg")


class ScmMedicionUnidadKg(db.Model):
    __tablename__ = "scm_medicion_unidad_kg"
    __table_args__ = (db.CheckConstraint("intencion IN ('RETORNO', 'DIVISION')", name="ck_scm_medicion_unidad_kg_intencion"), db.CheckConstraint("neto_kg > 0", name="ck_scm_medicion_unidad_kg_neto"), db.UniqueConstraint("station_id", "reading_id", name="uq_scm_medicion_unidad_kg_station_reading"), db.UniqueConstraint("operation_id", name="uq_scm_medicion_unidad_kg_operation"))
    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    unidad_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=False)
    retiro_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_retiro_armado_kg.id", ondelete="RESTRICT"), nullable=True)
    intencion = db.Column(db.String(16), nullable=False)
    station_id = db.Column(db.String(80), nullable=False)
    reading_id = db.Column(db.String(120), nullable=False)
    captured_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    reading_stable = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    modo_lectura = db.Column(db.String(32), nullable=False)
    bruto_kg = db.Column(db.Numeric(15, 3), nullable=True)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=True)
    neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    station_version = db.Column(db.String(80), nullable=True)
    payload_hash = db.Column(db.String(64), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    unidad = db.relationship("ScmUnidadFisicaKg", foreign_keys=[unidad_id])
    retiro = db.relationship("ScmRetiroArmadoKg", foreign_keys=[retiro_id])


class ScmDivisionUnidadKg(db.Model):
    __tablename__ = "scm_division_unidad_kg"
    __table_args__ = (db.CheckConstraint("estado IN ('CONFIRMADA', 'REVERSADA')", name="ck_scm_division_unidad_kg_estado"), db.UniqueConstraint("padre_id", name="uq_scm_division_unidad_kg_parent"), db.UniqueConstraint("operation_id", name="uq_scm_division_unidad_kg_operation"))
    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    padre_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=False)
    estado = db.Column(db.String(16), nullable=False, default="CONFIRMADA", server_default="CONFIRMADA")
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())


class ScmEtiquetaUnidadKg(db.Model):
    __tablename__ = "scm_etiqueta_unidad_kg"
    __table_args__ = (
        db.CheckConstraint("estado IN ('GENERADA', 'IMPRESA', 'EMISION_INCIERTA', 'INVALIDADA')", name="ck_scm_etiqueta_unidad_kg_estado"),
        db.UniqueConstraint("unidad_id", "version", name="uq_scm_etiqueta_unidad_kg_version"),
        db.UniqueConstraint("public_id", name="uq_scm_etiqueta_unidad_kg_public_id"),
    )
    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Public identity is the value carried by the compact QR.  Keep it
    # separate from the database key so a label can be resolved without
    # exposing storage identifiers.
    public_id = db.Column(Uuid(as_uuid=True), nullable=False, default=uuid.uuid4)
    unidad_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    estado = db.Column(db.String(24), nullable=False, default="GENERADA", server_default="GENERADA")
    payload_json = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    payload_hash = db.Column(db.String(64), nullable=False)
    print_job_id = db.Column(db.String(120), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
