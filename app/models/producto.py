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
    pieza_sku = db.Column(db.String(50), db.ForeignKey('pieza_color.sku'), nullable=False)
    
    # Cantidad de esta pieza en el producto (ej. 2 jarras en un pack)
    cantidad = db.Column(db.Integer, default=1)
    
    # Relaciones para acceso fácil
    producto_terminado = db.relationship('ProductoTerminado', backref='composicion_piezas')
    pieza = db.relationship('PiezaColor', backref='en_productos')
    
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

class ColorBase(db.Model):
    """El pigmento puro, independiente de la familia comercial."""
    __tablename__ = 'color_base'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50), nullable=False, unique=True) # e.g. "ROJO", "AZUL"
    
    def __repr__(self):
        return f'<ColorBase {self.nombre}>'

class ColorProduccion(db.Model):
    """La combinación operativa de un pigmento en un acabado."""
    __tablename__ = 'color_produccion'
    id = db.Column(db.Integer, primary_key=True)
    
    color_base_id = db.Column(db.Integer, db.ForeignKey('color_base.id'), nullable=False)
    color_base_rel = db.relationship('ColorBase', backref='colores_produccion')
    
    familia_color_id = db.Column(db.Integer, db.ForeignKey('familia_color.id'), nullable=False)
    familia_color_rel = db.relationship('FamiliaColor', backref='colores_produccion')
    
    codigo_legacy = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('color_base_id', 'familia_color_id', name='uix_color_base_familia'),
    )

    def __repr__(self):
        base_name = self.color_base_rel.nombre if self.color_base_rel else f"Base{self.color_base_id}"
        fam_name = self.familia_color_rel.nombre if self.familia_color_rel else f"Fam{self.familia_color_id}"
        return f'{base_name} {fam_name}'

    @property
    def nombre(self):
        return str(self)


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
        Genera el SKU basado en componentes.
        El SKU original era importado, la autogeneración ya no usa familia de color.
        """
        try:
            linea_code = self.linea_rel.codigo if self.linea_rel else 0
            familia_code = self.familia_rel.codigo if self.familia_rel else 0
            return f"0{linea_code}{familia_code}{self.cod_producto}"
        except:
            return None


class PiezaColor(db.Model):
    __tablename__ = 'pieza_color'

    sku = db.Column(db.String(50), primary_key=True)
    
    # Relación normalizada con Linea (REFACTORIZADO - eliminados campos legacy)
    linea_id = db.Column(db.Integer, db.ForeignKey('linea.id'), nullable=False)
    linea_rel = db.relationship('Linea', backref='piezas')
    
    # Relación normalizada con Familia (REFACTORIZADO - eliminados campos legacy)
    familia_id = db.Column(db.Integer, db.ForeignKey('familia.id'), nullable=False)
    familia_rel = db.relationship('Familia', backref='piezas')
    
    # Tipo de pieza: SIMPLE, KIT, COMPONENTE
    tipo = db.Column(db.String(20), default="SIMPLE")
    
    # --- RELACIÓN CON FORMA DEL MOLDE (Refactor) ---
    # Vincula esta pieza (SKU coloreado) con su forma/cavidad en el molde
    # Nullable: piezas legacy importadas del Excel pueden no tener esta relación aún
    pieza_id = db.Column(db.Integer, db.ForeignKey('pieza.id'), nullable=True)
    pieza_rel = db.relationship('Pieza', backref='variantes', foreign_keys=[pieza_id])
    
    # Restricción: No se puede repetir la misma forma y color de producción
    __table_args__ = (
        db.UniqueConstraint('pieza_id', 'color_produccion_id', name='uq_pieza_color'),
    )

    cod_pieza = db.Column(db.Integer)
    piezas = db.Column(db.String(200)) # Nombre Pieza
    
    # --- RELACION COLOR PRODUCCION (Refactor) ---
    color_produccion_id = db.Column(db.Integer, db.ForeignKey('color_produccion.id'), nullable=True)
    color_produccion_rel = db.relationship('ColorProduccion', backref='piezas_color')
    
    cavidad = db.Column(db.Integer)
    peso = db.Column(db.Float)
    cod_extru = db.Column(db.Integer)
    tipo_extruccion = db.Column(db.String(50))
    cod_mp = db.Column(db.String(50))
    mp = db.Column(db.String(100))
    
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
            linea_code = self.linea_rel.codigo if self.linea_rel else 0
            c_int = self.color_produccion_rel.codigo_legacy if self.color_produccion_rel else 0
            if c_int is None: c_int = 0
            
            return f"{linea_code}{self.cod_pieza}{self.cod_extru}{c_int}"
        except:
            return None


class PiezaComponente(db.Model):
    """
    Relación auto-referencial para Kits.
    Un Kit (PiezaColor) puede tener múltiples componentes (otras PiezasColor).
    """
    __tablename__ = 'pieza_componente'
    
    id = db.Column(db.Integer, primary_key=True)
    
    kit_sku = db.Column(db.String(50), db.ForeignKey('pieza_color.sku'), nullable=False)
    componente_sku = db.Column(db.String(50), db.ForeignKey('pieza_color.sku'), nullable=False)
    cantidad = db.Column(db.Integer, default=1)
    
    # Relaciones
    kit = db.relationship('PiezaColor', foreign_keys=[kit_sku], backref='componentes')
    componente = db.relationship('PiezaColor', foreign_keys=[componente_sku])
    
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

