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
    __tablename__ = 'familia_color'
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(50), nullable=False) # e.g. "Transparente", "Solido"

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
    __tablename__ = 'producto_terminado'

    cod_linea_num = db.Column(db.Integer)
    linea = db.Column(db.String(50))
    cod_familia = db.Column(db.Integer)
    familia = db.Column(db.String(100))
    cod_producto = db.Column(db.Integer)
    producto = db.Column(db.String(200))
    cod_color = db.Column(db.Integer)
    familia_color = db.Column(db.String(50))
    cod_sku_pt = db.Column(db.String(50), primary_key=True)
    
    # --- RELACION COLOR (Refactor) ---
    color_id = db.Column(db.Integer, db.ForeignKey('color_producto.id'), nullable=True)
    color_rel = db.relationship('ColorProducto', backref='productos')

    # CAMPOS LEGACY (Mantener por ahora, pero calcular desde relacion si existe)
    # cod_color int, familia_color str -> Se pueden volver properties o sync fields
    cod_color = db.Column(db.Integer) 
    familia_color = db.Column(db.String(50))

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
    
    # Acceso directo a piezas via la tabla intermedia
    @property
    def piezas(self):
        """Lista de piezas que componen este producto."""
        return [cp.pieza for cp in self.composicion_piezas]

    def generar_sku(self):
        """
        Genera el SKU basado en componentes:
        0 + LINEA + FAMILIA + PRODUCTO + 0 + COLOR
        """
        try:
            # Intentar usar la relación de Color si existe para obtener el código
            c_code = self.cod_color
            if self.color_rel:
                c_code = self.color_rel.codigo
            
            # Fallback seguro
            if c_code is None: c_code = 0
            
            return f"0{self.cod_linea_num}{self.cod_familia}{self.cod_producto}0{c_code}"
        except:
            return None


class Pieza(db.Model):
    __tablename__ = 'pieza'

    sku = db.Column(db.String(50), primary_key=True)
    cod_linea = db.Column(db.Integer)
    linea = db.Column(db.String(50))
    familia = db.Column(db.String(100))
    
    # Eliminada FK directa, ahora se usa tabla intermedia ProductoPieza
    
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
            # Priorizar relacional
            c_int = self.cod_color
            if self.color_rel:
                c_int = self.color_rel.codigo
                
            if c_int is None: c_int = 0
            
            return f"{self.cod_linea}{self.cod_pieza}{self.cod_col}{self.cod_extru}{c_int}"
        except:
            return None
