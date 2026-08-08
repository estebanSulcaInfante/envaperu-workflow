from datetime import datetime, timezone

from app.extensions import db
from app.models.scm_catalogos import scm_rol_capacidad


def utc_now():
    return datetime.now(timezone.utc)

# Tabla intermedia N:M para relacionar trabajadores con múltiples roles
trabajador_rol = db.Table('trabajador_rol',
    db.Column('trabajador_id', db.Integer, db.ForeignKey('trabajador.id'), primary_key=True),
    db.Column('rol_operativo_id', db.Integer, db.ForeignKey('rol_operativo.id'), primary_key=True),
    db.Column(
        'es_principal',
        db.Boolean,
        nullable=False,
        default=False,
        server_default=db.false(),
    ),
)
db.Index(
    'uq_trabajador_rol_principal',
    trabajador_rol.c.trabajador_id,
    unique=True,
    postgresql_where=trabajador_rol.c.es_principal.is_(True),
    sqlite_where=trabajador_rol.c.es_principal.is_(True),
)

class RolOperativo(db.Model):
    """
    Catálogo de roles operativos que puede tener un trabajador.
    Ejemplo: MAQUINISTA, OPERADOR_PESAJE, MEZCLADOR, SUPERVISOR.
    """
    __tablename__ = 'rol_operativo'
    __table_args__ = (
        db.CheckConstraint(
            'version > 0',
            name='ck_rol_operativo_workspace_version',
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    activo = db.Column(db.Boolean, default=True)
    workspace_focus = db.Column(db.Text, nullable=True)
    workspace_start_feature = db.Column(db.String(80), nullable=True)
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default='1',
    )
    capacidades = db.relationship(
        'ScmCapacidad',
        secondary=scm_rol_capacidad,
        back_populates='roles',
        lazy='selectin',
    )
    workspace_preferencias = db.relationship(
        'ScmRolWorkspacePreferencia',
        back_populates='rol_operativo',
        cascade='all, delete-orphan',
        lazy='selectin',
        order_by=lambda: (
            ScmRolWorkspacePreferencia.prioridad,
            ScmRolWorkspacePreferencia.feature_key,
        ),
    )

    def workspace_identity_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombre': self.nombre,
            'activo': self.activo,
            'workspace_focus': self.workspace_focus,
            'workspace_start_feature': self.workspace_start_feature,
            'workspace_preferencias': [
                preference.to_dict()
                for preference in self.workspace_preferencias
            ],
        }

    def to_dict(self):
        effective_capability_codes = sorted(
            capacidad.codigo
            for capacidad in self.capacidades
            if capacidad.activo
        )
        assigned_capability_codes = sorted(
            capacidad.codigo for capacidad in self.capacidades
        )
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombre': self.nombre,
            'activo': self.activo,
            # `capacidades` se conserva para clientes anteriores a N2.
            'capacidades': effective_capability_codes,
            'capacidad_codigos': assigned_capability_codes,
            'workspace_focus': self.workspace_focus,
            'workspace_start_feature': self.workspace_start_feature,
            'workspace_preferencias': [
                preference.to_dict()
                for preference in self.workspace_preferencias
            ],
            'version': self.version,
        }


class ScmRolWorkspacePreferencia(db.Model):
    __tablename__ = 'scm_rol_workspace_preferencia'
    __table_args__ = (
        db.CheckConstraint(
            'prioridad BETWEEN 0 AND 999',
            name='ck_scm_rol_workspace_preferencia_prioridad',
        ),
        db.Index(
            'ix_scm_rol_workspace_preferencia_orden',
            'rol_operativo_id',
            'fijada',
            'prioridad',
            'feature_key',
        ),
        db.Index(
            'ix_scm_rol_workspace_preferencia_created_by',
            'created_by_id',
        ),
        db.Index(
            'ix_scm_rol_workspace_preferencia_updated_by',
            'updated_by_id',
        ),
    )

    rol_operativo_id = db.Column(
        db.Integer,
        db.ForeignKey(
            'rol_operativo.id',
            name='fk_scm_rol_workspace_preferencia_rol',
            ondelete='RESTRICT',
        ),
        primary_key=True,
    )
    feature_key = db.Column(db.String(80), primary_key=True)
    prioridad = db.Column(db.SmallInteger, nullable=False)
    fijada = db.Column(
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
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey(
            'trabajador.id',
            name='fk_scm_rol_workspace_preferencia_creador',
            ondelete='RESTRICT',
        ),
        nullable=False,
    )
    updated_by_id = db.Column(
        db.Integer,
        db.ForeignKey(
            'trabajador.id',
            name='fk_scm_rol_workspace_preferencia_actualizador',
            ondelete='RESTRICT',
        ),
        nullable=False,
    )

    rol_operativo = db.relationship(
        'RolOperativo',
        back_populates='workspace_preferencias',
    )

    def to_dict(self):
        return {
            'feature_key': self.feature_key,
            'prioridad': self.prioridad,
            'fijada': self.fijada,
        }

class Trabajador(db.Model):
    """
    Entidad de catálogo maestra para los operadores de planta.
    Sustituye al texto libre y unifica en un solo catálogo.
    """
    __tablename__ = 'trabajador'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False) # ej TR-001
    nombres = db.Column(db.String(100), nullable=False)
    apellidos = db.Column(db.String(100), nullable=False)
    nombre_corto = db.Column(db.String(100), nullable=True)
    activo = db.Column(db.Boolean, default=True)
    observaciones = db.Column(db.Text, nullable=True)
    auth_user_id = db.Column(db.Uuid(as_uuid=True), unique=True, nullable=True)

    # Relación N:M
    roles = db.relationship('RolOperativo', secondary=trabajador_rol, lazy='subquery',
        backref=db.backref('trabajadores', lazy=True))
    rol_principal = db.relationship(
        'RolOperativo',
        secondary=trabajador_rol,
        primaryjoin=lambda: db.and_(
            Trabajador.id == trabajador_rol.c.trabajador_id,
            trabajador_rol.c.es_principal.is_(True),
        ),
        secondaryjoin=lambda: (
            RolOperativo.id == trabajador_rol.c.rol_operativo_id
        ),
        uselist=False,
        viewonly=True,
        lazy='selectin',
    )

    @property
    def nombre_completo(self):
        return f"{self.nombres} {self.apellidos}".strip()

    @property
    def capacidades_efectivas(self):
        if not self.activo:
            return set()

        return {
            capacidad.codigo
            for rol in self.roles
            if rol.activo
            for capacidad in rol.capacidades
            if capacidad.activo
        }

    def tiene_capacidad(self, codigo):
        return codigo in self.capacidades_efectivas

    @property
    def rol_principal_activo(self):
        role = self.rol_principal
        return role if role is not None and role.activo else None

    def to_dict(self):
        primary_role = self.rol_principal_activo
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombres': self.nombres,
            'apellidos': self.apellidos,
            'nombre_corto': self.nombre_corto,
            'nombre_completo': self.nombre_completo,
            'activo': self.activo,
            'observaciones': self.observaciones,
            'roles': [rol.to_dict() for rol in self.roles],
            'capacidades_efectivas': sorted(self.capacidades_efectivas),
            'rol_principal': (
                primary_role.workspace_identity_dict()
                if primary_role is not None
                else None
            ),
            'rol_principal_pendiente': primary_role is None,
        }
