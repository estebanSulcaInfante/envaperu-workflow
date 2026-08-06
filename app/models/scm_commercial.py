"""Presentaciones comerciales normalizadas de ProductoTerminado."""

from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ScmPresentacionComercial(db.Model):
    """Conversión comercial de una presentación hacia la unidad base SCM."""

    __tablename__ = "scm_presentacion_comercial"
    __table_args__ = (
        db.CheckConstraint(
            "unidades_base > 0",
            name="ck_scm_presentacion_comercial_unidades_base",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_presentacion_comercial_version",
        ),
        db.UniqueConstraint(
            "codigo",
            name="uq_scm_presentacion_comercial_codigo",
        ),
        db.UniqueConstraint(
            "producto_terminado_id",
            "nombre",
            name="uq_scm_presentacion_comercial_producto_nombre",
        ),
        db.Index(
            "uq_scm_presentacion_comercial_predeterminada_activa",
            "producto_terminado_id",
            unique=True,
            postgresql_where=db.text("predeterminada AND activo"),
            sqlite_where=db.text("predeterminada = 1 AND activo = 1"),
        ),
        db.Index(
            "uq_scm_presentacion_comercial_codigo_barra",
            "codigo_barra",
            unique=True,
            postgresql_where=db.text("codigo_barra IS NOT NULL"),
            sqlite_where=db.text("codigo_barra IS NOT NULL"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(32), nullable=False)
    producto_terminado_id = db.Column(
        db.String(50),
        db.ForeignKey("producto_terminado.cod_sku_pt", ondelete="RESTRICT"),
        nullable=False,
    )
    nombre = db.Column(db.String(100), nullable=False)
    unidades_base = db.Column(db.Integer, nullable=False)
    codigo_barra = db.Column(db.String(50), nullable=True)
    predeterminada = db.Column(
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

    producto_terminado = db.relationship(
        "ProductoTerminado",
        backref=db.backref("presentaciones_comerciales", lazy="selectin"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "producto_terminado_id": self.producto_terminado_id,
            "producto": (
                self.producto_terminado.producto
                if self.producto_terminado is not None
                else None
            ),
            "nombre": self.nombre,
            "unidades_base": self.unidades_base,
            "codigo_barra": self.codigo_barra,
            "predeterminada": self.predeterminada,
            "activo": self.activo,
            "version": self.version,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
