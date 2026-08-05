from datetime import datetime, timezone

from app.extensions import db


ESTADO_RUTA_BORRADOR = "BORRADOR"
ESTADO_RUTA_APROBADA = "APROBADA"
ESTADO_RUTA_RETIRADA = "RETIRADA"
ESTADOS_RUTA = (
    ESTADO_RUTA_BORRADOR,
    ESTADO_RUTA_APROBADA,
    ESTADO_RUTA_RETIRADA,
)

EXECUTOR_OP_OT = "OP_OT"
EXECUTOR_ORDEN_OPERACION = "ORDEN_OPERACION"
EXECUTOR_KINDS = (EXECUTOR_OP_OT, EXECUTOR_ORDEN_OPERACION)

TIPOS_OPERACION = (
    "INYECCION",
    "PREARMADO",
    "ENSAMBLE",
    "ACABADO",
    "EMPAQUE",
)


def utc_now():
    return datetime.now(timezone.utc)


def _isoformat(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class ScmCentroTrabajo(db.Model):
    __tablename__ = "scm_centro_trabajo"
    __table_args__ = (
        db.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_centro_trabajo_codigo",
        ),
        db.CheckConstraint(
            "tipo IN "
            "('INYECCION', 'PREARMADO', 'ENSAMBLE', "
            "'ACABADO', 'EMPAQUE')",
            name="ck_scm_centro_trabajo_tipo",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_centro_trabajo_version",
        ),
        db.UniqueConstraint(
            "codigo",
            name="uq_scm_centro_trabajo_codigo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    nombre = db.Column(db.String(160), nullable=False)
    tipo = db.Column(db.String(32), nullable=False)
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

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "nombre": self.nombre,
            "tipo": self.tipo,
            "activo": self.activo,
            "version": self.version,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


class ScmRutaRevision(db.Model):
    __tablename__ = "scm_ruta_revision"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('BORRADOR', 'APROBADA', 'RETIRADA')",
            name="ck_scm_ruta_revision_estado",
        ),
        db.CheckConstraint(
            "numero_revision > 0",
            name="ck_scm_ruta_revision_numero",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_ruta_revision_version",
        ),
        db.CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64",
            name="ck_scm_ruta_revision_hash",
        ),
        db.UniqueConstraint(
            "articulo_objetivo_id",
            "numero_revision",
            name="uq_scm_ruta_articulo_revision",
        ),
        db.Index(
            "ux_scm_ruta_aprobada_articulo",
            "articulo_objetivo_id",
            unique=True,
            postgresql_where=db.text("estado = 'APROBADA'"),
            sqlite_where=db.text("estado = 'APROBADA'"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    articulo_objetivo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_articulo.id",
            name="fk_scm_ruta_revision_objetivo",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    numero_revision = db.Column(db.Integer, nullable=False)
    estado = db.Column(
        db.String(32),
        nullable=False,
        default=ESTADO_RUTA_BORRADOR,
        server_default=ESTADO_RUTA_BORRADOR,
    )
    notas = db.Column(db.Text, nullable=True)
    content_hash = db.Column(db.String(64), nullable=True)
    creada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_ruta_revision_creador",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    aprobada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_ruta_revision_aprobador",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    aprobada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    retirada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_ruta_revision_retirador",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    retirada_at = db.Column(db.DateTime(timezone=True), nullable=True)
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

    articulo_objetivo = db.relationship("ScmArticulo")
    creada_por = db.relationship(
        "Trabajador",
        foreign_keys=[creada_por_id],
    )
    aprobada_por = db.relationship(
        "Trabajador",
        foreign_keys=[aprobada_por_id],
    )
    retirada_por = db.relationship(
        "Trabajador",
        foreign_keys=[retirada_por_id],
    )
    operaciones = db.relationship(
        "ScmOperacionRuta",
        back_populates="ruta",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ScmOperacionRuta.secuencia_visible",
    )
    precedencias = db.relationship(
        "ScmOperacionPrecedencia",
        back_populates="ruta",
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="ScmOperacionPrecedencia.ruta_id",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "articulo_objetivo_id": self.articulo_objetivo_id,
            "numero_revision": self.numero_revision,
            "estado": self.estado,
            "notas": self.notas,
            "content_hash": self.content_hash,
            "creada_por_id": self.creada_por_id,
            "aprobada_por_id": self.aprobada_por_id,
            "aprobada_at": _isoformat(self.aprobada_at),
            "retirada_por_id": self.retirada_por_id,
            "retirada_at": _isoformat(self.retirada_at),
            "version": self.version,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
            "operaciones": [
                operation.to_dict() for operation in self.operaciones
            ],
            "precedencias": [
                edge.to_dict() for edge in self.precedencias
            ],
        }


class ScmOperacionRuta(db.Model):
    __tablename__ = "scm_operacion_ruta"
    __table_args__ = (
        db.CheckConstraint(
            "clave = upper(trim(clave)) AND length(clave) > 0",
            name="ck_scm_operacion_ruta_clave",
        ),
        db.CheckConstraint(
            "secuencia_visible > 0",
            name="ck_scm_operacion_ruta_secuencia",
        ),
        db.CheckConstraint(
            "tipo IN "
            "('INYECCION', 'PREARMADO', 'ENSAMBLE', "
            "'ACABADO', 'EMPAQUE')",
            name="ck_scm_operacion_ruta_tipo",
        ),
        db.CheckConstraint(
            "executor_kind IN ('OP_OT', 'ORDEN_OPERACION')",
            name="ck_scm_operacion_ruta_executor",
        ),
        db.CheckConstraint(
            "(executor_kind = 'OP_OT' "
            "AND estructura_revision_id IS NULL) OR "
            "(executor_kind = 'ORDEN_OPERACION' "
            "AND estructura_revision_id IS NOT NULL)",
            name="ck_scm_operacion_ruta_estructura_executor",
        ),
        db.UniqueConstraint(
            "ruta_id",
            "clave",
            name="uq_scm_operacion_ruta_clave",
        ),
        db.UniqueConstraint(
            "ruta_id",
            "secuencia_visible",
            name="uq_scm_operacion_ruta_secuencia",
        ),
        db.UniqueConstraint(
            "ruta_id",
            "id",
            name="uq_scm_operacion_ruta_parent_id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ruta_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_ruta_revision.id",
            name="fk_scm_operacion_ruta_revision",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    clave = db.Column(db.String(64), nullable=False)
    secuencia_visible = db.Column(db.Integer, nullable=False)
    nombre = db.Column(db.String(160), nullable=False)
    tipo = db.Column(db.String(32), nullable=False)
    executor_kind = db.Column(db.String(32), nullable=False)
    centro_trabajo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_centro_trabajo.id",
            name="fk_scm_operacion_ruta_centro",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    articulo_salida_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_articulo.id",
            name="fk_scm_operacion_ruta_salida",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    estructura_revision_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_estructura_revision.id",
            name="fk_scm_operacion_ruta_estructura",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    permite_concurrente = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )

    ruta = db.relationship("ScmRutaRevision", back_populates="operaciones")
    centro_trabajo = db.relationship("ScmCentroTrabajo")
    articulo_salida = db.relationship("ScmArticulo")
    estructura_revision = db.relationship("ScmEstructuraRevision")

    def to_dict(self):
        return {
            "id": self.id,
            "clave": self.clave,
            "secuencia_visible": self.secuencia_visible,
            "nombre": self.nombre,
            "tipo": self.tipo,
            "executor_kind": self.executor_kind,
            "centro_trabajo_id": self.centro_trabajo_id,
            "articulo_salida_id": self.articulo_salida_id,
            "estructura_revision_id": self.estructura_revision_id,
            "permite_concurrente": self.permite_concurrente,
        }


class ScmOperacionPrecedencia(db.Model):
    __tablename__ = "scm_operacion_precedencia"
    __table_args__ = (
        db.CheckConstraint(
            "operacion_anterior_id <> operacion_siguiente_id",
            name="ck_scm_operacion_precedencia_no_self",
        ),
        db.ForeignKeyConstraint(
            ["ruta_id", "operacion_anterior_id"],
            ["scm_operacion_ruta.ruta_id", "scm_operacion_ruta.id"],
            name="fk_scm_precedencia_anterior_misma_ruta",
            ondelete="CASCADE",
        ),
        db.ForeignKeyConstraint(
            ["ruta_id", "operacion_siguiente_id"],
            ["scm_operacion_ruta.ruta_id", "scm_operacion_ruta.id"],
            name="fk_scm_precedencia_siguiente_misma_ruta",
            ondelete="CASCADE",
        ),
        db.UniqueConstraint(
            "ruta_id",
            "operacion_anterior_id",
            "operacion_siguiente_id",
            name="uq_scm_operacion_precedencia_arista",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    ruta_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_ruta_revision.id",
            name="fk_scm_operacion_precedencia_ruta",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    operacion_anterior_id = db.Column(db.Integer, nullable=False)
    operacion_siguiente_id = db.Column(db.Integer, nullable=False)

    ruta = db.relationship(
        "ScmRutaRevision",
        back_populates="precedencias",
        foreign_keys=[ruta_id],
    )
    anterior = db.relationship(
        "ScmOperacionRuta",
        foreign_keys=[operacion_anterior_id],
    )
    siguiente = db.relationship(
        "ScmOperacionRuta",
        foreign_keys=[operacion_siguiente_id],
    )

    def to_dict(self):
        return {
            "id": self.id,
            "anterior_id": self.operacion_anterior_id,
            "siguiente_id": self.operacion_siguiente_id,
            "anterior_clave": self.anterior.clave,
            "siguiente_clave": self.siguiente.clave,
        }
