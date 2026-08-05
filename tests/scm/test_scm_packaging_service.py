from decimal import Decimal
from uuid import uuid4

import pytest

from app.extensions import db
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_article_service import create_wip_article
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_packaging_service import (
    approve_packaging_rule,
    assign_article_profiles,
    calculate_packaging_capacity,
    calculate_packaging_plan,
    create_container_type,
    create_packable_profile,
    create_packaging_rule,
    publish_packaging_rule_directly,
    update_packaging_rule,
)
from app.services.scm_service_support import ScmServiceError


def _actors():
    ensure_initial_scm_configuration()
    creator = Trabajador.query.filter_by(codigo="TRB-01").one()
    creator.roles.append(
        RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
    )
    approver = Trabajador(
        codigo="TRB-R4-JP",
        nombres="Julia",
        apellidos="Produccion",
        activo=True,
        roles=[
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        ],
    )
    db.session.add(approver)
    db.session.commit()
    return creator, approver


def _container(
    actor,
    *,
    name="Manga estandar",
    tare="100",
    tare_tolerance="10",
    gross_max="10",
):
    return create_container_type(
        db.session,
        actor_id=actor.id,
        data={
            "clase": "MANGA",
            "nombre": name,
            "material": "PE",
            "dimensiones": {"ancho_mm": "500", "largo_mm": "800"},
            "tara_nominal_g": tare,
            "tolerancia_tara_g": tare_tolerance,
            "peso_bruto_max_kg": gross_max,
        },
    )


def _profile(actor, name="Balde suelto"):
    return create_packable_profile(
        db.session,
        actor_id=actor.id,
        data={
            "nombre": name,
            "descripcion_fisica": "Apilado vertical",
        },
    )


def _rule(
    actor,
    profile_id,
    container_id,
    *,
    target=8,
    tested=10,
    net_max="9",
    margin="0.5",
    measured=True,
):
    return create_packaging_rule(
        db.session,
        actor_id=actor.id,
        data={
            "perfil_empacable_id": profile_id,
            "tipo_contenedor_id": container_id,
            "medicion_fisica_probada": measured,
            "cantidad_objetivo_un": target,
            "cantidad_maxima_probada_un": tested,
            "peso_neto_operativo_max_kg": net_max,
            "margen_seguridad_kg": margin,
            "tolerancia_peso_abs_g": "50",
            "tolerancia_peso_pct": "2.5",
        },
    )


def _approve(actor, approver, rule):
    return approve_packaging_rule(
        db.session,
        actor_id=approver.id,
        revision_id=rule["revision_id"],
        operation_id=uuid4(),
        data={"version": rule["version"]},
    )


def test_calculadora_decimal_planifica_ultima_manga_parcial(app):
    with app.app_context():
        creator, approver = _actors()
        container = _container(creator)
        profile = _profile(creator)
        rule = _approve(
            creator,
            approver,
            _rule(creator, profile["id"], container["id"]),
        )

        plan = calculate_packaging_plan(
            db.session,
            actor_id=creator.id,
            data={
                "regla_revision_id": rule["revision_id"],
                "cantidad_planificada_un": 17,
                "peso_unitario_snapshot_g": "1000",
            },
        )

        assert plan["capacidad_por_peso_un"] == 9
        assert plan["capacidad_efectiva_un"] == 8
        assert plan["numero_contenedores"] == 3
        assert [
            item["cantidad_planificada_un"]
            for item in plan["contenedores"]
        ] == [8, 8, 1]
        assert plan["contenedores"][0]["peso_neto_teorico_kg"] == (
            "8.000"
        )
        pure_result = calculate_packaging_capacity(
            tara_nominal_g=Decimal("100"),
            tolerancia_tara_g=Decimal("10"),
            peso_bruto_max_kg=Decimal("10"),
            peso_neto_operativo_max_kg=Decimal("9"),
            margen_seguridad_kg=Decimal("0.5"),
            cantidad_objetivo_un=8,
            cantidad_maxima_probada_un=10,
            peso_unitario_snapshot_g=Decimal("1000"),
        )
        assert isinstance(
            pure_result["limite_neto_efectivo_kg"],
            Decimal,
        )


def test_regla_inviable_o_sin_medicion_no_puede_aprobarse(app):
    with app.app_context():
        creator, approver = _actors()
        container = _container(
            creator,
            name="Manga sin limite",
            gross_max="0",
        )
        profile = _profile(creator, "Perfil inviable")
        rule = _rule(
            creator,
            profile["id"],
            container["id"],
            measured=False,
        )

        with pytest.raises(ScmServiceError) as error:
            _approve(creator, approver, rule)
        assert error.value.code == "PACKAGING_RULE_NOT_VIABLE"

        rule = update_packaging_rule(
            db.session,
            actor_id=creator.id,
            revision_id=rule["revision_id"],
            data={
                "version": rule["version"],
                "medicion_fisica_probada": True,
                "cantidad_objetivo_un": 8,
                "cantidad_maxima_probada_un": 10,
                "peso_neto_operativo_max_kg": "9",
                "margen_seguridad_kg": "0.5",
                "tolerancia_peso_abs_g": "50",
                "tolerancia_peso_pct": "2.5",
            },
        )
        with pytest.raises(ScmServiceError) as physical_error:
            _approve(creator, approver, rule)
        assert physical_error.value.code == "PACKAGING_RULE_NOT_VIABLE"


def test_jefatura_publica_directamente_su_regla_de_empaque(app):
    with app.app_context():
        creator, jefe = _actors()
        container = _container(jefe, name="Manga directa")
        profile = _profile(jefe, "Perfil directo")
        rule = _rule(jefe, profile["id"], container["id"])

        published = publish_packaging_rule_directly(
            db.session,
            actor_id=jefe.id,
            revision_id=rule["revision_id"],
            operation_id=uuid4(),
            data={"version": rule["version"]},
        )

        assert published["estado"] == "APROBADA"
        assert published["creada_por_id"] == jefe.id
        assert published["aprobada_por_id"] == jefe.id

        foreign_rule = _rule(
            creator,
            _profile(creator, "Perfil sin publicación")["id"],
            _container(creator, name="Manga sin publicación")["id"],
        )
        with pytest.raises(ScmServiceError) as error:
            publish_packaging_rule_directly(
                db.session,
                actor_id=creator.id,
                revision_id=foreign_rule["revision_id"],
                operation_id=uuid4(),
                data={"version": foreign_rule["version"]},
            )
        assert error.value.code == "CAPABILITY_REQUIRED"


def test_override_nunca_expande_capacidad_aprobada(app):
    with app.app_context():
        creator, approver = _actors()
        container = _container(
            creator,
            name="Manga override",
            tare="1000",
            tare_tolerance="0",
            gross_max="10",
        )
        profile = _profile(creator, "Perfil override")
        rule = _approve(
            creator,
            approver,
            _rule(
                creator,
                profile["id"],
                container["id"],
                target=20,
                tested=20,
                net_max="20",
                margin="0",
            ),
        )

        base = calculate_packaging_plan(
            db.session,
            actor_id=creator.id,
            data={
                "regla_revision_id": rule["revision_id"],
                "cantidad_planificada_un": 20,
                "peso_unitario_snapshot_g": "1000",
            },
        )
        assert base["capacidad_efectiva_un"] == 9

        with pytest.raises(ScmServiceError) as error:
            calculate_packaging_plan(
                db.session,
                actor_id=creator.id,
                data={
                    "regla_revision_id": rule["revision_id"],
                    "cantidad_planificada_un": 20,
                    "peso_unitario_snapshot_g": "1000",
                    "override_cantidad_un": 10,
                    "motivo_override": "Intento de expansion",
                },
            )
        assert error.value.code == "PACKAGING_OVERRIDE_EXCEEDS_LIMIT"

        with_real_tare = calculate_packaging_plan(
            db.session,
            actor_id=creator.id,
            data={
                "regla_revision_id": rule["revision_id"],
                "cantidad_planificada_un": 20,
                "peso_unitario_snapshot_g": "1000",
                "tara_real_g": "0",
                "motivo_override": "Tara medida en balanza",
            },
        )
        assert with_real_tare["capacidad_efectiva_un"] == 9


def test_aprobada_es_inmutable_y_nueva_revision_retira_anterior(app):
    with app.app_context():
        creator, approver = _actors()
        container = _container(creator, name="Manga revisionada")
        profile = _profile(creator, "Perfil revisionado")
        first = _approve(
            creator,
            approver,
            _rule(creator, profile["id"], container["id"]),
        )
        assert first["estado"] == "APROBADA"
        assert first["content_hash"]
        assert first["tara_nominal_g_snapshot"] == "100.000"

        with pytest.raises(ScmServiceError) as error:
            update_packaging_rule(
                db.session,
                actor_id=creator.id,
                revision_id=first["revision_id"],
                data={
                    "version": first["version"],
                    "medicion_fisica_probada": True,
                    "cantidad_objetivo_un": 7,
                    "cantidad_maxima_probada_un": 10,
                    "peso_neto_operativo_max_kg": "9",
                    "margen_seguridad_kg": "0.5",
                    "tolerancia_peso_abs_g": "50",
                    "tolerancia_peso_pct": "2.5",
                },
            )
        assert error.value.code == "PACKAGING_RULE_NOT_EDITABLE"

        second = _rule(
            creator,
            profile["id"],
            container["id"],
            target=7,
        )
        assert second["numero_revision"] == 2
        second = _approve(creator, approver, second)
        assert second["estado"] == "APROBADA"


def test_articulo_solo_conserva_un_perfil_predeterminado_activo(app):
    with app.app_context():
        creator, _approver = _actors()
        article = create_wip_article(
            db.session,
            actor_id=creator.id,
            data={"nombre": "WIP con perfiles"},
        )
        loose = _profile(creator, "Balde suelto perfil")
        preassembled = _profile(creator, "Balde con asa perfil")

        assigned = assign_article_profiles(
            db.session,
            actor_id=creator.id,
            article_id=article["id"],
            data={
                "version": article["version"],
                "perfiles": [
                    {
                        "perfil_empacable_id": loose["id"],
                        "es_predeterminado": False,
                    },
                    {
                        "perfil_empacable_id": preassembled["id"],
                        "es_predeterminado": True,
                    },
                ],
            },
        )

        defaults = [
            item for item in assigned["perfiles"]
            if item["es_predeterminado"] and item["activo"]
        ]
        assert len(defaults) == 1
        assert defaults[0]["perfil_empacable_id"] == preassembled["id"]


def test_api_crea_y_aprueba_regla_y_calcula_plan(app, client):
    with app.app_context():
        creator, approver = _actors()
        creator_id = creator.id
        approver_id = approver.id

    container_response = client.post(
        "/api/scm/v1/tipos-contenedor",
        headers={"X-Actor-Id": str(creator_id)},
        json={
            "clase": "MANGA",
            "nombre": "Manga API",
            "tara_nominal_g": "100",
            "tolerancia_tara_g": "10",
            "peso_bruto_max_kg": "10",
        },
    )
    assert container_response.status_code == 201
    container = container_response.get_json()
    assert container["codigo"].startswith("TMG-")

    profile_response = client.post(
        "/api/scm/v1/perfiles-empacables",
        headers={"X-Actor-Id": str(creator_id)},
        json={
            "nombre": "Perfil API",
            "descripcion_fisica": "Acomodo API",
        },
    )
    assert profile_response.status_code == 201
    profile = profile_response.get_json()

    rule_response = client.post(
        "/api/scm/v1/reglas-empaque",
        headers={"X-Actor-Id": str(creator_id)},
        json={
            "perfil_empacable_id": profile["id"],
            "tipo_contenedor_id": container["id"],
            "medicion_fisica_probada": True,
            "cantidad_objetivo_un": 8,
            "cantidad_maxima_probada_un": 10,
            "peso_neto_operativo_max_kg": "9",
            "margen_seguridad_kg": "0.5",
            "tolerancia_peso_abs_g": "50",
            "tolerancia_peso_pct": "2.5",
        },
    )
    assert rule_response.status_code == 201
    rule = rule_response.get_json()

    missing_key = client.post(
        f"/api/scm/v1/reglas-empaque/{rule['revision_id']}/aprobar",
        headers={"X-Actor-Id": str(approver_id)},
        json={"version": rule["version"]},
    )
    assert missing_key.status_code == 400

    approved_response = client.post(
        f"/api/scm/v1/reglas-empaque/{rule['revision_id']}/aprobar",
        headers={
            "X-Actor-Id": str(approver_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={"version": rule["version"]},
    )
    assert approved_response.status_code == 200

    plan_response = client.post(
        "/api/scm/v1/reglas-empaque/calcular",
        headers={"X-Actor-Id": str(creator_id)},
        json={
            "regla_revision_id": rule["revision_id"],
            "cantidad_planificada_un": 17,
            "peso_unitario_snapshot_g": "1000",
        },
    )
    assert plan_response.status_code == 200
    assert plan_response.get_json()["numero_contenedores"] == 3
