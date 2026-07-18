import sys
from pathlib import Path

from sqlalchemy import inspect


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app
from app.extensions import db
from app.models.estacion_pesaje import (  # noqa: F401
    EstacionAvanceProduccion,
    EstacionEstadoActual,
    EstacionEstadoHistorial,
    EstacionHeartbeatRecepcion,
    EstacionPesaje,
    EstacionReporteAvanceRecepcion,
)
from app.models.legacy_pesaje import (  # noqa: F401
    EstacionCierreOpLegacy,
    EstacionImportacionPesajeLegacy,
    EstacionImportacionPesajeLegacyChunk,
    EstacionImportacionPesajeLegacyFila,
    EstacionPesajeLegacy,
)


STATION_MONITORING_TABLES = (
    "estacion_pesaje",
    "estacion_estado_actual",
    "estacion_heartbeat_recepcion",
    "estacion_estado_historial",
    "estacion_reporte_avance_recepcion",
    "estacion_avance_produccion",
    "estacion_importacion_pesaje_legacy",
    "estacion_importacion_pesaje_legacy_chunk",
    "estacion_pesaje_legacy",
    "estacion_importacion_pesaje_legacy_fila",
    "estacion_cierre_op_legacy",
)


def create_station_monitoring_tables(engine):
    """Create only the central tables owned by station monitoring."""
    existing = set(inspect(engine).get_table_names())
    created = []

    for table_name in STATION_MONITORING_TABLES:
        if table_name in existing:
            continue
        db.metadata.tables[table_name].create(bind=engine, checkfirst=True)
        created.append(table_name)

    return created


def main():
    app = create_app()
    with app.app_context():
        created = create_station_monitoring_tables(db.engine)

    if created:
        print("Tablas de monitoreo creadas:")
        for table_name in created:
            print(f"- {table_name}")
    else:
        print("El esquema de monitoreo ya estaba actualizado.")


if __name__ == "__main__":
    main()
