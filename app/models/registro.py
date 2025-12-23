from app.extensions import db
from datetime import datetime
from app.models.maquina import Maquina  # Import for relationship resolution

class RegistroDiarioProduccion(db.Model):
    """
    CABECERA: Representa la 'Hoja de Producción' física.
    """
    __tablename__ = 'registro_diario_produccion'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    
    # RELACIONES FK
    orden_id = db.Column(db.String(20), db.ForeignKey('orden_produccion.numero_op'), nullable=False)
    maquina_id = db.Column(db.Integer, db.ForeignKey('maquina.id'), nullable=False)
    
    # INPUTS: DATOS GENERALES (CABECERA)
    fecha = db.Column(db.Date, nullable=False)
    turno = db.Column(db.String(20))          # DIURNO, NOCTURNO, EXTRA
    hora_inicio = db.Column(db.String(10))    # 07:00
    hora_fin = db.Column(db.String(10))       # 19:00 (Opcional/Calculado)
    
    # CONTADORES MAQUINA (Validación Producción)
    colada_inicial = db.Column(db.Integer, default=0)
    colada_final = db.Column(db.Integer, default=0)
    
    # PARAMETROS REPORTADOS (Estado Maquina)
    tiempo_ciclo_reportado = db.Column(db.Float, default=0.0)      # Segundos, tomado del panel
    cantidad_por_hora_meta = db.Column(db.Integer, default=0)      # Meta teórica o input manual
    tiempo_enfriamiento = db.Column(db.Float, default=0.0)         # Segundos
    
    # SNAPSHOTS (Para valorizar producción histórica)
    # Se toman de la Orden/Producto al momento de crear el reporte
    snapshot_cavidades = db.Column(db.Integer, default=1)
    snapshot_peso_neto_gr = db.Column(db.Float, default=0.0)      # Peso de la pieza buena
    snapshot_peso_colada_gr = db.Column(db.Float, default=0.0)    # Peso del ramal
    snapshot_peso_extra_gr = db.Column(db.Float, default=0.0)     # Otros pesos
    
    # TOTALIZADORES (Calculados)
    total_coladas_calculada = db.Column(db.Integer, default=0)     # Final - Inicial
    total_piezas_buenas = db.Column(db.Integer, default=0)         # Suma de detalles o (Coladas * Cav)
    total_kg_real = db.Column(db.Float, default=0.0)               # Suma de los pesos por hora? O input manual total? 
                                                                   # En muchos reportes se pesa el total al final. 
                                                                   # Asumiremos input manual o suma según requiera user.
                                                                   # Por ahora calcularemos basado en coladas * pesos.

    # Relaciones
    orden = db.relationship('OrdenProduccion', backref='registros_diarios', lazy=True)
    maquina = db.relationship('Maquina', backref='registros_diarios', lazy=True)
    detalles = db.relationship('DetalleProduccionHora', backref='cabecera', cascade="all, delete-orphan", lazy=True)
    
    def actualizar_totales(self):
        """
        Recalcula totales basados en contadores y detalles.
        """
        # Validator de contadores
        if self.colada_final >= self.colada_inicial:
            self.total_coladas_calculada = self.colada_final - self.colada_inicial
        else:
             self.total_coladas_calculada = 0
             
        # Producción teórica basada en contadores
        self.total_piezas_buenas = self.total_coladas_calculada * self.snapshot_cavidades
        
        # Peso Teórico (Kg) = (Coladas * (PesoNeto + PesoColada)) / 1000
        peso_tiro_gr = (self.snapshot_peso_neto_gr * self.snapshot_cavidades) + self.snapshot_peso_colada_gr
        self.total_kg_real = (self.total_coladas_calculada * peso_tiro_gr) / 1000.0

    def to_dict(self):
        return {
            'id': self.id,
            'fecha': self.fecha.isoformat() if self.fecha else None,
            'turno': self.turno,
            'maquina': self.maquina.nombre if self.maquina else None,
            'orden': self.orden_id,
            'contadores': {
                'inicial': self.colada_inicial,
                'final': self.colada_final,
                'total': self.total_coladas_calculada
            },
            'parametros': {
                'ciclo': self.tiempo_ciclo_reportado,
                'enfriamiento': self.tiempo_enfriamiento
            },
            'totales_estimados': {
                 'piezas': self.total_piezas_buenas,
                 'kg_total': self.total_kg_real
            },
            'detalles': [d.to_dict() for d in self.detalles]
        }


class DetalleProduccionHora(db.Model):
    """
    DETALLE: Tabla interna del reporte (hora a hora).
    """
    __tablename__ = 'detalle_produccion_hora'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    registro_id = db.Column(db.Integer, db.ForeignKey('registro_diario_produccion.id'), nullable=False)
    
    hora = db.Column(db.String(10), nullable=False) # "07:00", "08:00"
    maquinista = db.Column(db.String(100))
    color = db.Column(db.String(50))
    observacion = db.Column(db.String(255))
    
    coladas_realizadas = db.Column(db.Integer, default=0) # Cantidad de ciclos en esta hora
    
    # Calculados (Helper)
    cantidad_piezas = db.Column(db.Integer, default=0) # Coladas * Cavs
    kg_producidos = db.Column(db.Float, default=0.0)   # Coladas * PesoTiro / 1000
    
    def calcular_metricas(self, cavidades, peso_tiro_gr):
        self.cantidad_piezas = self.coladas_realizadas * cavidades
        self.kg_producidos = (self.coladas_realizadas * peso_tiro_gr) / 1000.0
        
    def to_dict(self):
        return {
            'id': self.id,
            'hora': self.hora,
            'maquinista': self.maquinista,
            'color': self.color,
            'observacion': self.observacion,
            'coladas': self.coladas_realizadas,
            'piezas': self.cantidad_piezas,
            'kg': self.kg_producidos
        }
