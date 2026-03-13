from app.extensions import db
from datetime import datetime, timezone


class InventarioManga(db.Model):
    """
    Tracking liviano de cada manga/bulto (= pesaje físico).
    No duplica datos del pesaje: solo rastrea ubicación y estado.
    """
    __tablename__ = 'inventario_manga'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    # Identificador único: el ID del pesaje que generó este bulto (campo 0 del QR del sticker)
    pesaje_id = db.Column(db.Integer, unique=True, nullable=False, index=True)

    # Vínculo opcional con ControlPeso (se puede enlazar cuando el sync del scale module ocurra)
    control_peso_id = db.Column(db.Integer, db.ForeignKey('control_peso.id'), nullable=True)

    # Datos cacheados del QR para queries rápidos de inventario (evita JOIN pesados)
    nro_op = db.Column(db.String(20), nullable=True)
    molde = db.Column(db.String(100), nullable=True)
    color = db.Column(db.String(50), nullable=True)
    peso_kg = db.Column(db.Float, nullable=True)
    pieza_sku = db.Column(db.String(50), nullable=True)
    pieza_nombre = db.Column(db.String(100), nullable=True)

    # Campos extra flexibles del QR (posiciones 13, 14, 15)
    extra1 = db.Column(db.String(200), nullable=True)
    extra2 = db.Column(db.String(200), nullable=True)
    extra3 = db.Column(db.String(200), nullable=True)

    # Estado de inventario
    locacion_actual = db.Column(db.String(100), nullable=False)
    estado = db.Column(db.String(20), nullable=False, default='EN_INVENTARIO')  # EN_INVENTARIO | DESPACHADO | EN_TRANSITO

    fecha_ingreso = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    fecha_despacho = db.Column(db.DateTime, nullable=True)

    # Relaciones
    movimientos = db.relationship('MovimientoKardex', backref='manga', lazy='dynamic')

    def to_dict(self):
        return {
            'id': self.id,
            'pesaje_id': self.pesaje_id,
            'nro_op': self.nro_op,
            'molde': self.molde,
            'color': self.color,
            'peso_kg': self.peso_kg,
            'pieza_sku': self.pieza_sku,
            'pieza_nombre': self.pieza_nombre,
            'locacion_actual': self.locacion_actual,
            'estado': self.estado,
            'fecha_ingreso': self.fecha_ingreso.isoformat() if self.fecha_ingreso else None,
            'fecha_despacho': self.fecha_despacho.isoformat() if self.fecha_despacho else None,
            'extra1': self.extra1,
            'extra2': self.extra2,
            'extra3': self.extra3,
        }


class MovimientoKardex(db.Model):
    """
    Log de auditoría append-only. Cada entrada/salida/movimiento genera un registro.
    Nunca se edita ni se borra.
    """
    __tablename__ = 'movimiento_kardex'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)

    inventario_manga_id = db.Column(db.Integer, db.ForeignKey('inventario_manga.id'), nullable=False)

    tipo_operacion = db.Column(db.String(30), nullable=False)  # INGRESO-PROD, SAL-ARMAR, MOV-INTERNO, etc.
    locacion_origen = db.Column(db.String(100), nullable=True)
    locacion_destino = db.Column(db.String(100), nullable=True)
    operario_id = db.Column(db.String(50), nullable=True)
    metadatos = db.Column(db.Text, nullable=True)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self):
        return {
            'id': self.id,
            'inventario_manga_id': self.inventario_manga_id,
            'tipo_operacion': self.tipo_operacion,
            'locacion_origen': self.locacion_origen,
            'locacion_destino': self.locacion_destino,
            'operario_id': self.operario_id,
            'metadatos': self.metadatos,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
        }
