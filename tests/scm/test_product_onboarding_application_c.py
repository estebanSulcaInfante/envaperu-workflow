import copy
import io
from decimal import Decimal
from uuid import UUID, uuid4

from PIL import Image

from app.extensions import db
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    FamiliaColor,
    PiezaColor,
    ProductoTerminado,
)
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.receta_color import RecetaColorMaestra
from app.models.scm_articulos import (
    ScmArticulo,
    ScmArticuloPiezaColor,
    ScmArticuloProducto,
)
from app.models.scm_auditoria import ScmEvento, ScmOperacion
from app.models.scm_catalogos import ScmCapacidad
from app.models.scm_empaque import (
    ScmArticuloPerfil,
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_estructuras import ScmEstructuraRevision
from app.models.scm_product_onboarding import ScmAltaProductoSesion
from app.models.scm_rutas import ScmCentroTrabajo, ScmRutaRevision
from app.models.trabajador import RolOperativo, Trabajador
from app.services.catalog_image_storage import CatalogImageStorage


C_CAPABILITIES = {
    "ARTICULO_ADMINISTRAR",
    "ESTRUCTURA_VER",
    "ESTRUCTURA_ADMINISTRAR",
    "ESTRUCTURA_PUBLICAR_DIRECTO",
    "RUTA_VER",
    "RUTA_ADMINISTRAR",
    "RUTA_PUBLICAR_DIRECTO",
    "EMPAQUE_VER",
    "EMPAQUE_ADMINISTRAR",
    "EMPAQUE_PUBLICAR_DIRECTO",
}


def _png_bytes(color=(24, 96, 160)):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color=color).save(output, format="PNG")
    return output.getvalue()


class _FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.put_count = 0

    def put_object(self, **kwargs):
        self.put_count += 1
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = {
            "Body": kwargs["Body"],
            "ContentType": kwargs["ContentType"],
            "Metadata": kwargs["Metadata"],
        }

    def delete_object(self, **kwargs):
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


def _headers(actor_id, operation_id):
    return {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": str(operation_id),
    }


def _seed_c_session(app, scm_config):
    del scm_config
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        roles = [
            RolOperativo.query.filter_by(codigo=code).one()
            for code in ("INGENIERIA_SCM", "JEFE_PRODUCCION")
        ]
        for role in roles:
            if role not in actor.roles:
                actor.roles.append(role)
        assert C_CAPABILITIES <= {
            capability.codigo
            for actor_role in actor.roles
            for capability in actor_role.capacidades
        }

        product = ProductoTerminado(
            cod_sku_pt="PT-ALTA-C-001",
            producto="PT ALTA C",
            linea_id=1,
            familia_id=1,
            um="UN",
            status="ACTIVO",
        )
        piece = Pieza(
            codigo="PZ-ALTA-C-001",
            nombre="PIEZA ALTA C",
            peso_nominal_gr=Decimal("90"),
            activo=True,
        )
        family = FamiliaColor(
            codigo=9960,
            nombre="FAMILIA ALTA C",
            activo=True,
        )
        base = ColorBase(nombre="COLOR ALTA C")
        db.session.add_all([product, piece, family, base])
        db.session.flush()
        color = ColorProduccion(
            color_base_id=base.id,
            familia_color_id=family.id,
            hex_referencia="#234567",
            activo=True,
        )
        db.session.add(color)
        db.session.flush()
        variant = PiezaColor(
            sku="PC-ALTA-C-001",
            piezas="PIEZA COLOR ALTA C",
            pieza_id=piece.id,
            color_produccion_id=color.id,
            peso=Decimal("90"),
            estado_revision="VERIFICADO",
        )
        db.session.add(variant)
        db.session.flush()
        mold = Molde(
            codigo="ML-ALTA-C-001",
            nombre="MOLDE ALTA C",
            peso_tiro_gr=Decimal("100"),
            tiempo_ciclo_std=Decimal("30"),
            activo=True,
        )
        composition = MoldePieza(
            molde_id=mold.codigo,
            pieza_id=piece.id,
            cavidades=1,
            peso_unitario_gr=Decimal("90"),
            activo=True,
        )
        recipe = RecetaColorMaestra(
            color_produccion_id=color.id,
            producto_sku=product.cod_sku_pt,
            producto_scope=product.cod_sku_pt,
            nombre_variante="ALTA C",
            revision=1,
            estado="APROBADA",
            es_default=True,
            base_virgen_kg=Decimal("25"),
            origen="TEST",
        )
        db.session.add_all([mold, composition, recipe])
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
        center = ScmCentroTrabajo(
            codigo="CT-ALTA-C-001",
            nombre="CENTRO ALTA C",
            tipo="ENSAMBLE",
            activo=True,
        )
        fabrication_center = ScmCentroTrabajo(
            codigo="CT-ALTA-C-FAB",
            nombre="CENTRO FAB ALTA C",
            tipo="INYECCION",
            activo=True,
        )
        container = ScmTipoContenedor(
            codigo="CONT-ALTA-C-001",
            clase="MANGA",
            nombre="MANGA ALTA C",
            tara_nominal_g=Decimal("100"),
            tolerancia_tara_g=Decimal("10"),
            peso_bruto_max_kg=Decimal("10"),
            activo=True,
        )
        db.session.add_all([center, fabrication_center, container])
        db.session.flush()
        prior_refs = {
            "IDENTIDAD": {
                "producto_terminado_id": product.cod_sku_pt,
                "producto_ref": product.cod_sku_pt,
            },
            "COMPONENTES": {
                "molde_ref": mold.codigo,
                "piezas": [{
                    "client_id": "pieza-c",
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
        }
        journal = {
            code: {
                f"seed-{code.lower()}": {
                    "status": "APPLIED",
                    "result": {
                        "resolved_references": prior_refs[code],
                    },
                },
            }
            for code in ("IDENTIDAD", "COMPONENTES", "COLORES")
        }
        onboarding = ScmAltaProductoSesion(
            titulo="PT ALTA C",
            producto_terminado_id=product.cod_sku_pt,
            creada_por_id=actor.id,
            actualizada_por_id=actor.id,
            paso_actual="ESTRUCTURA",
            borrador_json={
                "IDENTIDAD": {"capturado": True},
                "COMPONENTES": {"capturado": True},
                "COLORES": {"capturado": True},
            },
            estados_paso_json={
                "IDENTIDAD": "COMPLETADO",
                "COMPONENTES": "COMPLETADO",
                "COLORES": "COMPLETADO",
            },
            referencias_json=prior_refs,
            application_journal_json=journal,
        )
        db.session.add(onboarding)
        db.session.commit()
        return {
            "actor_id": actor.id,
            "session_id": str(onboarding.id),
            "version": onboarding.version,
            "product_id": product.cod_sku_pt,
            "product_article_id": product_article.id,
            "piece_article_id": piece_article.id,
            "variant_id": variant.sku,
            "center_id": center.id,
            "fabrication_center_id": fabrication_center.id,
            "container_id": container.id,
        }


def _apply_structure(client, seeded):
    response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/ESTRUCTURA/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json={
            "expected_version": seeded["version"],
            "application_key": "estructura-c-v1",
            "data": {
                "target_article_ref": seeded["product_article_id"],
                "estructura": {
                    "modo": "NUEVA",
                    "accion": "GUARDAR_BORRADOR",
                    "payload": {
                        "notas": "Estructura C",
                        "componentes": [{
                            "secuencia": 1,
                            "articulo_id": seeded["piece_article_id"],
                            "cantidad": 1,
                            "unidad": "UN",
                            "merma_tecnica_pct": 0,
                        }],
                    },
                },
            },
        },
    )
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result["pasos"][3]["estado"] == "COMPLETADO"
    assert result["application_results"]["pending"][0]["type"] == (
        "ESTRUCTURA"
    )
    return result


def _route_packaging_payload(seeded, version, *, container_id):
    return {
        "expected_version": version,
        "application_key": "ruta-empaque-c-v1",
        "data": {
            "target_product_ref": seeded["product_id"],
            "target_article_ref": seeded["product_article_id"],
            "ruta": {
                "modo": "NUEVA",
                "accion": "GUARDAR_BORRADOR",
                "payload": {
                    "notas": "Ruta C",
                    "operaciones": [{
                        "clave": "ENSAMBLAR",
                        "secuencia_visible": 1,
                        "nombre": "Ensamblar PT",
                        "tipo": "ENSAMBLE",
                        "executor_kind": "OP_OT",
                        "centro_trabajo_id": seeded["center_id"],
                        "articulo_salida_id": seeded[
                            "product_article_id"
                        ],
                        "permite_concurrente": False,
                    }],
                    "precedencias": [],
                },
            },
            "empaques": [{
                "client_id": "pt-final",
                "articulo_ref": seeded["product_article_id"],
                "perfil_empacable": {
                    "modo": "NUEVO",
                    "payload": {
                        "nombre": "PERFIL PT ALTA C",
                        "descripcion_fisica": "Perfil para prueba C",
                    },
                    "asignar_predeterminado": True,
                },
                "regla_empaque": {
                    "modo": "NUEVA",
                    "accion": "GUARDAR_BORRADOR",
                    "payload": {
                        "tipo_contenedor_id": container_id,
                        "medicion_fisica_probada": True,
                        "cantidad_objetivo_un": 10,
                        "cantidad_maxima_probada_un": 12,
                        "peso_neto_operativo_max_kg": 9,
                        "margen_seguridad_kg": 0.5,
                        "tolerancia_peso_abs_g": 50,
                        "tolerancia_peso_pct": 2,
                        "notas": "Regla C",
                    },
                },
            }],
        },
    }


def test_aplicar_c_rollback_total_y_retry_no_deja_operacion_incompleta(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    structured = _apply_structure(client, seeded)
    operation_id = uuid4()
    invalid_payload = _route_packaging_payload(
        seeded,
        structured["version"],
        container_id=999999,
    )

    failed = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/RUTA_EMPAQUE/aplicar",
        headers=_headers(seeded["actor_id"], operation_id),
        json=invalid_payload,
    )

    assert failed.status_code == 404, failed.get_json()
    assert failed.get_json()["error"]["code"] == "CONTAINER_TYPE_NOT_FOUND"
    with app.app_context():
        assert ScmRutaRevision.query.count() == 0
        assert ScmPerfilEmpacable.query.count() == 0
        assert ScmArticuloPerfil.query.count() == 0
        assert ScmReglaEmpaque.query.count() == 0
        assert ScmReglaEmpaqueRevision.query.count() == 0
        assert db.session.get(ScmOperacion, operation_id) is None
        assert ScmEvento.query.filter(
            ScmEvento.tipo.in_((
                "ROUTE_CREATED",
                "PACKABLE_PROFILE_CREATED",
                "ARTICLE_PACKAGING_PROFILES_ASSIGNED",
                "PACKAGING_RULE_CREATED",
            ))
        ).count() == 0

    invalid_payload["data"]["empaques"][0]["regla_empaque"][
        "payload"
    ]["tipo_contenedor_id"] = seeded["container_id"]
    succeeded = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/RUTA_EMPAQUE/aplicar",
        headers=_headers(seeded["actor_id"], operation_id),
        json=invalid_payload,
    )

    assert succeeded.status_code == 200, succeeded.get_json()
    body = succeeded.get_json()
    assert body["pasos"][4]["estado"] == "COMPLETADO"
    refs = body["application_results"]["resolved_references"]
    assert len(refs["empaques"]) == 1
    assert refs["empaques"][0]["articulo_ref"] == seeded[
        "product_article_id"
    ]
    with app.app_context():
        assert ScmRutaRevision.query.count() == 1
        assert ScmPerfilEmpacable.query.count() == 1
        assert ScmArticuloPerfil.query.count() == 1
        assert ScmReglaEmpaque.query.count() == 1
        assert ScmReglaEmpaqueRevision.query.count() == 1
        operation = db.session.get(ScmOperacion, operation_id)
        assert operation is not None
        assert operation.estado_http == 200
        assert operation.response_json is not None


def test_aplicar_ruta_empaque_exige_cobertura_exacta_de_salidas(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    structured = _apply_structure(client, seeded)
    payload = _route_packaging_payload(
        seeded,
        structured["version"],
        container_id=seeded["container_id"],
    )
    payload["data"]["empaques"] = []

    response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/RUTA_EMPAQUE/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json=payload,
    )

    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == (
        "PACKAGING_OUTPUTS_REQUIRED"
    )
    with app.app_context():
        assert ScmRutaRevision.query.count() == 0
        assert ScmPerfilEmpacable.query.count() == 0
        assert ScmArticuloPerfil.query.count() == 0


def test_aplicar_estructura_crea_wip_contextual_en_la_misma_uow(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)

    response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/ESTRUCTURA/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json={
            "expected_version": seeded["version"],
            "application_key": "estructura-wip-c-v1",
            "data": {
                "target_article_ref": seeded["product_article_id"],
                "wips_nuevos": [{
                    "client_id": "prearmado",
                    "nombre": "PREARMADO ALTA C",
                    "descripcion": "WIP creado desde el alta",
                    "requiere_calidad": False,
                }],
                "estructura": {
                    "modo": "NUEVA",
                    "accion": "GUARDAR_BORRADOR",
                    "payload": {
                        "notas": "PT consume WIP",
                        "componentes": [{
                            "secuencia": 1,
                            "articulo_client_id": "prearmado",
                            "cantidad": 1,
                            "unidad": "UN",
                            "merma_tecnica_pct": 0,
                        }],
                    },
                },
            },
        },
    )

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    refs = body["application_results"]["resolved_references"]
    assert refs["wips"][0]["client_id"] == "prearmado"
    wip_id = refs["wips"][0]["articulo_ref"]
    structure_id = refs["estructura_revision_ref"]
    with app.app_context():
        wip = db.session.get(ScmArticulo, wip_id)
        structure = db.session.get(ScmEstructuraRevision, structure_id)
        assert wip.clase == "SUBENSAMBLE_WIP"
        assert structure.componentes[0].articulo_componente_id == wip_id
        assert ScmArticulo.query.filter_by(
            nombre="PREARMADO ALTA C"
        ).count() == 1


def test_aplicar_ruta_empaque_publica_dos_salidas_sin_colision_idempotente(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    structured = _apply_structure(client, seeded)
    payload = _route_packaging_payload(
        seeded,
        structured["version"],
        container_id=seeded["container_id"],
    )
    route = payload["data"]["ruta"]
    route["accion"] = "PUBLICAR"
    route["payload"]["operaciones"] = [
        {
            "clave": "FABRICAR",
            "secuencia_visible": 1,
            "nombre": "Fabricar pieza",
            "tipo": "INYECCION",
            "executor_kind": "OP_OT",
            "centro_trabajo_id": seeded["fabrication_center_id"],
            "articulo_salida_id": seeded["piece_article_id"],
            "permite_concurrente": False,
        },
        {
            "clave": "ENSAMBLAR",
            "secuencia_visible": 2,
            "nombre": "Ensamblar PT",
            "tipo": "ENSAMBLE",
            "executor_kind": "OP_OT",
            "centro_trabajo_id": seeded["center_id"],
            "articulo_salida_id": seeded["product_article_id"],
            "permite_concurrente": False,
        },
    ]
    route["payload"]["precedencias"] = [{
        "anterior_clave": "FABRICAR",
        "siguiente_clave": "ENSAMBLAR",
    }]
    final_packaging = payload["data"]["empaques"][0]
    final_packaging["regla_empaque"]["accion"] = "PUBLICAR"
    piece_packaging = copy.deepcopy(final_packaging)
    piece_packaging["client_id"] = "pieza-fabricada"
    piece_packaging["articulo_ref"] = seeded["piece_article_id"]
    piece_packaging["perfil_empacable"]["payload"]["nombre"] = (
        "PERFIL PIEZA ALTA C"
    )
    payload["data"]["empaques"] = [piece_packaging, final_packaging]
    operation_id = uuid4()

    response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/RUTA_EMPAQUE/aplicar",
        headers=_headers(seeded["actor_id"], operation_id),
        json=payload,
    )

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["application_results"]["pending"] == []
    refs = body["application_results"]["resolved_references"]
    assert {
        item["articulo_ref"] for item in refs["empaques"]
    } == {seeded["piece_article_id"], seeded["product_article_id"]}
    replay = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/RUTA_EMPAQUE/aplicar",
        headers=_headers(seeded["actor_id"], operation_id),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.get_json() == body
    with app.app_context():
        assert ScmRutaRevision.query.filter_by(estado="APROBADA").count() == 1
        assert ScmPerfilEmpacable.query.count() == 2
        assert ScmReglaEmpaqueRevision.query.filter_by(
            estado="APROBADA"
        ).count() == 2
        assert ScmArticuloPerfil.query.count() == 2


def test_aplicar_empaque_preserva_perfiles_alternativos_existentes(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    structured = _apply_structure(client, seeded)
    with app.app_context():
        alternate = ScmPerfilEmpacable(
            codigo="PEM-ALTERNATIVO-C",
            nombre="PERFIL ALTERNATIVO C",
            activo=True,
        )
        db.session.add(alternate)
        db.session.flush()
        db.session.add(ScmArticuloPerfil(
            articulo_id=seeded["product_article_id"],
            perfil_empacable_id=alternate.id,
            activo=True,
            es_predeterminado=True,
        ))
        db.session.commit()
        alternate_id = alternate.id

    payload = _route_packaging_payload(
        seeded,
        structured["version"],
        container_id=seeded["container_id"],
    )
    response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/RUTA_EMPAQUE/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json=payload,
    )
    assert response.status_code == 200, response.get_json()
    selected_id = response.get_json()["application_results"][
        "resolved_references"
    ]["empaques"][0]["perfil_empacable_ref"]
    with app.app_context():
        links = ScmArticuloPerfil.query.filter_by(
            articulo_id=seeded["product_article_id"]
        ).all()
        assert {link.perfil_empacable_id for link in links} == {
            alternate_id,
            selected_id,
        }
        alternate_link = next(
            link for link in links
            if link.perfil_empacable_id == alternate_id
        )
        selected_link = next(
            link for link in links
            if link.perfil_empacable_id == selected_id
        )
        assert alternate_link.activo is True
        assert alternate_link.es_predeterminado is False
        assert selected_link.activo is True
        assert selected_link.es_predeterminado is True


def test_backtracking_c_exige_supersede_y_edita_misma_estructura(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    applied = _apply_structure(client, seeded)
    refs = applied["application_results"]["resolved_references"]
    structure_id = refs["estructura_revision_ref"]
    previous_key = applied["pasos"][3]["application_status"][
        "application_key"
    ]
    payload = {
        "expected_version": applied["version"],
        "application_key": "estructura-c-v2",
        "supersedes_application_key": previous_key,
        "data": {
            "target_article_ref": seeded["product_article_id"],
            "estructura": {
                "modo": "EDITAR",
                "revision_ref": structure_id,
                "expected_version": refs["estructura_revision_version"],
                "accion": "GUARDAR_BORRADOR",
                "payload": {
                    "notas": "Estructura C corregida",
                    "componentes": [{
                        "secuencia": 1,
                        "articulo_id": seeded["piece_article_id"],
                        "cantidad": 2,
                        "unidad": "UN",
                        "merma_tecnica_pct": 0,
                    }],
                },
            },
        },
    }

    response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/ESTRUCTURA/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json=payload,
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    new_refs = body["application_results"]["resolved_references"]
    assert new_refs["estructura_revision_ref"] == structure_id
    assert new_refs["estructura_revision_version"] > refs[
        "estructura_revision_version"
    ]
    assert body["pasos"][3]["application_status"][
        "application_key"
    ] == "estructura-c-v2"
    with app.app_context():
        assert ScmEstructuraRevision.query.count() == 1
        structure = db.session.get(ScmEstructuraRevision, structure_id)
        assert structure.notas == "Estructura C corregida"
        assert structure.componentes[0].cantidad == 2
        onboarding = ScmAltaProductoSesion.query.one()
        previous = onboarding.application_journal_json["ESTRUCTURA"][
            previous_key
        ]
        assert previous["superseded_by"] == "estructura-c-v2"

    payload["expected_version"] = body["version"]
    payload["application_key"] = "estructura-c-v3"
    payload["supersedes_application_key"] = previous_key
    rejected = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/ESTRUCTURA/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json=payload,
    )
    assert rejected.status_code == 409
    assert rejected.get_json()["error"]["code"] == (
        "SUPERSEDED_APPLICATION_NOT_CURRENT"
    )


def test_aplicar_estructura_rechaza_actor_solo_articulos_sin_escrituras(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    with app.app_context():
        role = RolOperativo(
            codigo="ALTA_C_SOLO_ARTICULOS",
            nombre="Alta C solo articulos",
            activo=True,
        )
        role.capacidades.append(ScmCapacidad.query.filter_by(
            codigo="ARTICULO_ADMINISTRAR"
        ).one())
        actor = Trabajador(
            codigo="TRB-ALTA-C-LIMITADO",
            nombres="Actor",
            apellidos="Limitado",
            activo=True,
            roles=[role],
        )
        db.session.add_all([role, actor])
        db.session.flush()
        onboarding = ScmAltaProductoSesion.query.one()
        onboarding.creada_por_id = actor.id
        onboarding.actualizada_por_id = actor.id
        db.session.commit()
        actor_id = actor.id

    response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/ESTRUCTURA/aplicar",
        headers=_headers(actor_id, uuid4()),
        json={
            "expected_version": seeded["version"],
            "application_key": "estructura-limitada-v1",
            "data": {
                "target_article_ref": seeded["product_article_id"],
                "estructura": {
                    "modo": "NUEVA",
                    "accion": "GUARDAR_BORRADOR",
                    "payload": {
                        "componentes": [{
                            "articulo_id": seeded["piece_article_id"],
                            "cantidad": 1,
                            "unidad": "UN",
                        }],
                    },
                },
            },
        },
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"
    with app.app_context():
        assert ScmEstructuraRevision.query.count() == 0
        assert ScmOperacion.query.count() == 0


def test_aplicar_c_rechaza_estructura_y_ruta_de_otro_producto(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    with app.app_context():
        foreign_product = ProductoTerminado(
            cod_sku_pt="PT-ALTA-C-FOREIGN",
            producto="PT ALTA C FOREIGN",
            linea_id=1,
            familia_id=1,
            um="UN",
        )
        db.session.add(foreign_product)
        db.session.flush()
        foreign_article = db.session.scalar(
            db.select(ScmArticulo)
            .join(ScmArticuloProducto)
            .where(
                ScmArticuloProducto.producto_terminado_id
                == foreign_product.cod_sku_pt
            )
        )
        foreign_structure = ScmEstructuraRevision(
            articulo_resultado_id=foreign_article.id,
            numero_revision=1,
            estado="BORRADOR",
            notas="No tocar",
            creada_por_id=seeded["actor_id"],
        )
        db.session.add(foreign_structure)
        db.session.flush()
        foreign_article_id = foreign_article.id
        db.session.commit()
        foreign_structure_id = foreign_structure.id
        foreign_structure_version = foreign_structure.version

    structure_response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/ESTRUCTURA/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json={
            "expected_version": seeded["version"],
            "application_key": "foreign-structure-v1",
            "data": {
                "target_article_ref": seeded["product_article_id"],
                "estructura": {
                    "modo": "EDITAR",
                    "revision_ref": foreign_structure_id,
                    "expected_version": foreign_structure_version,
                    "accion": "GUARDAR_BORRADOR",
                    "payload": {
                        "notas": "Mutacion prohibida",
                        "componentes": [{
                            "articulo_id": seeded["piece_article_id"],
                            "cantidad": 1,
                            "unidad": "UN",
                        }],
                    },
                },
            },
        },
    )
    assert structure_response.status_code == 422
    assert structure_response.get_json()["error"]["code"] == (
        "STRUCTURE_TARGET_OUT_OF_SCOPE"
    )
    with app.app_context():
        foreign_structure = db.session.get(
            ScmEstructuraRevision, foreign_structure_id
        )
        assert foreign_structure.version == foreign_structure_version
        assert foreign_structure.notas == "No tocar"

    structured = _apply_structure(client, seeded)
    with app.app_context():
        foreign_route = ScmRutaRevision(
            articulo_objetivo_id=foreign_article_id,
            numero_revision=1,
            estado="BORRADOR",
            creada_por_id=seeded["actor_id"],
        )
        db.session.add(foreign_route)
        db.session.commit()
        foreign_route_id = foreign_route.id
        foreign_route_version = foreign_route.version
    route_response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/RUTA_EMPAQUE/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json={
            "expected_version": structured["version"],
            "application_key": "foreign-route-v1",
            "data": {
                "target_product_ref": seeded["product_id"],
                "target_article_ref": seeded["product_article_id"],
                "ruta": {
                    "modo": "REUTILIZAR",
                    "revision_ref": foreign_route_id,
                    "accion": "VINCULAR",
                },
                "empaques": [],
            },
        },
    )
    assert route_response.status_code == 422
    assert route_response.get_json()["error"]["code"] == (
        "ROUTE_TARGET_OUT_OF_SCOPE"
    )
    with app.app_context():
        foreign_route = db.session.get(ScmRutaRevision, foreign_route_id)
        assert foreign_route.version == foreign_route_version


def test_aplicar_empaque_rechaza_regla_de_otro_perfil_sin_mutar(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    structured = _apply_structure(client, seeded)
    with app.app_context():
        selected_profile = ScmPerfilEmpacable(
            codigo="PEM-SCOPE-C-A",
            nombre="PERFIL SCOPE A",
            activo=True,
        )
        foreign_profile = ScmPerfilEmpacable(
            codigo="PEM-SCOPE-C-B",
            nombre="PERFIL SCOPE B",
            activo=True,
        )
        db.session.add_all([selected_profile, foreign_profile])
        db.session.flush()
        foreign_rule = ScmReglaEmpaque(
            perfil_empacable_id=foreign_profile.id,
            tipo_contenedor_id=seeded["container_id"],
        )
        db.session.add(foreign_rule)
        db.session.flush()
        foreign_revision = ScmReglaEmpaqueRevision(
            regla_id=foreign_rule.id,
            numero_revision=1,
            estado="BORRADOR",
            medicion_fisica_probada=True,
            cantidad_objetivo_un=10,
            cantidad_maxima_probada_un=12,
            peso_neto_operativo_max_kg=Decimal("9"),
            margen_seguridad_kg=Decimal("0.5"),
            tolerancia_peso_abs_g=Decimal("50"),
            tolerancia_peso_pct=Decimal("2"),
            creada_por_id=seeded["actor_id"],
        )
        db.session.add(foreign_revision)
        db.session.commit()
        selected_profile_id = selected_profile.id
        foreign_revision_id = foreign_revision.id
        foreign_version = foreign_revision.version

    payload = _route_packaging_payload(
        seeded,
        structured["version"],
        container_id=seeded["container_id"],
    )
    packaging = payload["data"]["empaques"][0]
    packaging["perfil_empacable"] = {
        "modo": "REUTILIZAR",
        "ref": selected_profile_id,
        "asignar_predeterminado": True,
    }
    packaging["regla_empaque"] = {
        "modo": "REUTILIZAR",
        "revision_ref": foreign_revision_id,
        "accion": "VINCULAR",
    }
    response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/RUTA_EMPAQUE/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json=payload,
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == (
        "PACKAGING_RULE_PROFILE_CONFLICT"
    )
    with app.app_context():
        assert ScmRutaRevision.query.count() == 0
        assert ScmArticuloPerfil.query.count() == 0
        revision = db.session.get(
            ScmReglaEmpaqueRevision, foreign_revision_id
        )
        assert revision.version == foreign_version


def test_e2e_aplicar_c_validar_pending_y_finalizar_captura_sin_op(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    structured = _apply_structure(client, seeded)
    route_response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/RUTA_EMPAQUE/aplicar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json=_route_packaging_payload(
            seeded,
            structured["version"],
            container_id=seeded["container_id"],
        ),
    )
    assert route_response.status_code == 200, route_response.get_json()
    routed = route_response.get_json()
    revision_response = client.put(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        "/pasos/REVISION",
        headers=_headers(seeded["actor_id"], uuid4()),
        json={
            "expected_version": routed["version"],
            "estado_paso": "COMPLETADO",
            "data": {
                "confirmaciones": {
                    "datos_fuente_revisados": True,
                    "entiende_que_no_crea_op": True,
                    "pendientes_aceptados": True,
                },
                "pasos_revisados": [
                    "IDENTIDAD",
                    "COMPONENTES",
                    "COLORES",
                    "ESTRUCTURA",
                    "RUTA_EMPAQUE",
                ],
                "revisiones_revisadas": routed["readiness"][
                    "revision_snapshot"
                ],
            },
        },
    )
    assert revision_response.status_code == 200, revision_response.get_json()
    revised = revision_response.get_json()
    validated_response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}/validar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json={"expected_version": revised["version"]},
    )
    assert validated_response.status_code == 200
    validated = validated_response.get_json()
    assert validated["readiness"]["status"] == "PENDING_APPROVAL"
    assert validated["readiness"]["lista_para_finalizar"] is True
    assert {
        item["code"] for item in validated["readiness"]["items"]
        if item["result"] == "PENDING_APPROVAL"
    } >= {
        "STRUCTURE_NOT_APPROVED",
        "ROUTE_NOT_APPROVED",
        "PACKAGING_RULE_NOT_APPROVED",
    }
    with app.app_context():
        from app.models.orden import OrdenProduccion
        before_orders = OrdenProduccion.query.count()
    finalized_response = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}/finalizar",
        headers=_headers(seeded["actor_id"], uuid4()),
        json={"expected_version": validated["version"]},
    )
    assert finalized_response.status_code == 200, finalized_response.get_json()
    assert finalized_response.get_json()["estado"] == "FINALIZADA"
    with app.app_context():
        from app.models.orden import OrdenProduccion
        assert OrdenProduccion.query.count() == before_orders


def _upload_image(
    client,
    seeded,
    *,
    entity_type,
    entity_id,
    version,
    application_key,
    operation_id,
    content,
    filename="foto.png",
    mime="image/png",
):
    return client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        f"/imagenes/{entity_type}/{entity_id}",
        headers=_headers(seeded["actor_id"], operation_id),
        data={
            "expected_version": str(version),
            "application_key": application_key,
            "imagen": (io.BytesIO(content), filename, mime),
        },
        content_type="multipart/form-data",
    )


def test_imagenes_sesion_pt_y_pieza_color_son_idempotentes_y_reanudables(
    app, client, scm_config
):
    seeded = _seed_c_session(app, scm_config)
    png = _png_bytes()
    operation_id = uuid4()
    product_response = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=seeded["version"],
        application_key="imagen-pt-c-v1",
        operation_id=operation_id,
        content=png,
    )
    assert product_response.status_code == 200, product_response.get_json()
    product_body = product_response.get_json()
    assert product_body["image_results"]["status"] == "APPLIED"
    assert product_body["image_results"]["entity_type"] == (
        "PRODUCTO_TERMINADO"
    )
    assert len(product_body["imagenes"]) == 1

    replay = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=seeded["version"],
        application_key="imagen-pt-c-v1",
        operation_id=operation_id,
        content=png,
    )
    assert replay.status_code == 200
    assert replay.get_json() == product_body

    application_replay = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=product_body["version"],
        application_key="imagen-pt-c-v1",
        operation_id=uuid4(),
        content=png,
    )
    assert application_replay.status_code == 200
    assert application_replay.get_json()["image_results"]["status"] == (
        "REPLAYED"
    )

    piece_response = _upload_image(
        client,
        seeded,
        entity_type="PIEZA_COLOR",
        entity_id=seeded["variant_id"],
        version=product_body["version"],
        application_key="imagen-pc-c-v1",
        operation_id=uuid4(),
        content=_png_bytes(color=(160, 96, 24)),
    )
    assert piece_response.status_code == 200, piece_response.get_json()
    piece_body = piece_response.get_json()
    assert len(piece_body["imagenes"]) == 2
    assert {
        row["entity_type"] for row in piece_body["imagenes"]
    } == {"PRODUCTO_TERMINADO", "PIEZA_COLOR"}
    fetched = client.get(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}",
        headers={"X-Actor-Id": str(seeded["actor_id"])},
    )
    assert fetched.status_code == 200
    assert fetched.get_json()["imagenes"] == piece_body["imagenes"]
    with app.app_context():
        product = db.session.get(ProductoTerminado, seeded["product_id"])
        variant = db.session.get(PiezaColor, seeded["variant_id"])
        assert product.imagen_data == png
        assert variant.imagen_data == _png_bytes(color=(160, 96, 24))
        assert ScmEvento.query.filter_by(
            tipo="IMAGEN_ALTA_PRODUCTO_ASOCIADA"
        ).count() == 2


def test_imagen_s3_reemplazo_fallido_no_sobrescribe_ni_deja_huerfano(
    app, client, scm_config, monkeypatch
):
    seeded = _seed_c_session(app, scm_config)
    fake_s3 = _FakeS3Client()
    storage = CatalogImageStorage(
        {
            "CATALOG_IMAGE_STORAGE": "supabase_s3",
            "CATALOG_IMAGE_KEEP_DATABASE_COPY": False,
            "SUPABASE_S3_ENDPOINT": "https://storage.example.test/s3",
            "SUPABASE_S3_REGION": "us-east-1",
            "SUPABASE_S3_ACCESS_KEY_ID": "test-key",
            "SUPABASE_S3_SECRET_ACCESS_KEY": "test-secret",
            "SUPABASE_STORAGE_BUCKET": "catalog-images",
        },
        s3_client=fake_s3,
    )
    app.extensions["catalog_image_storage"] = storage
    original = _png_bytes()
    first_response = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=seeded["version"],
        application_key="imagen-pt-s3-v1",
        operation_id=uuid4(),
        content=original,
    )
    assert first_response.status_code == 200, first_response.get_json()
    first = first_response.get_json()
    with app.app_context():
        product = db.session.get(ProductoTerminado, seeded["product_id"])
        old_key = product.imagen_storage_key
        old_sha = product.imagen_sha256
    assert old_key
    assert fake_s3.put_count == 1
    assert ("catalog-images", old_key) in fake_s3.objects

    replay = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=first["version"],
        application_key="imagen-pt-s3-v1",
        operation_id=uuid4(),
        content=original,
    )
    assert replay.status_code == 200
    assert replay.get_json()["image_results"]["status"] == "REPLAYED"
    assert fake_s3.put_count == 1

    def fail_commit():
        raise RuntimeError("database commit failed")

    monkeypatch.setattr(db.session, "commit", fail_commit)
    replacement = _png_bytes(color=(160, 96, 24))
    failed_response = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=first["version"],
        application_key="imagen-pt-s3-v2",
        operation_id=uuid4(),
        content=replacement,
    )
    assert failed_response.status_code == 503
    assert failed_response.get_json()["error"]["code"] == (
        "ONBOARDING_IMAGE_CONFLICT"
    )
    assert fake_s3.put_count == 2
    assert list(fake_s3.objects) == [("catalog-images", old_key)]
    with app.app_context():
        db.session.expire_all()
        product = db.session.get(ProductoTerminado, seeded["product_id"])
        onboarding = db.session.get(
            ScmAltaProductoSesion,
            UUID(seeded["session_id"]),
        )
        assert product.imagen_storage_key == old_key
        assert product.imagen_sha256 == old_sha
        assert len(
            onboarding.application_journal_json["IMAGENES"]
        ) == 1


def test_imagen_sesion_rechaza_scope_mime_y_tamano(app, client, scm_config):
    seeded = _seed_c_session(app, scm_config)
    with app.app_context():
        foreign = ProductoTerminado(
            cod_sku_pt="PT-IMAGE-FOREIGN",
            producto="PT IMAGE FOREIGN",
            linea_id=1,
            familia_id=1,
            um="UN",
        )
        db.session.add(foreign)
        db.session.commit()
    png = _png_bytes()
    foreign_response = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id="PT-IMAGE-FOREIGN",
        version=seeded["version"],
        application_key="imagen-foreign-v1",
        operation_id=uuid4(),
        content=png,
    )
    assert foreign_response.status_code == 404
    assert foreign_response.get_json()["error"]["code"] == (
        "IMAGE_ENTITY_OUT_OF_SCOPE"
    )
    invalid_content = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=seeded["version"],
        application_key="imagen-invalid-v1",
        operation_id=uuid4(),
        content=b"no-es-png",
    )
    assert invalid_content.status_code == 415
    assert invalid_content.get_json()["error"]["code"] == (
        "IMAGEN_CONTENIDO_INVALIDO"
    )
    truncated_content = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=seeded["version"],
        application_key="imagen-truncated-v1",
        operation_id=uuid4(),
        content=b"\x89PNG\r\n\x1a\n",
    )
    assert truncated_content.status_code == 415
    assert truncated_content.get_json()["error"]["code"] == (
        "IMAGEN_CONTENIDO_INVALIDO"
    )
    polyglot_content = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=seeded["version"],
        application_key="imagen-polyglot-v1",
        operation_id=uuid4(),
        content=_png_bytes() + b"<script>polyglot</script>",
    )
    assert polyglot_content.status_code == 415
    assert polyglot_content.get_json()["error"]["code"] == (
        "IMAGEN_CONTENIDO_INVALIDO"
    )
    too_large = _upload_image(
        client,
        seeded,
        entity_type="PRODUCTO_TERMINADO",
        entity_id=seeded["product_id"],
        version=seeded["version"],
        application_key="imagen-large-v1",
        operation_id=uuid4(),
        content=b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024 * 1024),
    )
    assert too_large.status_code == 413
    assert too_large.get_json()["error"]["code"] == (
        "IMAGEN_DEMASIADO_GRANDE"
    )
    with app.app_context():
        role = RolOperativo(
            codigo="ALTA_C_IMAGE_FOREIGN",
            nombre="Imagen foreign",
            activo=True,
        )
        role.capacidades.append(ScmCapacidad.query.filter_by(
            codigo="ARTICULO_ADMINISTRAR"
        ).one())
        foreign_actor = Trabajador(
            codigo="TRB-IMAGE-FOREIGN",
            nombres="Imagen",
            apellidos="Foreign",
            activo=True,
            roles=[role],
        )
        db.session.add_all([role, foreign_actor])
        db.session.commit()
        foreign_actor_id = foreign_actor.id
    foreign_owner = client.post(
        f"/api/scm/v1/altas-producto/{seeded['session_id']}"
        f"/imagenes/PRODUCTO_TERMINADO/{seeded['product_id']}",
        headers=_headers(foreign_actor_id, uuid4()),
        data={
            "expected_version": str(seeded["version"]),
            "application_key": "imagen-owner-foreign-v1",
            "imagen": (
                io.BytesIO(b"contenido-corrupto-no-debe-decodificarse"),
                "foto.png",
                "image/png",
            ),
        },
        content_type="multipart/form-data",
    )
    assert foreign_owner.status_code == 404
    assert foreign_owner.get_json()["error"]["code"] == (
        "ONBOARDING_SESSION_NOT_FOUND"
    )
    with app.app_context():
        assert db.session.get(
            ProductoTerminado, "PT-IMAGE-FOREIGN"
        ).imagen_data is None
        assert db.session.get(
            ProductoTerminado, seeded["product_id"]
        ).imagen_data is None
