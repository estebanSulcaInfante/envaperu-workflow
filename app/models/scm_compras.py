from datetime import datetime, timezone

from app.extensions import db


ESTADO_OC_ACTIVA = "ACTIVA"
ESTADO_OC_CERRADA = "CERRADA"
ESTADO_OC_ANULADA = "ANULADA"
ESTADOS_ORDEN_COMPRA = (
    ESTADO_OC_ACTIVA,
    ESTADO_OC_CERRADA,
    ESTADO_OC_ANULADA,
)

ESTADO_REVISION_BORRADOR = "BORRADOR"
ESTADO_REVISION_PENDIENTE_APROBACION = "PENDIENTE_APROBACION"
ESTADO_REVISION_APROBADA = "APROBADA"
ESTADO_REVISION_RECHAZADA = "RECHAZADA"
ESTADO_REVISION_SUPERADA = "SUPERADA"
ESTADOS_ORDEN_COMPRA_REVISION = (
    ESTADO_REVISION_BORRADOR,
    ESTADO_REVISION_PENDIENTE_APROBACION,
    ESTADO_REVISION_APROBADA,
    ESTADO_REVISION_RECHAZADA,
    ESTADO_REVISION_SUPERADA,
)


def utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class ScmOrdenCompra(db.Model):
    __tablename__ = "scm_orden_compra"
    __table_args__ = (
        db.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_orden_compra_codigo_normalizado",
        ),
        db.CheckConstraint(
            "estado IN ('ACTIVA', 'CERRADA', 'ANULADA')",
            name="ck_scm_orden_compra_estado",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_orden_compra_version",
        ),
        db.UniqueConstraint(
            "codigo",
            name="uq_scm_orden_compra_codigo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    proveedor_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_proveedor.id",
            name="fk_scm_orden_compra_proveedor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    estado = db.Column(
        db.String(20),
        nullable=False,
        default=ESTADO_OC_ACTIVA,
        server_default=ESTADO_OC_ACTIVA,
    )
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

    proveedor = db.relationship(
        "ScmProveedor",
        back_populates="ordenes_compra",
    )
    revisiones = db.relationship(
        "ScmOrdenCompraRevision",
        back_populates="orden",
        lazy="selectin",
        order_by="ScmOrdenCompraRevision.numero",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "proveedor_id": self.proveedor_id,
            "estado": self.estado,
            "version": self.version,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


class ScmOrdenCompraRevision(db.Model):
    __tablename__ = "scm_orden_compra_revision"
    __table_args__ = (
        db.CheckConstraint(
            "numero > 0",
            name="ck_scm_orden_compra_revision_numero",
        ),
        db.CheckConstraint(
            "estado IN ('BORRADOR', 'PENDIENTE_APROBACION', "
            "'APROBADA', 'RECHAZADA', 'SUPERADA')",
            name="ck_scm_orden_compra_revision_estado",
        ),
        db.CheckConstraint(
            "aprobada_por_id IS NULL OR aprobada_por_id <> creada_por_id",
            name="ck_scm_orden_compra_revision_actores_distintos",
        ),
        db.CheckConstraint(
            "estado NOT IN ('PENDIENTE_APROBACION', 'APROBADA', "
            "'SUPERADA') OR enviada_at IS NOT NULL",
            name="ck_scm_orden_compra_revision_envio_coherente",
        ),
        db.CheckConstraint(
            "estado NOT IN ('APROBADA', 'SUPERADA') OR "
            "(aprobada_por_id IS NOT NULL AND aprobada_at IS NOT NULL)",
            name="ck_scm_orden_compra_revision_aprobacion_coherente",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_orden_compra_revision_version",
        ),
        db.UniqueConstraint(
            "orden_id",
            "numero",
            name="uq_scm_orden_compra_revision_orden_numero",
        ),
        db.Index(
            "ux_scm_oc_revision_aprobada_orden",
            "orden_id",
            unique=True,
            postgresql_where=db.text("estado = 'APROBADA'"),
            sqlite_where=db.text("estado = 'APROBADA'"),
        ),
        db.Index(
            "ux_scm_oc_revision_abierta_orden",
            "orden_id",
            unique=True,
            postgresql_where=db.text(
                "estado IN ('BORRADOR', 'PENDIENTE_APROBACION')"
            ),
            sqlite_where=db.text(
                "estado IN ('BORRADOR', 'PENDIENTE_APROBACION')"
            ),
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    orden_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_orden_compra.id",
            name="fk_scm_orden_compra_revision_orden",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    numero = db.Column(db.Integer, nullable=False)
    estado = db.Column(
        db.String(30),
        nullable=False,
        default=ESTADO_REVISION_BORRADOR,
        server_default=ESTADO_REVISION_BORRADOR,
    )
    creada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_orden_compra_revision_creada_por",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    enviada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    aprobada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_orden_compra_revision_aprobada_por",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    aprobada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    motivo_rechazo = db.Column(db.Text, nullable=True)
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

    orden = db.relationship(
        "ScmOrdenCompra",
        back_populates="revisiones",
    )
    creador = db.relationship(
        "Trabajador",
        foreign_keys=[creada_por_id],
    )
    aprobador = db.relationship(
        "Trabajador",
        foreign_keys=[aprobada_por_id],
    )
    lineas = db.relationship(
        "ScmOrdenCompraLinea",
        back_populates="revision",
        lazy="selectin",
        order_by="ScmOrdenCompraLinea.numero_linea",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "orden_id": self.orden_id,
            "numero": self.numero,
            "estado": self.estado,
            "creada_por_id": self.creada_por_id,
            "enviada_at": _isoformat(self.enviada_at),
            "aprobada_por_id": self.aprobada_por_id,
            "aprobada_at": _isoformat(self.aprobada_at),
            "motivo_rechazo": self.motivo_rechazo,
            "version": self.version,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


class ScmOrdenCompraLinea(db.Model):
    __tablename__ = "scm_orden_compra_linea"
    __table_args__ = (
        db.CheckConstraint(
            "numero_linea > 0",
            name="ck_scm_orden_compra_linea_numero",
        ),
        db.CheckConstraint(
            "cantidad_autorizada_kg > 0",
            name="ck_scm_orden_compra_linea_cantidad_positiva",
        ),
        db.UniqueConstraint(
            "revision_id",
            "numero_linea",
            name="uq_scm_orden_compra_linea_revision_numero",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    revision_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_orden_compra_revision.id",
            name="fk_scm_orden_compra_linea_revision",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    numero_linea = db.Column(db.Integer, nullable=False)
    material_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_material.id",
            name="fk_scm_orden_compra_linea_material",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    cantidad_autorizada_kg = db.Column(
        db.Numeric(15, 3),
        nullable=False,
    )
    fecha_requerida = db.Column(db.Date, nullable=True)
    observacion = db.Column(db.Text, nullable=True)

    revision = db.relationship(
        "ScmOrdenCompraRevision",
        back_populates="lineas",
    )
    material = db.relationship("ScmMaterial")

    def to_dict(self):
        return {
            "id": self.id,
            "revision_id": self.revision_id,
            "numero_linea": self.numero_linea,
            "material_id": self.material_id,
            "cantidad_autorizada_kg": (
                str(self.cantidad_autorizada_kg)
                if self.cantidad_autorizada_kg is not None
                else None
            ),
            "fecha_requerida": (
                self.fecha_requerida.isoformat()
                if self.fecha_requerida
                else None
            ),
            "observacion": self.observacion,
        }
