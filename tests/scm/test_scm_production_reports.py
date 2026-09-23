from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

from openpyxl import load_workbook

from app.services.scm_production_reports_service import (
    _filters,
    _group_history_rows,
    MEASURE_OPTIONS,
    _run_manga_values,
    _segment_conciliates,
    _valid_kg_segments,
)
from app.services.scm_service_support import ScmServiceError


def _segment(sequence, start, end, attributed, quality="MEDIDA_DIRECTA", attributed_un=None):
    return SimpleNamespace(
        id=sequence,
        secuencia=sequence,
        estado="CERRADO",
        cantidad_inicio_kg=Decimal(start),
        cantidad_fin_kg=Decimal(end),
        cantidad_atribuida_kg=Decimal(attributed),
        cantidad_atribuida_un=attributed_un,
        cerrada_at=None,
        calidad_evidencia_kg=quality,
    )


def test_kg_segments_require_zero_continuity_and_exact_net():
    segments = [_segment(1, "0", "4", "4"), _segment(2, "4", "9", "5")]
    assert _segment_conciliates(segments, Decimal("9")) is True
    assert _segment_conciliates(segments, Decimal("8")) is False
    assert _segment_conciliates(
        [_segment(1, "0", "4", "3"), _segment(2, "4", "9", "5")],
        Decimal("9"),
    ) is False


def test_un_and_default_kg_segments_are_not_evidence():
    manga = SimpleNamespace(
        tramos_trabajo=[
            _segment(1, "0", "4", "4", quality="MEDIDA_DIRECTA"),
            _segment(2, "4", "9", "5", quality="PENDIENTE"),
            _segment(3, "9", "10", "1", quality="MEDIDA_DIRECTA"),
        ]
    )
    assert [segment.secuencia for segment in _valid_kg_segments(manga, {})] == [1, 3]


def test_explicit_control_close_is_kg_evidence():
    manga = SimpleNamespace(tramos_trabajo=[_segment(1, "0", "9", "9", quality="MEDIDA_DIRECTA_CIERRE_CONTROL")])
    assert [segment.secuencia for segment in _valid_kg_segments(manga, {})] == [1]


def test_objective_without_mangas_does_not_claim_complete_weight_coverage():
    corrida = SimpleNamespace(objetivo_neto_kg=Decimal("10"))
    run = {"corrida": corrida, "mangas": {}}
    _final, _open, measured, total, known, _objective = _run_manga_values(run)
    assert (measured, total, known) == (None, 0, 0)


def test_history_subtotal_deduplicates_manga_across_kg_segments_and_groups():
    rows = []
    for day, ot, kg in (("2026-09-01", "OT-1", 4), ("2026-09-02", "OT-2", 5)):
        rows.append({
            "DIA": day, "MES": "2026-09", "OF": "OF-1", "CORRIDA": "C-1",
            "COLOR": "Rojo", "OT": ot, "RECURSO": "M1", "RESPONSABLE": "R",
            "ARTICULO": "A", "PESO_KG": kg, "SUBTOTAL_CONOCIDO_KG": kg,
            "SUBTOTAL_TEORICO_KG": None, "MANGAS": 1, "P_UNITARIO_G": 100,
            "P_UNITARIO_WEIGHT": 100, "P_UNITARIO_QTY": 1, "P_TEORICO_KG": 1,
            "_known": True, "_manga_id": 77,
        })
    for groups in (("OF",), ("DIA",), ("DIA", "OT")):
        items = _group_history_rows(rows, {"groups": list(groups), "measures": list(MEASURE_OPTIONS)})
        assert len(items) == (1 if groups == ("OF",) else 2)
        assert sum(item["PESO_KG"] for item in items) == 9
        assert sorted(item["PESO_KG"] for item in items) == ([9] if groups == ("OF",) else [4, 5])


def test_theoretical_measure_stays_null_without_un_evidence():
    row = {
        "DIA": "2026-09-01", "PESO_KG": 9, "SUBTOTAL_CONOCIDO_KG": 9,
        "SUBTOTAL_TEORICO_KG": 2, "P_TEORICO_KG": None, "MANGAS": 1,
        "P_UNITARIO_WEIGHT": None, "P_UNITARIO_QTY": None, "P_UNITARIO_G": None,
        "coverage": "INCOMPLETA", "_known": False, "_manga_id": 11,
    }
    item = _group_history_rows([row], {"groups": ["DIA"], "measures": list(MEASURE_OPTIONS)})[0]
    assert item["P_TEORICO_KG"] is None
    assert item["SUBTOTAL_TEORICO_KG"] == 2


def test_context_filter_does_not_turn_excluded_conciliated_segments_into_fallback():
    from datetime import date

    work = SimpleNamespace(id="work-1", codigo="TR-1")
    ot = SimpleNamespace(
        fecha=date(2026, 9, 1), codigo_ot="OT-1", estado="CERRADA",
        maquina_nombre_snapshot="M1", maquina_codigo_snapshot=None,
        responsable=None, orden_operacion=None,
    )
    color_work = SimpleNamespace(peso_neto_snapshot_g=100)
    manga = SimpleNamespace(
        id=77, _report_final_kg=Decimal("9"),
        _report_segments=[
            _segment(1, "0", "4", "4", attributed_un=40),
            _segment(2, "4", "9", "5", attributed_un=50),
        ], peso_unitario_snapshot_g=100, cantidad_confirmada_un=90,
        cantidad_asignada_un=90, articulo_codigo_snapshot="A",
        correccion_asignacion=None,
    )
    manga._report_segments[0].trabajo = work
    manga._report_segments[1].trabajo = work
    run = {
        "corrida": SimpleNamespace(id="run-1", codigo="C-1", objetivo_neto_kg=9),
        "orden": SimpleNamespace(codigo="OF-1", estado="CERRADA"),
        "ot": ot, "work": work, "color_work": color_work,
        "color_name": "Rojo", "resource": "M1", "responsible": None,
        "contexts": [{"work": work, "color_work": color_work, "ot": ot}],
        "mangas": {77: manga},
    }
    from app.services.scm_production_reports_service import _history_rows
    for raw in ({"q": "OT-2"}, {"estado_ot": "ANULADA"}):
        filters = _filters({"fecha_desde": "2026-09-01", "fecha_hasta": "2026-09-01", **raw})
        assert _history_rows([run], ["DIA"], filters) == []


def test_report_filters_reject_unknown_group_and_measure_without_fallback():
    for key, value, code in (
        ("agrupaciones", "NO_EXISTE", "INVALID_OBSERVABILITY_GROUP"),
        ("medidas", "NO_EXISTE", "INVALID_OBSERVABILITY_MEASURE"),
    ):
        try:
            _filters({"fecha_desde": "2026-09-01", "fecha_hasta": "2026-09-02", key: value})
        except ScmServiceError as error:
            assert error.status_code == 400
            assert error.code == code
        else:
            raise AssertionError("el filtro inválido no debe usar fallback")


def test_history_requires_operational_date_range(app, client, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        response = client.get(
            "/api/scm/v1/observabilidad/produccion-historica",
            headers={"X-Actor-Id": str(seeded["full"].id)},
        )
        assert response.status_code == 400
        assert "fecha_desde" in response.get_json()["error"]["message"]


def test_history_export_requires_weighing_capability_and_returns_workbook(app, client, scm_config):
    from test_scm_production_observability import _seed_observability_graph

    with app.app_context():
        seeded = _seed_observability_graph()
        params = {"fecha_desde": "2026-08-01", "fecha_hasta": "2026-08-31"}
        denied = client.get(
            "/api/scm/v1/observabilidad/produccion-historica/export.xlsx",
            query_string=params,
            headers={"X-Actor-Id": str(seeded["base"].id)},
        )
        assert denied.status_code == 403
        exported = client.get(
            "/api/scm/v1/observabilidad/produccion-historica/export.xlsx",
            query_string=params,
            headers={"X-Actor-Id": str(seeded["full"].id)},
        )
        assert exported.status_code == 200
        assert exported.mimetype == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        workbook = load_workbook(BytesIO(exported.data), read_only=True, data_only=True)
        assert workbook.sheetnames == ["Resumen", "Datos"]
        headers = next(workbook["Datos"].iter_rows(values_only=True))
        assert "SUBTOTAL_CONOCIDO_KG" in headers
        assert "PESO_KG" in headers
