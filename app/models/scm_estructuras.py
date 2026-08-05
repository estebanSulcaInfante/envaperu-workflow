from datetime import datetime, timezone

from app.extensions import db


ESTADO_ESTRUCTURA_BORRADOR = "BORRADOR"
ESTADO_ESTRUCTURA_PENDIENTE = "PENDIENTE_APROBACION"
ESTADO_ESTRUCTURA_APROBADA = "APROBADA"
ESTADO_ESTRUCTURA_RECHAZADA = "RECHAZADA"
ESTADO_ESTRUCTURA_RETIRADA = "RETIRADA"
ESTADO_ESTRUCTURA_DESCARTADA = "DESCARTADA"
ESTADOS_ESTRUCTURA = (
    ESTADO_ESTRUCTURA_BORRADOR,
    ESTADO_ESTRUCTURA_PENDIENTE,
    ESTADO_ESTRUCTURA_APROBADA,
    ESTADO_ESTRUCTURA_RECHAZADA,
    ESTADO_ESTRUCTURA_RETIRADA,
    ESTADO_ESTRUCTURA_DESCARTADA,
)


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


class ScmEstructuraRevision(db.Model):
    __tablename__ = "scm_estructura_revision"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN "
            "('BORRADOR', 'PENDIENTE_APROBACION', 'APROBADA', "
            "'RECHAZADA', 'RETIRADA', 'DESCARTADA')",
            name="ck_scm_estructura_revision_estado",
        ),
        db.CheckConstraint(
            "numero_revision > 0",
            name="ck_scm_estructura_revision_numero",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_estructura_revision_version",
        ),
        db.CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64",
            name="ck_scm_estructura_revision_hash",
        ),
        db.CheckConstraint(
            "(estado = 'RECHAZADA' AND rechazada_por_id IS NOT NULL "
            "AND rechazada_at IS NOT NULL AND motivo_rechazo IS NOT NULL) OR "
            "(estado <> 'RECHAZADA' AND rechazada_por_id IS NULL "
            "AND rechazada_at IS NULL AND motivo_rechazo IS NULL)",
            name="ck_scm_estructura_revision_rechazo",
        ),
        db.CheckConstraint(
            "(estado = 'DESCARTADA' AND descartada_por_id IS NOT NULL "
            "AND descartada_at IS NOT NULL AND motivo_descarte IS NOT NULL) OR "
            "(estado <> 'DESCARTADA' AND descartada_por_id IS NULL "
            "AND descartada_at IS NULL AND motivo_descarte IS NULL)",
            name="ck_scm_estructura_revision_descarte",
        ),
        db.UniqueConstraint(
            "articulo_resultado_id",
            "numero_revision",
            name="uq_scm_estructura_articulo_revision",
        ),
        db.Index(
            "ux_scm_estructura_aprobada_articulo",
            "articulo_resultado_id",
            unique=True,
            postgresql_where=db.text("estado = 'APROBADA'"),
            sqlite_where=db.text("estado = 'APROBADA'"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    articulo_resultado_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_articulo.id",
            name="fk_scm_estructura_revision_resultado",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    numero_revision = db.Column(db.Integer, nullable=False)
    estado = db.Column(
        db.String(32),
        nullable=False,
        default=ESTADO_ESTRUCTURA_BORRADOR,
        server_default=ESTADO_ESTRUCTURA_BORRADOR,
    )
    notas = db.Column(db.Text, nullable=True)
    content_hash = db.Column(db.String(64), nullable=True)
    creada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_estructura_revision_creador",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    enviada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    aprobada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_estructura_revision_aprobador",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    aprobada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    rechazada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_estructura_revision_rechazador",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    rechazada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    motivo_rechazo = db.Column(db.String(500), nullable=True)
    retirada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_estructura_revision_retirador",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    retirada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    descartada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_estructura_revision_descartador",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    descartada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    motivo_descarte = db.Column(db.String(500), nullable=True)
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

    articulo_resultado = db.relationship("ScmArticulo")
    creada_por = db.relationship(
        "Trabajador",
        foreign_keys=[creada_por_id],
    )
    aprobada_por = db.relationship(
        "Trabajador",
        foreign_keys=[aprobada_por_id],
    )
    rechazada_por = db.relationship(
        "Trabajador",
        foreign_keys=[rechazada_por_id],
    )
    retirada_por = db.relationship(
        "Trabajador",
        foreign_keys=[retirada_por_id],
    )
    descartada_por = db.relationship(
        "Trabajador",
        foreign_keys=[descartada_por_id],
    )
    componentes = db.relationship(
        "ScmEstructuraComponente",
        back_populates="revision",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ScmEstructuraComponente.secuencia",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "articulo_resultado_id": self.articulo_resultado_id,
            "numero_revision": self.numero_revision,
            "estado": self.estado,
            "notas": self.notas,
            "content_hash": self.content_hash,
            "creada_por_id": self.creada_por_id,
            "enviada_at": _isoformat(self.enviada_at),
            "aprobada_por_id": self.aprobada_por_id,
            "aprobada_at": _isoformat(self.aprobada_at),
            "rechazada_por_id": self.rechazada_por_id,
            "rechazada_at": _isoformat(self.rechazada_at),
            "motivo_rechazo": self.motivo_rechazo,
            "retirada_por_id": self.retirada_por_id,
            "retirada_at": _isoformat(self.retirada_at),
            "descartada_por_id": self.descartada_por_id,
            "descartada_at": _isoformat(self.descartada_at),
            "motivo_descarte": self.motivo_descarte,
            "version": self.version,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
            "componentes": [
                component.to_dict() for component in self.componentes
            ],
        }


class ScmEstructuraComponente(db.Model):
    __tablename__ = "scm_estructura_componente"
    __table_args__ = (
        db.CheckConstraint(
            "secuencia > 0",
            name="ck_scm_estructura_componente_secuencia",
        ),
        db.CheckConstraint(
            "cantidad > 0",
            name="ck_scm_estructura_componente_cantidad",
        ),
        db.CheckConstraint(
            "unidad = 'UN'",
            name="ck_scm_estructura_componente_unidad",
        ),
        db.CheckConstraint(
            "cantidad = CAST(cantidad AS INTEGER)",
            name="ck_scm_estructura_componente_cantidad_discreta",
        ),
        db.CheckConstraint(
            "merma_tecnica_pct IS NULL OR "
            "(merma_tecnica_pct >= 0 AND merma_tecnica_pct < 100)",
            name="ck_scm_estructura_componente_merma",
        ),
        db.UniqueConstraint(
            "revision_id",
            "secuencia",
            name="uq_scm_estructura_componente_secuencia",
        ),
        db.UniqueConstraint(
            "revision_id",
            "articulo_componente_id",
            name="uq_scm_estructura_componente_articulo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    revision_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_estructura_revision.id",
            name="fk_scm_estructura_componente_revision",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    secuencia = db.Column(db.Integer, nullable=False)
    articulo_componente_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_articulo.id",
            name="fk_scm_estructura_componente_articulo",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    cantidad = db.Column(db.Numeric(15, 6), nullable=False)
    unidad = db.Column(
        db.String(10),
        nullable=False,
        default="UN",
        server_default="UN",
    )
    merma_tecnica_pct = db.Column(db.Numeric(7, 4), nullable=True)

    revision = db.relationship(
        "ScmEstructuraRevision",
        back_populates="componentes",
    )
    articulo_componente = db.relationship("ScmArticulo")

    def to_dict(self):
        return {
            "id": self.id,
            "secuencia": self.secuencia,
            "articulo_id": self.articulo_componente_id,
            "cantidad": _decimal_text(self.cantidad),
            "unidad": self.unidad,
            "merma_tecnica_pct": _decimal_text(self.merma_tecnica_pct),
        }
