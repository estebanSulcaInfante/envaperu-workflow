from app.extensions import db
from datetime import datetime, timezone, timedelta


# =============================================================================
# NUEVA ENTIDAD: Snapshot de la composición del molde al crear la OP
# =============================================================================

class SnapshotComposicionMolde(db.Model):
    """
    Congela la configuración de piezas del molde en el momento de crear la OP.
    Una fila por tipo de pieza. Un molde simple tendrá 1 fila; uno multi-pieza, N filas.
    """
    __tablename__ = 'snapshot_composicion_molde'

    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    orden_id     = db.Column(db.String(20), db.ForeignKey('orden_produccion.numero_op'), nullable=False)

    # FK a Pieza (nullable: permite override manual sin pieza registrada en catálogo)
    pieza_sku    = db.Column(db.String(50), db.ForeignKey('pieza.sku'), nullable=True)

    cavidades    = db.Column(db.Integer,  nullable=False, default=1)
    peso_unit_gr = db.Column(db.Float,   nullable=False, default=0.0)

    # Relación de lectura para nombre de pieza
    pieza = db.relationship('Pieza', backref='snapshots_op')

    @property
    def peso_subtotal_gr(self):
        """Peso total de esta pieza en el golpe (cavidades × peso unitario)."""
        return (self.cavidades or 0) * (self.peso_unit_gr or 0.0)

    def to_dict(self):
        return {
            'pieza_sku':    self.pieza_sku,
            'pieza_nombre': self.pieza.piezas if self.pieza else None,
            'cavidades':    self.cavidades,
            'peso_unit_gr': self.peso_unit_gr,
            'peso_subtotal_gr': self.peso_subtotal_gr,
        }


# =============================================================================
# ENTIDAD PRINCIPAL: Orden de Producción
# =============================================================================

class OrdenProduccion(db.Model):
    __tablename__ = 'orden_produccion'

    numero_op      = db.Column(db.String(20), primary_key=True)
    fecha_creacion = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    fecha_inicio   = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    # --- CABECERA ---
    maquina_id  = db.Column(db.Integer, db.ForeignKey('maquina.id'), nullable=True)

    # --- PRODUCTO ---
    producto_sku = db.Column(db.String(50), db.ForeignKey('producto_terminado.cod_sku_pt'), nullable=True)
    producto_ref = db.relationship('ProductoTerminado', backref='ordenes')
    producto     = db.Column(db.String(100))  # nombre legacy / cache visual

    # --- MOLDE ---
    molde    = db.Column(db.String(100))       # nombre legacy
    molde_id = db.Column(db.String(50), db.ForeignKey('molde.codigo'), nullable=True)
    molde_ref = db.relationship('Molde', backref='ordenes_produccion')

    # --- PARÁMETROS TÉCNICOS ---
    snapshot_tiempo_ciclo  = db.Column(db.Float, default=0.0)   # seg por golpe
    snapshot_horas_turno   = db.Column(db.Float, default=24.0)  # horas/día
    snapshot_peso_colada_gr = db.Column(db.Float, default=0.0)  # peso del ramal/runner

    ciclos = db.Column(db.Float, nullable=True)  # ciclos teóricos

    # --- FINANCIERO ---
    tipo_cambio = db.Column(db.Float, nullable=True)  # USD/PEN al crear

    # --- ESTADO ---
    activa = db.Column(db.Boolean, default=True)

    # --- RELACIONES ---
    snapshot_composicion = db.relationship(
        'SnapshotComposicionMolde',
        backref='orden',
        cascade='all, delete-orphan',
        lazy=True
    )
    lotes = db.relationship(
        'LoteColor',
        backref='orden',
        lazy=True,
        cascade='all, delete-orphan'
    )

    # -------------------------------------------------------------------------
    # CÁLCULOS CACHEADOS (persistidos en BD)
    # -------------------------------------------------------------------------
    calculo_peso_tiro_gr     = db.Column(db.Float, default=0.0)  # neto_golpe + colada
    calculo_peso_neto_golpe  = db.Column(db.Float, default=0.0)  # SUM(cav*peso_unit)
    calculo_cavidades_totales = db.Column(db.Integer, default=1)

    calculo_peso_produccion  = db.Column(db.Float, default=0.0)  # SUM(lote.meta_kg)
    calculo_merma_pct        = db.Column(db.Float, default=0.0)

    calculo_peso_inc_merma   = db.Column(db.Float, default=0.0)
    calculo_merma_natural_kg = db.Column(db.Float, default=0.0)

    calculo_horas       = db.Column(db.Float, default=0.0)
    calculo_dias        = db.Column(db.Float, default=0.0)
    calculo_fecha_fin   = db.Column(db.DateTime, nullable=True)

    calculo_colores_activos = db.Column(db.Integer, default=1)
    calculo_familia_color   = db.Column(db.String(50), nullable=True)

    # -------------------------------------------------------------------------
    # PROPIEDADES DERIVADAS (desde snapshot_composicion)
    # -------------------------------------------------------------------------

    @property
    def peso_neto_golpe_gr(self):
        """Suma de (cavidades × peso_unit_gr) de todas las piezas del golpe."""
        return sum(s.peso_subtotal_gr for s in self.snapshot_composicion)

    @property
    def peso_tiro_gr(self):
        """Peso total del golpe: piezas netas + ramal."""
        return self.peso_neto_golpe_gr + (self.snapshot_peso_colada_gr or 0.0)

    @property
    def cavidades_totales(self):
        """Número total de cavidades del golpe."""
        return sum(s.cavidades for s in self.snapshot_composicion) or 1

    @property
    def es_multipieza(self):
        return len(self.snapshot_composicion) > 1

    @property
    def num_colores_activos(self):
        if self.calculo_colores_activos and self.calculo_colores_activos > 0:
            return self.calculo_colores_activos
        count = len(self.lotes)
        return count if count > 0 else 1

    # -------------------------------------------------------------------------
    # ACTUALIZACIÓN DE MÉTRICAS
    # -------------------------------------------------------------------------

    def actualizar_metricas(self):
        """
        Recalcula y persiste todos los valores calculo_*.
        Debe llamarse siempre que cambien datos técnicos de la OP.
        Dispara en cascada a LoteColor hijos.
        """
        # 0. Composición del golpe (desde snapshot)
        peso_neto   = self.peso_neto_golpe_gr
        peso_colada = self.snapshot_peso_colada_gr or 0.0
        peso_tiro   = peso_neto + peso_colada

        self.calculo_peso_neto_golpe   = peso_neto
        self.calculo_peso_tiro_gr      = peso_tiro
        self.calculo_cavidades_totales = self.cavidades_totales

        # 0b. Colores activos
        count = len(self.lotes)
        self.calculo_colores_activos = count if count > 0 else 1

        # 1. PESO PRODUCCIÓN = suma de meta_kg de cada lote
        self.calculo_peso_produccion = sum((l.meta_kg or 0.0) for l in self.lotes)
        peso_total_kg = self.calculo_peso_produccion

        # 2. MERMA (solo colada / runner)
        merma_pct = 0.0
        if peso_tiro > 0:
            merma_pct = (peso_tiro - peso_neto) / peso_tiro
        self.calculo_merma_pct = merma_pct

        # 3. MERMA NATURAL en kg
        peso_inc_merma = peso_total_kg * (1 + merma_pct) if peso_tiro > 0 else 0.0
        self.calculo_peso_inc_merma  = peso_inc_merma
        self.calculo_merma_natural_kg = peso_inc_merma - peso_total_kg

        # 4. TIEMPOS
        horas = 0.0
        dias  = 0.0
        if peso_tiro > 0 and self.snapshot_tiempo_ciclo:
            golpes   = (peso_total_kg * 1000) / peso_tiro
            segundos = golpes * self.snapshot_tiempo_ciclo
            horas    = segundos / 3600
            if self.snapshot_horas_turno and self.snapshot_horas_turno > 0:
                dias = horas / self.snapshot_horas_turno

        self.calculo_horas = horas
        self.calculo_dias  = dias

        fecha_fin = None
        if self.fecha_inicio and dias > 0:
            fecha_fin = self.fecha_inicio + timedelta(days=dias)
        self.calculo_fecha_fin = fecha_fin

        # 5. FAMILIA COLOR (cache desde Producto)
        self.calculo_familia_color = None
        if self.producto_ref:
            if self.producto_ref.familia_color_rel:
                self.calculo_familia_color = self.producto_ref.familia_color_rel.nombre
            elif self.producto_ref.familia_color:
                self.calculo_familia_color = self.producto_ref.familia_color

        # CASCADE a Lotes
        for lote in self.lotes:
            lote.actualizar_metricas(contexto_orden=self)

    # -------------------------------------------------------------------------
    # SERIALIZADORES
    # -------------------------------------------------------------------------

    @property
    def resumen_totales(self):
        """Lee los valores persistidos (llamar actualizar_metricas() antes si hubo cambios)."""
        return {
            'Peso(Kg) PRODUCCION':    self.calculo_peso_produccion or 0.0,
            'Peso (Kg) Inc. Merma':   self.calculo_peso_inc_merma or 0.0,
            '%Merma':                 self.calculo_merma_pct or 0.0,
            'Merma Natural Kg':       self.calculo_merma_natural_kg or 0.0,
            'Horas':                  self.calculo_horas or 0.0,
            'Días':                   self.calculo_dias or 0.0,
            'F. Fin':                 self.calculo_fecha_fin.isoformat() if self.calculo_fecha_fin else None,
            'Familia Color':          self.calculo_familia_color,
        }

    def to_dict(self):
        avance_real_kg     = sum(r.total_kg_real or 0.0 for r in self.registros_diarios)
        avance_real_coladas = sum(r.total_coladas_calculada or 0 for r in self.registros_diarios)

        return {
            'numero_op':    self.numero_op,
            'producto':     self.producto,
            'maquina':      self.maquina_ref.nombre if self.maquina_ref else None,
            'tipo_maquina': self.maquina_ref.tipo   if self.maquina_ref else None,
            'fecha':        self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_inicio': self.fecha_inicio.isoformat()   if self.fecha_inicio   else None,
            'molde':        self.molde,
            'activa':       self.activa,

            # Snapshot técnico completo
            'snapshot_tecnico': {
                'tiempo_ciclo_seg':    self.snapshot_tiempo_ciclo,
                'horas_turno':         self.snapshot_horas_turno,
                'peso_colada_gr':      self.snapshot_peso_colada_gr,
                'es_multipieza':       self.es_multipieza,
                'peso_neto_golpe_gr':  self.calculo_peso_neto_golpe,
                'peso_tiro_gr':        self.calculo_peso_tiro_gr,
                'cavidades_totales':   self.calculo_cavidades_totales,
                'composicion': [s.to_dict() for s in self.snapshot_composicion],
            },

            'lotes':           [lote.to_dict() for lote in self.lotes],
            'resumen_totales': self._round_dict(self.resumen_totales),

            'avance_real_kg':      round(avance_real_kg, 2),
            'avance_real_coladas': avance_real_coladas,
        }

    def _round_dict(self, data):
        rounded = {}
        for k, v in data.items():
            if isinstance(v, float):
                rounded[k] = round(v, 4) if ('%' in k or ('Merma' in k and v < 1)) else round(v, 2)
            else:
                rounded[k] = v
        return rounded