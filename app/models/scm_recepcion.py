from datetime import datetime, timezone

from app.extensions import db


TIPOS_DOCUMENTO_PROVEEDOR = ("GUIA_REMISION", "FACTURA", "OTRO")
ESTADO_RECEPCION_BORRADOR = "BORRADOR"
ESTADO_RECEPCION_CONFIRMADA = "CONFIRMADA"
ESTADO_RECEPCION_RECHAZADA = "RECHAZADA_PRE_CUSTODIA"
ESTADOS_RECEPCION = (
    ESTADO_RECEPCION_BORRADOR,
    ESTADO_RECEPCION_CONFIRMADA,
    ESTADO_RECEPCION_RECHAZADA,
)


def utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class ScmDocumentoProveedor(db.Model):
    __tablename__ = "scm_documento_proveedor"
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('GUIA_REMISION', 'FACTURA', 'OTRO')",
            name="ck_scm_documento_proveedor_tipo",
        ),
        db.CheckConstraint(
            "serie_normalizada = upper(trim(serie_normalizada)) "
            "AND length(serie_normalizada) > 0",
            name="ck_scm_documento_proveedor_serie_normalizada",
        ),
        db.CheckConstraint(
            "numero_normalizado = upper(trim(numero_normalizado)) "
            "AND length(numero_normalizado) > 0",
            name="ck_scm_documento_proveedor_numero_normalizado",
        ),
        db.CheckConstraint(
            "cantidad_total_documental_kg IS NULL "
            "OR cantidad_total_documental_kg > 0",
            name="ck_scm_documento_proveedor_cantidad_positiva",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_documento_proveedor_version",
        ),
        db.UniqueConstraint(
            "proveedor_id",
            "tipo",
            "serie_normalizada",
            "numero_normalizado",
            name="uq_scm_documento_proveedor_identidad",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    proveedor_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_proveedor.id",
            name="fk_scm_documento_proveedor_proveedor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    tipo = db.Column(db.String(24), nullable=False)
    serie_normalizada = db.Column(db.String(32), nullable=False)
    numero_normalizado = db.Column(db.String(64), nullable=False)
    fecha_emision = db.Column(db.Date, nullable=False)
    cantidad_total_documental_kg = db.Column(db.Numeric(15, 3), nullable=True)
    referencia = db.Column(db.String(128), nullable=True)
    observacion = db.Column(db.Text, nullable=True)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
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

    proveedor = db.relationship("ScmProveedor")
    recepciones = db.relationship(
        "ScmRecepcionDocumento",
        back_populates="documento",
        lazy="selectin",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "proveedor_id": self.proveedor_id,
            "tipo": self.tipo,
            "serie": self.serie_normalizada,
            "numero": self.numero_normalizado,
            "fecha_emision": self.fecha_emision.isoformat(),
            "cantidad_total_documental_kg": (
                str(self.cantidad_total_documental_kg)
                if self.cantidad_total_documental_kg is not None
                else None
            ),
            "referencia": self.referencia,
            "observacion": self.observacion,
            "version": self.version,
            "recepciones_count": len(self.recepciones),
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


class ScmRecepcion(db.Model):
    __tablename__ = "scm_recepcion"
    __table_args__ = (
        db.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_recepcion_codigo_normalizado",
        ),
        db.CheckConstraint(
            "estado IN ('BORRADOR', 'CONFIRMADA', 'RECHAZADA_PRE_CUSTODIA')",
            name="ck_scm_recepcion_estado",
        ),
        db.CheckConstraint(
            "estado <> 'CONFIRMADA' OR confirmada_at IS NOT NULL",
            name="ck_scm_recepcion_confirmacion_coherente",
        ),
        db.CheckConstraint("version > 0", name="ck_scm_recepcion_version"),
        db.UniqueConstraint("codigo", name="uq_scm_recepcion_codigo"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    proveedor_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_proveedor.id",
            name="fk_scm_recepcion_proveedor",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    estado = db.Column(
        db.String(30),
        nullable=False,
        default=ESTADO_RECEPCION_BORRADOR,
        server_default=ESTADO_RECEPCION_BORRADOR,
    )
    recibida_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_recepcion_recibida_por",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    confirmada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    observacion = db.Column(db.Text, nullable=True)
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
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

    proveedor = db.relationship("ScmProveedor")
    recibida_por = db.relationship("Trabajador")
    documentos = db.relationship(
        "ScmRecepcionDocumento",
        back_populates="recepcion",
        lazy="selectin",
        order_by="ScmRecepcionDocumento.documento_id",
    )
    lineas = db.relationship(
        "ScmRecepcionLinea",
        back_populates="recepcion",
        lazy="selectin",
        order_by="ScmRecepcionLinea.numero_linea",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "proveedor_id": self.proveedor_id,
            "estado": self.estado,
            "recibida_por_id": self.recibida_por_id,
            "confirmada_at": _isoformat(self.confirmada_at),
            "observacion": self.observacion,
            "version": self.version,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


class ScmRecepcionDocumento(db.Model):
    __tablename__ = "scm_recepcion_documento"

    recepcion_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_recepcion.id",
            name="fk_scm_recepcion_documento_recepcion",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    documento_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_documento_proveedor.id",
            name="fk_scm_recepcion_documento_documento",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )

    recepcion = db.relationship("ScmRecepcion", back_populates="documentos")
    documento = db.relationship("ScmDocumentoProveedor", back_populates="recepciones")


class ScmRecepcionLinea(db.Model):
    __tablename__ = "scm_recepcion_linea"
    __table_args__ = (
        db.CheckConstraint("numero_linea > 0", name="ck_scm_recepcion_linea_numero"),
        db.CheckConstraint(
            "modalidad IN ('VIRGEN_CONFIANZA_PROVEEDOR', 'SEGUNDA_PESAJE_BOLSA')",
            name="ck_scm_recepcion_linea_modalidad",
        ),
        db.CheckConstraint(
            "bultos_recibidos > 0",
            name="ck_scm_recepcion_linea_bultos_positivos",
        ),
        db.CheckConstraint(
            "cantidad_documental_kg IS NULL OR cantidad_documental_kg > 0",
            name="ck_scm_recepcion_linea_documental_positiva",
        ),
        db.CheckConstraint(
            "cantidad_medida_kg IS NULL OR cantidad_medida_kg > 0",
            name="ck_scm_recepcion_linea_medida_positiva",
        ),
        db.UniqueConstraint(
            "recepcion_id",
            "numero_linea",
            name="uq_scm_recepcion_linea_recepcion_numero",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    recepcion_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_recepcion.id",
            name="fk_scm_recepcion_linea_recepcion",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    numero_linea = db.Column(db.Integer, nullable=False)
    material_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_material.id",
            name="fk_scm_recepcion_linea_material",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    modalidad = db.Column(db.String(40), nullable=False)
    bultos_recibidos = db.Column(db.Integer, nullable=False)
    cantidad_documental_kg = db.Column(db.Numeric(15, 3), nullable=True)
    cantidad_medida_kg = db.Column(db.Numeric(15, 3), nullable=True)
    observacion = db.Column(db.Text, nullable=True)

    recepcion = db.relationship("ScmRecepcion", back_populates="lineas")
    material = db.relationship("ScmMaterial")
    pesajes = db.relationship(
        "ScmPesajeBolsa",
        back_populates="linea",
        lazy="selectin",
        order_by="ScmPesajeBolsa.secuencia",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "numero_linea": self.numero_linea,
            "material_id": self.material_id,
            "modalidad": self.modalidad,
            "bultos_recibidos": self.bultos_recibidos,
            "cantidad_documental_kg": (
                str(self.cantidad_documental_kg)
                if self.cantidad_documental_kg is not None
                else None
            ),
            "cantidad_medida_kg": (
                str(self.cantidad_medida_kg)
                if self.cantidad_medida_kg is not None
                else None
            ),
            "observacion": self.observacion,
            "pesajes_bolsa": [item.to_dict() for item in self.pesajes],
        }


class ScmPesajeBolsa(db.Model):
    __tablename__ = "scm_pesaje_bolsa"
    __table_args__ = (
        db.CheckConstraint("secuencia > 0", name="ck_scm_pesaje_bolsa_secuencia"),
        db.CheckConstraint("peso_kg > 0", name="ck_scm_pesaje_bolsa_peso_positivo"),
        db.UniqueConstraint(
            "recepcion_linea_id",
            "secuencia",
            name="uq_scm_pesaje_bolsa_linea_secuencia",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    recepcion_linea_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_recepcion_linea.id",
            name="fk_scm_pesaje_bolsa_linea",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    secuencia = db.Column(db.Integer, nullable=False)
    peso_kg = db.Column(db.Numeric(15, 3), nullable=False)
    balanza_codigo_snapshot = db.Column(db.String(64), nullable=True)
    registrado_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_pesaje_bolsa_registrado_por",
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

    linea = db.relationship("ScmRecepcionLinea", back_populates="pesajes")
    registrado_por = db.relationship("Trabajador")

    def to_dict(self):
        return {
            "id": self.id,
            "secuencia": self.secuencia,
            "peso_kg": str(self.peso_kg),
            "balanza_codigo_snapshot": self.balanza_codigo_snapshot,
            "registrado_por_id": self.registrado_por_id,
            "created_at": _isoformat(self.created_at),
        }
