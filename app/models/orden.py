from app.extensions import db
from datetime import datetime, timezone, timedelta

class OrdenProduccion(db.Model):
    __tablename__ = 'orden_produccion'

    numero_op = db.Column(db.String(20), primary_key=True)
    fecha_creacion = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    fecha_inicio = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    # --- CABECERA ---
    # FK a la entidad Maquina (preparado para Fase 2: Registro Diario)
    maquina_id = db.Column(db.Integer, db.ForeignKey('maquina.id'), nullable=True)
    # maquina_ref backref es definido en Maquina.ordenes

    # --- RELACION A PRODUCTO ---
    # Vinculo fuerte por SKU para sacar datos tecnicos (familia, color, pesos teoricos)
    producto_sku = db.Column(db.String(50), db.ForeignKey('producto_terminado.cod_sku_pt'), nullable=True)
    producto_ref = db.relationship('ProductoTerminado', backref='ordenes')

    producto = db.Column(db.String(100)) # Nombre (Legacy o Cache Visual)
    molde = db.Column(db.String(100))
    
    # --- PARAMETROS TÉCNICOS ---
    peso_unitario_gr = db.Column(db.Float, default=0.0) 
    peso_inc_colada = db.Column(db.Float, default=0.0)
    cavidades = db.Column(db.Integer, default=1)
    
    tiempo_ciclo = db.Column(db.Float, default=0.0) 
    ciclos = db.Column(db.Float) 
    horas_turno = db.Column(db.Float, default=24.0) 

    # --- ESTRATEGIA (STI) ---
    tipo_estrategia = db.Column(db.String(20), nullable=False)
    meta_total_kg = db.Column(db.Float, nullable=True)
    meta_total_doc = db.Column(db.Float, nullable=True)
    
    # T/C: Tipo de Cambio al momento de crear la orden
    tipo_cambio = db.Column(db.Float, nullable=True)
    
    # Estado de la orden (True = abierta para registros, False = cerrada)
    activa = db.Column(db.Boolean, default=True)

    # Relaciones
    # Relaciones
    lotes = db.relationship('LoteColor', backref='orden', lazy=True, cascade="all, delete-orphan")

    # -------------------------------------------------------------------------
    # PERSISTENCIA DE CÁLCULOS (Valores cacheados en DB)
    # -------------------------------------------------------------------------
    calculo_peso_produccion = db.Column(db.Float, default=0.0)
    calculo_merma_pct = db.Column(db.Float, default=0.0)
    calculo_extra_pct = db.Column(db.Float, default=0.0)
    calculo_extra_kg = db.Column(db.Float, default=0.0)
    calculo_peso_real_entregar = db.Column(db.Float, default=0.0)
    
    # NUEVOS CAMPOS (Informe MD)
    calculo_peso_inc_merma = db.Column(db.Float, default=0.0)
    calculo_merma_natural_kg = db.Column(db.Float, default=0.0)
    calculo_total_doc = db.Column(db.Float, default=0.0)

    # Tiempos
    calculo_horas = db.Column(db.Float, default=0.0)
    calculo_dias = db.Column(db.Float, default=0.0)
    calculo_fecha_fin = db.Column(db.DateTime, nullable=True)

    # Auxiliares
    calculo_colores_activos = db.Column(db.Integer, default=1)
    calculo_familia_color = db.Column(db.String(50), nullable=True) # Cache de Familia Color

    # -------------------------------------------------------------------------
    # NUEVA PROPIEDAD: Contar Colores para la División
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # PROPIEDAD: Contar Colores (Lectura directa o calculada)
    # -------------------------------------------------------------------------
    @property
    def num_colores_activos(self):
        """Devuelve el valor calculado persistido si existe, sino cuenta al vuelo."""
        if self.calculo_colores_activos and self.calculo_colores_activos > 0:
            return self.calculo_colores_activos
        count = len(self.lotes)
        return count if count > 0 else 1

    # -------------------------------------------------------------------------
    # CÁLCULOS GLOBALES (Resumen Totales)
    # -------------------------------------------------------------------------
    # -------------------------------------------------------------------------
    # MÉTODO DE ACTUALIZACIÓN (REEMPLAZA @property resumen_totales)
    # -------------------------------------------------------------------------
    def actualizar_metricas(self):
        """
        Calcula todos los valores y los guarda en las columnas 'calculo_...'.
        Tambien dispara la actualizacion en cascada a los Lotes hijos.
        """
        # Valores seguros
        p_inc_colada = self.peso_inc_colada or 0.0
        p_unit = self.peso_unitario_gr or 0.0
        cavs = self.cavidades or 1
        
        # 0. Actualizar contador de colores
        count = len(self.lotes)
        self.calculo_colores_activos = count if count > 0 else 1

        # 1. MERMA
        merma_pct = 0.0
        if p_inc_colada > 0:
            peso_real_piezas = p_unit * cavs
            merma_pct = (p_inc_colada - peso_real_piezas) / p_inc_colada

        self.calculo_merma_pct = merma_pct

        # 2. % EXTRA (Reglas de Negocio)
        if round(merma_pct, 4) < 0.05:
            extra_pct_calc = merma_pct
        elif round(merma_pct, 4) <= 0.10:
            extra_pct_calc = merma_pct * 0.5
        else:
            extra_pct_calc = 0.0
            
        self.calculo_extra_pct = extra_pct_calc

        # 3. BASE DE PRODUCCIÓN
        peso_total_kg = 0.0
        cantidad_docenas = 0.0
        tipo = self.tipo_estrategia or "POR_PESO"

        if tipo == "POR_PESO":
            peso_total_kg = self.meta_total_kg or 0.0
            if p_unit > 0:
                cantidad_docenas = (peso_total_kg * 1000) / p_unit / 12
            
        elif tipo == "POR_CANTIDAD":
            cantidad_docenas = self.meta_total_doc or 0.0
            peso_total_kg = (cantidad_docenas * 12 * p_unit) / 1000
            
        elif tipo == "STOCK":
            # Suma de stocks manuales de los lotes
            # IMPORTANTE: Los lotes deben tener sus valores actualizados antes o leemos directos
            total_lotes_kg = sum((l.stock_kg_manual or 0.0) for l in self.lotes)
            peso_total_kg = total_lotes_kg
            if p_unit > 0:
                cantidad_docenas = (peso_total_kg * 1000) / p_unit / 12

        self.calculo_peso_produccion = peso_total_kg
        self.calculo_total_doc = round(cantidad_docenas, 0) # Guardamos redondeado o raw? Mejor raw si se usa para calculos, pero aqui segun logica anterior
        # Ajuste: guardaremos cantidad_docenas raw si quisiéramos exactitud, pero el requerimiento anterior usaba total_doc redondeado visualmente.
        # Guardemos el raw en una variable local por si acaso, pero persistimos lo relevante.
        
        # 4. TOTAL EXTRA (KG)
        extra_kg = peso_total_kg * extra_pct_calc
        self.calculo_extra_kg = extra_kg
        
        # 5. TOTAL A MÁQUINA
        peso_real_entregar = peso_total_kg + extra_kg
        self.calculo_peso_real_entregar = peso_real_entregar
        
        # --- NUEVOS CÁLCULOS (Informe MD) ---
        peso_inc_merma = peso_total_kg * (1 + merma_pct) if p_inc_colada > 0 else 0.0
        self.calculo_peso_inc_merma = peso_inc_merma
        
        self.calculo_merma_natural_kg = peso_inc_merma - peso_total_kg
        
        # 6. TIEMPOS ESTIMADOS
        horas = 0.0
        dias = 0.0
        if p_inc_colada > 0 and self.tiempo_ciclo:
            golpes = (peso_real_entregar * 1000) / p_inc_colada
            segundos = golpes * self.tiempo_ciclo
            horas = segundos / 3600
            if self.horas_turno and self.horas_turno > 0:
                dias = horas / self.horas_turno
        
        self.calculo_horas = horas
        self.calculo_dias = dias
        
        # F. Fin
        fecha_fin = None
        if self.fecha_inicio and dias > 0:
             fecha_fin = self.fecha_inicio + timedelta(days=dias)
        # F. Fin
        fecha_fin = None
        if self.fecha_inicio and dias > 0:
             fecha_fin = self.fecha_inicio + timedelta(days=dias)
        self.calculo_fecha_fin = fecha_fin

        # 7. FAMILIA COLOR (Cache desde Producto)
        self.calculo_familia_color = None
        if self.producto_ref:
            # Opción A: sacar de relación ColorProducto -> Familia
            # Opción B: sacar de columna legacy si no hay relación
            if self.producto_ref.color_rel and self.producto_ref.color_rel.familia:
                self.calculo_familia_color = self.producto_ref.color_rel.familia.nombre
            elif self.producto_ref.familia_color:
                self.calculo_familia_color = self.producto_ref.familia_color

        # --- CASCADE UPDATE TO CHILDREN (LOTES) ---

        # --- CASCADE UPDATE TO CHILDREN (LOTES) ---
        # Pasamos "self" como contexto para no tener que hacer queries circulares o lazy loads innecesarios
        for lote in self.lotes:
            lote.actualizar_metricas(contexto_orden=self)


    @property
    def resumen_totales(self):
        """
        Retorna el diccionario esperado por el frontend leyendo los valores CALCULADOS PERSISTIDOS.
        SIEMPRE LLAMAR A `actualizar_metricas()` ANTES SI SE HAN CAMBIADO DATOS.
        """
        return {
            'Peso(Kg) PRODUCCION': self.calculo_peso_produccion or 0.0,
            'Peso (Kg) Inc. Merma': self.calculo_peso_inc_merma or 0.0,
            '%Merma': self.calculo_merma_pct or 0.0,
            'Merma Natural Kg': self.calculo_merma_natural_kg or 0.0,
            
            # Recalculamos docenas exactas inversa si es necesario, o usamos el guardado
            # En la version anterior 'Cantidad DOC' era float exacto, 'Total DOC' redondeado.
            # Aqui podemos derivarlo o guardarlo. 
            'Cantidad DOC': (self.calculo_peso_produccion * 1000 / (self.peso_unitario_gr * 12)) if self.peso_unitario_gr and self.peso_unitario_gr > 0 else 0.0,
            'Total DOC': self.calculo_total_doc or 0.0,
            
            '% EXTRA': self.calculo_extra_pct or 0.0,
            'EXTRA': self.calculo_extra_kg or 0.0,
            'Peso Kg REAL PARA ENTREGAR A MAQUINA': self.calculo_peso_real_entregar or 0.0, 
            'Peso REAL A ENTREGAR': self.calculo_peso_real_entregar or 0.0, # Alias
            
            'Horas': self.calculo_horas or 0.0,
            'Días': self.calculo_dias or 0.0,
            'Horas': self.calculo_horas or 0.0,
            'Días': self.calculo_dias or 0.0,
            'F. Fin': self.calculo_fecha_fin.isoformat() if self.calculo_fecha_fin else None,
            
            # Info Extra Reporte
            'Familia Color': self.calculo_familia_color
        }

    def to_dict(self):
        return {
            'numero_op': self.numero_op,
            'producto': self.producto,
            'maquina': self.maquina_ref.nombre if self.maquina_ref else None,
            'tipo_maquina': self.maquina_ref.tipo if self.maquina_ref else None,
            'fecha': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_inicio': self.fecha_inicio.isoformat() if self.fecha_inicio else None,
            'molde': self.molde,
            'cavidades': self.cavidades,
            'peso_tiro': self.peso_inc_colada,
            'ciclo_seg': self.tiempo_ciclo,
            'tipo': self.tipo_estrategia,
            'meta_kg': self.meta_total_kg,
            'activa': self.activa,
            'lotes': [lote.to_dict() for lote in self.lotes],
            'resumen_totales': self._round_dict(self.resumen_totales)
        }

    def _round_dict(self, data):
        """Redondea los valores del diccionario para presentación (Frontend)."""
        rounded = {}
        for k, v in data.items():
            if isinstance(v, float):
                # Porcentajes a 4 decimales, resto a 2
                if '%' in k or 'Merma' in k and v < 1: 
                    rounded[k] = round(v, 4)
                else:
                    rounded[k] = round(v, 2)
            else:
                rounded[k] = v
        return rounded