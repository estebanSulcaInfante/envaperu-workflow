"""Preparacion consolidada de material y bolsas trazables del piloto SCM."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ScmRequerimientoMaterialPreparado(db.Model):
    __tablename__ = "scm_requerimiento_material_preparado"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_requerida_kg > 0",
            name="ck_scm_req_mat_prep_cantidad",
        ),
        db.CheckConstraint(
            "estado IN ('PENDIENTE', 'CUBIERTA_PARCIAL', 'CUBIERTA', "
            "'SATISFECHA', 'CANCELADA')",
            name="ck_scm_req_mat_prep_estado",
        ),
        db.UniqueConstraint(
            "corrida_fabricacion_id",
            name="uq_scm_req_mat_prep_corrida",
        ),
        db.UniqueConstraint(
            "operation_id",
            name="uq_scm_req_mat_prep_operacion",
        ),
        db.Index("ix_scm_req_mat_prep_receta", "receta_revision_id"),
        db.Index("ix_scm_req_mat_prep_creador", "created_by_id"),
        db.Index(
            "ix_scm_req_mat_prep_estado_cursor",
            "estado",
            "created_at",
            "id",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    corrida_fabricacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_corrida_fabricacion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    receta_revision_id = db.Column(
        db.Integer,
        db.ForeignKey("receta_color_maestra.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_requerida_kg = db.Column(db.Numeric(15, 3), nullable=False)
    composicion_hash = db.Column(db.String(64), nullable=False)
    composicion_snapshot_json = db.Column(db.JSON, nullable=False)
    estado = db.Column(
        db.String(24), nullable=False, default="PENDIENTE",
        server_default="PENDIENTE",
    )
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    corrida = db.relationship("ScmCorridaFabricacion")
    receta_revision = db.relationship("RecetaColorMaestra")
    asignaciones = db.relationship(
        "ScmAsignacionRequerimientoPreparacion",
        back_populates="requerimiento",
        lazy="select",
    )


class ScmOrdenPreparacionMaterial(db.Model):
    __tablename__ = "scm_orden_preparacion_material"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_objetivo_kg > 0",
            name="ck_scm_opm_cantidad_objetivo",
        ),
        db.CheckConstraint(
            "estado IN ('BORRADOR', 'LIBERADA', 'EN_PREPARACION', "
            "'PENDIENTE_CONCILIACION', 'CERRADA', 'ANULADA')",
            name="ck_scm_opm_estado",
        ),
        db.UniqueConstraint("codigo", name="uq_scm_opm_codigo"),
        db.UniqueConstraint("operation_id", name="uq_scm_opm_operacion"),
        db.Index("ix_scm_opm_receta", "receta_revision_id"),
        db.Index("ix_scm_opm_creador", "created_by_id"),
        db.Index("ix_scm_opm_liberador", "released_by_id"),
        db.Index("ix_scm_opm_iniciador", "started_by_id"),
        db.Index("ix_scm_opm_cerrador", "closed_by_id"),
        db.Index("ix_scm_opm_estado_cursor", "estado", "created_at", "id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(64), nullable=False)
    receta_revision_id = db.Column(
        db.Integer,
        db.ForeignKey("receta_color_maestra.id", ondelete="RESTRICT"),
        nullable=False,
    )
    composicion_hash = db.Column(db.String(64), nullable=False)
    cantidad_objetivo_kg = db.Column(db.Numeric(15, 3), nullable=False)
    estado = db.Column(
        db.String(24), nullable=False, default="BORRADOR",
        server_default="BORRADOR",
    )
    motivo = db.Column(db.String(240), nullable=False)
    perdida_kg = db.Column(db.Numeric(15, 3), nullable=True)
    muestra_kg = db.Column(db.Numeric(15, 3), nullable=True)
    remanente_equipo_kg = db.Column(db.Numeric(15, 3), nullable=True)
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    released_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    started_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    closed_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    closed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    receta_revision = db.relationship("RecetaColorMaestra")
    asignaciones = db.relationship(
        "ScmAsignacionRequerimientoPreparacion",
        back_populates="orden",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="ScmAsignacionRequerimientoPreparacion.created_at",
    )
    requerimientos_insumo = db.relationship(
        "ScmRequerimientoMaterial",
        back_populates="orden_preparacion",
        lazy="select",
    )
    aportes = db.relationship(
        "ScmAportePreparacionMaterial",
        back_populates="orden",
        lazy="select",
        order_by="ScmAportePreparacionMaterial.created_at",
    )
    lecturas = db.relationship(
        "ScmLecturaPesoPreparacion",
        back_populates="orden",
        lazy="select",
        order_by="ScmLecturaPesoPreparacion.created_at",
    )
    bolsas = db.relationship(
        "ScmBolsaMaterialPreparado",
        back_populates="orden",
        lazy="select",
        order_by="ScmBolsaMaterialPreparado.secuencia",
    )
    lote = db.relationship(
        "ScmLoteMaterialPreparado",
        back_populates="orden",
        uselist=False,
    )


class ScmAsignacionRequerimientoPreparacion(db.Model):
    __tablename__ = "scm_asignacion_requerimiento_preparacion"
    __table_args__ = (
        db.CheckConstraint(
            "tipo_fuente IN ('LOTE_PREPARADO_STOCK', 'OPM_ESPERADA')",
            name="ck_scm_asig_req_prep_tipo_fuente",
        ),
        db.CheckConstraint(
            "cantidad_planificada_kg > 0 AND cantidad_comprometida_kg >= 0 "
            "AND cantidad_consumida_kg >= 0 AND "
            "cantidad_consumida_kg <= cantidad_comprometida_kg AND "
            "cantidad_comprometida_kg <= cantidad_planificada_kg",
            name="ck_scm_asig_req_prep_cantidades",
        ),
        db.CheckConstraint(
            "estado IN ('PLANIFICADA', 'COMPROMETIDA', 'SATISFECHA', "
            "'LIBERADA', 'CANCELADA')",
            name="ck_scm_asig_req_prep_estado",
        ),
        db.CheckConstraint(
            "(tipo_fuente = 'OPM_ESPERADA' AND "
            "orden_preparacion_id IS NOT NULL AND lote_id IS NULL AND "
            "bolsa_id IS NULL) OR "
            "(tipo_fuente = 'LOTE_PREPARADO_STOCK' AND "
            "orden_preparacion_id IS NULL AND lote_id IS NOT NULL AND "
            "bolsa_id IS NOT NULL)",
            name="ck_scm_asig_req_prep_fuente",
        ),
        db.UniqueConstraint(
            "orden_preparacion_id",
            "requerimiento_id",
            name="uq_scm_asig_req_prep_opm_req",
        ),
        db.Index("ix_scm_asig_req_prep_opm", "orden_preparacion_id"),
        db.Index("ix_scm_asig_req_prep_req", "requerimiento_id"),
        db.Index("ix_scm_asig_req_prep_lote", "lote_id"),
        db.Index("ix_scm_asig_req_prep_bolsa", "bolsa_id"),
        db.Index("ix_scm_asig_req_prep_creador", "created_by_id"),
        db.Index("ix_scm_asig_req_prep_liberador", "released_by_id"),
        db.Index(
            "uq_scm_asig_req_prep_bolsa_activa",
            "bolsa_id", unique=True,
            postgresql_where=db.text(
                "tipo_fuente = 'LOTE_PREPARADO_STOCK' AND "
                "estado IN ('PLANIFICADA', 'COMPROMETIDA')"
            ),
            sqlite_where=db.text(
                "tipo_fuente = 'LOTE_PREPARADO_STOCK' AND "
                "estado IN ('PLANIFICADA', 'COMPROMETIDA')"
            ),
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orden_preparacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_preparacion_material.id", ondelete="CASCADE"),
        nullable=True,
    )
    requerimiento_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_requerimiento_material_preparado.id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    tipo_fuente = db.Column(db.String(32), nullable=False)
    lote_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_lote_material_preparado.id", ondelete="RESTRICT"),
        nullable=True,
    )
    bolsa_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_bolsa_material_preparado.id", ondelete="RESTRICT"),
        nullable=True,
    )
    cantidad_planificada_kg = db.Column(db.Numeric(15, 3), nullable=False)
    cantidad_comprometida_kg = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    cantidad_consumida_kg = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    estado = db.Column(
        db.String(16), nullable=False, default="PLANIFICADA",
        server_default="PLANIFICADA",
    )
    motivo = db.Column(db.String(240), nullable=False)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    released_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    motivo_liberacion = db.Column(db.String(240), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )

    orden = db.relationship("ScmOrdenPreparacionMaterial", back_populates="asignaciones")
    requerimiento = db.relationship(
        "ScmRequerimientoMaterialPreparado", back_populates="asignaciones"
    )
    lote = db.relationship("ScmLoteMaterialPreparado", lazy="select")
    bolsa = db.relationship(
        "ScmBolsaMaterialPreparado",
        foreign_keys=[bolsa_id],
        lazy="select",
    )
    bolsas_salida = db.relationship(
        "ScmBolsaMaterialPreparado",
        foreign_keys="ScmBolsaMaterialPreparado.asignacion_requerimiento_id",
        back_populates="asignacion_requerimiento",
        lazy="select",
    )
    reservas_material_preparado = db.relationship(
        "ScmReservaMaterialPreparado", back_populates="asignacion",
        lazy="select",
    )
    lecturas_salida = db.relationship(
        "ScmLecturaPesoPreparacion",
        foreign_keys="ScmLecturaPesoPreparacion.asignacion_requerimiento_id",
        back_populates="asignacion_requerimiento",
        lazy="select",
    )

class ScmLecturaPesoPreparacion(db.Model):
    __tablename__ = "scm_lectura_peso_preparacion"
    __table_args__ = (
        db.CheckConstraint(
            "tipo_uso IN ('APORTE', 'BOLSA_SALIDA')",
            name="ck_scm_lectura_prep_tipo_uso",
        ),
        db.CheckConstraint(
            "(tipo_uso = 'APORTE' AND asignacion_requerimiento_id IS NULL) OR "
            "tipo_uso = 'BOLSA_SALIDA'",
            name="ck_scm_lectura_prep_asignacion",
        ),
        db.CheckConstraint(
            "peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0 AND "
            "peso_neto_kg = peso_bruto_kg - tara_kg",
            name="ck_scm_lectura_prep_pesos",
        ),
        db.CheckConstraint(
            "metodo IN ('CONTINGENCIA_MANUAL', 'BALANZA_ESTACION')",
            name="ck_scm_lectura_prep_metodo",
        ),
        db.CheckConstraint(
            "estado IN ('PENDIENTE_SEGUNDA_CONFIRMACION', 'APROBADA', "
            "'INVALIDADA', 'UTILIZADA')",
            name="ck_scm_lectura_prep_estado",
        ),
        db.UniqueConstraint("operation_id", name="uq_scm_lectura_prep_operacion"),
        db.Index("ix_scm_lectura_prep_orden", "orden_preparacion_id"),
        db.Index(
            "ix_scm_lectura_prep_asignacion",
            "asignacion_requerimiento_id",
        ),
        db.Index("ix_scm_lectura_prep_creador", "created_by_id"),
        db.Index("ix_scm_lectura_prep_invalidador", "invalidated_by_id"),
        db.Index("ix_scm_lectura_prep_estado", "estado", "created_at", "id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orden_preparacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_preparacion_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asignacion_requerimiento_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_asignacion_requerimiento_preparacion.id",
            name="fk_scm_lectura_prep_asignacion",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        nullable=True,
    )
    tipo_uso = db.Column(db.String(24), nullable=False)
    peso_bruto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=False)
    peso_neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    metodo = db.Column(db.String(32), nullable=False)
    evidencia_ref = db.Column(db.String(160), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    estado = db.Column(
        db.String(36), nullable=False,
        default="PENDIENTE_SEGUNDA_CONFIRMACION",
        server_default="PENDIENTE_SEGUNDA_CONFIRMACION",
    )
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    invalidated_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    invalidated_at = db.Column(db.DateTime(timezone=True), nullable=True)
    invalidation_reason = db.Column(db.String(240), nullable=True)
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    orden = db.relationship("ScmOrdenPreparacionMaterial", back_populates="lecturas")
    asignacion_requerimiento = db.relationship(
        "ScmAsignacionRequerimientoPreparacion",
        foreign_keys=[asignacion_requerimiento_id],
        back_populates="lecturas_salida",
    )
    aprobacion = db.relationship(
        "ScmAprobacionLecturaPesoPreparacion",
        back_populates="lectura",
        uselist=False,
    )


class ScmAprobacionLecturaPesoPreparacion(db.Model):
    __tablename__ = "scm_aprobacion_lectura_peso_preparacion"
    __table_args__ = (
        db.CheckConstraint(
            "peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0 AND "
            "peso_neto_kg = peso_bruto_kg - tara_kg",
            name="ck_scm_aprob_lect_prep_pesos",
        ),
        db.UniqueConstraint("lectura_id", name="uq_scm_aprobacion_lectura_prep"),
        db.UniqueConstraint("operation_id", name="uq_scm_aprob_lect_prep_operacion"),
        db.Index("ix_scm_aprob_lect_prep_actor", "actor_id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lectura_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_lectura_peso_preparacion.id", ondelete="RESTRICT"),
        nullable=False,
    )
    lectura_version = db.Column(db.Integer, nullable=False)
    peso_bruto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=False)
    peso_neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    actor_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    lectura = db.relationship("ScmLecturaPesoPreparacion", back_populates="aprobacion")


class ScmAportePreparacionMaterial(db.Model):
    __tablename__ = "scm_aporte_preparacion_material"
    __table_args__ = (
        db.CheckConstraint(
            "peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0 AND "
            "peso_neto_kg = peso_bruto_kg - tara_kg",
            name="ck_scm_aporte_prep_pesos",
        ),
        db.CheckConstraint(
            "metodo IN ('CONTINGENCIA_MANUAL', 'BALANZA_ESTACION')",
            name="ck_scm_aporte_prep_metodo",
        ),
        db.CheckConstraint(
            "estado = 'INCORPORADO'",
            name="ck_scm_aporte_prep_estado",
        ),
        db.UniqueConstraint("operation_id", name="uq_scm_aporte_prep_operacion"),
        db.Index("ix_scm_aporte_prep_orden", "orden_preparacion_id"),
        db.Index("ix_scm_aporte_prep_emision", "emision_id"),
        db.Index("ix_scm_aporte_prep_lectura", "lectura_id"),
        db.Index("ix_scm_aporte_prep_creador", "created_by_id"),
        db.Index("ix_scm_aporte_prep_confirmador", "confirmed_by_id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orden_preparacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_preparacion_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    emision_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_emision_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    lectura_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_lectura_peso_preparacion.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    peso_bruto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=False)
    peso_neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    metodo = db.Column(db.String(32), nullable=False)
    evidencia_ref = db.Column(db.String(160), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    estado = db.Column(
        db.String(32), nullable=False, default="INCORPORADO",
        server_default="INCORPORADO",
    )
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    orden = db.relationship("ScmOrdenPreparacionMaterial", back_populates="aportes")
    emision = db.relationship("ScmEmisionMaterial")
    lectura = db.relationship("ScmLecturaPesoPreparacion")


class ScmLoteMaterialPreparado(db.Model):
    __tablename__ = "scm_lote_material_preparado"
    __table_args__ = (
        db.CheckConstraint("cantidad_kg > 0", name="ck_scm_lote_mat_prep_cantidad"),
        db.CheckConstraint(
            "estado IN ('PENDIENTE_RECEPCION', 'PENDIENTE_CALIDAD', "
            "'DISPONIBLE', 'BLOQUEADO', "
            "'RECHAZADO', 'AGOTADO')",
            name="ck_scm_lote_mat_prep_estado",
        ),
        db.UniqueConstraint("codigo", name="uq_scm_lote_mat_prep_codigo"),
        db.UniqueConstraint(
            "orden_preparacion_id", name="uq_scm_lote_mat_prep_opm"
        ),
        db.Index("ix_scm_lote_mat_prep_receta", "receta_revision_id"),
        db.Index("ix_scm_lote_mat_prep_creador", "created_by_id"),
        db.Index(
            "ix_scm_lote_mat_prep_estado_cursor",
            "estado", "created_at", "id",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(64), nullable=False)
    orden_preparacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_preparacion_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    receta_revision_id = db.Column(
        db.Integer,
        db.ForeignKey("receta_color_maestra.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_kg = db.Column(db.Numeric(15, 3), nullable=False)
    estado = db.Column(
        db.String(24), nullable=False, default="PENDIENTE_RECEPCION",
        server_default="PENDIENTE_RECEPCION",
    )
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    orden = db.relationship("ScmOrdenPreparacionMaterial", back_populates="lote")
    receta_revision = db.relationship("RecetaColorMaestra")
    bolsas = db.relationship(
        "ScmBolsaMaterialPreparado", back_populates="lote", lazy="select"
    )
    decisiones_calidad = db.relationship(
        "ScmDecisionCalidadMaterialPreparado",
        back_populates="lote",
        lazy="select",
        order_by="ScmDecisionCalidadMaterialPreparado.created_at",
    )


class ScmBolsaMaterialPreparado(db.Model):
    __tablename__ = "scm_bolsa_material_preparado"
    __table_args__ = (
        db.CheckConstraint("secuencia > 0", name="ck_scm_bolsa_mat_prep_secuencia"),
        db.CheckConstraint(
            "peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0 AND "
            "peso_neto_kg = peso_bruto_kg - tara_kg",
            name="ck_scm_bolsa_mat_prep_pesos",
        ),
        db.CheckConstraint(
            "metodo = 'CONTINGENCIA_MANUAL'",
            name="ck_scm_bolsa_mat_prep_metodo",
        ),
        db.CheckConstraint(
            "estado IN ('PENDIENTE_CONFIRMACION', 'PESADA', "
            "'PENDIENTE_RECEPCION', 'PENDIENTE_CALIDAD', 'DISPONIBLE', "
            "'RESERVADA', 'EMITIDA', 'CONSUMIDA', 'DEVUELTA', "
            "'BLOQUEADA', 'RECHAZADA')",
            name="ck_scm_bolsa_mat_prep_estado",
        ),
        db.UniqueConstraint("codigo", name="uq_scm_bolsa_mat_prep_codigo"),
        db.UniqueConstraint(
            "orden_preparacion_id", "secuencia", name="uq_scm_bolsa_mat_prep_seq"
        ),
        db.UniqueConstraint("operation_id", name="uq_scm_bolsa_mat_prep_operacion"),
        db.Index("ix_scm_bolsa_mat_prep_orden", "orden_preparacion_id"),
        db.Index("ix_scm_bolsa_mat_prep_lote", "lote_id"),
        db.Index("ix_scm_bolsa_mat_prep_lectura", "lectura_id"),
        db.Index(
            "ix_scm_bolsa_mat_prep_asignacion",
            "asignacion_requerimiento_id",
        ),
        db.Index("ix_scm_bolsa_mat_prep_ubicacion", "ubicacion_id"),
        db.Index("ix_scm_bolsa_mat_prep_creador", "created_by_id"),
        db.Index("ix_scm_bolsa_mat_prep_confirmador", "confirmed_by_id"),
        db.Index("ix_scm_bolsa_mat_prep_estado", "estado", "created_at", "id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(64), nullable=False)
    orden_preparacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_preparacion_material.id", ondelete="RESTRICT"),
        nullable=False,
    )
    lote_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_lote_material_preparado.id", ondelete="RESTRICT"),
        nullable=True,
    )
    lectura_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_lectura_peso_preparacion.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    asignacion_requerimiento_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_asignacion_requerimiento_preparacion.id",
            name="fk_scm_bolsa_mat_prep_asignacion",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        nullable=True,
    )
    secuencia = db.Column(db.Integer, nullable=False)
    peso_bruto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=False)
    peso_neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    metodo = db.Column(db.String(32), nullable=False)
    evidencia_ref = db.Column(db.String(160), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    estado = db.Column(
        db.String(32), nullable=False, default="PENDIENTE_RECEPCION",
        server_default="PENDIENTE_RECEPCION",
    )
    ubicacion_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    confirmed_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    orden = db.relationship("ScmOrdenPreparacionMaterial", back_populates="bolsas")
    lote = db.relationship("ScmLoteMaterialPreparado", back_populates="bolsas")
    reservas = db.relationship(
        "ScmReservaMaterialPreparado", back_populates="bolsa", lazy="select"
    )
    ubicacion = db.relationship("ScmUbicacionInventario")
    recepcion = db.relationship(
        "ScmRecepcionBolsaMaterialPreparado",
        back_populates="bolsa",
        uselist=False,
    )
    lectura = db.relationship("ScmLecturaPesoPreparacion")
    asignacion_requerimiento = db.relationship(
        "ScmAsignacionRequerimientoPreparacion",
        foreign_keys=[asignacion_requerimiento_id],
        back_populates="bolsas_salida",
    )
    decision_calidad = db.relationship(
        "ScmDecisionCalidadMaterialPreparado",
        back_populates="bolsa",
        uselist=False,
    )


class ScmDecisionCalidadMaterialPreparado(db.Model):
    __tablename__ = "scm_decision_calidad_material_preparado"
    __table_args__ = (
        db.CheckConstraint(
            "decision IN ('LIBERAR', 'BLOQUEAR', 'RECHAZAR')",
            name="ck_scm_calidad_mat_prep_decision",
        ),
        db.UniqueConstraint("operation_id", name="uq_scm_calidad_mat_prep_operacion"),
        db.UniqueConstraint("bolsa_id", name="uq_scm_calidad_mat_prep_bolsa"),
        db.Index("ix_scm_calidad_mat_prep_lote", "lote_id"),
        db.Index("ix_scm_calidad_mat_prep_bolsa", "bolsa_id"),
        db.Index("ix_scm_calidad_mat_prep_actor", "actor_id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lote_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_lote_material_preparado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    bolsa_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_bolsa_material_preparado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision = db.Column(db.String(16), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    actor_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    lote = db.relationship(
        "ScmLoteMaterialPreparado", back_populates="decisiones_calidad"
    )
    bolsa = db.relationship("ScmBolsaMaterialPreparado", back_populates="decision_calidad")


class ScmReservaMaterialPreparado(db.Model):
    __tablename__ = "scm_reserva_material_preparado"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('ACTIVA', 'CONSUMIDA', 'DEVUELTA', 'LIBERADA', "
            "'CANCELADA')",
            name="ck_scm_reserva_mat_prep_estado",
        ),
        db.CheckConstraint(
            "cantidad_kg > 0", name="ck_scm_reserva_mat_prep_cantidad"
        ),
        db.UniqueConstraint("operation_id", name="uq_scm_reserva_mat_prep_operacion"),
        db.Index("ix_scm_reserva_mat_prep_bolsa", "bolsa_id"),
        db.Index("ix_scm_reserva_mat_prep_trabajo", "trabajo_ot_id"),
        db.Index("ix_scm_reserva_mat_prep_req", "requerimiento_id"),
        db.Index("ix_scm_reserva_mat_prep_asignacion", "asignacion_id"),
        db.Index("ix_scm_reserva_mat_prep_actor", "created_by_id"),
        db.Index("ix_scm_reserva_mat_prep_origen", "ubicacion_origen_id"),
        db.Index("ix_scm_reserva_mat_prep_cursor", "created_at", "id"),
        db.Index(
            "uq_scm_reserva_mat_prep_activa",
            "bolsa_id",
            unique=True,
            postgresql_where=db.text("estado = 'ACTIVA'"),
            sqlite_where=db.text("estado = 'ACTIVA'"),
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bolsa_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_bolsa_material_preparado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    asignacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_asignacion_requerimiento_preparacion.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    trabajo_ot_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_trabajo_ot.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requerimiento_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_requerimiento_material_preparado.id", ondelete="RESTRICT"
        ),
        nullable=False,
    )
    ubicacion_origen_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_kg = db.Column(db.Numeric(15, 3), nullable=False)
    estado = db.Column(
        db.String(16), nullable=False, default="ACTIVA", server_default="ACTIVA"
    )
    motivo = db.Column(db.String(240), nullable=False)
    created_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    bolsa = db.relationship("ScmBolsaMaterialPreparado", back_populates="reservas")
    trabajo = db.relationship("ScmTrabajoOt")
    requerimiento = db.relationship("ScmRequerimientoMaterialPreparado")
    asignacion = db.relationship(
        "ScmAsignacionRequerimientoPreparacion",
        back_populates="reservas_material_preparado",
    )
    ubicacion_origen = db.relationship("ScmUbicacionInventario")
    emision = db.relationship(
        "ScmEmisionMaterialPreparado", back_populates="reserva", uselist=False
    )


class ScmEmisionMaterialPreparado(db.Model):
    __tablename__ = "scm_emision_material_preparado"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('PREPARADA', 'EN_TRANSITO', 'RECIBIDA_MAQUINA', "
            "'CERRADA', 'RETORNADA_TOTAL', 'CANCELADA')",
            name="ck_scm_emision_mat_prep_estado",
        ),
        db.UniqueConstraint("reserva_id", name="uq_scm_emision_mat_prep_reserva"),
        db.UniqueConstraint("operation_id", name="uq_scm_emision_mat_prep_operacion"),
        db.Index("ix_scm_emision_mat_prep_actor", "actor_id"),
        db.Index("ix_scm_emision_mat_prep_despachador", "dispatched_by_id"),
        db.Index("ix_scm_emision_mat_prep_receptor", "received_by_id"),
        db.Index("ix_scm_emision_mat_prep_retornador", "returned_by_id"),
        db.Index("ix_scm_emision_mat_prep_cerrador", "closed_by_id"),
        db.Index("ix_scm_emision_mat_prep_destino", "ubicacion_destino_id"),
        db.Index("ix_scm_emision_mat_prep_maquina", "maquina_recepcion_id"),
        db.Index("ix_scm_emision_mat_prep_retorno", "ubicacion_retorno_id"),
        db.Index("ix_scm_emision_mat_prep_cursor", "created_at", "id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reserva_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_reserva_material_preparado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ubicacion_destino_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ubicacion_retorno_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=True,
    )
    maquina_recepcion_id = db.Column(
        db.Integer,
        db.ForeignKey("maquina.id", ondelete="RESTRICT"),
        nullable=True,
    )
    punto_recepcion_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=True,
    )
    maquina_qr_snapshot = db.Column(db.String(80), nullable=True)
    bolsa_qr_snapshot = db.Column(db.String(80), nullable=True)
    recepcion_metodo = db.Column(db.String(32), nullable=True)
    estado = db.Column(
        db.String(24), nullable=False, default="PREPARADA", server_default="PREPARADA"
    )
    motivo = db.Column(db.String(240), nullable=False)
    actor_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    dispatched_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True
    )
    received_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True
    )
    returned_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True
    )
    closed_by_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=True
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )
    dispatched_at = db.Column(db.DateTime(timezone=True), nullable=True)
    received_at = db.Column(db.DateTime(timezone=True), nullable=True)
    consumed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    returned_at = db.Column(db.DateTime(timezone=True), nullable=True)
    cancelled_at = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    reserva = db.relationship("ScmReservaMaterialPreparado", back_populates="emision")
    ubicacion_destino = db.relationship(
        "ScmUbicacionInventario", foreign_keys=[ubicacion_destino_id]
    )
    ubicacion_retorno = db.relationship(
        "ScmUbicacionInventario", foreign_keys=[ubicacion_retorno_id]
    )
    maquina_recepcion = db.relationship("Maquina")
    punto_recepcion = db.relationship(
        "ScmUbicacionInventario", foreign_keys=[punto_recepcion_id]
    )


class ScmRecepcionBolsaMaterialPreparado(db.Model):
    __tablename__ = "scm_recepcion_bolsa_material_preparado"
    __table_args__ = (
        db.UniqueConstraint("bolsa_id", name="uq_scm_recepcion_bolsa_mat_prep"),
        db.UniqueConstraint("operation_id", name="uq_scm_recepcion_mat_prep_operacion"),
        db.Index("ix_scm_recepcion_mat_prep_ubicacion", "ubicacion_id"),
        db.Index("ix_scm_recepcion_mat_prep_actor", "actor_id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bolsa_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_bolsa_material_preparado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ubicacion_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    motivo = db.Column(db.String(240), nullable=False)
    actor_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    bolsa = db.relationship("ScmBolsaMaterialPreparado", back_populates="recepcion")
    ubicacion = db.relationship("ScmUbicacionInventario")


class ScmSaldoMaterialPreparado(db.Model):
    __tablename__ = "scm_saldo_material_preparado"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_fisica_kg >= 0 AND cantidad_reservada_kg >= 0 AND "
            "cantidad_no_disponible_kg >= 0 AND "
            "cantidad_reservada_kg + cantidad_no_disponible_kg <= "
            "cantidad_fisica_kg",
            name="ck_scm_saldo_mat_prep_cantidades",
        ),
        db.UniqueConstraint(
            "receta_revision_id", "ubicacion_id",
            name="uq_scm_saldo_mat_prep_receta_ubicacion",
        ),
        db.Index("ix_scm_saldo_mat_prep_receta", "receta_revision_id"),
        db.Index("ix_scm_saldo_mat_prep_ubicacion", "ubicacion_id"),
        db.Index("ix_scm_saldo_mat_prep_actualizado", "updated_at", "id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    receta_revision_id = db.Column(
        db.Integer,
        db.ForeignKey("receta_color_maestra.id", ondelete="RESTRICT"),
        nullable=False,
    )
    ubicacion_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_fisica_kg = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    cantidad_reservada_kg = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    cantidad_no_disponible_kg = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        onupdate=utc_now, server_default=db.func.now(),
    )

    receta_revision = db.relationship("RecetaColorMaestra")
    ubicacion = db.relationship("ScmUbicacionInventario")


class ScmMovimientoMaterialPreparado(db.Model):
    __tablename__ = "scm_movimiento_material_preparado"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('RECEPCION', 'LIBERACION_CALIDAD', 'BLOQUEO_CALIDAD', "
            "'RESERVA', 'LIBERACION_RESERVA', 'EMISION_SALIDA', "
            "'EMISION_ENTRADA', 'CONSUMO', "
            "'RETORNO_SALIDA', 'RETORNO_ENTRADA')",
            name="ck_scm_mov_mat_prep_tipo",
        ),
        db.CheckConstraint(
            "delta_fisico_kg <> 0 OR delta_reservado_kg <> 0 OR "
            "delta_no_disponible_kg <> 0",
            name="ck_scm_mov_mat_prep_delta",
        ),
        db.CheckConstraint(
            "saldo_fisico_resultante_kg >= 0 AND "
            "saldo_reservado_resultante_kg >= 0 AND "
            "saldo_no_disponible_resultante_kg >= 0",
            name="ck_scm_mov_mat_prep_resultado",
        ),
        db.UniqueConstraint("operation_id", name="uq_scm_mov_mat_prep_operacion"),
        db.Index("ix_scm_mov_mat_prep_saldo", "saldo_id"),
        db.Index("ix_scm_mov_mat_prep_bolsa", "bolsa_id"),
        db.Index("ix_scm_mov_mat_prep_actor", "actor_id"),
        db.Index("ix_scm_mov_mat_prep_cursor", "created_at", "id"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    saldo_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_saldo_material_preparado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    bolsa_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_bolsa_material_preparado.id", ondelete="RESTRICT"),
        nullable=False,
    )
    tipo = db.Column(db.String(24), nullable=False)
    delta_fisico_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0)
    delta_reservado_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0)
    delta_no_disponible_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0)
    saldo_fisico_resultante_kg = db.Column(db.Numeric(15, 3), nullable=False)
    saldo_reservado_resultante_kg = db.Column(db.Numeric(15, 3), nullable=False)
    saldo_no_disponible_resultante_kg = db.Column(db.Numeric(15, 3), nullable=False)
    motivo = db.Column(db.String(240), nullable=False)
    actor_id = db.Column(
        db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now,
        server_default=db.func.now(),
    )

    saldo = db.relationship("ScmSaldoMaterialPreparado")
    bolsa = db.relationship("ScmBolsaMaterialPreparado")
