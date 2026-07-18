import os
import sys
from pathlib import Path

from sqlalchemy import inspect


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app
from app.extensions import db
from app.models.estacion_pesaje import EstacionPesaje
from app.services.station_auth import hash_station_token, provision_station


BOOTSTRAP_STATION_ENV = {
    "station_id": "BOOTSTRAP_STATION_ID",
    "code": "BOOTSTRAP_STATION_CODE",
    "name": "BOOTSTRAP_STATION_NAME",
    "location": "BOOTSTRAP_STATION_LOCATION",
    "token": "BOOTSTRAP_STATION_TOKEN",
}


def provision_bootstrap_station(environ=None):
    environ = os.environ if environ is None else environ
    values = {
        field: str(environ.get(variable, "")).strip()
        for field, variable in BOOTSTRAP_STATION_ENV.items()
    }
    configured = [field for field, value in values.items() if value]
    if not configured:
        return None, False
    missing = [field for field, value in values.items() if not value]
    if missing:
        raise RuntimeError(
            "Bootstrap station incompleto: " + ", ".join(sorted(missing))
        )

    station = db.session.get(EstacionPesaje, values["station_id"])
    if station is None:
        station, _ = provision_station(**values)
        return station, True

    expected = {
        "codigo": values["code"],
        "nombre": values["name"],
        "ubicacion": values["location"],
        "token_hash": hash_station_token(values["token"]),
    }
    mismatches = [
        field for field, expected_value in expected.items()
        if getattr(station, field) != expected_value
    ]
    if mismatches:
        raise RuntimeError(
            "Bootstrap station no coincide: " + ", ".join(sorted(mismatches))
        )
    return station, False


def main():
    app = create_app()
    with app.app_context():
        before = set(inspect(db.engine).get_table_names())
        db.create_all()
        after = set(inspect(db.engine).get_table_names())
        station, station_created = provision_bootstrap_station()

    created = sorted(after - before)
    print(f"Render database ready: {len(after)} tables, {len(created)} created.")
    if station is not None:
        action = "created" if station_created else "already_registered"
        print(f"Bootstrap station {action}: {station.codigo}")


if __name__ == '__main__':
    main()
