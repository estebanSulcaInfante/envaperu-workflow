"""Almacenes configurables, alcance, sesiones QR y transferencias de inventario."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    return value.isoformat() if value else None


class ScmAlmacen(db.Model):
    __tablename__ = "scm_almacen"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('MATERIAS_PRIMAS', 'PIEZAS_WIP', "
            "'PRODUCTO_TERMINADO', 'GENERAL_CONTINGENCIA')",
            name="ck_scm_almacen_tipo",
        ),
        db.UniqueConstraint("codigo", name="uq_scm_almacen_codigo"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(40), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(32), nullable=False)
    configuracion_json = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    ubicaciones = db.relationship(
        "ScmUbicacionInventario", back_populates="almacen", lazy="selectin"
    )

    def to_dict(self, *, include_locations=False):
        payload = {
            "id": str(self.id), "codigo": self.codigo, "nombre": self.nombre,
            "tipo": self.tipo, "configuracion": dict(self.configuracion_json or {}),
            "activo": self.activo, "version": self.version,
            "created_at": _iso(self.created_at), "updated_at": _iso(self.updated_at),
        }
        if include_locations:
            payload["ubicaciones"] = [item.to_dict() for item in self.ubicaciones]
        return payload


class ScmAlmacenTrabajador(db.Model):
    __tablename__ = "scm_almacen_trabajador"
    __table_args__ = (
        db.UniqueConstraint("almacen_id", "trabajador_id", name="uq_scm_almacen_trabajador"),
        db.Index(
            "ix_scm_almacen_trabajador_scope",
            "trabajador_id",
            "activo",
            "almacen_id",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    almacen_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_almacen.id", ondelete="CASCADE"), nullable=False)
    trabajador_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    clases_articulo_json = db.Column(db.JSON, nullable=False, default=list, server_default="[]")
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    asignado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    almacen = db.relationship("ScmAlmacen")
    trabajador = db.relationship("Trabajador", foreign_keys=[trabajador_id])
    asignado_por = db.relationship("Trabajador", foreign_keys=[asignado_por_id])

    def to_dict(self):
        return {
            "id": str(self.id), "almacen_id": str(self.almacen_id),
            "trabajador_id": self.trabajador_id,
            "clases_articulo": list(self.clases_articulo_json or []),
            "activo": self.activo, "version": self.version,
        }


class ScmSesionOperacionAlmacen(db.Model):
    __tablename__ = "scm_sesion_operacion_almacen"
    __table_args__ = (
        db.CheckConstraint("tipo IN ('ENTRADA', 'SALIDA', 'TRANSFERENCIA', 'RETORNO')", name="ck_scm_sesion_operacion_tipo"),
        db.CheckConstraint("modalidad IN ('PICKUP', 'ENTREGA')", name="ck_scm_sesion_operacion_modalidad"),
        db.CheckConstraint("estado IN ('ABIERTA', 'LISTA', 'CONFIRMADA', 'CANCELADA', 'EXPIRADA')", name="ck_scm_sesion_operacion_estado"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tipo = db.Column(db.String(20), nullable=False)
    modalidad = db.Column(db.String(16), nullable=False)
    origen_ubicacion_id = db.Column(db.Integer, db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"), nullable=False)
    destino_ubicacion_id = db.Column(db.Integer, db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"), nullable=False)
    estado = db.Column(db.String(16), nullable=False, default="ABIERTA", server_default="ABIERTA")
    contexto_json = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    origen = db.relationship("ScmUbicacionInventario", foreign_keys=[origen_ubicacion_id])
    destino = db.relationship("ScmUbicacionInventario", foreign_keys=[destino_ubicacion_id])
    items = db.relationship("ScmSesionOperacionItem", back_populates="sesion", cascade="all, delete-orphan", lazy="selectin", order_by="ScmSesionOperacionItem.orden")


class ScmSesionOperacionItem(db.Model):
    __tablename__ = "scm_sesion_operacion_item"
    __table_args__ = (
        db.UniqueConstraint("sesion_id", "existencia_manga_id", name="uq_scm_sesion_item_existencia"),
        db.CheckConstraint("estado IN ('VALIDA', 'RECHAZADA')", name="ck_scm_sesion_item_estado"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sesion_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_sesion_operacion_almacen.id", ondelete="CASCADE"), nullable=False)
    existencia_manga_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_existencia_manga.id", ondelete="RESTRICT"), nullable=False)
    codigo_escaneado = db.Column(db.String(120), nullable=False)
    cantidad_snapshot = db.Column(db.Numeric(15, 3), nullable=False)
    estado = db.Column(db.String(16), nullable=False, default="VALIDA", server_default="VALIDA")
    motivo = db.Column(db.String(240), nullable=True)
    orden = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    sesion = db.relationship("ScmSesionOperacionAlmacen", back_populates="items")
    existencia = db.relationship("ScmExistenciaManga")


class ScmTransferenciaInventario(db.Model):
    __tablename__ = "scm_transferencia_inventario"
    __table_args__ = (
        db.UniqueConstraint("codigo", name="uq_scm_transferencia_codigo"),
        db.UniqueConstraint("operation_id", name="uq_scm_transferencia_operation"),
        db.CheckConstraint("modalidad IN ('PICKUP', 'ENTREGA')", name="ck_scm_transferencia_modalidad"),
        db.CheckConstraint("estado IN ('RECIBIDA', 'EN_TRANSITO', 'CERRADA', 'INCIDENCIA', 'RETORNADA')", name="ck_scm_transferencia_estado"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(40), nullable=False)
    sesion_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_sesion_operacion_almacen.id", ondelete="RESTRICT"), nullable=False)
    origen_ubicacion_id = db.Column(db.Integer, db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"), nullable=False)
    destino_ubicacion_id = db.Column(db.Integer, db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"), nullable=False)
    modalidad = db.Column(db.String(16), nullable=False)
    estado = db.Column(db.String(20), nullable=False)
    custodio_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    incidencia_json = db.Column(db.JSON, nullable=False, default=dict, server_default="{}")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    origen = db.relationship("ScmUbicacionInventario", foreign_keys=[origen_ubicacion_id])
    destino = db.relationship("ScmUbicacionInventario", foreign_keys=[destino_ubicacion_id])
    custodio = db.relationship("Trabajador", foreign_keys=[custodio_id])
    items = db.relationship("ScmTransferenciaItem", back_populates="transferencia", cascade="all, delete-orphan", lazy="selectin")


class ScmTransferenciaItem(db.Model):
    __tablename__ = "scm_transferencia_item"
    __table_args__ = (
        db.UniqueConstraint("transferencia_id", "existencia_manga_id", name="uq_scm_transferencia_item_existencia"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    transferencia_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_transferencia_inventario.id", ondelete="CASCADE"), nullable=False)
    existencia_manga_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_existencia_manga.id", ondelete="RESTRICT"), nullable=False)
    cantidad = db.Column(db.Numeric(15, 3), nullable=False)
    movimiento_salida_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_movimiento_inventario.id", ondelete="RESTRICT"), nullable=True)
    movimiento_transito_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_movimiento_inventario.id", ondelete="RESTRICT"), nullable=True)
    movimiento_entrada_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_movimiento_inventario.id", ondelete="RESTRICT"), nullable=True)

    transferencia = db.relationship("ScmTransferenciaInventario", back_populates="items")
    existencia = db.relationship("ScmExistenciaManga")
