"""
Receta de Color Normalizada
Acumula la dosis de pigmentos en gr/kg de producto por combinación (color, colorante).
Se actualiza automáticamente cada vez que se crea una Orden de Producción.
"""
from app.extensions import db
from datetime import datetime, timezone


class RecetaColorMaestra(db.Model):
    """Fórmula manual, versionada y gobernada por el maestro de colores."""

    __tablename__ = 'receta_color_maestra'
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('BORRADOR', 'APROBADA', 'INACTIVA')",
            name='ck_receta_color_maestra_estado',
        ),
        db.CheckConstraint('revision > 0', name='ck_receta_color_maestra_revision'),
        db.CheckConstraint('version > 0', name='ck_receta_color_maestra_version'),
        db.CheckConstraint(
            'base_virgen_kg > 0',
            name='ck_receta_color_maestra_base_virgen',
        ),
        db.UniqueConstraint(
            'color_produccion_id',
            'producto_scope',
            'nombre_variante',
            'revision',
            name='uq_receta_color_maestra_revision',
        ),
        db.Index(
            'uq_receta_color_maestra_default',
            'color_produccion_id',
            'producto_scope',
            unique=True,
            postgresql_where=db.text("es_default AND estado = 'APROBADA'"),
            sqlite_where=db.text("es_default = 1 AND estado = 'APROBADA'"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    color_produccion_id = db.Column(
        db.Integer,
        db.ForeignKey('color_produccion.id', ondelete='RESTRICT'),
        nullable=False,
    )
    producto_sku = db.Column(
        db.String(50),
        db.ForeignKey('producto_terminado.cod_sku_pt', ondelete='RESTRICT'),
        nullable=True,
    )
    producto_scope = db.Column(db.String(60), nullable=False, default='*')
    nombre_variante = db.Column(db.String(120), nullable=False)
    revision = db.Column(db.Integer, nullable=False, default=1)
    estado = db.Column(db.String(20), nullable=False, default='BORRADOR')
    es_default = db.Column(db.Boolean, nullable=False, default=False)
    base_virgen_kg = db.Column(db.Numeric(10, 3), nullable=False, default=25)
    notas = db.Column(db.String(500), nullable=True)
    origen = db.Column(db.String(30), nullable=False, default='MANUAL')
    version = db.Column(db.Integer, nullable=False, default=1)
    creado_en = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    actualizado_en = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    color_produccion = db.relationship('ColorProduccion', backref='recetas_maestras')
    producto = db.relationship('ProductoTerminado', backref='recetas_color_maestras')
    lineas = db.relationship(
        'RecetaColorLinea',
        back_populates='receta',
        cascade='all, delete-orphan',
        order_by='RecetaColorLinea.orden',
    )


class RecetaColorLinea(db.Model):
    """Componente de una fórmula, ligado a la identidad común de material SCM."""

    __tablename__ = 'receta_color_linea'
    __table_args__ = (
        db.CheckConstraint(
            "tipo_componente IN ('MATERIA_PRIMA', 'COLORANTE', 'ADITIVO')",
            name='ck_receta_color_linea_tipo',
        ),
        db.CheckConstraint(
            "unidad IN ('FRACCION', 'GRAMOS')",
            name='ck_receta_color_linea_unidad',
        ),
        db.CheckConstraint('cantidad > 0', name='ck_receta_color_linea_cantidad'),
        db.CheckConstraint(
            "(tipo_componente = 'MATERIA_PRIMA' AND unidad = 'FRACCION' "
            "AND base_kg IS NULL) OR "
            "(tipo_componente IN ('COLORANTE', 'ADITIVO') AND unidad = 'GRAMOS' "
            "AND base_kg IS NOT NULL AND base_kg > 0)",
            name='ck_receta_color_linea_semantica',
        ),
        db.UniqueConstraint(
            'receta_id',
            'material_id',
            'tipo_componente',
            name='uq_receta_color_linea_material',
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    receta_id = db.Column(
        db.Integer,
        db.ForeignKey('receta_color_maestra.id', ondelete='CASCADE'),
        nullable=False,
    )
    material_id = db.Column(
        db.Integer,
        db.ForeignKey('scm_material.id', ondelete='RESTRICT'),
        nullable=False,
    )
    tipo_componente = db.Column(db.String(20), nullable=False)
    cantidad = db.Column(db.Numeric(12, 4), nullable=False)
    unidad = db.Column(db.String(20), nullable=False)
    base_kg = db.Column(db.Numeric(10, 3), nullable=True)
    orden = db.Column(db.Integer, nullable=False, default=0)

    receta = db.relationship('RecetaColorMaestra', back_populates='lineas')
    material = db.relationship('ScmMaterial')


class RecetaColorNormalizada(db.Model):
    """
    Conocimiento acumulado: cuántos gramos de un colorante se usan
    por cada kg de producto, para una combinación de color (y opcionalmente producto).

    La normalización permite prefill inteligente: dado un color y una meta_kg,
    el sistema sugiere los gramos absolutos a usar.

    Restricción única: (color_id, colorante_id, producto_sku)
    Si producto_sku es NULL = receta genérica del color (aplica a cualquier producto).
    """
    __tablename__ = 'receta_color_normalizada'

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # Claves de clasificación
    color_produccion_id = db.Column(db.Integer, db.ForeignKey('color_produccion.id'), nullable=False)
    colorante_id  = db.Column(db.Integer, db.ForeignKey('colorante.id'), nullable=False)
    producto_sku  = db.Column(db.String(50), db.ForeignKey('producto_terminado.cod_sku_pt'), nullable=True)

    # Métrica normalizada (promedio ponderado acumulado)
    gr_por_kg     = db.Column(db.Float, nullable=False, default=0.0)
    n_muestras    = db.Column(db.Integer, nullable=False, default=0)

    ultima_actualizacion = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc)
    )

    # Relaciones de lectura
    color_produccion = db.relationship('ColorProduccion', backref='recetas_normalizadas')
    colorante = db.relationship('Colorante', backref='recetas_normalizadas')
    producto  = db.relationship('ProductoTerminado', backref='recetas_color')

    # Restricción única: una sola receta por combinación
    __table_args__ = (
        db.UniqueConstraint('color_produccion_id', 'colorante_id', 'producto_sku',
                            name='uq_receta_color_normalizada'),
    )

    # -----------------------------------------------------------------------
    # LÓGICA DE ACUMULACIÓN
    # -----------------------------------------------------------------------

    def absorber_nueva_muestra(self, gr_por_kg_nuevo: float):
        """
        Actualiza el promedio ponderado con una nueva observación.
        Fórmula: nuevo_promedio = (actual * n + nuevo) / (n + 1)
        """
        n = self.n_muestras or 0
        self.gr_por_kg = (self.gr_por_kg * n + gr_por_kg_nuevo) / (n + 1)
        self.n_muestras = n + 1
        self.ultima_actualizacion = datetime.now(timezone.utc)

    # -----------------------------------------------------------------------
    # CLASE HELPER: UPSERT
    # -----------------------------------------------------------------------

    @classmethod
    def upsert(cls, session, color_produccion_id: int, colorante_id: int,
               producto_sku: str | None, gr_por_kg_nuevo: float):
        """
        Busca o crea la receta y absorbe la nueva muestra.
        Retorna la instancia actualizada (no hace commit).
        """
        receta = session.query(cls).filter_by(
            color_produccion_id=color_produccion_id,
            colorante_id=colorante_id,
            producto_sku=producto_sku
        ).first()

        if receta is None:
            receta = cls(
                color_produccion_id=color_produccion_id,
                colorante_id=colorante_id,
                producto_sku=producto_sku,
                gr_por_kg=gr_por_kg_nuevo,
                n_muestras=1,
            )
            session.add(receta)
        else:
            receta.absorber_nueva_muestra(gr_por_kg_nuevo)

        return receta

    # -----------------------------------------------------------------------
    # SERIALIZACIÓN
    # -----------------------------------------------------------------------

    def to_dict(self, meta_kg: float | None = None):
        d = {
            'colorante_id': self.colorante_id,
            'nombre':        self.colorante.nombre if self.colorante else None,
            'gr_por_kg':     round(self.gr_por_kg, 4),
            'n_muestras':    self.n_muestras,
        }
        if meta_kg is not None and meta_kg > 0:
            d['gramos'] = round(self.gr_por_kg * meta_kg, 2)
        return d

    def __repr__(self):
        return (f'<RecetaColor color_prod={self.color_produccion_id} '
                f'colorante={self.colorante_id} '
                f'gr_kg={self.gr_por_kg:.4f} n={self.n_muestras}>')
