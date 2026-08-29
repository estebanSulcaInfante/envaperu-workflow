"""Reserva y movimientos de una salida fabricada que se consume en Armado."""

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


class ScmSaldoWipSalida(db.Model):
    """Proyección por TrabajoColor y salida; los movimientos son la historia."""

    __tablename__ = "scm_saldo_wip_salida"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_acreditada >= 0 AND cantidad_consumida >= 0 "
            "AND cantidad_consumida <= cantidad_acreditada",
            name="ck_scm_saldo_wip_salida_cantidades",
        ),
        db.CheckConstraint(
            "version > 0", name="ck_scm_saldo_wip_salida_version"
        ),
        db.UniqueConstraint(
            "trabajo_color_id",
            "orden_operacion_salida_id",
            name="uq_scm_saldo_wip_salida_trabajo_salida",
        ),
        db.Index("ix_scm_saldo_wip_salida_trabajo", "trabajo_color_id"),
        db.Index(
            "ix_scm_saldo_wip_salida_orden_salida",
            "orden_operacion_salida_id",
        ),
        db.Index("ix_scm_saldo_wip_salida_articulo", "articulo_id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trabajo_color_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_trabajo_ot.id", ondelete="RESTRICT"),
        nullable=False,
    )
    orden_operacion_salida_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_operacion_salida.id", ondelete="RESTRICT"),
        nullable=False,
    )
    articulo_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_acreditada = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    cantidad_consumida = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    version = db.Column(
        db.Integer, nullable=False, default=1, server_default="1"
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=db.func.now(),
    )

    trabajo_color = db.relationship(
        "ScmTrabajoOt", back_populates="saldos_wip_salida"
    )
    orden_operacion_salida = db.relationship("ScmOrdenOperacionSalida")
    articulo = db.relationship("ScmArticulo")
    reservas = db.relationship(
        "ScmReservaWipSalida", back_populates="saldo", lazy="selectin"
    )
    movimientos = db.relationship(
        "ScmMovimientoWipSalida", back_populates="saldo", lazy="selectin"
    )

    @property
    def cantidad_disponible(self):
        return Decimal(self.cantidad_acreditada) - Decimal(
            self.cantidad_consumida
        )

    def to_dict(self):
        return {
            "id": str(self.id),
            "trabajo_color_id": str(self.trabajo_color_id),
            "orden_operacion_salida_id": str(
                self.orden_operacion_salida_id
            ),
            "articulo": self.articulo.to_dict(),
            "cantidad_acreditada": _decimal(self.cantidad_acreditada),
            "cantidad_consumida": _decimal(self.cantidad_consumida),
            "cantidad_disponible": _decimal(self.cantidad_disponible),
            "version": self.version,
            "updated_at": _iso(self.updated_at),
        }


class ScmReservaWipSalida(db.Model):
    """Autorización central, por manga, para crédito y consumo simultáneos."""

    __tablename__ = "scm_reserva_wip_salida"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('CREDITO_EN_LINEA_PENDIENTE', 'APLICADA', "
            "'LIBERADA', 'CANCELADA')",
            name="ck_scm_reserva_wip_salida_estado",
        ),
        db.CheckConstraint(
            "cantidad_reservada > 0 AND cantidad_aplicada >= 0 "
            "AND cantidad_aplicada <= cantidad_reservada",
            name="ck_scm_reserva_wip_salida_cantidades",
        ),
        db.UniqueConstraint(
            "manga_id",
            "articulo_componente_id",
            name="uq_scm_reserva_wip_salida_manga_componente",
        ),
        db.UniqueConstraint(
            "id",
            "saldo_id",
            name="uq_scm_reserva_wip_salida_id_saldo",
        ),
        db.Index("ix_scm_reserva_wip_salida_saldo", "saldo_id"),
        db.Index("ix_scm_reserva_wip_salida_manga", "manga_id"),
        db.Index(
            "ix_scm_reserva_wip_salida_articulo",
            "articulo_componente_id",
        ),
        db.Index(
            "ix_scm_reserva_wip_salida_pendiente",
            "saldo_id",
            postgresql_where=db.text(
                "estado = 'CREDITO_EN_LINEA_PENDIENTE'"
            ),
            sqlite_where=db.text(
                "estado = 'CREDITO_EN_LINEA_PENDIENTE'"
            ),
        ),
        db.Index("ix_scm_reserva_wip_salida_creada_por", "creada_por_id"),
        db.Index("ix_scm_reserva_wip_salida_operacion", "operation_id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    saldo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_saldo_wip_salida.id", ondelete="RESTRICT"),
        nullable=False,
    )
    manga_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asignacion_plan_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_asignacion_plan_manga_ot.id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    articulo_componente_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_reservada = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_aplicada = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    estado = db.Column(
        db.String(36),
        nullable=False,
        default="CREDITO_EN_LINEA_PENDIENTE",
        server_default="CREDITO_EN_LINEA_PENDIENTE",
    )
    creada_por_id = db.Column(
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
    aplicada_at = db.Column(db.DateTime(timezone=True), nullable=True)

    saldo = db.relationship("ScmSaldoWipSalida", back_populates="reservas")
    manga = db.relationship("ScmManga")
    asignacion_plan = db.relationship("ScmAsignacionPlanMangaOt")
    articulo_componente = db.relationship("ScmArticulo")
    creada_por = db.relationship("Trabajador")
    consumos = db.relationship(
        "ScmConsumoComponenteArmado",
        back_populates="reserva_wip_salida",
        lazy="selectin",
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "saldo_id": str(self.saldo_id),
            "manga_id": str(self.manga.public_id),
            "manga_codigo": self.manga.codigo,
            "asignacion_plan_id": self.asignacion_plan_id,
            "articulo": self.articulo_componente.to_dict(),
            "cantidad_reservada": _decimal(self.cantidad_reservada),
            "cantidad_aplicada": _decimal(self.cantidad_aplicada),
            "estado": self.estado,
            "trabajo_color_id": str(self.saldo.trabajo_color_id),
            "orden_operacion_salida_id": str(
                self.saldo.orden_operacion_salida_id
            ),
            "created_at": _iso(self.created_at),
            "aplicada_at": _iso(self.aplicada_at),
        }


class ScmMovimientoWipSalida(db.Model):
    """Ledger append-only del crédito y consumo de la salida fresca."""

    __tablename__ = "scm_movimiento_wip_salida"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('SALIDA_BUENA_CONFIRMADA', "
            "'CONSUMO_EN_LINEA_ARMADO', 'REVERSO_SALIDA_BUENA', "
            "'REVERSO_CONSUMO_EN_LINEA_ARMADO')",
            name="ck_scm_movimiento_wip_salida_tipo",
        ),
        db.CheckConstraint(
            "cantidad > 0", name="ck_scm_movimiento_wip_salida_cantidad"
        ),
        db.UniqueConstraint(
            "effect_key", name="uq_scm_movimiento_wip_salida_effect_key"
        ),
        db.ForeignKeyConstraint(
            ["reserva_id", "saldo_id"],
            [
                "scm_reserva_wip_salida.id",
                "scm_reserva_wip_salida.saldo_id",
            ],
            name="fk_scm_movimiento_wip_reserva_saldo",
            ondelete="RESTRICT",
        ),
        db.Index("ix_scm_movimiento_wip_salida_saldo", "saldo_id"),
        db.Index("ix_scm_movimiento_wip_salida_reserva", "reserva_id"),
        db.Index(
            "ix_scm_movimiento_wip_salida_confirmacion", "confirmacion_id"
        ),
        db.Index("ix_scm_movimiento_wip_salida_actor", "actor_id"),
        db.Index("ix_scm_movimiento_wip_salida_operacion", "operation_id"),
        db.Index(
            "ix_scm_movimiento_wip_salida_created_id", "created_at", "id"
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    saldo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_saldo_wip_salida.id", ondelete="RESTRICT"),
        nullable=False,
    )
    reserva_id = db.Column(Uuid(as_uuid=True), nullable=False)
    confirmacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_confirmacion_manga_armado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tipo = db.Column(db.String(40), nullable=False)
    cantidad = db.Column(db.Numeric(15, 3), nullable=False)
    effect_key = db.Column(db.String(160), nullable=False)
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

    saldo = db.relationship("ScmSaldoWipSalida", back_populates="movimientos")
    reserva = db.relationship(
        "ScmReservaWipSalida", overlaps="movimientos,saldo"
    )
    confirmacion = db.relationship("ScmConfirmacionMangaArmado")
    actor = db.relationship("Trabajador")

    def to_dict(self):
        return {
            "id": str(self.id),
            "saldo_id": str(self.saldo_id),
            "reserva_id": str(self.reserva_id),
            "confirmacion_id": str(self.confirmacion_id),
            "tipo": self.tipo,
            "cantidad": _decimal(self.cantidad),
            "effect_key": self.effect_key,
            "actor_id": self.actor_id,
            "operation_id": str(self.operation_id),
            "created_at": _iso(self.created_at),
        }
