import copy
from decimal import Decimal
from uuid import UUID, uuid4

from app.extensions import db
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.orden import OrdenProduccion
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    FamiliaColor,
    PiezaColor,
    ProductoTerminado,
)
from app.models.receta_color import RecetaColorLinea, RecetaColorMaestra
from app.models.scm_articulos import (
    ScmArticulo,
    ScmArticuloPiezaColor,
    ScmArticuloProducto,
)
from app.models.scm_catalogos import (
    ScmCapacidad,
    ScmCategoriaRecepcion,
    ScmMaterial,
)
from app.models.scm_empaque import (
    ScmArticuloPerfil,
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_estructuras import (
    ScmEstructuraComponente,
    ScmEstructuraRevision,
)
from app.models.scm_product_onboarding import ScmAltaProductoSesion
from app.models.scm_rutas import (
    ScmCentroTrabajo,
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.trabajador import Trabajador
from app.services.scm_article_service import create_wip_article


STEPS = (
    "IDENTIDAD",
    "COMPONENTES",
    "COLORES",
    "ESTRUCTURA",
    "RUTA_EMPAQUE",
    "REVISION",
)


def _actor(app):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        capability = ScmCapacidad.query.filter_by(
            codigo="ARTICULO_ADMINISTRAR"
        ).one_or_none()
        if capability is None:
            capability = ScmCapacidad(
                codigo="ARTICULO_ADMINISTRAR",
                nombre="Administrar articulos SCM",
            )
            db.session.add(capability)
        if capability not in actor.roles[0].capacidades:
            actor.roles[0].capacidades.append(capability)
        db.session.commit()
        return actor.id


def _headers(actor_id, operation_id):
    return {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": str(operation_id),
    }


def _ready_graph(app, actor_id):
    with app.app_context():
        product = ProductoTerminado(
            cod_sku_pt="PT-READINESS-001",
            producto="PT READINESS",
            linea_id=1,
            familia_id=1,
            um="UN",
            status="ACTIVO",
        )
        piece = Pieza(
            codigo="PZ-READINESS-001",
            nombre="PIEZA READINESS",
            peso_nominal_gr=90,
            activo=True,
        )
        mold = Molde(
            codigo="ML-READINESS-001",
            nombre="MOLDE READINESS",
            peso_tiro_gr=100,
            tiempo_ciclo_std=30,
            activo=True,
        )
        family = FamiliaColor(
            codigo=9920,
            nombre="FAMILIA READINESS",
            activo=True,
        )
        base = ColorBase(nombre="COLOR READINESS")
        db.session.add_all([product, piece, mold, family, base])
        db.session.flush()
        composition = MoldePieza(
            molde_id=mold.codigo,
            pieza_id=piece.id,
            cavidades=1,
            peso_unitario_gr=90,
            activo=True,
        )
        color = ColorProduccion(
            color_base_id=base.id,
            familia_color_id=family.id,
            hex_referencia="#123456",
            activo=True,
        )
        db.session.add_all([composition, color])
        db.session.flush()
        variant = PiezaColor(
            sku="PC-READINESS-001",
            piezas="PIEZA COLOR READINESS",
            pieza_id=piece.id,
            color_produccion_id=color.id,
            peso=90,
            estado_revision="VERIFICADO",
        )
        recipe = RecetaColorMaestra(
            color_produccion_id=color.id,
            producto_sku=product.cod_sku_pt,
            producto_scope=product.cod_sku_pt,
            nombre_variante="READINESS",
            revision=1,
            estado="APROBADA",
            es_default=True,
            base_virgen_kg=Decimal("25"),
            origen="TEST",
        )
        db.session.add_all([variant, recipe])
        db.session.flush()
        product_article = db.session.scalar(
            db.select(ScmArticulo)
            .join(ScmArticuloProducto)
            .where(
                ScmArticuloProducto.producto_terminado_id
                == product.cod_sku_pt
            )
        )
        piece_article = db.session.scalar(
            db.select(ScmArticulo)
            .join(ScmArticuloPiezaColor)
            .where(ScmArticuloPiezaColor.pieza_color_sku == variant.sku)
        )
        structure = ScmEstructuraRevision(
            articulo_resultado_id=product_article.id,
            numero_revision=1,
            estado="APROBADA",
            creada_por_id=actor_id,
            aprobada_por_id=actor_id,
            aprobada_at=db.func.now(),
            content_hash="a" * 64,
        )
        db.session.add(structure)
        db.session.flush()
        db.session.add(ScmEstructuraComponente(
            revision_id=structure.id,
            secuencia=1,
            articulo_componente_id=piece_article.id,
            cantidad=1,
            unidad="UN",
        ))
        center = ScmCentroTrabajo(
            codigo="CT-READINESS-001",
            nombre="CENTRO READINESS",
            tipo="INYECCION",
            activo=True,
        )
        db.session.add(center)
        db.session.flush()
        route = ScmRutaRevision(
            articulo_objetivo_id=product_article.id,
            numero_revision=1,
            estado="APROBADA",
            creada_por_id=actor_id,
            aprobada_por_id=actor_id,
            aprobada_at=db.func.now(),
            content_hash="b" * 64,
        )
        db.session.add(route)
        db.session.flush()
        db.session.add(ScmOperacionRuta(
            ruta_id=route.id,
            clave="FABRICAR",
            secuencia_visible=1,
            nombre="Fabricar",
            tipo="INYECCION",
            executor_kind="OP_OT",
            centro_trabajo_id=center.id,
            articulo_salida_id=product_article.id,
            permite_concurrente=False,
        ))
        profile = ScmPerfilEmpacable(
            codigo="PEM-READINESS-001",
            nombre="PERFIL READINESS",
            activo=True,
        )
        container = ScmTipoContenedor(
            codigo="CONT-READINESS-001",
            clase="MANGA",
            nombre="MANGA READINESS",
            tara_nominal_g=Decimal("100"),
            tolerancia_tara_g=Decimal("10"),
            peso_bruto_max_kg=Decimal("10"),
            activo=True,
        )
        db.session.add_all([profile, container])
        db.session.flush()
        db.session.add(ScmArticuloPerfil(
            articulo_id=product_article.id,
            perfil_empacable_id=profile.id,
            es_predeterminado=True,
            activo=True,
        ))
        rule = ScmReglaEmpaque(
            perfil_empacable_id=profile.id,
            tipo_contenedor_id=container.id,
        )
        db.session.add(rule)
        db.session.flush()
        rule_revision = ScmReglaEmpaqueRevision(
            regla_id=rule.id,
            numero_revision=1,
            estado="APROBADA",
            medicion_fisica_probada=True,
            cantidad_objetivo_un=10,
            cantidad_maxima_probada_un=12,
            peso_neto_operativo_max_kg=Decimal("9"),
            margen_seguridad_kg=Decimal("0.5"),
            tolerancia_peso_abs_g=Decimal("50"),
            tolerancia_peso_pct=Decimal("2"),
            creada_por_id=actor_id,
            aprobada_por_id=actor_id,
            aprobada_at=db.func.now(),
            content_hash="c" * 64,
        )
        db.session.add(rule_revision)
        db.session.flush()
        onboarding = ScmAltaProductoSesion(
            titulo="PT READINESS",
            producto_terminado_id=product.cod_sku_pt,
            creada_por_id=actor_id,
            actualizada_por_id=actor_id,
            paso_actual="REVISION",
            borrador_json={
                **{
                    code: {"capturado": True}
                    for code in STEPS if code != "REVISION"
                },
                "REVISION": {
                    "confirmaciones": {
                        "datos_fuente_revisados": True,
                        "entiende_que_no_crea_op": True,
                        "pendientes_aceptados": True,
                    },
                    "pasos_revisados": [
                        code for code in STEPS if code != "REVISION"
                    ],
                    "revisiones_revisadas": [
                        {
                            "tipo": "ESTRUCTURA",
                            "id": structure.id,
                            "version": structure.version,
                            "content_hash": structure.content_hash,
                        },
                        {
                            "tipo": "PERFIL_EMPAQUE",
                            "id": profile.id,
                            "version": profile.version,
                            "content_hash": None,
                        },
                        {
                            "tipo": "REGLA_EMPAQUE",
                            "id": rule_revision.id,
                            "version": rule_revision.version,
                            "content_hash": rule_revision.content_hash,
                        },
                        {
                            "tipo": "RUTA",
                            "id": route.id,
                            "version": route.version,
                            "content_hash": route.content_hash,
                        },
                    ],
                },
            },
            estados_paso_json={code: "COMPLETADO" for code in STEPS},
            referencias_json={
                "IDENTIDAD": {
                    "producto_terminado_id": product.cod_sku_pt,
                },
                "COMPONENTES": {
                    "molde_ref": mold.codigo,
                    "piezas": [{
                        "client_id": "pieza",
                        "pieza_ref": piece.id,
                        "molde_pieza_ref": composition.id,
                    }],
                },
                "COLORES": {
                    "colores": [{"color_ref": color.id}],
                    "matriz": [{
                        "pieza_ref": piece.id,
                        "color_ref": color.id,
                        "pieza_color_ref": variant.sku,
                    }],
                    "formulaciones": [{
                        "color_ref": color.id,
                        "tipo": "EXISTENTE",
                        "receta_ref": recipe.id,
                        "estado": "RESUELTA",
                    }],
                },
                "ESTRUCTURA": {
                    "estructura_revision_ref": structure.id,
                },
                "RUTA_EMPAQUE": {
                    "ruta_revision_ref": route.id,
                    "perfil_empacable_ref": profile.id,
                    "regla_empaque_revision_ref": rule_revision.id,
                },
            },
        )
        db.session.add(onboarding)
        db.session.commit()
        return {
            "session_id": str(onboarding.id),
            "version": onboarding.version,
            "route_id": route.id,
            "structure_id": structure.id,
            "recipe_id": recipe.id,
            "product_article_id": product_article.id,
            "profile_id": profile.id,
            "rule_id": rule.id,
            "rule_revision_id": rule_revision.id,
        }


def test_validar_alta_bloquea_pt_con_ruta_borrador(app, client):
    actor_id = _actor(app)
    graph = _ready_graph(app, actor_id)
    with app.app_context():
        route = db.session.get(ScmRutaRevision, graph["route_id"])
        route.estado = "BORRADOR"
        route.aprobada_por_id = None
        route.aprobada_at = None
        db.session.commit()

    response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": graph["version"]},
    )

    assert response.status_code == 200, response.get_json()
    readiness = response.get_json()["readiness"]
    assert readiness["status"] == "PENDING_APPROVAL"
    assert readiness["checked_at"]
    assert "ROUTE_NOT_APPROVED" in {
        item["code"] for item in readiness["items"]
    }


def test_restaurar_colores_desde_bom_aprobada_reconstruye_borrador_sin_crear(
    app, client
):
    actor_id = _actor(app)
    graph = _ready_graph(app, actor_id)
    with app.app_context():
        onboarding = db.session.get(
            ScmAltaProductoSesion, UUID(graph["session_id"])
        )
        states = dict(onboarding.estados_paso_json)
        states["COLORES"] = "INVALIDADO"
        states["ESTRUCTURA"] = "INVALIDADO"
        onboarding.estados_paso_json = states
        onboarding.paso_actual = "COLORES"
        previous_references = copy.deepcopy(
            onboarding.referencias_json["COLORES"]
        )
        onboarding.application_journal_json = {
            "COMPONENTES": {
                "components-complete-v1": {
                    "status": "APPLIED",
                    "session_version": onboarding.version,
                    "recorded_at": "2026-08-20T19:00:00+00:00",
                    "result": {
                        "created": [],
                        "reused": [],
                        "pending": [],
                        "resolved_references": copy.deepcopy(
                            onboarding.referencias_json["COMPONENTES"]
                        ),
                    },
                },
            },
            "COLORES": {
                "colors-complete-before-recovery-v1": {
                    "status": "APPLIED",
                    "session_version": onboarding.version,
                    "recorded_at": "2026-08-20T20:00:00+00:00",
                    "result": {
                        "created": [],
                        "reused": [],
                        "pending": [],
                        "resolved_references": previous_references,
                    },
                },
            },
        }
        db.session.commit()
        version = onboarding.version
        before_variants = PiezaColor.query.count()
        before_recipes = RecetaColorMaestra.query.count()

    response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}"
        "/pasos/COLORES/restaurar-desde-estructura",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": version},
    )
    assert response.status_code == 200, response.get_json()
    recovered = response.get_json()
    color_step = next(
        step for step in recovered["pasos"] if step["codigo"] == "COLORES"
    )
    assert color_step["estado"] == "EN_PROGRESO"
    assert color_step["data"]["recuperada_desde_estructura_ref"] == (
        graph["structure_id"]
    )
    assert color_step["data"]["colores"][0]["nombre"] == "COLOR READINESS FAMILIA READINESS"
    assert color_step["data"]["matriz"] == [{
        "pieza_ref": 1,
        "color_ref": 1,
        "seleccionada": True,
        "pieza_color_ref": "PC-READINESS-001",
    }]
    assert color_step["data"]["formulaciones"][0]["tipo"] == "EXISTENTE"
    assert recovered["color_recovery"] == {
        "estructura_revision_ref": graph["structure_id"],
        "colores": 1,
        "piezas_color": 1,
    }

    reapplied_response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}"
        "/pasos/COLORES/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": recovered["version"],
            "application_key": "colors-after-recovery-v2",
            "supersedes_application_key": (
                "colors-complete-before-recovery-v1"
            ),
            "data": {
                "colores": color_step["data"]["colores"],
                "matriz": color_step["data"]["matriz"],
                "formulaciones": color_step["data"]["formulaciones"],
            },
        },
    )
    assert reapplied_response.status_code == 200, (
        reapplied_response.get_json()
    )
    assert reapplied_response.get_json()["pasos"][2][
        "application_status"
    ]["application_key"] == "colors-after-recovery-v2"
    with app.app_context():
        assert PiezaColor.query.count() == before_variants
        assert RecetaColorMaestra.query.count() == before_recipes


def test_validar_alta_ready_y_finalizar_es_idempotente_sin_crear_op(
    app, client
):
    actor_id = _actor(app)
    graph = _ready_graph(app, actor_id)
    with app.app_context():
        before_orders = OrdenProduccion.query.count()
    validated_response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": graph["version"]},
    )
    assert validated_response.status_code == 200
    validated = validated_response.get_json()
    assert validated["readiness"]["status"] == "READY"
    assert validated["estado"] == "LISTA_PARA_PUBLICAR"

    operation_id = uuid4()
    finalize_payload = {"expected_version": validated["version"]}
    finalized_response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/finalizar",
        headers=_headers(actor_id, operation_id),
        json=finalize_payload,
    )
    assert finalized_response.status_code == 200
    finalized = finalized_response.get_json()
    assert finalized["estado"] == "FINALIZADA"
    assert finalized["readiness"]["status"] == "READY"
    replay = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/finalizar",
        headers=_headers(actor_id, operation_id),
        json=finalize_payload,
    )
    assert replay.status_code == 200
    assert replay.get_json() == finalized
    with app.app_context():
        assert OrdenProduccion.query.count() == before_orders


def test_finalizar_revalida_cambios_canonicos_posteriores(app, client):
    actor_id = _actor(app)
    graph = _ready_graph(app, actor_id)
    validated = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": graph["version"]},
    ).get_json()
    assert validated["readiness"]["status"] == "READY"
    with app.app_context():
        route = db.session.get(ScmRutaRevision, graph["route_id"])
        route.estado = "RETIRADA"
        db.session.commit()

    response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/finalizar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": validated["version"]},
    )

    assert response.status_code == 422
    current = response.get_json()["error"]["details"]["current_session"]
    assert current["estado"] == "CON_BLOQUEOS"
    assert current["readiness"]["status"] == "BLOCKED"
    assert "ROUTE_NOT_CURRENT" in {
        item["code"] for item in current["readiness"]["items"]
    }


def test_finalizar_rechaza_confirmacion_si_revision_canonica_cambio(
    app, client
):
    actor_id = _actor(app)
    graph = _ready_graph(app, actor_id)
    validated_response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": graph["version"]},
    )
    assert validated_response.status_code == 200
    validated = validated_response.get_json()
    assert validated["readiness"]["status"] == "READY"
    confirmed_snapshot = validated["readiness"]["revision_snapshot"]

    with app.app_context():
        route = db.session.get(ScmRutaRevision, graph["route_id"])
        route.version += 1
        route.content_hash = "d" * 64
        db.session.commit()

    finalized_response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/finalizar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": validated["version"]},
    )
    assert finalized_response.status_code == 422
    current = finalized_response.get_json()["error"]["details"][
        "current_session"
    ]
    assert current["estado"] == "CON_BLOQUEOS"
    assert current["finalizada_at"] is None
    assert "REVISION_CONFIRMATION_STALE" in {
        item["code"] for item in current["readiness"]["items"]
    }
    current_snapshot = current["readiness"]["revision_snapshot"]
    assert current_snapshot != confirmed_snapshot

    reconfirmed_response = client.put(
        f"/api/scm/v1/altas-producto/{graph['session_id']}"
        "/pasos/REVISION",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": current["version"],
            "estado_paso": "COMPLETADO",
            "data": {
                "confirmaciones": {
                    "datos_fuente_revisados": True,
                    "entiende_que_no_crea_op": True,
                    "pendientes_aceptados": True,
                },
                "pasos_revisados": [
                    code for code in STEPS if code != "REVISION"
                ],
                "revisiones_revisadas": current_snapshot,
            },
        },
    )
    assert reconfirmed_response.status_code == 200
    assert reconfirmed_response.get_json()["estado"] == "BORRADOR"


def test_readiness_bloquea_receta_aprobada_con_material_inactivo(
    app, client
):
    actor_id = _actor(app)
    graph = _ready_graph(app, actor_id)
    with app.app_context():
        category = ScmCategoriaRecepcion(
            codigo="CAT-READINESS-MATERIAL",
            nombre="Categoria readiness material",
            modalidad_default="POR_CONFIGURAR",
            recepcion_habilitada=False,
            activo=True,
        )
        db.session.add(category)
        db.session.flush()
        material = ScmMaterial(
            codigo="MP-READINESS-INACTIVE",
            nombre="Material readiness",
            clase="MATERIA_PRIMA",
            categoria_recepcion_id=category.id,
            activo=True,
        )
        db.session.add(material)
        db.session.flush()
        db.session.add(RecetaColorLinea(
            receta_id=graph["recipe_id"],
            material_id=material.id,
            tipo_componente="MATERIA_PRIMA",
            cantidad=Decimal("1"),
            unidad="FRACCION",
            orden=1,
        ))
        db.session.commit()
        material_id = material.id

    ready_response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": graph["version"]},
    )
    assert ready_response.status_code == 200
    ready = ready_response.get_json()
    assert ready["readiness"]["status"] == "READY"

    with app.app_context():
        material = db.session.get(ScmMaterial, material_id)
        material.activo = False
        material.version += 1
        db.session.commit()

    blocked_response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": ready["version"]},
    )
    assert blocked_response.status_code == 200
    readiness = blocked_response.get_json()["readiness"]
    assert readiness["status"] == "BLOCKED"
    assert "FORMULATION_MATERIAL_INACTIVE" in {
        item["code"] for item in readiness["items"]
    }


def test_revision_no_acepta_checklist_opaco_ni_finaliza_legacy_invalido(
    app, client
):
    actor_id = _actor(app)
    graph = _ready_graph(app, actor_id)
    invalid_put = client.put(
        f"/api/scm/v1/altas-producto/{graph['session_id']}"
        "/pasos/REVISION",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": graph["version"],
            "data": {},
            "estado_paso": "COMPLETADO",
        },
    )
    assert invalid_put.status_code == 422
    assert invalid_put.get_json()["error"]["code"] == (
        "REVISION_CONFIRMATION_REQUIRED"
    )

    with app.app_context():
        onboarding = db.session.get(
            ScmAltaProductoSesion, UUID(graph["session_id"])
        )
        draft = dict(onboarding.borrador_json)
        draft["REVISION"] = {"x": 1}
        onboarding.borrador_json = draft
        states = dict(onboarding.estados_paso_json)
        states["REVISION"] = "COMPLETADO"
        onboarding.estados_paso_json = states
        db.session.commit()

    finalized = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/finalizar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": graph["version"]},
    )
    assert finalized.status_code == 422
    current = finalized.get_json()["error"]["details"]["current_session"]
    assert current["estado"] == "CON_BLOQUEOS"
    assert current["readiness"]["status"] == "BLOCKED"
    assert current["finalizada_at"] is None


def test_readiness_recorre_wip_anidado_y_bloquea_bom_faltante(
    app, client
):
    actor_id = _actor(app)
    graph = _ready_graph(app, actor_id)
    with app.app_context():
        wip_a = create_wip_article(
            db.session,
            actor_id=actor_id,
            data={"nombre": "WIP A READINESS", "requiere_calidad": False},
        )
        wip_b = create_wip_article(
            db.session,
            actor_id=actor_id,
            data={"nombre": "WIP B READINESS", "requiere_calidad": False},
        )
        top = db.session.get(ScmEstructuraRevision, graph["structure_id"])
        for component in list(top.componentes):
            db.session.delete(component)
        db.session.flush()
        db.session.add(ScmEstructuraComponente(
            revision_id=top.id,
            secuencia=1,
            articulo_componente_id=wip_a["id"],
            cantidad=1,
            unidad="UN",
        ))
        nested = ScmEstructuraRevision(
            articulo_resultado_id=wip_a["id"],
            numero_revision=1,
            estado="APROBADA",
            creada_por_id=actor_id,
            aprobada_por_id=actor_id,
            aprobada_at=db.func.now(),
            content_hash="d" * 64,
        )
        db.session.add(nested)
        db.session.flush()
        db.session.add(ScmEstructuraComponente(
            revision_id=nested.id,
            secuencia=1,
            articulo_componente_id=wip_b["id"],
            cantidad=1,
            unidad="UN",
        ))
        db.session.commit()

    response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": graph["version"]},
    )
    assert response.status_code == 200
    readiness = response.get_json()["readiness"]
    assert readiness["status"] == "BLOCKED"
    blockers = [
        item for item in readiness["items"]
        if item["code"] == "WIP_STRUCTURE_NOT_APPROVED"
    ]
    assert any(item["entity"]["id"] == wip_b["id"] for item in blockers)


def test_readiness_respeta_regla_seleccionada_aunque_exista_otra_aprobada(
    app, client
):
    actor_id = _actor(app)
    graph = _ready_graph(app, actor_id)
    with app.app_context():
        draft_rule = ScmReglaEmpaqueRevision(
            regla_id=graph["rule_id"],
            numero_revision=2,
            estado="BORRADOR",
            medicion_fisica_probada=True,
            cantidad_objetivo_un=11,
            cantidad_maxima_probada_un=13,
            peso_neto_operativo_max_kg=Decimal("9"),
            margen_seguridad_kg=Decimal("0.5"),
            tolerancia_peso_abs_g=Decimal("50"),
            tolerancia_peso_pct=Decimal("2"),
            creada_por_id=actor_id,
        )
        db.session.add(draft_rule)
        db.session.flush()
        onboarding = db.session.get(
            ScmAltaProductoSesion, UUID(graph["session_id"])
        )
        refs = dict(onboarding.referencias_json)
        route_refs = dict(refs["RUTA_EMPAQUE"])
        route_refs["empaques"] = [{
            "client_id": "pt-final",
            "articulo_ref": graph["product_article_id"],
            "perfil_empacable_ref": graph["profile_id"],
            "regla_empaque_revision_ref": draft_rule.id,
            "regla_empaque_revision_version": draft_rule.version,
            "estado": "BORRADOR",
        }]
        refs["RUTA_EMPAQUE"] = route_refs
        onboarding.referencias_json = refs
        draft = dict(onboarding.borrador_json)
        revision_data = dict(draft["REVISION"])
        revision_data["revisiones_revisadas"] = [
            {
                **item,
                "id": draft_rule.id,
                "version": draft_rule.version,
                "content_hash": draft_rule.content_hash,
            }
            if item["tipo"] == "REGLA_EMPAQUE" else item
            for item in revision_data["revisiones_revisadas"]
        ]
        draft["REVISION"] = revision_data
        onboarding.borrador_json = draft
        db.session.commit()
        draft_rule_id = draft_rule.id

    response = client.post(
        f"/api/scm/v1/altas-producto/{graph['session_id']}/validar",
        headers=_headers(actor_id, uuid4()),
        json={"expected_version": graph["version"]},
    )
    assert response.status_code == 200
    readiness = response.get_json()["readiness"]
    assert readiness["status"] == "PENDING_APPROVAL"
    assert any(
        item["code"] == "PACKAGING_RULE_NOT_APPROVED"
        and item["entity"]["id"] == draft_rule_id
        for item in readiness["items"]
    )
