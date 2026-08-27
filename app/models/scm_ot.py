"""Modelo central de OT, planificación y mangas del corte US-010C."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _decimal_text(value):
    return format(value, "f") if value is not None else None


class ScmLoteArticulo(db.Model):
    """Identidad SCM fuerte para una salida física de PiezaColor."""

    __tablename__ = "scm_lote_articulo"
    __table_args__ = (
        db.CheckConstraint(
            "clase IN "
            "('LOTE_SALIDA_PIEZA_COLOR', 'SALIDA_ORDEN_OPERACION')",
            name="ck_scm_lote_articulo_clase_c_core",
        ),
        db.CheckConstraint(
            "(clase = 'LOTE_SALIDA_PIEZA_COLOR' "
            "AND lote_salida_pieza_color_id IS NOT NULL "
            "AND orden_operacion_salida_id IS NULL) OR "
            "(clase = 'SALIDA_ORDEN_OPERACION' "
            "AND orden_operacion_salida_id IS NOT NULL "
            "AND lote_salida_pieza_color_id IS NULL)",
            name="ck_scm_lote_articulo_origen_exclusivo",
        ),
        db.CheckConstraint(
            "cantidad_acreditada >= 0",
            name="ck_scm_lote_articulo_cantidad",
        ),
        db.UniqueConstraint(
            "public_id",
            name="uq_scm_lote_articulo_public_id",
        ),
        db.UniqueConstraint(
            "codigo",
            name="uq_scm_lote_articulo_codigo",
        ),
        db.UniqueConstraint(
            "lote_salida_pieza_color_id",
            name="uq_scm_lote_articulo_salida",
        ),
        db.UniqueConstraint(
            "orden_operacion_salida_id",
            name="uq_scm_lote_articulo_salida_canonica",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(
        Uuid(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    codigo = db.Column(db.String(64), nullable=False)
    articulo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_articulo.id",
            name="fk_scm_lote_articulo_articulo",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    clase = db.Column(
        db.String(40),
        nullable=False,
        default="LOTE_SALIDA_PIEZA_COLOR",
        server_default="LOTE_SALIDA_PIEZA_COLOR",
    )
    lote_salida_pieza_color_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "lote_salida_pieza_color.id",
            name="fk_scm_lote_articulo_salida",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    orden_operacion_salida_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_orden_operacion_salida.id",
            name="fk_scm_lote_articulo_salida_canonica",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    cantidad_acreditada = db.Column(
        db.Numeric(15, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    estado_calidad = db.Column(
        db.String(32),
        nullable=False,
        default="PLANIFICADO",
        server_default="PLANIFICADO",
    )
    event_time = db.Column(db.DateTime(timezone=True), nullable=True)
    record_time = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    actor_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_lote_articulo_actor",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    articulo = db.relationship("ScmArticulo")
    salida = db.relationship("LoteSalidaPiezaColor")
    salida_canonica = db.relationship("ScmOrdenOperacionSalida")
    actor = db.relationship("Trabajador")

    def to_dict(self):
        return {
            "id": self.id,
            "public_id": str(self.public_id) if self.public_id else None,
            "codigo": self.codigo,
            "articulo_id": self.articulo_id,
            "clase": self.clase,
            "lote_salida_pieza_color_id": self.lote_salida_pieza_color_id,
            "orden_operacion_salida_id": (
                str(self.orden_operacion_salida_id)
                if self.orden_operacion_salida_id else None
            ),
            "cantidad_acreditada": _decimal_text(
                self.cantidad_acreditada
            ),
            "estado_calidad": self.estado_calidad,
            "event_time": _isoformat(self.event_time),
            "record_time": _isoformat(self.record_time),
            "actor_id": self.actor_id,
        }


class ScmPlanMangaOp(db.Model):
    __tablename__ = "scm_plan_manga_op"
    __table_args__ = (
        db.CheckConstraint(
            "revision > 0",
            name="ck_scm_plan_manga_revision",
        ),
        db.CheckConstraint(
            "estado IN ('ACTIVO', 'SUPERADO')",
            name="ck_scm_plan_manga_estado",
        ),
        db.CheckConstraint(
            "length(content_hash) = 64",
            name="ck_scm_plan_manga_hash",
        ),
        db.UniqueConstraint(
            "orden_id",
            "revision",
            name="uq_scm_plan_manga_op_revision",
        ),
        db.UniqueConstraint(
            "orden_operacion_id",
            "revision",
            name="uq_scm_plan_manga_of_revision",
        ),
        db.CheckConstraint(
            "(orden_id IS NOT NULL AND orden_operacion_id IS NULL) OR "
            "(orden_id IS NULL AND orden_operacion_id IS NOT NULL)",
            name="ck_scm_plan_manga_origen_exclusivo",
        ),
        db.Index(
            "ux_scm_plan_manga_op_activo",
            "orden_id",
            unique=True,
            postgresql_where=db.text("estado = 'ACTIVO'"),
            sqlite_where=db.text("estado = 'ACTIVO'"),
        ),
        db.Index(
            "ux_scm_plan_manga_of_activo",
            "orden_operacion_id",
            unique=True,
            postgresql_where=db.text(
                "estado = 'ACTIVO' AND orden_operacion_id IS NOT NULL"
            ),
            sqlite_where=db.text(
                "estado = 'ACTIVO' AND orden_operacion_id IS NOT NULL"
            ),
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    orden_id = db.Column(
        db.String(20),
        db.ForeignKey(
            "orden_produccion.numero_op",
            name="fk_scm_plan_manga_op_orden",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    orden_operacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_orden_operacion.id",
            name="fk_scm_plan_manga_orden_operacion",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    revision = db.Column(db.Integer, nullable=False)
    estado = db.Column(
        db.String(20),
        nullable=False,
        default="ACTIVO",
        server_default="ACTIVO",
    )
    calculado_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_plan_manga_calculador",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_operacion.operation_id",
            name="fk_scm_plan_manga_operacion",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    content_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )

    orden = db.relationship("OrdenProduccion")
    orden_operacion = db.relationship("ScmOrdenOperacion")
    calculado_por = db.relationship("Trabajador")
    lineas = db.relationship(
        "ScmPlanMangaOpLinea",
        back_populates="plan",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ScmPlanMangaOpLinea.id",
    )


class ScmPlanMangaOpLinea(db.Model):
    __tablename__ = "scm_plan_manga_op_linea"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_objetivo_un > 0",
            name="ck_scm_plan_manga_linea_objetivo",
        ),
        db.CheckConstraint(
            "capacidad_efectiva_un > 0",
            name="ck_scm_plan_manga_linea_capacidad",
        ),
        db.CheckConstraint(
            "mangas_propuestas > 0",
            name="ck_scm_plan_manga_linea_mangas",
        ),
        db.CheckConstraint(
            "peso_unitario_snapshot_g > 0",
            name="ck_scm_plan_manga_linea_peso",
        ),
        db.UniqueConstraint(
            "plan_id",
            "lote_salida_pieza_color_id",
            name="uq_scm_plan_manga_linea_salida",
        ),
        db.UniqueConstraint(
            "plan_id",
            "orden_operacion_salida_id",
            name="uq_scm_plan_manga_linea_salida_canonica",
        ),
        db.CheckConstraint(
            "(lote_salida_pieza_color_id IS NOT NULL "
            "AND orden_operacion_salida_id IS NULL) OR "
            "(lote_salida_pieza_color_id IS NULL "
            "AND orden_operacion_salida_id IS NOT NULL)",
            name="ck_scm_plan_manga_linea_origen_exclusivo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    plan_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_plan_manga_op.id",
            name="fk_scm_plan_manga_linea_plan",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    lote_salida_pieza_color_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "lote_salida_pieza_color.id",
            name="fk_scm_plan_manga_linea_salida",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    orden_operacion_salida_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_orden_operacion_salida.id",
            name="fk_scm_plan_manga_linea_salida_canonica",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    lote_articulo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_lote_articulo.id",
            name="fk_scm_plan_manga_linea_lote_articulo",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    perfil_empacable_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_perfil_empacable.id",
            name="fk_scm_plan_manga_linea_perfil",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    regla_revision_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_regla_empaque_revision.id",
            name="fk_scm_plan_manga_linea_regla",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    tipo_contenedor_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_tipo_contenedor.id",
            name="fk_scm_plan_manga_linea_contenedor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    cantidad_objetivo_un = db.Column(db.Numeric(15, 3), nullable=False)
    capacidad_efectiva_un = db.Column(db.Integer, nullable=False)
    mangas_propuestas = db.Column(db.Integer, nullable=False)
    peso_unitario_snapshot_g = db.Column(db.Numeric(12, 4), nullable=False)
    articulo_codigo_snapshot = db.Column(db.String(64), nullable=False)
    articulo_nombre_snapshot = db.Column(db.String(200), nullable=False)
    pieza_color_sku_snapshot = db.Column(db.String(50), nullable=True)
    color_snapshot = db.Column(db.String(120), nullable=True)
    regla_hash_snapshot = db.Column(db.String(64), nullable=False)
    tara_nominal_g_snapshot = db.Column(db.Numeric(12, 3), nullable=False)
    tolerancia_tara_g_snapshot = db.Column(db.Numeric(12, 3), nullable=False)
    peso_bruto_max_kg_snapshot = db.Column(db.Numeric(12, 3), nullable=False)

    plan = db.relationship("ScmPlanMangaOp", back_populates="lineas")
    salida = db.relationship("LoteSalidaPiezaColor")
    salida_canonica = db.relationship("ScmOrdenOperacionSalida")
    lote_articulo = db.relationship("ScmLoteArticulo")
    perfil = db.relationship("ScmPerfilEmpacable")
    regla_revision = db.relationship("ScmReglaEmpaqueRevision")
    tipo_contenedor = db.relationship("ScmTipoContenedor")
    asignaciones = db.relationship(
        "ScmAsignacionPlanMangaOt",
        back_populates="plan_linea",
        lazy="selectin",
    )


class ScmTrabajoOt(db.Model):
    """Unidad ejecutable dentro de una OT de maquina/turno."""

    __tablename__ = "scm_trabajo_ot"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('COLOR')",
            name="ck_scm_trabajo_ot_tipo",
        ),
        db.CheckConstraint(
            "estado IN ('PLANIFICADO', 'EN_EJECUCION', 'PAUSADO', "
            "'COMPLETADO', 'ANULADO')",
            name="ck_scm_trabajo_ot_estado",
        ),
        db.CheckConstraint(
            "secuencia > 0",
            name="ck_scm_trabajo_ot_secuencia",
        ),
        db.CheckConstraint(
            "cantidad_objetivo_un >= 0 AND cantidad_confirmada_un >= 0",
            name="ck_scm_trabajo_ot_cantidades",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_trabajo_ot_version",
        ),
        db.UniqueConstraint(
            "orden_trabajo_id",
            "secuencia",
            name="uq_scm_trabajo_ot_orden_secuencia",
        ),
        db.UniqueConstraint(
            "id",
            "orden_trabajo_id",
            name="uq_scm_trabajo_ot_id_orden",
        ),
        db.UniqueConstraint("codigo", name="uq_scm_trabajo_ot_codigo"),
        db.UniqueConstraint(
            "continua_de_id",
            name="uq_scm_trabajo_ot_continuacion",
        ),
        db.Index(
            "ix_scm_trabajo_ot_orden_trabajo",
            "orden_trabajo_id",
        ),
        db.Index(
            "ix_scm_trabajo_ot_orden_operacion",
            "orden_operacion_id",
        ),
        db.Index("ix_scm_trabajo_ot_created_by", "created_by_id"),
        db.Index("ix_scm_trabajo_ot_anulada_por", "anulada_por_id"),
        db.Index(
            "uq_scm_trabajo_ot_activo",
            "orden_trabajo_id",
            unique=True,
            postgresql_where=db.text("estado = 'EN_EJECUCION'"),
            sqlite_where=db.text("estado = 'EN_EJECUCION'"),
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orden_trabajo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "registro_diario_produccion.id",
            name="fk_scm_trabajo_ot_orden_trabajo",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    codigo = db.Column(db.String(64), nullable=False)
    tipo = db.Column(
        db.String(20), nullable=False, default="COLOR", server_default="COLOR"
    )
    secuencia = db.Column(db.Integer, nullable=False)
    estado = db.Column(
        db.String(24),
        nullable=False,
        default="PLANIFICADO",
        server_default="PLANIFICADO",
    )
    orden_operacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_orden_operacion.id",
            name="fk_scm_trabajo_ot_orden_operacion",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    continua_de_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_trabajo_ot.id",
            name="fk_scm_trabajo_ot_continua_de",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    cantidad_objetivo_un = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    cantidad_confirmada_un = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    iniciada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    pausada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    completada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    anulada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    motivo_pausa = db.Column(db.String(500), nullable=True)
    motivo_anulacion = db.Column(db.String(500), nullable=True)
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    anulada_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
        server_default=db.func.now(),
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    orden_trabajo = db.relationship(
        "RegistroDiarioProduccion",
        back_populates="trabajos_ot",
        foreign_keys=[orden_trabajo_id],
    )
    orden_operacion = db.relationship("ScmOrdenOperacion")
    continua_de = db.relationship(
        "ScmTrabajoOt",
        remote_side=[id],
        foreign_keys=[continua_de_id],
        uselist=False,
    )
    trabajo_color = db.relationship(
        "ScmTrabajoColor",
        back_populates="trabajo",
        uselist=False,
        cascade="all, delete-orphan",
    )
    asignaciones_personal = db.relationship(
        "ScmAsignacionPersonalTrabajoOt",
        back_populates="trabajo",
        lazy="selectin",
        order_by="ScmAsignacionPersonalTrabajoOt.asignada_at",
    )
    mangas = db.relationship(
        "ScmManga",
        back_populates="trabajo",
        lazy="selectin",
        order_by="ScmManga.secuencia_ot",
    )
    tramos_manga = db.relationship(
        "ScmTramoMangaTrabajo",
        back_populates="trabajo",
        lazy="selectin",
        order_by="ScmTramoMangaTrabajo.secuencia",
    )
    created_by = db.relationship("Trabajador", foreign_keys=[created_by_id])
    anulada_por = db.relationship("Trabajador", foreign_keys=[anulada_por_id])


class ScmTrabajoColor(db.Model):
    """Especializacion de Fabricacion para una corrida/color homogeneos."""

    __tablename__ = "scm_trabajo_color"
    __table_args__ = (
        db.Index("ix_scm_trabajo_color_corrida", "corrida_fabricacion_id"),
        db.CheckConstraint(
            "cavidades_snapshot IS NULL OR cavidades_snapshot > 0",
            name="ck_scm_trabajo_color_cavidades",
        ),
        db.CheckConstraint(
            "peso_neto_snapshot_g IS NULL OR peso_neto_snapshot_g >= 0",
            name="ck_scm_trabajo_color_peso_neto",
        ),
        db.CheckConstraint(
            "peso_colada_snapshot_g IS NULL OR peso_colada_snapshot_g >= 0",
            name="ck_scm_trabajo_color_peso_colada",
        ),
    )

    trabajo_ot_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_trabajo_ot.id", ondelete="CASCADE"),
        primary_key=True,
    )
    corrida_fabricacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_corrida_fabricacion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    molde_codigo_snapshot = db.Column(db.String(50), nullable=True)
    color_id_snapshot = db.Column(db.Integer, nullable=True)
    color_nombre_snapshot = db.Column(db.String(120), nullable=True)
    receta_revision_id_snapshot = db.Column(db.Integer, nullable=True)
    receta_hash_snapshot = db.Column(db.String(64), nullable=True)
    cavidades_snapshot = db.Column(db.Integer, nullable=True)
    peso_neto_snapshot_g = db.Column(db.Numeric(15, 4), nullable=True)
    peso_colada_snapshot_g = db.Column(db.Numeric(15, 4), nullable=True)
    colada_inicial = db.Column(db.Integer, nullable=True)
    colada_final = db.Column(db.Integer, nullable=True)

    trabajo = db.relationship("ScmTrabajoOt", back_populates="trabajo_color")
    corrida = db.relationship("ScmCorridaFabricacion")


class ScmAsignacionPersonalTrabajoOt(db.Model):
    """Asignacion auditada por intervalo; un relevo no cambia de trabajo."""

    __tablename__ = "scm_asignacion_personal_trabajo_ot"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('PREVISTA', 'ACTIVA', 'CERRADA', 'CANCELADA')",
            name="ck_scm_asignacion_personal_estado",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_asignacion_personal_version",
        ),
        db.CheckConstraint(
            "(estado IN ('PREVISTA', 'ACTIVA') AND finalizada_at IS NULL) OR "
            "(estado IN ('CERRADA', 'CANCELADA') AND finalizada_at IS NOT NULL)",
            name="ck_scm_asignacion_personal_intervalo",
        ),
        db.Index(
            "uq_scm_asignacion_personal_activa",
            "trabajo_ot_id",
            unique=True,
            postgresql_where=db.text("estado = 'ACTIVA'"),
            sqlite_where=db.text("estado = 'ACTIVA'"),
        ),
        db.Index(
            "ix_scm_asignacion_personal_trabajador",
            "trabajador_id",
        ),
        db.Index(
            "ix_scm_asignacion_personal_trabajo",
            "trabajo_ot_id",
        ),
        db.Index(
            "ix_scm_asignacion_personal_asignada_por",
            "asignada_por_id",
        ),
        db.Index(
            "ix_scm_asignacion_personal_finalizada_por",
            "finalizada_por_id",
        ),
        db.UniqueConstraint(
            "id",
            "trabajo_ot_id",
            name="uq_scm_asignacion_personal_id_trabajo",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trabajo_ot_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_trabajo_ot.id", ondelete="RESTRICT"),
        nullable=False,
    )
    trabajador_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    estado = db.Column(
        db.String(20),
        nullable=False,
        default="PREVISTA",
        server_default="PREVISTA",
    )
    asignada_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    iniciada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    finalizada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    asignada_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    finalizada_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    motivo = db.Column(db.String(500), nullable=True)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    trabajo = db.relationship("ScmTrabajoOt", back_populates="asignaciones_personal")
    trabajador = db.relationship("Trabajador", foreign_keys=[trabajador_id])
    asignada_por = db.relationship("Trabajador", foreign_keys=[asignada_por_id])
    finalizada_por = db.relationship(
        "Trabajador", foreign_keys=[finalizada_por_id]
    )
    mangas = db.relationship(
        "ScmManga",
        back_populates="asignacion_personal_trabajo",
        foreign_keys="ScmManga.asignacion_personal_trabajo_id",
    )
    pesajes = db.relationship(
        "ScmPesajeManga",
        back_populates="asignacion_personal_trabajo",
        foreign_keys="ScmPesajeManga.asignacion_personal_trabajo_id",
    )
    tramos_manga = db.relationship(
        "ScmTramoMangaTrabajo",
        back_populates="asignacion_personal_trabajo",
        foreign_keys="ScmTramoMangaTrabajo.asignacion_personal_trabajo_id",
    )


class ScmAsignacionPlanMangaOt(db.Model):
    __tablename__ = "scm_asignacion_plan_manga_ot"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_asignada_un >= 0",
            name="ck_scm_asignacion_plan_cantidad",
        ),
        db.CheckConstraint(
            "mangas_asignadas >= 0",
            name="ck_scm_asignacion_plan_mangas",
        ),
        db.Index(
            "uq_scm_asignacion_plan_trabajo",
            "plan_linea_id",
            "trabajo_ot_id",
            unique=True,
            postgresql_where=db.text("trabajo_ot_id IS NOT NULL"),
            sqlite_where=db.text("trabajo_ot_id IS NOT NULL"),
        ),
        db.Index(
            "uq_scm_asignacion_plan_ot_legacy",
            "plan_linea_id",
            "ot_id",
            unique=True,
            postgresql_where=db.text("trabajo_ot_id IS NULL"),
            sqlite_where=db.text("trabajo_ot_id IS NULL"),
        ),
        db.Index(
            "ix_scm_asignacion_plan_trabajo",
            "trabajo_ot_id",
        ),
        db.ForeignKeyConstraint(
            ["trabajo_ot_id", "ot_id"],
            ["scm_trabajo_ot.id", "scm_trabajo_ot.orden_trabajo_id"],
            name="fk_scm_asignacion_plan_trabajo_ot",
            ondelete="RESTRICT",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    plan_linea_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_plan_manga_op_linea.id",
            name="fk_scm_asignacion_plan_linea",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    ot_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "registro_diario_produccion.id",
            name="fk_scm_asignacion_plan_ot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    trabajo_ot_id = db.Column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    cantidad_asignada_un = db.Column(db.Numeric(15, 3), nullable=False)
    mangas_asignadas = db.Column(db.Integer, nullable=False)
    asignada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_asignacion_plan_actor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    asignada_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )

    plan_linea = db.relationship(
        "ScmPlanMangaOpLinea",
        back_populates="asignaciones",
    )
    ot = db.relationship("RegistroDiarioProduccion", overlaps="trabajo")
    trabajo = db.relationship("ScmTrabajoOt", overlaps="ot")
    asignada_por = db.relationship("Trabajador")


class ScmManga(db.Model):
    __tablename__ = "scm_manga"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('NORMAL', 'EXTRA')",
            name="ck_scm_manga_tipo",
        ),
        db.CheckConstraint(
            "estado IN ('PLANIFICADA', 'PREETIQUETADA', 'EN_ARMADO', "
            "'CONTINUIDAD_PENDIENTE', 'EN_LLENADO', "
            "'CERRADA_ARMADO_PENDIENTE_PESAJE', 'PESADA', "
            "'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN', "
            "'RECIBIDA', 'ANULADA')",
            name="ck_scm_manga_estado",
        ),
        db.CheckConstraint(
            "cantidad_asignada_un > 0",
            name="ck_scm_manga_cantidad",
        ),
        db.CheckConstraint(
            "secuencia_ot > 0",
            name="ck_scm_manga_secuencia",
        ),
        db.UniqueConstraint("public_id", name="uq_scm_manga_public_id"),
        db.UniqueConstraint("codigo", name="uq_scm_manga_codigo"),
        db.UniqueConstraint(
            "ot_id",
            "secuencia_ot",
            name="uq_scm_manga_ot_secuencia",
        ),
        db.Index("ix_scm_manga_trabajo", "trabajo_ot_id"),
        db.Index(
            "ix_scm_manga_asignacion_personal",
            "asignacion_personal_trabajo_id",
        ),
        db.ForeignKeyConstraint(
            ["trabajo_ot_id", "ot_id"],
            ["scm_trabajo_ot.id", "scm_trabajo_ot.orden_trabajo_id"],
            name="fk_scm_manga_trabajo_ot",
            ondelete="RESTRICT",
        ),
        db.ForeignKeyConstraint(
            ["asignacion_personal_trabajo_id", "trabajo_ot_id"],
            [
                "scm_asignacion_personal_trabajo_ot.id",
                "scm_asignacion_personal_trabajo_ot.trabajo_ot_id",
            ],
            name="fk_scm_manga_asignacion_personal_trabajo",
            ondelete="RESTRICT",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(
        Uuid(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    codigo = db.Column(db.String(80), nullable=False)
    ot_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "registro_diario_produccion.id",
            name="fk_scm_manga_ot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    trabajo_ot_id = db.Column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    asignacion_personal_trabajo_id = db.Column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    plan_linea_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_plan_manga_op_linea.id",
            name="fk_scm_manga_plan_linea",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    asignacion_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_asignacion_plan_manga_ot.id",
            name="fk_scm_manga_asignacion",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    lote_articulo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_lote_articulo.id",
            name="fk_scm_manga_lote_articulo",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    secuencia_ot = db.Column(db.Integer, nullable=False)
    tipo = db.Column(
        db.String(16),
        nullable=False,
        default="NORMAL",
        server_default="NORMAL",
    )
    estado = db.Column(
        db.String(32),
        nullable=False,
        default="PLANIFICADA",
        server_default="PLANIFICADA",
    )
    cantidad_planificada_un = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_asignada_un = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_confirmada_un = db.Column(db.Numeric(15, 3), nullable=True)
    cantidad_contenida_un = db.Column(db.Numeric(15, 3), nullable=True)
    maquinista_previsto_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_manga_maquinista",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    articulo_codigo_snapshot = db.Column(db.String(64), nullable=False)
    articulo_nombre_snapshot = db.Column(db.String(200), nullable=False)
    # Una salida canónica también puede ser PT directo o WIP y no poseer SKU
    # PiezaColor. El artículo SCM conserva la identidad estable.
    pieza_color_sku_snapshot = db.Column(db.String(50), nullable=True)
    color_snapshot = db.Column(db.String(120), nullable=True)
    regla_revision_id_snapshot = db.Column(db.Integer, nullable=False)
    regla_hash_snapshot = db.Column(db.String(64), nullable=False)
    tipo_contenedor_codigo_snapshot = db.Column(db.String(64), nullable=False)
    tipo_contenedor_nombre_snapshot = db.Column(db.String(160), nullable=False)
    peso_unitario_snapshot_g = db.Column(db.Numeric(12, 4), nullable=False)
    tara_nominal_g_snapshot = db.Column(db.Numeric(12, 3), nullable=False)
    tolerancia_tara_g_snapshot = db.Column(db.Numeric(12, 3), nullable=False)
    peso_bruto_max_kg_snapshot = db.Column(db.Numeric(12, 3), nullable=False)
    motivo_extra = db.Column(db.String(250), nullable=True)
    extra_solicitada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_manga_extra_solicitante",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    extra_aprobada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_manga_extra_aprobador",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    extra_aprobada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_manga_creador",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    anulada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    anulada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_manga_anulador",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    motivo_anulacion = db.Column(db.String(500), nullable=True)
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    ot = db.relationship("RegistroDiarioProduccion", overlaps="mangas,trabajo")
    trabajo = db.relationship(
        "ScmTrabajoOt",
        back_populates="mangas",
        foreign_keys=[trabajo_ot_id],
    )
    asignacion_personal_trabajo = db.relationship(
        "ScmAsignacionPersonalTrabajoOt",
        back_populates="mangas",
        foreign_keys=[asignacion_personal_trabajo_id],
    )
    plan_linea = db.relationship("ScmPlanMangaOpLinea")
    asignacion = db.relationship("ScmAsignacionPlanMangaOt")
    lote_articulo = db.relationship("ScmLoteArticulo")
    maquinista_previsto = db.relationship(
        "Trabajador",
        foreign_keys=[maquinista_previsto_id],
    )
    created_by = db.relationship(
        "Trabajador",
        foreign_keys=[created_by_id],
    )
    etiquetas = db.relationship(
        "ScmEtiquetaManga",
        back_populates="manga",
        lazy="selectin",
        order_by="ScmEtiquetaManga.version",
    )
    confirmacion_armado = db.relationship(
        "ScmConfirmacionMangaArmado",
        back_populates="manga",
        uselist=False,
    )
    tramos_trabajo = db.relationship(
        "ScmTramoMangaTrabajo",
        back_populates="manga",
        lazy="selectin",
        order_by="ScmTramoMangaTrabajo.secuencia",
    )
    controles_peso = db.relationship(
        "ScmControlPesoManga",
        back_populates="manga",
        lazy="selectin",
        order_by="ScmControlPesoManga.pesado_at",
    )


class ScmTramoMangaTrabajo(db.Model):
    """Responsabilidad de una manga dentro de un Trabajo de color.

    La manga conserva su OT de origen. Los tramos son el ledger que permite
    continuar la misma identidad fisica y el mismo QR en turnos posteriores.
    """

    __tablename__ = "scm_tramo_manga_trabajo"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('PROGRAMADO', 'ACTIVO', 'CERRADO', 'ANULADO')",
            name="ck_scm_tramo_manga_estado",
        ),
        db.CheckConstraint(
            "secuencia > 0 AND cantidad_inicio_un >= 0",
            name="ck_scm_tramo_manga_inicio",
        ),
        db.CheckConstraint(
            "cantidad_fin_un IS NULL OR cantidad_fin_un > cantidad_inicio_un",
            name="ck_scm_tramo_manga_fin",
        ),
        db.CheckConstraint(
            "cantidad_atribuida_un >= 0",
            name="ck_scm_tramo_manga_atribuida",
        ),
        db.UniqueConstraint(
            "manga_id", "secuencia", name="uq_scm_tramo_manga_secuencia"
        ),
        db.Index("ix_scm_tramo_manga_trabajo", "trabajo_ot_id"),
        db.Index("ix_scm_tramo_manga_asignacion", "asignacion_personal_trabajo_id"),
        db.Index(
            "uq_scm_tramo_manga_abierto",
            "manga_id",
            unique=True,
            postgresql_where=db.text("estado IN ('PROGRAMADO', 'ACTIVO')"),
            sqlite_where=db.text("estado IN ('PROGRAMADO', 'ACTIVO')"),
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manga_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asignacion_plan_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_asignacion_plan_manga_ot.id", ondelete="RESTRICT"),
        nullable=True,
    )
    trabajo_ot_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_trabajo_ot.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asignacion_personal_trabajo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_asignacion_personal_trabajo_ot.id",
            name="fk_scm_tramo_manga_asignacion_personal",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    secuencia = db.Column(db.Integer, nullable=False)
    estado = db.Column(
        db.String(20), nullable=False, default="PROGRAMADO",
        server_default="PROGRAMADO",
    )
    cantidad_inicio_un = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_fin_un = db.Column(db.Numeric(15, 3), nullable=True)
    cantidad_atribuida_un = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    iniciada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    cerrada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    motivo_cierre = db.Column(db.String(500), nullable=True)
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    manga = db.relationship("ScmManga", back_populates="tramos_trabajo")
    trabajo = db.relationship("ScmTrabajoOt", back_populates="tramos_manga")
    asignacion_personal_trabajo = db.relationship(
        "ScmAsignacionPersonalTrabajoOt", back_populates="tramos_manga",
    )
    asignacion_plan = db.relationship("ScmAsignacionPlanMangaOt")
    created_by = db.relationship("Trabajador")
    control_peso = db.relationship(
        "ScmControlPesoManga", back_populates="tramo", uselist=False,
    )

    def to_dict(self):
        return {
            "id": str(self.id),
            "manga_id": str(self.manga.public_id) if self.manga else None,
            "secuencia": self.secuencia,
            "estado": self.estado,
            "trabajo_color_id": str(self.trabajo_ot_id),
            "trabajo_color_codigo": self.trabajo.codigo if self.trabajo else None,
            "ot_id": (
                str(self.trabajo.orden_trabajo.public_id)
                if self.trabajo and self.trabajo.orden_trabajo else None
            ),
            "ot_codigo": (
                self.trabajo.orden_trabajo.codigo_ot
                if self.trabajo and self.trabajo.orden_trabajo else None
            ),
            "asignacion_personal_trabajo_id": str(
                self.asignacion_personal_trabajo_id
            ),
            "asignacion_plan_id": self.asignacion_plan_id,
            "maquinista_id": (
                self.asignacion_personal_trabajo.trabajador_id
                if self.asignacion_personal_trabajo else None
            ),
            "maquinista": (
                self.asignacion_personal_trabajo.trabajador.nombre_completo
                if self.asignacion_personal_trabajo
                and self.asignacion_personal_trabajo.trabajador else None
            ),
            "cantidad_inicio_un": _decimal_text(self.cantidad_inicio_un),
            "cantidad_fin_un": _decimal_text(self.cantidad_fin_un),
            "cantidad_atribuida_un": _decimal_text(self.cantidad_atribuida_un),
            "iniciada_at": _isoformat(self.iniciada_at),
            "cerrada_at": _isoformat(self.cerrada_at),
            "motivo_cierre": self.motivo_cierre,
        }


class ScmControlPesoManga(db.Model):
    """Lectura acumulada de control; no es un pesaje final ni crea Kardex."""

    __tablename__ = "scm_control_peso_manga"
    __table_args__ = (
        db.CheckConstraint(
            "tipo = 'CORTE_TURNO'", name="ck_scm_control_peso_manga_tipo"
        ),
        db.CheckConstraint(
            "peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0",
            name="ck_scm_control_peso_manga_pesos",
        ),
        db.CheckConstraint(
            "peso_neto_kg = peso_bruto_kg - tara_kg",
            name="ck_scm_control_peso_manga_neto",
        ),
        db.CheckConstraint(
            "conteo_acumulado_un > 0",
            name="ck_scm_control_peso_manga_conteo",
        ),
        db.UniqueConstraint("public_id", name="uq_scm_control_peso_manga_public"),
        db.UniqueConstraint("tramo_id", name="uq_scm_control_peso_manga_tramo"),
        db.UniqueConstraint("operation_id", name="uq_scm_control_peso_manga_operation"),
        db.UniqueConstraint(
            "source_system", "capture_id", name="uq_scm_control_peso_manga_capture"
        ),
        db.Index("ix_scm_control_peso_manga_manga", "manga_id"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(Uuid(as_uuid=True), nullable=False, default=uuid.uuid4)
    manga_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tramo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_tramo_manga_trabajo.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_system = db.Column(db.String(40), nullable=False)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
    )
    capture_id = db.Column(Uuid(as_uuid=True), nullable=False)
    tipo = db.Column(
        db.String(24), nullable=False, default="CORTE_TURNO",
        server_default="CORTE_TURNO",
    )
    peso_bruto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=False)
    peso_neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_fuente = db.Column(db.String(28), nullable=False)
    conteo_acumulado_un = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(500), nullable=False)
    pesado_at = db.Column(db.DateTime(timezone=True), nullable=False)
    timezone_snapshot = db.Column(
        db.String(64), nullable=False, server_default="America/Lima"
    )
    fecha_local_pesaje = db.Column(db.Date, nullable=False)
    pesado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    manga = db.relationship("ScmManga", back_populates="controles_peso")
    tramo = db.relationship("ScmTramoMangaTrabajo", back_populates="control_peso")
    pesado_por = db.relationship("Trabajador")

    def to_dict(self):
        return {
            "id": str(self.public_id),
            "manga_id": str(self.manga.public_id) if self.manga else None,
            "tramo_id": str(self.tramo_id),
            "operation_id": str(self.operation_id),
            "capture_id": str(self.capture_id),
            "station_id": self.station_id,
            "tipo": self.tipo,
            "peso_bruto_kg": _decimal_text(self.peso_bruto_kg),
            "tara_kg": _decimal_text(self.tara_kg),
            "peso_neto_kg": _decimal_text(self.peso_neto_kg),
            "tara_fuente": self.tara_fuente,
            "conteo_acumulado_un": _decimal_text(self.conteo_acumulado_un),
            "motivo": self.motivo,
            "pesado_at": _isoformat(self.pesado_at),
            "fecha_local_pesaje": self.fecha_local_pesaje.isoformat(),
            "pesado_por_id": self.pesado_por_id,
        }





class ScmSolicitudMangaExtra(db.Model):
    __tablename__ = "scm_solicitud_manga_extra"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('PENDIENTE', 'APROBADA', 'RECHAZADA')",
            name="ck_scm_solicitud_manga_extra_estado",
        ),
        db.CheckConstraint(
            "cantidad_solicitada_un > 0",
            name="ck_scm_solicitud_manga_extra_cantidad",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_solicitud_manga_extra_version",
        ),
        db.Index("ix_scm_solicitud_extra_trabajo", "trabajo_ot_id"),
        db.ForeignKeyConstraint(
            ["trabajo_ot_id", "ot_id"],
            ["scm_trabajo_ot.id", "scm_trabajo_ot.orden_trabajo_id"],
            name="fk_scm_solicitud_extra_trabajo_ot",
            ondelete="RESTRICT",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(
        Uuid(as_uuid=True),
        nullable=False,
        unique=True,
        default=uuid.uuid4,
    )
    ot_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "registro_diario_produccion.id",
            name="fk_scm_solicitud_extra_ot",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    trabajo_ot_id = db.Column(
        Uuid(as_uuid=True),
        nullable=True,
    )
    plan_linea_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_plan_manga_op_linea.id",
            name="fk_scm_solicitud_extra_linea",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    cantidad_solicitada_un = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(250), nullable=False)
    estado = db.Column(
        db.String(20),
        nullable=False,
        default="PENDIENTE",
        server_default="PENDIENTE",
    )
    solicitada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_solicitud_extra_solicitante",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    solicitada_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    resuelta_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_solicitud_extra_resolutor",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    resuelta_at = db.Column(db.DateTime(timezone=True), nullable=True)
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    ot = db.relationship("RegistroDiarioProduccion")
    trabajo = db.relationship("ScmTrabajoOt", foreign_keys=[trabajo_ot_id])
    plan_linea = db.relationship("ScmPlanMangaOpLinea")
    solicitada_por = db.relationship(
        "Trabajador",
        foreign_keys=[solicitada_por_id],
    )
    resuelta_por = db.relationship(
        "Trabajador",
        foreign_keys=[resuelta_por_id],
    )


class ScmTrabajoImpresionManga(db.Model):
    __tablename__ = "scm_trabajo_impresion_manga"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('GENERADO', 'PROCESADO', 'PARCIAL', 'FALLIDO')",
            name="ck_scm_trabajo_impresion_estado",
        ),
        db.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_scm_trabajo_impresion_hash",
        ),
    )

    public_id = db.Column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    estado = db.Column(
        db.String(20),
        nullable=False,
        default="GENERADO",
        server_default="GENERADO",
    )
    plantilla_version = db.Column(
        db.String(32),
        nullable=False,
        default="PREPESAJE_TSPL_1",
        server_default="PREPESAJE_TSPL_1",
    )
    payload_hash = db.Column(db.String(64), nullable=False)
    solicitado_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_trabajo_impresion_solicitante",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    station_id = db.Column(
        db.String(36),
        db.ForeignKey(
            "estacion_pesaje.station_id",
            name="fk_scm_trabajo_impresion_estacion",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    processed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    solicitado_por = db.relationship("Trabajador")
    etiquetas = db.relationship(
        "ScmEtiquetaManga",
        back_populates="trabajo_impresion",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ScmEtiquetaManga.id",
    )


class ScmEtiquetaManga(db.Model):
    __tablename__ = "scm_etiqueta_manga"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('PREPESAJE', 'POSTPESAJE')",
            name="ck_scm_etiqueta_manga_tipo",
        ),
        db.CheckConstraint(
            "estado IN ('GENERADA', 'IMPRESA', 'FALLIDA_SIN_EMISION', "
            "'EMISION_INCIERTA', 'INVALIDADA')",
            name="ck_scm_etiqueta_manga_estado",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_etiqueta_manga_version",
        ),
        db.CheckConstraint(
            "length(payload_hash) = 64",
            name="ck_scm_etiqueta_manga_hash",
        ),
        db.UniqueConstraint(
            "public_id",
            name="uq_scm_etiqueta_manga_public_id",
        ),
        db.UniqueConstraint(
            "manga_id",
            "tipo",
            "version",
            name="uq_scm_etiqueta_manga_version",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(
        Uuid(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    manga_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_manga.id",
            name="fk_scm_etiqueta_manga_manga",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    trabajo_impresion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_trabajo_impresion_manga.public_id",
            name="fk_scm_etiqueta_manga_trabajo",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    tipo = db.Column(
        db.String(20),
        nullable=False,
        default="PREPESAJE",
        server_default="PREPESAJE",
    )
    version = db.Column(db.Integer, nullable=False, default=1)
    estado = db.Column(
        db.String(28),
        nullable=False,
        default="GENERADA",
        server_default="GENERADA",
    )
    plantilla_version = db.Column(
        db.String(32),
        nullable=False,
        default="PREPESAJE_TSPL_1",
        server_default="PREPESAJE_TSPL_1",
    )
    payload_json = db.Column(db.JSON, nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    generated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    printed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey(
            "estacion_pesaje.station_id",
            name="fk_scm_etiqueta_manga_estacion",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    printer_name = db.Column(db.String(160), nullable=True)
    error_tecnico = db.Column(db.String(500), nullable=True)
    invalidada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_etiqueta_manga_invalidada_por",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    invalidada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    motivo_invalidacion = db.Column(db.String(500), nullable=True)
    reemplazada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_etiqueta_manga.id",
            name="fk_scm_etiqueta_manga_reemplazo",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )

    manga = db.relationship("ScmManga", back_populates="etiquetas")
    trabajo_impresion = db.relationship(
        "ScmTrabajoImpresionManga",
        back_populates="etiquetas",
    )
    invalidada_por = db.relationship("Trabajador")


class ScmPesajeManga(db.Model):
    """Hecho central e inmutable de peso para una manga SCM."""

    __tablename__ = "scm_pesaje_manga"
    __table_args__ = (
        db.CheckConstraint(
            "peso_bruto_kg > 0 AND tara_kg >= 0 "
            "AND peso_fisico_neto_kg > 0",
            name="ck_scm_pesaje_manga_pesos_positivos",
        ),
        db.CheckConstraint(
            "peso_fisico_neto_kg = peso_bruto_kg - tara_kg",
            name="ck_scm_pesaje_manga_neto",
        ),
        db.CheckConstraint(
            "tara_fuente IN ('TIPO_MANGA', 'MEDIDA_AUTORIZADA', "
            "'CORRECCION')",
            name="ck_scm_pesaje_manga_tara_fuente",
        ),
        db.CheckConstraint(
            "fuente_cantidad IN ('PLAN_CONFIRMADO_POR_PESAJE', "
            "'RESPONSABLE_ARMADO', 'CORRECCION_AUTORIZADA')",
            name="ck_scm_pesaje_manga_fuente_cantidad",
        ),
        db.UniqueConstraint("public_id", name="uq_scm_pesaje_manga_public_id"),
        db.UniqueConstraint("manga_id", name="uq_scm_pesaje_manga_manga"),
        db.UniqueConstraint(
            "operation_id", name="uq_scm_pesaje_manga_operation"
        ),
        db.UniqueConstraint(
            "source_system", "capture_id",
            name="uq_scm_pesaje_manga_capture",
        ),
        db.Index(
            "ix_scm_pesaje_asignacion_personal",
            "asignacion_personal_trabajo_id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(Uuid(as_uuid=True), nullable=False, default=uuid.uuid4)
    manga_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_system = db.Column(db.String(40), nullable=False)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
    )
    capture_id = db.Column(Uuid(as_uuid=True), nullable=False)
    peso_bruto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=False)
    peso_fisico_neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_fuente = db.Column(db.String(28), nullable=False)
    cantidad_confirmada = db.Column(db.Numeric(15, 3), nullable=False)
    fuente_cantidad = db.Column(
        db.String(36),
        nullable=False,
        default="PLAN_CONFIRMADO_POR_PESAJE",
        server_default="PLAN_CONFIRMADO_POR_PESAJE",
    )
    kg_produccion_ot = db.Column(db.Numeric(15, 3), nullable=False)
    pesada_at = db.Column(db.DateTime(timezone=True), nullable=False)
    timezone_snapshot = db.Column(
        db.String(64), nullable=False, server_default="America/Lima"
    )
    fecha_local_pesaje = db.Column(db.Date, nullable=False)
    dias_desfase_operativo = db.Column(db.Integer, nullable=False)
    alerta_fecha = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false"
    )
    motivo_desfase_texto = db.Column(db.String(500), nullable=True)
    pesado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asignacion_personal_trabajo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_asignacion_personal_trabajo_ot.id",
            name="fk_scm_pesaje_asignacion_personal",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    snapshots_json = db.Column(db.JSON, nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )

    manga = db.relationship("ScmManga")
    pesado_por = db.relationship("Trabajador")
    asignacion_personal_trabajo = db.relationship(
        "ScmAsignacionPersonalTrabajoOt",
        back_populates="pesajes",
    )
    anulacion = db.relationship(
        "ScmAnulacionPesajeManga",
        back_populates="pesaje",
        uselist=False,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "public_id": str(self.public_id),
            "manga_id": str(self.manga.public_id) if self.manga else None,
            "operation_id": str(self.operation_id),
            "capture_id": str(self.capture_id),
            "station_id": self.station_id,
            "peso_bruto_kg": _decimal_text(self.peso_bruto_kg),
            "tara_kg": _decimal_text(self.tara_kg),
            "peso_fisico_neto_kg": _decimal_text(self.peso_fisico_neto_kg),
            "cantidad_confirmada": _decimal_text(self.cantidad_confirmada),
            "fuente_cantidad": self.fuente_cantidad,
            "kg_produccion_ot": _decimal_text(self.kg_produccion_ot),
            "pesada_at": _isoformat(self.pesada_at),
            "fecha_local_pesaje": self.fecha_local_pesaje.isoformat(),
            "dias_desfase_operativo": self.dias_desfase_operativo,
            "alerta_fecha": self.alerta_fecha,
            "asignacion_personal_trabajo_id": (
                str(self.asignacion_personal_trabajo_id)
                if self.asignacion_personal_trabajo_id else None
            ),
            "trabajo_color_id": (
                str(self.manga.trabajo_ot_id)
                if self.manga and self.manga.trabajo_ot_id else None
            ),
            "trabajo_color_codigo": (
                self.manga.trabajo.codigo
                if self.manga and self.manga.trabajo else None
            ),
            "estado_manga": self.manga.estado if self.manga else None,
            "estado_inventario": "NO_INGRESADA",
            "ubicacion_id": None,
        }


class ScmAnulacionPesajeManga(db.Model):
    """Compensacion append-only que invalida un pesaje sin borrarlo."""

    __tablename__ = "scm_anulacion_pesaje_manga"
    __table_args__ = (
        db.UniqueConstraint("public_id", name="uq_scm_anulacion_pesaje_public_id"),
        db.UniqueConstraint("pesaje_id", name="uq_scm_anulacion_pesaje_pesaje"),
        db.UniqueConstraint("operation_id", name="uq_scm_anulacion_pesaje_operation"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(Uuid(as_uuid=True), nullable=False, default=uuid.uuid4)
    pesaje_id = db.Column(db.Integer, db.ForeignKey("scm_pesaje_manga.id", ondelete="RESTRICT"), nullable=False)
    motivo = db.Column(db.String(500), nullable=False)
    evidencia = db.Column(db.String(500), nullable=True)
    anulada_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    anulada_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    cantidad_devuelta_plan_un = db.Column(db.Numeric(15, 3), nullable=False)
    ot_reabierta = db.Column(db.Boolean, nullable=False, default=False, server_default="false")

    pesaje = db.relationship("ScmPesajeManga", back_populates="anulacion")
    anulada_por = db.relationship("Trabajador")

    def to_dict(self):
        return {
            "id": str(self.public_id),
            "pesaje_id": str(self.pesaje.public_id),
            "motivo": self.motivo,
            "evidencia": self.evidencia,
            "anulada_por_id": self.anulada_por_id,
            "anulada_at": _isoformat(self.anulada_at),
            "operation_id": str(self.operation_id),
            "cantidad_devuelta_plan_un": _decimal_text(self.cantidad_devuelta_plan_un),
            "ot_reabierta": self.ot_reabierta,
        }


class ScmCorreccionPesajeManga(db.Model):
    """Solicitud y resultado compensatorio; el pesaje original no se edita."""

    __tablename__ = "scm_correccion_pesaje_manga"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('PENDIENTE', 'RECHAZADA', 'APLICADA')",
            name="ck_scm_correccion_pesaje_estado",
        ),
        db.UniqueConstraint(
            "public_id", name="uq_scm_correccion_pesaje_public_id"
        ),
        db.UniqueConstraint(
            "request_operation_id",
            name="uq_scm_correccion_pesaje_request_operation",
        ),
        db.UniqueConstraint(
            "approval_operation_id",
            name="uq_scm_correccion_pesaje_approval_operation",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(Uuid(as_uuid=True), nullable=False, default=uuid.uuid4)
    pesaje_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_pesaje_manga.id", ondelete="RESTRICT"),
        nullable=False,
    )
    estado = db.Column(
        db.String(20), nullable=False, default="PENDIENTE",
        server_default="PENDIENTE",
    )
    proposed_json = db.Column(db.JSON, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    evidence_reference = db.Column(db.String(500), nullable=True)
    requested_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    request_operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    resolved_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    approval_operation_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"),
        nullable=True,
    )
    resolution_reason = db.Column(db.String(500), nullable=True)
    result_projection_json = db.Column(db.JSON, nullable=True)

    pesaje = db.relationship("ScmPesajeManga")
    requested_by = db.relationship(
        "Trabajador", foreign_keys=[requested_by_id]
    )
    resolved_by = db.relationship(
        "Trabajador", foreign_keys=[resolved_by_id]
    )

    def to_dict(self):
        return {
            "id": str(self.public_id),
            "pesaje_id": str(self.pesaje.public_id),
            "estado": self.estado,
            "proposed": self.proposed_json,
            "reason": self.reason,
            "evidence_reference": self.evidence_reference,
            "requested_by_id": self.requested_by_id,
            "requested_at": _isoformat(self.requested_at),
            "resolved_by_id": self.resolved_by_id,
            "resolved_at": _isoformat(self.resolved_at),
            "resolution_reason": self.resolution_reason,
            "result_projection": self.result_projection_json,
        }
