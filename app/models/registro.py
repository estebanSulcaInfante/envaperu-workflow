import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db
from app.models.maquina import Maquina  # Import for relationship resolution


def _legacy_ot_code():
    """Fallback for callers that still create the historical daily record."""
    return f"OT-LEGACY-NEW-{uuid.uuid4().hex[:12].upper()}"


class RegistroDiarioProduccion(db.Model):
    """
    CABECERA: Representa la 'Hoja de Producción' física.
    """
    __tablename__ = 'registro_diario_produccion'
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('BORRADOR', 'PLANIFICADA', 'EN_EJECUCION', "
            "'CERRADA', 'ANULADA', "
            "'MIGRADA_PENDIENTE_CLASIFICACION')",
            name="ck_registro_diario_estado_ot",
        ),
        db.CheckConstraint(
            "version > 0",
            name="ck_registro_diario_version_ot",
        ),
        db.CheckConstraint(
            "secuencia_siguiente_manga > 0",
            name="ck_registro_diario_secuencia_manga",
        ),
        db.CheckConstraint(
            "secuencia_siguiente_trabajo > 0",
            name="ck_registro_diario_secuencia_trabajo",
        ),
        db.CheckConstraint(
            "tipo_ot IN ('FABRICACION', 'ENSAMBLE')",
            name="ck_registro_diario_tipo_ot",
        ),
        db.CheckConstraint(
            "(tipo_ot = 'FABRICACION' AND maquina_id IS NOT NULL) OR "
            "(tipo_ot = 'ENSAMBLE' AND centro_trabajo_id IS NOT NULL)",
            name="ck_registro_diario_recurso_ot",
        ),
        db.CheckConstraint(
            "cantidad_objetivo IS NULL OR cantidad_objetivo > 0",
            name="ck_registro_diario_cantidad_objetivo",
        ),
        db.CheckConstraint(
            "(tipo_ot = 'ENSAMBLE' AND modo_ejecucion_ensamble IN "
            "('MESA', 'CONCURRENTE')) OR "
            "(tipo_ot <> 'ENSAMBLE' AND modo_ejecucion_ensamble IS NULL)",
            name="ck_registro_diario_modo_ensamble",
        ),
        db.CheckConstraint(
            "(modo_ejecucion_ensamble = 'CONCURRENTE' AND ("
            "ot_fabricacion_contexto_id IS NOT NULL OR "
            "trabajo_color_contexto_id IS NOT NULL)) OR "
            "(modo_ejecucion_ensamble IS DISTINCT FROM 'CONCURRENTE' AND "
            "ot_fabricacion_contexto_id IS NULL AND "
            "trabajo_color_contexto_id IS NULL)",
            name="ck_registro_diario_contexto_ensamble",
        ),
        db.Index(
            "ix_registro_diario_trabajo_color_contexto",
            "trabajo_color_contexto_id",
        ),
        db.Index(
            "uq_registro_ot_fabricacion_recurso_turno_activa",
            "maquina_id",
            "fecha",
            "turno",
            unique=True,
            postgresql_where=db.text(
                "tipo_ot = 'FABRICACION' "
                "AND codigo_ot_sintetico = false "
                "AND estado <> 'ANULADA' "
                "AND orden_id IS NULL "
                "AND orden_operacion_id IS NULL "
                "AND corrida_fabricacion_id IS NULL"
            ),
            sqlite_where=db.text(
                "tipo_ot = 'FABRICACION' "
                "AND codigo_ot_sintetico = 0 "
                "AND estado <> 'ANULADA' "
                "AND orden_id IS NULL "
                "AND orden_operacion_id IS NULL "
                "AND corrida_fabricacion_id IS NULL"
            ),
        ),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # Identidad canónica de OT. Las filas históricas reciben un código
    # sintético mediante migración y no participan en el piloto SCM hasta su
    # conciliación explícita.
    public_id = db.Column(
        Uuid(as_uuid=True),
        nullable=False,
        unique=True,
        default=uuid.uuid4,
    )
    codigo_ot = db.Column(
        db.String(32),
        nullable=False,
        unique=True,
        default=_legacy_ot_code,
    )
    codigo_ot_sintetico = db.Column(
        db.Boolean,
        nullable=False,
        default=True,
        server_default=db.false(),
    )
    estado = db.Column(
        db.String(32),
        nullable=False,
        default="MIGRADA_PENDIENTE_CLASIFICACION",
        server_default="BORRADOR",
    )
    tipo_ot = db.Column(
        db.String(20),
        nullable=False,
        default="FABRICACION",
        server_default="FABRICACION",
    )
    modo_ejecucion_ensamble = db.Column(db.String(20), nullable=True)
    ot_fabricacion_contexto_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey(
            'registro_diario_produccion.public_id', ondelete='RESTRICT'
        ),
        nullable=True,
    )
    timezone_snapshot = db.Column(
        db.String(64),
        nullable=False,
        default="America/Lima",
        server_default="America/Lima",
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        default=lambda: datetime.now(timezone.utc),
    )
    created_at_source = db.Column(
        db.String(32),
        nullable=False,
        default="CENTRAL",
        server_default="CENTRAL",
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=True,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    iniciada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    cerrada_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    maquinista_previsto_id = db.Column(
        db.Integer,
        db.ForeignKey("trabajador.id", ondelete="RESTRICT"),
        nullable=True,
    )
    version = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    secuencia_siguiente_manga = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    secuencia_siguiente_trabajo = db.Column(
        db.Integer,
        nullable=False,
        default=1,
        server_default="1",
    )
    
    # RELACIONES FK
    orden_id = db.Column(
        db.String(20),
        db.ForeignKey('orden_produccion.numero_op'),
        nullable=True,
    )
    # Identidad canónica TS-010P. Durante expand son nullables para no inventar
    # genealogía en OT legacy; las OT v2 exigirán ambos campos desde servicio.
    orden_operacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey('scm_orden_operacion.id', ondelete='RESTRICT'),
        nullable=True,
    )
    corrida_fabricacion_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey('scm_corrida_fabricacion.id', ondelete='RESTRICT'),
        nullable=True,
    )
    trabajo_color_contexto_id = db.Column(
        Uuid(as_uuid=True),
        db.ForeignKey('scm_trabajo_ot.id', ondelete='RESTRICT'),
        nullable=True,
    )
    maquina_id = db.Column(db.Integer, db.ForeignKey('maquina.id'), nullable=True)
    centro_trabajo_id = db.Column(
        db.Integer,
        db.ForeignKey('scm_centro_trabajo.id', ondelete='RESTRICT'),
        nullable=True,
    )
    responsable_id = db.Column(
        db.Integer,
        db.ForeignKey('trabajador.id', ondelete='RESTRICT'),
        nullable=True,
    )
    cantidad_objetivo = db.Column(db.Numeric(15, 3), nullable=True)
    cantidad_confirmada = db.Column(
        db.Numeric(15, 3), nullable=False, default=0, server_default="0"
    )
    
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
    
    # SNAPSHOTS DE MÁQUINA (Para conservar dato histórico si la máquina cambia)
    maquina_codigo_snapshot = db.Column(db.String(20), nullable=True)
    maquina_nombre_snapshot = db.Column(db.String(100), nullable=True)
    
    # TOTALIZADORES (Calculados)
    total_coladas_calculada = db.Column(db.Integer, default=0)     # Final - Inicial
    total_piezas_buenas = db.Column(db.Integer, default=0)         # Suma de detalles o (Coladas * Cav)
    total_kg_real = db.Column(db.Float, default=0.0)               # Suma de los pesos por hora? O input manual total? 
                                                                   # En muchos reportes se pesa el total al final. 
                                                                   # Asumiremos input manual o suma según requiera user.
                                                                   # Por ahora calcularemos basado en coladas * pesos.

    # Relaciones
    orden = db.relationship('OrdenProduccion', backref='registros_diarios', lazy=True)
    orden_operacion = db.relationship('ScmOrdenOperacion')
    corrida_fabricacion = db.relationship('ScmCorridaFabricacion')
    maquina = db.relationship('Maquina', backref='registros_diarios', lazy=True)
    centro_trabajo = db.relationship('ScmCentroTrabajo')
    responsable = db.relationship(
        'Trabajador', foreign_keys=[responsable_id]
    )
    detalles = db.relationship('DetalleProduccionHora', backref='cabecera', cascade="all, delete-orphan", lazy=True)
    controles_peso = db.relationship('ControlPeso', backref='registro', cascade="all, delete-orphan", lazy=True)
    created_by = db.relationship(
        "Trabajador",
        foreign_keys=[created_by_id],
    )
    maquinista_previsto = db.relationship(
        "Trabajador",
        foreign_keys=[maquinista_previsto_id],
    )
    ot_fabricacion_contexto = db.relationship(
        'RegistroDiarioProduccion',
        remote_side=[public_id],
        foreign_keys=[ot_fabricacion_contexto_id],
    )
    trabajos_ot = db.relationship(
        'ScmTrabajoOt',
        back_populates='orden_trabajo',
        foreign_keys='ScmTrabajoOt.orden_trabajo_id',
        lazy='selectin',
        order_by='ScmTrabajoOt.secuencia',
    )
    trabajo_color_contexto = db.relationship(
        'ScmTrabajoOt',
        foreign_keys=[trabajo_color_contexto_id],
    )
    
    def actualizar_totales(self):
        """
        Recalcula totales basados en contadores y detalles.
        Si los contadores son 0, usa la suma de los detalles horarios.
        """
        # 1. Calcular desde contadores
        diff_contadores = 0
        if self.colada_final is not None and self.colada_inicial is not None:
             if self.colada_final >= self.colada_inicial:
                diff_contadores = self.colada_final - self.colada_inicial
             
        # 2. Calcular desde suma de horas (Fallback)
        sum_detalles = 0
        if self.detalles:
             sum_detalles = sum([d.coladas_realizadas or 0 for d in self.detalles])
        
        # Calcular suma de Pesajes (ControlPeso) via Query directo para ver datos flushed
        from app.models.control_peso import ControlPeso
        from sqlalchemy import func
        
        sum_pesos_control = 0.0
        # Check ID existence to avoid query on transient object if needed
        if self.id:
            q_sum = db.session.query(func.sum(ControlPeso.peso_real_kg)).filter(ControlPeso.registro_id == self.id).scalar()
            sum_pesos_control = q_sum or 0.0
            
        print(f"DEBUG: updating totals query. ID: {self.id}, Sum: {sum_pesos_control}")

        # 3. Decidir cuál usar para COLADAS
        if diff_contadores > 0:
            self.total_coladas_calculada = diff_contadores
        else:
            self.total_coladas_calculada = sum_detalles
             
        # Producción teórica (Piezas)
        cavs = self.snapshot_cavidades or 1
        self.total_piezas_buenas = self.total_coladas_calculada * cavs
        
        # 4. Decidir KG REAL
        # Prioridad 1: Pesajes Reales (ControlPeso)
        if sum_pesos_control > 0:
            self.total_kg_real = sum_pesos_control
        else:
            # Prioridad 2: Cálculo por Coladas × Peso Tiro
            # snapshot_peso_neto_gr = peso neto TOTAL del golpe (ya incluye todas cav)
            # snapshot_peso_colada_gr = ramal
            p_neto   = self.snapshot_peso_neto_gr   or 0.0
            p_colada = self.snapshot_peso_colada_gr or 0.0
            peso_tiro_gr = p_neto + p_colada

            self.total_kg_real = (self.total_coladas_calculada * peso_tiro_gr) / 1000.0

    def to_dict(self):
        return {
            'id': self.id,
            'public_id': str(self.public_id) if self.public_id else None,
            'codigo_ot': self.codigo_ot,
            'codigo_ot_sintetico': self.codigo_ot_sintetico,
            'estado': self.estado,
            'tipo_ot': self.tipo_ot,
            'modo_ejecucion_armado': self.modo_ejecucion_ensamble,
            # Alias técnico heredado para clientes anteriores al cambio de vocabulario.
            'modo_ejecucion_ensamble': self.modo_ejecucion_ensamble,
            'ot_fabricacion_contexto_id': (
                str(self.ot_fabricacion_contexto_id)
                if self.ot_fabricacion_contexto_id else None
            ),
            'trabajo_color_contexto_id': (
                str(self.trabajo_color_contexto_id)
                if self.trabajo_color_contexto_id else None
            ),
            'ot_fabricacion_contexto': (
                {
                    'public_id': str(self.ot_fabricacion_contexto.public_id),
                    'codigo_ot': self.ot_fabricacion_contexto.codigo_ot,
                    'estado': self.ot_fabricacion_contexto.estado,
                    'fecha_operativa': (
                        self.ot_fabricacion_contexto.fecha.isoformat()
                        if self.ot_fabricacion_contexto.fecha else None
                    ),
                    'maquina': (
                        self.ot_fabricacion_contexto.maquina_nombre_snapshot
                        or (
                            self.ot_fabricacion_contexto.maquina.nombre
                            if self.ot_fabricacion_contexto.maquina else None
                        )
                    ),
                }
                if self.ot_fabricacion_contexto else None
            ),
            'fecha': self.fecha.isoformat() if self.fecha else None,
            'fecha_operativa': self.fecha.isoformat() if self.fecha else None,
            'turno': self.turno,
            'maquinista_previsto_id': self.maquinista_previsto_id,
            'maquinista_previsto': (
                self.maquinista_previsto.nombre_completo
                if self.maquinista_previsto
                else None
            ),
            'maquina_id': self.maquina_id,
            'maquina': self.maquina_nombre_snapshot or (self.maquina.nombre if self.maquina else None),
            'maquina_codigo': self.maquina_codigo_snapshot or (self.maquina.codigo if self.maquina else None),
            'centro_trabajo': (
                self.centro_trabajo.to_dict() if self.centro_trabajo else None
            ),
            'responsable_id': self.responsable_id,
            'responsable': (
                self.responsable.nombre_completo if self.responsable else None
            ),
            'cantidad_objetivo': (
                format(self.cantidad_objetivo, 'f')
                if self.cantidad_objetivo is not None else None
            ),
            'cantidad_confirmada': format(
                self.cantidad_confirmada or 0, 'f'
            ),
            'orden': self.orden_id,
            'orden_operacion_id': (
                str(self.orden_operacion_id)
                if self.orden_operacion_id else None
            ),
            'corrida_fabricacion_id': (
                str(self.corrida_fabricacion_id)
                if self.corrida_fabricacion_id else None
            ),
            'orden_fabricacion': (
                {
                    'id': str(self.orden_operacion.id),
                    'codigo': self.orden_operacion.codigo,
                    'codigo_legacy_op': (
                        self.orden_operacion.fabricacion.codigo_legacy_op
                        if self.orden_operacion.fabricacion else None
                    ),
                }
                if self.orden_operacion else None
            ),
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
            'detalles': [d.to_dict() for d in self.detalles],
            'version': self.version,
            'created_at': (
                self.created_at.isoformat() if self.created_at else None
            ),
            'created_at_source': self.created_at_source,
            'iniciada_at': (
                self.iniciada_at.isoformat() if self.iniciada_at else None
            ),
            'cerrada_at': (
                self.cerrada_at.isoformat() if self.cerrada_at else None
            ),
        }


class DetalleProduccionHora(db.Model):
    """
    DETALLE: Tabla interna del reporte (hora a hora).
    """
    __tablename__ = 'detalle_produccion_hora'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    registro_id = db.Column(db.Integer, db.ForeignKey('registro_diario_produccion.id'), nullable=False)
    
    hora = db.Column(db.String(10), nullable=False) # "07:00", "08:00"
    
    # Nuevos campos normalizados
    trabajador_id = db.Column(db.Integer, db.ForeignKey('trabajador.id'), nullable=True) # nullable temporalmente para historial
    maquinista_snapshot = db.Column(db.String(100)) # Reemplaza a "maquinista", retiene el texto
    
    color = db.Column(db.String(50))
    observacion = db.Column(db.String(255))
    
    # Relación
    trabajador = db.relationship('Trabajador', backref='detalles_produccion', lazy=True)
    
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
            'trabajador_id': self.trabajador_id,
            'maquinista': self.maquinista_snapshot, # Mantenemos el key 'maquinista' para el frontend temporalmente
            'trabajador_nombre': self.trabajador.nombre_completo if self.trabajador else self.maquinista_snapshot,
            'color': self.color,
            'observacion': self.observacion,
            'coladas': self.coladas_realizadas,
            'piezas': self.cantidad_piezas,
            'kg': self.kg_producidos
        }
