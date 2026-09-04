import pytest
from types import SimpleNamespace

from app.services import scm_weighing_service as service


def test_transparent_identity_collapses_repeated_base_and_family_name():
    transparent = SimpleNamespace(
        id=7,
        color_base_rel=SimpleNamespace(id=4, nombre="TRANSPARENTE"),
        familia_color_rel=SimpleNamespace(id=3, nombre="TRANSPARENTE"),
        hex_referencia="#EAF7F7",
    )
    color_work = SimpleNamespace(
        color_id_snapshot=7,
        color_nombre_snapshot="TRANSPARENTE TRANSPARENTE",
        corrida=SimpleNamespace(color_produccion=transparent),
    )
    current_work = SimpleNamespace(trabajo_color=color_work)
    manga = SimpleNamespace(color_snapshot="TRANSPARENTE TRANSPARENTE")

    assert service._weighing_color_identity(manga, current_work) == {
        "id": 7,
        "nombre": "TRANSPARENTE",
        "base": {"id": 4, "nombre": "TRANSPARENTE"},
        "familia": {"id": 3, "nombre": "TRANSPARENTE"},
        "hex": "#EAF7F7",
    }


@pytest.mark.parametrize("overrides, expected", [
    ({"manga_state": "PESADA", "has_weighing": True}, "MANGA_CERRADA"),
    ({"manga_state": "RECIBIDA"}, "MANGA_CERRADA"),
    ({"manga_state": "ANULADA", "has_weighing": True}, "MANGA_ANULADA"),
    ({"work_state": "PROGRAMADO"}, "TRABAJO_NO_INICIADO"),
    ({"work_state": "FINALIZADO"}, "TRABAJO_NO_HABILITADO"),
    ({"manga_state": "CONTINUIDAD_PENDIENTE"}, "CONTINUIDAD_PENDIENTE"),
    ({"manga_state": "EN_LLENADO", "segment_state": "PROGRAMADO"}, "CONTINUIDAD_NO_INICIADA"),
    ({"label_state": "PENDIENTE"}, "ETIQUETA_NO_IMPRESA"),
    ({"label_type": "POSTPESAJE"}, "PREETIQUETA_REQUERIDA"),
    ({"is_assembly": True}, "ARMADO_PENDIENTE_CIERRE"),
    ({"is_assembly": True, "action": "registrar_avance_kg"}, "CONTROL_ARMADO_NO_DISPONIBLE"),
    ({"manga_state": "ESTADO_FUTURO"}, "MANGA_NO_HABILITADA"),
])
def test_disabled_action_has_specific_reason_and_recovery(overrides, expected):
    values = dict(manga_state="PREETIQUETADA", label_type="PREPESAJE",
                  label_state="IMPRESA", has_weighing=False,
                  work_state="EN_EJECUCION", segment_state=None,
                  is_assembly=False, action="completar_final")
    values.update(overrides)
    reason = service._weighing_block_reason(**values)
    assert reason["codigo"] == expected
    assert reason["mensaje"]
    assert reason["recuperacion"]
    assert reason["responsable"] in {"CENTRAL", "ESTACION"}
