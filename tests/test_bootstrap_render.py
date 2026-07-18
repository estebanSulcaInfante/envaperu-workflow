import uuid

import pytest

from app.models.estacion_pesaje import EstacionPesaje
from app.services.station_auth import hash_station_token
from scripts.bootstrap_render import provision_bootstrap_station


def _bootstrap_environment():
    return {
        "BOOTSTRAP_STATION_ID": str(uuid.uuid4()),
        "BOOTSTRAP_STATION_CODE": "PESAJE-PLANTA-01",
        "BOOTSTRAP_STATION_NAME": "Balanza principal",
        "BOOTSTRAP_STATION_LOCATION": "Planta - pesaje",
        "BOOTSTRAP_STATION_TOKEN": "station-token-bootstrap-test-0001",
    }


def test_bootstrap_station_is_idempotent_and_never_rotates_token(app):
    environ = _bootstrap_environment()
    with app.app_context():
        station, created = provision_bootstrap_station(environ)
        repeated, repeated_created = provision_bootstrap_station(environ)

        assert created is True
        assert repeated_created is False
        assert repeated.station_id == station.station_id
        assert EstacionPesaje.query.count() == 1
        assert station.token_hash == hash_station_token(
            environ["BOOTSTRAP_STATION_TOKEN"]
        )

        changed = {**environ, "BOOTSTRAP_STATION_TOKEN": "different-token"}
        with pytest.raises(RuntimeError, match="token_hash"):
            provision_bootstrap_station(changed)


def test_bootstrap_station_rejects_partial_configuration(app):
    with app.app_context(), pytest.raises(RuntimeError, match="incompleto"):
        provision_bootstrap_station({"BOOTSTRAP_STATION_ID": str(uuid.uuid4())})
