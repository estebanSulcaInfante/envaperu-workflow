from app.extensions import db
import math

class LoteColor(db.Model):
    __tablename__ = 'lote_color'

    id = db.Column(db.Integer, primary_key=True)
    
    # Relación con el Padre
    numero_op = db.Column(db.String(20), db.ForeignKey('orden_produccion.numero_op'), nullable=False)
    
    # --- COLOR REAL (Refactor: FK a Tabla Colores) ---
    color_id = db.Column(db.Integer, db.ForeignKey('color_producto.id'), nullable=True)
    
    # --- SKU SALIDA (Refactor: Link a Inventario) ---
    producto_sku_output = db.Column(db.String(50), db.ForeignKey('producto_terminado.cod_sku_pt'), nullable=True)
    
    # Relaciones
    color_rel = db.relationship('ColorProducto', backref='lotes')
    producto_output = db.relationship('ProductoTerminado', foreign_keys=[producto_sku_output], backref='lotes_produccion')

    # --- INPUT MANUAL (Solo para estrategia STOCK) ---
    stock_kg_manual = db.Column(db.Float, nullable=True)
    
    # --- MANO DE OBRA ---
    personas = db.Column(db.Integer, default=1)

    # Relaciones con Materiales (Recetas)
    materias_primas = db.relationship('SeCompone', backref='lote', lazy=True)
    colorantes = db.relationship('SeColorea', backref='lote', lazy=True)

    # -------------------------------------------------------------------------
    # PERSISTENCIA DE CÁLCULOS
    # -------------------------------------------------------------------------
    calculo_peso_base = db.Column(db.Float, default=0.0)
    calculo_col_c = db.Column(db.Float, nullable=True)
    calculo_col_d = db.Column(db.Float, nullable=True)
    calculo_col_e = db.Column(db.Float, nullable=True)
    
    calculo_extra_kg = db.Column(db.Float, default=0.0)
    calculo_coladas = db.Column(db.Float, default=0.0)
    calculo_horas_hombre = db.Column(db.Float, default=0.0)

    # -------------------------------------------------------------------------
    # MÉTODO DE ACTUALIZACIÓN (REEMPLAZA @property valores_polimorficos)
    # -------------------------------------------------------------------------
    def actualizar_metricas(self, contexto_orden=None):
        """
        Recalcula métricas del lote usando el contexto de la orden (para evitar queries lazy).
        Luego cascada a sus recetas.
        """
        orden_padre = contexto_orden or self.orden
        if not orden_padre:
            return 

        # 1. VALORES POLIMÓRFICOS
        estrategia = orden_padre.tipo_estrategia or "POR_PESO"
        
        # Reset
        self.calculo_col_c = None
        self.calculo_col_d = None
        self.calculo_col_e = None
        peso_base = 0.0
        
        # Leemos el numero de colores YA CALCULADO de la orden
        n_colores = orden_padre.calculo_colores_activos
        if not n_colores or n_colores < 1: n_colores = 1

        # --- CASO A: POR CANTIDAD ---
        if estrategia == 'POR_CANTIDAD':
            # Usamos los valores cacheados de la orden si existen, sino recalculamos lo minimo
            # Pero la idea es que la orden ya se calculo antes de llamar a esto.
            # calculo_peso_produccion tiene el TOTAL KG
            kilos_totales = orden_padre.calculo_peso_produccion
            
            valor_calculado = kilos_totales / n_colores
            self.calculo_col_c = valor_calculado
            peso_base = valor_calculado

        # --- CASO B: POR PESO (Kilos Repartidos) ---
        elif estrategia == 'POR_PESO':
            kilos_totales = orden_padre.calculo_peso_produccion
            # En POR_PESO, meta_total_kg es directo, pero orden.calculo_peso_produccion ya lo tiene.
            
            valor_calculado = kilos_totales / n_colores
            self.calculo_col_d = valor_calculado
            peso_base = valor_calculado

        # --- CASO C: STOCK (Input Manual) ---
        elif estrategia == 'STOCK':
            valor_manual = self.stock_kg_manual or 0.0
            self.calculo_col_e = valor_manual
            peso_base = valor_manual

        self.calculo_peso_base = peso_base
        
        # 2. EXTRA KG ASIGNADO
        # Proporcional: TotalExtraOrden / NColores
        total_extra = orden_padre.calculo_extra_kg or 0.0
        self.calculo_extra_kg = total_extra / n_colores
        
        # 3. COLADAS
        if orden_padre.snapshot_peso_inc_colada and orden_padre.snapshot_peso_inc_colada > 0:
            peso_total_maquina = peso_base + self.calculo_extra_kg
            peso_neto_tiro = orden_padre.snapshot_peso_unitario_gr * orden_padre.snapshot_cavidades
            
            if peso_neto_tiro > 0:
                self.calculo_coladas = (peso_total_maquina * 1000) / peso_neto_tiro
            else:
                self.calculo_coladas = 0.0
        else:
            self.calculo_coladas = 0.0
            
        # 4. HORAS HOMBRE
        # HH = (Dias * HorasTurno * Personas) / #Colores
        dias_orden = orden_padre.calculo_dias or 0.0
        horas_turno = orden_padre.snapshot_horas_turno or 24.0
        self.calculo_horas_hombre = (dias_orden * horas_turno * self.personas) / n_colores

        # --- CASCADE TO RECIPES ---
        for receta in self.materias_primas:
            receta.actualizar_metricas(contexto_lote=self)
            
        # Nota: SeColorea (Pigmentos) es input manual en gramos, ¿necesita calculo?
        # Revisando reglas: SeColorea tiene 'gramos' directo. No parece depender de % o totales dinamicos.
        # "Dosis por bolsa??" - En tu codigo actual es un input directo.
        # Lo dejamos asi por ahora.


    # -------------------------------------------------------------------------
    # PROPIEDADES DE LECTURA (Facades para compatibilidad o comodidad)
    # -------------------------------------------------------------------------
    @property
    def valores_polimorficos(self):
        """Retorna dict leido de columnas persistidas."""
        return {
            'col_C': self.calculo_col_c,
            'col_D': self.calculo_col_d,
            'col_E': self.calculo_col_e,
            'peso_base': self.calculo_peso_base
        }

    # -------------------------------------------------------------------------
    # 2. CÁLCULOS DERIVADOS (Coladas, Extras)
    # -------------------------------------------------------------------------
    @property
    def peso_total_objetivo(self):
        """Devuelve los Kg base del lote, venga de donde venga (C, D o E)."""
        return self.calculo_peso_base or 0.0

    @property
    def extra_kg_asignado(self):
        """Calcula la parte proporcional del desperdicio asignada a este lote."""
        return self.calculo_extra_kg or 0.0

    @property
    def cantidad_coladas_calculada(self):
        return self.calculo_coladas or 0.0

    @property
    def horas_hombre(self):
        return self.calculo_horas_hombre or 0.0

    def to_dict(self):
        vals = self.valores_polimorficos
        
        # Cálculos para JSON report
        peso_base = vals['peso_base']
        extra_kg = self.extra_kg_asignado
        total_mas_extra = peso_base + extra_kg # TOTAL + EXTRA (Kg)
        
        return {
            'id': self.id,
            'Color': self.color_rel.nombre if self.color_rel else "Sin Color",
            
            # --- VISTA POLIMÓRFICA (Frontend decide cuál pintar) ---
            'Por Cantidad (Kg)': vals['col_C'],  
            'Peso (Kg)': vals['col_D'],  
            'Stock (Kg)': vals['col_E'],
            
            # --- NUEVOS CAMPOS (Informe MD) ---
            'Extra (Kg)': extra_kg,
            'TOTAL + EXTRA (Kg)': total_mas_extra,
            
            # --- LISTAS DINÁMICAS ---
            'materiales': [
                {
                    'nombre': m.materia.nombre,
                    'tipo': m.materia.tipo,
                    'fraccion': m.fraccion,
                    'peso_kg': m.peso_kg
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
                'horas_hombre': self.horas_hombre
            },

            # --- RESULTADOS TÉCNICOS ---
            'coladas_calculadas': self.cantidad_coladas_calculada
        }