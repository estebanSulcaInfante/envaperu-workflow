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
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    __table_args__ = (
        db.CheckConstraint('version >= 1', name='ck_familia_color_version'),
    )


class Linea(db.Model):
    """
    Línea de productos normalizada.
    Ejemplos: HOGAR (1), INDUSTRIAL (2)
    """
    __tablename__ = 'linea'
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.Integer, unique=True, nullable=False)  # 1, 2...
    nombre = db.Column(db.String(50), unique=True, nullable=False)  # HOGAR, INDUSTRIAL
    activo = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    relaciones_familia = db.relationship(
        "LineaFamilia",
        back_populates="linea_rel",
        lazy="selectin",
        order_by="LineaFamilia.id",
    )

    __table_args__ = (
        db.CheckConstraint("version > 0", name="ck_linea_version"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "codigo_display": f"LIN-{self.codigo:06d}",
            "nombre": self.nombre,
            "activo": self.activo,
            "version": self.version,
        }
    
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
    activo = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    relaciones_linea = db.relationship(
        "LineaFamilia",
        back_populates="familia_rel",
        lazy="selectin",
        order_by="LineaFamilia.id",
    )

    __table_args__ = (
        db.CheckConstraint("version > 0", name="ck_familia_version"),
    )

    def to_dict(self):
        return {
            "id": self.id,
            "codigo": self.codigo,
            "codigo_display": f"FAM-{self.codigo:06d}",
            "nombre": self.nombre,
            "activo": self.activo,
            "version": self.version,
        }
    
    def __repr__(self):
        return f'<Familia {self.codigo}: {self.nombre}>'


class LineaFamilia(db.Model):
    """Combinacion permitida entre los catalogos de linea y familia."""

    __tablename__ = "linea_familia"
    __table_args__ = (
        db.CheckConstraint("version > 0", name="ck_linea_familia_version"),
        db.UniqueConstraint(
            "linea_id",
            "familia_id",
            name="uq_linea_familia_linea_familia",
        ),
        db.Index(
            "ix_linea_familia_linea_activo",
            "linea_id",
            "activo",
        ),
        db.Index(
            "ix_linea_familia_familia_activo",
            "familia_id",
            "activo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    linea_id = db.Column(
        db.Integer,
        db.ForeignKey("linea.id", ondelete="RESTRICT"),
        nullable=False,
    )
    familia_id = db.Column(
        db.Integer,
        db.ForeignKey("familia.id", ondelete="RESTRICT"),
        nullable=False,
    )
    activo = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    linea_rel = db.relationship("Linea", back_populates="relaciones_familia")
    familia_rel = db.relationship("Familia", back_populates="relaciones_linea")

    def to_dict(self, include_catalogos=False):
        data = {
            "id": self.id,
            "linea_id": self.linea_id,
            "familia_id": self.familia_id,
            "activo": self.activo,
            "version": self.version,
        }
        if include_catalogos:
            data["linea"] = self.linea_rel.to_dict() if self.linea_rel else None
            data["familia"] = (
                self.familia_rel.to_dict() if self.familia_rel else None
            )
        return data

    def __repr__(self):
        return f"<LineaFamilia {self.linea_id}/{self.familia_id}>"

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
    hex_referencia = db.Column(db.String(7), nullable=True)
    activo = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.true(),
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )

    __table_args__ = (
        db.CheckConstraint('version > 0', name='ck_color_produccion_version'),
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
    __table_args__ = (
        db.CheckConstraint(
            "length(trim(producto)) > 0",
            name="ck_producto_terminado_nombre_no_vacio",
        ),
    )

    # Relación normalizada con Linea (REFACTORIZADO - eliminados campos legacy)
    linea_id = db.Column(db.Integer, db.ForeignKey('linea.id'), nullable=False)
    linea_rel = db.relationship('Linea', backref='productos_terminados')
    
    # Relación normalizada con Familia (REFACTORIZADO - eliminados campos legacy)
    familia_id = db.Column(db.Integer, db.ForeignKey('familia.id'), nullable=False)
    familia_rel = db.relationship('Familia', backref='productos_terminados')
    
    producto = db.Column(db.String(200), nullable=False)
    cod_sku_pt = db.Column(db.String(50), primary_key=True)

    um = db.Column(db.String(20))
    doc_x_paq = db.Column(db.Integer)
    doc_x_bulto = db.Column(db.Integer)
    peso_g = db.Column(db.Float)
    imagen_mime = db.Column(db.String(32), nullable=True)
    imagen_data = db.Column(db.LargeBinary, nullable=True)
    imagen_storage_key = db.Column(db.String(512), nullable=True)
    imagen_sha256 = db.Column(db.String(64), nullable=True)
    imagen_size_bytes = db.Column(db.Integer, nullable=True)
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
        """Compatibilidad: devuelve la identidad ya asignada, sin recalcularla.

        Los códigos nuevos se reservan transaccionalmente mediante
        ``generar_codigo_catalogo`` antes de construir el modelo. El nombre,
        la línea, la familia y el color nunca deben cambiar una identidad.
        """
        return self.cod_sku_pt


class PiezaColor(db.Model):
    __tablename__ = 'pieza_color'

    sku = db.Column(db.String(50), primary_key=True)
    
    # Relación normalizada con Linea (REFACTORIZADO - eliminados campos legacy)
    linea_id = db.Column(db.Integer, db.ForeignKey('linea.id'), nullable=False)
    linea_rel = db.relationship('Linea', backref='piezas')
    
    # Relación normalizada con Familia (REFACTORIZADO - eliminados campos legacy)
    familia_id = db.Column(db.Integer, db.ForeignKey('familia.id'), nullable=False)
    familia_rel = db.relationship('Familia', backref='piezas')
    
    # --- RELACIÓN CON EL MAESTRO GLOBAL DE PIEZAS ---
    # No identifica un molde: una misma Pieza puede estar en varios moldes mediante
    # MoldePieza. Nullable conserva SKUs legacy aún pendientes de normalizar.
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
    
    # Campos legacy importados. ``cavidad`` no es fuente operativa: las cavidades
    # vigentes viven en MoldePieza y se congelan en el snapshot de cada OP.
    cavidad = db.Column(db.Integer)
    peso = db.Column(db.Float)
    cod_extru = db.Column(db.Integer)
    tipo_extruccion = db.Column(db.String(50))
    cod_mp = db.Column(db.String(50))
    mp = db.Column(db.String(100))
    imagen_mime = db.Column(db.String(32), nullable=True)
    imagen_data = db.Column(db.LargeBinary, nullable=True)
    imagen_storage_key = db.Column(db.String(512), nullable=True)
    imagen_sha256 = db.Column(db.String(64), nullable=True)
    imagen_size_bytes = db.Column(db.Integer, nullable=True)
    
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

    def to_dict(self):
        """Representación del SKU; no expone cavidades como dato operativo."""
        return {
            'sku': self.sku,
            'nombre': self.piezas,
            'pieza_id': self.pieza_id,
            'linea_id': self.linea_id,
            'familia_id': self.familia_id,
            'color_produccion_id': self.color_produccion_id,
            'color': (
                self.color_produccion_rel.nombre
                if self.color_produccion_rel else None
            ),
            'color_hex': (
                self.color_produccion_rel.hex_referencia
                if self.color_produccion_rel else None
            ),
            'peso': self.peso,
            'cavidad_legacy': self.cavidad,
            'imagen_url': (
                f'/api/piezas-color/{self.sku}/imagen'
                if self.imagen_storage_key or self.imagen_data else None
            ),
            'estado_revision': self.estado_revision,
        }

    def generar_sku(self):
        """Compatibilidad: devuelve la identidad ya asignada, sin recalcularla."""
        return self.sku

