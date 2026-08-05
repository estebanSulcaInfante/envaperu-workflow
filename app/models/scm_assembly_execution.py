"""Cierre trazable de mangas producidas por una OT de Ensamble."""

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
    return format(Decimal(value), "f") if value is not None else None


class ScmConfirmacionMangaArmado(db.Model):
    __tablename__ = "scm_confirmacion_manga_armado"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_planificada > 0 AND cantidad_real > 0",
            name="ck_scm_confirmacion_armado_cantidad",
        ),
        db.CheckConstraint(
            "length(estructura_hash) = 64 AND length(payload_hash) = 64",
            name="ck_scm_confirmacion_armado_hashes",
        ),
        db.UniqueConstraint("manga_id", name="uq_scm_confirmacion_armado_manga"),
        db.UniqueConstraint(
            "operation_id", name="uq_scm_confirmacion_armado_operation"
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manga_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
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
    articulo_salida_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"),
        nullable=False,
    )
    estructura_revision_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_estructura_revision.id", ondelete="RESTRICT"),
        nullable=False,
    )
    estructura_hash = db.Column(db.String(64), nullable=False)
    cantidad_planificada = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_real = db.Column(db.Numeric(15, 3), nullable=False)
    diferencia_cantidad = db.Column(db.Numeric(15, 3), nullable=False)
    motivo_diferencia = db.Column(db.String(500), nullable=True)
    confirmado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    confirmado_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)

    manga = db.relationship("ScmManga", back_populates="confirmacion_armado")
    orden_ensamble = db.relationship("ScmOrdenOperacion")
    orden_trabajo = db.relationship("RegistroDiarioProduccion")
    articulo_salida = db.relationship("ScmArticulo")
    estructura_revision = db.relationship("ScmEstructuraRevision")
    confirmado_por = db.relationship("Trabajador")
    consumos = db.relationship(
        "ScmConsumoComponenteArmado", back_populates="confirmacion",
        cascade="all, delete-orphan", lazy="selectin",
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "manga_id": str(self.manga.public_id),
            "manga_codigo": self.manga.codigo,
            "orden_ensamble_id": str(self.orden_ensamble_id),
            "orden_trabajo_id": str(self.orden_trabajo.public_id),
            "articulo_salida": self.articulo_salida.to_dict(),
            "cantidad_planificada": _decimal(self.cantidad_planificada),
            "cantidad_real": _decimal(self.cantidad_real),
            "diferencia_cantidad": _decimal(self.diferencia_cantidad),
            "motivo_diferencia": self.motivo_diferencia,
            "estructura_revision_id": self.estructura_revision_id,
            "estructura_hash": self.estructura_hash,
            "confirmado_por_id": self.confirmado_por_id,
            "confirmado_at": _iso(self.confirmado_at),
            "consumos": [item.to_dict() for item in self.consumos],
        }


class ScmConsumoComponenteArmado(db.Model):
    __tablename__ = "scm_consumo_componente_armado"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_incorporada > 0 AND cantidad_merma >= 0",
            name="ck_scm_consumo_armado_cantidad",
        ),
        db.CheckConstraint(
            "nivel_genealogia IN ('EXACTA', 'CONJUNTO_CANDIDATOS', "
            "'LEGACY_SIN_ORIGEN')",
            name="ck_scm_consumo_armado_genealogia",
        ),
        db.UniqueConstraint(
            "confirmacion_id", "asignacion_abastecimiento_id",
            name="uq_scm_consumo_armado_asignacion",
        ),
        db.UniqueConstraint(
            "confirmacion_id", "asignacion_pool_id",
            name="uq_scm_consumo_armado_pool",
        ),
        db.CheckConstraint(
            "(asignacion_abastecimiento_id IS NOT NULL) <> "
            "(asignacion_pool_id IS NOT NULL)",
            name="ck_scm_consumo_armado_fuente_unica",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    confirmacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_confirmacion_manga_armado.id", ondelete="CASCADE"),
        nullable=False,
    )
    asignacion_pool_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_asignacion_pool_armado.id", ondelete="RESTRICT"),
        nullable=True,
    )
    asignacion_abastecimiento_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_asignacion_abastecimiento.id", ondelete="RESTRICT"),
        nullable=True,
    )
    articulo_componente_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_incorporada = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_merma = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    nivel_genealogia = db.Column(
        db.String(28), nullable=False, default="EXACTA", server_default="EXACTA"
    )

    confirmacion = db.relationship(
        "ScmConfirmacionMangaArmado", back_populates="consumos"
    )
    asignacion_abastecimiento = db.relationship("ScmAsignacionAbastecimiento")
    asignacion_pool = db.relationship("ScmAsignacionPoolArmado")
    articulo_componente = db.relationship("ScmArticulo")

    def to_dict(self):
        assignment = self.asignacion_abastecimiento
        payload = {
            "id": str(self.id),
            "articulo": self.articulo_componente.to_dict(),
            "cantidad_incorporada": _decimal(self.cantidad_incorporada),
            "cantidad_merma": _decimal(self.cantidad_merma),
            "nivel_genealogia": self.nivel_genealogia,
        }
        if assignment is not None:
            payload.update({
                "asignacion_abastecimiento_id": str(assignment.id),
                "manga_origen_id": str(assignment.existencia.manga.public_id),
                "manga_origen_codigo": assignment.existencia.manga.codigo,
            })
        else:
            pool_assignment = self.asignacion_pool
            payload.update({
                "asignacion_pool_id": str(pool_assignment.id),
                "pool_origen_id": str(pool_assignment.pool_id),
                "candidatos": [
                    {"manga_id": str(value.manga.public_id), "codigo": value.manga.codigo}
                    for value in pool_assignment.pool.candidatos
                ],
            })
        return payload


class ScmCorreccionMangaArmado(db.Model):
    """Evento compensatorio; la confirmacion original permanece inmutable."""

    __tablename__ = "scm_correccion_manga_armado"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('PENDIENTE', 'RECHAZADA', 'APLICADA')",
            name="ck_scm_correccion_armado_estado",
        ),
        db.CheckConstraint(
            "cantidad_propuesta > 0", name="ck_scm_correccion_armado_cantidad"
        ),
        db.UniqueConstraint("request_operation_id", name="uq_scm_correccion_armado_request"),
        db.UniqueConstraint("approval_operation_id", name="uq_scm_correccion_armado_approval"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    confirmacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_confirmacion_manga_armado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    estado = db.Column(db.String(16), nullable=False, default="PENDIENTE", server_default="PENDIENTE")
    cantidad_anterior = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_propuesta = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(500), nullable=False)
    solicitada_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    solicitada_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    request_operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    resuelta_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True)
    resuelta_at = db.Column(db.DateTime(timezone=True), nullable=True)
    approval_operation_id = db.Column(Uuid(as_uuid=True), nullable=True)
    motivo_resolucion = db.Column(db.String(500), nullable=True)
    efectos_json = db.Column(db.JSON, nullable=True)

    confirmacion = db.relationship("ScmConfirmacionMangaArmado")
    solicitada_por = db.relationship("Trabajador", foreign_keys=[solicitada_por_id])
    resuelta_por = db.relationship("Trabajador", foreign_keys=[resuelta_por_id])

    def to_dict(self):
        return {
            "id": str(self.id), "confirmacion_id": str(self.confirmacion_id),
            "manga_id": str(self.confirmacion.manga.public_id), "estado": self.estado,
            "cantidad_anterior": _decimal(self.cantidad_anterior),
            "cantidad_propuesta": _decimal(self.cantidad_propuesta), "motivo": self.motivo,
            "solicitada_por_id": self.solicitada_por_id, "solicitada_at": _iso(self.solicitada_at),
            "resuelta_por_id": self.resuelta_por_id, "resuelta_at": _iso(self.resuelta_at),
            "motivo_resolucion": self.motivo_resolucion,
            "efectos": self.efectos_json,
        }
