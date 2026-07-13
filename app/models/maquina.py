from app.extensions import db

class TipoMaquina(db.Model):
    """
    Clasificación reusable para las máquinas (ej. Inyectoras, Sopladoras).
    """
    __tablename__ = 'tipo_maquina'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    proceso = db.Column(db.String(50), nullable=False) # INYECCION, SOPLADO, OTRO
    fabricante = db.Column(db.String(100), nullable=True)
    modelo = db.Column(db.String(100), nullable=True)
    capacidad_toneladas = db.Column(db.Float, nullable=True)
    activo = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombre': self.nombre,
            'proceso': self.proceso,
            'fabricante': self.fabricante,
            'modelo': self.modelo,
            'capacidad_toneladas': self.capacidad_toneladas,
            'activo': self.activo
        }

class Maquina(db.Model):
    """
    Entidad de catálogo para máquinas de producción físicas.
    Permite asociar órdenes de producción y registros diarios a máquinas específicas.
    """
    __tablename__ = 'maquina'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    codigo = db.Column(db.String(20), unique=True, nullable=False)
    nombre = db.Column(db.String(100), nullable=False)
    tipo_maquina_id = db.Column(db.Integer, db.ForeignKey('tipo_maquina.id'), nullable=False)
    estado = db.Column(db.String(50), default='OPERATIVA') # OPERATIVA, MANTENIMIENTO, FUERA_SERVICIO, BAJA
    activo = db.Column(db.Boolean, default=True)
    numero_serie = db.Column(db.String(100), nullable=True)
    observaciones = db.Column(db.Text, nullable=True)

    # El campo antiguo 'tipo' se retiene temporalmente durante migracion y luego se debe eliminar, 
    # pero para evitar conflictos en codigo en vivo lo dejaré por un momento
    tipo = db.Column(db.String(100), nullable=True)

    # Relaciones
    tipo_maquina = db.relationship('TipoMaquina', backref='maquinas', lazy=True)
    # Back-reference to orders (defined in OrdenProduccion)
    ordenes = db.relationship('OrdenProduccion', backref='maquina_ref', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'codigo': self.codigo,
            'nombre': self.nombre,
            'tipo_maquina_id': self.tipo_maquina_id,
            'tipo_maquina': self.tipo_maquina.to_dict() if self.tipo_maquina else None,
            'estado': self.estado,
            'activo': self.activo,
            'numero_serie': self.numero_serie,
            'observaciones': self.observaciones,
            'tipo_legacy': self.tipo
        }

    def __repr__(self):
        return f'<Maquina {self.codigo} - {self.nombre}>'
