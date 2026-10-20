from io import BytesIO
from uuid import UUID, uuid4

from openpyxl import load_workbook
import pytest
from sqlalchemy import event

from app.extensions import db
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_production_orders import ScmOrdenOperacion
from app.models.scm_ot import (
    ScmAsignacionPersonalTrabajoOt,
    ScmAsignacionPlanMangaOt,
    ScmCorreccionAsignacionManga,
    ScmManga,
    ScmTrabajoColor,
    ScmTrabajoOt,
    ScmTramoMangaTrabajo,
)
from app.services.scm_assignment_correction_service import apply_assignment_correction
from app.services.scm_ot_service import create_fabrication_ot_header
from app.services.scm_manga_explorer_service import _canonical_color_hex, _safe_cell
from tests.scm.test_scm_production_observability import _manga, _seed_observability_graph
from tests.scm.test_scm_kg_production import _auto_final_kg_fixture


def _headers(actor):
    return {"X-Actor-Id": str(actor.id)}


def _add_scaled_manga_graph(seeded, count=120):
    """Build a page/export-sized physical set without changing production data."""
    from app.models.scm_ot import ScmTrabajoOt
    from app.models.trabajador import Trabajador

    worker = Trabajador.query.filter_by(codigo="TRB-01").one()
    works = [
        ScmTrabajoOt.query.filter_by(codigo="TC-OBS-ROJO").one(),
        ScmTrabajoOt.query.filter_by(codigo="TC-OBS-AZUL").one(),
    ]
    secondary_operation = ScmOrdenOperacion(
        codigo="OF-OBS-002",
        tipo="FABRICACION",
        origen_demanda="PLANIFICADA",
        plan_produccion_id=works[0].orden_operacion.plan_produccion_id,
        propuesta_clave="OF-2",
        estado="EN_EJECUCION",
        created_by_id=seeded["full"].id,
    )
    secondary_ot = RegistroDiarioProduccion(
        codigo_ot="OT-OBS-FAB-B",
        codigo_ot_sintetico=False,
        estado="EN_EJECUCION",
        tipo_ot="FABRICACION",
        fecha=seeded["fab"].fecha,
        turno="NOCHE",
        maquina_id=1,
        maquina_codigo_snapshot="MAQ-B",
        maquina_nombre_snapshot="Máquina B",
        responsable_id=worker.id,
        maquinista_previsto_id=worker.id,
        created_by_id=seeded["full"].id,
        created_at=seeded["fab"].created_at,
        updated_at=seeded["fab"].updated_at,
    )
    db.session.add_all([secondary_operation, secondary_ot])
    db.session.flush()
    secondary_work = ScmTrabajoOt(
        orden_trabajo_id=secondary_ot.id,
        codigo="TC-OBS-VERDE-B",
        secuencia=1,
        estado="PLANIFICADO",
        orden_operacion_id=secondary_operation.id,
        cantidad_objetivo_un=250,
        cantidad_confirmada_un=150,
        created_by_id=seeded["full"].id,
    )
    db.session.add(secondary_work)
    db.session.flush()
    db.session.add(ScmTrabajoColor(
        trabajo_ot_id=secondary_work.id,
        corrida_fabricacion_id=uuid4(),
        color_nombre_snapshot="VERDE",
    ))
    db.session.flush()
    works.append(secondary_work)
    ots = [seeded["fab"], seeded["fab"], secondary_ot]
    assignments = [
        ScmAsignacionPersonalTrabajoOt(
            trabajo_ot_id=work.id,
            trabajador_id=worker.id,
            asignada_por_id=seeded["full"].id,
            estado="PREVISTA",
        )
        for work in works
    ]
    db.session.add_all(assignments)
    db.session.flush()
    mangas = []
    for sequence in range(100, 100 + count):
        work = works[sequence % len(works)]
        manga = _manga(
            ot=ots[sequence % len(ots)], work=work, sequence=sequence, state="PREETIQUETADA", worker=worker,
        )
        manga.asignacion_personal_trabajo = assignments[sequence % len(assignments)]
        mangas.append(manga)
    db.session.add_all(mangas)
    db.session.flush()
    for index, manga in enumerate(mangas[::10], start=1):
        assignment = assignments[index % len(assignments)]
        db.session.add(ScmTramoMangaTrabajo(
            manga=manga,
            trabajo=assignment.trabajo,
            asignacion_personal_trabajo=assignment,
            secuencia=1,
            estado="ACTIVO",
            cantidad_inicio_un=0,
            cantidad_atribuida_un=0,
            created_by_id=worker.id,
        ))
    db.session.commit()


def _add_auditable_correction(app, station_code):
    """Create a real correction overlay through the canonical service path."""
    from tests.scm.test_assignment_correction_service import _grant_assignment_correction

    creator, _approver, manga, _station, _label, _weighed = _auto_final_kg_fixture(
        app, station_code=station_code,
    )
    _grant_assignment_correction(creator)
    source = manga.trabajo
    source_color = source.trabajo_color
    second_manga = (
        ScmManga.query.filter_by(trabajo_ot_id=source.id)
        .filter(ScmManga.id != manga.id)
        .order_by(ScmManga.id)
        .first()
    )
    assert second_manga is not None
    for corrected_manga in (manga, second_manga):
        corrected_manga.estado = "PESADA"
        db.session.add(ScmTramoMangaTrabajo(
            manga=corrected_manga,
            trabajo=source,
            asignacion_personal_trabajo=corrected_manga.asignacion_personal_trabajo,
            asignacion_plan_id=corrected_manga.asignacion_id,
            secuencia=1,
            estado="CERRADO",
            cantidad_inicio_un=0,
            cantidad_atribuida_un=0,
            cantidad_fin_un=corrected_manga.cantidad_asignada_un,
            motivo_cierre="PESAJE_FINAL",
            created_by_id=creator.id,
        ))
    db.session.flush()
    def _target_work(target_ot, code):
        target = ScmTrabajoOt(
            orden_trabajo_id=target_ot.id,
            codigo=f"{target_ot.codigo_ot}-{code}",
            tipo="COLOR",
            secuencia=1,
            estado="PAUSADO",
            orden_operacion_id=source.orden_operacion_id,
            cantidad_objetivo_un=0,
            cantidad_confirmada_un=0,
            created_by_id=creator.id,
        )
        target.trabajo_color = ScmTrabajoColor(
            corrida_fabricacion_id=source_color.corrida_fabricacion_id,
            molde_codigo_snapshot=source_color.molde_codigo_snapshot,
            color_id_snapshot=source_color.color_id_snapshot,
            color_nombre_snapshot=source_color.color_nombre_snapshot,
            receta_revision_id_snapshot=source_color.receta_revision_id_snapshot,
            receta_hash_snapshot=source_color.receta_hash_snapshot,
            cavidades_snapshot=source_color.cavidades_snapshot,
            peso_neto_snapshot_g=source_color.peso_neto_snapshot_g,
            peso_colada_snapshot_g=source_color.peso_colada_snapshot_g,
        )
        assignment = ScmAsignacionPersonalTrabajoOt(
            trabajo=target,
            trabajador_id=creator.id,
            estado="PREVISTA",
            asignada_por_id=creator.id,
        )
        db.session.add_all([target, assignment])
        db.session.flush()
        target_plan = ScmAsignacionPlanMangaOt(
            plan_linea_id=manga.plan_linea_id,
            ot_id=target_ot.id,
            trabajo_ot_id=target.id,
            cantidad_asignada_un=0,
            mangas_asignadas=0,
            asignada_por_id=creator.id,
        )
        db.session.add(target_plan)
        db.session.flush()
        return target, assignment, target_plan

    active_header = create_fabrication_ot_header(
        db.session,
        actor_id=creator.id,
        operation_id=uuid4(),
        data={
            "maquina_id": source.orden_trabajo.maquina_id,
            "fecha_operativa": "2026-09-19",
            "turno": "DIA",
            "maquinista_predeterminado_id": creator.id,
        },
    )["ot"]
    active_ot = RegistroDiarioProduccion.query.filter_by(
        public_id=UUID(active_header["public_id"]),
    ).one()
    active_work, active_assignment, active_plan = _target_work(active_ot, "C01")

    superseded_header = create_fabrication_ot_header(
        db.session,
        actor_id=creator.id,
        operation_id=uuid4(),
        data={
            "maquina_id": source.orden_trabajo.maquina_id,
            "fecha_operativa": "2026-09-20",
            "turno": "DIA",
            "maquinista_predeterminado_id": creator.id,
        },
    )["ot"]
    superseded_ot = RegistroDiarioProduccion.query.filter_by(
        public_id=UUID(superseded_header["public_id"]),
    ).one()
    superseded_correction_work, superseded_assignment, superseded_plan = _target_work(
        superseded_ot, "C01",
    )
    latest_header = create_fabrication_ot_header(
        db.session,
        actor_id=creator.id,
        operation_id=uuid4(),
        data={
            "maquina_id": source.orden_trabajo.maquina_id,
            "fecha_operativa": "2026-09-21",
            "turno": "DIA",
            "maquinista_predeterminado_id": creator.id,
        },
    )["ot"]
    superseded_latest_ot = RegistroDiarioProduccion.query.filter_by(
        public_id=UUID(latest_header["public_id"]),
    ).one()
    superseded_latest_work, superseded_latest_assignment, superseded_latest_plan = _target_work(
        superseded_latest_ot, "C01",
    )
    db.session.commit()
    apply_assignment_correction(
        db.session,
        actor_id=creator.id,
        manga_id=manga.public_id,
        operation_id=uuid4(),
        data={
            "destino_trabajo_ot_id": str(active_work.id),
            "destino_asignacion_id": str(active_assignment.id),
            "tramo_objetivo_id": str(manga.tramos_trabajo[0].id),
            "version": manga.version,
            "motivo": "La manga fue vinculada a la OT efectiva del turno.",
        },
    )
    apply_assignment_correction(
        db.session,
        actor_id=creator.id,
        manga_id=second_manga.public_id,
        operation_id=uuid4(),
        data={
            "destino_trabajo_ot_id": str(superseded_correction_work.id),
            "destino_asignacion_id": str(superseded_assignment.id),
            "tramo_objetivo_id": str(second_manga.tramos_trabajo[0].id),
            "version": second_manga.version,
            "motivo": "La manga fue vinculada a la OT efectiva del turno.",
        },
    )
    db.session.add(ScmTramoMangaTrabajo(
        manga=second_manga,
        trabajo=superseded_latest_work,
        asignacion_personal_trabajo=superseded_latest_assignment,
        asignacion_plan_id=superseded_latest_plan.id,
        secuencia=2,
        estado="ACTIVO",
        cantidad_inicio_un=0,
        cantidad_atribuida_un=0,
        created_by_id=creator.id,
    ))
    db.session.commit()
    return {
        "manga": manga,
        "second_manga": second_manga,
        "origin_ot": source.orden_trabajo,
        "effective_ot": active_ot,
        "effective_work": active_work,
        "active_ot": active_ot,
        "active_work": active_work,
        "superseded_correction_ot": superseded_ot,
        "superseded_correction_work": superseded_correction_work,
        "superseded_latest_ot": superseded_latest_ot,
        "superseded_latest_work": superseded_latest_work,
    }


def test_manga_explorer_is_fabrication_only_and_keeps_physical_rows(app, client, scm_config):
    with app.app_context():
        seeded = _seed_observability_graph()
        response = client.get(
            "/api/scm/v1/observabilidad/mangas-fabricacion",
            headers=_headers(seeded["full"]),
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert {item["ot"]["tipo"] for item in payload["items"]} == {"FABRICACION"}
        assert len({item["manga"]["public_id"] for item in payload["items"]}) == len(payload["items"])

        supervision = client.get(
            "/api/scm/v1/observabilidad/mangas",
            headers=_headers(seeded["full"]),
        )
        assert supervision.status_code == 200
        assert "ARMADO" in {item["ot"]["tipo"] for item in supervision.get_json()["items"]}


def test_manga_explorer_supports_uuid_search_and_export_modes(app, client, scm_config):
    with app.app_context():
        seeded = _seed_observability_graph()
        listed = client.get(
            "/api/scm/v1/observabilidad/mangas-fabricacion?q=00000000-0000-0000-0000-000000000000",
            headers=_headers(seeded["full"]),
        )
        assert listed.status_code == 200
        by_origin_ot = client.get(
            "/api/scm/v1/observabilidad/mangas-fabricacion?q=OT-OBS-FAB",
            headers=_headers(seeded["full"]),
        )
        assert by_origin_ot.status_code == 200
        assert len(by_origin_ot.get_json()["items"]) == 4
        by_of = client.get(
            "/api/scm/v1/observabilidad/mangas-fabricacion?q=OF-OBS-001",
            headers=_headers(seeded["full"]),
        )
        assert by_of.status_code == 200
        assert len(by_of.get_json()["items"]) == 4

        exported = client.get(
            "/api/scm/v1/observabilidad/mangas-fabricacion/export.xlsx?vista=AMBOS&cursor=cursor-de-la-pagina-actual",
            headers=_headers(seeded["full"]),
        )
        assert exported.status_code == 200
        workbook = load_workbook(BytesIO(exported.data), read_only=False, data_only=False)
        assert workbook.sheetnames == ["Resumen", "Detalle"]
        assert next(workbook["Resumen"].iter_rows(values_only=True))[0] == "Fecha operativa de origen"
        assert next(workbook["Detalle"].iter_rows(values_only=True))[0] == "Fecha operativa de origen"
        assert "Cohorte por OT origen" in workbook["Resumen"]["A1"].comment.text
        assert "no reparto diario de masa" in workbook["Detalle"]["A1"].comment.text


def test_manga_explorer_search_uses_effective_ot_after_auditable_corrections(app, client, scm_config):
    with app.app_context():
        first = _add_auditable_correction(app, "PESAJE-MANGA-EXPLORER-CORR-1")
        actor = first["manga"].maquinista_previsto

        effective = client.get(
            f"/api/scm/v1/observabilidad/mangas-fabricacion?q={first['effective_ot'].codigo_ot}",
            headers=_headers(actor),
        )
        assert effective.status_code == 200
        assert ScmCorreccionAsignacionManga.query.count() == 2
        assert ScmTramoMangaTrabajo.query.filter_by(
            secuencia=2, estado="ACTIVO",
        ).count() == 1
        assert ScmTramoMangaTrabajo.query.filter_by(
            manga_id=first["manga"].id, secuencia=2,
        ).count() == 0
        assert ScmTramoMangaTrabajo.query.filter_by(
            manga_id=first["second_manga"].id, secuencia=2,
        ).count() == 1
        effective_ids = {item["manga"]["public_id"] for item in effective.get_json()["items"]}
        assert str(first["manga"].public_id) in effective_ids

        origin = client.get(
            f"/api/scm/v1/observabilidad/mangas-fabricacion?q={first['origin_ot'].codigo_ot}",
            headers=_headers(actor),
        )
        assert origin.status_code == 200
        origin_ids = {item["manga"]["public_id"] for item in origin.get_json()["items"]}
        assert str(first["manga"].public_id) not in origin_ids

        effective_work = client.get(
            f"/api/scm/v1/observabilidad/mangas-fabricacion?q={first['effective_work'].codigo}",
            headers=_headers(actor),
        )
        assert effective_work.status_code == 200
        assert str(first["manga"].public_id) in {
            item["manga"]["public_id"] for item in effective_work.get_json()["items"]
        }
        origin_work = client.get(
            f"/api/scm/v1/observabilidad/mangas-fabricacion?q={first['manga'].trabajo.codigo}",
            headers=_headers(actor),
        )
        assert origin_work.status_code == 200
        assert str(first["manga"].public_id) not in {
            item["manga"]["public_id"] for item in origin_work.get_json()["items"]
        }

        listed = client.get(
            "/api/scm/v1/observabilidad/mangas-fabricacion?limit=100",
            headers=_headers(actor),
        )
        assert listed.status_code == 200
        by_id = {
            item["manga"]["public_id"]: item
            for item in listed.get_json()["items"]
        }
        active_item = by_id[str(first["manga"].public_id)]
        superseded_item = by_id[str(first["second_manga"].public_id)]

        active_work = first["active_work"]
        active_ot = first["active_ot"]
        latest_work = first["superseded_latest_work"]
        latest_ot = first["superseded_latest_ot"]
        superseded_ot = first["superseded_correction_ot"]
        for item, ot, work in (
            (active_item, active_ot, active_work),
            (superseded_item, latest_ot, latest_work),
        ):
            assert item["ot"]["public_id"] == str(ot.public_id)
            assert item["ot"]["codigo"] == ot.codigo_ot
            assert item["trabajo"]["codigo"] == work.codigo
            assert item["upstream"]["orden"]["codigo"] == work.orden_operacion.codigo
            assert item["recurso"]["codigo"] == ot.maquina.codigo
            assert item["manga"]["color"] == work.trabajo_color.color_nombre_snapshot
            assert item["trabajo"]["molde_codigo"] == work.trabajo_color.molde_codigo_snapshot

        search_payloads = {}

        def search_ids(term):
            response = client.get(
                "/api/scm/v1/observabilidad/mangas-fabricacion",
                query_string={"q": term, "limit": 100},
                headers=_headers(actor),
            )
            assert response.status_code == 200, (term, response.get_json())
            search_payloads[term] = response.get_json()
            return {
                item["manga"]["public_id"]
                for item in search_payloads[term]["items"]
            }

        active_id = str(first["manga"].public_id)
        superseded_id = str(first["second_manga"].public_id)
        destination_terms = (
            active_ot.codigo_ot,
            active_work.codigo,
            active_work.orden_operacion.codigo,
            active_ot.maquina_codigo_snapshot or active_ot.maquina.codigo,
            active_work.trabajo_color.color_nombre_snapshot,
            active_work.trabajo_color.molde_codigo_snapshot,
        )
        assert all(active_id in search_ids(term) for term in destination_terms)
        assert active_id not in search_ids(first["origin_ot"].codigo_ot)
        assert active_id not in search_ids(first["manga"].trabajo.codigo)

        latest_terms = (
            latest_ot.codigo_ot,
            latest_work.codigo,
            latest_work.orden_operacion.codigo,
            latest_ot.maquina_codigo_snapshot or latest_ot.maquina.codigo,
            latest_work.trabajo_color.color_nombre_snapshot,
            latest_work.trabajo_color.molde_codigo_snapshot,
        )
        for term in latest_terms:
            ids = search_ids(term)
            assert superseded_id in ids, (term, ids, search_payloads[term])
        assert superseded_id not in search_ids(first["origin_ot"].codigo_ot)
        assert superseded_id not in search_ids(first["second_manga"].trabajo.codigo)
        assert superseded_id not in search_ids(superseded_ot.codigo_ot)
        assert superseded_id not in search_ids(first["superseded_correction_work"].codigo)


@pytest.mark.parametrize("limit", [1, 25, 100])
def test_manga_explorer_uses_bounded_projection_queries(app, client, scm_config, limit):
    with app.app_context():
        _add_auditable_correction(app, "PESAJE-MANGA-EXPLORER-SCALE")
        seeded = _seed_observability_graph()
        _add_scaled_manga_graph(seeded)
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            response = client.get(
                f"/api/scm/v1/observabilidad/mangas-fabricacion?limit={limit}",
                headers=_headers(seeded["full"]),
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)

        assert response.status_code == 200
        payload = response.get_json()
        assert len(payload["items"]) == limit
        assert payload["page"]["limit"] == limit
        assert payload["page"]["has_more"] is True
        assert ScmManga.query.count() == 127
        assert len(statements) == {1: 84, 25: 90, 100: 90}[limit]
        # The explorer hydrates a page in batches; a full manga-detail query per
        # row would grow linearly with the physical set and is not acceptable.
        # The eager projection is bounded by relation batches, not by the
        # number of physical mangas.  SQLite emits a few additional batches
        # once the page crosses its parameter/chunk boundaries, but it must
        # remain well below a per-row detail query graph.
        assert len(statements) <= 100


def test_manga_explorer_export_uses_canonical_final_and_excludes_annulled_weight(app, client, scm_config):
    with app.app_context():
        _add_auditable_correction(app, "PESAJE-MANGA-EXPLORER-EXPORT")
        seeded = _seed_observability_graph()
        _add_scaled_manga_graph(seeded)
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", capture)
        try:
            exported = client.get(
                "/api/scm/v1/observabilidad/mangas-fabricacion/export.xlsx?vista=DETALLADO",
                headers=_headers(seeded["full"]),
            )
        finally:
            event.remove(db.engine, "before_cursor_execute", capture)
        assert exported.status_code == 200
        assert len(statements) == 90
        workbook = load_workbook(BytesIO(exported.data), read_only=True, data_only=False)
        rows = list(workbook["Detalle"].iter_rows(values_only=True))
        assert len(rows) - 1 == 126
        headers = rows[0]
        by_manga = {row[headers.index("Manga")]: row for row in rows[1:]}
        corrected = by_manga["M-OT-OBS-FAB-02"]
        annulled = by_manga["M-OT-OBS-FAB-04"]
        assert corrected[headers.index("KG final vigente")] == 11.5
        assert annulled[headers.index("KG final vigente")] is None
        assert annulled[headers.index("Estado pesaje")] == "SIN_PESAJE_VIGENTE"


def test_export_without_weighing_permission_is_explicitly_restricted(app, client, scm_config):
    with app.app_context():
        seeded = _seed_observability_graph()
        exported = client.get(
            "/api/scm/v1/observabilidad/mangas-fabricacion/export.xlsx?vista=AMBOS",
            headers=_headers(seeded["base"]),
        )
        assert exported.status_code == 200
        workbook = load_workbook(BytesIO(exported.data), read_only=True, data_only=False)
        summary_headers = next(workbook["Resumen"].iter_rows(values_only=True))
        detail_headers = next(workbook["Detalle"].iter_rows(values_only=True))
        assert "Mangas con pesaje restringido" in summary_headers
        assert "KG abierto" not in detail_headers
        assert "Estado pesaje" in detail_headers
        summary_rows = list(workbook["Resumen"].iter_rows(min_row=2, values_only=True))
        restricted_index = summary_headers.index("Mangas con pesaje restringido")
        open_index = summary_headers.index("Mangas abiertas")
        assert any(row[restricted_index] for row in summary_rows)
        assert all(row[open_index] in (None, 0) for row in summary_rows)


def test_manga_explorer_keeps_formula_like_cells_as_text_and_hex_is_canonical():
    assert [_safe_cell(value) for value in ("=SUM(A1)", "+unsafe", "-unsafe", "@unsafe")] == [
        "'=SUM(A1)", "'+unsafe", "'-unsafe", "'@unsafe",
    ]
    assert _safe_cell("plain") == "plain"
    assert _canonical_color_hex(" #aBc123 ") == "#ABC123"
    assert _canonical_color_hex("transparent") is None
    assert _canonical_color_hex("#12345") is None
