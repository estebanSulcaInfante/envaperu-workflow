"""
Modelo HistorialEstadoOrden para tracking de cambios de estado en OrdenProduccion.
"""
from datetime import datetime, timezone
from app.extensions import db


class HistorialEstadoOrden(db.Model):
    """
    Registra cada cambio de estado de una OrdenProduccion.
    Permite tracking de múltiples aperturas y cierres.
    """
    __tablename__ = 'historial_estado_orden'
    
    id = db.Column(db.Integer, primary_key=True)
    
    # FK a la orden
    numero_op = db.Column(db.String(20), db.ForeignKey('orden_produccion.numero_op'), nullable=False)
    
    # Estado anterior y nuevo
    estado_anterior = db.Column(db.Boolean, nullable=True)  # True=Abierta, False=Cerrada, None=Nueva
    estado_nuevo = db.Column(db.Boolean, nullable=False)    # True=Abierta, False=Cerrada
    
    # Metadatos
    fecha = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    usuario = db.Column(db.String(100), nullable=True)  # Quien hizo el cambio
    motivo = db.Column(db.String(200), nullable=True)   # Razón del cambio
    
    # Relación
    orden = db.relationship('OrdenProduccion', backref=db.backref('historial_estados', lazy=True, order_by='HistorialEstadoOrden.fecha.desc()'))
    
    @property
    def accion(self):
        """Describe la acción realizada."""
        if self.estado_anterior is None:
            return 'CREADA'
        elif self.estado_nuevo:
            return 'REABIERTA'
        else:
            return 'CERRADA'
    
    def to_dict(self):
        return {
            'id': self.id,
            'numero_op': self.numero_op,
            'estado_anterior': self.estado_anterior,
            'estado_nuevo': self.estado_nuevo,
            'accion': self.accion,
            'fecha': self.fecha.isoformat() if self.fecha else None,
            'usuario': self.usuario,
            'motivo': self.motivo
        }
    
    def __repr__(self):
        return f'<HistorialEstadoOrden {self.numero_op} {self.accion} {self.fecha}>'


def registrar_cambio_estado(orden, nuevo_estado: bool, usuario: str = None, motivo: str = None):
    """
    Registra un cambio de estado en el historial y actualiza la orden.
    
    Args:
        orden: OrdenProduccion object
        nuevo_estado: True=Abrir, False=Cerrar
        usuario: Quien realiza el cambio
        motivo: Razón del cambio
    
    Returns:
        HistorialEstadoOrden object creado
    """
    estado_anterior = orden.activa
    
    # No registrar si no hay cambio real
    if estado_anterior == nuevo_estado:
        return None
    
    # Crear registro de historial
    historial = HistorialEstadoOrden(
        numero_op=orden.numero_op,
        estado_anterior=estado_anterior,
        estado_nuevo=nuevo_estado,
        usuario=usuario,
        motivo=motivo
    )
    
    # Actualizar estado de la orden
    orden.activa = nuevo_estado
    
    db.session.add(historial)
    db.session.commit()
    
    return historial
