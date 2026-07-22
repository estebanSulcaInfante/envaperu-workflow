from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


MODALIDAD_VIRGEN = "VIRGEN_CONFIANZA_PROVEEDOR"
MODALIDAD_SEGUNDA = "SEGUNDA_PESAJE_BOLSA"
MODALIDAD_POR_CONFIGURAR = "POR_CONFIGURAR"
MODALIDADES_RECEPCION = (
    MODALIDAD_VIRGEN,
    MODALIDAD_SEGUNDA,
    MODALIDAD_POR_CONFIGURAR,
)

CLASE_MATERIA_PRIMA = "MATERIA_PRIMA"
CLASE_COLORANTE = "COLORANTE"
CLASES_MATERIAL = (
    CLASE_MATERIA_PRIMA,
    CLASE_COLORANTE,
)


class ScmProveedor(db.Model):
    __tablename__ = "scm_proveedor"
    __table_args__ = (
        db.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_proveedor_codigo_normalizado",
        ),
        db.CheckConstraint(
            "ruc IS NULL OR length(ruc) = 11",
            name="ck_scm_proveedor_ruc_longitud",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_proveedor_version",
        ),
        db.UniqueConstraint(
            "codigo",
            name="uq_scm_proveedor_codigo",
        ),
        db.UniqueConstraint(
            "ruc",
            name="uq_scm_proveedor_ruc",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    razon_social = db.Column(db.String(200), nullable=False)
    ruc = db.Column(db.String(11), nullable=True)
    activo = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
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

    ordenes_compra = db.relationship(
        "ScmOrdenCompra",
        back_populates="proveedor",
        lazy="selectin",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "razon_social": self.razon_social,
            "ruc": self.ruc,
            "activo": self.activo,
            "version": self.version,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


scm_rol_capacidad = db.Table(
    "scm_rol_capacidad",
    db.Column(
        "rol_operativo_id",
        db.Integer,
        db.ForeignKey(
            "rol_operativo.id",
            name="fk_scm_rol_capacidad_rol_operativo",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    ),
    db.Column(
        "capacidad_id",
        db.Integer,
        db.ForeignKey(
            "scm_capacidad.id",
            name="fk_scm_rol_capacidad_capacidad",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    ),
)


class ScmCategoriaRecepcion(db.Model):
    __tablename__ = "scm_categoria_recepcion"
    __table_args__ = (
        db.CheckConstraint(
            "modalidad_default IN "
            "('VIRGEN_CONFIANZA_PROVEEDOR', "
            "'SEGUNDA_PESAJE_BOLSA', 'POR_CONFIGURAR')",
            name="ck_scm_categoria_recepcion_modalidad",
        ),
        db.CheckConstraint(
            "NOT (modalidad_default = 'POR_CONFIGURAR' "
            "AND recepcion_habilitada)",
            name="ck_scm_categoria_recepcion_configurada",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_categoria_recepcion_version",
        ),
        db.UniqueConstraint(
            "codigo",
            name="uq_scm_categoria_recepcion_codigo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    modalidad_default = db.Column(db.String(40), nullable=False)
    lote_externo_obligatorio = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    recepcion_habilitada = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    activo = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    materiales = db.relationship(
        "ScmMaterial",
        back_populates="categoria_recepcion",
        lazy="selectin",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "nombre": self.nombre,
            "modalidad_default": self.modalidad_default,
            "lote_externo_obligatorio": self.lote_externo_obligatorio,
            "recepcion_habilitada": self.recepcion_habilitada,
            "activo": self.activo,
            "version": self.version,
        }


class ScmMaterial(db.Model):
    __tablename__ = "scm_material"
    __table_args__ = (
        db.CheckConstraint(
            "clase IN ('MATERIA_PRIMA', 'COLORANTE')",
            name="ck_scm_material_clase",
        ),
        db.CheckConstraint(
            "unidad_base = 'KG'",
            name="ck_scm_material_unidad_base",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_material_version",
        ),
        db.UniqueConstraint("codigo", name="uq_scm_material_codigo"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    clase = db.Column(db.String(30), nullable=False)
    categoria_recepcion_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_categoria_recepcion.id",
            name="fk_scm_material_categoria_recepcion",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    unidad_base = db.Column(
        db.String(10),
        nullable=False,
        default="KG",
        server_default="KG",
    )
    activo = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    categoria_recepcion = db.relationship(
        "ScmCategoriaRecepcion",
        back_populates="materiales",
    )
    materia_prima = db.relationship(
        "MateriaPrima",
        back_populates="scm_material",
        uselist=False,
    )
    colorante = db.relationship(
        "Colorante",
        back_populates="scm_material",
        uselist=False,
    )

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "nombre": self.nombre,
            "clase": self.clase,
            "categoria_recepcion_id": self.categoria_recepcion_id,
            "unidad_base": self.unidad_base,
            "activo": self.activo,
            "version": self.version,
        }


class ScmCapacidad(db.Model):
    __tablename__ = "scm_capacidad"
    __table_args__ = (
        db.UniqueConstraint("codigo", name="uq_scm_capacidad_codigo"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    nombre = db.Column(db.String(120), nullable=False)
    descripcion = db.Column(db.Text, nullable=True)
    activo = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )

    roles = db.relationship(
        "RolOperativo",
        secondary=scm_rol_capacidad,
        back_populates="capacidades",
        lazy="selectin",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "nombre": self.nombre,
            "descripcion": self.descripcion,
            "activo": self.activo,
        }
