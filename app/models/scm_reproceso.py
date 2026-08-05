"""Modelo trazable de merma, molienda, material recuperado y alertas SCM."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


def iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class ScmFamiliaMaterialReproceso(db.Model):
    __tablename__ = "scm_familia_material_reproceso"
    __table_args__ = (
        db.UniqueConstraint("codigo", name="uq_scm_familia_material_rep_codigo"),
        db.CheckConstraint("version > 0", name="ck_scm_familia_material_rep_version"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(40), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    descripcion = db.Column(db.Text)
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    def to_dict(self):
        return {
            "id": self.id, "codigo": self.codigo, "nombre": self.nombre,
            "descripcion": self.descripcion, "activo": self.activo,
            "version": self.version, "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


class ScmProcesoMaterialReproceso(db.Model):
    __tablename__ = "scm_proceso_material_reproceso"
    __table_args__ = (
        db.UniqueConstraint("codigo", name="uq_scm_proceso_material_rep_codigo"),
        db.CheckConstraint("version > 0", name="ck_scm_proceso_material_rep_version"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(40), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    descripcion = db.Column(db.Text)
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    def to_dict(self):
        return {
            "id": self.id, "codigo": self.codigo, "nombre": self.nombre,
            "descripcion": self.descripcion, "activo": self.activo,
            "version": self.version, "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }


class ScmCondicionMerma(db.Model):
    __tablename__ = "scm_condicion_merma"
    __table_args__ = (
        db.UniqueConstraint("codigo", name="uq_scm_condicion_merma_codigo"),
        db.CheckConstraint("version > 0", name="ck_scm_condicion_merma_version"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(40), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    recuperable = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    descripcion = db.Column(db.Text)
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    def to_dict(self):
        return {
            "id": self.id, "codigo": self.codigo, "nombre": self.nombre,
            "recuperable": self.recuperable, "descripcion": self.descripcion,
            "activo": self.activo, "version": self.version,
            "created_at": iso(self.created_at), "updated_at": iso(self.updated_at),
        }


class ScmReglaCompatibilidadReproceso(db.Model):
    __tablename__ = "scm_regla_compatibilidad_reproceso"
    __table_args__ = (
        db.UniqueConstraint("codigo", "revision", name="uq_scm_regla_rep_codigo_revision"),
        db.CheckConstraint("revision > 0", name="ck_scm_regla_rep_revision"),
        db.CheckConstraint("estado IN ('BORRADOR','APROBADA','RETIRADA')", name="ck_scm_regla_rep_estado"),
        db.CheckConstraint("resultado IN ('COMPATIBLE','CONDICIONADA','INCOMPATIBLE')", name="ck_scm_regla_rep_resultado"),
        db.CheckConstraint("porcentaje_maximo IS NULL OR (porcentaje_maximo > 0 AND porcentaje_maximo <= 100)", name="ck_scm_regla_rep_porcentaje"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    revision = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    nombre = db.Column(db.String(160), nullable=False)
    estado = db.Column(db.String(20), nullable=False, default="BORRADOR", server_default="BORRADOR")
    familia_objetivo_id = db.Column(db.Integer, db.ForeignKey("scm_familia_material_reproceso.id", ondelete="RESTRICT"), nullable=False)
    proceso_objetivo_id = db.Column(db.Integer, db.ForeignKey("scm_proceso_material_reproceso.id", ondelete="RESTRICT"), nullable=False)
    familia_aporte_id = db.Column(db.Integer, db.ForeignKey("scm_familia_material_reproceso.id", ondelete="RESTRICT"), nullable=False)
    proceso_aporte_id = db.Column(db.Integer, db.ForeignKey("scm_proceso_material_reproceso.id", ondelete="RESTRICT"), nullable=False)
    color_objetivo_id = db.Column(db.Integer, db.ForeignKey("color_base.id", ondelete="RESTRICT"))
    familia_color_objetivo_id = db.Column(db.Integer, db.ForeignKey("familia_color.id", ondelete="RESTRICT"))
    color_aporte_id = db.Column(db.Integer, db.ForeignKey("color_base.id", ondelete="RESTRICT"))
    familia_color_aporte_id = db.Column(db.Integer, db.ForeignKey("familia_color.id", ondelete="RESTRICT"))
    resultado = db.Column(db.String(20), nullable=False)
    porcentaje_maximo = db.Column(db.Numeric(5, 2))
    simetrica = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    notas = db.Column(db.Text)
    creado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    aprobado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    approved_at = db.Column(db.DateTime(timezone=True))

    familia_objetivo = db.relationship("ScmFamiliaMaterialReproceso", foreign_keys=[familia_objetivo_id])
    familia_aporte = db.relationship("ScmFamiliaMaterialReproceso", foreign_keys=[familia_aporte_id])
    proceso_objetivo = db.relationship("ScmProcesoMaterialReproceso", foreign_keys=[proceso_objetivo_id])
    proceso_aporte = db.relationship("ScmProcesoMaterialReproceso", foreign_keys=[proceso_aporte_id])

    def to_dict(self):
        return {
            "id": self.id, "codigo": self.codigo, "revision": self.revision,
            "nombre": self.nombre, "estado": self.estado,
            "familia_objetivo_id": self.familia_objetivo_id,
            "proceso_objetivo_id": self.proceso_objetivo_id,
            "familia_aporte_id": self.familia_aporte_id,
            "proceso_aporte_id": self.proceso_aporte_id,
            "color_objetivo_id": self.color_objetivo_id,
            "familia_color_objetivo_id": self.familia_color_objetivo_id,
            "color_aporte_id": self.color_aporte_id,
            "familia_color_aporte_id": self.familia_color_aporte_id,
            "resultado": self.resultado,
            "porcentaje_maximo": format(self.porcentaje_maximo, "f") if self.porcentaje_maximo is not None else None,
            "simetrica": self.simetrica, "notas": self.notas,
            "creado_por_id": self.creado_por_id, "aprobado_por_id": self.aprobado_por_id,
            "created_at": iso(self.created_at), "approved_at": iso(self.approved_at),
        }


class ScmLoteMermaRecuperable(db.Model):
    __tablename__ = "scm_lote_merma_recuperable"
    __table_args__ = (
        db.UniqueConstraint("codigo", name="uq_scm_lote_merma_codigo"),
        db.CheckConstraint("estado IN ('ALMACENADA','RESERVADA','CONSUMIDA','BLOQUEADA','ANULADA')", name="ck_scm_lote_merma_estado"),
        db.CheckConstraint("peso_neto_almacen_kg > 0", name="ck_scm_lote_merma_peso"),
        db.CheckConstraint("saldo_disponible_kg >= 0 AND saldo_reservado_kg >= 0 AND saldo_reservado_kg <= saldo_disponible_kg", name="ck_scm_lote_merma_saldo"),
        db.CheckConstraint("version > 0", name="ck_scm_lote_merma_version"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(64), nullable=False)
    familia_material_id = db.Column(db.Integer, db.ForeignKey("scm_familia_material_reproceso.id", ondelete="RESTRICT"), nullable=False)
    proceso_origen_id = db.Column(db.Integer, db.ForeignKey("scm_proceso_material_reproceso.id", ondelete="RESTRICT"), nullable=False)
    condicion_id = db.Column(db.Integer, db.ForeignKey("scm_condicion_merma.id", ondelete="RESTRICT"), nullable=False)
    color_id = db.Column(db.Integer, db.ForeignKey("color_base.id", ondelete="RESTRICT"), nullable=False)
    familia_color_id = db.Column(db.Integer, db.ForeignKey("familia_color.id", ondelete="RESTRICT"))
    material_id = db.Column(db.Integer, db.ForeignKey("scm_material.id", ondelete="RESTRICT"))
    ubicacion_id = db.Column(db.Integer, db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"), nullable=False)
    origen_tipo = db.Column(db.String(40), nullable=False)
    origen_id = db.Column(db.String(64), nullable=False)
    estado = db.Column(db.String(20), nullable=False, default="ALMACENADA", server_default="ALMACENADA")
    peso_bruto_almacen_kg = db.Column(db.Numeric(15, 3), nullable=False)
    tara_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    peso_neto_almacen_kg = db.Column(db.Numeric(15, 3), nullable=False)
    saldo_disponible_kg = db.Column(db.Numeric(15, 3), nullable=False)
    saldo_reservado_kg = db.Column(db.Numeric(15, 3), nullable=False, default=0, server_default="0")
    observaciones = db.Column(db.Text)
    pesado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    pesado_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    familia_material = db.relationship("ScmFamiliaMaterialReproceso")
    proceso_origen = db.relationship("ScmProcesoMaterialReproceso")
    condicion = db.relationship("ScmCondicionMerma")
    ubicacion = db.relationship("ScmUbicacionInventario")
    movimientos = db.relationship("ScmMovimientoMerma", back_populates="lote", lazy="selectin")

    def to_dict(self):
        return {
            "id": str(self.id), "codigo": self.codigo, "estado": self.estado,
            "familia_material_id": self.familia_material_id,
            "familia_material": self.familia_material.nombre if self.familia_material else None,
            "proceso_origen_id": self.proceso_origen_id,
            "proceso_origen": self.proceso_origen.nombre if self.proceso_origen else None,
            "condicion_id": self.condicion_id,
            "condicion": self.condicion.nombre if self.condicion else None,
            "color_id": self.color_id, "familia_color_id": self.familia_color_id,
            "material_id": self.material_id,
            "ubicacion": self.ubicacion.to_dict() if self.ubicacion else None,
            "origen_tipo": self.origen_tipo, "origen_id": self.origen_id,
            "peso_bruto_almacen_kg": format(self.peso_bruto_almacen_kg, "f"),
            "tara_kg": format(self.tara_kg, "f"),
            "peso_neto_almacen_kg": format(self.peso_neto_almacen_kg, "f"),
            "saldo_disponible_kg": format(self.saldo_disponible_kg, "f"),
            "saldo_reservado_kg": format(self.saldo_reservado_kg, "f"),
            "saldo_libre_kg": format(self.saldo_disponible_kg - self.saldo_reservado_kg, "f"),
            "observaciones": self.observaciones, "pesado_por_id": self.pesado_por_id,
            "pesado_at": iso(self.pesado_at), "version": self.version,
            "created_at": iso(self.created_at), "updated_at": iso(self.updated_at),
        }


class ScmMovimientoMerma(db.Model):
    __tablename__ = "scm_movimiento_merma"
    __table_args__ = (
        db.CheckConstraint("tipo IN ('INGRESO_ALMACEN','RESERVA_MOLIENDA','LIBERACION_RESERVA','CONSUMO_MOLIENDA','AJUSTE')", name="ck_scm_movimiento_merma_tipo"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lote_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_lote_merma_recuperable.id", ondelete="RESTRICT"), nullable=False)
    tipo = db.Column(db.String(30), nullable=False)
    cantidad_delta_kg = db.Column(db.Numeric(15, 3), nullable=False)
    saldo_resultante_kg = db.Column(db.Numeric(15, 3), nullable=False)
    referencia_tipo = db.Column(db.String(40))
    referencia_id = db.Column(db.String(64))
    motivo = db.Column(db.Text, nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    operation_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_operacion.operation_id", ondelete="RESTRICT"), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    lote = db.relationship("ScmLoteMermaRecuperable", back_populates="movimientos")

    def to_dict(self):
        return {
            "id": str(self.id), "lote_id": str(self.lote_id), "tipo": self.tipo,
            "cantidad_delta_kg": format(self.cantidad_delta_kg, "f"),
            "saldo_resultante_kg": format(self.saldo_resultante_kg, "f"),
            "referencia_tipo": self.referencia_tipo, "referencia_id": self.referencia_id,
            "motivo": self.motivo, "actor_id": self.actor_id,
            "operation_id": str(self.operation_id), "created_at": iso(self.created_at),
        }


class ScmOrdenMolienda(db.Model):
    __tablename__ = "scm_orden_molienda"
    __table_args__ = (
        db.UniqueConstraint("codigo", name="uq_scm_orden_molienda_codigo"),
        db.CheckConstraint("estado IN ('BORRADOR','VALIDADA','BLOQUEADA_COMPATIBILIDAD','EN_EJECUCION','CERRADA','ANULADA')", name="ck_scm_orden_molienda_estado"),
        db.CheckConstraint("version > 0", name="ck_scm_orden_molienda_version"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(64), nullable=False)
    estado = db.Column(db.String(32), nullable=False, default="BORRADOR", server_default="BORRADOR")
    familia_objetivo_id = db.Column(db.Integer, db.ForeignKey("scm_familia_material_reproceso.id", ondelete="RESTRICT"), nullable=False)
    proceso_objetivo_id = db.Column(db.Integer, db.ForeignKey("scm_proceso_material_reproceso.id", ondelete="RESTRICT"), nullable=False)
    color_objetivo_id = db.Column(db.Integer, db.ForeignKey("color_base.id", ondelete="RESTRICT"), nullable=False)
    familia_color_objetivo_id = db.Column(db.Integer, db.ForeignKey("familia_color.id", ondelete="RESTRICT"))
    material_salida_id = db.Column(db.Integer, db.ForeignKey("scm_material.id", ondelete="RESTRICT"), nullable=False)
    tolerancia_custodia_kg = db.Column(db.Numeric(15, 3), nullable=False, default=1, server_default="1")
    tolerancia_balance_kg = db.Column(db.Numeric(15, 3))
    entrada_real_kg = db.Column(db.Numeric(15, 3))
    salida_real_kg = db.Column(db.Numeric(15, 3))
    perdida_real_kg = db.Column(db.Numeric(15, 3))
    diferencia_balance_kg = db.Column(db.Numeric(15, 3))
    mezcla_excepcional = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    excepcion_motivo = db.Column(db.Text)
    excepcion_aprobada_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"))
    creado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    ejecutado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"))
    cerrado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"))
    notas = db.Column(db.Text)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    validated_at = db.Column(db.DateTime(timezone=True))
    started_at = db.Column(db.DateTime(timezone=True))
    closed_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())

    aportes = db.relationship("ScmOrdenMoliendaAporte", back_populates="orden", lazy="selectin")
    lotes_salida = db.relationship("ScmLoteMaterialRecuperado", back_populates="orden", lazy="selectin")

    def to_dict(self, include_lines=True):
        payload = {
            "id": str(self.id), "codigo": self.codigo, "estado": self.estado,
            "familia_objetivo_id": self.familia_objetivo_id,
            "proceso_objetivo_id": self.proceso_objetivo_id,
            "color_objetivo_id": self.color_objetivo_id,
            "familia_color_objetivo_id": self.familia_color_objetivo_id,
            "material_salida_id": self.material_salida_id,
            "tolerancia_custodia_kg": format(self.tolerancia_custodia_kg, "f"),
            "tolerancia_balance_kg": format(self.tolerancia_balance_kg, "f") if self.tolerancia_balance_kg is not None else None,
            "entrada_real_kg": format(self.entrada_real_kg, "f") if self.entrada_real_kg is not None else None,
            "salida_real_kg": format(self.salida_real_kg, "f") if self.salida_real_kg is not None else None,
            "perdida_real_kg": format(self.perdida_real_kg, "f") if self.perdida_real_kg is not None else None,
            "diferencia_balance_kg": format(self.diferencia_balance_kg, "f") if self.diferencia_balance_kg is not None else None,
            "mezcla_excepcional": self.mezcla_excepcional,
            "excepcion_motivo": self.excepcion_motivo,
            "excepcion_aprobada_por_id": self.excepcion_aprobada_por_id,
            "creado_por_id": self.creado_por_id, "ejecutado_por_id": self.ejecutado_por_id,
            "cerrado_por_id": self.cerrado_por_id, "notas": self.notas,
            "version": self.version, "created_at": iso(self.created_at),
            "validated_at": iso(self.validated_at), "started_at": iso(self.started_at),
            "closed_at": iso(self.closed_at), "updated_at": iso(self.updated_at),
        }
        if include_lines:
            payload["aportes"] = [item.to_dict() for item in self.aportes]
            payload["lotes_salida"] = [item.to_dict() for item in self.lotes_salida]
        return payload


class ScmOrdenMoliendaAporte(db.Model):
    __tablename__ = "scm_orden_molienda_aporte"
    __table_args__ = (
        db.UniqueConstraint("orden_id", "lote_merma_id", name="uq_scm_orden_molienda_aporte_lote"),
        db.CheckConstraint("cantidad_planificada_kg > 0", name="ck_scm_aporte_planificado"),
        db.CheckConstraint("peso_pre_molino_kg IS NULL OR peso_pre_molino_kg > 0", name="ck_scm_aporte_pre_molino"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    orden_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_orden_molienda.id", ondelete="RESTRICT"), nullable=False)
    lote_merma_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_lote_merma_recuperable.id", ondelete="RESTRICT"), nullable=False)
    cantidad_planificada_kg = db.Column(db.Numeric(15, 3), nullable=False)
    peso_pre_molino_kg = db.Column(db.Numeric(15, 3))
    diferencia_custodia_kg = db.Column(db.Numeric(15, 3))
    porcentaje_real = db.Column(db.Numeric(7, 4))
    resultado_compatibilidad = db.Column(db.String(20))
    regla_revision_id = db.Column(db.Integer, db.ForeignKey("scm_regla_compatibilidad_reproceso.id", ondelete="RESTRICT"))
    regla_snapshot = db.Column(db.JSON)
    excede_tolerancia = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    motivo_diferencia = db.Column(db.Text)
    autorizado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"))
    pesado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"))
    pesado_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    orden = db.relationship("ScmOrdenMolienda", back_populates="aportes")
    lote_merma = db.relationship("ScmLoteMermaRecuperable")
    regla_revision = db.relationship("ScmReglaCompatibilidadReproceso")

    def to_dict(self):
        return {
            "id": self.id, "orden_id": str(self.orden_id),
            "lote_merma_id": str(self.lote_merma_id),
            "lote_codigo": self.lote_merma.codigo if self.lote_merma else None,
            "cantidad_planificada_kg": format(self.cantidad_planificada_kg, "f"),
            "peso_pre_molino_kg": format(self.peso_pre_molino_kg, "f") if self.peso_pre_molino_kg is not None else None,
            "diferencia_custodia_kg": format(self.diferencia_custodia_kg, "f") if self.diferencia_custodia_kg is not None else None,
            "porcentaje_real": format(self.porcentaje_real, "f") if self.porcentaje_real is not None else None,
            "resultado_compatibilidad": self.resultado_compatibilidad,
            "regla_revision_id": self.regla_revision_id,
            "regla_snapshot": self.regla_snapshot,
            "excede_tolerancia": self.excede_tolerancia,
            "motivo_diferencia": self.motivo_diferencia,
            "autorizado_por_id": self.autorizado_por_id,
            "pesado_por_id": self.pesado_por_id, "pesado_at": iso(self.pesado_at),
        }


class ScmLoteMaterialRecuperado(db.Model):
    __tablename__ = "scm_lote_material_recuperado"
    __table_args__ = (
        db.UniqueConstraint("codigo", name="uq_scm_lote_material_recuperado_codigo"),
        db.CheckConstraint("estado IN ('PENDIENTE_LIBERACION','DISPONIBLE','BLOQUEADO','ANULADO')", name="ck_scm_lote_material_recuperado_estado"),
        db.CheckConstraint("peso_neto_kg > 0", name="ck_scm_lote_material_recuperado_peso"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    codigo = db.Column(db.String(64), nullable=False)
    orden_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_orden_molienda.id", ondelete="RESTRICT"), nullable=False)
    material_id = db.Column(db.Integer, db.ForeignKey("scm_material.id", ondelete="RESTRICT"), nullable=False)
    ubicacion_id = db.Column(db.Integer, db.ForeignKey("scm_ubicacion_inventario.id", ondelete="RESTRICT"), nullable=False)
    estado = db.Column(db.String(30), nullable=False, default="PENDIENTE_LIBERACION", server_default="PENDIENTE_LIBERACION")
    peso_neto_kg = db.Column(db.Numeric(15, 3), nullable=False)
    saldo_disponible_kg = db.Column(db.Numeric(15, 3), nullable=False)
    composicion_snapshot = db.Column(db.JSON, nullable=False)
    mezcla_excepcional = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    producido_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    liberado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"))
    producido_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    liberado_at = db.Column(db.DateTime(timezone=True))

    orden = db.relationship("ScmOrdenMolienda", back_populates="lotes_salida")
    ubicacion = db.relationship("ScmUbicacionInventario")

    def to_dict(self):
        return {
            "id": str(self.id), "codigo": self.codigo, "orden_id": str(self.orden_id),
            "material_id": self.material_id,
            "ubicacion": self.ubicacion.to_dict() if self.ubicacion else None,
            "estado": self.estado, "peso_neto_kg": format(self.peso_neto_kg, "f"),
            "saldo_disponible_kg": format(self.saldo_disponible_kg, "f"),
            "composicion_snapshot": self.composicion_snapshot,
            "mezcla_excepcional": self.mezcla_excepcional,
            "producido_por_id": self.producido_por_id,
            "liberado_por_id": self.liberado_por_id,
            "producido_at": iso(self.producido_at), "liberado_at": iso(self.liberado_at),
        }


class ScmReglaAlerta(db.Model):
    __tablename__ = "scm_regla_alerta"
    __table_args__ = (db.UniqueConstraint("codigo", name="uq_scm_regla_alerta_codigo"),)

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    nombre = db.Column(db.String(160), nullable=False)
    descripcion = db.Column(db.Text)
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    revisiones = db.relationship("ScmReglaAlertaRevision", back_populates="regla", lazy="selectin")

    def to_dict(self):
        approved = [item for item in self.revisiones if item.estado == "APROBADA"]
        current = max(approved, key=lambda item: item.revision) if approved else None
        return {
            "id": self.id, "codigo": self.codigo, "nombre": self.nombre,
            "descripcion": self.descripcion, "activo": self.activo,
            "revision_actual": current.to_dict() if current else None,
            "revisiones": [item.to_dict() for item in sorted(self.revisiones, key=lambda item: item.revision, reverse=True)],
        }


class ScmReglaAlertaRevision(db.Model):
    __tablename__ = "scm_regla_alerta_revision"
    __table_args__ = (
        db.UniqueConstraint("regla_id", "revision", name="uq_scm_regla_alerta_revision"),
        db.CheckConstraint("estado IN ('BORRADOR','APROBADA','RETIRADA')", name="ck_scm_regla_alerta_revision_estado"),
        db.CheckConstraint("severidad IN ('INFO','ADVERTENCIA','CRITICA')", name="ck_scm_regla_alerta_revision_severidad"),
        db.CheckConstraint("unidad IN ('HORAS','DIAS_CALENDARIO','KG','PORCENTAJE')", name="ck_scm_regla_alerta_revision_unidad"),
        db.CheckConstraint("umbral > 0", name="ck_scm_regla_alerta_revision_umbral"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    regla_id = db.Column(db.Integer, db.ForeignKey("scm_regla_alerta.id", ondelete="RESTRICT"), nullable=False)
    revision = db.Column(db.Integer, nullable=False)
    estado = db.Column(db.String(20), nullable=False, default="BORRADOR", server_default="BORRADOR")
    umbral = db.Column(db.Numeric(15, 3), nullable=False)
    unidad = db.Column(db.String(24), nullable=False)
    severidad = db.Column(db.String(20), nullable=False)
    alcance = db.Column(db.String(40), nullable=False, default="PRODUCCION", server_default="PRODUCCION")
    creado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    aprobado_por_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    approved_at = db.Column(db.DateTime(timezone=True))

    regla = db.relationship("ScmReglaAlerta", back_populates="revisiones")

    def to_dict(self):
        return {
            "id": self.id, "regla_id": self.regla_id, "revision": self.revision,
            "estado": self.estado, "umbral": format(self.umbral, "f"),
            "unidad": self.unidad, "severidad": self.severidad,
            "alcance": self.alcance, "creado_por_id": self.creado_por_id,
            "aprobado_por_id": self.aprobado_por_id,
            "created_at": iso(self.created_at), "approved_at": iso(self.approved_at),
        }


class ScmAlertaOperativa(db.Model):
    __tablename__ = "scm_alerta_operativa"
    __table_args__ = (
        db.UniqueConstraint("huella", name="uq_scm_alerta_operativa_huella"),
        db.CheckConstraint("estado IN ('ABIERTA','RECONOCIDA','RESUELTA','DESCARTADA')", name="ck_scm_alerta_operativa_estado"),
        db.CheckConstraint("severidad IN ('INFO','ADVERTENCIA','CRITICA')", name="ck_scm_alerta_operativa_severidad"),
    )

    id = db.Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    regla_revision_id = db.Column(db.Integer, db.ForeignKey("scm_regla_alerta_revision.id", ondelete="RESTRICT"), nullable=False)
    huella = db.Column(db.String(64), nullable=False)
    tipo = db.Column(db.String(64), nullable=False)
    agregado_tipo = db.Column(db.String(64), nullable=False)
    agregado_id = db.Column(db.String(64), nullable=False)
    estado = db.Column(db.String(20), nullable=False, default="ABIERTA", server_default="ABIERTA")
    severidad = db.Column(db.String(20), nullable=False)
    resumen = db.Column(db.String(240), nullable=False)
    detalle = db.Column(db.JSON, nullable=False)
    asignada_a_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"))
    detectada_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    reconocida_at = db.Column(db.DateTime(timezone=True))
    cerrada_at = db.Column(db.DateTime(timezone=True))
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now, server_default=db.func.now())
    eventos = db.relationship("ScmAlertaEvento", back_populates="alerta", lazy="selectin")

    def to_dict(self):
        return {
            "id": str(self.id), "regla_revision_id": self.regla_revision_id,
            "huella": self.huella, "tipo": self.tipo,
            "agregado_tipo": self.agregado_tipo, "agregado_id": self.agregado_id,
            "estado": self.estado, "severidad": self.severidad,
            "resumen": self.resumen, "detalle": self.detalle,
            "asignada_a_id": self.asignada_a_id,
            "detectada_at": iso(self.detectada_at), "reconocida_at": iso(self.reconocida_at),
            "cerrada_at": iso(self.cerrada_at), "updated_at": iso(self.updated_at),
            "eventos": [item.to_dict() for item in self.eventos],
        }


class ScmAlertaEvento(db.Model):
    __tablename__ = "scm_alerta_evento"
    __table_args__ = (
        db.CheckConstraint("tipo IN ('CREADA','RECONOCIDA','ASIGNADA','RESUELTA','DESCARTADA')", name="ck_scm_alerta_evento_tipo"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    alerta_id = db.Column(Uuid(as_uuid=True), db.ForeignKey("scm_alerta_operativa.id", ondelete="RESTRICT"), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("trabajador.id", ondelete="RESTRICT"), nullable=False)
    motivo = db.Column(db.Text)
    detalle = db.Column(db.JSON)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())
    alerta = db.relationship("ScmAlertaOperativa", back_populates="eventos")

    def to_dict(self):
        return {
            "id": self.id, "tipo": self.tipo, "actor_id": self.actor_id,
            "motivo": self.motivo, "detalle": self.detalle,
            "created_at": iso(self.created_at),
        }
