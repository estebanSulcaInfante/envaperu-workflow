from types import SimpleNamespace

from app.services.scm_process_resolution import (
    resolve_order_process,
    route_operation_dto,
)


def _order(*, state="BORRADOR", snapshot=None, source=None, route=None, mold=None):
    return SimpleNamespace(
        estado=state,
        fabricacion=SimpleNamespace(
            snapshot_proceso=snapshot,
            fuente_proceso=source,
            molde_id=mold,
        ),
        origen_demanda="EXCEPCIONAL",
        operacion_ruta_revision=route,
    )


def test_explicit_snapshot_is_authoritative():
    assert resolve_order_process(
        _order(snapshot="SOPLADO", source="EXPLICITO")
    ) == ("SOPLADO", "EXPLICITO", None)


def test_draft_legacy_without_snapshot_stays_pending():
    assert resolve_order_process(_order()) == (None, None, "LEGACY_PENDIENTE")


def test_released_legacy_read_keeps_compatibility_assumption():
    assert resolve_order_process(_order(state="LIBERADA", mold="ML-1")) == (
        "INYECCION",
        None,
        "LEGACY_ASUMIDO_INYECCION",
    )


def test_header_route_is_resolved_as_route_cabecera():
    route = SimpleNamespace(
        tipo="SOPLADO",
        ruta=SimpleNamespace(estado="APROBADA"),
    )
    assert resolve_order_process(_order(state="LIBERADA", route=route)) == (
        "SOPLADO",
        "RUTA_CABECERA",
        None,
    )


def test_planned_draft_header_route_is_read_only_process_projection():
    route = SimpleNamespace(
        tipo="INYECCION",
        ruta=SimpleNamespace(estado="APROBADA"),
    )
    order = _order(state="BORRADOR", route=route)
    order.origen_demanda = "ORDEN_PRODUCCION"
    assert resolve_order_process(order) == ("INYECCION", "RUTA_CABECERA", None)
def test_route_dto_exposes_revision_and_output_article():
    operation = SimpleNamespace(
        id=9,
        clave="SOPLAR",
        nombre="Soplar pieza",
        tipo="SOPLADO",
        executor_kind="OP_OT",
        ruta_revision_id=1,
        ruta=SimpleNamespace(
            id=1,
            numero_revision=2,
            content_hash="a" * 64,
        ),
        articulo_salida_id=44,
        articulo_salida=SimpleNamespace(
            id=44,
            codigo="PC-44",
            nombre="Pieza azul",
            clase="PIEZA_COLOR",
        ),
    )
    assert route_operation_dto(operation) == {
        "id": 9,
        "clave": "SOPLAR",
        "nombre": "Soplar pieza",
        "tipo": "SOPLADO",
        "executor_kind": "OP_OT",
        "ruta_revision_id": 1,
        "ruta_numero_revision": 2,
        "ruta_content_hash": "a" * 64,
        "articulo_salida_id": 44,
        "articulo_salida": {
            "id": 44,
            "codigo": "PC-44",
            "nombre": "Pieza azul",
            "clase": "PIEZA_COLOR",
        },
    }
