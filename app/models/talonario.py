"""
Modelo Talonario para gestión de correlativos RDP
"""
from datetime import datetime, timezone
from app.extensions import db


class Talonario(db.Model):
    """
    Representa un talonario de Registros Diarios de Producción.
    Cada talonario tiene un rango de correlativos (desde-hasta).
    """
    __tablename__ = 'talonario'
    
    id = db.Column(db.Integer, primary_key=True)
    
    desde = db.Column(db.Integer, nullable=False)  # Correlativo inicial
    hasta = db.Column(db.Integer, nullable=False)  # Correlativo final
    
    descripcion = db.Column(db.String(200))  # "Lote Enero 2026"
    fecha_registro = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    activo = db.Column(db.Boolean, default=True)
    
    # Tracking de uso
    ultimo_usado = db.Column(db.Integer, nullable=True)  # Último correlativo usado
    
    @property
    def total(self):
        """Total de correlativos en el talonario"""
        return self.hasta - self.desde + 1
    
    @property
    def usados(self):
        """Cantidad de correlativos usados"""
        if self.ultimo_usado is None:
            return 0
        return self.ultimo_usado - self.desde + 1
    
    @property
    def disponibles(self):
        """Cantidad de correlativos disponibles"""
        return self.total - self.usados
    
    @property
    def siguiente(self):
        """Próximo correlativo a usar, None si agotado"""
        if self.ultimo_usado is None:
            return self.desde
        next_val = self.ultimo_usado + 1
        if next_val > self.hasta:
            return None  # Talonario agotado
        return next_val
    
    @property
    def porcentaje_uso(self):
        """Porcentaje de uso del talonario"""
        if self.total == 0:
            return 0
        return (self.usados / self.total) * 100
    
    def consumir(self):
        """
        Consume el siguiente correlativo disponible.
        Retorna el correlativo asignado o None si está agotado.
        """
        sig = self.siguiente
        if sig is None:
            return None
        self.ultimo_usado = sig
        return sig
    
    def to_dict(self):
        return {
            'id': self.id,
            'desde': self.desde,
            'hasta': self.hasta,
            'descripcion': self.descripcion,
            'fecha_registro': self.fecha_registro.isoformat() if self.fecha_registro else None,
            'activo': self.activo,
            'ultimo_usado': self.ultimo_usado,
            'total': self.total,
            'usados': self.usados,
            'disponibles': self.disponibles,
            'siguiente': self.siguiente,
            'porcentaje_uso': round(self.porcentaje_uso, 1)
        }
    
    def __repr__(self):
        return f'<Talonario {self.desde}-{self.hasta}>'
