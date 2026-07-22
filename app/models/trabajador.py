from app.extensions import db
from app.models.scm_catalogos import scm_rol_capacidad

# Tabla intermedia N:M para relacionar trabajadores con múltiples roles
trabajador_rol = db.Table('trabajador_rol',
    db.Column('trabajador_id', db.Integer, db.ForeignKey('trabajador.id'), primary_key=True),
    db.Column('rol_operativo_id', db.Integer, db.ForeignKey('rol_operativo.id'), primary_key=True)
)

class RolOperativo(db.Model):
    """
    Catálogo de roles operativos que puede tener un trabajador.
    Ejemplo: MAQUINISTA, OPERADOR_PESAJE, MEZCLADOR, SUPERVISOR.
    """
    __tablename__ = 'rol_operativo'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    activo = db.Column(db.Boolean, default=True)
    capacidades = db.relationship(
        'ScmCapacidad',
        secondary=scm_rol_capacidad,
        back_populates='roles',
        lazy='selectin',
    )

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombre': self.nombre,
            'activo': self.activo,
            'capacidades': [
                capacidad.codigo
                for capacidad in self.capacidades
                if capacidad.activo
            ],
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

    # Relación N:M
    roles = db.relationship('RolOperativo', secondary=trabajador_rol, lazy='subquery',
        backref=db.backref('trabajadores', lazy=True))

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

    def to_dict(self):
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
        }
