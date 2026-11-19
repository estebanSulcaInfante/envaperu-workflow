"""Persistencia del levantamiento físico parcial de la estación Mesa.

Estas tablas conservan evidencia de hallazgo y pesaje sin sustituir el
Kardex. La identidad de una manga antigua se enlaza, cuando existe, con la
identidad global ``ScmUnidadFisicaKg``; las cuatro zonas son únicamente zonas
de hallazgo de esta campaña piloto.
"""

import uuid
from datetime import datetime, timezone

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


class ScmMesaInventarioCampana(db.Model):
    __tablename__ = "scm_mesa_inventario_campana"
    __table_args__ = (
        db.CheckConstraint("estado IN ('ABIERTA', 'CERRADA')", name="ck_mesa_inv_campana_estado"),
        db.UniqueConstraint("codigo", name="uq_mesa_inv_campana_codigo"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(64), nullable=False)
    estado = db.Column(db.String(16), nullable=False, default="ABIERTA", server_default="ABIERTA")
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    creado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    closed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    creado_por = db.relationship("Trabajador")
    sesiones = db.relationship("ScmMesaInventarioSesion", back_populates="campana", cascade="all, delete-orphan")


class ScmMesaInventarioSesion(db.Model):
    __tablename__ = "scm_mesa_inventario_sesion"
    __table_args__ = (
        db.CheckConstraint("estado IN ('ABIERTA', 'CERRADA')", name="ck_mesa_inv_sesion_estado"),
        db.UniqueConstraint("clave", name="uq_mesa_inv_sesion_clave"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campana_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_mesa_inventario_campana.id", ondelete="RESTRICT"), nullable=False)
    zona_id = db.Column(db.String(64), nullable=False)
    estado = db.Column(db.String(16), nullable=False, default="ABIERTA", server_default="ABIERTA")
    clave = db.Column(db.String(120), nullable=False)
    cobertura_confirmada = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    creado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    closed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    campana = db.relationship("ScmMesaInventarioCampana", back_populates="sesiones")
    creado_por = db.relationship("Trabajador")
    lecturas = db.relationship("ScmMesaInventarioLectura", back_populates="sesion", cascade="all, delete-orphan")


class ScmMesaInventarioManga(db.Model):
    __tablename__ = "scm_mesa_inventario_manga"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('HALLAZGO', 'PENDIENTE_PESAJE', 'PESADA_PENDIENTE_CONFIRMACION', 'DISPONIBLE', 'BAJA_COMPROBADA', 'BAJA_NO_RECUPERABLE')",
            name="ck_mesa_inv_manga_estado",
        ),
        db.UniqueConstraint("codigo", name="uq_mesa_inv_manga_codigo"),
        db.Index("ix_mesa_inv_manga_estado", "estado"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(120), nullable=False)
    unidad_fisica_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_unidad_fisica_kg.id", ondelete="RESTRICT"), nullable=True)
    kardex_existence_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_existencia_manga_kg.id", ondelete="RESTRICT"), nullable=True)
    articulo_id = db.Column(db.Integer, db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"), nullable=False)
    zona_hallazgo = db.Column(db.String(64), nullable=True)
    estado = db.Column(db.String(48), nullable=False, default="PENDIENTE_PESAJE", server_default="PENDIENTE_PESAJE")
    bruto_kg = db.Column(db.Numeric(15, 3), nullable=True)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=True)
    neto_kg = db.Column(db.Numeric(15, 3), nullable=True)
    baja_motivo = db.Column(db.String(240), nullable=True)
    baja_movimiento_id = db.Column(Uuid(as_uuid=True), nullable=True)
    baja_referencia = db.Column(db.String(120), nullable=True)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    unidad_fisica = db.relationship("ScmUnidadFisicaKg")
    articulo = db.relationship("ScmArticulo")


class ScmMesaInventarioLectura(db.Model):
    __tablename__ = "scm_mesa_inventario_lectura"
    __table_args__ = (
        db.UniqueConstraint("sesion_id", "manga_id", name="uq_mesa_inv_lectura_sesion_manga"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sesion_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_mesa_inventario_sesion.id", ondelete="CASCADE"), nullable=False)
    manga_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_mesa_inventario_manga.id", ondelete="RESTRICT"), nullable=False)
    zona_anterior = db.Column(db.String(64), nullable=True)
    estado_anterior = db.Column(db.String(48), nullable=True)
    neto_anterior_kg = db.Column(db.Numeric(15, 3), nullable=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    sesion = db.relationship("ScmMesaInventarioSesion", back_populates="lecturas")
    manga = db.relationship("ScmMesaInventarioManga")


class ScmMesaInventarioPesaje(db.Model):
    __tablename__ = "scm_mesa_inventario_pesaje"
    __table_args__ = (db.UniqueConstraint("clave", name="uq_mesa_inv_pesaje_clave"),)

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manga_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_mesa_inventario_manga.id", ondelete="RESTRICT"), nullable=False)
    bruto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=False)
    neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    lectura_id = db.Column(db.String(120), nullable=False)
    station_id = db.Column(db.String(80), nullable=False)
    captura_base = db.Column(db.String(2), nullable=True)
    captura_kg = db.Column(db.Numeric(15, 3), nullable=True)
    captura_at = db.Column(db.DateTime(timezone=True), nullable=True)
    clave = db.Column(db.String(120), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    manga = db.relationship("ScmMesaInventarioManga")


class ScmMesaInventarioRevision(db.Model):
    __tablename__ = "scm_mesa_inventario_revision"
    __table_args__ = (db.UniqueConstraint("codigo", "estado", name="uq_mesa_inv_revision_codigo_estado"),)

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(120), nullable=False)
    estado = db.Column(db.String(16), nullable=False, default="ACTIVA", server_default="ACTIVA")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)


class ScmMesaInventarioMovimiento(db.Model):
    __tablename__ = "scm_mesa_inventario_movimiento"
    __table_args__ = (db.UniqueConstraint("clave", name="uq_mesa_inv_movimiento_clave"),)

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manga_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_mesa_inventario_manga.id", ondelete="RESTRICT"), nullable=False)
    tipo = db.Column(db.String(32), nullable=False)
    delta_kg = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    clave = db.Column(db.String(120), nullable=False)
    vinculo_baja_id = db.Column(Uuid(as_uuid=True), nullable=True)
    kardex_movimiento_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_movimiento_inventario_kg.id", ondelete="RESTRICT"), nullable=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())


class ScmMesaInventarioSolicitud(db.Model):
    __tablename__ = "scm_mesa_inventario_solicitud"
    __table_args__ = (db.UniqueConstraint("clave", name="uq_mesa_inv_solicitud_clave"),)

    clave = db.Column(db.String(120), primary_key=True)
    operacion = db.Column(db.String(48), nullable=False)
    fingerprint = db.Column(db.String(64), nullable=False)
    respuesta_json = db.Column(db.JSON, nullable=False)
    status_code = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())


# Aliases keep the model discoverable to callers using either Spanish domain
# names or the shorter station terminology.
MesaInventoryCampaign = ScmMesaInventarioCampana
MesaInventorySession = ScmMesaInventarioSesion
MesaInventoryManga = ScmMesaInventarioManga
MesaInventoryReading = ScmMesaInventarioLectura
MesaInventoryWeighing = ScmMesaInventarioPesaje
MesaInventoryReview = ScmMesaInventarioRevision
MesaInventoryMovement = ScmMesaInventarioMovimiento
MesaInventoryRequest = ScmMesaInventarioSolicitud


__all__ = [
    "ScmMesaInventarioCampana", "ScmMesaInventarioSesion",
    "ScmMesaInventarioManga", "ScmMesaInventarioLectura",
    "ScmMesaInventarioPesaje", "ScmMesaInventarioRevision",
    "ScmMesaInventarioMovimiento", "ScmMesaInventarioSolicitud",
]
