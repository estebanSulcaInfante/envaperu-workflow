from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

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
from app.models.scm_rutas import (
    ScmCentroTrabajo,
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_internal_supply_service import (
    create_assembly_ot,
    create_supply_request,
)
from app.services.scm_assembly_order_service import transition_assembly_order
from app.services.scm_service_support import ScmServiceError


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
        codigo="OE-H-000001",
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


def test_concurrent_assembly_ot_stores_fabrication_context(app, scm_config):
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
                "ot_fabricacion_contexto_id": str(fabrication_ot.public_id),
            },
        )["ot"]

        assert created["modo_ejecucion_ensamble"] == "CONCURRENTE"
        assert created["ot_fabricacion_contexto_id"] == str(fabrication_ot.public_id)
        assert created["ot_fabricacion_contexto"]["codigo_ot"] == fabrication_ot.codigo_ot


def test_legacy_close_is_blocked_when_assembly_order_has_traceable_ot(
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

        assert error.value.code == "OE_TRACEABLE_CLOSE_REQUIRED"
