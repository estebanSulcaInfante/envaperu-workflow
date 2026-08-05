import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid, event, select

from app.extensions import db


CLASE_PIEZA_COLOR = "PIEZA_COLOR"
CLASE_SUBENSAMBLE_WIP = "SUBENSAMBLE_WIP"
CLASE_PRODUCTO_TERMINADO = "PRODUCTO_TERMINADO"
CLASES_ARTICULO = (
    CLASE_PIEZA_COLOR,
    CLASE_SUBENSAMBLE_WIP,
    CLASE_PRODUCTO_TERMINADO,
)


def utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class ScmArticulo(db.Model):
    __tablename__ = "scm_articulo"
    __table_args__ = (
        db.CheckConstraint(
            "clase IN "
            "('PIEZA_COLOR', 'SUBENSAMBLE_WIP', 'PRODUCTO_TERMINADO')",
            name="ck_scm_articulo_clase",
        ),
        db.CheckConstraint(
            "unidad_base = 'UN'",
            name="ck_scm_articulo_unidad_base",
        ),
        db.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_articulo_codigo_normalizado",
        ),
        db.CheckConstraint("version > 0", name="ck_scm_articulo_version"),
        db.UniqueConstraint("public_id", name="uq_scm_articulo_public_id"),
        db.UniqueConstraint("codigo", name="uq_scm_articulo_codigo"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(
        Uuid(as_uuid=True),
        nullable=False,
        default=uuid.uuid4,
    )
    codigo = db.Column(db.String(64), nullable=False)
    nombre = db.Column(db.String(200), nullable=False)
    clase = db.Column(db.String(32), nullable=False)
    unidad_base = db.Column(
        db.String(10),
        nullable=False,
        default="UN",
        server_default="UN",
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

    pieza_color = db.relationship(
        "ScmArticuloPiezaColor",
        back_populates="articulo",
        uselist=False,
        cascade="all, delete-orphan",
    )
    definicion_wip = db.relationship(
        "ScmDefinicionWip",
        back_populates="articulo",
        uselist=False,
        cascade="all, delete-orphan",
    )
    producto = db.relationship(
        "ScmArticuloProducto",
        back_populates="articulo",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        subtype = None
        if self.pieza_color is not None:
            subtype = {
                "pieza_color_sku": self.pieza_color.pieza_color_sku,
            }
        elif self.definicion_wip is not None:
            subtype = {
                "descripcion": self.definicion_wip.descripcion,
                "requiere_calidad": self.definicion_wip.requiere_calidad,
            }
        elif self.producto is not None:
            subtype = {
                "producto_terminado_id": (
                    self.producto.producto_terminado_id
                ),
            }
        return {
            "id": self.id,
            "public_id": str(self.public_id) if self.public_id else None,
            "codigo": self.codigo,
            "nombre": self.nombre,
            "clase": self.clase,
            "unidad_base": self.unidad_base,
            "activo": self.activo,
            "version": self.version,
            "wip": subtype if self.clase == CLASE_SUBENSAMBLE_WIP else None,
            "subtipo": subtype,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


class ScmArticuloPiezaColor(db.Model):
    __tablename__ = "scm_articulo_pieza_color"
    __table_args__ = (
        db.UniqueConstraint(
            "pieza_color_sku",
            name="uq_scm_articulo_pieza_color_sku",
        ),
    )

    articulo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_articulo.id",
            name="fk_scm_articulo_pieza_articulo",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    pieza_color_sku = db.Column(
        db.String(50),
        db.ForeignKey(
            "pieza_color.sku",
            name="fk_scm_articulo_pieza_pieza_color",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    articulo = db.relationship("ScmArticulo", back_populates="pieza_color")
    pieza_color = db.relationship("PiezaColor")


class ScmDefinicionWip(db.Model):
    __tablename__ = "scm_definicion_wip"

    articulo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_articulo.id",
            name="fk_scm_definicion_wip_articulo",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    descripcion = db.Column(db.Text, nullable=True)
    requiere_calidad = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
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

    articulo = db.relationship("ScmArticulo", back_populates="definicion_wip")


class ScmArticuloProducto(db.Model):
    __tablename__ = "scm_articulo_producto"
    __table_args__ = (
        db.UniqueConstraint(
            "producto_terminado_id",
            name="uq_scm_articulo_producto_producto",
        ),
    )

    articulo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_articulo.id",
            name="fk_scm_articulo_producto_articulo",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    producto_terminado_id = db.Column(
        db.String(50),
        db.ForeignKey(
            "producto_terminado.cod_sku_pt",
            name="fk_scm_articulo_producto_producto",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    articulo = db.relationship("ScmArticulo", back_populates="producto")
    producto_terminado = db.relationship("ProductoTerminado")


def _dual_write_catalog_article(
    connection,
    *,
    code,
    name,
    article_class,
    subtype_table,
    subtype_column,
):
    article_table = ScmArticulo.__table__
    existing = connection.execute(
        select(article_table.c.id, article_table.c.clase).where(
            article_table.c.codigo == code
        )
    ).one_or_none()
    if existing is None:
        article_id = connection.execute(
            article_table.insert()
            .values(
                public_id=uuid.uuid4(),
                codigo=code,
                nombre=_normalized_catalog_name(name, code),
                clase=article_class,
                unidad_base="UN",
                activo=True,
                version=1,
            )
            .returning(article_table.c.id)
        ).scalar_one()
    else:
        article_id, existing_class = existing
        if existing_class != article_class:
            raise ValueError(
                "ARTICLE_SUBTYPE_MISMATCH: catalog code already belongs "
                f"to {existing_class}"
            )

    linked_article_id = connection.execute(
        select(subtype_table.c.articulo_id).where(
            subtype_column == code
        )
    ).scalar_one_or_none()
    if linked_article_id is None:
        connection.execute(subtype_table.insert().values(
            articulo_id=article_id,
            **{subtype_column.name: code},
        ))
    elif linked_article_id != article_id:
        raise ValueError(
            "ARTICLE_SUBTYPE_MISMATCH: catalog row already linked"
        )


def _normalized_catalog_name(value, fallback):
    normalized = str(value or "").strip()
    return normalized or fallback


def _piece_article_after_insert(_mapper, connection, target):
    _dual_write_catalog_article(
        connection,
        code=target.sku,
        name=target.piezas,
        article_class=CLASE_PIEZA_COLOR,
        subtype_table=ScmArticuloPiezaColor.__table__,
        subtype_column=ScmArticuloPiezaColor.__table__.c.pieza_color_sku,
    )


def _product_article_after_insert(_mapper, connection, target):
    _dual_write_catalog_article(
        connection,
        code=target.cod_sku_pt,
        name=target.producto,
        article_class=CLASE_PRODUCTO_TERMINADO,
        subtype_table=ScmArticuloProducto.__table__,
        subtype_column=ScmArticuloProducto.__table__.c.producto_terminado_id,
    )


# Import tardío para evitar que los modelos legacy dependan del supertipo SCM.
from app.models.producto import PiezaColor, ProductoTerminado  # noqa: E402


event.listen(PiezaColor, "after_insert", _piece_article_after_insert)
event.listen(
    ProductoTerminado,
    "after_insert",
    _product_article_after_insert,
)
