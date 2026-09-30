"""KG009 PostgreSQL concurrency contracts.

These tests use the real weighing and KG opt-in services against the dedicated
``kg009_concurrency`` database.  Every worker owns its Flask/SQLAlchemy
session; the events only order the two sessions and never replace a database
lock with an in-memory assertion.
"""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Event
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import DBAPIError

from app import create_app, db
from app.config import Config
from app.models.scm_articulos import ScmArticulo
from app.models.scm_ot import ScmAtribucionProduccionKg, ScmManga, ScmPesajeManga
from app.models.scm_auditoria import ScmOperacion
from app.models.scm_inventory_kg import (
    ScmExistenciaMangaKg,
    ScmMovimientoInventarioKg,
)
from app.models.maquina import Maquina, TipoMaquina
from app.models.producto import Familia, Linea
from app.models.trabajador import RolOperativo, Trabajador
from app.models.scm_catalogos import ScmCapacidad
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_kg_service import activate_article_for_kg
from app.services.scm_weighing_service import confirm_manga_weighing
from app.services.scm_kg_pilot_service import prepare_kg_pilot
from app.services.scm_kg_recovery_service import (
    RECOVERY_NAMESPACE,
    apply_kg_recovery,
    preview_kg_recovery,
)
from app.services.scm_service_support import ScmServiceError
import app.services.scm_service_support as service_support
import app.services.scm_kg_pilot_service as kg_pilot_service
import app.services.scm_kg_service as kg_service
import app.services.scm_weighing_service as weighing_service

from tests.scm.test_scm_kg_receipt import (
    _print_color_manga,
    _seed_aggregate_color_work,
)
from app.services.scm_ot_service import transition_color_work
from test_scm_kg009_recovery import _seed_recovery_service_fixture
import test_scm_kg009_recovery as recovery_tests


pytestmark = pytest.mark.postgres

KG009_DATABASE_URL = "postgresql://kg009_test@127.0.0.1:55441/kg009_concurrency"


@pytest.fixture(scope="module")
def postgres_kg009_app():
    """Bind a real app to the coordinator-owned, isolated KG009 database."""
    engine = create_engine(KG009_DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            database = connection.execute(text("SELECT current_database()" )).scalar_one()
            if database != "kg009_concurrency":
                pytest.fail(f"suite KG009 conectada a base no autorizada: {database}")
            tables = set(connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )).scalars())
        if "scm_articulo" not in tables or "scm_manga" not in tables:
            pytest.fail(
                "kg009_concurrency no tiene el esquema migrado; ejecutar la "
                "preparación PostgreSQL aprobada antes de esta suite"
            )
        # This database is created exclusively for this suite.  Reset only its
        # data so repeated RED/GREEN runs cannot reuse actors or hard-coded
        # legacy fixture codes; migration history remains intact.
        data_tables = sorted(tables - {"alembic_version"})
        if data_tables:
            quoted = ", ".join(f'"{table}"' for table in data_tables)
            with engine.begin() as connection:
                connection.execute(text(
                    f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"
                ))
    finally:
        engine.dispose()

    original_uri = Config.SQLALCHEMY_DATABASE_URI
    Config.SQLALCHEMY_DATABASE_URI = KG009_DATABASE_URL
    app = create_app()
    app.config.update(
        TESTING=True,
        KG_RECEIPT_WRITE_ENABLED=True,
        KG_AUTOMATIC_INTAKE_ENABLED=False,
    )
    try:
        with app.app_context():
            ensure_initial_scm_configuration()
            if Linea.query.first() is None:
                db.session.add(Linea(codigo=9009, nombre="KG009 LINEA"))
            if Familia.query.first() is None:
                db.session.add(Familia(codigo=9009, nombre="KG009 FAMILIA"))
            if db.session.get(TipoMaquina, 1) is None:
                machine_type = TipoMaquina(
                    codigo="KG009-INYECCION",
                    nombre="KG009 INYECCION",
                    proceso="INYECCION",
                )
                db.session.add(machine_type)
                db.session.flush()
                db.session.add(Maquina(
                    codigo="KG009-MQ-01",
                    nombre="KG009 Maquina",
                    tipo_maquina_id=machine_type.id,
                    estado="OPERATIVA",
                    activo=True,
                ))
            if Trabajador.query.filter_by(codigo="TRB-01").first() is None:
                db.session.add(Trabajador(
                    codigo="TRB-01",
                    nombres="KG009",
                    apellidos="Operador",
                    activo=True,
                    roles=[RolOperativo.query.filter_by(codigo="MAQUINISTA").one()],
                ))
            db.session.commit()
        yield app
    finally:
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        Config.SQLALCHEMY_DATABASE_URI = original_uri


def _prepare_unweighed_manga(app):
    """Create one real prelabelled manga whose article remains UN."""
    _reset_dedicated_data(app)
    with app.app_context():
        ensure_initial_scm_configuration()
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=120)
        )
        manga = ScmManga.query.filter_by(
            public_id=UUID(created["mangas"][0]["public_id"])
        ).one()
        manga.cantidad_planificada_un = Decimal("120")
        manga.cantidad_asignada_un = Decimal("120")
        if manga.asignacion is not None:
            manga.asignacion.cantidad_asignada_un = Decimal("120")
        if manga.plan_linea is not None:
            manga.plan_linea.capacidad_efectiva_un = 120
        article = manga.lote_articulo.articulo
        # The current catalog policy defaults PIEZA_COLOR to KG.  KG009's
        # race starts from a legacy UN article and converts it through the
        # real activation service, so make that precondition explicit.
        article._allow_kg_downgrade = True
        article.unidad_inventario = "UN"
        article.version = 1
        assert article.unidad_inventario == "UN"
        db.session.flush()
        transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]),
            operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]},
            action="iniciar",
        )
        station, label = _print_color_manga(
            actor=creator,
            manga_id=created["mangas"][0]["public_id"],
            station_code=f"KG009-CONC-{uuid4().hex[:8].upper()}",
        )
        db.session.commit()
        return {
            "actor_id": creator.id,
            "article_id": article.id,
            "article_public_id": str(article.public_id),
            "manga_id": manga.id,
            "manga_public_id": str(manga.public_id),
            "station_id": station.station_id,
            "label_id": label["public_id"],
        }


def _reset_dedicated_data(app):
    """Reset only the explicitly dedicated database between scenarios."""
    with app.app_context():
        db.session.rollback()
        db.session.remove()
        with db.engine.begin() as connection:
            tables = set(connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )).scalars())
            data_tables = sorted(tables - {"alembic_version"})
            if data_tables:
                quoted = ", ".join(f'"{table}"' for table in data_tables)
                connection.execute(text(
                    f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"
                ))
        # Recreate the minimal dependencies expected by the real OT/manga
        # fixture after the data reset (the migration only creates tables).
        ensure_initial_scm_configuration()
        if Linea.query.first() is None:
            db.session.add(Linea(codigo=9009, nombre="KG009 LINEA"))
        if Familia.query.first() is None:
            db.session.add(Familia(codigo=9009, nombre="KG009 FAMILIA"))
        if db.session.get(TipoMaquina, 1) is None:
            machine_type = TipoMaquina(
                codigo="KG009-INYECCION",
                nombre="KG009 INYECCION",
                proceso="INYECCION",
            )
            db.session.add(machine_type)
            db.session.flush()
            db.session.add(Maquina(
                codigo="KG009-MQ-01",
                nombre="KG009 Maquina",
                tipo_maquina_id=machine_type.id,
                estado="OPERATIVA",
                activo=True,
            ))
        if Trabajador.query.filter_by(codigo="TRB-01").first() is None:
            db.session.add(Trabajador(
                codigo="TRB-01",
                nombres="KG009",
                apellidos="Operador",
                activo=True,
                roles=[RolOperativo.query.filter_by(codigo="MAQUINISTA").one()],
            ))
        db.session.commit()


def _weighing_data(scenario):
    return {
        "label_id": scenario["label_id"],
        "capture_id": str(uuid4()),
        "peso_bruto_kg": "12.100",
        "tara_kg": "0.100",
        "tara_fuente": "TIPO_MANGA",
        "pesada_at": "2026-08-11T16:55:00-05:00",
        "reading_stable": True,
    }


def _grant_capability(app, actor_id, capability_code):
    """Grant only the explicit capability needed by the real command path."""
    with app.app_context():
        actor = db.session.get(Trabajador, actor_id)
        role = actor.roles[0]
        capability = db.session.scalar(
            select(ScmCapacidad).where(ScmCapacidad.codigo == capability_code)
        )
        if capability not in role.capacidades:
            role.capacidades.append(capability)
        db.session.commit()


def _prepare_kg_pilot_in_thread(app, scenario, operation_id):
    with app.app_context():
        try:
            return prepare_kg_pilot(
                db.session,
                actor_id=scenario["actor_id"],
                article_ids=[scenario["article_id"]],
                reason="KG009 concurrencia CLI",
                operation_id=operation_id,
                apply=True,
            )
        except Exception:
            db.session.rollback()
            raise
        finally:
            db.session.remove()


def _activate_in_thread(app, scenario, done=None):
    with app.app_context():
        try:
            article = activate_article_for_kg(
                db.session, article_id=scenario["article_id"]
            )
            db.session.commit()
            return {"unit": article.unidad_inventario, "version": article.version}
        except Exception:
            db.session.rollback()
            raise
        finally:
            if done is not None:
                done.set()
            db.session.remove()


def _confirm_in_thread(app, scenario, started=None, operation_id=None):
    with app.app_context():
        try:
            if started is not None:
                started.set()
            return confirm_manga_weighing(
                db.session,
                station_id=scenario["station_id"],
                operation_id=operation_id or uuid4(),
                actor_id=scenario["actor_id"],
                data=_weighing_data(scenario),
            )
        except Exception:
            db.session.rollback()
            raise
        finally:
            db.session.remove()


def _kg_weighing_row(app, scenario):
    with app.app_context():
        row = db.session.scalar(
            select(ScmPesajeManga).where(
                ScmPesajeManga.manga_id == scenario["manga_id"]
            )
        )
        article = db.session.get(ScmArticulo, scenario["article_id"])
        assert row is not None
        return row, article


def _prepare_recovery_source(app):
    """Create one governed recovery source in the dedicated PostgreSQL DB."""
    _reset_dedicated_data(app)
    with app.app_context():
        # The dedicated concurrency schema predates the current role-code
        # width.  The recovery fixture's normal helper creates a role named
        # after each capability (25 chars), which is valid in the application
        # schema but cannot be inserted here.  Keep the production capability
        # code unchanged and attach it to the existing MAQUINISTA role only
        # for this isolated test fixture.
        original_grant = recovery_tests._grant_capabilities

        def grant_recovery_capability(actor, capabilities):
            for code in capabilities:
                capacity = ScmCapacidad.query.filter_by(codigo=code).first()
                if capacity is None:
                    capacity = ScmCapacidad(codigo=code, nombre=code)
                    db.session.add(capacity)
                    db.session.flush()
                role = next(
                    (candidate for candidate in actor.roles if candidate.codigo == "MAQUINISTA"),
                    None,
                )
                assert role is not None
                if capacity not in role.capacidades:
                    role.capacidades.append(capacity)

        recovery_tests._grant_capabilities = grant_recovery_capability
        try:
            ctx = _seed_recovery_service_fixture(
                app, station_code=f"KG009-REC-{uuid4().hex[:8].upper()}"
            )
        finally:
            recovery_tests._grant_capabilities = original_grant
        source_id = ctx["weighing"].public_id
        preview = preview_kg_recovery(
            db.session,
            actor_id=ctx["creator"].id,
            article_ids=[ctx["article"].id],
            reason="KG009 PostgreSQL recovery concurrency",
            source_pesaje_ids=[source_id],
        )
        assert preview["apply_allowed"] is True
        return {
            "actor_id": ctx["creator"].id,
            "article_id": ctx["article"].id,
            "manga_id": ctx["manga"].id,
            "weighing_id": ctx["weighing"].id,
            "source_id": source_id,
            "source_hash": preview["sources"][0]["source_snapshot_hash"],
        }


def _apply_recovery_in_thread(app, scenario, operation_id):
    with app.app_context():
        try:
            return apply_kg_recovery(
                db.session,
                actor_id=scenario["actor_id"],
                article_ids=[scenario["article_id"]],
                reason="KG009 PostgreSQL recovery concurrency",
                operation_id=operation_id,
                source_pesaje_ids=[scenario["source_id"]],
                source_snapshot_hashes={
                    str(scenario["source_id"]): scenario["source_hash"]
                },
            )
        except Exception:
            db.session.rollback()
            raise
        finally:
            db.session.remove()


def test_kg009_postgres_advisory_lock_serializes_optin_before_pesaje(
    postgres_kg009_app, monkeypatch,
):
    """An opt-in holding the advisory lock precedes the real weighing route."""
    app = postgres_kg009_app
    scenario = _prepare_unweighed_manga(app)
    real_lock = service_support.acquire_kg_productive_write_lock
    optin_lock_acquired = Event()
    release_optin = Event()
    weighing_started = Event()

    def hold_optin_lock(session):
        real_lock(session)
        optin_lock_acquired.set()
        assert release_optin.wait(timeout=30)

    monkeypatch.setattr(kg_service, "acquire_kg_productive_write_lock", hold_optin_lock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        optin = executor.submit(_activate_in_thread, app, scenario)
        assert optin_lock_acquired.wait(timeout=30)
        pesaje = executor.submit(
            _confirm_in_thread, app, scenario, weighing_started
        )
        assert weighing_started.wait(timeout=30)
        release_optin.set()
        optin_result = optin.result(timeout=30)
        pesaje_result = pesaje.result(timeout=30)

    row, article = _kg_weighing_row(app, scenario)
    assert optin_result == {"unit": "KG", "version": 2}
    assert pesaje_result["weighing"]["fuente_cantidad"] == "RESPONSABLE_ARMADO"
    assert article.unidad_inventario == "KG"
    assert row.cantidad_confirmada == Decimal("0.000")
    assert row.fuente_cantidad == "RESPONSABLE_ARMADO"


def test_kg009_postgres_pesaje_refreshes_preloaded_un_after_conversion(
    postgres_kg009_app,
):
    """A session that preloaded UN must observe KG/version after the lock."""
    app = postgres_kg009_app
    scenario = _prepare_unweighed_manga(app)
    preloaded = Event()
    converted = Event()

    def stale_session_confirm():
        with app.app_context():
            try:
                article = db.session.get(ScmArticulo, scenario["article_id"])
                assert article.unidad_inventario == "UN"
                assert article.version == 1
                preloaded.set()
                assert converted.wait(timeout=30)
                return confirm_manga_weighing(
                    db.session,
                    station_id=scenario["station_id"],
                    operation_id=uuid4(),
                    actor_id=scenario["actor_id"],
                    data=_weighing_data(scenario),
                )
            except Exception:
                db.session.rollback()
                raise
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        pesaje = executor.submit(stale_session_confirm)
        assert preloaded.wait(timeout=30)
        conversion = executor.submit(_activate_in_thread, app, scenario, converted)
        conversion_result = conversion.result(timeout=30)
        result = pesaje.result(timeout=30)

    row, article = _kg_weighing_row(app, scenario)
    assert conversion_result == {"unit": "KG", "version": 2}
    assert result["weighing"]["fuente_cantidad"] == "RESPONSABLE_ARMADO"
    assert row.cantidad_confirmada == Decimal("0.000")
    assert row.fuente_cantidad == "RESPONSABLE_ARMADO"
    assert article.unidad_inventario == "KG"
    assert article.version == 2


def test_kg009_postgres_prepare_cli_lock_precedes_real_pesaje(
    postgres_kg009_app, monkeypatch,
):
    """The actual prepare-kg-pilot service and weighing route share ordering."""
    app = postgres_kg009_app
    scenario = _prepare_unweighed_manga(app)
    _grant_capability(app, scenario["actor_id"], "ALMACEN_CONFIG_ADMINISTRAR")
    entered = Event()
    release = Event()
    weighing_started = Event()
    real_lock = kg_pilot_service.acquire_kg_productive_write_lock

    def hold_pilot_lock(session):
        real_lock(session)
        entered.set()
        assert release.wait(timeout=30)

    # Only the CLI service symbol is held; confirm_manga_weighing retains its
    # direct reference to the real support lock and must wait on PostgreSQL.
    monkeypatch.setattr(
        kg_pilot_service, "acquire_kg_productive_write_lock", hold_pilot_lock
    )
    operation_id = uuid4()
    with ThreadPoolExecutor(max_workers=2) as executor:
        pilot = executor.submit(
            _prepare_kg_pilot_in_thread, app, scenario, operation_id
        )
        assert entered.wait(timeout=30)
        pesaje = executor.submit(
            _confirm_in_thread, app, scenario, weighing_started
        )
        assert weighing_started.wait(timeout=30)
        release.set()
        pilot_result = pilot.result(timeout=30)
        weighing_result = pesaje.result(timeout=30)

    row, article = _kg_weighing_row(app, scenario)
    assert pilot_result["mode"] == "APPLIED"
    assert pilot_result["items"][0]["after"] == "KG"
    assert weighing_result["weighing"]["fuente_cantidad"] == "RESPONSABLE_ARMADO"
    assert article.unidad_inventario == "KG"
    assert row.cantidad_confirmada == Decimal("0.000")


def test_kg009_postgres_replayed_prepare_releases_advisory_for_next_session(
    postgres_kg009_app,
):
    """A replay must not leave its transaction-scoped advisory lock held."""
    app = postgres_kg009_app
    scenario = _prepare_unweighed_manga(app)
    _grant_capability(app, scenario["actor_id"], "ALMACEN_CONFIG_ADMINISTRAR")
    operation_id = uuid4()
    with app.app_context():
        first = prepare_kg_pilot(
            db.session,
            actor_id=scenario["actor_id"],
            article_ids=[scenario["article_id"]],
            reason="KG009 replay lock",
            operation_id=operation_id,
            apply=True,
        )
        assert first["mode"] == "APPLIED"
        db.session.remove()

    replay_ready = Event()
    release_replay = Event()

    def replay_session():
        with app.app_context():
            try:
                result = prepare_kg_pilot(
                    db.session,
                    actor_id=scenario["actor_id"],
                    article_ids=[scenario["article_id"]],
                    reason="KG009 replay lock",
                    operation_id=operation_id,
                    apply=True,
                )
                replay_ready.set()
                assert release_replay.wait(timeout=30)
                # If the service did not rollback its replay, this rollback
                # is deliberately delayed until after session B probes.
                return result
            finally:
                db.session.rollback()
                db.session.remove()

    def probe_session():
        with app.app_context():
            try:
                db.session.execute(text("SET LOCAL lock_timeout = '500ms'"))
                service_support.acquire_kg_productive_write_lock(db.session)
                return "acquired"
            except DBAPIError:
                db.session.rollback()
                return "blocked"
            finally:
                db.session.remove()

    with ThreadPoolExecutor(max_workers=2) as executor:
        replay = executor.submit(replay_session)
        assert replay_ready.wait(timeout=30)
        probe = executor.submit(probe_session)
        probe_result = probe.result(timeout=30)
        release_replay.set()
        replay_result = replay.result(timeout=30)

    assert probe_result == "acquired"
    assert replay_result == first


def test_kg009_postgres_same_uuid_cross_endpoint_is_idempotency_conflict(
    postgres_kg009_app, monkeypatch,
):
    """Reserve in weighing while pilot competes for the same UUID in session B."""
    app = postgres_kg009_app
    scenario = _prepare_unweighed_manga(app)
    _grant_capability(app, scenario["actor_id"], "ALMACEN_CONFIG_ADMINISTRAR")
    operation_id = uuid4()
    weighing_reserved = Event()
    pilot_reserving = Event()
    reserve_weighing = weighing_service._reserve_operation
    reserve_pilot = kg_pilot_service._reserve_operation

    def hold_reserved_weighing(*args, **kwargs):
        result = reserve_weighing(*args, **kwargs)
        weighing_reserved.set()
        assert pilot_reserving.wait(timeout=15)
        return result

    def competing_pilot(*args, **kwargs):
        pilot_reserving.set()
        return reserve_pilot(*args, **kwargs)

    monkeypatch.setattr(weighing_service, "_reserve_operation", hold_reserved_weighing)
    monkeypatch.setattr(kg_pilot_service, "_reserve_operation", competing_pilot)
    with ThreadPoolExecutor(max_workers=2) as executor:
        weighing = executor.submit(_confirm_in_thread, app, scenario, None, operation_id)
        assert weighing_reserved.wait(timeout=15)
        pilot = executor.submit(_prepare_kg_pilot_in_thread, app, scenario, operation_id)
        weighing_result = weighing.result(timeout=30)
        with pytest.raises(ScmServiceError) as conflict:
            pilot.result(timeout=30)
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    assert weighing_result["weighing"]
    with app.app_context():
        assert db.session.scalar(select(db.func.count(ScmPesajeManga.id)).where(
            ScmPesajeManga.manga_id == scenario["manga_id"]
        )) == 1


def test_kg009_postgres_recovery_same_parent_two_sessions_is_one_apply(
    postgres_kg009_app,
):
    """Two real recovery sessions share one parent idempotency operation."""
    app = postgres_kg009_app
    scenario = _prepare_recovery_source(app)
    operation_id = uuid4()
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_apply_recovery_in_thread, app, scenario, operation_id)
            for _ in range(2)
        ]
        results = [future.result(timeout=60) for future in futures]
    assert results[0] == results[1]
    assert results[0]["mode"] == "APPLIED"
    with app.app_context():
        assert db.session.scalar(select(db.func.count(ScmExistenciaMangaKg.id)).where(
            ScmExistenciaMangaKg.manga_id == scenario["manga_id"]
        )) == 1
        assert db.session.scalar(select(db.func.count(ScmMovimientoInventarioKg.id)).where(
            ScmMovimientoInventarioKg.pesaje_public_id == scenario["source_id"]
        )) == 1
        assert db.session.scalar(select(db.func.count(ScmAtribucionProduccionKg.id)).where(
            ScmAtribucionProduccionKg.pesaje_id == scenario["weighing_id"],
            ScmAtribucionProduccionKg.tipo == "NETO_MEDIDO",
        )) == 1
        service_support.acquire_kg_productive_write_lock(db.session)
        db.session.rollback()


def test_kg009_postgres_recovery_stale_hash_refreshes_locked_source(
    postgres_kg009_app,
):
    """A preloaded session rejects a changed locked manga before any write."""
    app = postgres_kg009_app
    scenario = _prepare_recovery_source(app)
    operation_id = uuid4()
    def mutate_manga():
        with app.app_context():
            try:
                manga = db.session.get(ScmManga, scenario["manga_id"])
                manga.version += 1
                db.session.commit()
            finally:
                db.session.remove()

    with app.app_context():
        preloaded_manga = db.session.get(ScmManga, scenario["manga_id"])
        preloaded_weighing = db.session.get(ScmPesajeManga, scenario["weighing_id"])
        initial_manga_version = preloaded_manga.version
        assert initial_manga_version >= 1
        assert preloaded_weighing.public_id == scenario["source_id"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(mutate_manga).result(timeout=30)
        with pytest.raises(ScmServiceError) as error:
            apply_kg_recovery(
                db.session,
                actor_id=scenario["actor_id"],
                article_ids=[scenario["article_id"]],
                reason="KG009 stale hash",
                operation_id=operation_id,
                source_pesaje_ids=[scenario["source_id"]],
                source_snapshot_hashes={
                    str(scenario["source_id"]): scenario["source_hash"]
                },
            )
        assert error.value.code == "KG_RECOVERY_CONFLICT"
        assert any(
            "SOURCE_SNAPSHOT_CHANGED" in conflict.get("conflicts", [])
            for conflict in error.value.details["conflicts"]
        )
        assert db.session.scalar(select(db.func.count(ScmOperacion.operation_id)).where(
            ScmOperacion.operation_id == operation_id
        )) == 0
        assert db.session.scalar(select(db.func.count(ScmMovimientoInventarioKg.id))) == 0
        assert db.session.scalar(select(db.func.count(ScmExistenciaMangaKg.id))) == 0


def test_kg009_postgres_recovery_child_uuid_conflict_has_no_effects(
    postgres_kg009_app,
):
    """An occupied deterministic child UUID rejects without ledger writes."""
    app = postgres_kg009_app
    scenario = _prepare_recovery_source(app)
    parent_id = uuid4()
    child_id = uuid5(
        RECOVERY_NAMESPACE, f"{parent_id}:{scenario['source_id']}"
    )
    with app.app_context():
        db.session.add(ScmOperacion(
            operation_id=child_id,
            endpoint="/otro-endpoint",
            actor_id=scenario["actor_id"],
            request_sha256="x" * 64,
            response_json={"occupied": True},
            estado_http=200,
        ))
        db.session.commit()
        with pytest.raises(ScmServiceError) as error:
            apply_kg_recovery(
                db.session,
                actor_id=scenario["actor_id"],
                article_ids=[scenario["article_id"]],
                reason="KG009 child conflict",
                operation_id=parent_id,
                source_pesaje_ids=[scenario["source_id"]],
                source_snapshot_hashes={
                    str(scenario["source_id"]): scenario["source_hash"]
                },
            )
        assert error.value.code == "IDEMPOTENCY_CONFLICT"
        assert db.session.scalar(select(db.func.count(ScmMovimientoInventarioKg.id))) == 0
        assert db.session.scalar(select(db.func.count(ScmExistenciaMangaKg.id))) == 0
        assert db.session.get(ScmArticulo, scenario["article_id"]).unidad_inventario == "UN"
