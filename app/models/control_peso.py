from app.extensions import db
from datetime import datetime, timezone

class ControlPeso(db.Model):
    """
    Representa el pesaje individual de un 'bulto' o paquete.
    Se asocia a un RegistroDiarioProduccion para validación cruzada.
    """
    __tablename__ = 'control_peso'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    
    # FK hacia la Cabecera de Produccion
    registro_id = db.Column(db.Integer, db.ForeignKey('registro_diario_produccion.id'), nullable=False)
    
    # Datos del Pesaje
    peso_real_kg = db.Column(db.Float, nullable=False, default=0.0)
    
    # Color: Puede ser un ID si se linkea estricto, o un string si es flexible
    # Por ahora permitiremos ambos conceptos o string como fallback
    color_nombre = db.Column(db.String(50), nullable=True) 
    color_id = db.Column(db.Integer, db.ForeignKey('color_producto.id'), nullable=True)
    
    # Auditoría
    hora_registro = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    usuario_id = db.Column(db.Integer, nullable=True) # Quien pesó (opcional por ahora)

    # Relaciones
    # registro = db.relationship('RegistroDiarioProduccion', backref='controles_peso') # Definido en registro.py o aqui via backref

    def to_dict(self):
        return {
            'id': self.id,
            'registro_id': self.registro_id,
            'peso_real_kg': self.peso_real_kg,
            'color': self.color_nombre,
            'hora': self.hora_registro.isoformat() if self.hora_registro else None
        }
