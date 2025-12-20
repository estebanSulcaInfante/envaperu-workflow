from app.extensions import db
from datetime import datetime, timezone, timedelta

class OrdenProduccion(db.Model):
    __tablename__ = 'orden_produccion'

    numero_op = db.Column(db.String(20), primary_key=True)
    fecha_creacion = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    fecha_inicio = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    
    # --- CABECERA ---
    maquina_id = db.Column(db.String(50))
    tipo_maquina = db.Column(db.String(50))
    producto = db.Column(db.String(100))
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

    # Relaciones
    lotes = db.relationship('LoteColor', backref='orden', lazy=True, cascade="all, delete-orphan")

    # -------------------------------------------------------------------------
    # NUEVA PROPIEDAD: Contar Colores para la División
    # -------------------------------------------------------------------------
    @property
    def num_colores_activos(self):
        """Cuenta cuántos lotes tiene la orden. Evita división por cero."""
        count = len(self.lotes)
        return count if count > 0 else 1

    # -------------------------------------------------------------------------
    # CÁLCULOS GLOBALES (Resumen Totales)
    # -------------------------------------------------------------------------
    @property
    def resumen_totales(self):
        """
        Calcula la tabla auxiliar completa.
        """
        # Valores seguros
        p_inc_colada = self.peso_inc_colada or 0.0
        p_unit = self.peso_unitario_gr or 0.0
        cavs = self.cavidades or 1
        
        # 1. MERMA
        merma_pct = 0.0
        if p_inc_colada > 0:
            peso_real_piezas = p_unit * cavs
            merma_pct = (p_inc_colada - peso_real_piezas) / p_inc_colada

        # 2. % EXTRA (Reglas de Negocio)
        # < 5% -> 100% de la merma
        # 5-10% -> 50% de la merma
        # > 10% -> 0%
        if round(merma_pct, 4) < 0.05:
            extra_pct_calc = merma_pct
        elif round(merma_pct, 4) <= 0.10:
            extra_pct_calc = merma_pct * 0.5
        else:
            extra_pct_calc = 0.0

        # 3. BASE DE PRODUCCIÓN
        peso_produccion_kg = 0.0
        cantidad_docenas = 0.0
        tipo = self.tipo_estrategia or "POR_PESO"

        if tipo == "POR_PESO":
            peso_produccion_kg = self.meta_total_kg or 0.0
            if p_unit > 0:
                cantidad_docenas = (peso_produccion_kg * 1000) / p_unit / 12
            
        elif tipo == "POR_CANTIDAD":
            cantidad_docenas = self.meta_total_doc or 0.0
            peso_produccion_kg = (cantidad_docenas * 12 * p_unit) / 1000
            
        elif tipo == "STOCK":
            # Aquí usamos el nuevo campo del Lote (stock_kg_manual)
            # Ojo: LoteColor tendrá 'stock_kg_manual', debemos sumar ese.
            total_lotes_kg = sum((l.stock_kg_manual or 0.0) for l in self.lotes)
            peso_produccion_kg = total_lotes_kg
            if p_unit > 0:
                cantidad_docenas = (peso_produccion_kg * 1000) / p_unit / 12

        # 4. TOTAL EXTRA (KG)
        extra_kg = peso_produccion_kg * extra_pct_calc
        
        # 5. TOTAL A MÁQUINA
        peso_real_entregar = peso_produccion_kg + extra_kg
        
        # --- NUEVOS CÁLCULOS (Informe MD) ---
        # Peso (Kg) Inc. Merma: Produccion * (1 + %Merma)
        peso_inc_merma = peso_produccion_kg * (1 + merma_pct) if p_inc_colada > 0 else 0.0
        
        # Merma Natural Kg: PesoIncMerma - PesoProduccion
        merma_natural_kg = peso_inc_merma - peso_produccion_kg
        
        # Total DOC: Redondeo visual
        total_doc = round(cantidad_docenas, 0)
        
        # 6. TIEMPOS ESTIMADOS
        horas = 0.0
        dias = 0.0
        if p_inc_colada > 0 and self.tiempo_ciclo:
            # Golpes = (KgTotal * 1000) / PesoTiro
            golpes = (peso_real_entregar * 1000) / p_inc_colada
            # Días = (TotalGolpes * Ciclo) / 3600 / HorasTurno
            segundos = golpes * self.tiempo_ciclo
            horas = segundos / 3600
            if self.horas_turno and self.horas_turno > 0:
                dias = horas / self.horas_turno
        
        # F. Fin: WORKDAY(FechaInicio, Dias) -> Aprox: Sumar dias naturales
        fecha_fin = None
        if self.fecha_inicio and dias > 0:
             # Se usa timedelta. Para dias laborales exactos se requeriria logica extra.
             fecha_fin = self.fecha_inicio + timedelta(days=dias)

        return {
            'Peso(Kg) PRODUCCION': peso_produccion_kg,
            'Peso (Kg) Inc. Merma': peso_inc_merma, # Nuevo
            '%Merma': merma_pct,
            'Merma Natural Kg': merma_natural_kg,   # Nuevo
            
            'Cantidad DOC': cantidad_docenas,
            'Total DOC': total_doc,                           # Nuevo
            
            '% EXTRA': extra_pct_calc,
            'EXTRA': extra_kg,
            'Peso Kg REAL PARA ENTREGAR A MAQUINA': peso_real_entregar, 
            'Peso REAL A ENTREGAR': peso_real_entregar,
            
            'Horas': horas,
            'Días': dias, # Markdown dice "Días" con tilde
            'F. Fin': fecha_fin.isoformat() if fecha_fin else None # Nuevo
        }

    def to_dict(self):
        return {
            'numero_op': self.numero_op,
            'producto': self.producto,
            'maquina': self.maquina_id,
            'fecha': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'fecha_inicio': self.fecha_inicio.isoformat() if self.fecha_inicio else None,
            'molde': self.molde,
            'cavidades': self.cavidades,
            'peso_tiro': self.peso_inc_colada,
            'ciclo_seg': self.tiempo_ciclo,
            'tipo': self.tipo_estrategia,
            'meta_kg': self.meta_total_kg,
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