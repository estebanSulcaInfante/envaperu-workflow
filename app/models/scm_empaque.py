from datetime import datetime, timezone

from app.extensions import db


ESTADO_REGLA_BORRADOR = "BORRADOR"
ESTADO_REGLA_APROBADA = "APROBADA"
ESTADO_REGLA_RETIRADA = "RETIRADA"
ESTADOS_REGLA = (
    ESTADO_REGLA_BORRADOR,
    ESTADO_REGLA_APROBADA,
    ESTADO_REGLA_RETIRADA,
)

CLASES_CONTENEDOR = ("MANGA", "BOLSA", "JABA", "CAJA", "OTRO")


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


class ScmTipoContenedor(db.Model):
    __tablename__ = "scm_tipo_contenedor"
    __table_args__ = (
        db.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_tipo_contenedor_codigo",
        ),
        db.CheckConstraint(
            "clase IN ('MANGA', 'BOLSA', 'JABA', 'CAJA', 'OTRO')",
            name="ck_scm_tipo_contenedor_clase",
        ),
        db.CheckConstraint(
            "tara_nominal_g >= 0",
            name="ck_scm_tipo_contenedor_tara",
        ),
        db.CheckConstraint(
            "tolerancia_tara_g >= 0",
            name="ck_scm_tipo_contenedor_tolerancia",
        ),
        db.CheckConstraint(
            "peso_bruto_max_kg >= 0",
            name="ck_scm_tipo_contenedor_bruto",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_tipo_contenedor_version",
        ),
        db.UniqueConstraint(
            "codigo",
            name="uq_scm_tipo_contenedor_codigo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    clase = db.Column(db.String(32), nullable=False)
    nombre = db.Column(db.String(160), nullable=False)
    material = db.Column(db.String(120), nullable=True)
    dimensiones_json = db.Column(db.JSON, nullable=True)
    tara_nominal_g = db.Column(
        db.Numeric(12, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    tolerancia_tara_g = db.Column(
        db.Numeric(12, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    peso_bruto_max_kg = db.Column(
        db.Numeric(12, 3),
        nullable=False,
        default=0,
        server_default="0",
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

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "clase": self.clase,
            "nombre": self.nombre,
            "material": self.material,
            "dimensiones": self.dimensiones_json,
            "tara_nominal_g": _decimal_text(self.tara_nominal_g),
            "tolerancia_tara_g": _decimal_text(
                self.tolerancia_tara_g
            ),
            "peso_bruto_max_kg": _decimal_text(
                self.peso_bruto_max_kg
            ),
            "activo": self.activo,
            "version": self.version,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


class ScmPerfilEmpacable(db.Model):
    __tablename__ = "scm_perfil_empacable"
    __table_args__ = (
        db.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_perfil_empacable_codigo",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_perfil_empacable_version",
        ),
        db.UniqueConstraint(
            "codigo",
            name="uq_scm_perfil_empacable_codigo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(64), nullable=False)
    nombre = db.Column(db.String(160), nullable=False)
    descripcion_fisica = db.Column(db.Text, nullable=True)
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
            "descripcion_fisica": self.descripcion_fisica,
            "activo": self.activo,
            "version": self.version,
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
        }


class ScmArticuloPerfil(db.Model):
    __tablename__ = "scm_articulo_perfil"
    __table_args__ = (
        db.UniqueConstraint(
            "articulo_id",
            "perfil_empacable_id",
            name="uq_scm_articulo_perfil",
        ),
        db.Index(
            "ux_scm_articulo_perfil_predeterminado",
            "articulo_id",
            unique=True,
            postgresql_where=db.text(
                "activo AND es_predeterminado"
            ),
            sqlite_where=db.text(
                "activo = 1 AND es_predeterminado = 1"
            ),
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    articulo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_articulo.id",
            name="fk_scm_articulo_perfil_articulo",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    perfil_empacable_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_perfil_empacable.id",
            name="fk_scm_articulo_perfil_perfil",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    es_predeterminado = db.Column(
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

    articulo = db.relationship("ScmArticulo")
    perfil = db.relationship("ScmPerfilEmpacable")

    def to_dict(self):
        return {
            "id": self.id,
            "articulo_id": self.articulo_id,
            "perfil_empacable_id": self.perfil_empacable_id,
            "es_predeterminado": self.es_predeterminado,
            "activo": self.activo,
            "perfil": self.perfil.to_dict(),
        }


class ScmReglaEmpaque(db.Model):
    __tablename__ = "scm_regla_empaque"
    __table_args__ = (
        db.UniqueConstraint(
            "perfil_empacable_id",
            "tipo_contenedor_id",
            name="uq_scm_regla_empaque_combinacion",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    perfil_empacable_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_perfil_empacable.id",
            name="fk_scm_regla_empaque_perfil",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    tipo_contenedor_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_tipo_contenedor.id",
            name="fk_scm_regla_empaque_contenedor",
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

    perfil = db.relationship("ScmPerfilEmpacable")
    tipo_contenedor = db.relationship("ScmTipoContenedor")
    revisiones = db.relationship(
        "ScmReglaEmpaqueRevision",
        back_populates="regla",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ScmReglaEmpaqueRevision.numero_revision",
    )


class ScmReglaEmpaqueRevision(db.Model):
    __tablename__ = "scm_regla_empaque_revision"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('BORRADOR', 'APROBADA', 'RETIRADA')",
            name="ck_scm_regla_empaque_revision_estado",
        ),
        db.CheckConstraint(
            "numero_revision > 0",
            name="ck_scm_regla_empaque_revision_numero",
        ),
        db.CheckConstraint(
            "cantidad_objetivo_un > 0",
            name="ck_scm_regla_empaque_objetivo",
        ),
        db.CheckConstraint(
            "cantidad_maxima_probada_un > 0",
            name="ck_scm_regla_empaque_maxima",
        ),
        db.CheckConstraint(
            "cantidad_objetivo_un <= cantidad_maxima_probada_un",
            name="ck_scm_regla_empaque_objetivo_maxima",
        ),
        db.CheckConstraint(
            "peso_neto_operativo_max_kg > 0",
            name="ck_scm_regla_empaque_neto",
        ),
        db.CheckConstraint(
            "margen_seguridad_kg >= 0",
            name="ck_scm_regla_empaque_margen",
        ),
        db.CheckConstraint(
            "tolerancia_peso_abs_g >= 0",
            name="ck_scm_regla_empaque_tolerancia_abs",
        ),
        db.CheckConstraint(
            "tolerancia_peso_pct >= 0 "
            "AND tolerancia_peso_pct < 100",
            name="ck_scm_regla_empaque_tolerancia_pct",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_scm_regla_empaque_version",
        ),
        db.CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64",
            name="ck_scm_regla_empaque_hash",
        ),
        db.UniqueConstraint(
            "regla_id",
            "numero_revision",
            name="uq_scm_regla_empaque_revision",
        ),
        db.Index(
            "ux_scm_regla_empaque_aprobada",
            "regla_id",
            unique=True,
            postgresql_where=db.text("estado = 'APROBADA'"),
            sqlite_where=db.text("estado = 'APROBADA'"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    regla_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "scm_regla_empaque.id",
            name="fk_scm_regla_empaque_revision_regla",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    numero_revision = db.Column(db.Integer, nullable=False)
    estado = db.Column(
        db.String(32),
        nullable=False,
        default=ESTADO_REGLA_BORRADOR,
        server_default=ESTADO_REGLA_BORRADOR,
    )
    medicion_fisica_probada = db.Column(
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    )
    cantidad_objetivo_un = db.Column(db.Integer, nullable=False)
    cantidad_maxima_probada_un = db.Column(db.Integer, nullable=False)
    peso_neto_operativo_max_kg = db.Column(
        db.Numeric(12, 3),
        nullable=False,
    )
    margen_seguridad_kg = db.Column(
        db.Numeric(12, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    tolerancia_peso_abs_g = db.Column(
        db.Numeric(12, 3),
        nullable=False,
        default=0,
        server_default="0",
    )
    tolerancia_peso_pct = db.Column(
        db.Numeric(7, 4),
        nullable=False,
        default=0,
        server_default="0",
    )
    tara_nominal_g_snapshot = db.Column(
        db.Numeric(12, 3),
        nullable=True,
    )
    tolerancia_tara_g_snapshot = db.Column(
        db.Numeric(12, 3),
        nullable=True,
    )
    peso_bruto_max_kg_snapshot = db.Column(
        db.Numeric(12, 3),
        nullable=True,
    )
    notas = db.Column(db.Text, nullable=True)
    content_hash = db.Column(db.String(64), nullable=True)
    creada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_regla_empaque_revision_creador",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    aprobada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_regla_empaque_revision_aprobador",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    aprobada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    retirada_por_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "trabajador.id",
            name="fk_scm_regla_empaque_revision_retirador",
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

    regla = db.relationship(
        "ScmReglaEmpaque",
        back_populates="revisiones",
    )
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

    def to_dict(self):
        return {
            "revision_id": self.id,
            "regla_id": self.regla_id,
            "numero_revision": self.numero_revision,
            "estado": self.estado,
            "medicion_fisica_probada": self.medicion_fisica_probada,
            "cantidad_objetivo_un": self.cantidad_objetivo_un,
            "cantidad_maxima_probada_un": (
                self.cantidad_maxima_probada_un
            ),
            "peso_neto_operativo_max_kg": _decimal_text(
                self.peso_neto_operativo_max_kg
            ),
            "margen_seguridad_kg": _decimal_text(
                self.margen_seguridad_kg
            ),
            "tolerancia_peso_abs_g": _decimal_text(
                self.tolerancia_peso_abs_g
            ),
            "tolerancia_peso_pct": _decimal_text(
                self.tolerancia_peso_pct
            ),
            "tara_nominal_g_snapshot": _decimal_text(
                self.tara_nominal_g_snapshot
            ),
            "tolerancia_tara_g_snapshot": _decimal_text(
                self.tolerancia_tara_g_snapshot
            ),
            "peso_bruto_max_kg_snapshot": _decimal_text(
                self.peso_bruto_max_kg_snapshot
            ),
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
        }
