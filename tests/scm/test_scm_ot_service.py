from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.extensions import db
from app.models.lote import LoteColor, LoteSalidaPiezaColor
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.maquina import Maquina
from app.models.orden import OrdenProduccion, SnapshotComposicionMolde
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    Familia,
    FamiliaColor,
    Linea,
    PiezaColor,
)
from app.models.scm_articulos import ScmArticuloPiezaColor
from app.models.scm_articulos import ScmArticulo
from app.models.scm_assembly_execution import ScmConfirmacionMangaArmado
from app.models.scm_assembly_execution import ScmCorreccionMangaArmado
from app.models.scm_empaque import ScmArticuloPerfil, ScmPerfilEmpacable
from app.models.scm_estructuras import ScmEstructuraComponente, ScmEstructuraRevision
from app.models.scm_reproceso import ScmAlertaOperativa
from app.models.scm_auditoria import ScmEvento
from app.models.scm_ot import (
    ScmAsignacionPersonalTrabajoOt,
    ScmAsignacionPlanMangaOt,
    ScmEtiquetaManga,
    ScmManga,
    ScmTrabajoColor,
    ScmTrabajoImpresionManga,
    ScmTrabajoOt,
)
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
)
from app.models.scm_rutas import ScmCentroTrabajo, ScmOperacionRuta, ScmRutaRevision
from app.models.estacion_pesaje import EstacionPesaje
from app.models.scm_ot import ScmCorreccionPesajeManga, ScmPesajeManga
from app.models.scm_inventory import (
    ScmMovimientoInventario,
    ScmSaldoInventario,
)
from app.models.scm_warehouse import ScmExistenciaManga
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_fabrication_order_service import list_fabrication_orders
from app.services.scm_ot_service import (
    _fabrication_run_for_update_query,
    add_color_work,
    add_work_mangas,
    acknowledge_station_print_job,
    annul_manga,
    assign_color_work_worker,
    approve_extra_manga,
    create_fabrication_ot_header,
    create_fabrication_ot,
    create_ot,
    generate_prelabels,
    get_station_print_job,
    claim_station_print_job,
    list_station_print_jobs,
    list_ots,
    list_control_print_jobs,
    list_extra_manga_requests,
    recalculate_manga_plan,
    recalculate_fabrication_manga_plan,
    replace_prelabel,
    request_extra_manga,
    transition_color_work,
    transition_ot,
)


def test_fabrication_run_lock_targets_only_run_on_postgresql():
    statement = _fabrication_run_for_update_query(
        run_id=uuid4(),
        order_id=uuid4(),
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "LEFT OUTER JOIN color_produccion" in sql
    assert "FOR UPDATE OF scm_corrida_fabricacion" in sql
from app.services.scm_assembly_execution_service import (
    approve_assembly_quantity_correction,
    assign_assembly_output_mangas,
    close_assembly_manga,
    recalculate_assembly_manga_plan,
    request_assembly_quantity_correction,
)
from app.services.scm_internal_supply_service import (
    assign_supply_manga,
    create_assembly_ot,
    create_supply_request,
    dispatch_supply,
    dispatch_supply_return,
    mark_supply_ready,
    list_assembly_ots,
    receive_supply,
    receive_supply_return,
    request_supply_return,
)
from app.services.scm_packaging_service import (
    approve_packaging_rule,
    assign_article_profiles,
    create_container_type,
    create_packable_profile,
    create_packaging_rule,
)
from app.services.scm_service_support import ScmServiceError
from app.services.scm_weighing_service import (
    approve_weighing_correction,
    confirm_manga_weighing,
    get_manga_weighing,
    request_weighing_correction,
    resolve_manga_label,
)
from app.services.scm_warehouse_service import (
    decide_manga_quality,
    receive_manga,
    resolve_receiving_label,
)
from app.models.scm_ot import ScmAnulacionPesajeManga
from app.services.scm_ot_service import add_normal_mangas
from app.services.scm_weighing_service import annul_manga_weighing
from app.services.scm_warehouse_service import (
    request_receipt_reversal, resolve_receipt_reversal,
)


def _seed_normalized_order():
    ensure_initial_scm_configuration()
    creator = Trabajador.query.filter_by(codigo="TRB-01").one()
    creator.roles.extend([
        RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one(),
        RolOperativo.query.filter_by(codigo="SUPERVISOR").one(),
    ])
    approver = Trabajador(
        codigo="TRB-C-JP",
        nombres="Jefa",
        apellidos="Produccion",
        activo=True,
        roles=[
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        ],
    )
    db.session.add(approver)
    linea = Linea.query.first()
    familia = Familia.query.first()
    piece = Pieza(
        codigo="PZ-C-000001",
        nombre="Asa piloto",
        linea_id=linea.id,
        familia_id=familia.id,
        peso_nominal_gr=100,
        activo=True,
    )
    mold = Molde(
        codigo="ML-C-000001",
        nombre="Molde piloto C",
        peso_tiro_gr=110,
        tiempo_ciclo_std=10,
        activo=True,
    )
    mold_piece = MoldePieza(
        molde=mold,
        pieza=piece,
        cavidades=1,
        peso_unitario_gr=100,
        activo=True,
    )
    color_family = FamiliaColor(codigo=91, nombre="SOLIDO C")
    color_base = ColorBase(nombre="FUCSIA C")
    color = ColorProduccion(
        color_base_rel=color_base,
        familia_color_rel=color_family,
        hex_referencia="#E91E63",
    )
    piece_color = PiezaColor(
        sku="PC-C-000001",
        linea_id=linea.id,
        familia_id=familia.id,
        pieza_rel=piece,
        piezas="Asa piloto fucsia",
        color_produccion_rel=color,
        peso=100,
    )
    db.session.add_all([
        piece, mold, mold_piece, color_family, color_base, color,
        piece_color,
    ])
    db.session.flush()
    order = OrdenProduccion(
        numero_op="OP0084",
        maquina_id=1,
        molde_id=mold.codigo,
        activa=True,
        snapshot_tiempo_ciclo=10,
        snapshot_horas_turno=8,
        snapshot_peso_colada_gr=10,
    )
    db.session.add(order)
    db.session.flush()
    snapshot = SnapshotComposicionMolde(
        orden_id=order.numero_op,
        pieza_id=piece.id,
        pieza_codigo_snapshot=piece.codigo,
        pieza_nombre_snapshot=piece.nombre,
        cavidades=1,
        peso_unit_gr=100,
    )
    db.session.add(snapshot)
    db.session.flush()
    lot = LoteColor(
        numero_op=order.numero_op,
        color_produccion_id=color.id,
        meta_kg=25,
    )
    db.session.add(lot)
    db.session.flush()
    output = LoteSalidaPiezaColor(
        lote_color_id=lot.id,
        snapshot_pieza_id=snapshot.id,
        pieza_id=piece.id,
        pieza_color_sku=piece_color.sku,
        cavidades_snapshot=1,
        peso_unitario_snapshot_gr=100,
        cantidad_objetivo=250,
        kg_objetivo_neto=25,
    )
    db.session.add(output)
    db.session.commit()

    article_link = ScmArticuloPiezaColor.query.filter_by(
        pieza_color_sku=piece_color.sku
    ).one()
    container = create_container_type(
        db.session,
        actor_id=creator.id,
        data={
            "clase": "MANGA",
            "nombre": "Manga 100 unidades",
            "material": "PE",
            "dimensiones": {"ancho_mm": "500", "largo_mm": "800"},
            "tara_nominal_g": "100",
            "tolerancia_tara_g": "10",
            "peso_bruto_max_kg": "100",
        },
    )
    profile = create_packable_profile(
        db.session,
        actor_id=creator.id,
        data={
            "nombre": "Asa apilada",
            "descripcion_fisica": "Piloto C",
        },
    )
    article = article_link.articulo
    assign_article_profiles(
        db.session,
        actor_id=creator.id,
        article_id=article.id,
        data={
            "version": article.version,
            "perfiles": [{
                "perfil_empacable_id": profile["id"],
                "es_predeterminado": True,
                "activo": True,
            }],
        },
    )
    rule = create_packaging_rule(
        db.session,
        actor_id=creator.id,
        data={
            "perfil_empacable_id": profile["id"],
            "tipo_contenedor_id": container["id"],
            "medicion_fisica_probada": True,
            "cantidad_objetivo_un": 100,
            "cantidad_maxima_probada_un": 100,
            "peso_neto_operativo_max_kg": "99",
            "margen_seguridad_kg": "0",
            "tolerancia_peso_abs_g": "20",
            "tolerancia_peso_pct": "1",
        },
    )
    approve_packaging_rule(
        db.session,
        actor_id=approver.id,
        revision_id=rule["revision_id"],
        operation_id=uuid4(),
        data={"version": rule["version"]},
    )
    return creator, approver, order, output


def _seed_fabrication_order():
    creator, approver, legacy_order, legacy_output = (
        _seed_normalized_order()
    )
    article_link = ScmArticuloPiezaColor.query.filter_by(
        pieza_color_sku=legacy_output.pieza_color_sku
    ).one()
    legacy_lot = db.session.get(LoteColor, legacy_output.lote_color_id)
    operation = ScmOrdenOperacion(
        codigo="OF-000900",
        tipo="FABRICACION",
        origen_demanda="EXCEPCIONAL",
        estado="LIBERADA",
        created_by_id=creator.id,
        released_by_id=approver.id,
    )
    fabrication = ScmOrdenFabricacion(
        orden_operacion=operation,
        molde_id=legacy_order.molde_id,
        maquina_prevista_id=legacy_order.maquina_id,
        snapshot_tiempo_ciclo_seg=10,
        snapshot_horas_turno=8,
        snapshot_peso_colada_gr=10,
    )
    run = ScmCorridaFabricacion(
        orden_fabricacion=fabrication,
        codigo="OF-000900-C01",
        secuencia=1,
        color_produccion_id=legacy_lot.color_produccion_id,
        ciclos_objetivo=250,
        estado="LIBERADA",
    )
    canonical_output = ScmOrdenOperacionSalida(
        orden_operacion=operation,
        corrida_fabricacion=run,
        articulo_scm_id=article_link.articulo_id,
        cantidad_por_ciclo_snapshot=1,
        peso_unitario_snapshot_g=100,
        cantidad_objetivo=250,
        kg_estandar_objetivo=25,
    )
    db.session.add_all([operation, fabrication, run, canonical_output])
    db.session.commit()
    return creator, approver, operation, run, canonical_output


def test_plan_of_sin_perfil_explica_articulo_conteos_y_accion_segura(
    app,
    client,
):
    with app.app_context():
        creator, _approver, order, _run, output = (
            _seed_fabrication_order()
        )
        article = output.articulo
        link = ScmArticuloPerfil.query.filter_by(
            articulo_id=article.id,
        ).one()
        db.session.delete(link)
        db.session.commit()
        actor_id = creator.id
        order_id = order.id
        article_id = article.id
        article_code = article.codigo
        article_name = article.nombre

    response = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/plan-mangas/recalcular",
        json={},
        headers={
            "X-Actor-Id": str(actor_id),
            "Idempotency-Key": str(uuid4()),
        },
    )

    assert response.status_code == 422
    error = response.get_json()["error"]
    assert error["code"] == "PACKAGING_RULE_MISSING"
    assert article_code in error["message"]
    assert error["details"]["articulo"] == {
        "id": article_id,
        "codigo": article_code,
        "nombre": article_name,
        "clase": "PIEZA_COLOR",
    }
    assert error["details"]["perfiles"] == {
        "asignados": 0,
        "activos": 0,
        "predeterminados_activos": 0,
        "items": [],
    }
    assert error["details"]["reglas"][
        "manga_aprobadas_para_perfiles_activos"
    ] == 0
    assert error["details"]["accion"] == {
        "etiqueta": f"Revisar empaque de {article_code}",
        "ruta": (
            "/datos-maestros/ingenieria-scm"
            f"?tab=empaque&articulo={article_id}"
        ),
        "requiere_validacion_fisica": True,
    }


def test_plan_of_distingue_perfil_activo_sin_predeterminado(
    app,
    client,
):
    with app.app_context():
        creator, _approver, order, _run, output = (
            _seed_fabrication_order()
        )
        link = ScmArticuloPerfil.query.filter_by(
            articulo_id=output.articulo_scm_id,
        ).one()
        link.es_predeterminado = False
        db.session.commit()
        actor_id = creator.id
        order_id = order.id

    response = client.post(
        f"/api/scm/v1/ordenes-fabricacion/{order_id}/plan-mangas/recalcular",
        json={},
        headers={
            "X-Actor-Id": str(actor_id),
            "Idempotency-Key": str(uuid4()),
        },
    )

    assert response.status_code == 422
    details = response.get_json()["error"]["details"]
    assert details["perfiles"]["asignados"] == 1
    assert details["perfiles"]["activos"] == 1
    assert details["perfiles"]["predeterminados_activos"] == 0
    assert details["reglas"][
        "manga_aprobadas_para_perfiles_activos"
    ] == 1


def test_of_corrida_expone_identidad_humana_de_color_sin_codigo_inventado(
    app,
    client,
):
    with app.app_context():
        creator, _approver, order, run, _output = _seed_fabrication_order()
        color = db.session.get(ColorProduccion, run.color_produccion_id)
        actor_id = creator.id
        run_id = str(run.id)

        response = client.get(
            "/api/scm/v1/ordenes-fabricacion",
            headers={"X-Actor-Id": str(actor_id)},
        )
        assert response.status_code == 200
        payload = response.get_json()["items"]
        serialized_run = next(
            item
            for item in payload[0]["corridas"]
            if item["id"] == run_id
        )

        assert serialized_run["color"] == "FUCSIA C SOLIDO C"
        assert serialized_run["color_nombre"] == "FUCSIA C SOLIDO C"
        assert serialized_run["color_hex"] == "#E91E63"
        assert serialized_run["color_identidad"] == {
            "id": color.id,
            "nombre": "FUCSIA C SOLIDO C",
            "base": {
                "id": color.color_base_id,
                "nombre": "FUCSIA C",
            },
            "familia": {
                "id": color.familia_color_id,
                "nombre": "SOLIDO C",
            },
            "hex": "#E91E63",
        }
        assert "codigo" not in serialized_run["color_identidad"]

        run.color_produccion_id = None
        db.session.commit()
        response = client.get(
            "/api/scm/v1/ordenes-fabricacion",
            headers={"X-Actor-Id": str(actor_id)},
        )
        assert response.status_code == 200
        without_color = response.get_json()["items"][0]["corridas"][0]
        assert without_color["color"] is None
        assert without_color["color_nombre"] is None
        assert without_color["color_hex"] is None
        assert without_color["color_identidad"] is None


def test_tablero_ot_filtrado_expone_trabajo_asignacion_vigente_y_mangas(
    app,
    client,
):
    with app.app_context():
        creator, _approver, order, run, _output = _seed_fabrication_order()
        plan = recalculate_fabrication_manga_plan(
            db.session,
            actor_id=creator.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={},
        )["plan"]
        line = plan["lineas"][0]
        created = create_fabrication_ot(
            db.session,
            actor_id=creator.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(run.id),
                "fecha_operativa": "2026-08-09",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": line["id"],
                    "cantidad_un": 150,
                }],
            },
        )

        response = client.get(
            "/api/scm/v1/ots",
            query_string={
                "tipo_ot": "FABRICACION",
                "fecha_operativa": "2026-08-09",
                "turno": "DIA",
            },
            headers={"X-Actor-Id": str(creator.id)},
        )
        assert response.status_code == 200
        listed = response.get_json()["items"]

        assert [item["public_id"] for item in listed] == [
            created["ot"]["public_id"]
        ]
        assert listed[0]["maquina_id"] == (
            order.fabricacion.maquina_prevista_id
        )
        assert listed[0]["maquina_codigo"] == "MQ-01"
        work = listed[0]["trabajos_color"][0]
        assert work["asignacion_activa"] is None
        assert work["asignacion_vigente"]["estado"] == "PREVISTA"
        assert work["asignacion_vigente"]["trabajador_id"] == creator.id
        assert work["color"] == "FUCSIA C SOLIDO C"
        assert work["color_identidad"]["id"] == run.color_produccion_id
        assert work["cantidad_objetivo_un"] == "150"
        assert work["cantidad_confirmada_un"] == "0"
        assert [item["estado"] for item in work["mangas"]] == [
            "PLANIFICADA",
            "PLANIFICADA",
        ]
        assert sum(
            Decimal(item["cantidad_asignada_un"])
            for item in work["mangas"]
        ) == Decimal("150")


def test_of_corrida_ot_mangas_y_etiqueta_usen_identidad_canonica(app):
    with app.app_context():
        creator, _approver, order, run, output = (
            _seed_fabrication_order()
        )

        planned = recalculate_fabrication_manga_plan(
            db.session,
            actor_id=creator.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={},
        )
        line = planned["plan"]["lineas"][0]
        assert planned["plan"]["orden_id"] is None
        assert planned["plan"]["orden_operacion_id"] == str(order.id)
        assert line["orden_operacion_salida_id"] == str(output.id)

        created = create_fabrication_ot(
            db.session,
            actor_id=creator.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(run.id),
                "fecha_operativa": "2026-07-29",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": line["id"],
                    "cantidad_un": 150,
                }],
            },
        )

        ot = created["ot"]
        assert ot["orden_id"] is None
        assert ot["orden_operacion_id"] is None
        assert ot["corrida_fabricacion_id"] is None
        assert created["trabajo_color"]["orden_fabricacion_id"] == str(order.id)
        assert created["trabajo_color"]["corrida_fabricacion_id"] == str(run.id)
        assert [item["codigo"] for item in ot["mangas"]] == [
            "OF000900-OT001-M001",
            "OF000900-OT001-M002",
        ]
        labels = generate_prelabels(
            db.session,
            actor_id=creator.id,
            manga_id=UUID(ot["mangas"][0]["public_id"]),
            operation_id=uuid4(),
            data={},
        )
        payload = labels["labels"][0]["payload"]
        assert labels["template"]["version"] == "PREPESAJE_TSPL_3"
        assert payload["of_ot"] == "OF-000900 - OT-000001"
        assert "op_ot" not in payload
        assert payload["qr"]["v"] == 1
        assert payload["qr"]["manga_id"] == ot["mangas"][0]["public_id"]
        station_id = str(uuid4())
        db.session.add(EstacionPesaje(
            station_id=station_id,
            codigo="PESAJE-OF-01",
            nombre="Balanza OF",
            ubicacion="Piloto",
            token_hash="d" * 64,
        ))
        db.session.commit()
        acknowledge_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=UUID(labels["print_job_id"]),
            data={"results": [{
                "label_id": labels["labels"][0]["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]),
            operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]},
            action="iniciar",
        )
        resolved = resolve_manga_label(
            db.session,
            label_id=UUID(labels["labels"][0]["public_id"]),
        )
        assert resolved["manga"]["of_ot"] == "OF-000900 - OT-000001"
        assert "op_ot" not in resolved["manga"]
        weighed = confirm_manga_weighing(
            db.session,
            station_id=station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": labels["labels"][0]["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "10.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-07-29T14:31:09-05:00",
                "pesado_por_id": creator.id,
                "reading_stable": True,
            },
        )
        assert weighed["post_label"]["plantilla_version"] == (
            "POSTPESAJE_TSPL_3"
        )
        assert weighed["post_label"]["payload"]["of_ot"] == (
            "OF-000900 - OT-000001"
        )


def test_m2_multipieza_materializa_salidas_y_cupos_separados(app):
    with app.app_context():
        creator, approver, order, run, first_output = (
            _seed_fabrication_order()
        )
        first_article_profile = ScmArticuloPerfil.query.filter_by(
            articulo_id=first_output.articulo_scm_id,
            es_predeterminado=True,
            activo=True,
        ).one()
        first_piece_color = first_output.articulo.pieza_color.pieza_color
        second_piece = Pieza(
            codigo="PZ-C-000002",
            nombre="Tapa piloto",
            linea_id=first_piece_color.linea_id,
            familia_id=first_piece_color.familia_id,
            peso_nominal_gr=100,
            activo=True,
        )
        second_piece_color = PiezaColor(
            sku="PC-C-000002",
            linea_id=first_piece_color.linea_id,
            familia_id=first_piece_color.familia_id,
            pieza_rel=second_piece,
            piezas="Tapa piloto fucsia",
            color_produccion_id=first_piece_color.color_produccion_id,
            peso=100,
        )
        db.session.add_all([second_piece, second_piece_color])
        db.session.commit()
        second_article = ScmArticuloPiezaColor.query.filter_by(
            pieza_color_sku=second_piece_color.sku
        ).one().articulo
        profile_id = first_article_profile.perfil_empacable_id
        second_output = ScmOrdenOperacionSalida(
            orden_operacion=order,
            corrida_fabricacion=run,
            articulo_scm_id=second_article.id,
            cantidad_por_ciclo_snapshot=1,
            peso_unitario_snapshot_g=100,
            cantidad_objetivo=100,
            kg_estandar_objetivo=10,
        )
        db.session.add_all([
            ScmArticuloPerfil(
                articulo_id=second_article.id,
                perfil_empacable_id=profile_id,
                es_predeterminado=True,
                activo=True,
            ),
            second_output,
        ])
        db.session.commit()

        plan = recalculate_fabrication_manga_plan(
            db.session,
            actor_id=creator.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={},
        )["plan"]
        lines = {
            item["orden_operacion_salida_id"]: item
            for item in plan["lineas"]
        }
        assert set(lines) == {str(first_output.id), str(second_output.id)}
        header = create_fabrication_ot_header(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            data={
                "maquina_id": order.fabricacion.maquina_prevista_id,
                "fecha_operativa": "2026-08-10",
                "turno": "DIA",
                "maquinista_predeterminado_id": creator.id,
            },
        )["ot"]
        created = add_color_work(
            db.session,
            actor_id=creator.id,
            ot_id=UUID(header["public_id"]),
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(run.id),
                "maquinista_id": creator.id,
                "asignaciones": [
                    {
                        "plan_linea_id": lines[str(first_output.id)]["id"],
                        "cantidad_un": 100,
                    },
                    {
                        "plan_linea_id": lines[str(second_output.id)]["id"],
                        "cantidad_un": 100,
                    },
                ],
            },
        )
        work = created["trabajo_color"]
        assert work["cantidad_objetivo_un"] == "200"
        assert len(created["mangas"]) == 2
        manga_models = [
            ScmManga.query.filter_by(public_id=UUID(item["public_id"])).one()
            for item in created["mangas"]
        ]
        assert {item.pieza_color_sku_snapshot for item in manga_models} == {
            first_output.articulo.pieza_color.pieza_color_sku,
            second_piece_color.sku,
        }
        assert len({item.plan_linea_id for item in manga_models}) == 2
        assert len({item.asignacion_id for item in manga_models}) == 2
        assert {
            Decimal(item.asignacion.cantidad_asignada_un)
            for item in manga_models
        } == {Decimal("100")}

        annul_manga(
            db.session,
            actor_id=approver.id,
            manga_id=manga_models[0].public_id,
            operation_id=uuid4(),
            data={"motivo": "Verificacion de cupo multipieza"},
        )
        db.session.expire_all()
        assert Decimal(db.session.get(
            ScmTrabajoOt, UUID(work["id"])
        ).cantidad_objetivo_un) == Decimal("100")
        remaining = ScmManga.query.filter(
            ScmManga.trabajo_ot_id == UUID(work["id"]),
            ScmManga.estado != "ANULADA",
        ).one()
        assert Decimal(remaining.asignacion.cantidad_asignada_un) == Decimal("100")


def test_ot_maquina_contiene_varios_trabajos_color_y_ejecucion_exclusiva(app):
    with app.app_context():
        creator, _approver, order, first_run, first_output = (
            _seed_fabrication_order()
        )
        second_run = ScmCorridaFabricacion(
            orden_fabricacion=order.fabricacion,
            codigo="OF-000900-C02",
            secuencia=2,
            color_produccion_id=first_run.color_produccion_id,
            ciclos_objetivo=250,
            estado="LIBERADA",
        )
        second_output = ScmOrdenOperacionSalida(
            orden_operacion=order,
            corrida_fabricacion=second_run,
            articulo_scm_id=first_output.articulo_scm_id,
            cantidad_por_ciclo_snapshot=1,
            peso_unitario_snapshot_g=100,
            cantidad_objetivo=250,
            kg_estandar_objetivo=25,
        )
        relief = Trabajador(
            codigo="TRB-REL-01",
            nombres="Relevo",
            apellidos="Turno",
            activo=True,
            roles=[
                RolOperativo.query.filter_by(codigo="SUPERVISOR").one(),
                RolOperativo.query.filter_by(codigo="MAQUINISTA").one(),
            ],
        )
        db.session.add_all([second_run, second_output, relief])
        db.session.commit()

        plan = recalculate_fabrication_manga_plan(
            db.session,
            actor_id=creator.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={},
        )["plan"]
        lines = {
            item["corrida_fabricacion_id"]: item
            for item in plan["lineas"]
        }
        header = create_fabrication_ot_header(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            data={
                "maquina_id": order.fabricacion.maquina_prevista_id,
                "fecha_operativa": "2026-08-10",
                "turno": "DIA",
                "maquinista_predeterminado_id": creator.id,
            },
        )["ot"]
        assert header["orden_operacion_id"] is None
        assert header["corrida_fabricacion_id"] is None
        assert header["trabajos_color"] == []

        first = add_color_work(
            db.session,
            actor_id=creator.id,
            ot_id=UUID(header["public_id"]),
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(first_run.id),
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": lines[str(first_run.id)]["id"],
                    "cantidad_un": 100,
                }],
            },
        )["trabajo_color"]
        second = add_color_work(
            db.session,
            actor_id=creator.id,
            ot_id=UUID(header["public_id"]),
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(second_run.id),
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": lines[str(second_run.id)]["id"],
                    "cantidad_un": 100,
                }],
            },
        )["trabajo_color"]
        assert first["codigo"].endswith("-TC01")
        assert second["codigo"].endswith("-TC02")
        assert ScmTrabajoOt.query.count() == 2
        assert ScmTrabajoColor.query.count() == 2

        first = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(first["id"]),
            operation_id=uuid4(),
            data={"version": first["version"]},
            action="iniciar",
        )["trabajo_color"]
        with pytest.raises(ScmServiceError) as conflict:
            transition_color_work(
                db.session,
                actor_id=creator.id,
                work_id=UUID(second["id"]),
                operation_id=uuid4(),
                data={"version": second["version"]},
                action="iniciar",
            )
        assert conflict.value.code == "OT_WORK_ALREADY_RUNNING"

        first = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(first["id"]),
            operation_id=uuid4(),
            data={"version": first["version"]},
            action="pausar",
        )["trabajo_color"]
        second = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(second["id"]),
            operation_id=uuid4(),
            data={"version": second["version"]},
            action="iniciar",
        )["trabajo_color"]
        reassigned = assign_color_work_worker(
            db.session,
            actor_id=creator.id,
            work_id=UUID(second["id"]),
            operation_id=uuid4(),
            data={
                "trabajador_id": relief.id,
                "motivo": "Relevo programado",
                "version": second["version"],
            },
        )
        assert reassigned["trabajo_color"]["id"] == second["id"]
        assert reassigned["asignacion"]["trabajador_id"] == relief.id
        assert ScmAsignacionPersonalTrabajoOt.query.filter_by(
            trabajo_ot_id=UUID(second["id"]), estado="ACTIVA"
        ).count() == 1
        assert first["estado"] == "PAUSADO"


def test_listado_global_ot_armado_expone_contexto_concurrente_y_trabajos(
    app,
    client,
):
    with app.app_context():
        creator, approver, order, run, output = _seed_fabrication_order()
        plan = recalculate_fabrication_manga_plan(
            db.session,
            actor_id=creator.id,
            order_id=order.id,
            operation_id=uuid4(),
            data={},
        )["plan"]
        line = plan["lineas"][0]
        header = create_fabrication_ot_header(
            db.session,
            actor_id=creator.id,
            operation_id=uuid4(),
            data={
                "maquina_id": order.fabricacion.maquina_prevista_id,
                "fecha_operativa": "2026-08-10",
                "turno": "DIA",
                "maquinista_predeterminado_id": creator.id,
            },
        )["ot"]
        work = add_color_work(
            db.session,
            actor_id=creator.id,
            ot_id=UUID(header["public_id"]),
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(run.id),
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": line["id"],
                    "cantidad_un": 100,
                }],
            },
        )["trabajo_color"]
        projected_order = list_fabrication_orders(
            db.session,
            actor_id=creator.id,
        )["items"][0]
        assert projected_order["rango_fechas_ot"] == {
            "desde": "2026-08-10",
            "hasta": "2026-08-10",
            "cantidad": 1,
        }
        assert projected_order["programacion_estado"] == "PROGRAMADA"

        finished_article = ScmArticulo(
            codigo="PT-CONTEXTO-ARMADO",
            nombre="Salida prearmada concurrente",
            clase="PRODUCTO_TERMINADO",
        )
        center = ScmCentroTrabajo(
            codigo="CTR-CONTEXTO-ARMADO",
            nombre="Mesa concurrente",
            tipo="PREARMADO",
        )
        structure = ScmEstructuraRevision(
            articulo_resultado=finished_article,
            numero_revision=1,
            estado="APROBADA",
            content_hash="8" * 64,
            creada_por_id=creator.id,
            aprobada_por_id=approver.id,
            componentes=[ScmEstructuraComponente(
                secuencia=1,
                articulo_componente_id=output.articulo_scm_id,
                cantidad=1,
                unidad="UN",
            )],
        )
        route = ScmRutaRevision(
            articulo_objetivo=finished_article,
            numero_revision=1,
            estado="APROBADA",
            content_hash="9" * 64,
            creada_por_id=creator.id,
            aprobada_por_id=approver.id,
        )
        db.session.add_all([finished_article, center, structure, route])
        db.session.flush()
        route_operation = ScmOperacionRuta(
            ruta=route,
            clave="PREARMAR_CONTEXTO",
            secuencia_visible=1,
            nombre="Prearmar junto a maquina",
            tipo="PREARMADO",
            executor_kind="ORDEN_OPERACION",
            centro_trabajo=center,
            articulo_salida=finished_article,
            estructura_revision=structure,
            permite_concurrente=True,
        )
        assembly_order = ScmOrdenOperacion(
            codigo="OA-CONTEXTO-001",
            tipo="ENSAMBLE",
            origen_demanda="ORDEN_PRODUCCION",
            estado="LIBERADA",
            operacion_ruta_revision=route_operation,
            operacion_ruta_hash=route.content_hash,
            created_by_id=creator.id,
            released_by_id=approver.id,
            salidas=[ScmOrdenOperacionSalida(
                articulo=finished_article,
                cantidad_objetivo=100,
            )],
        )
        db.session.add_all([route_operation, assembly_order])
        db.session.commit()

        assembly_ot = create_assembly_ot(
            db.session,
            actor_id=creator.id,
            order_id=assembly_order.id,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-08-10",
                "turno": "DIA",
                "centro_trabajo_id": center.id,
                "responsable_id": creator.id,
                "cantidad_objetivo": 100,
                "modo_ejecucion": "CONCURRENTE",
                "trabajo_color_contexto_id": work["id"],
            },
        )["ot"]

        fabrication_items = list_ots(
            db.session,
            actor_id=creator.id,
            tipo_ot="FABRICACION",
            operational_date="2026-08-10",
            shift="DIA",
        )["items"]
        assert fabrication_items[0]["trabajos_color"][0]["id"] == work["id"]
        assert fabrication_items[0]["trabajos_color"][0][
            "orden_fabricacion_codigo"
        ] == order.codigo
        assert fabrication_items[0]["trabajos_color"][0][
            "articulos_salida"
        ] == [{
            "id": output.articulo.id,
            "codigo": output.articulo.codigo,
            "nombre": output.articulo.nombre,
            "clase": output.articulo.clase,
            "unidad": output.articulo.unidad_base,
        }]

        assembly_items = list_ots(
            db.session,
            actor_id=creator.id,
            tipo_ot="ENSAMBLE",
            operational_date="2026-08-10",
            shift="DIA",
        )["items"]
        assert len(assembly_items) == 1
        item = assembly_items[0]
        assert item["public_id"] == assembly_ot["public_id"]
        assert item["fecha_operativa"] == "2026-08-10"
        assert item["turno"] == "DIA"
        assert item["modo_ejecucion_armado"] == "CONCURRENTE"
        assert item["centro_trabajo"]["id"] == center.id
        assert item["responsable_id"] == creator.id
        assert item["ot_fabricacion_contexto"]["public_id"] == header["public_id"]
        assert item["trabajo_color_contexto_id"] == work["id"]
        assert item["trabajo_color_contexto"]["id"] == work["id"]
        assert item["trabajo_color_contexto"]["color"] == work["color"]
        assert item["trabajo_color_contexto"][
            "orden_fabricacion_codigo"
        ] == order.codigo
        assert item["orden_armado"] == {
            "id": str(assembly_order.id),
            "codigo": assembly_order.codigo,
            "salida": {
                "articulo": {
                    "id": finished_article.id,
                    "codigo": finished_article.codigo,
                    "nombre": finished_article.nombre,
                    "clase": finished_article.clase,
                    "unidad": finished_article.unidad_base,
                },
                "cantidad_objetivo": "100",
            },
        }
        assert item["abastecimiento"] is None

        response = client.get(
            "/api/scm/v1/ots",
            query_string={
                "tipo_ot": "ENSAMBLE",
                "fecha_operativa": "2026-08-10",
                "turno": "DIA",
            },
            headers={"X-Actor-Id": str(creator.id)},
        )
        assert response.status_code == 200
        assert response.get_json()["items"][0]["trabajo_color_contexto"][
            "id"
        ] == work["id"]


def test_anular_manga_normal_prepesaje_devuelve_cupo_al_trabajo(app):
    with app.app_context():
        creator, approver, order, run, _output = _seed_fabrication_order()
        plan = recalculate_fabrication_manga_plan(
            db.session, actor_id=creator.id, order_id=order.id,
            operation_id=uuid4(), data={},
        )["plan"]
        line = plan["lineas"][0]
        header = create_fabrication_ot_header(
            db.session, actor_id=creator.id, operation_id=uuid4(),
            data={
                "maquina_id": order.fabricacion.maquina_prevista_id,
                "fecha_operativa": "2026-08-10",
                "turno": "DIA",
                "maquinista_predeterminado_id": creator.id,
            },
        )["ot"]
        work = add_color_work(
            db.session, actor_id=creator.id,
            ot_id=UUID(header["public_id"]), operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(run.id),
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": line["id"], "cantidad_un": 100,
                }],
            },
        )["trabajo_color"]
        manga = work["mangas"][0]
        cancelled = annul_manga(
            db.session, actor_id=approver.id,
            manga_id=UUID(manga["public_id"]), operation_id=uuid4(),
            data={"motivo": "Sticker dañado antes del pesaje"},
        )
        assert cancelled["plan"]["cantidad_devuelta_un"] == "100"
        replacement = add_work_mangas(
            db.session, actor_id=creator.id,
            work_id=UUID(work["id"]), operation_id=uuid4(),
            data={"plan_linea_id": line["id"], "cantidad_un": 100},
        )["mangas"]
        assert len(replacement) == 1
        assert replacement[0]["trabajo_color_id"] == work["id"]


def test_plan_no_materializa_mangas_hasta_asignar_ot(app):
    with app.app_context():
        creator, approver, order, _output = _seed_normalized_order()

        planned = recalculate_manga_plan(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=uuid4(),
            data={},
        )

        line = planned["plan"]["lineas"][0]
        assert line["cantidad_objetivo_un"] == "250"
        assert line["capacidad_efectiva_un"] == 100
        assert line["mangas_propuestas"] == 3
        assert ScmManga.query.count() == 0

        command_id = uuid4()
        command = {
            "fecha_operativa": "2026-07-28",
            "maquina_id": 1,
            "turno": "DIA",
            "maquinista_id": creator.id,
            "asignaciones": [{
                "plan_linea_id": line["id"],
                "cantidad_un": 150,
            }],
        }
        created = create_ot(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=command_id,
            data=command,
        )

        assert created["ot"]["codigo_ot"] == "OT-000001"
        assert [item["cantidad_asignada_un"] for item in
                created["ot"]["mangas"]] == ["100", "50"]
        assert [item["codigo"] for item in created["ot"]["mangas"]] == [
            "OP0084-OT001-M001",
            "OP0084-OT001-M002",
        ]
        replay = create_ot(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=command_id,
            data=command,
        )
        assert replay == created
        assert ScmManga.query.count() == 2
        with pytest.raises(ScmServiceError) as conflict:
            create_ot(
                db.session,
                actor_id=creator.id,
                op_number=order.numero_op,
                operation_id=command_id,
                data={**command, "turno": "NOCHE"},
            )
        assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_op_legacy_se_rechaza_sin_mutarla(app, scm_config):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        actor.roles.append(
            RolOperativo.query.filter_by(codigo="SUPERVISOR").one()
        )
        legacy = OrdenProduccion(
            numero_op="OP-LEGACY-C",
            maquina_id=1,
            activa=True,
        )
        db.session.add(legacy)
        db.session.commit()

        with pytest.raises(ScmServiceError) as error:
            recalculate_manga_plan(
                db.session,
                actor_id=actor.id,
                op_number=legacy.numero_op,
                operation_id=uuid4(),
                data={},
            )

        assert error.value.code == "OP_NOT_EXECUTABLE"
        assert db.session.get(
            OrdenProduccion, "OP-LEGACY-C"
        ).activa is True
        assert ScmManga.query.count() == 0


def test_trabajo_dos_up_conserva_dos_identidades(app):
    with app.app_context():
        creator, approver, order, _output = _seed_normalized_order()
        plan = recalculate_manga_plan(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=uuid4(),
            data={},
        )
        ot = create_ot(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-07-28",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": plan["plan"]["lineas"][0]["id"],
                    "cantidad_un": 150,
                }],
            },
        )["ot"]
        ids = [item["public_id"] for item in ot["mangas"]]

        labels = generate_prelabels(
            db.session,
            actor_id=creator.id,
            manga_id=UUID(ids[0]),
            operation_id=uuid4(),
            data={"manga_ids": ids},
        )

        assert len(labels["labels"]) == 2
        assert labels["labels"][0]["public_id"] != (
            labels["labels"][1]["public_id"]
        )
        assert labels["labels"][0]["payload"]["qr"]["manga_id"] == ids[0]
        assert labels["labels"][1]["payload"]["qr"]["manga_id"] == ids[1]

        station_id = str(uuid4())
        db.session.add(EstacionPesaje(
            station_id=station_id,
            codigo="PESAJE-C-01",
            nombre="Balanza C",
            ubicacion="Piloto",
            token_hash="b" * 64,
        ))
        db.session.commit()
        job_id = UUID(labels["print_job_id"])
        failed = acknowledge_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=job_id,
            data={"results": [
                {
                    "label_id": label["public_id"],
                    "estado": "FALLIDA_SIN_EMISION",
                }
                for label in labels["labels"]
            ]},
        )
        assert failed["estado"] == "FALLIDO"

        retried = acknowledge_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=job_id,
            data={"results": [
                {
                    "label_id": label["public_id"],
                    "estado": "IMPRESA",
                    "printer_name": "TSC",
                }
                for label in labels["labels"]
            ]},
        )
        assert retried["estado"] == "PROCESADO"
        assert {item["estado"] for item in retried["labels"]} == {"IMPRESA"}

        replacement = replace_prelabel(
            db.session,
            actor_id=approver.id,
            label_id=UUID(labels["labels"][0]["public_id"]),
            operation_id=uuid4(),
            data={"motivo": "Etiqueta dañada durante manipulación"},
        )
        assert replacement["invalidated_label_id"] == (
            labels["labels"][0]["public_id"]
        )
        assert replacement["label"]["version"] == 2
        annulled = annul_manga(
            db.session,
            actor_id=approver.id,
            manga_id=UUID(ids[0]),
            operation_id=uuid4(),
            data={"motivo": "Manga descartada antes del pesaje"},
        )
        assert annulled["manga"]["estado"] == "ANULADA"
        assert annulled["manga"]["etiqueta_vigente"] is None


def test_bandeja_preview_no_reclama_y_claim_es_explicito_e_idempotente(app):
    with app.app_context():
        creator, _approver, order, _output = _seed_normalized_order()
        plan = recalculate_manga_plan(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=uuid4(),
            data={},
        )
        ot = create_ot(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-07-28",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": plan["plan"]["lineas"][0]["id"],
                    "cantidad_un": 100,
                }],
            },
        )["ot"]
        generated = generate_prelabels(
            db.session,
            actor_id=creator.id,
            manga_id=UUID(ot["mangas"][0]["public_id"]),
            operation_id=uuid4(),
            data={},
        )
        station_a = str(uuid4())
        station_b = str(uuid4())
        db.session.add_all([
            EstacionPesaje(
                station_id=station_a,
                codigo="PESAJE-PREVIEW-A",
                nombre="Balanza preview A",
                ubicacion="Piloto",
                token_hash="a" * 64,
            ),
            EstacionPesaje(
                station_id=station_b,
                codigo="PESAJE-PREVIEW-B",
                nombre="Balanza preview B",
                ubicacion="Piloto",
                token_hash="b" * 64,
            ),
        ])
        db.session.commit()
        job_id = UUID(generated["print_job_id"])

        listed = list_station_print_jobs(
            db.session,
            station_id=station_a,
            status="PENDING",
            limit=20,
        )
        assert listed["count"] == 1
        assert listed["print_jobs"][0]["status"] == "PENDING"
        assert listed["print_jobs"][0]["estado"] == "GENERADO"
        assert listed["print_jobs"][0]["labels"][0]["estado"] == "GENERADA"
        control = list_control_print_jobs(
            db.session,
            actor_id=creator.id,
            filters={"status": "PENDING", "tipo": "PREPESAJE"},
        )
        assert control["count"] == 1
        assert control["items"][0]["print_job_id"] == str(job_id)
        assert control["items"][0]["labels"][0]["manga_codigo"]
        assert control["items"][0]["station_id"] is None
        assert control["as_of"]
        searched = list_control_print_jobs(
            db.session, actor_id=creator.id,
            filters={"q": control["items"][0]["labels"][0]["manga_codigo"]},
        )
        assert searched["count"] == 1

        first_preview = get_station_print_job(
            db.session,
            station_id=station_a,
            print_job_id=job_id,
        )
        second_preview = get_station_print_job(
            db.session,
            station_id=station_b,
            print_job_id=job_id,
        )
        assert first_preview == second_preview
        assert first_preview["station_id"] is None
        assert first_preview["status"] == "PENDING"
        assert db.session.get(
            ScmTrabajoImpresionManga, job_id
        ).station_id is None

        claimed = claim_station_print_job(
            db.session,
            station_id=station_a,
            print_job_id=job_id,
        )
        replay = claim_station_print_job(
            db.session,
            station_id=station_a,
            print_job_id=job_id,
        )
        assert claimed == replay
        assert claimed["station_id"] == station_a

        with pytest.raises(ScmServiceError) as conflict:
            claim_station_print_job(
                db.session,
                station_id=station_b,
                print_job_id=job_id,
            )
        assert conflict.value.code == "PRINT_JOB_ALREADY_CLAIMED"

        assert list_station_print_jobs(
            db.session,
            station_id=station_b,
            status="PENDING",
            limit=20,
        )["count"] == 0


def test_extra_requiere_otro_actor_y_queda_listable(app):
    with app.app_context():
        creator, approver, order, _output = _seed_normalized_order()
        plan = recalculate_manga_plan(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=uuid4(),
            data={},
        )
        ot = create_ot(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-07-28",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": plan["plan"]["lineas"][0]["id"],
                    "cantidad_un": 250,
                }],
            },
        )["ot"]
        request_item = request_extra_manga(
            db.session,
            actor_id=creator.id,
            public_id=UUID(ot["public_id"]),
            operation_id=uuid4(),
            data={
                "plan_linea_id": plan["plan"]["lineas"][0]["id"],
                "cantidad_un": 40,
                "motivo": "Exceso de produccion",
            },
        )["solicitud"]

        pending = list_extra_manga_requests(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            state="PENDIENTE",
        )
        assert [item["id"] for item in pending["items"]] == [
            request_item["id"]
        ]
        with pytest.raises(ScmServiceError) as four_eyes:
            approve_extra_manga(
                db.session,
                actor_id=creator.id,
                request_id=UUID(request_item["id"]),
                operation_id=uuid4(),
                data={},
            )
        assert four_eyes.value.code == "CAPABILITY_REQUIRED"

        approved = approve_extra_manga(
            db.session,
            actor_id=approver.id,
            request_id=UUID(request_item["id"]),
            operation_id=uuid4(),
            data={},
        )
        assert approved["solicitud"]["estado"] == "APROBADA"
        assert approved["mangas"][0]["tipo"] == "EXTRA"
        assert approved["mangas"][0]["motivo_extra"] == (
            "Exceso de produccion"
        )


def test_pesaje_scm_es_idempotente_y_no_crea_kardex(app):
    with app.app_context():
        creator, approver, order, _output = _seed_normalized_order()
        plan = recalculate_manga_plan(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=uuid4(),
            data={},
        )
        ot = create_ot(
            db.session,
            actor_id=creator.id,
            op_number=order.numero_op,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-07-28",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": plan["plan"]["lineas"][0]["id"],
                    "cantidad_un": 100,
                }],
            },
        )["ot"]
        manga_id = UUID(ot["mangas"][0]["public_id"])
        prelabel_job = generate_prelabels(
            db.session,
            actor_id=creator.id,
            manga_id=manga_id,
            operation_id=uuid4(),
            data={},
        )
        station_id = str(uuid4())
        db.session.add(EstacionPesaje(
            station_id=station_id,
            codigo="PESAJE-D-01",
            nombre="Balanza D",
            ubicacion="Piloto",
            token_hash="c" * 64,
        ))
        db.session.commit()
        prelabel = prelabel_job["labels"][0]
        acknowledge_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=UUID(prelabel_job["print_job_id"]),
            data={"results": [{
                "label_id": prelabel["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        resolved = resolve_manga_label(
            db.session, label_id=UUID(prelabel["public_id"])
        )
        assert resolved["can_weigh"] is True
        assert resolved["manga"]["tara_nominal_kg"] == "0.100"

        operation_id = uuid4()
        command = {
            "label_id": prelabel["public_id"],
            "capture_id": str(uuid4()),
            "peso_bruto_kg": "10.100",
            "tara_kg": "0.100",
            "tara_fuente": "TIPO_MANGA",
            "pesada_at": "2026-07-30T14:31:09-05:00",
            "pesado_por_id": creator.id,
            "reading_stable": True,
        }
        result = confirm_manga_weighing(
            db.session,
            station_id=station_id,
            operation_id=operation_id,
            actor_id=creator.id,
            data=command,
        )

        assert result["weighing"]["peso_fisico_neto_kg"] == "10.000"
        assert result["weighing"]["kg_produccion_ot"] == "10.000"
        assert result["weighing"]["estado_manga"] == "PESADA"
        assert result["weighing"]["dias_desfase_operativo"] == 2
        assert result["weighing"]["alerta_fecha"] is True
        assert "PESAJE_FECHA_OPERATIVA_DIFERENTE" in {
            item.tipo for item in ScmAlertaOperativa.query.all()
        }
        assert result["post_label"]["tipo"] == "POSTPESAJE"
        assert ScmPesajeManga.query.count() == 1

        replay = confirm_manga_weighing(
            db.session,
            station_id=station_id,
            operation_id=operation_id,
            actor_id=creator.id,
            data=command,
        )
        assert replay == result
        assert ScmPesajeManga.query.count() == 1

        post_ack = acknowledge_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=UUID(result["print_job_id"]),
            data={"results": [{
                "label_id": result["post_label"]["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        assert post_ack["labels"][0]["estado"] == "IMPRESA"
        assert ScmPesajeManga.query.one().manga.estado == (
            "PENDIENTE_RECEPCION_ALMACEN"
        )

        original = ScmPesajeManga.query.one()
        correction_request = request_weighing_correction(
            db.session,
            actor_id=creator.id,
            weighing_id=original.public_id,
            operation_id=uuid4(),
            data={
                "proposed": {
                    "peso_bruto_kg": "9.900",
                    "cantidad_confirmada": "98",
                },
                "motivo": "Lectura digitada incorrectamente en validacion",
            },
        )
        correction_id = UUID(correction_request["correction"]["id"])
        assert correction_request["correction"]["estado"] == "PENDIENTE"

        creator.roles.append(
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        )
        db.session.commit()
        with pytest.raises(ScmServiceError) as four_eyes:
            approve_weighing_correction(
                db.session,
                actor_id=creator.id,
                correction_id=correction_id,
                operation_id=uuid4(),
                data={},
            )
        assert four_eyes.value.code == "FOUR_EYES_REQUIRED"

        corrected = approve_weighing_correction(
            db.session,
            actor_id=approver.id,
            correction_id=correction_id,
            operation_id=uuid4(),
            data={"motivo_aprobacion": "Validado contra evidencia fisica"},
        )
        assert corrected["correction"]["estado"] == "APLICADA"
        assert corrected["correction"]["result_projection"][
            "peso_fisico_neto_kg"
        ] == "9.800"
        assert corrected["correction"]["result_projection"][
            "kg_produccion_ot"
        ] == "9.800"
        assert corrected["post_label"]["payload"]["kg_fisico"] == "9.800"
        assert corrected["post_label"]["payload"][
            "kg_produccion_ot"
        ] == "9.800"
        assert corrected["post_label"]["version"] == 2
        assert "CORRECCION_PESAJE_TARDIA" in {
            item.tipo for item in ScmAlertaOperativa.query.all()
        }

        # La correccion es compensatoria: conserva la captura original.
        assert ScmPesajeManga.query.count() == 1
        assert format(original.peso_bruto_kg, "f") == "10.100"
        assert format(original.peso_fisico_neto_kg, "f") == "10.000"
        assert ScmCorreccionPesajeManga.query.count() == 1
        projection = get_manga_weighing(
            db.session,
            actor_id=creator.id,
            manga_id=manga_id,
        )
        assert projection["original"]["peso_fisico_neto_kg"] == "10.000"
        assert projection["vigente"]["peso_fisico_neto_kg"] == "9.800"
        assert projection["vigente"]["cantidad_confirmada"] == "98.000"
        assert projection["estado_inventario"] == "NO_INGRESADA"
        valid_postlabels = [
            label
            for label in projection["etiquetas_postpesaje"]
            if label["estado"] != "INVALIDADA"
        ]
        assert [label["version"] for label in valid_postlabels] == [2]

        replacement = replace_prelabel(
            db.session,
            actor_id=approver.id,
            label_id=UUID(corrected["post_label"]["public_id"]),
            operation_id=uuid4(),
            data={"motivo": "Etiqueta final dañada"},
        )
        assert replacement["label"]["tipo"] == "POSTPESAJE"
        assert replacement["label"]["version"] == 3
        assert replacement["label"]["payload"]["kg_fisico"] == "9.800"
        assert ScmPesajeManga.query.one().manga.estado == "PESADA"
        acknowledge_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=UUID(replacement["print_job_id"]),
            data={"results": [{
                "label_id": replacement["label"]["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        assert ScmPesajeManga.query.one().manga.estado == (
            "PENDIENTE_RECEPCION_ALMACEN"
        )

        warehouse_role = RolOperativo.query.filter_by(
            codigo="ALMACEN_RECEPCION"
        ).one()
        quality_role = RolOperativo.query.filter_by(codigo="CALIDAD").one()
        warehouse_actor = Trabajador(
            codigo="TRB-ALM-01",
            nombres="UAT",
            apellidos="Almacen",
            activo=True,
            roles=[warehouse_role],
        )
        quality_actor = Trabajador(
            codigo="TRB-CAL-01",
            nombres="UAT",
            apellidos="Calidad",
            activo=True,
            roles=[quality_role],
        )
        db.session.add_all([warehouse_actor, quality_actor])
        db.session.commit()

        candidate = resolve_receiving_label(
            db.session,
            actor_id=warehouse_actor.id,
            label_id=UUID(replacement["label"]["public_id"]),
        )
        assert candidate["cantidad_confirmada"] == "98.000"
        assert candidate["peso_neto_kg"] == "9.800"

        receipt_operation = uuid4()
        receipt_command = {
            "label_id": replacement["label"]["public_id"],
            "ubicacion_codigo": "RECEPCION_PIEZAS_WIP",
            "presencia_confirmada": True,
            "bolsa_cerrada": True,
            "coincidencia_etiquetas": True,
        }
        received = receive_manga(
            db.session,
            actor_id=warehouse_actor.id,
            operation_id=receipt_operation,
            data=receipt_command,
        )
        assert received["existencia"]["estado_calidad"] == "PENDIENTE"
        assert received["existencia"]["cantidad_fisica"] == "98.000"
        assert received["existencia"]["cantidad_libre"] == "0"
        assert ScmExistenciaManga.query.count() == 1
        assert ScmMovimientoInventario.query.filter_by(
            tipo="INGRESO_PRODUCCION"
        ).count() == 1
        balance = ScmSaldoInventario.query.one()
        assert format(balance.cantidad_fisica, "f") == "98.000"
        assert format(balance.cantidad_no_disponible, "f") == "98.000"

        replay_receipt = receive_manga(
            db.session,
            actor_id=warehouse_actor.id,
            operation_id=receipt_operation,
            data=receipt_command,
        )
        assert replay_receipt == received
        assert ScmExistenciaManga.query.count() == 1

        released = decide_manga_quality(
            db.session,
            actor_id=quality_actor.id,
            existence_id=UUID(received["existencia"]["id"]),
            operation_id=uuid4(),
            data={
                "decision": "LIBERADA",
                "motivo": "Muestra UAT conforme",
                "version": received["existencia"]["version"],
            },
        )
        assert released["existencia"]["estado_calidad"] == "LIBERADA"
        assert released["existencia"]["cantidad_libre"] == "98.000"
        balance = ScmSaldoInventario.query.one()
        assert format(balance.cantidad_no_disponible, "f") == "0.000"

        post_receipt_request = request_weighing_correction(
            db.session,
            actor_id=creator.id,
            weighing_id=original.public_id,
            operation_id=uuid4(),
            data={
                "proposed": {"cantidad_confirmada": "97"},
                "motivo": "Conteo final conciliado despues de recepcion",
            },
        )
        post_receipt_correction = approve_weighing_correction(
            db.session,
            actor_id=approver.id,
            correction_id=UUID(post_receipt_request["correction"]["id"]),
            operation_id=uuid4(),
            data={"motivo_aprobacion": "Compensar Kardex sin borrar hechos"},
        )
        assert post_receipt_correction["ajuste_inventario"][
            "cantidad_delta"
        ] == "-1.000"
        existence = ScmExistenciaManga.query.one()
        assert format(existence.cantidad_fisica, "f") == "97.000"
        assert existence.manga.estado == "RECIBIDA"
        balance = ScmSaldoInventario.query.one()
        assert format(balance.cantidad_fisica, "f") == "97.000"
        assert format(balance.cantidad_no_disponible, "f") == "0.000"
        assert ScmMovimientoInventario.query.count() == 2

        # US-010H: la misma manga liberada se reserva, traslada y retorna
        # con custodia doble, sin crear un consumo manual independiente.
        finished_article = ScmArticulo(
            codigo="PT-H-FLOW",
            nombre="Producto armado H",
            clase="PRODUCTO_TERMINADO",
        )
        assembly_center = ScmCentroTrabajo(
            codigo="MESA-H-FLOW",
            nombre="Mesa de Armado H",
            tipo="ENSAMBLE",
        )
        structure = ScmEstructuraRevision(
            articulo_resultado=finished_article,
            numero_revision=1,
            estado="APROBADA",
            content_hash="3" * 64,
            creada_por_id=creator.id,
            aprobada_por_id=approver.id,
            componentes=[ScmEstructuraComponente(
                secuencia=1,
                articulo_componente=existence.articulo,
                cantidad=1,
                unidad="UN",
            )],
        )
        route = ScmRutaRevision(
            articulo_objetivo=finished_article,
            numero_revision=1,
            estado="APROBADA",
            content_hash="4" * 64,
            creada_por_id=creator.id,
            aprobada_por_id=approver.id,
        )
        db.session.add_all([finished_article, assembly_center, structure, route])
        db.session.flush()
        route_operation = ScmOperacionRuta(
            ruta=route,
            clave="ENSAMBLAR_H",
            secuencia_visible=1,
            nombre="Armar producto H",
            tipo="ENSAMBLE",
            executor_kind="ORDEN_OPERACION",
            centro_trabajo=assembly_center,
            articulo_salida=finished_article,
            estructura_revision=structure,
        )
        db.session.add(route_operation)
        db.session.flush()
        assembly_order = ScmOrdenOperacion(
            codigo="OA-H-FLOW",
            tipo="ENSAMBLE",
            origen_demanda="ORDEN_PRODUCCION",
            estado="LIBERADA",
            operacion_ruta_revision_id=route_operation.id,
            operacion_ruta_hash=route.content_hash,
            created_by_id=creator.id,
            released_by_id=approver.id,
            salidas=[ScmOrdenOperacionSalida(
                articulo=finished_article,
                cantidad_objetivo=10,
                peso_unitario_snapshot_g=100,
            )],
        )
        db.session.add(ScmArticuloPerfil(
            articulo=finished_article,
            perfil=ScmPerfilEmpacable.query.filter_by(
                nombre="Asa apilada"
            ).one(),
            es_predeterminado=True,
            activo=True,
        ))
        db.session.add(assembly_order)
        db.session.commit()

        assembly_plan = recalculate_assembly_manga_plan(
            db.session,
            actor_id=creator.id,
            order_id=assembly_order.id,
            operation_id=uuid4(),
            data={},
        )["plan"]
        assert assembly_plan["lineas"][0]["cantidad_objetivo_un"] == "10"

        assembly_ot = create_assembly_ot(
            db.session,
            actor_id=creator.id,
            order_id=assembly_order.id,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-08-04",
                "turno": "DIA",
                "centro_trabajo_id": assembly_center.id,
                "responsable_id": creator.id,
                "cantidad_objetivo": 10,
            },
        )["ot"]
        assigned_output = assign_assembly_output_mangas(
            db.session,
            actor_id=creator.id,
            ot_id=UUID(assembly_ot["public_id"]),
            operation_id=uuid4(),
            data={"version": assembly_ot["version"]},
        )
        output_manga = assigned_output["mangas"][0]
        listed = list_assembly_ots(
            db.session,
            actor_id=creator.id,
            order_id=assembly_order.id,
        )["items"]
        listed_ot = next(
            item for item in listed
            if item["public_id"] == assembly_ot["public_id"]
        )
        assert [item["public_id"] for item in listed_ot["mangas"]] == [
            output_manga["public_id"]
        ]
        assert listed_ot["orden_armado"] == {
            "id": str(assembly_order.id),
            "codigo": assembly_order.codigo,
            "salida": {
                "articulo": {
                    "id": finished_article.id,
                    "codigo": finished_article.codigo,
                    "nombre": finished_article.nombre,
                    "clase": finished_article.clase,
                    "unidad": finished_article.unidad_base,
                },
                "cantidad_objetivo": "10",
            },
        }
        assert listed_ot["abastecimiento"] is None
        supply = create_supply_request(
            db.session,
            actor_id=creator.id,
            ot_id=UUID(assembly_ot["public_id"]),
            operation_id=uuid4(),
        )["solicitud"]
        supply = assign_supply_manga(
            db.session,
            actor_id=warehouse_actor.id,
            request_id=UUID(supply["id"]),
            operation_id=uuid4(),
            data={
                "version": supply["version"],
                "linea_id": supply["lineas"][0]["id"],
                "manga_codigo": existence.manga.codigo,
            },
        )["solicitud"]
        assert supply["lineas"][0]["cantidad_asignada"] == "97.000"
        assert ScmMovimientoInventario.query.count() == 2

        supply = mark_supply_ready(
            db.session,
            actor_id=warehouse_actor.id,
            request_id=UUID(supply["id"]),
            operation_id=uuid4(),
            data={"version": supply["version"]},
        )["solicitud"]
        supply = dispatch_supply(
            db.session,
            actor_id=warehouse_actor.id,
            request_id=UUID(supply["id"]),
            operation_id=uuid4(),
            data={"version": supply["version"]},
        )["solicitud"]
        supply = receive_supply(
            db.session,
            actor_id=creator.id,
            request_id=UUID(supply["id"]),
            operation_id=uuid4(),
            data={"version": supply["version"]},
        )["solicitud"]
        assignment = supply["lineas"][0]["asignaciones"][0]
        assert assignment["estado"] == "EN_STAGING_ARMADO"
        assert assignment["manga"]["ubicacion"]["codigo"] == "MESA_ARMADO"

        prelabel = generate_prelabels(
            db.session,
            actor_id=creator.id,
            manga_id=UUID(output_manga["public_id"]),
            operation_id=uuid4(),
            data={},
        )
        acknowledge_station_print_job(
            db.session,
            station_id=station_id,
            print_job_id=UUID(prelabel["print_job_id"]),
            data={"results": [{
                "label_id": prelabel["labels"][0]["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        listed_after_prelabel = list_assembly_ots(
            db.session,
            actor_id=creator.id,
            order_id=assembly_order.id,
        )["items"]
        assert listed_after_prelabel[0]["mangas"][0]["estado"] == (
            "PREETIQUETADA"
        )
        assert listed_after_prelabel[0]["abastecimiento"] == {
            "codigo": supply["codigo"],
            "estado": "RECIBIDA",
        }
        started_ot = transition_ot(
            db.session,
            actor_id=creator.id,
            public_id=UUID(assembly_ot["public_id"]),
            operation_id=uuid4(),
            data={"version": assigned_output["ot"]["version"]},
            action="iniciar",
        )["ot"]
        closed = close_assembly_manga(
            db.session,
            actor_id=creator.id,
            manga_id=UUID(output_manga["public_id"]),
            operation_id=uuid4(),
            data={
                "version": started_ot["mangas"][0]["version"],
                "cantidad_real": 10,
            },
        )
        assert closed["manga"]["estado"] == (
            "CERRADA_ARMADO_PENDIENTE_PESAJE"
        )
        assert closed["confirmacion"]["cantidad_real"] == "10.000"
        assert closed["confirmacion"]["consumos"][0][
            "manga_origen_codigo"
        ] == existence.manga.codigo
        assert closed["pesaje_creado"] is False
        assert closed["kardex_salida_creado"] is False
        assert ScmConfirmacionMangaArmado.query.count() == 1
        assert format(existence.cantidad_fisica, "f") == "87.000"
        assert ScmPesajeManga.query.count() == 1
        listed_after_close = list_assembly_ots(
            db.session,
            actor_id=creator.id,
            order_id=assembly_order.id,
        )["items"]
        assert listed_after_close[0]["mangas"][0]["estado"] == (
            "CERRADA_ARMADO_PENDIENTE_PESAJE"
        )

        correction = request_assembly_quantity_correction(
            db.session,
            actor_id=creator.id,
            manga_id=UUID(output_manga["public_id"]),
            operation_id=uuid4(),
            data={
                "cantidad_propuesta": 9,
                "motivo": "Conteo de mesa rectificado antes del pesaje",
            },
        )["correccion"]
        with pytest.raises(ScmServiceError) as self_approval:
            approve_assembly_quantity_correction(
                db.session, actor_id=creator.id,
                correction_id=UUID(correction["id"]), operation_id=uuid4(),
                data={"motivo_aprobacion": "No debe autoaprobarse"},
            )
        assert self_approval.value.code == "FOUR_EYES_REQUIRED"
        corrected = approve_assembly_quantity_correction(
            db.session, actor_id=approver.id,
            correction_id=UUID(correction["id"]), operation_id=uuid4(),
            data={"motivo_aprobacion": "Conteo físico verificado por jefatura"},
        )
        assert corrected["manga"]["cantidad_confirmada_un"] == "9"
        assert corrected["correccion"]["estado"] == "APLICADA"
        assert ScmCorreccionMangaArmado.query.count() == 1
        assert format(existence.cantidad_fisica, "f") == "88.000"

        resolved_output = resolve_manga_label(
            db.session,
            label_id=UUID(prelabel["labels"][0]["public_id"]),
        )
        assert resolved_output["can_weigh"] is True
        assert resolved_output["manga"]["cantidad_confirmada_un"] == "9"
        assert resolved_output["manga"]["cantidad_fuente"] == (
            "RESPONSABLE_ARMADO"
        )
        assembly_weighing = confirm_manga_weighing(
            db.session,
            station_id=station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": prelabel["labels"][0]["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "1.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-08-04T14:31:09-05:00",
                "reading_stable": True,
            },
        )
        assert assembly_weighing["weighing"]["cantidad_confirmada"] == (
            "9.000"
        )
        assert assembly_weighing["weighing"]["fuente_cantidad"] == (
            "RESPONSABLE_ARMADO"
        )
        assert assembly_weighing["post_label"]["payload"]["oa_ot"] == (
            f"{assembly_order.codigo} - {assembly_ot['codigo_ot']}"
        )
        assert ScmPesajeManga.query.count() == 2

        supply = request_supply_return(
            db.session,
            actor_id=creator.id,
            assignment_id=UUID(assignment["id"]),
            operation_id=uuid4(),
        )["solicitud"]
        supply = dispatch_supply_return(
            db.session,
            actor_id=creator.id,
            assignment_id=UUID(assignment["id"]),
            operation_id=uuid4(),
        )["solicitud"]
        supply = receive_supply_return(
            db.session,
            actor_id=warehouse_actor.id,
            assignment_id=UUID(assignment["id"]),
            operation_id=uuid4(),
            data={"ubicacion_codigo": "RECEPCION_PIEZAS_WIP"},
        )["solicitud"]
        assert supply["estado"] == "CERRADA"
        final_existence = ScmExistenciaManga.query.one()
        assert final_existence.estado_logistico == "RECIBIDA_ALMACEN"
        assert format(final_existence.cantidad_reservada, "f") == "0.000"
        assert ScmMovimientoInventario.query.count() == 12


def test_anular_pesaje_exige_reversa_invalida_qr_y_devuelve_cupo(app):
    with app.app_context():
        creator, approver, order, _output = _seed_normalized_order()
        plan = recalculate_manga_plan(
            db.session, actor_id=creator.id, op_number=order.numero_op,
            operation_id=uuid4(), data={},
        )
        plan_line_id = plan["plan"]["lineas"][0]["id"]
        ot = create_ot(
            db.session, actor_id=creator.id, op_number=order.numero_op,
            operation_id=uuid4(),
            data={
                "fecha_operativa": "2026-07-28",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": plan_line_id,
                    "cantidad_un": 100,
                }],
            },
        )["ot"]
        manga_id = UUID(ot["mangas"][0]["public_id"])
        prelabel_job = generate_prelabels(
            db.session, actor_id=creator.id, manga_id=manga_id,
            operation_id=uuid4(), data={},
        )
        station_id = str(uuid4())
        db.session.add(EstacionPesaje(
            station_id=station_id,
            codigo="PESAJE-ANUL-01",
            nombre="Balanza anulacion",
            ubicacion="Piloto",
            token_hash="e" * 64,
        ))
        db.session.commit()
        prelabel = prelabel_job["labels"][0]
        acknowledge_station_print_job(
            db.session, station_id=station_id,
            print_job_id=UUID(prelabel_job["print_job_id"]),
            data={"results": [{
                "label_id": prelabel["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        weighed = confirm_manga_weighing(
            db.session, station_id=station_id, operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": prelabel["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "10.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-07-28T14:31:09-05:00",
                "reading_stable": True,
            },
        )
        acknowledge_station_print_job(
            db.session, station_id=station_id,
            print_job_id=UUID(weighed["print_job_id"]),
            data={"results": [{
                "label_id": weighed["post_label"]["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        warehouse_actor = Trabajador(
            codigo="TRB-ALM-ANUL",
            nombres="UAT",
            apellidos="Almacen anulacion",
            activo=True,
            roles=[RolOperativo.query.filter_by(
                codigo="ALMACEN_RECEPCION"
            ).one()],
        )
        db.session.add(warehouse_actor)
        db.session.commit()
        received = receive_manga(
            db.session, actor_id=warehouse_actor.id, operation_id=uuid4(),
            data={
                "label_id": weighed["post_label"]["public_id"],
                "ubicacion_codigo": "RECEPCION_PIEZAS_WIP",
                "presencia_confirmada": True,
                "bolsa_cerrada": True,
                "coincidencia_etiquetas": True,
            },
        )
        weighing_id = UUID(weighed["weighing"]["public_id"])
        command = {"motivo": "Sticker asociado a una manga descartada"}
        with pytest.raises(ScmServiceError) as reversal_required:
            annul_manga_weighing(
                db.session, actor_id=approver.id, weighing_id=weighing_id,
                operation_id=uuid4(), data=command,
            )
        assert reversal_required.value.code == "RECEIPT_REVERSAL_REQUIRED"

        reversal = request_receipt_reversal(
            db.session, actor_id=warehouse_actor.id,
            existence_id=UUID(received["existencia"]["id"]),
            operation_id=uuid4(),
            data={"motivo": "Retirar recepcion antes de anular pesaje"},
        )["reversion"]
        resolve_receipt_reversal(
            db.session, actor_id=approver.id,
            reversal_id=UUID(reversal["id"]), operation_id=uuid4(),
            data={"aprobar": True, "motivo": "Reversa validada"},
        )
        annul_operation = uuid4()
        annulled = annul_manga_weighing(
            db.session, actor_id=approver.id, weighing_id=weighing_id,
            operation_id=annul_operation, data=command,
        )
        assert annulled["manga"]["estado"] == "ANULADA"
        assert annulled["plan"]["cantidad_devuelta_un"] == "100.000"
        assert annulled["plan"]["cantidad_asignada_un"] == "0.000"
        assert ScmPesajeManga.query.count() == 1
        assert ScmAnulacionPesajeManga.query.count() == 1
        assert all(
            label.estado == "INVALIDADA"
            for label in ScmManga.query.one().etiquetas
        )
        with pytest.raises(ScmServiceError) as invalid_qr:
            resolve_manga_label(
                db.session,
                label_id=UUID(weighed["post_label"]["public_id"]),
            )
        assert invalid_qr.value.code == "LABEL_INVALIDATED"
        replay = annul_manga_weighing(
            db.session, actor_id=approver.id, weighing_id=weighing_id,
            operation_id=annul_operation, data=command,
        )
        assert replay == annulled
        replacement = add_normal_mangas(
            db.session, actor_id=approver.id,
            public_id=UUID(ot["public_id"]), operation_id=uuid4(),
            data={"plan_linea_id": plan_line_id, "cantidad_un": 100},
        )["mangas"]
        assert len(replacement) == 1
        assert replacement[0]["tipo"] == "NORMAL"
        assert replacement[0]["estado"] == "PLANIFICADA"
        detail = get_manga_weighing(
            db.session, actor_id=creator.id, manga_id=manga_id
        )
        assert detail["vigente"] is None
        assert detail["anulacion"]["motivo"] == command["motivo"]


def _seed_aggregate_color_work(*, quantity=100, operational_date="2026-08-11"):
    creator, approver, order, run, output = _seed_fabrication_order()
    if quantity > Decimal(output.cantidad_objetivo):
        output.cantidad_objetivo = quantity
        output.kg_estandar_objetivo = Decimal(quantity) / Decimal("10")
        run.ciclos_objetivo = quantity
        db.session.commit()
    plan = recalculate_fabrication_manga_plan(
        db.session,
        actor_id=creator.id,
        order_id=order.id,
        operation_id=uuid4(),
        data={},
    )["plan"]
    line = next(
        item for item in plan["lineas"]
        if item["corrida_fabricacion_id"] == str(run.id)
    )
    header = create_fabrication_ot_header(
        db.session,
        actor_id=creator.id,
        operation_id=uuid4(),
        data={
            "maquina_id": order.fabricacion.maquina_prevista_id,
            "fecha_operativa": operational_date,
            "turno": "DIA",
            "maquinista_predeterminado_id": creator.id,
        },
    )["ot"]
    created = add_color_work(
        db.session,
        actor_id=creator.id,
        ot_id=UUID(header["public_id"]),
        operation_id=uuid4(),
        data={
            "corrida_fabricacion_id": str(run.id),
            "maquinista_id": creator.id,
            "asignaciones": [{
                "plan_linea_id": line["id"],
                "cantidad_un": quantity,
            }],
        },
    )
    return creator, approver, order, run, output, line, header, created


def _print_color_manga(*, actor, manga_id, station_code):
    generated = generate_prelabels(
        db.session,
        actor_id=actor.id,
        manga_id=UUID(str(manga_id)),
        operation_id=uuid4(),
        data={},
    )
    station_uuid = uuid4()
    station = EstacionPesaje(
        station_id=str(station_uuid),
        codigo=station_code,
        nombre=f"Balanza {station_code}",
        ubicacion="Piloto",
        token_hash=station_uuid.hex * 2,
    )
    db.session.add(station)
    db.session.commit()
    acknowledge_station_print_job(
        db.session,
        station_id=station.station_id,
        print_job_id=UUID(generated["print_job_id"]),
        data={"results": [{
            "label_id": generated["labels"][0]["public_id"],
            "estado": "IMPRESA",
            "printer_name": "TSC",
        }]},
    )
    return station, generated["labels"][0]


def _seed_two_work_aggregate(*, first_quantity=200):
    creator, approver, order, first_run, first_output = (
        _seed_fabrication_order()
    )
    second_run = ScmCorridaFabricacion(
        orden_fabricacion=order.fabricacion,
        codigo="OF-000900-C02",
        secuencia=2,
        color_produccion_id=first_run.color_produccion_id,
        ciclos_objetivo=250,
        estado="LIBERADA",
    )
    second_output = ScmOrdenOperacionSalida(
        orden_operacion=order,
        corrida_fabricacion=second_run,
        articulo_scm_id=first_output.articulo_scm_id,
        cantidad_por_ciclo_snapshot=1,
        peso_unitario_snapshot_g=100,
        cantidad_objetivo=250,
        kg_estandar_objetivo=25,
    )
    relief = Trabajador(
        codigo="TRB-UAT-M-REL",
        nombres="Luis",
        apellidos="Relevo UAT M",
        activo=True,
        roles=[RolOperativo.query.filter_by(codigo="MAQUINISTA").one()],
    )
    db.session.add_all([second_run, second_output, relief])
    db.session.commit()
    plan = recalculate_fabrication_manga_plan(
        db.session, actor_id=creator.id, order_id=order.id,
        operation_id=uuid4(), data={},
    )["plan"]
    lines = {
        item["corrida_fabricacion_id"]: item for item in plan["lineas"]
    }
    header = create_fabrication_ot_header(
        db.session, actor_id=creator.id, operation_id=uuid4(),
        data={
            "maquina_id": order.fabricacion.maquina_prevista_id,
            "fecha_operativa": "2026-08-11",
            "turno": "DIA",
            "maquinista_predeterminado_id": creator.id,
        },
    )["ot"]
    first = add_color_work(
        db.session, actor_id=creator.id,
        ot_id=UUID(header["public_id"]), operation_id=uuid4(),
        data={
            "corrida_fabricacion_id": str(first_run.id),
            "maquinista_id": relief.id,
            "asignaciones": [{
                "plan_linea_id": lines[str(first_run.id)]["id"],
                "cantidad_un": first_quantity,
            }],
        },
    )
    second = add_color_work(
        db.session, actor_id=creator.id,
        ot_id=UUID(header["public_id"]), operation_id=uuid4(),
        data={
            "corrida_fabricacion_id": str(second_run.id),
            "maquinista_id": creator.id,
            "asignaciones": [{
                "plan_linea_id": lines[str(second_run.id)]["id"],
                "cantidad_un": 100,
            }],
        },
    )
    return {
        "creator": creator,
        "approver": approver,
        "order": order,
        "header": header,
        "first": first,
        "second": second,
        "relief": relief,
        "lines": lines,
        "runs": (first_run, second_run),
    }


def test_uat_m02_continuacion_y_cierre_explicito_de_cabecera(app):
    with app.app_context():
        creator, _approver, _order, run, _output, line, header, created = (
            _seed_aggregate_color_work(quantity=100)
        )
        work = created["trabajo_color"]
        with pytest.raises(ScmServiceError) as duplicate:
            add_color_work(
                db.session,
                actor_id=creator.id,
                ot_id=UUID(header["public_id"]),
                operation_id=uuid4(),
                data={
                    "corrida_fabricacion_id": str(run.id),
                    "maquinista_id": creator.id,
                    "asignaciones": [{
                        "plan_linea_id": line["id"], "cantidad_un": 50,
                    }],
                },
            )
        assert duplicate.value.code == "COLOR_WORK_ALREADY_EXISTS"

        work = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=uuid4(), data={"version": work["version"]},
            action="iniciar",
        )["trabajo_color"]
        pause_operation_id = uuid4()
        pause_payload = {
            "version": work["version"], "motivo": "Cambio a otro color",
        }
        work = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=pause_operation_id,
            data=pause_payload,
            action="pausar",
        )["trabajo_color"]
        pause_replay = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=pause_operation_id,
            data=pause_payload,
            action="pausar",
        )["trabajo_color"]
        assert pause_replay == work

        resume_operation_id = uuid4()
        resume_payload = {"version": work["version"]}
        resumed = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=resume_operation_id, data=resume_payload,
            action="reanudar",
        )["trabajo_color"]
        resume_replay = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=resume_operation_id, data=resume_payload,
            action="reanudar",
        )["trabajo_color"]
        assert resume_replay == resumed
        assert resumed["id"] == work["id"]
        paused = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(resumed["id"]),
            operation_id=uuid4(),
            data={"version": resumed["version"], "motivo": "Cambio tecnico"},
            action="pausar",
        )["trabajo_color"]

        run.receta_hash = "cambio-tecnico-v2"
        db.session.commit()
        continuation = add_color_work(
            db.session,
            actor_id=creator.id,
            ot_id=UUID(header["public_id"]),
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(run.id),
                "continua_de_id": paused["id"],
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": line["id"], "cantidad_un": 50,
                }],
            },
        )["trabajo_color"]
        assert continuation["continua_de_id"] == paused["id"]
        assert continuation["id"] != paused["id"]

        parent = db.session.get(
            ScmTrabajoOt, UUID(paused["id"])
        ).orden_trabajo
        with pytest.raises(ScmServiceError) as manual_start:
            transition_ot(
                db.session, actor_id=creator.id,
                public_id=parent.public_id, operation_id=uuid4(),
                data={"version": parent.version}, action="iniciar",
            )
        assert manual_start.value.code == "USE_COLOR_WORK_ACTION"
        with pytest.raises(ScmServiceError) as premature_close:
            transition_ot(
                db.session, actor_id=creator.id,
                public_id=parent.public_id, operation_id=uuid4(),
                data={"version": parent.version}, action="cerrar",
            )
        assert premature_close.value.code == "OT_HAS_PENDING_COLOR_WORKS"

        for current in ScmTrabajoOt.query.filter_by(
            orden_trabajo_id=parent.id
        ).all():
            transition_color_work(
                db.session, actor_id=creator.id, work_id=current.id,
                operation_id=uuid4(),
                data={"version": current.version, "motivo": "Cierre controlado"},
                action="anular",
            )
        db.session.refresh(parent)
        closed = transition_ot(
            db.session, actor_id=creator.id,
            public_id=parent.public_id, operation_id=uuid4(),
            data={"version": parent.version}, action="cerrar",
        )["ot"]
        assert closed["estado"] == "CERRADA"


def test_uat_m03_m08_m10_m13_pesaje_diferido_snapshot_y_correccion(app):
    with app.app_context():
        ctx = _seed_two_work_aggregate(first_quantity=200)
        creator = ctx["creator"]
        approver = ctx["approver"]
        first = ctx["first"]["trabajo_color"]
        second = ctx["second"]["trabajo_color"]
        first_mangas = ctx["first"]["mangas"]
        second_manga = ctx["second"]["mangas"][0]

        with pytest.raises(ScmServiceError) as cross_ot:
            assign_color_work_worker(
                db.session, actor_id=creator.id,
                work_id=UUID(first["id"]), operation_id=uuid4(),
                data={
                    "trabajador_id": ctx["relief"].id,
                    "motivo": "Intento de cruce controlado",
                    "version": first["version"],
                    "manga_ids": [second_manga["public_id"]],
                },
            )
        assert cross_ot.value.code == "MULTI_SHIFT_BAG_NOT_ENABLED"
        assert ScmManga.query.filter_by(
            public_id=UUID(second_manga["public_id"])
        ).one().trabajo_ot_id == UUID(second["id"])

        with pytest.raises(ScmServiceError) as mixed_batch:
            generate_prelabels(
                db.session,
                actor_id=creator.id,
                manga_id=UUID(first_mangas[0]["public_id"]),
                operation_id=uuid4(),
                data={"manga_ids": [
                    first_mangas[0]["public_id"],
                    second_manga["public_id"],
                ]},
            )
        assert mixed_batch.value.code == "MIXED_COLOR_WORK_LABEL_BATCH"

        station_a, label_a = _print_color_manga(
            actor=creator,
            manga_id=first_mangas[0]["public_id"],
            station_code="PESAJE-UAT-M-A",
        )
        station_b, label_b = _print_color_manga(
            actor=creator,
            manga_id=second_manga["public_id"],
            station_code="PESAJE-UAT-M-B",
        )
        first = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(first["id"]),
            operation_id=uuid4(), data={"version": first["version"]},
            action="iniciar",
        )["trabajo_color"]
        first = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(first["id"]),
            operation_id=uuid4(),
            data={"version": first["version"], "motivo": "Cambio a azul"},
            action="pausar",
        )["trabajo_color"]
        second = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(second["id"]),
            operation_id=uuid4(), data={"version": second["version"]},
            action="iniciar",
        )["trabajo_color"]

        resolved_a = resolve_manga_label(
            db.session, label_id=UUID(label_a["public_id"])
        )
        assert resolved_a["can_weigh"] is True
        assert resolved_a["manga"]["ot"]["id"] == ctx["header"]["public_id"]
        assert resolved_a["manga"]["trabajo_color"]["id"] == first["id"]
        assert resolved_a["manga"]["trabajo_color"]["estado"] == "PAUSADO"
        assert resolved_a["manga"]["asignacion_vigente"]["maquinista_id"] == (
            ctx["relief"].id
        )
        assert resolved_a["manga"]["of_ot"].endswith(" - OT-000001")
        assert "TC" not in resolved_a["manga"]["of_ot"]
        assert label_a["payload"]["qr"]["trabajo_color_id"] == first["id"]

        weighed_a = confirm_manga_weighing(
            db.session, station_id=station_a.station_id,
            operation_id=uuid4(), actor_id=creator.id,
            data={
                "label_id": label_a["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "10.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-08-11T14:31:09-05:00",
                "reading_stable": True,
            },
        )
        fact_a = ScmPesajeManga.query.filter_by(
            public_id=UUID(weighed_a["weighing"]["public_id"])
        ).one()
        assert fact_a.pesado_por_id == creator.id
        assert fact_a.asignacion_personal_trabajo.trabajador_id == ctx["relief"].id
        assert fact_a.asignacion_personal_trabajo.estado == "CERRADA"
        assert weighed_a["post_label"]["payload"]["qr"][
            "trabajo_color_id"
        ] == first["id"]

        correction = request_weighing_correction(
            db.session, actor_id=creator.id,
            weighing_id=fact_a.public_id, operation_id=uuid4(),
            data={
                "proposed": {"cantidad_confirmada": "98"},
                "motivo": "Conteo fisico corregido para UAT M",
            },
        )["correction"]
        approve_weighing_correction(
            db.session, actor_id=approver.id,
            correction_id=UUID(correction["id"]), operation_id=uuid4(),
            data={"motivo_aprobacion": "Evidencia fisica validada"},
        )
        db.session.expire_all()
        assert Decimal(db.session.get(ScmTrabajoOt, UUID(first["id"])).cantidad_confirmada_un) == Decimal("98")

        weighed_b = confirm_manga_weighing(
            db.session, station_id=station_b.station_id,
            operation_id=uuid4(), actor_id=creator.id,
            data={
                "label_id": label_b["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "10.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-08-11T15:01:09-05:00",
                "reading_stable": True,
            },
        )
        assert weighed_b["weighing"]["trabajo_color_id"] == second["id"]
        db.session.expire_all()
        first_model = db.session.get(ScmTrabajoOt, UUID(first["id"]))
        second_model = db.session.get(ScmTrabajoOt, UUID(second["id"]))
        assert first_model.estado == "PAUSADO"
        assert second_model.estado == "EN_EJECUCION"
        assert Decimal(second_model.cantidad_confirmada_un) == Decimal("100")
        aggregate = list_ots(
            db.session,
            actor_id=creator.id,
            operational_date="2026-08-11",
            machine_id=ctx["order"].fabricacion.maquina_prevista_id,
            shift="DIA",
        )["items"]
        assert len(aggregate) == 1
        assert aggregate[0]["cantidad_objetivo_un"] == "300"
        assert aggregate[0]["cantidad_confirmada_un"] == "198"

        with pytest.raises(ScmServiceError) as pending:
            transition_color_work(
                db.session, actor_id=creator.id, work_id=first_model.id,
                operation_id=uuid4(), data={"version": first_model.version},
                action="completar",
            )
        assert pending.value.code == "WORK_HAS_PENDING_MANGAS"
        pending_manga = next(
            manga for manga in first_model.mangas if manga.estado == "PLANIFICADA"
        )
        annul_manga(
            db.session, actor_id=approver.id,
            manga_id=pending_manga.public_id, operation_id=uuid4(),
            data={"motivo": "Saldo no producido al cierre"},
        )
        db.session.refresh(first_model)
        completed = transition_color_work(
            db.session, actor_id=creator.id, work_id=first_model.id,
            operation_id=uuid4(), data={"version": first_model.version},
            action="completar",
        )["trabajo_color"]
        db.session.refresh(second_model)
        assert completed["estado"] == "COMPLETADO"
        assert completed["cantidad_confirmada_un"] == "98"
        assert second_model.estado == "EN_EJECUCION"


def test_uat_m04_m05_m06_m07_relevo_stickers_y_frontera(app):
    with app.app_context():
        creator, _approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=1000)
        )
        work = created["trabajo_color"]
        mangas = created["mangas"]
        assert len(mangas) == 10
        renato = Trabajador(
            codigo="TRB-UAT-M-REN",
            nombres="Renato",
            apellidos="UAT M",
            activo=True,
            roles=[RolOperativo.query.filter_by(codigo="MAQUINISTA").one()],
        )
        luis = Trabajador(
            codigo="TRB-UAT-M-LUI",
            nombres="Luis",
            apellidos="UAT M",
            activo=True,
            roles=[RolOperativo.query.filter_by(codigo="MAQUINISTA").one()],
        )
        db.session.add_all([renato, luis])
        db.session.commit()
        plan_assignment = ScmAsignacionPlanMangaOt.query.filter_by(
            trabajo_ot_id=UUID(work["id"])
        ).one()
        quota_before = Decimal(plan_assignment.cantidad_asignada_un)

        first_six = [item["public_id"] for item in mangas[:6]]
        subset_operation_id = uuid4()
        subset_payload = {
            "trabajador_id": renato.id,
            "motivo": "Distribucion inicial por subconjunto",
            "version": work["version"],
            "manga_ids": first_six,
        }
        reassigned = assign_color_work_worker(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=subset_operation_id,
            data=subset_payload,
        )
        assignment_count = ScmAsignacionPersonalTrabajoOt.query.filter_by(
            trabajo_ot_id=UUID(work["id"])
        ).count()
        replay = assign_color_work_worker(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=subset_operation_id,
            data=subset_payload,
        )
        assert replay == reassigned
        assert ScmAsignacionPersonalTrabajoOt.query.filter_by(
            trabajo_ot_id=UUID(work["id"])
        ).count() == assignment_count
        work = reassigned["trabajo_color"]
        assert len(reassigned["mangas"]) == 6
        assert {
            ScmManga.query.filter_by(public_id=UUID(item)).one().maquinista_previsto_id
            for item in first_six
        } == {renato.id}
        assert {
            ScmManga.query.filter_by(
                public_id=UUID(item["public_id"])
            ).one().maquinista_previsto_id
            for item in mangas[6:]
        } == {creator.id}
        assert ScmAsignacionPersonalTrabajoOt.query.filter_by(
            trabajo_ot_id=UUID(work["id"]), estado="PREVISTA"
        ).count() == 2
        assert {
            ScmManga.query.filter_by(
                public_id=UUID(item["public_id"])
            ).one().asignacion_personal_trabajo.estado
            for item in mangas[6:]
        } == {"PREVISTA"}

        printed_manga = mangas[6]
        station, old_label = _print_color_manga(
            actor=creator,
            manga_id=printed_manga["public_id"],
            station_code="PESAJE-UAT-M-REL",
        )
        with pytest.raises(ScmServiceError) as unconfirmed_empty:
            assign_color_work_worker(
                db.session, actor_id=creator.id, work_id=UUID(work["id"]),
                operation_id=uuid4(),
                data={
                    "trabajador_id": luis.id,
                    "motivo": "Intento sin confirmar manga vacia",
                    "version": work["version"],
                    "manga_ids": [printed_manga["public_id"]],
                },
            )
        assert unconfirmed_empty.value.code == "EMPTY_STICKER_CONFIRMATION_REQUIRED"
        assert ScmEtiquetaManga.query.filter_by(
            public_id=UUID(old_label["public_id"])
        ).one().estado == "IMPRESA"
        replacement = assign_color_work_worker(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=uuid4(),
            data={
                "trabajador_id": luis.id,
                "motivo": "Relevo despues de imprimir",
                "version": work["version"],
                "manga_ids": [printed_manga["public_id"]],
                "confirmacion_stickers_vacios": True,
            },
        )
        work = replacement["trabajo_color"]
        jobs = replacement["trabajos_impresion_reemplazo"]
        assert len(jobs) == 1
        new_label = jobs[0]["labels"][0]
        old_model = ScmEtiquetaManga.query.filter_by(
            public_id=UUID(old_label["public_id"])
        ).one()
        assert old_model.estado == "INVALIDADA"
        assert old_model.reemplazada_por_id == ScmEtiquetaManga.query.filter_by(
            public_id=UUID(new_label["public_id"])
        ).one().id
        with pytest.raises(ScmServiceError) as old_qr:
            resolve_manga_label(
                db.session, label_id=UUID(old_label["public_id"])
            )
        assert old_qr.value.code == "LABEL_INVALIDATED"
        acknowledge_station_print_job(
            db.session, station_id=station.station_id,
            print_job_id=UUID(jobs[0]["print_job_id"]),
            data={"results": [{
                "label_id": new_label["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        new_qr = resolve_manga_label(
            db.session, label_id=UUID(new_label["public_id"])
        )
        assert new_qr["manga"]["asignacion_vigente"]["maquinista_id"] == luis.id
        assert new_qr["manga"]["trabajo_color"]["id"] == work["id"]

        open_manga = mangas[7]
        open_station, open_label = _print_color_manga(
            actor=creator,
            manga_id=open_manga["public_id"],
            station_code="PESAJE-UAT-M-OPEN",
        )
        pesajes_before = ScmPesajeManga.query.count()
        with pytest.raises(ScmServiceError) as open_relief:
            assign_color_work_worker(
                db.session, actor_id=creator.id, work_id=UUID(work["id"]),
                operation_id=uuid4(),
                data={
                    "trabajador_id": renato.id,
                    "motivo": "Intento de transferir manga con contenido",
                    "version": work["version"],
                    "manga_ids": [open_manga["public_id"]],
                    "manga_abierta": True,
                    "conteo_frontera": 37,
                },
            )
        assert open_relief.value.code == "OPEN_MANGA_RELIEF_NOT_ALLOWED"
        assert ScmPesajeManga.query.count() == pesajes_before
        assert ScmEtiquetaManga.query.filter_by(
            public_id=UUID(open_label["public_id"])
        ).one().estado == "IMPRESA"

        work = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(work["id"]),
            operation_id=uuid4(),
            data={"version": work["version"]},
            action="iniciar",
        )["trabajo_color"]
        personnel_only = assign_color_work_worker(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=uuid4(),
            data={
                "trabajador_id": renato.id,
                "motivo": "Cambio de turno sin stickers pendientes",
                "version": work["version"],
                "manga_ids": [],
            },
        )
        assert personnel_only["relevo_sin_stickers"] is True
        assert personnel_only["stickers_transferidos"] == 0
        assert personnel_only["mangas"] == []
        assert personnel_only["trabajos_impresion_reemplazo"] == []
        assert personnel_only["asignacion"]["trabajador_id"] == renato.id
        assert ScmPesajeManga.query.count() == pesajes_before
        db.session.refresh(plan_assignment)
        assert Decimal(plan_assignment.cantidad_asignada_un) == quota_before
        assert ScmManga.query.filter_by(trabajo_ot_id=UUID(work["id"])).count() == 10
        final_weighing = confirm_manga_weighing(
            db.session,
            station_id=open_station.station_id,
            operation_id=uuid4(),
            actor_id=creator.id,
            data={
                "label_id": open_label["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "10.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-08-11T17:01:09-05:00",
                "reading_stable": True,
            },
        )
        assert ScmPesajeManga.query.count() == pesajes_before + 1
        assert final_weighing["weighing"]["trabajo_color_id"] == work["id"]
        assert final_weighing["post_label"] is not None
        final_fact = ScmPesajeManga.query.filter_by(
            public_id=UUID(final_weighing["weighing"]["public_id"])
        ).one()
        # Una manga con contenido permanece atribuida al responsable
        # saliente aunque se pese después de registrar el relevo.
        assert final_fact.asignacion_personal_trabajo.trabajador_id == creator.id
        assert final_fact.pesado_por_id == creator.id


def test_uat_m01_m11_fachada_legacy_filtros_y_extras_por_trabajo(app):
    with app.app_context():
        creator, approver, order, first_run, first_output = (
            _seed_fabrication_order()
        )
        second_run = ScmCorridaFabricacion(
            orden_fabricacion=order.fabricacion,
            codigo="OF-000900-C02",
            secuencia=2,
            color_produccion_id=first_run.color_produccion_id,
            ciclos_objetivo=250,
            estado="LIBERADA",
        )
        second_output = ScmOrdenOperacionSalida(
            orden_operacion=order,
            corrida_fabricacion=second_run,
            articulo_scm_id=first_output.articulo_scm_id,
            cantidad_por_ciclo_snapshot=1,
            peso_unitario_snapshot_g=100,
            cantidad_objetivo=250,
            kg_estandar_objetivo=25,
        )
        db.session.add_all([second_run, second_output])
        db.session.commit()
        plan = recalculate_fabrication_manga_plan(
            db.session, actor_id=creator.id, order_id=order.id,
            operation_id=uuid4(), data={},
        )["plan"]
        lines = {
            item["corrida_fabricacion_id"]: item for item in plan["lineas"]
        }
        first = create_fabrication_ot(
            db.session, actor_id=creator.id, order_id=order.id,
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(first_run.id),
                "fecha_operativa": "2026-08-12",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": lines[str(first_run.id)]["id"],
                    "cantidad_un": 100,
                }],
            },
        )
        second = create_fabrication_ot(
            db.session, actor_id=creator.id, order_id=order.id,
            operation_id=uuid4(),
            data={
                "corrida_fabricacion_id": str(second_run.id),
                "fecha_operativa": "2026-08-12",
                "turno": "DIA",
                "maquinista_id": creator.id,
                "asignaciones": [{
                    "plan_linea_id": lines[str(second_run.id)]["id"],
                    "cantidad_un": 100,
                }],
            },
        )
        assert first["ot"]["public_id"] == second["ot"]["public_id"]
        assert len(second["ot"]["trabajos_color"]) == 2
        assert {
            item["corrida_fabricacion_id"]
            for item in second["ot"]["trabajos_color"]
        } == {str(first_run.id), str(second_run.id)}

        by_header = list_ots(
            db.session,
            actor_id=creator.id,
            operational_date="2026-08-12",
            machine_id=order.fabricacion.maquina_prevista_id,
            shift="dia",
        )
        assert [item["public_id"] for item in by_header["items"]] == [
            first["ot"]["public_id"]
        ]
        by_of = list_ots(
            db.session,
            actor_id=creator.id,
            operation_order_id=order.id,
        )
        assert [item["public_id"] for item in by_of["items"]] == [
            first["ot"]["public_id"]
        ]

        with pytest.raises(ScmServiceError) as duplicate_header:
            create_fabrication_ot_header(
                db.session, actor_id=creator.id, operation_id=uuid4(),
                data={
                    "maquina_id": order.fabricacion.maquina_prevista_id,
                    "fecha_operativa": "2026-08-12",
                    "turno": "DIA",
                    "maquinista_predeterminado_id": creator.id,
                },
            )
        assert duplicate_header.value.code == "OT_MACHINE_SHIFT_ALREADY_EXISTS"

        first_work = first["trabajo_color"]
        extra = request_extra_manga(
            db.session, actor_id=creator.id,
            public_id=UUID(first["ot"]["public_id"]), operation_id=uuid4(),
            data={
                "trabajo_color_id": first_work["id"],
                "plan_linea_id": lines[str(first_run.id)]["id"],
                "cantidad_un": 20,
                "motivo": "Exceso controlado por trabajo",
            },
        )["solicitud"]
        assert extra["trabajo_color_id"] == first_work["id"]
        approved = approve_extra_manga(
            db.session, actor_id=approver.id,
            request_id=UUID(extra["id"]), operation_id=uuid4(), data={},
        )
        assert approved["mangas"][0]["trabajo_color_id"] == first_work["id"]
        added = add_normal_mangas(
            db.session, actor_id=creator.id,
            public_id=UUID(first["ot"]["public_id"]), operation_id=uuid4(),
            data={
                "trabajo_color_id": first_work["id"],
                "plan_linea_id": lines[str(first_run.id)]["id"],
                "cantidad_un": 50,
            },
        )["mangas"]
        assert added[0]["trabajo_color_id"] == first_work["id"]

        wrong_machine = Maquina(
            codigo="MAQ-UAT-M-WRONG",
            nombre="Maquina UAT incompatible",
            tipo_maquina_id=db.session.get(
                Maquina, order.fabricacion.maquina_prevista_id
            ).tipo_maquina_id,
            activo=True,
        )
        db.session.add(wrong_machine)
        db.session.commit()
        wrong_header = create_fabrication_ot_header(
            db.session, actor_id=creator.id, operation_id=uuid4(),
            data={
                "maquina_id": wrong_machine.id,
                "fecha_operativa": "2026-08-13",
                "turno": "DIA",
                "maquinista_predeterminado_id": creator.id,
            },
        )["ot"]
        with pytest.raises(ScmServiceError) as incompatible:
            add_color_work(
                db.session, actor_id=creator.id,
                ot_id=UUID(wrong_header["public_id"]), operation_id=uuid4(),
                data={
                    "corrida_fabricacion_id": str(first_run.id),
                    "maquinista_id": creator.id,
                    "asignaciones": [{
                        "plan_linea_id": lines[str(first_run.id)]["id"],
                        "cantidad_un": 50,
                    }],
                },
            )
        assert incompatible.value.code == "COLOR_WORK_MACHINE_MISMATCH"

        ineligible = Trabajador(
            codigo="TRB-UAT-M-NO",
            nombres="No",
            apellidos="Maquinista",
            activo=True,
            roles=[RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()],
        )
        db.session.add(ineligible)
        db.session.commit()
        with pytest.raises(ScmServiceError) as eligibility:
            create_fabrication_ot_header(
                db.session, actor_id=creator.id, operation_id=uuid4(),
                data={
                    "maquina_id": wrong_machine.id,
                    "fecha_operativa": "2026-08-14",
                    "turno": "DIA",
                    "maquinista_predeterminado_id": ineligible.id,
                },
            )
        assert eligibility.value.code == "WORKER_NOT_ELIGIBLE"


def test_uat_m09_anular_pesaje_reabre_trabajo_y_permite_reemplazo(app):
    with app.app_context():
        creator, approver, _order, _run, _output, line, _header, created = (
            _seed_aggregate_color_work(quantity=100)
        )
        work = created["trabajo_color"]
        manga = created["mangas"][0]
        station, prelabel = _print_color_manga(
            actor=creator,
            manga_id=manga["public_id"],
            station_code="PESAJE-UAT-M-ANUL",
        )
        work = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=uuid4(), data={"version": work["version"]},
            action="iniciar",
        )["trabajo_color"]
        weighed = confirm_manga_weighing(
            db.session, station_id=station.station_id,
            operation_id=uuid4(), actor_id=creator.id,
            data={
                "label_id": prelabel["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "10.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-08-11T16:01:09-05:00",
                "reading_stable": True,
            },
        )
        acknowledge_station_print_job(
            db.session, station_id=station.station_id,
            print_job_id=UUID(weighed["print_job_id"]),
            data={"results": [{
                "label_id": weighed["post_label"]["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        warehouse_actor = Trabajador(
            codigo="TRB-UAT-M-ALM",
            nombres="Almacen",
            apellidos="UAT M",
            activo=True,
            roles=[RolOperativo.query.filter_by(
                codigo="ALMACEN_RECEPCION"
            ).one()],
        )
        db.session.add(warehouse_actor)
        db.session.commit()
        candidate = resolve_receiving_label(
            db.session,
            actor_id=warehouse_actor.id,
            label_id=UUID(weighed["post_label"]["public_id"]),
        )
        assert candidate["trabajo_color"]["id"] == work["id"]
        assert candidate["asignacion_personal_trabajo_id"] == (
            weighed["weighing"]["asignacion_personal_trabajo_id"]
        )
        with pytest.raises(ScmServiceError) as direct_delete:
            annul_manga(
                db.session, actor_id=approver.id,
                manga_id=UUID(manga["public_id"]), operation_id=uuid4(),
                data={"motivo": "Intento directo no permitido"},
            )
        assert direct_delete.value.code == "INVALID_STATE_TRANSITION"

        db.session.expire_all()
        work_model = db.session.get(ScmTrabajoOt, UUID(work["id"]))
        completed = transition_color_work(
            db.session, actor_id=creator.id, work_id=work_model.id,
            operation_id=uuid4(), data={"version": work_model.version},
            action="completar",
        )["trabajo_color"]
        assert completed["estado"] == "COMPLETADO"
        annulled = annul_manga_weighing(
            db.session, actor_id=approver.id,
            weighing_id=UUID(weighed["weighing"]["public_id"]),
            operation_id=uuid4(),
            data={"motivo": "Manga defectuosa antes de Almacen"},
        )
        assert annulled["trabajo_color_reabierto"] is True
        assert annulled["trabajo_color_id"] == work["id"]
        db.session.expire_all()
        work_model = db.session.get(ScmTrabajoOt, UUID(work["id"]))
        assert work_model.estado == "PAUSADO"
        planned = next(
            item for item in work_model.asignaciones_personal
            if item.estado == "PREVISTA"
        )
        assert planned.trabajador_id == creator.id
        replacement = add_work_mangas(
            db.session, actor_id=creator.id, work_id=work_model.id,
            operation_id=uuid4(),
            data={"plan_linea_id": line["id"], "cantidad_un": 100},
        )["mangas"][0]
        assert replacement["tipo"] == "NORMAL"
        assert replacement["estado"] == "PLANIFICADA"
        assert replacement["trabajo_color_id"] == work["id"]
        assert replacement["asignacion_personal_trabajo_id"] == str(planned.id)


def test_uat_m09_correccion_post_recepcion_y_reversa_conservan_trabajo(app):
    with app.app_context():
        creator, approver, _order, _run, _output, _line, _header, created = (
            _seed_aggregate_color_work(quantity=100)
        )
        work = created["trabajo_color"]
        manga = created["mangas"][0]
        station, prelabel = _print_color_manga(
            actor=creator,
            manga_id=manga["public_id"],
            station_code="PESAJE-UAT-M-REV",
        )
        work = transition_color_work(
            db.session, actor_id=creator.id, work_id=UUID(work["id"]),
            operation_id=uuid4(), data={"version": work["version"]},
            action="iniciar",
        )["trabajo_color"]
        weighed = confirm_manga_weighing(
            db.session, station_id=station.station_id,
            operation_id=uuid4(), actor_id=creator.id,
            data={
                "label_id": prelabel["public_id"],
                "capture_id": str(uuid4()),
                "peso_bruto_kg": "10.100",
                "tara_kg": "0.100",
                "tara_fuente": "TIPO_MANGA",
                "pesada_at": "2026-08-11T17:01:09-05:00",
                "reading_stable": True,
            },
        )
        acknowledge_station_print_job(
            db.session, station_id=station.station_id,
            print_job_id=UUID(weighed["print_job_id"]),
            data={"results": [{
                "label_id": weighed["post_label"]["public_id"],
                "estado": "IMPRESA",
                "printer_name": "TSC",
            }]},
        )
        warehouse_actor = Trabajador(
            codigo="TRB-UAT-M-ALM-REV",
            nombres="Almacen",
            apellidos="Reversa UAT M",
            activo=True,
            roles=[RolOperativo.query.filter_by(
                codigo="ALMACEN_RECEPCION"
            ).one()],
        )
        db.session.add(warehouse_actor)
        db.session.commit()
        received = receive_manga(
            db.session, actor_id=warehouse_actor.id, operation_id=uuid4(),
            data={
                "label_id": weighed["post_label"]["public_id"],
                "ubicacion_codigo": "RECEPCION_PIEZAS_WIP",
                "presencia_confirmada": True,
                "bolsa_cerrada": True,
                "coincidencia_etiquetas": True,
            },
        )
        correction = request_weighing_correction(
            db.session, actor_id=creator.id,
            weighing_id=UUID(weighed["weighing"]["public_id"]),
            operation_id=uuid4(),
            data={
                "proposed": {"cantidad_confirmada": "98"},
                "motivo": "Conteo conciliado despues de recepcion",
            },
        )["correction"]
        corrected = approve_weighing_correction(
            db.session, actor_id=approver.id,
            correction_id=UUID(correction["id"]), operation_id=uuid4(),
            data={"motivo_aprobacion": "Kardex y evidencia conciliados"},
        )
        db.session.expire_all()
        assert Decimal(db.session.get(
            ScmTrabajoOt, UUID(work["id"])
        ).cantidad_confirmada_un) == Decimal("98")
        assert corrected["ajuste_inventario"]["cantidad_delta"] == "-2.000"
        assert Decimal(ScmExistenciaManga.query.one().cantidad_fisica) == Decimal("98")

        reversal = request_receipt_reversal(
            db.session, actor_id=warehouse_actor.id,
            existence_id=UUID(received["existencia"]["id"]),
            operation_id=uuid4(),
            data={"motivo": "Reversa antes de anular el pesaje"},
        )["reversion"]
        resolve_receipt_reversal(
            db.session, actor_id=approver.id,
            reversal_id=UUID(reversal["id"]), operation_id=uuid4(),
            data={"aprobar": True, "motivo": "Evidencia validada"},
        )
        annulled = annul_manga_weighing(
            db.session, actor_id=approver.id,
            weighing_id=UUID(weighed["weighing"]["public_id"]),
            operation_id=uuid4(),
            data={"motivo": "Anulacion posterior a reversa"},
        )
        assert annulled["trabajo_color_id"] == work["id"]
        assert annulled["manga"]["estado"] == "ANULADA"
        assert ScmAnulacionPesajeManga.query.count() == 1


def test_cerrar_of_acredita_demanda_y_completa_op_sin_duplicar_kardex(app):
    from datetime import date

    from app.models.producto import ProductoTerminado
    from app.models.scm_inventory import ScmMovimientoInventario
    from app.models.scm_ot import ScmLoteArticulo
    from app.models.scm_production_orders import (
        ScmAsignacionDemandaSuministro,
        ScmOrdenProduccion,
        ScmOrdenProduccionLinea,
    )
    from app.services.scm_fabrication_order_service import (
        close_fabrication_order,
    )

    with app.app_context():
        (
            creator,
            approver,
            order,
            run,
            output,
            _line,
            _header,
            created,
        ) = _seed_aggregate_color_work(quantity=250)
        product = ProductoTerminado(
            cod_sku_pt="PT-CIERRE-OF",
            producto="Producto para cierre reconciliado",
            linea_id=1,
            familia_id=1,
        )
        demand = ScmOrdenProduccion(
            codigo="OP-CIERRE-OF",
            origen="PLANIFICACION",
            fecha_necesidad=date(2026, 8, 14),
            estado="PLANIFICADA",
            created_by_id=creator.id,
            approved_by_id=approver.id,
        )
        demand_line = ScmOrdenProduccionLinea(
            producto_terminado=product,
            cantidad_solicitada=Decimal("250"),
        )
        demand.lineas.append(demand_line)
        allocation = ScmAsignacionDemandaSuministro(
            orden_produccion_linea=demand_line,
            fuente_tipo="SALIDA_ORDEN",
            orden_operacion_salida=output,
            cantidad_planificada=Decimal("250"),
        )
        db.session.add_all([product, demand, allocation])
        db.session.commit()

        work = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]),
            operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]},
            action="iniciar",
        )["trabajo_color"]
        db.session.refresh(demand)
        assert demand.estado == "EN_COBERTURA"

        work_model = db.session.get(ScmTrabajoOt, UUID(work["id"]))
        for manga in work_model.mangas:
            manga.estado = "PESADA"
            manga.cantidad_confirmada_un = manga.cantidad_asignada_un
        db.session.commit()
        completed = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=work_model.id,
            operation_id=uuid4(),
            data={"version": work_model.version},
            action="completar",
        )["trabajo_color"]
        assert completed["cantidad_confirmada_un"] == "250"

        inventory_before = ScmMovimientoInventario.query.count()
        close_operation = uuid4()
        command = {"version": order.version}
        closed = close_fabrication_order(
            db.session,
            actor_id=approver.id,
            operation_id=close_operation,
            operation_order_id=order.id,
            data=command,
        )
        assert closed["estado"] == "CERRADA"
        assert closed["corridas"][0]["estado"] == "COMPLETADA"
        assert closed["corridas"][0]["salidas"][0]["cantidad_real"] == (
            "250.000"
        )
        assert closed["cierre"]["ordenes_produccion"][0]["estado"] == (
            "COMPLETADA"
        )
        db.session.refresh(demand)
        db.session.refresh(demand_line)
        db.session.refresh(allocation)
        assert demand.estado == "COMPLETADA"
        assert demand_line.estado == "SATISFECHA"
        assert allocation.estado == "SATISFECHA"
        assert Decimal(allocation.cantidad_satisfecha) == Decimal("250")
        assert Decimal(
            ScmLoteArticulo.query.filter_by(
                orden_operacion_salida_id=output.id
            ).one().cantidad_acreditada
        ) == Decimal("250")
        assert ScmMovimientoInventario.query.count() == inventory_before

        replay = close_fabrication_order(
            db.session,
            actor_id=approver.id,
            operation_id=close_operation,
            operation_order_id=order.id,
            data=command,
        )
        assert replay == closed
        assert ScmMovimientoInventario.query.count() == inventory_before


def test_cerrar_of_con_faltante_exige_motivo_y_no_completa_op(app):
    from datetime import date

    from app.models.producto import ProductoTerminado
    from app.models.scm_production_orders import (
        ScmAsignacionDemandaSuministro,
        ScmOrdenProduccion,
        ScmOrdenProduccionLinea,
    )
    from app.services.scm_fabrication_order_service import (
        close_fabrication_order,
    )

    with app.app_context():
        (
            creator,
            approver,
            order,
            _run,
            output,
            _line,
            _header,
            created,
        ) = _seed_aggregate_color_work(quantity=250)
        product = ProductoTerminado(
            cod_sku_pt="PT-CIERRE-PARCIAL",
            producto="Producto con cierre parcial",
            linea_id=1,
            familia_id=1,
        )
        demand = ScmOrdenProduccion(
            codigo="OP-CIERRE-PARCIAL",
            origen="PLANIFICACION",
            fecha_necesidad=date(2026, 8, 14),
            estado="PLANIFICADA",
            created_by_id=creator.id,
            approved_by_id=approver.id,
        )
        demand_line = ScmOrdenProduccionLinea(
            producto_terminado=product,
            cantidad_solicitada=Decimal("250"),
        )
        demand.lineas.append(demand_line)
        allocation = ScmAsignacionDemandaSuministro(
            orden_produccion_linea=demand_line,
            fuente_tipo="SALIDA_ORDEN",
            orden_operacion_salida=output,
            cantidad_planificada=Decimal("250"),
        )
        db.session.add_all([product, demand, allocation])
        db.session.commit()

        work = transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=UUID(created["trabajo_color"]["id"]),
            operation_id=uuid4(),
            data={"version": created["trabajo_color"]["version"]},
            action="iniciar",
        )["trabajo_color"]
        with pytest.raises(ScmServiceError) as pending_work:
            close_fabrication_order(
                db.session,
                actor_id=approver.id,
                operation_id=uuid4(),
                operation_order_id=order.id,
                data={"version": order.version},
            )
        assert pending_work.value.code == "OF_HAS_PENDING_WORKS"
        order = db.session.get(ScmOrdenOperacion, order.id)
        work_model = db.session.get(ScmTrabajoOt, UUID(work["id"]))
        first, *remaining = work_model.mangas
        first.estado = "PESADA"
        first.cantidad_confirmada_un = first.cantidad_asignada_un
        for manga in remaining:
            manga.estado = "ANULADA"
        db.session.commit()
        transition_color_work(
            db.session,
            actor_id=creator.id,
            work_id=work_model.id,
            operation_id=uuid4(),
            data={"version": work_model.version},
            action="completar",
        )

        with pytest.raises(ScmServiceError) as missing_reason:
            close_fabrication_order(
                db.session,
                actor_id=approver.id,
                operation_id=uuid4(),
                operation_order_id=order.id,
                data={"version": order.version},
            )
        assert missing_reason.value.code == "OF_CLOSE_REASON_REQUIRED"
        db.session.refresh(order)
        db.session.refresh(allocation)
        assert order.estado == "EN_EJECUCION"
        assert Decimal(allocation.cantidad_satisfecha) == Decimal("0")

        closed = close_fabrication_order(
            db.session,
            actor_id=approver.id,
            operation_id=uuid4(),
            operation_order_id=order.id,
            data={
                "version": order.version,
                "motivo": "Se terminó el turno con saldo pendiente",
            },
        )
        assert closed["estado"] == "CERRADA"
        assert closed["cierre"]["diferencias"] == [{
            "salida_id": str(output.id),
            "articulo": output.articulo.codigo,
            "objetivo": "250.000",
            "real": "100.000",
        }]
        db.session.refresh(demand)
        db.session.refresh(demand_line)
        db.session.refresh(allocation)
        assert demand.estado == "EN_COBERTURA"
        assert demand_line.estado == "ACTIVA"
        assert allocation.estado == "COMPROMETIDA"
        assert Decimal(allocation.cantidad_satisfecha) == Decimal("100")
