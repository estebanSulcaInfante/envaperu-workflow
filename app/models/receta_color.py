"""
Receta de Color Normalizada
Acumula la dosis de pigmentos en gr/kg de producto por combinación (color, colorante).
Se actualiza automáticamente cada vez que se crea una Orden de Producción.
"""
from app.extensions import db
from datetime import datetime, timezone


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
    color_id      = db.Column(db.Integer, db.ForeignKey('color_producto.id'), nullable=False)
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
    color     = db.relationship('ColorProducto', backref='recetas_normalizadas')
    colorante = db.relationship('Colorante', backref='recetas_normalizadas')
    producto  = db.relationship('ProductoTerminado', backref='recetas_color')

    # Restricción única: una sola receta por combinación
    __table_args__ = (
        db.UniqueConstraint('color_id', 'colorante_id', 'producto_sku',
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
    def upsert(cls, session, color_id: int, colorante_id: int,
               producto_sku: str | None, gr_por_kg_nuevo: float):
        """
        Busca o crea la receta y absorbe la nueva muestra.
        Retorna la instancia actualizada (no hace commit).
        """
        receta = session.query(cls).filter_by(
            color_id=color_id,
            colorante_id=colorante_id,
            producto_sku=producto_sku
        ).first()

        if receta is None:
            receta = cls(
                color_id=color_id,
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
        return (f'<RecetaColor color={self.color_id} '
                f'colorante={self.colorante_id} '
                f'gr_kg={self.gr_por_kg:.4f} n={self.n_muestras}>')
