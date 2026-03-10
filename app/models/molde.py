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
    
    # Relación N:M con Pieza via tabla MoldePieza (única fuente de verdad)
    piezas = db.relationship('MoldePieza', backref='molde', lazy=True, cascade="all, delete-orphan")

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
    
    def to_dict(self):
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
            'piezas': [mp.to_dict() for mp in self.piezas]
        }
    
    def __repr__(self):
        return f'<Molde {self.codigo}: {self.nombre}>'


class MoldePieza(db.Model):
    """
    Definición de forma/cavidad de un molde.
    
    Cada registro representa una FORMA (no un SKU coloreado).
    Las Piezas coloreadas (SKUs de inventario) apuntan aquí
    vía Pieza.molde_pieza_id.
    
    Ejemplo: Molde "Jarra Regadera" tiene 2 MoldePieza:
      - id=1: "Tapa Regadera" (2 cav, 15g)
      - id=2: "Base Regadera" (2 cav, 15g)
    """
    __tablename__ = 'molde_pieza'
    
    id = db.Column(db.Integer, primary_key=True)
    
    molde_id = db.Column(db.String(50), db.ForeignKey('molde.codigo'), nullable=False)
    
    # Nombre de la forma (ej. "Tapa Regadera")
    nombre = db.Column(db.String(200), nullable=True)
    
    # Legacy: pieza_sku apunta a una pieza específica (nullable para formas puras)
    # use_alter=True rompe la dependencia circular Pieza↔MoldePieza
    pieza_sku = db.Column(db.String(50), db.ForeignKey('pieza.sku', name='fk_moldepieza_pieza_sku', use_alter=True), nullable=True)
    
    # Atributos de la forma
    cavidades = db.Column(db.Integer, nullable=False, default=1)
    peso_unitario_gr = db.Column(db.Float, nullable=False)  # Peso de UNA pieza
    
    # Relación legacy a Pieza (puede ser None para formas puras)
    pieza = db.relationship('Pieza', backref='molde_piezas', foreign_keys=[pieza_sku])
    
    # Constraint: un molde no tiene dos formas con el mismo nombre
    __table_args__ = (
        db.UniqueConstraint('molde_id', 'nombre', name='uq_molde_pieza_nombre'),
    )
    
    @property
    def peso_total_gr(self):
        """Peso total de esta pieza en el molde (unitario × cavidades)"""
        return self.peso_unitario_gr * self.cavidades
    
    def to_dict(self):
        return {
            'id': self.id,
            'molde_id': self.molde_id,
            'nombre': self.nombre,
            'pieza_sku': self.pieza_sku,
            'pieza_nombre': self.pieza.piezas if self.pieza else self.nombre,
            'cavidades': self.cavidades,
            'peso_unitario_gr': self.peso_unitario_gr,
            'peso_total_gr': self.peso_total_gr,
            'variantes_count': len(self.variantes) if hasattr(self, 'variantes') else 0
        }
    
    def __repr__(self):
        label = self.nombre or self.pieza_sku or '?'
        return f'<MoldePieza {self.molde_id}/{label}: {self.cavidades} cav>'
