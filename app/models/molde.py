"""
Modelos para Moldes de Producción
"""
from app.extensions import db


class Molde(db.Model):
    """
    Representa un molde físico de inyección.
    Un molde produce una o más piezas por golpe.
    Relación 1:N: Un Molde tiene muchas Piezas.
    """
    __tablename__ = 'molde'
    
    codigo = db.Column(db.String(50), primary_key=True)  # "MOL-REGADERA"
    nombre = db.Column(db.String(100), nullable=False)   # "Regadera Completa"
    
    peso_tiro_gr = db.Column(db.Float, nullable=False)   # Peso total del golpe (con colada)
    tiempo_ciclo_std = db.Column(db.Float, default=30.0) # Tiempo ciclo estándar en segundos
    
    activo = db.Column(db.Boolean, default=True)
    notas = db.Column(db.Text, nullable=True)
    
    # Relación 1:N con Pieza (backref 'piezas_producidas' definido en Pieza)
    # Legacy: mantiene 'piezas' para compatibilidad con MoldePieza si existe
    piezas = db.relationship('MoldePieza', backref='molde', lazy=True, cascade="all, delete-orphan")
    
    # --- Propiedades Calculadas usando relación 1:N ---
    @property
    def peso_neto_gr(self):
        """Peso de todas las piezas sin colada"""
        # Usar piezas_producidas (relación directa 1:N) si existe, sino MoldePieza
        if hasattr(self, 'piezas_producidas') and self.piezas_producidas.count() > 0:
            return sum((p.peso or 0) * (p.cavidad or 1) for p in self.piezas_producidas)
        return sum(mp.peso_unitario_gr * mp.cavidades for mp in self.piezas)
    
    @property
    def peso_colada_gr(self):
        """Peso de la colada/desperdicio"""
        return self.peso_tiro_gr - self.peso_neto_gr
    
    @property
    def cavidades_totales(self):
        """Total de cavidades del molde"""
        if hasattr(self, 'piezas_producidas') and self.piezas_producidas.count() > 0:
            return sum((p.cavidad or 1) for p in self.piezas_producidas)
        return sum(mp.cavidades for mp in self.piezas)
    
    @property
    def merma_pct(self):
        """Porcentaje de merma del molde"""
        if self.peso_tiro_gr and self.peso_tiro_gr > 0:
            return (self.peso_tiro_gr - self.peso_neto_gr) / self.peso_tiro_gr
        return 0.0
    
    def to_dict(self):
        # Obtener lista de piezas (preferir relación directa)
        if hasattr(self, 'piezas_producidas') and self.piezas_producidas.count() > 0:
            piezas_list = [{
                'sku': p.sku,
                'nombre': p.piezas,
                'cavidades': p.cavidad,
                'peso_unitario_gr': p.peso
            } for p in self.piezas_producidas]
        else:
            piezas_list = [mp.to_dict() for mp in self.piezas]
        
        return {
            'codigo': self.codigo,
            'nombre': self.nombre,
            'peso_tiro_gr': self.peso_tiro_gr,
            'tiempo_ciclo_std': self.tiempo_ciclo_std,
            'activo': self.activo,
            'notas': self.notas,
            # Calculados
            'peso_neto_gr': self.peso_neto_gr,
            'peso_colada_gr': self.peso_colada_gr,
            'cavidades_totales': self.cavidades_totales,
            'merma_pct': self.merma_pct,
            # Piezas
            'piezas': piezas_list
        }
    
    def __repr__(self):
        return f'<Molde {self.codigo}: {self.nombre}>'


class MoldePieza(db.Model):
    """
    Relación N:N entre Molde y Pieza con atributos.
    Define cuántas cavidades de cada pieza tiene el molde.
    """
    __tablename__ = 'molde_pieza'
    
    id = db.Column(db.Integer, primary_key=True)
    
    molde_id = db.Column(db.String(50), db.ForeignKey('molde.codigo'), nullable=False)
    pieza_sku = db.Column(db.String(50), db.ForeignKey('pieza.sku'), nullable=False)
    
    # Atributos de la relación
    cavidades = db.Column(db.Integer, nullable=False, default=1)
    peso_unitario_gr = db.Column(db.Float, nullable=False)  # Peso de UNA pieza
    
    # Relación a Pieza
    pieza = db.relationship('Pieza', backref='molde_piezas')
    
    # Constraint único
    __table_args__ = (
        db.UniqueConstraint('molde_id', 'pieza_sku', name='uq_molde_pieza'),
    )
    
    @property
    def peso_total_gr(self):
        """Peso total de esta pieza en el molde (unitario × cavidades)"""
        return self.peso_unitario_gr * self.cavidades
    
    def to_dict(self):
        return {
            'id': self.id,
            'molde_id': self.molde_id,
            'pieza_sku': self.pieza_sku,
            'pieza_nombre': self.pieza.piezas if self.pieza else None,
            'cavidades': self.cavidades,
            'peso_unitario_gr': self.peso_unitario_gr,
            'peso_total_gr': self.peso_total_gr
        }
    
    def __repr__(self):
        return f'<MoldePieza {self.molde_id}/{self.pieza_sku}: {self.cavidades} cav>'
