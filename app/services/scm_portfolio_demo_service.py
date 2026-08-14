"""Isolated, resettable data set for the public portfolio demo."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import PurePosixPath
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from app.extensions import db
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_articulos import ScmArticulo
from app.models.scm_inventory import (
    ScmLoteAperturaInventario,
    ScmMovimientoInventario,
    ScmMovimientoMaterialInventario,
    ScmSaldoInventario,
    ScmSaldoMaterialInventario,
)
from app.models.scm_ot import (
    ScmEtiquetaManga,
    ScmManga,
    ScmPesajeManga,
    ScmTrabajoImpresionManga,
)
from app.models.scm_production_orders import (
    ScmOrdenOperacion,
    ScmOrdenProduccion,
)
from app.models.scm_warehouse import ScmExistenciaManga
from app.models.trabajador import Trabajador
from app.services.scm_demo_seed_service import (
    ALEMBIC_HEAD as DEMO_ALEMBIC_HEAD,
    seed_alcancia_pablo_demo,
)
from app.services.scm_inventory_opening_service import (
    create_inventory_opening,
    resolve_inventory_opening,
    submit_inventory_opening,
)
from app.services.scm_ot_service import acknowledge_station_print_job
from app.services.scm_uat_walkthrough_seed_service import seed_uat_walkthrough
from app.services.scm_weighing_service import confirm_manga_weighing
from app.services.scm_warehouse_service import (
    decide_manga_quality,
    receive_manga,
)


PORTFOLIO_DATABASE_FILENAME = "envaperu_portfolio_demo.db"
PORTFOLIO_MARKER = "ENVAPERU_PORTFOLIO_PILOT_V1"


class PortfolioDemoError(RuntimeError):
    """The portfolio data set cannot be prepared safely."""


def _stable_uuid(suffix: str):
    return uuid5(NAMESPACE_URL, f"{PORTFOLIO_MARKER}:{suffix}")


def assert_portfolio_demo_database(database_url: str, *, demo_mode: str) -> None:
    """Fail closed unless the target is the dedicated SQLite demo file."""

    if str(demo_mode or "").strip().lower() != "portfolio":
        raise PortfolioDemoError("SCM_DEMO_MODE debe ser portfolio.")
    try:
        parsed = make_url(database_url)
    except (TypeError, ValueError) as error:
        raise PortfolioDemoError("La URL de base de la demo no es valida.") from error
    if parsed.get_backend_name() != "sqlite":
        raise PortfolioDemoError("La demo publica exige una base SQLite aislada.")
    normalized = str(parsed.database or "").replace("\\", "/")
    if PurePosixPath(normalized).name != PORTFOLIO_DATABASE_FILENAME:
        raise PortfolioDemoError(
            f"La demo solo puede usar {PORTFOLIO_DATABASE_FILENAME}."
        )


def _count_by_state(session, model):
    rows = session.execute(
        select(model.estado, func.count()).group_by(model.estado)
    ).all()
    return {str(state): int(total) for state, total in rows}


def portfolio_demo_status(session):
    active_ot = session.scalar(
        select(RegistroDiarioProduccion)
        .where(RegistroDiarioProduccion.estado == "EN_EJECUCION")
        .order_by(RegistroDiarioProduccion.id.desc())
    )
    pending_manga = session.scalar(
        select(ScmManga)
        .where(ScmManga.estado == "PREETIQUETADA")
        .order_by(ScmManga.secuencia_ot)
    )
    received_manga = session.scalar(
        select(ScmManga)
        .where(ScmManga.estado == "RECIBIDA")
        .order_by(ScmManga.secuencia_ot)
    )
    return {
        "status": "ready",
        "mode": "portfolio",
        "scenario": "Primer piloto SCM EnvaPeru",
        "synthetic_data": True,
        "hardware_mode": "simulated",
        "resettable": True,
        "generated_at": datetime.now(ZoneInfo("America/Lima")).isoformat(),
        "counts": {
            "actors": session.query(Trabajador).count(),
            "articles": session.query(ScmArticulo).count(),
            "production_orders": session.query(ScmOrdenProduccion).count(),
            "operation_orders": session.query(ScmOrdenOperacion).count(),
            "work_orders": session.query(RegistroDiarioProduccion).count(),
            "mangas": session.query(ScmManga).count(),
            "weighings": session.query(ScmPesajeManga).count(),
            "warehouse_existences": session.query(ScmExistenciaManga).count(),
            "inventory_openings": session.query(ScmLoteAperturaInventario).count(),
            "inventory_movements": (
                session.query(ScmMovimientoInventario).count()
                + session.query(ScmMovimientoMaterialInventario).count()
            ),
            "print_jobs": session.query(ScmTrabajoImpresionManga).count(),
        },
        "manga_states": _count_by_state(session, ScmManga),
        "highlights": {
            "active_ot": active_ot.codigo_ot if active_ot else None,
            "pending_manga": pending_manga.codigo if pending_manga else None,
            "received_manga": received_manga.codigo if received_manga else None,
        },
    }


def _seed_initial_inventory(walkthrough):
    suggestion = walkthrough["opening_suggestion"]
    created = create_inventory_opening(
        db.session,
        actor_id=suggestion["preparer_actor_id"],
        operation_id=_stable_uuid("inventory-opening:create"),
        data=suggestion["payload"],
    )
    submitted = submit_inventory_opening(
        db.session,
        actor_id=suggestion["preparer_actor_id"],
        opening_id=UUID(created["id"]),
        operation_id=_stable_uuid("inventory-opening:submit"),
        version=created["version"],
    )
    return resolve_inventory_opening(
        db.session,
        actor_id=suggestion["approver_actor_id"],
        opening_id=UUID(created["id"]),
        operation_id=_stable_uuid("inventory-opening:approve"),
        data={
            "version": submitted["version"],
            "decision": "APROBAR",
            "motivo_resolucion": "Conteo inicial validado para la demo publica",
        },
    )


def _seed_weighing_and_receipt(*, walkthrough, fabrication_demo, now):
    weighing = confirm_manga_weighing(
        db.session,
        station_id=fabrication_demo["station_id"],
        operation_id=_stable_uuid("weighing:confirm"),
        actor_id=fabrication_demo["operator_id"],
        data={
            "label_id": fabrication_demo["label_ids"][0],
            "capture_id": str(_stable_uuid("weighing:capture")),
            "peso_bruto_kg": "8.385",
            "tara_kg": "0.010",
            "tara_fuente": "TIPO_MANGA",
            "pesada_at": now.isoformat(),
            "pesado_por_id": fabrication_demo["operator_id"],
            "reading_stable": True,
        },
    )
    acknowledge_station_print_job(
        db.session,
        station_id=fabrication_demo["station_id"],
        print_job_id=UUID(weighing["print_job_id"]),
        data={"results": [{
            "label_id": weighing["post_label"]["public_id"],
            "estado": "IMPRESA",
            "printer_name": "TSC_SIMULADA",
        }]},
    )
    received = receive_manga(
        db.session,
        actor_id=walkthrough["actor_ids"]["almacen"],
        operation_id=_stable_uuid("warehouse:receive"),
        data={
            "label_id": weighing["post_label"]["public_id"],
            "ubicacion_codigo": walkthrough["location_codes"]["finished_product"],
            "presencia_confirmada": True,
            "bolsa_cerrada": True,
            "coincidencia_etiquetas": True,
        },
    )
    released = decide_manga_quality(
        db.session,
        actor_id=walkthrough["actor_ids"]["calidad"],
        existence_id=UUID(received["existencia"]["id"]),
        operation_id=_stable_uuid("warehouse:quality-release"),
        data={
            "decision": "LIBERADA",
            "motivo": "Muestra conforme para el recorrido del portafolio",
            "version": received["existencia"]["version"],
        },
    )
    return {
        "weighing_id": weighing["weighing"]["public_id"],
        "existence_id": released["existencia"]["id"],
    }


def prepare_portfolio_demo(
    *,
    database_url: str,
    demo_mode: str,
    operational_date: date | None = None,
    reset_schema: bool = True,
):
    """Rebuild a complete synthetic pilot without touching external data."""

    assert_portfolio_demo_database(database_url, demo_mode=demo_mode)
    now = datetime.now(ZoneInfo("America/Lima"))
    business_date = operational_date or now.date()
    if reset_schema:
        db.session.remove()
        db.drop_all()
        db.create_all()

    walkthrough = seed_uat_walkthrough(
        db.session,
        database_url=database_url,
        connection_database=PORTFOLIO_DATABASE_FILENAME,
        migration_revision=DEMO_ALEMBIC_HEAD,
        operational_date=business_date,
        validate_environment=False,
    )
    opening = _seed_initial_inventory(walkthrough)
    fabrication_demo = seed_alcancia_pablo_demo(
        db.session,
        database_url=database_url,
        connection_database=PORTFOLIO_DATABASE_FILENAME,
        migration_revision=DEMO_ALEMBIC_HEAD,
        operational_date=business_date,
        validate_environment=False,
    )
    evidence = _seed_weighing_and_receipt(
        walkthrough=walkthrough,
        fabrication_demo=fabrication_demo,
        now=now,
    )
    status = portfolio_demo_status(db.session)
    status["seed"] = {
        "opening_id": opening["id"],
        "weighing_id": evidence["weighing_id"],
        "existence_id": evidence["existence_id"],
    }
    return status
