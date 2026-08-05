from uuid import uuid4

import pytest

from app.extensions import db
from app.models.scm_estructuras import (
    ESTADO_ESTRUCTURA_APROBADA,
    ESTADO_ESTRUCTURA_DESCARTADA,
    ESTADO_ESTRUCTURA_PENDIENTE,
    ESTADO_ESTRUCTURA_RECHAZADA,
    ESTADO_ESTRUCTURA_RETIRADA,
    ScmEstructuraRevision,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_article_service import create_wip_article
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_service_support import ScmServiceError
from app.services.scm_structure_service import (
    approve_structure,
    create_structure,
    discard_structure,
    publish_structure_directly,
    reject_structure,
    send_structure_for_approval,
    update_structure,
)
from app.models.scm_articulos import ScmArticulo


def _actors():
    ensure_initial_scm_configuration()
    creator = Trabajador.query.filter_by(codigo="TRB-01").one()
    creator.roles.append(
        RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
    )
    approver = Trabajador(
        codigo="TRB-R2-JP",
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


def _wip(actor, name):
    payload = create_wip_article(
        db.session,
        actor_id=actor.id,
        data={"nombre": name, "requiere_calidad": False},
    )
    return payload["id"]


def _create_and_approve(actor, approver, result_id, component_id):
    revision = create_structure(
        db.session,
        actor_id=actor.id,
        article_id=result_id,
        data={
            "notas": "Estructura de prueba",
            "componentes": [
                {
                    "articulo_id": component_id,
                    "cantidad": "1",
                }
            ],
        },
    )
    pending = send_structure_for_approval(
        db.session,
        actor_id=actor.id,
        structure_id=revision["id"],
        operation_id=uuid4(),
        data={"version": revision["version"]},
    )
    return approve_structure(
        db.session,
        actor_id=approver.id,
        structure_id=revision["id"],
        operation_id=uuid4(),
        data={"version": pending["version"]},
    )


def test_aprobar_estructura_rechaza_ciclo_indirecto(app):
    with app.app_context():
        creator, approver = _actors()
        article_a = _wip(creator, "WIP A")
        article_b = _wip(creator, "WIP B")
        article_c = _wip(creator, "WIP C")

        assert _create_and_approve(
            creator, approver, article_a, article_b
        )["estado"] == ESTADO_ESTRUCTURA_APROBADA
        assert _create_and_approve(
            creator, approver, article_b, article_c
        )["estado"] == ESTADO_ESTRUCTURA_APROBADA

        candidate = create_structure(
            db.session,
            actor_id=creator.id,
            article_id=article_c,
            data={
                "componentes": [
                    {"articulo_id": article_a, "cantidad": "1"}
                ]
            },
        )
        pending = send_structure_for_approval(
            db.session,
            actor_id=creator.id,
            structure_id=candidate["id"],
            operation_id=uuid4(),
            data={"version": candidate["version"]},
        )

        with pytest.raises(ScmServiceError) as error:
            approve_structure(
                db.session,
                actor_id=approver.id,
                structure_id=candidate["id"],
                operation_id=uuid4(),
                data={"version": pending["version"]},
            )

        assert error.value.code == "STRUCTURE_CYCLE"
        persisted = db.session.get(ScmEstructuraRevision, candidate["id"])
        assert persisted.estado == ESTADO_ESTRUCTURA_PENDIENTE
        assert persisted.content_hash is None


def test_creador_no_puede_aprobar_y_fraccion_un_es_rechazada(app):
    with app.app_context():
        creator, _approver = _actors()
        result_id = _wip(creator, "WIP resultado")
        component_id = _wip(creator, "WIP componente")

        with pytest.raises(ScmServiceError) as quantity_error:
            create_structure(
                db.session,
                actor_id=creator.id,
                article_id=result_id,
                data={
                    "componentes": [
                        {
                            "articulo_id": component_id,
                            "cantidad": "1.5",
                        }
                    ]
                },
            )
        assert quantity_error.value.code == "DISCRETE_QUANTITY_REQUIRED"

        revision = create_structure(
            db.session,
            actor_id=creator.id,
            article_id=result_id,
            data={
                "componentes": [
                    {"articulo_id": component_id, "cantidad": "2"}
                ]
            },
        )
        pending = send_structure_for_approval(
            db.session,
            actor_id=creator.id,
            structure_id=revision["id"],
            operation_id=uuid4(),
            data={"version": revision["version"]},
        )
        creator.roles.append(
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        )
        db.session.commit()

        with pytest.raises(ScmServiceError) as approval_error:
            approve_structure(
                db.session,
                actor_id=creator.id,
                structure_id=revision["id"],
                operation_id=uuid4(),
                data={"version": pending["version"]},
            )
        assert approval_error.value.code == "CREATOR_CANNOT_APPROVE"


def test_jefatura_publica_su_borrador_directamente_con_auditoria(app):
    with app.app_context():
        creator, _approver = _actors()
        creator.roles.append(
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        )
        db.session.commit()
        result_id = _wip(creator, "WIP publicado por jefatura")
        component_id = _wip(creator, "WIP componente publicable")
        revision = create_structure(
            db.session,
            actor_id=creator.id,
            article_id=result_id,
            data={
                "componentes": [
                    {"articulo_id": component_id, "cantidad": "1"}
                ]
            },
        )

        published = publish_structure_directly(
            db.session,
            actor_id=creator.id,
            structure_id=revision["id"],
            operation_id=uuid4(),
            data={"version": revision["version"]},
        )

        assert published["estado"] == ESTADO_ESTRUCTURA_APROBADA
        assert published["creada_por_id"] == creator.id
        assert published["aprobada_por_id"] == creator.id
        assert published["enviada_at"] is not None
        assert published["aprobada_at"] is not None


def test_rechazo_y_descarte_son_transiciones_auditables(app):
    with app.app_context():
        creator, approver = _actors()
        result_id = _wip(creator, "WIP gobernado")
        component_id = _wip(creator, "WIP componente gobernado")
        revision = create_structure(
            db.session,
            actor_id=creator.id,
            article_id=result_id,
            data={"componentes": [{"articulo_id": component_id, "cantidad": "1"}]},
        )
        pending = send_structure_for_approval(
            db.session,
            actor_id=creator.id,
            structure_id=revision["id"],
            operation_id=uuid4(),
            data={"version": revision["version"]},
        )
        creator.roles.append(
            RolOperativo.query.filter_by(codigo="JEFE_PRODUCCION").one()
        )
        db.session.commit()

        with pytest.raises(ScmServiceError) as creator_error:
            reject_structure(
                db.session,
                actor_id=creator.id,
                structure_id=revision["id"],
                operation_id=uuid4(),
                data={"version": pending["version"], "motivo": "Propio"},
            )
        assert creator_error.value.code == "CREATOR_CANNOT_REVIEW"

        with pytest.raises(ScmServiceError) as reason_error:
            reject_structure(
                db.session,
                actor_id=approver.id,
                structure_id=revision["id"],
                operation_id=uuid4(),
                data={"version": pending["version"], "motivo": "  "},
            )
        assert reason_error.value.code == "STRUCTURE_REASON_REQUIRED"

        rejected = reject_structure(
            db.session,
            actor_id=approver.id,
            structure_id=revision["id"],
            operation_id=uuid4(),
            data={
                "version": pending["version"],
                "motivo": "Artículo resultado incorrecto durante UAT",
            },
        )
        assert rejected["estado"] == ESTADO_ESTRUCTURA_RECHAZADA
        assert rejected["rechazada_por_id"] == approver.id
        assert rejected["motivo_rechazo"] == (
            "Artículo resultado incorrecto durante UAT"
        )

        draft = create_structure(
            db.session,
            actor_id=creator.id,
            article_id=result_id,
            data={"componentes": [{"articulo_id": component_id, "cantidad": "2"}]},
        )
        discarded = discard_structure(
            db.session,
            actor_id=creator.id,
            structure_id=draft["id"],
            operation_id=uuid4(),
            data={"version": draft["version"], "motivo": "Borrador duplicado"},
        )
        assert discarded["estado"] == ESTADO_ESTRUCTURA_DESCARTADA
        assert discarded["descartada_por_id"] == creator.id
        assert discarded["motivo_descarte"] == "Borrador duplicado"


def test_estructura_restringe_clases_de_resultado_y_componente(app):
    with app.app_context():
        creator, _approver = _actors()
        wip_id = _wip(creator, "WIP válido")
        piece = ScmArticulo(
            codigo="PC-UAT-CLASE",
            nombre="Pieza UAT",
            clase="PIEZA_COLOR",
        )
        product = ScmArticulo(
            codigo="PT-UAT-CLASE",
            nombre="PT UAT",
            clase="PRODUCTO_TERMINADO",
        )
        db.session.add_all([piece, product])
        db.session.commit()

        with pytest.raises(ScmServiceError) as result_error:
            create_structure(
                db.session,
                actor_id=creator.id,
                article_id=piece.id,
                data={"componentes": [{"articulo_id": wip_id, "cantidad": "1"}]},
            )
        assert result_error.value.code == "STRUCTURE_RESULT_CLASS_INVALID"

        with pytest.raises(ScmServiceError) as component_error:
            create_structure(
                db.session,
                actor_id=creator.id,
                article_id=wip_id,
                data={"componentes": [{"articulo_id": product.id, "cantidad": "1"}]},
            )
        assert component_error.value.code == "STRUCTURE_COMPONENT_CLASS_INVALID"


def test_revision_aprobada_es_inmutable_y_replay_no_duplica_eventos(app):
    with app.app_context():
        creator, approver = _actors()
        result_id = _wip(creator, "WIP resultado inmutable")
        component_id = _wip(creator, "WIP componente inmutable")
        revision = create_structure(
            db.session,
            actor_id=creator.id,
            article_id=result_id,
            data={
                "componentes": [
                    {"articulo_id": component_id, "cantidad": "4"}
                ]
            },
        )
        send_key = uuid4()
        pending = send_structure_for_approval(
            db.session,
            actor_id=creator.id,
            structure_id=revision["id"],
            operation_id=send_key,
            data={"version": revision["version"]},
        )
        assert send_structure_for_approval(
            db.session,
            actor_id=creator.id,
            structure_id=revision["id"],
            operation_id=send_key,
            data={"version": revision["version"]},
        ) == pending

        approved = approve_structure(
            db.session,
            actor_id=approver.id,
            structure_id=revision["id"],
            operation_id=uuid4(),
            data={"version": pending["version"]},
        )
        assert approved["content_hash"]

        with pytest.raises(ScmServiceError) as error:
            update_structure(
                db.session,
                actor_id=creator.id,
                structure_id=revision["id"],
                data={
                    "version": approved["version"],
                    "notas": "Mutacion prohibida",
                    "componentes": [
                        {"articulo_id": component_id, "cantidad": "5"}
                    ],
                },
            )
        assert error.value.code == "STRUCTURE_NOT_EDITABLE"

        replacement = create_structure(
            db.session,
            actor_id=creator.id,
            article_id=result_id,
            data={
                "componentes": [
                    {"articulo_id": component_id, "cantidad": "5"}
                ]
            },
        )
        assert replacement["numero_revision"] == 2
        replacement = send_structure_for_approval(
            db.session,
            actor_id=creator.id,
            structure_id=replacement["id"],
            operation_id=uuid4(),
            data={"version": replacement["version"]},
        )
        replacement = approve_structure(
            db.session,
            actor_id=approver.id,
            structure_id=replacement["id"],
            operation_id=uuid4(),
            data={"version": replacement["version"]},
        )
        assert replacement["estado"] == ESTADO_ESTRUCTURA_APROBADA
        assert db.session.get(
            ScmEstructuraRevision,
            revision["id"],
        ).estado == ESTADO_ESTRUCTURA_RETIRADA


def test_api_expone_flujo_bom_y_exige_idempotency_key(app, client):
    with app.app_context():
        creator, approver = _actors()
        result_id = _wip(creator, "WIP API resultado")
        component_id = _wip(creator, "WIP API componente")
        creator_id = creator.id
        approver_id = approver.id

    created_response = client.post(
        f"/api/scm/v1/articulos/{result_id}/estructuras",
        headers={"X-Actor-Id": str(creator_id)},
        json={
            "componentes": [
                {"articulo_id": component_id, "cantidad": "3"}
            ]
        },
    )
    assert created_response.status_code == 201
    created = created_response.get_json()

    missing_key = client.post(
        f"/api/scm/v1/estructuras/{created['id']}/enviar",
        headers={"X-Actor-Id": str(creator_id)},
        json={"version": created["version"]},
    )
    assert missing_key.status_code == 400
    assert missing_key.get_json()["error"]["code"] == (
        "IDEMPOTENCY_KEY_REQUIRED"
    )

    pending_response = client.post(
        f"/api/scm/v1/estructuras/{created['id']}/enviar",
        headers={
            "X-Actor-Id": str(creator_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={"version": created["version"]},
    )
    assert pending_response.status_code == 200
    pending = pending_response.get_json()

    approved_response = client.post(
        f"/api/scm/v1/estructuras/{created['id']}/aprobar",
        headers={
            "X-Actor-Id": str(approver_id),
            "Idempotency-Key": str(uuid4()),
        },
        json={"version": pending["version"]},
    )
    assert approved_response.status_code == 200
    approved = approved_response.get_json()
    assert approved["estado"] == ESTADO_ESTRUCTURA_APROBADA
    assert approved["componentes"][0]["cantidad"] == "3.000000"

    detail = client.get(
        f"/api/scm/v1/estructuras/{created['id']}",
        headers={"X-Actor-Id": str(creator_id)},
    )
    assert detail.status_code == 200
    assert detail.get_json()["content_hash"] == approved["content_hash"]
