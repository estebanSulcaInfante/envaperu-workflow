from app.extensions import db
import math

class LoteColor(db.Model):
    __tablename__ = 'lote_color'

    id = db.Column(db.Integer, primary_key=True)
    
    # Relación con el Padre
    orden_id = db.Column(db.Integer, db.ForeignKey('orden_produccion.id'), nullable=False)
    color_nombre = db.Column(db.String(50), nullable=False)

    # --- INPUT MANUAL (Solo para estrategia STOCK) ---
    stock_kg_manual = db.Column(db.Float, nullable=True)
    
    # --- MANO DE OBRA ---
    personas = db.Column(db.Integer, default=1)

    # Relaciones con Materiales (Recetas)
    materias_primas = db.relationship('SeCompone', backref='lote', lazy=True)
    colorantes = db.relationship('SeColorea', backref='lote', lazy=True)

    # -------------------------------------------------------------------------
    # 1. CEREBRO POLIMÓRFICO (Replica filas 13 del Excel: C, D, E)
    # -------------------------------------------------------------------------
    @property
    def valores_polimorficos(self):
        """
        Determina qué columna (C, D, E) tiene valor según la estrategia.
        Retorna también el peso base en KG para cálculos internos.
        """
        # Si no hay orden asociada, retornamos vacíos
        if not self.orden:
            return {}

        estrategia = self.orden.tipo_estrategia or "POR_PESO"
        
        # Variables visuales (Solo una tendrá valor, las otras None)
        col_cantidad_kg = None # Visual "Por Cantidad" (Col C) - Valor en KG
        col_peso_kg = None     # Visual "Por Peso" (Col D)
        col_stock_kg = None    # Visual "Stock" (Col E)
        
        # Peso base para la máquina (Lo que se va a inyectar realmente antes de extras)
        peso_base = 0.0
        
        # Evitar división por cero
        n_colores = self.orden.num_colores_activos

        # --- CASO A: POR CANTIDAD (Docenas -> Kilos Repartidos) ---
        if estrategia == 'POR_CANTIDAD':
            # Datos del Padre
            meta_doc = self.orden.meta_total_doc or 0.0
            peso_u = self.orden.peso_unitario_gr or 0.0
            
            # Fórmula C13 Excel: (MetaDoc * 12 * P.Unit / 1000) / NroColores
            kilos_totales_teoricos = (meta_doc * 12 * peso_u) / 1000
            
            valor_calculado = kilos_totales_teoricos / n_colores
            col_cantidad_kg = valor_calculado
            peso_base = valor_calculado

        # --- CASO B: POR PESO (Kilos Repartidos) ---
        elif estrategia == 'POR_PESO':
            meta_kg = self.orden.meta_total_kg or 0.0
            
            # Fórmula D13 Excel: MetaKg / NroColores
            valor_calculado = meta_kg / n_colores
            col_peso_kg = valor_calculado
            peso_base = valor_calculado

        # --- CASO C: STOCK (Input Manual) ---
        elif estrategia == 'STOCK':
            # Fórmula E13 Excel: Directo del input manual del Lote
            valor_manual = self.stock_kg_manual or 0.0
            col_stock_kg = valor_manual
            peso_base = valor_manual

        return {
            'col_C': col_cantidad_kg,
            'col_D': col_peso_kg,
            'col_E': col_stock_kg,
            'peso_base': peso_base
        }

    # -------------------------------------------------------------------------
    # 2. CÁLCULOS DERIVADOS (Coladas, Extras)
    # -------------------------------------------------------------------------
    @property
    def peso_total_objetivo(self):
        """Devuelve los Kg base del lote, venga de donde venga (C, D o E)."""
        return self.valores_polimorficos.get('peso_base', 0.0)

    @property
    def extra_kg_asignado(self):
        """Calcula la parte proporcional del desperdicio asignada a este lote."""
        if not self.orden: 
            return 0.0
            
        resumen = self.orden.resumen_totales
        # Usamos safely get, aunque resumen_totales devuelve keys constantes
        total_extra_orden = resumen.get('EXTRA', 0.0)
        n_colores = self.orden.num_colores_activos
        return total_extra_orden / n_colores

    @property
    def cantidad_coladas_calculada(self):
        """
        Calcula coladas usando el peso dinámico + el extra asignado.
        Replica la lógica de Columnas G y H del Excel.
        """
        if not self.orden or not self.orden.peso_inc_colada or self.orden.peso_inc_colada == 0:
            return 0
        
        peso_puro = self.peso_total_objetivo
        extra_kg_lote = self.extra_kg_asignado # Refactored to property
        
        peso_total_maquina = peso_puro + extra_kg_lote
        coladas = (peso_total_maquina * 1000) / self.orden.peso_inc_colada
        
        return math.ceil(coladas)

    @property
    def horas_hombre(self):
        # Fórmula: (TotalDias * HorasTurno * #Personas) / #Colores
        if not self.orden: return 0.0
        
        # Necesitamos 'TotalDias' del resumen
        resumen = self.orden.resumen_totales
        dias = resumen.get('Días', 0.0) # Ojo con la tilde
        horas_turno = self.orden.horas_turno or 24.0
        n_colores = self.orden.num_colores_activos
        
        return (dias * horas_turno * self.personas) / n_colores

    def to_dict(self):
        vals = self.valores_polimorficos
        
        # Cálculos para JSON report
        peso_base = vals['peso_base']
        extra_kg = self.extra_kg_asignado
        total_mas_extra = peso_base + extra_kg # TOTAL + EXTRA (Kg)
        
        return {
            'id': self.id,
            'Color': self.color_nombre,
            
            # --- VISTA POLIMÓRFICA (Frontend decide cuál pintar) ---
            'Por Cantidad (Kg)': vals['col_C'],  
            'Peso (Kg)': vals['col_D'],  
            'Stock (Kg)': vals['col_E'],
            
            # --- NUEVOS CAMPOS (Informe MD) ---
            'Extra (Kg)': round(extra_kg, 2),
            'TOTAL + EXTRA (Kg)': round(total_mas_extra, 2),
            
            # --- LISTAS DINÁMICAS ---
            'materiales': [
                {
                    'nombre': m.materia.nombre,
                    'tipo': m.materia.tipo,
                    'fraccion': m.fraccion,
                    'peso_kg': round(m.peso_kg, 2)
                } for m in self.materias_primas
            ],
            
            'pigmentos': [
                {
                    'nombre': p.pigmento.nombre,
                    'dosis_gr': p.gramos
                } for p in self.colorantes
            ],
            
            'mano_obra': {
                'personas': self.personas,
                'horas_hombre': round(self.horas_hombre, 2)
            },

            # --- RESULTADOS TÉCNICOS ---
            'coladas_calculadas': self.cantidad_coladas_calculada
        }