from app.extensions import db
from datetime import datetime

class RegistroDiarioProduccion(db.Model):
    __tablename__ = 'registro_diario_produccion'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    
    # RELACIONES FK
    orden_id = db.Column(db.String(20), db.ForeignKey('orden_produccion.numero_op'), nullable=False)
    maquina_id = db.Column(db.Integer, db.ForeignKey('maquina.id'), nullable=False)
    
    # INPUTS: DATOS GENERALES
    fecha = db.Column(db.Date, nullable=False)
    turno = db.Column(db.String(20))     # Turno de trabajo
    hora_ingreso = db.Column(db.String(20)) # Hora entrada
    maquinista = db.Column(db.String(100))
    
    # INPUTS: DATOS PRODUCTO
    molde = db.Column(db.String(100))
    pieza_color = db.Column(db.String(100)) # Concatenacion Pieza + Color
    
    # INPUTS: PRODUCCION
    coladas = db.Column(db.Integer, default=0)
    horas_trabajadas = db.Column(db.Float, default=0.0)
    peso_real_kg = db.Column(db.Float, default=0.0)
    
    # INPUTS: EMPAQUE
    cantidad_x_bulto = db.Column(db.Integer, default=0)
    numero_bultos = db.Column(db.Integer, default=0)
    doc_registro_nro = db.Column(db.String(50)) # Identificador físico/papel
    
    # INPUTS: MERMA
    color_merma = db.Column(db.String(50))
    peso_merma = db.Column(db.Float, default=0.0)
    peso_chancaca = db.Column(db.Float, default=0.0)
    fraccion_virgen = db.Column(db.Float, default=0.0) # 0.0 a 1.0
    
    # SNAPSHOTS (Datos copiados de la Orden/Producto al momento del registro para historicidad)
    snapshot_cavidades = db.Column(db.Integer, default=1)
    snapshot_ciclo_seg = db.Column(db.Float, default=0.0)
    snapshot_peso_unitario_gr = db.Column(db.Float, default=0.0) # Peso de pieza individual
    
    # CALCULADOS PERSISTIDOS
    calculo_peso_aprox_kg = db.Column(db.Float, default=0.0)
    calculo_doc = db.Column(db.Float, default=0.0) # Cantidad total piezas (o docenas?) - Revisar formula user: "DOC = Cavidades * Coladas" -> Esto son PIEZAS, aunque label dice DOC. Lo llamaré doc_cantidad
    calculo_cantidad_real = db.Column(db.Float, default=0.0)
    calculo_produccion_esperada_kg = db.Column(db.Float, default=0.0)
    
    # Relaciones
    orden = db.relationship('OrdenProduccion', backref='registros_diarios', lazy=True)
    maquina = db.relationship('Maquina', backref='registros_diarios', lazy=True)
    
    def actualizar_metricas(self):
        """
        Realiza los cálculos definidos por el usuario.
        Se asume que los snapshots ya tienen valores.
        """
        # Valores seguros
        coladas = self.coladas or 0
        p_unit = self.snapshot_peso_unitario_gr or 0.0
        cavs = self.snapshot_cavidades or 1
        h_trab = self.horas_trabajadas or 0.0
        ciclo = self.snapshot_ciclo_seg or 0.0
        p_real = self.peso_real_kg or 0.0
        
        # 1. Peso Aprox = Peso unitario (pieza) * Coladas / 1000
        # CORRECCION INTERPRETACION: Generalmente Peso Aprox es del TIRO o total?
        # Fórmula usuario: "Peso aprox = Peso unitario * Coladas / 1000"
        # Si P.Unit es en gramos, /1000 lo hace kg. Si es por colada, sería (P.Unit * Cavs) * Coladas.
        # User dijo "Peso unitario = Pieza-Color.Peso x cav" en otra formula. 
        # Pero luego aclaró "se refiere a que se debe sacar el precio unitario de la pieza original".
        # Asumiré: P.Aprox = (P.UnitPieza * Cavs * Coladas) / 1000 para que sea "Peso Total Aprox"
        # O si es literal User Formula: P.Unit * Coladas / 1000. 
        # Voy a usar la logica física: Peso Total = PesoTiro * Coladas.
        peso_tiro_gr = p_unit * cavs
        self.calculo_peso_aprox_kg = (peso_tiro_gr * coladas) / 1000.0
        
        # 2. DOC (Cantidad Total Piezas)
        # Formula User: "DOC = Cavidades * Coladas"
        self.calculo_doc = cavs * coladas
        
        # 3. Cantidad Real
        # Formula User: "Peso Real (kg) * 1000 / Peso Unitario"
        # Aquí P.Unitario debe ser de la PIEZA para obtener cantidad de piezas.
        if p_unit > 0:
            self.calculo_cantidad_real = (p_real * 1000.0) / p_unit
        else:
            self.calculo_cantidad_real = 0.0
            
        # 4. Produccion Esperada
        # Formula User: "Horas Trab. * Ciclo SKU * Peso Unitario / 1000"
        # Interpretación critica: Si "Ciclo SKU" es seg/ciclo, la formula está 'rara' dimensionalmente.
        # Si Ciclo SKU es "Ciclos por Hora", entonces: Horas * (Ciclos/H) * Peso / 1000 = Kg Esperados.
        # Dado que en orden.py 'tiempo_ciclo' son SEGUNDOS, convertiré a ciclos/hora.
        # Ciclos por hora = 3600 / tiempo_ciclo_seg
        ciclos_por_hora = 0
        if ciclo > 0:
            ciclos_por_hora = 3600.0 / ciclo
            
        # Aplico fórmula asumiendo 'Ciclo SKU' en la fórmula del usuario se refería a la cadencia (ciclos/hora)
        # user formula: H * Ciclo * Peso / 1000
        # Peso aqui sería Peso del TIRO (Producción por ciclo)? 
        # Si multiplica por PesoUnitarioPieza, daría Kg de 1 pieza * ciclos.
        # Asumo User quiere Peso Total Esperado: H * Ciclos/H * (PesoUnit * Cavs) / 1000
        
        self.calculo_produccion_esperada_kg = (h_trab * ciclos_por_hora * peso_tiro_gr) / 1000.0

    def to_dict(self):
        return {
            'id': self.id,
            'fecha': self.fecha.isoformat() if self.fecha else None,
            'turno': self.turno,
            'maquina': self.maquina.nombre if self.maquina else None,
            'maquinista': self.maquinista,
            'orden': self.orden_id,
            'inputs': {
                'coladas': self.coladas,
                'horas': self.horas_trabajadas,
                'peso_real': self.peso_real_kg
            },
            'calculos': {
                'peso_aprox': self.calculo_peso_aprox_kg,
                'doc_cantidad': self.calculo_doc,
                'produccion_esperada': self.calculo_produccion_esperada_kg
            }
        }
