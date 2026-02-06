from app.extensions import db

# Tabla de Asociación N:M entre ProductoTerminado y Pieza
class ProductoPieza(db.Model):
    """
    Tabla intermedia para la relación muchos-a-muchos.
    Un ProductoTerminado puede tener varias Piezas.
    Una Pieza puede pertenecer a varios ProductosTerminados (ej. packs, combos).
    """
    __tablename__ = 'producto_pieza'
    
    id = db.Column(db.Integer, primary_key=True)
    producto_terminado_id = db.Column(db.String(50), db.ForeignKey('producto_terminado.cod_sku_pt'), nullable=False)
    pieza_sku = db.Column(db.String(50), db.ForeignKey('pieza.sku'), nullable=False)
    
    # Cantidad de esta pieza en el producto (ej. 2 jarras en un pack)
    cantidad = db.Column(db.Integer, default=1)
    
    # Relaciones para acceso fácil
    producto_terminado = db.relationship('ProductoTerminado', backref='composicion_piezas')
    pieza = db.relationship('Pieza', backref='en_productos')
    
    # Evitar duplicados
    __table_args__ = (db.UniqueConstraint('producto_terminado_id', 'pieza_sku', name='uq_producto_pieza'),)


class FamiliaColor(db.Model):
    """
    Familia de Color para ProductoTerminado.
    Ejemplos: SOLIDO, CARAMELO, TRANSPARENTE, PASTEL, VARIOS
    
    NOTA: ProductoTerminado NO tiene color específico, solo familia de color.
          Las Piezas sí tienen color específico (ColorProducto).
    """
    __tablename__ = 'familia_color'
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.Integer, unique=True, nullable=True)  # Cod Color del CSV (1, 2, 3...)
    nombre = db.Column(db.String(50), unique=True, nullable=False)  # SOLIDO, CARAMELO, etc.


class Linea(db.Model):
    """
    Línea de productos normalizada.
    Ejemplos: HOGAR (1), INDUSTRIAL (2)
    """
    __tablename__ = 'linea'
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.Integer, unique=True, nullable=False)  # 1, 2...
    nombre = db.Column(db.String(50), unique=True, nullable=False)  # HOGAR, INDUSTRIAL
    
    def __repr__(self):
        return f'<Linea {self.codigo}: {self.nombre}>'


class Familia(db.Model):
    """
    Familia de productos normalizada.
    Ejemplos: Baldes, Jarras, Tinas, etc.
    """
    __tablename__ = 'familia'
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.Integer, unique=True, nullable=False)  # 01, 02, 03...
    nombre = db.Column(db.String(100), unique=True, nullable=False)  # Baldes, Jarras, etc.
    
    def __repr__(self):
        return f'<Familia {self.codigo}: {self.nombre}>'

class ColorProducto(db.Model):
    __tablename__ = 'color_producto'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50), nullable=False) # e.g. "Rojo", "Azul"
    codigo = db.Column(db.Integer, nullable=False)    # e.g. 5
    familia_id = db.Column(db.Integer, db.ForeignKey('familia_color.id'), nullable=True)
    
    familia = db.relationship('FamiliaColor', backref='colores')

    def __repr__(self):
        return f'{self.nombre} ({self.codigo})'


class ProductoTerminado(db.Model):
    """
    Producto Terminado - SKU final que se vende.
    
    IMPORTANTE: ProductoTerminado tiene FAMILIA DE COLOR, no color específico.
    La familia de color describe el tipo/acabado: SOLIDO, CARAMELO, TRANSPARENTE, etc.
    El color específico (Rojo, Azul, Verde) está en las Piezas.
    """
    __tablename__ = 'producto_terminado'

    # Relación normalizada con Linea (REFACTORIZADO - eliminados campos legacy)
    linea_id = db.Column(db.Integer, db.ForeignKey('linea.id'), nullable=False)
    linea_rel = db.relationship('Linea', backref='productos_terminados')
    
    # Relación normalizada con Familia (REFACTORIZADO - eliminados campos legacy)
    familia_id = db.Column(db.Integer, db.ForeignKey('familia.id'), nullable=False)
    familia_rel = db.relationship('Familia', backref='productos_terminados')
    
    cod_producto = db.Column(db.Integer)
    producto = db.Column(db.String(200))
    cod_sku_pt = db.Column(db.String(50), primary_key=True)
    
    # --- FAMILIA DE COLOR (Refactorizado) ---
    # cod_color en el CSV de productos es realmente el código de FAMILIA
    cod_familia_color = db.Column(db.Integer)  # Renombrado para claridad (antes cod_color)
    familia_color = db.Column(db.String(50))   # Nombre: SOLIDO, CARAMELO, etc.
    
    # Relación con FamiliaColor (opcional - para normalización)
    familia_color_id = db.Column(db.Integer, db.ForeignKey('familia_color.id'), nullable=True)
    familia_color_rel = db.relationship('FamiliaColor', backref='productos')

    um = db.Column(db.String(20))
    doc_x_paq = db.Column(db.Integer)
    doc_x_bulto = db.Column(db.Integer)
    peso_g = db.Column(db.Float)
    precio_estimado = db.Column(db.Float)
    precio_sin_igv = db.Column(db.Float)
    indicador_x_kg = db.Column(db.Float)
    status = db.Column(db.String(50))
    codigo_barra = db.Column(db.String(50))
    marca = db.Column(db.String(50))
    nombre_gs1 = db.Column(db.String(200))
    obs = db.Column(db.Text)
    
    # --- CAMPOS DE REVISIÓN PROGRESIVA ---
    estado_revision = db.Column(db.String(20), default='IMPORTADO')  # IMPORTADO, EN_REVISION, VERIFICADO
    fecha_importacion = db.Column(db.DateTime, default=db.func.now())
    fecha_revision = db.Column(db.DateTime, nullable=True)
    notas_revision = db.Column(db.Text, nullable=True)
    
    # Acceso directo a piezas via la tabla intermedia
    @property
    def piezas(self):
        """Lista de piezas que componen este producto."""
        return [cp.pieza for cp in self.composicion_piezas]

    def generar_sku(self):
        """
        Genera el SKU basado en componentes:
        0 + LINEA + FAMILIA + PRODUCTO + 0 + COD_FAMILIA_COLOR
        """
        try:
            # Usar código de linea desde relación normalizada
            linea_code = self.linea_rel.codigo if self.linea_rel else 0
            
            # Usar código de familia desde relación normalizada
            familia_code = self.familia_rel.codigo if self.familia_rel else 0
            
            # Usar código de familia de color
            fc_code = self.cod_familia_color
            if self.familia_color_rel:
                fc_code = self.familia_color_rel.codigo
            
            if fc_code is None: fc_code = 0
            
            return f"0{linea_code}{familia_code}{self.cod_producto}0{fc_code}"
        except:
            return None


class Pieza(db.Model):
    __tablename__ = 'pieza'

    sku = db.Column(db.String(50), primary_key=True)
    
    # Relación normalizada con Linea (REFACTORIZADO - eliminados campos legacy)
    linea_id = db.Column(db.Integer, db.ForeignKey('linea.id'), nullable=False)
    linea_rel = db.relationship('Linea', backref='piezas')
    
    # Relación normalizada con Familia (REFACTORIZADO - eliminados campos legacy)
    familia_id = db.Column(db.Integer, db.ForeignKey('familia.id'), nullable=False)
    familia_rel = db.relationship('Familia', backref='piezas')
    
    # Tipo de pieza: SIMPLE, KIT, COMPONENTE
    tipo = db.Column(db.String(20), default="SIMPLE")
    
    # Relación 1:N con Molde (1 Molde → N Piezas, 1 Pieza → 1 Molde)
    molde_id = db.Column(db.String(50), db.ForeignKey('molde.codigo'), nullable=True)
    molde = db.relationship('Molde', backref=db.backref('piezas_producidas', lazy='dynamic'))
    
    cod_pieza = db.Column(db.Integer)
    piezas = db.Column(db.String(200)) # Nombre Pieza
    
    # --- RELACION COLOR (Refactor) ---
    color_id = db.Column(db.Integer, db.ForeignKey('color_producto.id'), nullable=True)
    color_rel = db.relationship('ColorProducto', backref='piezas')
    
    cod_col = db.Column(db.String(10)) # Legacy string code?
    tipo_color = db.Column(db.String(50))
    
    cavidad = db.Column(db.Integer)
    peso = db.Column(db.Float)
    cod_extru = db.Column(db.Integer)
    tipo_extruccion = db.Column(db.String(50))
    cod_mp = db.Column(db.String(50))
    mp = db.Column(db.String(100))
    
    cod_color = db.Column(db.Integer) # Legacy integer code?
    color = db.Column(db.String(50)) # Legacy name
    
    # --- CAMPOS DE REVISIÓN PROGRESIVA ---
    estado_revision = db.Column(db.String(20), default='IMPORTADO')  # IMPORTADO, EN_REVISION, VERIFICADO
    fecha_importacion = db.Column(db.DateTime, default=db.func.now())
    fecha_revision = db.Column(db.DateTime, nullable=True)
    notas_revision = db.Column(db.Text, nullable=True)
    
    # Acceso directo a productos donde se usa esta pieza
    @property
    def productos_terminados(self):
        """Lista de productos que usan esta pieza."""
        return [ep.producto_terminado for ep in self.en_productos]

    def generar_sku(self):
        """
        Genera SKU Pieza:
        LINEA + PIEZA + COD_COL(Str) + EXTRU + COD_COLOR(Int)
        """
        try:
            # Usar código de linea desde relación normalizada
            linea_code = self.linea_rel.codigo if self.linea_rel else 0
            
            # Priorizar relacional para color
            c_int = self.cod_color
            if self.color_rel:
                c_int = self.color_rel.codigo
                
            if c_int is None: c_int = 0
            
            return f"{linea_code}{self.cod_pieza}{self.cod_col}{self.cod_extru}{c_int}"
        except:
            return None


class PiezaComponente(db.Model):
    """
    Relación auto-referencial para Kits.
    Un Kit (Pieza) puede tener múltiples componentes (otras Piezas).
    """
    __tablename__ = 'pieza_componente'
    
    id = db.Column(db.Integer, primary_key=True)
    
    kit_sku = db.Column(db.String(50), db.ForeignKey('pieza.sku'), nullable=False)
    componente_sku = db.Column(db.String(50), db.ForeignKey('pieza.sku'), nullable=False)
    cantidad = db.Column(db.Integer, default=1)
    
    # Relaciones
    kit = db.relationship('Pieza', foreign_keys=[kit_sku], backref='componentes')
    componente = db.relationship('Pieza', foreign_keys=[componente_sku])
    
    # Constraint único
    __table_args__ = (
        db.UniqueConstraint('kit_sku', 'componente_sku', name='uq_pieza_componente'),
    )
    
    def to_dict(self):
        return {
            'id': self.id,
            'kit_sku': self.kit_sku,
            'componente_sku': self.componente_sku,
            'componente_nombre': self.componente.piezas if self.componente else None,
            'cantidad': self.cantidad
        }
    
    def __repr__(self):
        return f'<PiezaComponente {self.kit_sku} -> {self.componente_sku} x{self.cantidad}>'

