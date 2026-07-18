from datetime import datetime, timezone

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class EstacionImportacionPesajeLegacy(db.Model):
    __tablename__ = "estacion_importacion_pesaje_legacy"
    __table_args__ = (
        db.UniqueConstraint(
            "station_id",
            "source_sha256",
            name="uq_importacion_legacy_estacion_fuente",
        ),
    )

    import_id = db.Column(db.String(36), primary_key=True)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_sha256 = db.Column(db.String(64), nullable=False)
    source_size_bytes = db.Column(db.BigInteger, nullable=False)
    source_schema_version = db.Column(db.Integer, nullable=False)
    source_total_rows = db.Column(db.Integer, nullable=False)
    source_active_rows = db.Column(db.Integer, nullable=False)
    source_deleted_rows = db.Column(db.Integer, nullable=False)
    source_first_capture_local = db.Column(db.String(40), nullable=True)
    source_last_capture_local = db.Column(db.String(40), nullable=True)
    manifest_json = db.Column(db.Text, nullable=False)
    total_chunks = db.Column(db.Integer, nullable=False)
    chunks_received = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(20), nullable=False, default="RECEIVING")
    started_at_utc = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    completed_at_utc = db.Column(db.DateTime(timezone=True), nullable=True)


class EstacionImportacionPesajeLegacyChunk(db.Model):
    __tablename__ = "estacion_importacion_pesaje_legacy_chunk"
    __table_args__ = (
        db.UniqueConstraint(
            "import_id",
            "chunk_index",
            name="uq_importacion_legacy_chunk",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    import_id = db.Column(
        db.String(36),
        db.ForeignKey(
            "estacion_importacion_pesaje_legacy.import_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    chunk_index = db.Column(db.Integer, nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    rows_count = db.Column(db.Integer, nullable=False)
    received_at_utc = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class EstacionPesajeLegacy(db.Model):
    __tablename__ = "estacion_pesaje_legacy"
    __table_args__ = (
        db.UniqueConstraint(
            "station_id",
            "legacy_pesaje_id",
            name="uq_pesaje_legacy_estacion_id",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    legacy_pesaje_id = db.Column(db.Integer, nullable=False)
    first_import_id = db.Column(
        db.String(36),
        db.ForeignKey(
            "estacion_importacion_pesaje_legacy.import_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    row_hash = db.Column(db.String(64), nullable=False)
    weight_kg = db.Column(db.Numeric(14, 3), nullable=False)
    captured_at_local = db.Column(db.String(40), nullable=False)
    captured_at_utc = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    operational_date = db.Column(db.Date, nullable=False, index=True)
    deleted_at_local = db.Column(db.String(40), nullable=True)
    deleted_at_utc = db.Column(db.DateTime(timezone=True), nullable=True)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False, index=True)
    op_raw = db.Column(db.String(50), nullable=True, index=True)
    op_normalized = db.Column(db.String(50), nullable=True, index=True)
    op_resolution_status = db.Column(db.String(30), nullable=False, index=True)
    ot_raw = db.Column(db.String(50), nullable=True)
    ot_normalized = db.Column(db.String(50), nullable=True)
    mold_raw = db.Column(db.String(300), nullable=True)
    mold_normalized = db.Column(db.String(300), nullable=True)
    color_raw = db.Column(db.String(300), nullable=True)
    color_normalized = db.Column(db.String(300), nullable=True)
    machine_raw = db.Column(db.String(100), nullable=True)
    machine_normalized = db.Column(db.String(100), nullable=True)
    shift_raw = db.Column(db.String(100), nullable=True)
    shift_normalized = db.Column(db.String(100), nullable=True)
    operator_raw = db.Column(db.String(300), nullable=True)
    operator_normalized = db.Column(db.String(300), nullable=True)
    raw_payload_json = db.Column(db.Text, nullable=False)
    imported_at_utc = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class EstacionImportacionPesajeLegacyFila(db.Model):
    __tablename__ = "estacion_importacion_pesaje_legacy_fila"
    __table_args__ = (
        db.UniqueConstraint(
            "import_id",
            "capture_id",
            name="uq_importacion_legacy_fila",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    import_id = db.Column(
        db.String(36),
        db.ForeignKey(
            "estacion_importacion_pesaje_legacy.import_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    capture_id = db.Column(
        db.Integer,
        db.ForeignKey("estacion_pesaje_legacy.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )


class EstacionCierreOpLegacy(db.Model):
    __tablename__ = "estacion_cierre_op_legacy"
    __table_args__ = (
        db.UniqueConstraint(
            "import_id",
            "station_id",
            "op_raw",
            name="uq_importacion_cierre_op_legacy",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    import_id = db.Column(
        db.String(36),
        db.ForeignKey(
            "estacion_importacion_pesaje_legacy.import_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    op_raw = db.Column(db.String(50), nullable=False, index=True)
    op_normalized = db.Column(db.String(50), nullable=False)
    mold_raw = db.Column(db.String(300), nullable=True)
    reason_raw = db.Column(db.String(500), nullable=True)
    closed_at_local = db.Column(db.String(40), nullable=False)
    closed_at_utc = db.Column(db.DateTime(timezone=True), nullable=False)


class EstacionDeltaPesajeLegacy(db.Model):
    __tablename__ = "estacion_delta_pesaje_legacy"

    batch_id = db.Column(db.String(36), primary_key=True)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    payload_hash = db.Column(db.String(64), nullable=False)
    rows_received = db.Column(db.Integer, nullable=False)
    rows_created = db.Column(db.Integer, nullable=False)
    high_watermark = db.Column(db.Integer, nullable=False)
    received_at_utc = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now
    )


class EstacionComandoPiloto(db.Model):
    __tablename__ = "estacion_comando_piloto"

    command_id = db.Column(db.String(36), primary_key=True)
    station_id = db.Column(
        db.String(36),
        db.ForeignKey("estacion_pesaje.station_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    action = db.Column(db.String(30), nullable=False, index=True)
    legacy_pesaje_id = db.Column(db.Integer, nullable=True)
    op_raw = db.Column(db.String(50), nullable=True)
    requested_by = db.Column(db.String(120), nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    payload_hash = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="PENDING", index=True)
    requested_at_utc = db.Column(
        db.DateTime(timezone=True), nullable=False, default=utc_now
    )
    delivered_at_utc = db.Column(db.DateTime(timezone=True), nullable=True)
    applied_at_utc = db.Column(db.DateTime(timezone=True), nullable=True)
    error_code = db.Column(db.String(100), nullable=True)
    result_json = db.Column(db.Text, nullable=True)
