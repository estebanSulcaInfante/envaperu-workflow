import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid
from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


PRODUCT_ONBOARDING_JSON = db.JSON().with_variant(JSONB(), "postgresql")


def utc_now():
    return datetime.now(timezone.utc)


SESSION_STATES = (
    "BORRADOR",
    "CON_BLOQUEOS",
    "LISTA_PARA_PUBLICAR",
    "FINALIZADA",
    "ABANDONADA",
)

ONBOARDING_STEPS = (
    "IDENTIDAD",
    "COMPONENTES",
    "COLORES",
    "ESTRUCTURA",
    "RUTA_EMPAQUE",
    "REVISION",
)


class ScmAltaProductoSesion(db.Model):
    """Borrador durable del alta integral de un ProductoTerminado.

    La sesion es un agregado de workflow. Sus JSON conservan captura y
    procedencia, pero nunca sustituyen a los maestros canonicos.
    """

    __tablename__ = "scm_alta_producto_sesion"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('BORRADOR', 'CON_BLOQUEOS', "
            "'LISTA_PARA_PUBLICAR', 'FINALIZADA', 'ABANDONADA')",
            name="ck_scm_alta_producto_estado",
        ),
        db.CheckConstraint(
            "paso_actual IN ('IDENTIDAD', 'COMPONENTES', 'COLORES', "
            "'ESTRUCTURA', 'RUTA_EMPAQUE', 'REVISION')",
            name="ck_scm_alta_producto_paso_actual",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_alta_producto_version",
        ),
        db.Index(
            "ix_scm_alta_producto_estado_actualizada",
            "estado",
            "updated_at",
        ),
        db.Index(
            "ix_scm_alta_producto_producto",
            "producto_terminado_id",
        ),
        db.Index(
            "ix_scm_alta_producto_creada_por",
            "creada_por_id",
        ),
        db.Index(
            "ix_scm_alta_producto_actualizada_por",
            "actualizada_por_id",
        ),
    )

    id = db.Column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    titulo = db.Column(db.Text, nullable=False)
    producto_terminado_id = db.Column(
        db.String(50),
        db.ForeignKey(
            "producto_terminado.cod_sku_pt",
            name="fk_scm_alta_producto_producto",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    estado = db.Column(
        db.String(32),
        nullable=False,
        default="BORRADOR",
        server_default="BORRADOR",
    )
    paso_actual = db.Column(
        db.String(32),
        nullable=False,
        default="IDENTIDAD",
        server_default="IDENTIDAD",
    )
    borrador_json = db.Column(
        PRODUCT_ONBOARDING_JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    estados_paso_json = db.Column(
        PRODUCT_ONBOARDING_JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    bloqueos_paso_json = db.Column(
        PRODUCT_ONBOARDING_JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    fuentes_json = db.Column(
        PRODUCT_ONBOARDING_JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    referencias_json = db.Column(
        PRODUCT_ONBOARDING_JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    readiness_json = db.Column(
        PRODUCT_ONBOARDING_JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    invalidated_steps_json = db.Column(
        PRODUCT_ONBOARDING_JSON,
        nullable=False,
        default=list,
        server_default="[]",
    )
    application_journal_json = db.Column(
        PRODUCT_ONBOARDING_JSON,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    creada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_alta_producto_creada_por",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    actualizada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_alta_producto_actualizada_por",
            ondelete="RESTRICT",
        ),
        nullable=False,
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
    finalizada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    abandonada_at = db.Column(db.DateTime(timezone=True), nullable=True)

    producto_terminado = db.relationship("ProductoTerminado")
    creada_por = db.relationship(
        "Trabajador",
        foreign_keys=[creada_por_id],
    )
    actualizada_por = db.relationship(
        "Trabajador",
        foreign_keys=[actualizada_por_id],
    )
