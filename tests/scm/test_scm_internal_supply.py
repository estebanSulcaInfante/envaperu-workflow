from datetime import date
from collections import Counter
from decimal import Decimal
import re
from uuid import uuid4

import pytest
from sqlalchemy import event

from app import db
from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_articulos import ScmArticulo
from app.models.scm_estructuras import (
    ScmEstructuraComponente,
    ScmEstructuraRevision,
)
from app.models.scm_production_orders import (
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
)
from app.models.scm_ot import ScmTrabajoOt
from app.models.scm_rutas import (
    ScmCentroTrabajo,
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_internal_supply_service import (
    create_assembly_ot,
    create_supply_request,
    list_assembly_ots,
)
from app.services.scm_assembly_order_service import transition_assembly_order
from app.services.scm_service_support import ScmServiceError


def _count_selects(callback):
    statements = []

    def before_cursor_execute(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", before_cursor_execute)
    try:
        result = callback()
    finally:
        event.remove(
            db.engine, "before_cursor_execute", before_cursor_execute
        )
    return result, statements


def _statement_tables(statements):
    tables = []
    for statement in statements:
        match = re.search(r"\bFROM\s+([^\s]+)", statement, re.IGNORECASE)
        tables.append(match.group(1) if match else statement.splitlines()[0])
    return Counter(tables)


def _assembly_order():
    actor = Trabajador.query.filter_by(codigo="TRB-01").one()
    for code in ("JEFE_PRODUCCION", "JEFE_ENSAMBLE"):
        role = RolOperativo.query.filter_by(codigo=code).one()
        if role not in actor.roles:
            actor.roles.append(role)
    component = ScmArticulo(
        codigo="PC-H-001",
        nombre="Cuerpo de balde amarillo",
        clase="PIEZA_COLOR",
    )
    result = ScmArticulo(
        codigo="PT-H-001",
        nombre="Balde armado",
        clase="PRODUCTO_TERMINADO",
    )
    center = ScmCentroTrabajo(
        codigo="MESA-H-01", nombre="Mesa de armado H", tipo="ENSAMBLE"
    )
    structure = ScmEstructuraRevision(
        articulo_resultado=result,
        numero_revision=1,
        estado="APROBADA",
        content_hash="1" * 64,
        creada_por_id=actor.id,
        aprobada_por_id=actor.id,
        componentes=[ScmEstructuraComponente(
            secuencia=1,
            articulo_componente=component,
            cantidad=2,
            unidad="UN",
        )],
    )
    route = ScmRutaRevision(
        articulo_objetivo=result,
        numero_revision=1,
        estado="APROBADA",
        content_hash="2" * 64,
        creada_por_id=actor.id,
        aprobada_por_id=actor.id,
    )
    db.session.add_all([component, result, center, structure, route])
    db.session.flush()
    operation = ScmOperacionRuta(
        ruta=route,
        clave="ENSAMBLAR",
        secuencia_visible=1,
        nombre="Armar balde",
        tipo="ENSAMBLE",
        executor_kind="ORDEN_OPERACION",
        centro_trabajo=center,
        articulo_salida=result,
        estructura_revision=structure,
    )
    db.session.add(operation)
    db.session.flush()
    order = ScmOrdenOperacion(
        codigo="OA-H-000001",
        tipo="ENSAMBLE",
        origen_demanda="ORDEN_PRODUCCION",
        estado="LIBERADA",
        operacion_ruta_revision_id=operation.id,
        operacion_ruta_hash=route.content_hash,
        created_by_id=actor.id,
        released_by_id=actor.id,
        salidas=[ScmOrdenOperacionSalida(
            articulo=result,
            cantidad_objetivo=Decimal("10.000"),
        )],
    )
    db.session.add(order)
    db.session.commit()
    return actor, order, center


def test_assembly_ot_and_supply_request_derive_bom_by_daily_quota(app, scm_config):
    with app.app_context():
        actor, order, center = _assembly_order()
        created = create_assembly_ot(
            db.session,
            actor_id=actor.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={
                "fecha_operativa": date(2026, 8, 4).isoformat(),
                "turno": "DIA",
                "centro_trabajo_id": center.id,
                "responsable_id": actor.id,
                "cantidad_objetivo": 5,
            },
        )
        assert created["ot"]["tipo_ot"] == "ENSAMBLE"
        assert created["ot"]["maquina_id"] is None
        assert created["ot"]["cantidad_objetivo"] == "5.000"

        request = create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=created["ot"]["public_id"],
            operation_id=uuid4(),
        )["solicitud"]
        assert request["estado"] == "SOLICITADA"
        assert request["lineas"][0]["articulo"]["codigo"] == "PC-H-001"
        assert request["lineas"][0]["cantidad_requerida"] == "10.000"


def test_assembly_ot_listing_enrichment_uses_bounded_queries(app, scm_config):
    with app.app_context():
        actor, order, center = _assembly_order()
        first = create_assembly_ot(
            db.session,
            actor_id=actor.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-08-04",
                "turno": "DIA",
                "centro_trabajo_id": center.id,
                "responsable_id": actor.id,
                "cantidad_objetivo": 4,
            },
        )["ot"]
        create_supply_request(
            db.session,
            actor_id=actor.id,
            ot_id=first["public_id"],
            operation_id=uuid4(),
        )
        db.session.expire_all()
        first_items, first_statements = _count_selects(
            lambda: list_assembly_ots(
                db.session,
                actor_id=actor.id,
                order_id=order.id,
            )["items"]
        )
        assert len(first_items) == 1

        create_assembly_ot(
            db.session,
            actor_id=actor.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-08-05",
                "turno": "DIA",
                "centro_trabajo_id": center.id,
                "responsable_id": actor.id,
                "cantidad_objetivo": 6,
            },
        )
        db.session.expire_all()
        second_items, second_statements = _count_selects(
            lambda: list_assembly_ots(
                db.session,
                actor_id=actor.id,
                order_id=order.id,
            )["items"]
        )

        assert len(second_items) == 2
        assert len(second_statements) <= len(first_statements) + 1, (
            _statement_tables(second_statements)
            - _statement_tables(first_statements),
        )
        assert {item["abastecimiento"] is None for item in second_items} == {
            True,
            False,
        }


def test_assembly_ot_quota_cannot_exceed_order_target(app, scm_config):
    with app.app_context():
        actor, order, center = _assembly_order()
        common = {
            "fecha_operativa": "2026-08-04",
            "turno": "DIA",
            "centro_trabajo_id": center.id,
            "responsable_id": actor.id,
            "cantidad_objetivo": 8,
        }
        create_assembly_ot(
            db.session,
            actor_id=actor.id,
            order_id=order.id,
            operation_id=uuid4(),
            data=common,
        )
        with pytest.raises(ScmServiceError) as error:
            create_assembly_ot(
                db.session,
                actor_id=actor.id,
                order_id=order.id,
                operation_id=uuid4(),
                data={**common, "fecha_operativa": "2026-08-05", "cantidad_objetivo": 3},
            )
        assert error.value.code == "ASSEMBLY_OT_QUOTA_EXCEEDED"


def test_concurrent_assembly_ot_requires_allowed_route_and_fabrication_context(
    app, scm_config
):
    with app.app_context():
        actor, order, center = _assembly_order()
        with pytest.raises(ScmServiceError) as error:
            create_assembly_ot(
                db.session,
                actor_id=actor.id,
                order_id=order.id,
                operation_id=uuid4(),
                data={
                    "fecha_operativa": "2026-08-04",
                    "turno": "DIA",
                    "centro_trabajo_id": center.id,
                    "responsable_id": actor.id,
                    "cantidad_objetivo": 5,
                    "modo_ejecucion": "CONCURRENTE",
                },
            )
        assert error.value.code == "ASSEMBLY_CONCURRENT_NOT_ALLOWED"


def test_concurrent_assembly_ot_requires_exact_color_work(app, scm_config):
    with app.app_context():
        actor, order, center = _assembly_order()
        operation = db.session.get(ScmOperacionRuta, order.operacion_ruta_revision_id)
        operation.permite_concurrente = True
        machine = Maquina.query.first()
        fabrication_ot = RegistroDiarioProduccion(
            codigo_ot=f"OT-FAB-{uuid4().hex[:8].upper()}",
            codigo_ot_sintetico=False,
            estado="EN_EJECUCION",
            tipo_ot="FABRICACION",
            maquina_id=machine.id,
            fecha=date(2026, 8, 4),
            turno="DIA",
            created_by_id=actor.id,
        )
        db.session.add(fabrication_ot)
        db.session.commit()

        with pytest.raises(ScmServiceError) as error:
            create_assembly_ot(
                db.session,
                actor_id=actor.id,
                order_id=order.id,
                operation_id=uuid4(),
                data={
                    "fecha_operativa": "2026-08-04",
                    "turno": "DIA",
                    "centro_trabajo_id": center.id,
                    "responsable_id": actor.id,
                    "cantidad_objetivo": 5,
                    "modo_ejecucion": "CONCURRENTE",
                    "ot_fabricacion_contexto_id": str(
                        fabrication_ot.public_id
                    ),
                },
            )

        assert error.value.code == "ASSEMBLY_COLOR_WORK_CONTEXT_REQUIRED"


def test_concurrent_assembly_ot_accepts_color_work_adapter(app, scm_config):
    with app.app_context():
        actor, order, center = _assembly_order()
        operation = db.session.get(ScmOperacionRuta, order.operacion_ruta_revision_id)
        operation.permite_concurrente = True
        machine = Maquina.query.first()
        fabrication_ot = RegistroDiarioProduccion(
            codigo_ot=f"OT-FAB-{uuid4().hex[:8].upper()}",
            codigo_ot_sintetico=False,
            estado="EN_EJECUCION",
            tipo_ot="FABRICACION",
            maquina_id=machine.id,
            fecha=date(2026, 8, 4),
            turno="DIA",
            created_by_id=actor.id,
            secuencia_siguiente_trabajo=2,
        )
        db.session.add(fabrication_ot)
        db.session.flush()
        fabrication_order = ScmOrdenOperacion(
            codigo=f"OF-H-{uuid4().hex[:8].upper()}",
            tipo="FABRICACION",
            origen_demanda="ORDEN_PRODUCCION",
            estado="LIBERADA",
            created_by_id=actor.id,
        )
        db.session.add(fabrication_order)
        db.session.flush()
        color_work = ScmTrabajoOt(
            orden_trabajo_id=fabrication_ot.id,
            codigo=f"{fabrication_ot.codigo_ot}-TC01",
            tipo="COLOR",
            secuencia=1,
            estado="EN_EJECUCION",
            orden_operacion_id=fabrication_order.id,
            cantidad_objetivo_un=5,
            cantidad_confirmada_un=0,
            created_by_id=actor.id,
        )
        db.session.add(color_work)
        db.session.commit()

        created = create_assembly_ot(
            db.session,
            actor_id=actor.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-08-04",
                "turno": "DIA",
                "centro_trabajo_id": center.id,
                "responsable_id": actor.id,
                "cantidad_objetivo": 5,
                "modo_ejecucion": "CONCURRENTE",
                "trabajo_color_contexto_id": str(color_work.id),
            },
        )["ot"]

        assert created["ot_fabricacion_contexto_id"] == str(fabrication_ot.public_id)
        assert created["trabajo_color_contexto_id"] == str(color_work.id)

        listed = list_assembly_ots(
            db.session,
            actor_id=actor.id,
            order_id=order.id,
        )["items"]
        listed_ot = next(
            item for item in listed
            if item["public_id"] == created["public_id"]
        )
        assert listed_ot["ot_fabricacion_contexto"]["codigo_ot"] == (
            fabrication_ot.codigo_ot
        )
        assert listed_ot["trabajo_color_contexto"]["id"] == str(color_work.id)
        assert listed_ot["trabajo_color_contexto"]["estado"] == "EN_EJECUCION"


def test_concurrent_assembly_ot_rejects_color_work_linked_to_assembly_order(
    app, scm_config
):
    with app.app_context():
        actor, order, center = _assembly_order()
        operation = db.session.get(
            ScmOperacionRuta, order.operacion_ruta_revision_id
        )
        operation.permite_concurrente = True
        machine = Maquina.query.first()
        fabrication_ot = RegistroDiarioProduccion(
            codigo_ot=f"OT-FAB-{uuid4().hex[:8].upper()}",
            codigo_ot_sintetico=False,
            estado="EN_EJECUCION",
            tipo_ot="FABRICACION",
            maquina_id=machine.id,
            fecha=date(2026, 8, 4),
            turno="DIA",
            created_by_id=actor.id,
            secuencia_siguiente_trabajo=2,
        )
        db.session.add(fabrication_ot)
        db.session.flush()
        invalid_work = ScmTrabajoOt(
            orden_trabajo_id=fabrication_ot.id,
            codigo=f"{fabrication_ot.codigo_ot}-TC01",
            tipo="COLOR",
            secuencia=1,
            estado="EN_EJECUCION",
            orden_operacion_id=order.id,
            cantidad_objetivo_un=5,
            cantidad_confirmada_un=0,
            created_by_id=actor.id,
        )
        db.session.add(invalid_work)
        db.session.commit()

        with pytest.raises(ScmServiceError) as error:
            create_assembly_ot(
                db.session,
                actor_id=actor.id,
                order_id=order.id,
                operation_id=uuid4(),
                data={
                    "fecha_operativa": "2026-08-04",
                    "turno": "DIA",
                    "centro_trabajo_id": center.id,
                    "responsable_id": actor.id,
                    "cantidad_objetivo": 5,
                    "modo_ejecucion": "CONCURRENTE",
                    "trabajo_color_contexto_id": str(invalid_work.id),
                },
            )

        assert error.value.code == "ASSEMBLY_COLOR_WORK_NOT_FABRICATION"


def test_traceable_close_waits_until_assembly_ot_is_terminal(
    app, scm_config
):
    with app.app_context():
        actor, order, center = _assembly_order()
        create_assembly_ot(
            db.session,
            actor_id=actor.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-08-04",
                "turno": "DIA",
                "centro_trabajo_id": center.id,
                "responsable_id": actor.id,
                "cantidad_objetivo": 5,
            },
        )
        order = db.session.get(ScmOrdenOperacion, order.id)
        order.estado = "EN_EJECUCION"
        db.session.commit()

        with pytest.raises(ScmServiceError) as error:
            transition_assembly_order(
                db.session,
                actor_id=actor.id,
                operation_id=uuid4(),
                order_id=order.id,
                action="cerrar",
                data={
                    "version": order.version,
                    "cantidad_real": 5,
                    "cantidad_rechazada": 0,
                },
            )

        assert error.value.code == "OA_HAS_PENDING_OTS"

        db.session.rollback()
        order = db.session.get(ScmOrdenOperacion, order.id)
        traceable_ot = RegistroDiarioProduccion.query.filter_by(
            orden_operacion_id=order.id,
            tipo_ot="ENSAMBLE",
        ).one()
        traceable_ot.estado = "CERRADA"
        order.salidas[0].cantidad_real = order.salidas[0].cantidad_objetivo
        db.session.commit()

        closed = transition_assembly_order(
            db.session,
            actor_id=actor.id,
            operation_id=uuid4(),
            order_id=order.id,
            action="cerrar",
            data={"version": order.version},
        )
        assert closed["estado"] == "CERRADA"
        assert closed["salida"]["cantidad_real"] == (
            format(order.salidas[0].cantidad_objetivo, "f")
        )
