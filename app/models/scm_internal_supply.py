"""Abastecimiento interno exacto desde Kardex hacia una OT de Armado."""

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


class ScmSolicitudAbastecimiento(db.Model):
    __tablename__ = "scm_solicitud_abastecimiento"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('SOLICITADA', 'EN_PREPARACION', 'LISTA', "
            "'DESPACHADA', 'RECIBIDA', 'CERRADA', 'CANCELADA', "
            "'INCIDENCIA')",
            name="ck_scm_solicitud_abastecimiento_estado",
        ),
        db.CheckConstraint(
            "version > 0", name="ck_scm_solicitud_abastecimiento_version"
        ),
        db.UniqueConstraint("codigo", name="uq_scm_solicitud_abastecimiento_codigo"),
        db.UniqueConstraint(
            "orden_trabajo_id", name="uq_scm_solicitud_abastecimiento_ot"
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(32), nullable=False)
    orden_ensamble_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_operacion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    orden_trabajo_id = db.Column(
        db.Integer,
        db.ForeignKey("registro_diario_produccion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    estado = db.Column(
        db.String(24), nullable=False, default="SOLICITADA", server_default="SOLICITADA"
    )
    solicitado_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    solicitado_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    preparada_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True
    )
    preparada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    despachada_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True
    )
    despachada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    recibida_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True
    )
    recibida_at = db.Column(db.DateTime(timezone=True), nullable=True)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )

    orden_ensamble = db.relationship("ScmOrdenOperacion")
    orden_trabajo = db.relationship("RegistroDiarioProduccion")
    solicitado_por = db.relationship("Trabajador", foreign_keys=[solicitado_por_id])
    preparada_por = db.relationship("Trabajador", foreign_keys=[preparada_por_id])
    despachada_por = db.relationship("Trabajador", foreign_keys=[despachada_por_id])
    recibida_por = db.relationship("Trabajador", foreign_keys=[recibida_por_id])
    lineas = db.relationship(
        "ScmSolicitudAbastecimientoLinea", back_populates="solicitud",
        cascade="all, delete-orphan", lazy="selectin",
    )


class ScmSolicitudAbastecimientoLinea(db.Model):
    __tablename__ = "scm_solicitud_abastecimiento_linea"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_requerida > 0", name="ck_scm_abastecimiento_linea_cantidad"
        ),
        db.UniqueConstraint(
            "solicitud_id", "articulo_scm_id", name="uq_scm_abastecimiento_linea_articulo"
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    solicitud_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_solicitud_abastecimiento.id", ondelete="CASCADE"),
        nullable=False,
    )
    articulo_scm_id = db.Column(
        db.Integer, db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"), nullable=False
    )
    cantidad_requerida = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_por_salida = db.Column(db.Numeric(15, 6), nullable=False)
    merma_tecnica_pct = db.Column(
        db.Numeric(8, 4), nullable=False, default=0, server_default="0"
    )

    solicitud = db.relationship("ScmSolicitudAbastecimiento", back_populates="lineas")
    articulo = db.relationship("ScmArticulo")
    asignaciones = db.relationship(
        "ScmAsignacionAbastecimiento", back_populates="linea",
        cascade="all, delete-orphan", lazy="selectin",
    )
    asignaciones_pool = db.relationship(
        "ScmAsignacionPoolArmado", back_populates="linea",
        cascade="all, delete-orphan", lazy="selectin",
    )

    @property
    def cantidad_asignada(self):
        exacta = sum(
            (item.cantidad_asignada for item in self.asignaciones
             if item.estado not in ("CANCELADA", "RETORNADA", "CONSUMIDA")),
            0,
        )
        no_exacta = sum(
            (item.cantidad_asignada for item in self.asignaciones_pool
             if item.estado not in ("CANCELADA", "RETORNADA", "CONSUMIDA")),
            0,
        )
        return exacta + no_exacta


class ScmAsignacionAbastecimiento(db.Model):
    __tablename__ = "scm_asignacion_abastecimiento"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('RESERVADA', 'EN_PICKING', "
            "'EN_TRANSITO_PRODUCCION', 'EN_STAGING_ARMADO', "
            "'ABIERTA_EN_CONSUMO', 'PENDIENTE_RETORNO', "
            "'EN_TRANSITO_ALMACEN', 'RETORNADA', 'CONSUMIDA', 'CANCELADA')",
            name="ck_scm_asignacion_abastecimiento_estado",
        ),
        db.CheckConstraint(
            "cantidad_asignada > 0 AND cantidad_consumida >= 0 "
            "AND cantidad_retornada >= 0 AND "
            "cantidad_consumida + cantidad_retornada <= cantidad_asignada",
            name="ck_scm_asignacion_abastecimiento_cantidades",
        ),
        db.UniqueConstraint(
            "linea_id", "existencia_manga_id", name="uq_scm_asignacion_abastecimiento_manga"
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    linea_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_solicitud_abastecimiento_linea.id", ondelete="CASCADE"),
        nullable=False,
    )
    existencia_manga_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_existencia_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_asignada = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_consumida = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    cantidad_retornada = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    estado = db.Column(
        db.String(32), nullable=False, default="RESERVADA", server_default="RESERVADA"
    )
    asignada_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    asignada_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )

    linea = db.relationship("ScmSolicitudAbastecimientoLinea", back_populates="asignaciones")
    existencia = db.relationship("ScmExistenciaManga")
    asignada_por = db.relationship("Trabajador")

    @property
    def saldo(self):
        return self.cantidad_asignada - self.cantidad_consumida - self.cantidad_retornada


scm_pool_origen_candidato = db.Table(
    "scm_pool_origen_candidato",
    db.Column(
        "pool_id", Uuid(as_uuid=True),
        db.ForeignKey("scm_pool_origen_armado.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "existencia_manga_id", Uuid(as_uuid=True),
        db.ForeignKey("scm_existencia_manga.id", ondelete="RESTRICT"),
        primary_key=True,
    ),
)


class ScmPoolOrigenArmado(db.Model):
    """Saldo identificado como conjunto; nunca reparte consumo entre candidatos."""

    __tablename__ = "scm_pool_origen_armado"
    __table_args__ = (
        db.CheckConstraint(
            "modo IN ('CONJUNTO_CANDIDATOS', 'LEGACY_SIN_ORIGEN')",
            name="ck_scm_pool_origen_modo",
        ),
        db.CheckConstraint(
            "cantidad_inicial > 0 AND cantidad_disponible >= 0 AND "
            "cantidad_disponible <= cantidad_inicial",
            name="ck_scm_pool_origen_cantidad",
        ),
        db.UniqueConstraint("operation_id", name="uq_scm_pool_origen_operation"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    articulo_scm_id = db.Column(
        db.Integer, db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"), nullable=False
    )
    saldo_id = db.Column(
        Uuid(as_uuid=True), db.ForeignKey("scm_saldo_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    modo = db.Column(db.String(28), nullable=False)
    cantidad_inicial = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_disponible = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(500), nullable=False)
    creado_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    creado_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)

    articulo = db.relationship("ScmArticulo")
    saldo = db.relationship("ScmSaldoInventario")
    creado_por = db.relationship("Trabajador")
    candidatos = db.relationship("ScmExistenciaManga", secondary=scm_pool_origen_candidato)


class ScmAsignacionPoolArmado(db.Model):
    __tablename__ = "scm_asignacion_pool_armado"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('RESERVADA', 'EN_PICKING', 'EN_TRANSITO_PRODUCCION', "
            "'EN_STAGING_ARMADO', 'ABIERTA_EN_CONSUMO', 'PENDIENTE_RETORNO', "
            "'EN_TRANSITO_ALMACEN', 'RETORNADA', 'CONSUMIDA', 'CANCELADA')",
            name="ck_scm_asignacion_pool_estado",
        ),
        db.CheckConstraint(
            "cantidad_asignada > 0 AND cantidad_consumida >= 0 AND "
            "cantidad_retornada >= 0 AND "
            "cantidad_consumida + cantidad_retornada <= cantidad_asignada",
            name="ck_scm_asignacion_pool_cantidad",
        ),
        db.UniqueConstraint("linea_id", "pool_id", name="uq_scm_asignacion_pool_linea"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    linea_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_solicitud_abastecimiento_linea.id", ondelete="CASCADE"),
        nullable=False,
    )
    pool_id = db.Column(
        Uuid(as_uuid=True), db.ForeignKey("scm_pool_origen_armado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    saldo_id = db.Column(
        Uuid(as_uuid=True), db.ForeignKey("scm_saldo_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_asignada = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_consumida = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    cantidad_retornada = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    estado = db.Column(db.String(32), nullable=False, default="RESERVADA", server_default="RESERVADA")
    asignada_por_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    asignada_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    linea = db.relationship("ScmSolicitudAbastecimientoLinea", back_populates="asignaciones_pool")
    pool = db.relationship("ScmPoolOrigenArmado")
    saldo = db.relationship("ScmSaldoInventario")
    asignada_por = db.relationship("Trabajador")

    @property
    def saldo_cantidad(self):
        return self.cantidad_asignada - self.cantidad_consumida - self.cantidad_retornada


def pool_assignment_payload(item):
    return {
        "id": str(item.id),
        "estado": item.estado,
        "modo": item.pool.modo,
        "cantidad_asignada": _decimal(item.cantidad_asignada),
        "cantidad_consumida": _decimal(item.cantidad_consumida),
        "cantidad_retornada": _decimal(item.cantidad_retornada),
        "saldo": _decimal(item.saldo_cantidad),
        "ubicacion": item.saldo.ubicacion.to_dict(),
        "motivo": item.pool.motivo,
        "candidatos": [
            {"existencia_id": str(value.id), "manga_id": str(value.manga.public_id),
             "codigo": value.manga.codigo}
            for value in item.pool.candidatos
        ],
    }


def assignment_payload(item):
    existence = item.existencia
    return {
        "id": str(item.id),
        "estado": item.estado,
        "cantidad_asignada": _decimal(item.cantidad_asignada),
        "cantidad_consumida": _decimal(item.cantidad_consumida),
        "cantidad_retornada": _decimal(item.cantidad_retornada),
        "saldo": _decimal(item.saldo),
        "manga": {
            "existencia_id": str(existence.id),
            "codigo": existence.manga.codigo,
            "manga_id": str(existence.manga.public_id),
            "estado_calidad": existence.estado_calidad,
            "estado_logistico": existence.estado_logistico,
            "ubicacion": existence.ubicacion.to_dict(),
        },
        "asignada_por_id": item.asignada_por_id,
        "asignada_at": _iso(item.asignada_at),
    }


def request_payload(item):
    return {
        "id": str(item.id),
        "codigo": item.codigo,
        "estado": item.estado,
        "version": item.version,
        "orden_armado": {
            "id": str(item.orden_ensamble.id),
            "codigo": item.orden_ensamble.codigo,
        },
        "orden_ensamble": {
            "id": str(item.orden_ensamble.id),
            "codigo": item.orden_ensamble.codigo,
        },
        "orden_trabajo": item.orden_trabajo.to_dict(),
        "lineas": [{
            "id": str(line.id),
            "articulo": {
                "id": line.articulo.id,
                "codigo": line.articulo.codigo,
                "nombre": line.articulo.nombre,
                "clase": line.articulo.clase,
                "unidad": line.articulo.unidad_base,
            },
            "cantidad_requerida": _decimal(line.cantidad_requerida),
            "cantidad_asignada": _decimal(line.cantidad_asignada),
            "cantidad_por_salida": _decimal(line.cantidad_por_salida),
            "merma_tecnica_pct": _decimal(line.merma_tecnica_pct),
            "asignaciones": [assignment_payload(a) for a in line.asignaciones],
            "asignaciones_no_exactas": [
                pool_assignment_payload(a) for a in line.asignaciones_pool
            ],
        } for line in item.lineas],
        "solicitado_por_id": item.solicitado_por_id,
        "solicitado_at": _iso(item.solicitado_at),
        "preparada_por_id": item.preparada_por_id,
        "preparada_at": _iso(item.preparada_at),
        "despachada_por_id": item.despachada_por_id,
        "despachada_at": _iso(item.despachada_at),
        "recibida_por_id": item.recibida_por_id,
        "recibida_at": _iso(item.recibida_at),
    }
