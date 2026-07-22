from app.extensions import db


class LoteColor(db.Model):
    __tablename__ = 'lote_color'

    id = db.Column(db.Integer, primary_key=True)

    # Relación con el Padre
    numero_op = db.Column(db.String(20), db.ForeignKey('orden_produccion.numero_op'), nullable=False)

    # --- COLOR REAL ---
    color_produccion_id = db.Column(db.Integer, db.ForeignKey('color_produccion.id'), nullable=True)

    # --- SKU SALIDA ---
    producto_sku_output = db.Column(db.String(50), db.ForeignKey('producto_terminado.cod_sku_pt'), nullable=True)

    # Receta maestra aplicada. La FK permite navegar al maestro actual y los
    # snapshots conservan exactamente la revisión usada por esta OP.
    receta_color_maestra_id = db.Column(
        db.Integer,
        db.ForeignKey('receta_color_maestra.id', ondelete='RESTRICT'),
        nullable=True,
    )
    receta_revision_snapshot = db.Column(db.Integer, nullable=True)
    receta_nombre_snapshot = db.Column(db.String(120), nullable=True)
    receta_base_virgen_kg_snapshot = db.Column(db.Numeric(10, 3), nullable=True)

    # Relaciones
    color_produccion_rel = db.relationship('ColorProduccion', backref='lotes')
    producto_output = db.relationship('ProductoTerminado', foreign_keys=[producto_sku_output], backref='lotes_produccion')
    receta_color_maestra = db.relationship('RecetaColorMaestra')

    # --- META (input directo por lote) ---
    meta_kg = db.Column(db.Float, nullable=False, default=0.0)

    # --- MANO DE OBRA ---
    personas = db.Column(db.Integer, default=1)

    # Relaciones con Materiales (Recetas)
    materias_primas = db.relationship('SeCompone', backref='lote', lazy=True)
    colorantes      = db.relationship('SeColorea', backref='lote', lazy=True)
    salidas = db.relationship(
        'LoteSalidaPiezaColor',
        back_populates='lote',
        cascade='all, delete-orphan',
        lazy=True,
    )

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
            'Color':  str(self.color_produccion_rel) if self.color_produccion_rel else "Sin Color",
            'color_hex': (
                self.color_produccion_rel.hex_referencia
                if self.color_produccion_rel
                else None
            ),

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
            'receta_aplicada': (
                {
                    'id': self.receta_color_maestra_id,
                    'revision': self.receta_revision_snapshot,
                    'nombre': self.receta_nombre_snapshot,
                    'base_virgen_kg': (
                        float(self.receta_base_virgen_kg_snapshot)
                        if self.receta_base_virgen_kg_snapshot is not None
                        else None
                    ),
                }
                if self.receta_color_maestra_id is not None
                else None
            ),
            'salidas': [salida.to_dict() for salida in self.salidas],
            'mano_obra': {
                'personas':    self.personas,
                'horas_hombre': self.horas_hombre
            },

            # Alias de compatibilidad (para código que aún lea estos campos)
            'coladas_calculadas': round(self.calculo_coladas, 4),
        }


class LoteSalidaPiezaColor(db.Model):
    """Salida física objetivo de una pieza coloreada dentro de un lote de OP."""

    __tablename__ = 'lote_salida_pieza_color'
    __table_args__ = (
        db.UniqueConstraint(
            'lote_color_id',
            'pieza_id',
            name='uq_lote_salida_lote_pieza',
        ),
        db.CheckConstraint('cavidades_snapshot > 0', name='ck_lote_salida_cavidades'),
        db.CheckConstraint(
            'peso_unitario_snapshot_gr > 0',
            name='ck_lote_salida_peso_unitario',
        ),
        db.CheckConstraint(
            'cantidad_objetivo >= 0 AND kg_objetivo_neto >= 0',
            name='ck_lote_salida_objetivos_no_negativos',
        ),
        db.CheckConstraint(
            'cantidad_buena_real >= 0 AND cantidad_rechazada_real >= 0 '
            'AND kg_bueno_real >= 0',
            name='ck_lote_salida_reales_no_negativos',
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    lote_color_id = db.Column(
        db.Integer,
        db.ForeignKey('lote_color.id', ondelete='CASCADE'),
        nullable=False,
    )
    snapshot_pieza_id = db.Column(
        db.Integer,
        db.ForeignKey('snapshot_composicion_molde.id', ondelete='RESTRICT'),
        nullable=False,
    )
    pieza_id = db.Column(
        db.Integer,
        db.ForeignKey('pieza.id', ondelete='RESTRICT'),
        nullable=False,
    )
    pieza_color_sku = db.Column(
        db.String(50),
        db.ForeignKey('pieza_color.sku', ondelete='RESTRICT'),
        nullable=False,
    )
    cavidades_snapshot = db.Column(db.Integer, nullable=False)
    peso_unitario_snapshot_gr = db.Column(db.Numeric(12, 4), nullable=False)
    cantidad_objetivo = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    kg_objetivo_neto = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    cantidad_buena_real = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    cantidad_rechazada_real = db.Column(db.Numeric(14, 4), nullable=False, default=0)
    kg_bueno_real = db.Column(db.Numeric(14, 4), nullable=False, default=0)

    lote = db.relationship('LoteColor', back_populates='salidas')
    snapshot_pieza = db.relationship('SnapshotComposicionMolde')
    pieza = db.relationship('Pieza')
    pieza_color = db.relationship('PiezaColor')

    def to_dict(self):
        return {
            'id': self.id,
            'lote_color_id': self.lote_color_id,
            'snapshot_pieza_id': self.snapshot_pieza_id,
            'pieza_id': self.pieza_id,
            'pieza_codigo': (
                self.snapshot_pieza.pieza_codigo_snapshot
                if self.snapshot_pieza else None
            ),
            'pieza_nombre': (
                self.snapshot_pieza.pieza_nombre_snapshot
                if self.snapshot_pieza else None
            ),
            'pieza_color_sku': self.pieza_color_sku,
            'cavidades_snapshot': self.cavidades_snapshot,
            'peso_unitario_snapshot_gr': float(self.peso_unitario_snapshot_gr),
            'cantidad_objetivo': float(self.cantidad_objetivo),
            'kg_objetivo_neto': float(self.kg_objetivo_neto),
            'cantidad_buena_real': float(self.cantidad_buena_real),
            'cantidad_rechazada_real': float(self.cantidad_rechazada_real),
            'kg_bueno_real': float(self.kg_bueno_real),
        }
