import json
from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class EstacionPesaje(db.Model):
    __tablename__ = "estacion_pesaje"

    station_id = db.Column(db.String(36), primary_key=True)
    codigo = db.Column(db.String(50), nullable=False, unique=True)
    nombre = db.Column(db.String(120), nullable=False)
    ubicacion = db.Column(db.String(200), nullable=False)
    estado_admin = db.Column(db.String(20), nullable=False, default="ACTIVA")
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    created_at_utc = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    retired_at_utc = db.Column(db.DateTime(timezone=True), nullable=True)

    estado_actual = db.relationship(
        "EstacionEstadoActual",
        back_populates="estacion",
        uselist=False,
        cascade="all, delete-orphan",
    )
    heartbeats = db.relationship(
        "EstacionHeartbeatRecepcion",
        back_populates="estacion",
        order_by="EstacionHeartbeatRecepcion.id",
    )
    historial = db.relationship(
        "EstacionEstadoHistorial",
        back_populates="estacion",
        order_by="EstacionEstadoHistorial.id",
    )
    reportes_avance = db.relationship(
        "EstacionReporteAvanceRecepcion",
        back_populates="estacion",
        order_by="EstacionReporteAvanceRecepcion.id",
    )
    avance_produccion = db.relationship(
        "EstacionAvanceProduccion",
        back_populates="estacion",
        order_by="EstacionAvanceProduccion.id",
        cascade="all, delete-orphan",
    )


class EstacionEstadoActual(db.Model):
    __tablename__ = "estacion_estado_actual"

    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="CASCADE"),
        primary_key=True,
    )
    heartbeat_id = db.Column(db.String(36), nullable=False, unique=True)
    boot_id = db.Column(db.String(36), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    generated_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    received_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    clock_skew_seconds = db.Column(db.Float, nullable=False)
    app_version = db.Column(db.String(50), nullable=False)
    mode = db.Column(db.String(30), nullable=False)
    process_state = db.Column(db.String(30), nullable=False)
    database_state = db.Column(db.String(30), nullable=False)
    scale_state = db.Column(db.String(40), nullable=False)
    printer_state = db.Column(db.String(30), nullable=False)
    catalog_state = db.Column(db.String(30), nullable=False)
    communication_state = db.Column(db.String(40), nullable=False)
    last_central_ack_utc = db.Column(db.DateTime(timezone=True), nullable=True)
    legacy_unsynced_count = db.Column(db.Integer, nullable=False, default=0)
    oldest_legacy_unsynced_at_utc = db.Column(db.DateTime(timezone=True), nullable=True)
    last_error_code = db.Column(db.String(100), nullable=True)
    context_json = db.Column(db.Text, nullable=False)
    last_capture_json = db.Column(db.Text, nullable=True)
    local_summary_json = db.Column(db.Text, nullable=False)
    payload_json = db.Column(db.Text, nullable=False)

    estacion = db.relationship("EstacionPesaje", back_populates="estado_actual")

    @staticmethod
    def decode_json(value):
        return json.loads(value) if value else None


class EstacionHeartbeatRecepcion(db.Model):
    __tablename__ = "estacion_heartbeat_recepcion"

    id = db.Column(db.Integer, primary_key=True)
    heartbeat_id = db.Column(db.String(36), nullable=False, unique=True)
    payload_hash = db.Column(db.String(64), nullable=False)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    boot_id = db.Column(db.String(36), nullable=False)
    sequence = db.Column(db.Integer, nullable=False)
    generated_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    received_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    applied_to_current = db.Column(db.Boolean, nullable=False, default=False)
    payload_json = db.Column(db.Text, nullable=False)

    estacion = db.relationship("EstacionPesaje", back_populates="heartbeats")


class EstacionEstadoHistorial(db.Model):
    __tablename__ = "estacion_estado_historial"

    id = db.Column(db.Integer, primary_key=True)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    heartbeat_id = db.Column(db.String(36), nullable=False)
    event_type = db.Column(db.String(50), nullable=False)
    occurred_at_utc = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    summary_json = db.Column(db.Text, nullable=False)

    estacion = db.relationship("EstacionPesaje", back_populates="historial")


class EstacionReporteAvanceRecepcion(db.Model):
    __tablename__ = "estacion_reporte_avance_recepcion"

    id = db.Column(db.Integer, primary_key=True)
    report_id = db.Column(db.String(36), nullable=False, unique=True)
    payload_hash = db.Column(db.String(64), nullable=False)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    generated_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    received_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    window_start_date = db.Column(db.Date, nullable=False)
    window_end_date = db.Column(db.Date, nullable=False)
    rows_count = db.Column(db.Integer, nullable=False)
    payload_json = db.Column(db.Text, nullable=False)

    estacion = db.relationship("EstacionPesaje", back_populates="reportes_avance")


class EstacionAvanceProduccion(db.Model):
    __tablename__ = "estacion_avance_produccion"
    __table_args__ = (
        db.UniqueConstraint(
            "station_id",
            "operational_date",
            "group_key",
            name="uq_estacion_avance_grupo",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    report_id = db.Column(
        db.String(36),
        db.ForeignKey(
            "estacion_reporte_avance_recepcion.report_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )
    operational_date = db.Column(db.Date, nullable=False, index=True)
    group_key = db.Column(db.String(64), nullable=False)
    op = db.Column(db.String(20), nullable=True, index=True)
    ot = db.Column(db.String(20), nullable=True)
    mold = db.Column(db.String(100), nullable=True)
    color = db.Column(db.String(100), nullable=True)
    machine_code = db.Column(db.String(50), nullable=True, index=True)
    shift = db.Column(db.String(20), nullable=True, index=True)
    bags = db.Column(db.Integer, nullable=False)
    weight_kg = db.Column(db.Numeric(14, 3), nullable=False)
    first_capture_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    last_capture_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)
    report_received_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)

    estacion = db.relationship("EstacionPesaje", back_populates="avance_produccion")
