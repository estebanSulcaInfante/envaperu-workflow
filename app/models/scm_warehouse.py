"""Recepción de mangas y existencia unitaria del Kardex SCM."""

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
    return format(value, "f") if value is not None else None


class ScmSesionRecepcionManga(db.Model):
    __tablename__ = "scm_sesion_recepcion_manga"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('ABIERTA', 'CERRADA')",
            name="ck_scm_sesion_recepcion_estado",
        ),
        db.UniqueConstraint(
            "codigo", name="uq_scm_sesion_recepcion_codigo"
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(48), nullable=False)
    punto_ingreso = db.Column(db.String(80), nullable=False)
    estado = db.Column(
        db.String(16), nullable=False, default="ABIERTA", server_default="ABIERTA"
    )
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    abierta_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    cerrada_at = db.Column(db.DateTime(timezone=True), nullable=True)

    actor = db.relationship("Trabajador")

    def to_dict(self):
        return {
            "id": str(self.id),
            "codigo": self.codigo,
            "punto_ingreso": self.punto_ingreso,
            "estado": self.estado,
            "actor_id": self.actor_id,
            "abierta_at": _iso(self.abierta_at),
            "cerrada_at": _iso(self.cerrada_at),
        }


class ScmExistenciaManga(db.Model):
    """Existencia física 1:1 creada al aceptar custodia en Almacén."""

    __tablename__ = "scm_existencia_manga"
    __table_args__ = (
        db.CheckConstraint(
            "estado_logistico IN ('RECIBIDA_ALMACEN', 'RESERVADA', "
            "'EN_PICKING', 'EN_TRANSITO_PRODUCCION', "
            "'EN_STAGING_ARMADO', 'ABIERTA_EN_CONSUMO', 'CONSUMIDA', "
            "'AGRUPADA_CANDIDATOS', "
            "'PENDIENTE_RETORNO', 'EN_TRANSITO_ALMACEN', 'REVERSADA')",
            name="ck_scm_existencia_manga_logistica",
        ),
        db.CheckConstraint(
            "estado_calidad IN ('PENDIENTE', 'LIBERADA', 'BLOQUEADA', 'RECHAZADA')",
            name="ck_scm_existencia_manga_calidad",
        ),
        db.CheckConstraint(
            "resuelta_por IN ('QR_FINAL', 'QR_PREETIQUETA', 'CODIGO_MANUAL')",
            name="ck_scm_existencia_manga_resolucion",
        ),
        db.CheckConstraint(
            "cantidad_fisica >= 0 AND cantidad_reservada >= 0 "
            "AND cantidad_reservada <= cantidad_fisica AND "
            "((estado_logistico = 'CONSUMIDA' AND cantidad_fisica = 0) OR "
            "(estado_logistico <> 'CONSUMIDA' AND cantidad_fisica > 0))",
            name="ck_scm_existencia_manga_cantidad",
        ),
        db.UniqueConstraint("manga_id", name="uq_scm_existencia_manga_manga"),
        db.UniqueConstraint(
            "movimiento_ingreso_id",
            name="uq_scm_existencia_manga_movimiento",
        ),
        db.UniqueConstraint(
            "operation_id", name="uq_scm_existencia_manga_operation"
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manga_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sesion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_sesion_recepcion_manga.id", ondelete="RESTRICT"),
        nullable=True,
    )
    etiqueta_resuelta_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_etiqueta_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
    articulo_scm_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"),
        nullable=False,
    )
    saldo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_saldo_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ubicacion_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    movimiento_ingreso_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_movimiento_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    resuelta_por = db.Column(db.String(24), nullable=False)
    estado_logistico = db.Column(
        db.String(32),
        nullable=False,
        default="RECIBIDA_ALMACEN",
        server_default="RECIBIDA_ALMACEN",
    )
    estado_calidad = db.Column(
        db.String(20),
        nullable=False,
        default="PENDIENTE",
        server_default="PENDIENTE",
    )
    cantidad_fisica = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_reservada = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    peso_neto_snapshot_kg = db.Column(db.Numeric(15, 3), nullable=False)
    recibida_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    recibida_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    calidad_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    calidad_at = db.Column(db.DateTime(timezone=True), nullable=True)
    calidad_motivo = db.Column(db.String(500), nullable=True)
    calidad_evidencia = db.Column(db.String(500), nullable=True)
    version = db.Column(
        db.Integer, nullable=False, default=1, server_default="1"
    )

    manga = db.relationship("ScmManga")
    sesion = db.relationship("ScmSesionRecepcionManga")
    etiqueta = db.relationship("ScmEtiquetaManga")
    articulo = db.relationship("ScmArticulo")
    saldo = db.relationship("ScmSaldoInventario")
    ubicacion = db.relationship("ScmUbicacionInventario")
    movimiento_ingreso = db.relationship("ScmMovimientoInventario")
    recibida_por = db.relationship("Trabajador", foreign_keys=[recibida_por_id])
    calidad_por = db.relationship("Trabajador", foreign_keys=[calidad_por_id])

    @property
    def cantidad_libre(self):
        if self.estado_calidad != "LIBERADA" or self.estado_logistico != "RECIBIDA_ALMACEN":
            return Decimal("0")
        return self.cantidad_fisica - self.cantidad_reservada

    def to_dict(self):
        return {
            "id": str(self.id),
            "manga_id": str(self.manga.public_id),
            "manga_codigo": self.manga.codigo,
            "sesion_id": str(self.sesion_id) if self.sesion_id else None,
            "etiqueta_id": str(self.etiqueta.public_id),
            "articulo": {
                "id": self.articulo.id,
                "codigo": self.articulo.codigo,
                "nombre": self.articulo.nombre,
                "clase": self.articulo.clase,
                "unidad": self.articulo.unidad_base,
            },
            "ubicacion": self.ubicacion.to_dict(),
            "resuelta_por": self.resuelta_por,
            "estado_logistico": self.estado_logistico,
            "estado_calidad": self.estado_calidad,
            "cantidad_fisica": _decimal(self.cantidad_fisica),
            "cantidad_reservada": _decimal(self.cantidad_reservada),
            "cantidad_libre": _decimal(self.cantidad_libre),
            "peso_neto_snapshot_kg": _decimal(self.peso_neto_snapshot_kg),
            "recibida_por_id": self.recibida_por_id,
            "recibida_at": _iso(self.recibida_at),
            "calidad_por_id": self.calidad_por_id,
            "calidad_at": _iso(self.calidad_at),
            "calidad_motivo": self.calidad_motivo,
            "calidad_evidencia": self.calidad_evidencia,
            "version": self.version,
        }


class ScmRechazoRecepcionManga(db.Model):
    __tablename__ = "scm_rechazo_recepcion_manga"
    __table_args__ = (
        db.UniqueConstraint(
            "operation_id", name="uq_scm_rechazo_recepcion_operation"
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manga_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
    etiqueta_resuelta_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_etiqueta_manga.id", ondelete="RESTRICT"),
        nullable=True,
    )
    motivo = db.Column(db.String(500), nullable=False)
    evidencia = db.Column(db.String(500), nullable=True)
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )

    manga = db.relationship("ScmManga")
    etiqueta = db.relationship("ScmEtiquetaManga")
    actor = db.relationship("Trabajador")

    def to_dict(self):
        return {
            "id": str(self.id),
            "manga_id": str(self.manga.public_id),
            "manga_codigo": self.manga.codigo,
            "etiqueta_id": str(self.etiqueta.public_id) if self.etiqueta else None,
            "motivo": self.motivo,
            "evidencia": self.evidencia,
            "actor_id": self.actor_id,
            "created_at": _iso(self.created_at),
        }


class ScmReversionRecepcionManga(db.Model):
    """Solicitud segregada; la entrada original permanece auditable."""

    __tablename__ = "scm_reversion_recepcion_manga"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('PENDIENTE', 'APROBADA', 'RECHAZADA')",
            name="ck_scm_reversion_recepcion_estado",
        ),
        db.UniqueConstraint("request_operation_id", name="uq_scm_reversion_recepcion_request"),
        db.UniqueConstraint("resolution_operation_id", name="uq_scm_reversion_recepcion_resolution"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    existencia_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_existencia_manga.id", ondelete="RESTRICT"), nullable=False)
    estado = db.Column(db.String(16), nullable=False, default="PENDIENTE", server_default="PENDIENTE")
    motivo = db.Column(db.String(500), nullable=False)
    evidencia = db.Column(db.String(500), nullable=True)
    solicitada_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    solicitada_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    resuelta_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True)
    resuelta_at = db.Column(db.DateTime(timezone=True), nullable=True)
    resolucion_motivo = db.Column(db.String(500), nullable=True)
    request_operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    resolution_operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=True)

    existencia = db.relationship("ScmExistenciaManga")

    def to_dict(self):
        return {
            "id": str(self.id), "existencia_id": str(self.existencia_id),
            "manga_codigo": self.existencia.manga.codigo,
            "estado": self.estado, "motivo": self.motivo, "evidencia": self.evidencia,
            "solicitada_por_id": self.solicitada_por_id, "solicitada_at": _iso(self.solicitada_at),
            "resuelta_por_id": self.resuelta_por_id, "resuelta_at": _iso(self.resuelta_at),
            "resolucion_motivo": self.resolucion_motivo,
        }
