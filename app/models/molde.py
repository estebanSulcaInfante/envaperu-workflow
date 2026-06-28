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
    
    # Relación N:M con Pieza (forma pura)
    piezas = db.relationship('Pieza', backref='molde', lazy=True, cascade="all, delete-orphan")

    @property
    def peso_neto_gr(self):
        """Peso neto del golpe: suma de (cavidades × peso_unit) de todas las piezas."""
        return sum(mp.peso_unitario_gr * mp.cavidades for mp in self.piezas)
    
    @property
    def peso_colada_gr(self):
        """Peso del ramal/runner del golpe."""
        return self.peso_tiro_gr - self.peso_neto_gr

    @property
    def cavidades_totales(self):
        """Total de cavidades del golpe."""
        return sum(mp.cavidades for mp in self.piezas)
    
    @property
    def merma_pct(self):
        """Porcentaje de merma del molde"""
        if self.peso_tiro_gr and self.peso_tiro_gr > 0:
            return (self.peso_tiro_gr - self.peso_neto_gr) / self.peso_tiro_gr
        return 0.0
    
    def to_dict(self, include_variantes=False):
        return {
            'codigo':           self.codigo,
            'nombre':           self.nombre,
            'peso_tiro_gr':     self.peso_tiro_gr,
            'tiempo_ciclo_std': self.tiempo_ciclo_std,
            'activo':           self.activo,
            'notas':            self.notas,
            # Calculados
            'peso_neto_gr':      self.peso_neto_gr,
            'peso_colada_gr':    self.peso_colada_gr,
            'cavidades_totales': self.cavidades_totales,
            'merma_pct':         self.merma_pct,
            # Piezas (única fuente: MoldePieza)
            'formas': [mp.to_dict(include_variantes=include_variantes) for mp in self.piezas]
        }
    
    def __repr__(self):
        return f'<Molde {self.codigo}: {self.nombre}>'


class Pieza(db.Model):
    """
    Definición de forma pura (geometría) de un molde.
    
    Cada registro representa una FORMA (no un SKU coloreado).
    Las variantes coloreadas (SKUs de inventario) apuntan aquí.
    """
    __tablename__ = 'pieza'
    
    id = db.Column(db.Integer, primary_key=True)
    
    molde_id = db.Column(db.String(50), db.ForeignKey('molde.codigo'), nullable=False)
    
    # Nombre de la forma (ej. "Tapa Regadera")
    nombre = db.Column(db.String(200), nullable=False)
    
    # Clasificación (migrado desde el SKU coloreado)
    linea_id = db.Column(db.Integer, db.ForeignKey('linea.id'), nullable=True)
    familia_id = db.Column(db.Integer, db.ForeignKey('familia.id'), nullable=True)
    
    # Atributos de la forma
    cavidades = db.Column(db.Integer, nullable=False, default=1)
    peso_unitario_gr = db.Column(db.Float, nullable=False)  # Peso de UNA pieza
    
    # Constraint: un molde no tiene dos formas con el mismo nombre
    __table_args__ = (
        db.UniqueConstraint('molde_id', 'nombre', name='uq_molde_pieza_nombre'),
    )
    
    @property
    def peso_total_gr(self):
        """Peso total de esta pieza en el molde (unitario × cavidades)"""
        return self.peso_unitario_gr * self.cavidades
    
    def to_dict(self, include_variantes=False):
        data = {
            'id': self.id,
            'molde_id': self.molde_id,
            'nombre': self.nombre,
            'linea_id': self.linea_id,
            'familia_id': self.familia_id,
            'cavidades': self.cavidades,
            'peso_unitario_gr': self.peso_unitario_gr,
            'peso_total_gr': self.peso_total_gr,
            'variantes_count': len(self.variantes) if hasattr(self, 'variantes') else 0
        }
        if include_variantes and hasattr(self, 'variantes'):
            data['variantes'] = [v.to_dict() for v in self.variantes]
        return data
    
    def __repr__(self):
        return f'<Pieza {self.molde_id}/{self.nombre}: {self.cavidades} cav>'
