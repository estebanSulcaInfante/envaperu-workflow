from datetime import date

import pytest

from app import db
from app.models.estacion_pesaje import EstacionPesaje
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_ot import (
    ScmAsignacionPersonalTrabajoOt,
    ScmEtiquetaManga,
    ScmManga,
    ScmPesajeManga,
)
from app.models.scm_production_orders import (
    ScmAsignacionDemandaSuministro,
    ScmOrdenOperacion,
    ScmOrdenProduccion,
)
from app.models.scm_recepcion import ScmPesajeBolsa
from app.models.scm_ot import ScmTrabajoOt
from app.models.trabajador import Trabajador
from app.services.scm_demo_seed_service import (
    ALEMBIC_HEAD,
    DEMO_MARKER,
    DEMO_OF_ACTIVE_CODE,
    DEMO_OF_IDLE_CODE,
    LocalDemoSeedError,
    assert_local_demo_database,
    seed_alcancia_pablo_demo,
)
from app.services.scm_weighing_service import resolve_manga_label
from app.services.station_auth import hash_station_token


LOCAL_URL = (
    "postgresql+psycopg2://postgres:secret@localhost/"
    "enva_uat_alcancia"
)


def test_local_demo_guard_rejects_remote_wrong_database_and_schema_drift():
    with pytest.raises(LocalDemoSeedError, match="loopback"):
        assert_local_demo_database(
            "postgresql://postgres:secret@db.example.com/enva_uat_alcancia",
            connection_database="enva_uat_alcancia",
            migration_revision=ALEMBIC_HEAD,
        )
    with pytest.raises(LocalDemoSeedError, match="no autorizada"):
        assert_local_demo_database(
            "postgresql://postgres:secret@localhost/envaperu_test",
            connection_database="envaperu_test",
            migration_revision=ALEMBIC_HEAD,
        )
    with pytest.raises(LocalDemoSeedError, match="no coincide"):
        assert_local_demo_database(
            LOCAL_URL,
            connection_database="enva_test",
            migration_revision=ALEMBIC_HEAD,
        )
    with pytest.raises(LocalDemoSeedError, match="migraciones"):
        assert_local_demo_database(
            LOCAL_URL,
            connection_database="enva_uat_alcancia",
            migration_revision="f60c8d5e6f43",
        )


def test_seed_demo_alcancia_is_idempotent_and_uses_only_mangas(app):
    with app.app_context():
        command = {
            "database_url": LOCAL_URL,
            "connection_database": "enva_uat_alcancia",
            "migration_revision": ALEMBIC_HEAD,
            "operational_date": date(2026, 8, 10),
        }
        first = seed_alcancia_pablo_demo(db.session, **command)

        demand = ScmOrdenProduccion.query.filter_by(
            referencia_origen=DEMO_MARKER
        ).one()
        assert demand.estado == "PLANIFICADA"
        assert demand.fecha_necesidad == date(2026, 8, 14)
        assert len(demand.lineas) == 1
        assert demand.lineas[0].cantidad_solicitada == 2400

        idle = ScmOrdenOperacion.query.filter_by(
            codigo=DEMO_OF_IDLE_CODE
        ).one()
        active = ScmOrdenOperacion.query.filter_by(
            codigo=DEMO_OF_ACTIVE_CODE
        ).one()
        assert idle.estado == "LIBERADA"
        assert active.estado == "EN_EJECUCION"
        assert idle.plan_produccion_id == active.plan_produccion_id
        assert idle.plan_produccion.orden_produccion_id == demand.id
        allocations = ScmAsignacionDemandaSuministro.query.all()
        assert len(allocations) == 2
        assert {item.orden_operacion_salida.orden_operacion_id for item in allocations} == {
            idle.id,
            active.id,
        }
        assert {
            item.orden_operacion_salida.articulo.clase for item in allocations
        } == {"PRODUCTO_TERMINADO"}
        assert sum(item.cantidad_planificada for item in allocations) == 2400

        ots = RegistroDiarioProduccion.query.filter_by(
            tipo_ot="FABRICACION",
            codigo_ot_sintetico=False,
        ).all()
        assert len(ots) == 1
        assert ots[0].estado == "EN_EJECUCION"
        assert ots[0].fecha == date(2026, 8, 10)
        assert ots[0].orden_operacion_id is None

        works = ScmTrabajoOt.query.all()
        assert len(works) == 1
        assert works[0].orden_operacion_id == active.id
        assert works[0].estado == "EN_EJECUCION"

        mangas = ScmManga.query.order_by(ScmManga.secuencia_ot).all()
        assert len(mangas) == 2
        assert {item.estado for item in mangas} == {"PREETIQUETADA"}
        assert {item.trabajo_ot_id for item in mangas} == {works[0].id}
        labels = ScmEtiquetaManga.query.order_by(ScmEtiquetaManga.id).all()
        assert len(labels) == 2
        assert {item.tipo for item in labels} == {"PREPESAJE"}
        assert {item.estado for item in labels} == {"IMPRESA"}
        assert {item.station_id for item in labels} == {first["station_id"]}
        assert all(
            resolve_manga_label(db.session, label_id=item.public_id)["can_weigh"]
            for item in labels
        )
        assignments = ScmAsignacionPersonalTrabajoOt.query.all()
        assert len(assignments) == 1
        assert assignments[0].trabajador_id == first["operator_id"]
        assert {item.asignacion_personal_trabajo_id for item in mangas} == {
            assignments[0].id
        }
        station = db.session.get(EstacionPesaje, first["station_id"])
        assert station.token_hash == hash_station_token(first["station_token"])
        operator = db.session.get(Trabajador, first["operator_id"])
        assert operator.rol_principal_activo.codigo == "GERENTE_GENERAL"
        assert {role.codigo for role in operator.roles}.issuperset(
            {"GERENTE_GENERAL", "MAQUINISTA"}
        )
        assert first["label_ids"] == [str(item.public_id) for item in labels]
        assert first["qr_json"] == [item.payload_json["qr"] for item in labels]
        assert ScmPesajeManga.query.count() == 0
        assert ScmPesajeBolsa.query.count() == 0

        counts_before = {
            "orders": ScmOrdenOperacion.query.count(),
            "ots": RegistroDiarioProduccion.query.count(),
            "works": ScmTrabajoOt.query.count(),
            "mangas": ScmManga.query.count(),
            "labels": ScmEtiquetaManga.query.count(),
        }
        second = seed_alcancia_pablo_demo(db.session, **command)
        counts_after = {
            "orders": ScmOrdenOperacion.query.count(),
            "ots": RegistroDiarioProduccion.query.count(),
            "works": ScmTrabajoOt.query.count(),
            "mangas": ScmManga.query.count(),
            "labels": ScmEtiquetaManga.query.count(),
        }

        assert first["marker"] == DEMO_MARKER
        assert first["created"] is True
        assert second["marker"] == DEMO_MARKER
        assert second["created"] is False
        assert second["ot_id"] == first["ot_id"]
        assert counts_after == counts_before


def test_cli_seed_demo_requires_explicit_local_confirmation(app, runner):
    with app.app_context():
        response = runner.invoke(
            args=["seed-demo-alcancia-pablo", "--fecha-operativa", "2026-08-10"]
        )
        assert response.exit_code != 0
        assert "--confirm-local" in response.output
        assert ScmOrdenProduccion.query.filter_by(
            referencia_origen=DEMO_MARKER
        ).count() == 0
