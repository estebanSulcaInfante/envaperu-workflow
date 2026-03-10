from app.extensions import db


class LoteColor(db.Model):
    __tablename__ = 'lote_color'

    id = db.Column(db.Integer, primary_key=True)

    # Relación con el Padre
    numero_op = db.Column(db.String(20), db.ForeignKey('orden_produccion.numero_op'), nullable=False)

    # --- COLOR REAL ---
    color_id = db.Column(db.Integer, db.ForeignKey('color_producto.id'), nullable=True)

    # --- SKU SALIDA ---
    producto_sku_output = db.Column(db.String(50), db.ForeignKey('producto_terminado.cod_sku_pt'), nullable=True)

    # Relaciones
    color_rel      = db.relationship('ColorProducto', backref='lotes')
    producto_output = db.relationship('ProductoTerminado', foreign_keys=[producto_sku_output], backref='lotes_produccion')

    # --- META (input directo por lote) ---
    meta_kg = db.Column(db.Float, nullable=False, default=0.0)

    # --- MANO DE OBRA ---
    personas = db.Column(db.Integer, default=1)

    # Relaciones con Materiales (Recetas)
    materias_primas = db.relationship('SeCompone', backref='lote', lazy=True)
    colorantes      = db.relationship('SeColorea', backref='lote', lazy=True)

    # -------------------------------------------------------------------------
    # PERSISTENCIA DE CÁLCULOS
    # -------------------------------------------------------------------------
    calculo_coladas       = db.Column(db.Float, default=0.0)   # golpes necesarios (Float, sin redondeo)
    calculo_kg_real       = db.Column(db.Float, default=0.0)   # kg reales = coladas * peso_neto_golpe / 1000
    calculo_horas_hombre  = db.Column(db.Float, default=0.0)

    # -------------------------------------------------------------------------
    # ACTUALIZACIÓN
    # -------------------------------------------------------------------------
    def actualizar_metricas(self, contexto_orden=None):
        """
        Recalcula métricas del lote en base a meta_kg y el peso del golpe de la orden.
        Coladas es Float (sin redondeo) — la máquina opera en golpes, el sistema muestra el resultado exacto.
        """
        orden_padre = contexto_orden or self.orden
        if not orden_padre:
            return

        peso_neto_golpe = orden_padre.calculo_peso_neto_golpe or 0.0

        if peso_neto_golpe > 0:
            self.calculo_coladas = (self.meta_kg * 1000) / peso_neto_golpe
            self.calculo_kg_real = self.calculo_coladas * peso_neto_golpe / 1000
        else:
            self.calculo_coladas = 0.0
            self.calculo_kg_real = 0.0

        # HORAS HOMBRE: proporcional a los días de la orden
        n_colores = orden_padre.calculo_colores_activos or 1
        dias_orden  = orden_padre.calculo_dias or 0.0
        horas_turno = orden_padre.snapshot_horas_turno or 24.0
        self.calculo_horas_hombre = (dias_orden * horas_turno * self.personas) / n_colores

        # CASCADE a recetas de materiales
        for receta in self.materias_primas:
            receta.actualizar_metricas(contexto_lote=self)

    # -------------------------------------------------------------------------
    # PROPIEDADES DE LECTURA
    # -------------------------------------------------------------------------
    @property
    def peso_total_objetivo(self):
        return self.meta_kg or 0.0

    @property
    def cantidad_coladas_calculada(self):
        return self.calculo_coladas or 0.0

    @property
    def horas_hombre(self):
        return self.calculo_horas_hombre or 0.0

    # -------------------------------------------------------------------------
    # SERIALIZACIÓN
    # -------------------------------------------------------------------------
    def to_dict(self):
        return {
            'id':     self.id,
            'Color':  self.color_rel.nombre if self.color_rel else "Sin Color",

            # Meta y resultado
            'meta_kg':       self.meta_kg,
            'kg_real':       round(self.calculo_kg_real, 3),
            'coladas':       round(self.calculo_coladas, 4),

            # Recetas
            'materiales': [
                {
                    'nombre':  m.materia.nombre,
                    'tipo':    m.materia.tipo,
                    'fraccion': m.fraccion,
                    'peso_kg': m.peso_kg
                } for m in self.materias_primas
            ],
            'pigmentos': [
                {
                    'nombre':   p.pigmento.nombre,
                    'dosis_gr': p.gramos
                } for p in self.colorantes
            ],
            'mano_obra': {
                'personas':    self.personas,
                'horas_hombre': self.horas_hombre
            },

            # Alias de compatibilidad (para código que aún lea estos campos)
            'coladas_calculadas': round(self.calculo_coladas, 4),
        }