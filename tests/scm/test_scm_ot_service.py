from uuid import UUID, uuid4

import pytest

from app.extensions import db
from app.models.lote import LoteColor, LoteSalidaPiezaColor
from app.models.molde import Molde, MoldePieza, Pieza
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
from app.models.scm_ot import ScmManga
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
from app.services.scm_ot_service import (
    acknowledge_station_print_job,
    annul_manga,
    approve_extra_manga,
    create_fabrication_ot,
    create_ot,
    generate_prelabels,
    list_extra_manga_requests,
    recalculate_manga_plan,
    recalculate_fabrication_manga_plan,
    replace_prelabel,
    request_extra_manga,
    transition_ot,
)
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
        assert ot["orden_operacion_id"] == str(order.id)
        assert ot["corrida_fabricacion_id"] == str(run.id)
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
        assert labels["template"]["version"] == "PREPESAJE_TSPL_2"
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
            "POSTPESAJE_TSPL_2"
        )
        assert weighed["post_label"]["payload"]["of_ot"] == (
            "OF-000900 - OT-000001"
        )


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
            nombre="Ensamblar producto H",
            tipo="ENSAMBLE",
            executor_kind="ORDEN_OPERACION",
            centro_trabajo=assembly_center,
            articulo_salida=finished_article,
            estructura_revision=structure,
        )
        db.session.add(route_operation)
        db.session.flush()
        assembly_order = ScmOrdenOperacion(
            codigo="OE-H-FLOW",
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
        assert assembly_weighing["post_label"]["payload"]["oe_ot"] == (
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
