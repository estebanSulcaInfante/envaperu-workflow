"""Modelo documental SCM para demanda, fabricacion, ensamble y cobertura.

Estas tablas son la fase *expand* de TS-010P. La tabla legacy
``orden_produccion`` permanece intacta y se vincula como alias tecnico de una
OF durante el backfill.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


OP_STATES = (
    "BORRADOR",
    "APROBADA",
    "PLANIFICADA",
    "EN_COBERTURA",
    "COMPLETADA",
    "CANCELADA",
)
OP_LINE_STATES = ("ACTIVA", "CANCELADA", "SATISFECHA")
OPERATION_TYPES = ("FABRICACION", "ENSAMBLE")
OPERATION_STATES = (
    "BORRADOR",
    "LIBERADA",
    "PROGRAMADA",
    "EN_EJECUCION",
    "CERRADA",
    "ANULADA",
)
RUN_STATES = (
    "BORRADOR",
    "LIBERADA",
    "EN_EJECUCION",
    "COMPLETADA",
    "ANULADA",
)
SUPPLY_SOURCE_TYPES = ("STOCK", "SALIDA_ORDEN")
ALLOCATION_STATES = (
    "PLANIFICADA",
    "COMPROMETIDA",
    "SATISFECHA",
    "CANCELADA",
)


class ScmOrdenProduccion(db.Model):
    """Demanda de uno o varios ProductosTerminados."""

    __tablename__ = "scm_orden_produccion"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN "
            "('BORRADOR', 'APROBADA', 'PLANIFICADA', 'EN_COBERTURA', "
            "'COMPLETADA', 'CANCELADA')",
            name="ck_scm_orden_produccion_estado",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_orden_produccion_version",
        ),
        db.UniqueConstraint(
            "codigo",
            name="uq_scm_orden_produccion_codigo",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(32), nullable=False)
    origen = db.Column(db.String(32), nullable=False)
    referencia_origen = db.Column(db.String(100), nullable=True)
    fecha_necesidad = db.Column(db.Date, nullable=False)
    prioridad = db.Column(
        db.String(24),
        nullable=False,
        default="NORMAL",
        server_default="NORMAL",
    )
    estado = db.Column(
        db.String(24),
        nullable=False,
        default="BORRADOR",
        server_default="BORRADOR",
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approved_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    approved_at = db.Column(db.DateTime(timezone=True), nullable=True)
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

    lineas = db.relationship(
        "ScmOrdenProduccionLinea",
        back_populates="orden_produccion",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    planes = db.relationship(
        "ScmPlanProduccion",
        back_populates="orden_produccion",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ScmPlanProduccion.revision",
    )
    created_by = db.relationship("Trabajador", foreign_keys=[created_by_id])
    approved_by = db.relationship("Trabajador", foreign_keys=[approved_by_id])


class ScmOrdenProduccionLinea(db.Model):
    """Necesidad indivisible de PT dentro de una OP."""

    __tablename__ = "scm_orden_produccion_linea"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_solicitada > 0",
            name="ck_scm_op_linea_cantidad",
        ),
        db.CheckConstraint(
            "estado IN ('ACTIVA', 'CANCELADA', 'SATISFECHA')",
            name="ck_scm_op_linea_estado",
        ),
        db.CheckConstraint("version > 0", name="ck_scm_op_linea_version"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orden_produccion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_orden_produccion.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    producto_terminado_id = db.Column(
        db.String(50),
        db.ForeignKey(
            "producto_terminado.cod_sku_pt",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    cantidad_solicitada = db.Column(db.Numeric(15, 3), nullable=False)
    fecha_necesidad = db.Column(db.Date, nullable=True)
    estructura_revision_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_estructura_revision.id", ondelete="RESTRICT"),
        nullable=True,
    )
    estructura_hash = db.Column(db.String(64), nullable=True)
    ruta_revision_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_ruta_revision.id", ondelete="RESTRICT"),
        nullable=True,
    )
    ruta_hash = db.Column(db.String(64), nullable=True)
    estado = db.Column(
        db.String(24),
        nullable=False,
        default="ACTIVA",
        server_default="ACTIVA",
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    orden_produccion = db.relationship(
        "ScmOrdenProduccion",
        back_populates="lineas",
    )
    producto_terminado = db.relationship("ProductoTerminado")
    asignaciones = db.relationship(
        "ScmAsignacionDemandaSuministro",
        back_populates="orden_produccion_linea",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    reservas_inventario = db.relationship(
        "ScmReservaInventario",
        back_populates="orden_produccion_linea",
        lazy="selectin",
    )


class ScmPlanProduccion(db.Model):
    """Propuesta auditable entre una OP aprobada y documentos operativos."""

    __tablename__ = "scm_plan_produccion"
    __table_args__ = (
        db.CheckConstraint(
            "revision > 0",
            name="ck_scm_plan_produccion_revision",
        ),
        db.CheckConstraint(
            "estado IN ('CALCULADO', 'CONFIRMADO', 'SUPERADO')",
            name="ck_scm_plan_produccion_estado",
        ),
        db.CheckConstraint(
            "length(input_hash) = 64 AND length(content_hash) = 64",
            name="ck_scm_plan_produccion_hashes",
        ),
        db.UniqueConstraint(
            "orden_produccion_id",
            "revision",
            name="uq_scm_plan_produccion_revision",
        ),
        db.UniqueConstraint(
            "operation_id",
            name="uq_scm_plan_produccion_operation",
        ),
        db.Index(
            "ux_scm_plan_produccion_calculado",
            "orden_produccion_id",
            unique=True,
            postgresql_where=db.text("estado = 'CALCULADO'"),
            sqlite_where=db.text("estado = 'CALCULADO'"),
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orden_produccion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_produccion.id", ondelete="CASCADE"),
        nullable=False,
    )
    revision = db.Column(db.Integer, nullable=False)
    estado = db.Column(
        db.String(24),
        nullable=False,
        default="CALCULADO",
        server_default="CALCULADO",
    )
    input_hash = db.Column(db.String(64), nullable=False)
    content_hash = db.Column(db.String(64), nullable=False)
    propuesta_json = db.Column(db.JSON, nullable=False)
    calculado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=False,
    )
    confirmado_por_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        server_default=db.func.now(),
    )
    confirmado_at = db.Column(db.DateTime(timezone=True), nullable=True)

    orden_produccion = db.relationship(
        "ScmOrdenProduccion",
        back_populates="planes",
    )
    calculado_por = db.relationship(
        "Trabajador",
        foreign_keys=[calculado_por_id],
    )
    confirmado_por = db.relationship(
        "Trabajador",
        foreign_keys=[confirmado_por_id],
    )


class ScmOrdenOperacion(db.Model):
    """Cabecera comun para OF y OE."""

    __tablename__ = "scm_orden_operacion"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('FABRICACION', 'ENSAMBLE')",
            name="ck_scm_orden_operacion_tipo",
        ),
        db.CheckConstraint(
            "estado IN "
            "('BORRADOR', 'LIBERADA', 'PROGRAMADA', 'EN_EJECUCION', "
            "'CERRADA', 'ANULADA')",
            name="ck_scm_orden_operacion_estado",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_orden_operacion_version",
        ),
        db.UniqueConstraint("codigo", name="uq_scm_orden_operacion_codigo"),
        db.UniqueConstraint(
            "plan_produccion_id",
            "propuesta_clave",
            name="uq_scm_orden_operacion_plan_propuesta",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(32), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    origen_demanda = db.Column(db.String(32), nullable=False)
    motivo = db.Column(db.Text, nullable=True)
    plan_produccion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_plan_produccion.id", ondelete="RESTRICT"),
        nullable=True,
    )
    propuesta_clave = db.Column(db.String(80), nullable=True)
    estado = db.Column(
        db.String(24),
        nullable=False,
        default="BORRADOR",
        server_default="BORRADOR",
    )
    operacion_ruta_revision_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_operacion_ruta.id", ondelete="RESTRICT"),
        nullable=True,
    )
    operacion_ruta_hash = db.Column(db.String(64), nullable=True)
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        # Las OF provenientes del backfill no tienen actor historico
        # demostrable. La API exige actor para toda orden nueva.
        nullable=True,
    )
    released_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    released_at = db.Column(db.DateTime(timezone=True), nullable=True)
    started_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    closed_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    closed_at = db.Column(db.DateTime(timezone=True), nullable=True)
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

    fabricacion = db.relationship(
        "ScmOrdenFabricacion",
        back_populates="orden_operacion",
        uselist=False,
        cascade="all, delete-orphan",
    )
    salidas = db.relationship(
        "ScmOrdenOperacionSalida",
        back_populates="orden_operacion",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    created_by = db.relationship("Trabajador", foreign_keys=[created_by_id])
    released_by = db.relationship("Trabajador", foreign_keys=[released_by_id])
    started_by = db.relationship("Trabajador", foreign_keys=[started_by_id])
    closed_by = db.relationship("Trabajador", foreign_keys=[closed_by_id])
    plan_produccion = db.relationship("ScmPlanProduccion")


class ScmOrdenFabricacion(db.Model):
    """Extension tecnica 1:1 de una orden operativa FABRICACION."""

    __tablename__ = "scm_orden_fabricacion"
    __table_args__ = (
        db.CheckConstraint(
            "snapshot_tiempo_ciclo_seg IS NULL "
            "OR snapshot_tiempo_ciclo_seg > 0",
            name="ck_scm_of_tiempo_ciclo",
        ),
        db.CheckConstraint(
            "snapshot_horas_turno IS NULL OR snapshot_horas_turno > 0",
            name="ck_scm_of_horas_turno",
        ),
        db.CheckConstraint(
            "snapshot_peso_colada_gr IS NULL "
            "OR snapshot_peso_colada_gr >= 0",
            name="ck_scm_of_peso_colada",
        ),
        db.UniqueConstraint(
            "codigo_legacy_op",
            name="uq_scm_of_codigo_legacy",
        ),
    )

    orden_operacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_operacion.id", ondelete="CASCADE"),
        primary_key=True,
    )
    molde_id = db.Column(
        db.String(50),
        db.ForeignKey("molde.codigo", ondelete="RESTRICT"),
        nullable=True,
    )
    maquina_prevista_id = db.Column(
        db.Integer,
        db.ForeignKey("maquina.id", ondelete="RESTRICT"),
        nullable=True,
    )
    snapshot_tiempo_ciclo_seg = db.Column(db.Numeric(12, 4), nullable=True)
    snapshot_horas_turno = db.Column(db.Numeric(8, 3), nullable=True)
    snapshot_peso_colada_gr = db.Column(db.Numeric(12, 4), nullable=True)
    codigo_legacy_op = db.Column(
        db.String(20),
        db.ForeignKey("orden_produccion.numero_op", ondelete="RESTRICT"),
        nullable=True,
    )

    orden_operacion = db.relationship(
        "ScmOrdenOperacion",
        back_populates="fabricacion",
    )
    corridas = db.relationship(
        "ScmCorridaFabricacion",
        back_populates="orden_fabricacion",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ScmCorridaFabricacion.secuencia",
    )


class ScmCorridaFabricacion(db.Model):
    """Corrida de una OF para exactamente un color y una receta."""

    __tablename__ = "scm_corrida_fabricacion"
    __table_args__ = (
        db.CheckConstraint(
            "secuencia > 0",
            name="ck_scm_corrida_secuencia",
        ),
        db.CheckConstraint(
            "ciclos_objetivo IS NULL OR ciclos_objetivo > 0",
            name="ck_scm_corrida_ciclos",
        ),
        db.CheckConstraint(
            "estado IN "
            "('BORRADOR', 'LIBERADA', 'EN_EJECUCION', 'COMPLETADA', "
            "'ANULADA')",
            name="ck_scm_corrida_estado",
        ),
        db.UniqueConstraint(
            "orden_fabricacion_id",
            "secuencia",
            name="uq_scm_corrida_of_secuencia",
        ),
        db.UniqueConstraint(
            "orden_fabricacion_id",
            "codigo",
            name="uq_scm_corrida_of_codigo",
        ),
        db.UniqueConstraint(
            "lote_color_legacy_id",
            name="uq_scm_corrida_lote_legacy",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orden_fabricacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_orden_fabricacion.orden_operacion_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    codigo = db.Column(db.String(48), nullable=False)
    secuencia = db.Column(db.Integer, nullable=False)
    color_produccion_id = db.Column(
        db.Integer,
        db.ForeignKey("color_produccion.id", ondelete="RESTRICT"),
        nullable=True,
    )
    receta_revision_id = db.Column(
        db.Integer,
        db.ForeignKey("receta_color_maestra.id", ondelete="RESTRICT"),
        nullable=True,
    )
    receta_hash = db.Column(db.String(64), nullable=True)
    ciclos_objetivo = db.Column(db.Integer, nullable=True)
    estado = db.Column(
        db.String(24),
        nullable=False,
        default="BORRADOR",
        server_default="BORRADOR",
    )
    lote_color_legacy_id = db.Column(
        db.Integer,
        db.ForeignKey("lote_color.id", ondelete="RESTRICT"),
        nullable=True,
    )
    meta_kg_legacy = db.Column(db.Numeric(15, 6), nullable=True)

    orden_fabricacion = db.relationship(
        "ScmOrdenFabricacion",
        back_populates="corridas",
    )
    receta_revision = db.relationship("RecetaColorMaestra")
    corrida_premezclas = db.relationship(
        "ScmLotePremezcla", back_populates="corrida", lazy="selectin",
        order_by="ScmLotePremezcla.secuencia",
    )
    salidas = db.relationship(
        "ScmOrdenOperacionSalida",
        back_populates="corrida_fabricacion",
        lazy="selectin",
    )


class ScmOrdenOperacionSalida(db.Model):
    """Salida planificada generica de una OF u OE."""

    __tablename__ = "scm_orden_operacion_salida"
    __table_args__ = (
        db.CheckConstraint(
            "cantidad_objetivo > 0",
            name="ck_scm_salida_cantidad",
        ),
        db.CheckConstraint(
            "cantidad_por_ciclo_snapshot IS NULL "
            "OR cantidad_por_ciclo_snapshot > 0",
            name="ck_scm_salida_por_ciclo",
        ),
        db.CheckConstraint(
            "peso_unitario_snapshot_g IS NULL "
            "OR peso_unitario_snapshot_g > 0",
            name="ck_scm_salida_peso_unitario",
        ),
        db.CheckConstraint(
            "kg_estandar_objetivo IS NULL OR kg_estandar_objetivo >= 0",
            name="ck_scm_salida_kg_estandar",
        ),
        db.CheckConstraint(
            "excedente_objetivo >= 0",
            name="ck_scm_salida_excedente",
        ),
        db.UniqueConstraint(
            "orden_operacion_id",
            "corrida_fabricacion_id",
            "articulo_scm_id",
            name="uq_scm_salida_orden_corrida_articulo",
        ),
        db.UniqueConstraint(
            "lote_salida_legacy_id",
            name="uq_scm_salida_legacy",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orden_operacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_orden_operacion.id", ondelete="CASCADE"),
        nullable=False,
    )
    corrida_fabricacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey("scm_corrida_fabricacion.id", ondelete="CASCADE"),
        nullable=True,
    )
    articulo_scm_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_articulo.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cantidad_por_ciclo_snapshot = db.Column(db.Numeric(12, 4), nullable=True)
    peso_unitario_snapshot_g = db.Column(db.Numeric(12, 4), nullable=True)
    cantidad_objetivo = db.Column(db.Numeric(15, 3), nullable=False)
    kg_estandar_objetivo = db.Column(db.Numeric(15, 6), nullable=True)
    excedente_objetivo = db.Column(
        db.Numeric(15, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    cantidad_real = db.Column(db.Numeric(15, 3), nullable=True)
    cantidad_rechazada = db.Column(db.Numeric(15, 3), nullable=True)
    lote_salida_legacy_id = db.Column(
        db.Integer,
        db.ForeignKey("lote_salida_pieza_color.id", ondelete="RESTRICT"),
        nullable=True,
    )

    orden_operacion = db.relationship(
        "ScmOrdenOperacion",
        back_populates="salidas",
    )
    corrida_fabricacion = db.relationship(
        "ScmCorridaFabricacion",
        back_populates="salidas",
    )
    articulo = db.relationship("ScmArticulo")
    asignaciones = db.relationship(
        "ScmAsignacionDemandaSuministro",
        back_populates="orden_operacion_salida",
        lazy="selectin",
    )


class ScmAsignacionDemandaSuministro(db.Model):
    """Relacion N:M cuantificada entre demanda de PT y una fuente."""

    __tablename__ = "scm_asignacion_demanda_suministro"
    __table_args__ = (
        db.CheckConstraint(
            "fuente_tipo IN ('STOCK', 'SALIDA_ORDEN')",
            name="ck_scm_asignacion_fuente_tipo",
        ),
        db.CheckConstraint(
            "(fuente_tipo = 'STOCK' "
            "AND lote_articulo_id IS NOT NULL "
            "AND orden_operacion_salida_id IS NULL) OR "
            "(fuente_tipo = 'SALIDA_ORDEN' "
            "AND orden_operacion_salida_id IS NOT NULL "
            "AND lote_articulo_id IS NULL)",
            name="ck_scm_asignacion_fuente_exclusiva",
        ),
        db.CheckConstraint(
            "cantidad_planificada >= 0 "
            "AND cantidad_comprometida >= 0 "
            "AND cantidad_satisfecha >= 0",
            name="ck_scm_asignacion_cantidades",
        ),
        db.CheckConstraint(
            "estado IN "
            "('PLANIFICADA', 'COMPROMETIDA', 'SATISFECHA', 'CANCELADA')",
            name="ck_scm_asignacion_estado",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_asignacion_version",
        ),
        db.UniqueConstraint(
            "operation_id",
            name="uq_scm_asignacion_operation",
        ),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    orden_produccion_linea_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_orden_produccion_linea.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    fuente_tipo = db.Column(db.String(24), nullable=False)
    orden_operacion_salida_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            "scm_orden_operacion_salida.id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    lote_articulo_id = db.Column(
        db.Integer,
        db.ForeignKey("scm_lote_articulo.id", ondelete="RESTRICT"),
        nullable=True,
    )
    cantidad_planificada = db.Column(
        db.Numeric(15, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    cantidad_comprometida = db.Column(
        db.Numeric(15, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    cantidad_satisfecha = db.Column(
        db.Numeric(15, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    estado = db.Column(
        db.String(24),
        nullable=False,
        default="PLANIFICADA",
        server_default="PLANIFICADA",
    )
    operation_id = db.Column(Uuid(as_uuid=True), nullable=True)
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
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

    orden_produccion_linea = db.relationship(
        "ScmOrdenProduccionLinea",
        back_populates="asignaciones",
    )
    orden_operacion_salida = db.relationship(
        "ScmOrdenOperacionSalida",
        back_populates="asignaciones",
    )
    lote_articulo = db.relationship("ScmLoteArticulo")
