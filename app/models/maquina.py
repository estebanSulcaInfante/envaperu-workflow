from app.extensions import db


class Maquina(db.Model):
    """
    Entidad de catálogo para máquinas de producción.
    Permite asociar órdenes de producción y registros diarios a máquinas específicas.
    """
    __tablename__ = 'maquina'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(50), unique=True, nullable=False)
    tipo = db.Column(db.String(100))

    # Back-reference to orders (defined in OrdenProduccion)
    ordenes = db.relationship('OrdenProduccion', backref='maquina_ref', lazy=True)

    def to_dict(self):
        return {
            'id': self.id,
            'nombre': self.nombre,
            'tipo': self.tipo
        }

    def __repr__(self):
        return f'<Maquina {self.nombre}>'
