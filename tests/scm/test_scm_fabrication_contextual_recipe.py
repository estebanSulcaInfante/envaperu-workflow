from contextlib import nullcontext
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.receta_color import RecetaColorMaestra
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    FamiliaColor,
    ProductoTerminado,
)
from app.models.scm_articulos import (
    ScmArticuloProducto,
)
from app.models.scm_catalogos import ScmCapacidad, ScmCategoriaRecepcion, ScmMaterial
from app.models.scm_production_orders import (
    ScmCorridaFabricacion,
    ScmOrdenFabricacion,
    ScmOrdenOperacionSalida,
    ScmOrdenOperacion,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_fabrication_contextual_recipe_service import (
    _reserve_operation as _reserve_contextual_operation,
)
from app.services.scm_ot_service import _operation_hash


API = "/api/scm/v1"


def _headers(actor_id, key=None):
    return {
        "X-Actor-Id": str(actor_id),
        "Idempotency-Key": str(key or uuid4()),
    }


def test_contextual_reserva_recupera_la_solicitud_ganadora_en_carrera():
    operation_id = uuid4()
    endpoint = "/formulacion-contextual"
    actor = SimpleNamespace(id=41)
    data = {"version": 1, "accion": "GUARDAR_BORRADOR"}
    existing = SimpleNamespace(
        endpoint=endpoint,
        request_sha256=_operation_hash(endpoint, actor.id, data),
        response_json={"receta": {"id": 9}},
    )

    class RacingSession:
        def __init__(self):
            self.reads = 0
            self.expired = False

        def get(self, _model, requested_id):
            assert requested_id == operation_id
            self.reads += 1
            return None if self.reads == 1 else existing

        def begin_nested(self):
            return nullcontext()

        def add(self, _operation):
            return None

        def flush(self):
            raise IntegrityError("INSERT", {}, RuntimeError("duplicate"))

        def expire_all(self):
            self.expired = True

    session = RacingSession()
    reserved, replay = _reserve_contextual_operation(
        session, operation_id, endpoint, actor, data
    )

    assert reserved is None
    assert replay == {"receta": {"id": 9}}
    assert session.expired is True


@pytest.fixture
def contextual_fixture(app, scm_config):
    del scm_config
    with app.app_context():
        capability_codes = {
            "OF_EDITAR_BORRADOR",
            "ARTICULO_ADMINISTRAR",
            "CATALOGO_MATERIAL_ADMINISTRAR",
            "CONFIG_RECEPCION_ADMINISTRAR",
            "FORMULACION_PUBLICAR_DIRECTO",
        }
        capabilities = list(
            ScmCapacidad.query.filter(ScmCapacidad.codigo.in_(capability_codes))
        )
        role = RolOperativo(
            codigo=f"CTX-{uuid4().hex[:8].upper()}",
            nombre="Contextual UAT",
            capacidades=capabilities,
        )
        actor = Trabajador(
            codigo=f"TRB-CTX-{uuid4().hex[:8].upper()}",
            nombres="Contextual",
            apellidos="Admin",
            activo=True,
            roles=[role],
        )
        weak_role = RolOperativo(
            codigo=f"CTX-W-{uuid4().hex[:8].upper()}",
            nombre="Contextual sin permiso",
            capacidades=[],
        )
        weak_actor = Trabajador(
            codigo=f"TRB-CTX-W-{uuid4().hex[:8].upper()}",
            nombres="Contextual",
            apellidos="Reader",
            activo=True,
            roles=[weak_role],
        )
        family = FamiliaColor(
            nombre=f"CTX-FAMILY-{uuid4().hex[:6].upper()}",
            codigo=900001,
            activo=True,
        )
        base = ColorBase(nombre=f"CTX-COLOR-{uuid4().hex[:6].upper()}")
        color = ColorProduccion(
            color_base_id=None,
            familia_color_id=None,
            activo=True,
        )
        order = ScmOrdenOperacion(
            codigo=f"OF-CTX-{uuid4().hex[:8].upper()}",
            tipo="FABRICACION",
            origen_demanda="EXCEPCIONAL",
            estado="BORRADOR",
            created_by=actor,
        )
        fabrication = ScmOrdenFabricacion(orden_operacion=order)
        run = ScmCorridaFabricacion(
            codigo=f"{order.codigo}-C01",
            secuencia=1,
            estado="BORRADOR",
        )
        fabrication.corridas.append(run)
        db.session.add_all([actor, weak_actor, order])
        db.session.add_all([family, base])
        db.session.flush()
        color.color_base_id = base.id
        color.familia_color_id = family.id
        db.session.add(color)
        db.session.flush()
        category = ScmCategoriaRecepcion.query.filter_by(activo=True).first()
        db.session.commit()
        return {
            "actor_id": actor.id,
            "weak_actor_id": weak_actor.id,
            "order_id": str(order.id),
            "run_id": str(run.id),
            "color_id": color.id,
            "category_id": category.id,
        }


def _payload(context, *, action="GUARDAR_BORRADOR", new_color=False):
    color = (
        {
            "nombre": f"CTX-NEW-{uuid4().hex[:6].upper()}",
            "familia_nueva_nombre": f"CTX-FAM-NEW-{uuid4().hex[:6].upper()}",
            "hex_referencia": "#AABBCC",
        }
        if new_color
        else {"id": context["color_id"]}
    )
    return {
        "version": 1,
        "accion": action,
        "color": color,
        "materiales_nuevos": [{
            "client_id": "mp-base",
            "nombre": f"MP CONTEXTUAL {uuid4().hex[:6].upper()}",
            "clase": "MATERIA_PRIMA",
            "categoria_recepcion_id": context["category_id"],
        }],
        "receta": {
            "variante": f"Contextual {uuid4().hex[:6].upper()}",
            "alcance": "*",
            "base_virgen_kg": 25,
            "notas": "creada desde OF",
            "es_default": False,
            "lineas": [{
                "material_client_id": "mp-base",
                "cantidad": 1,
            }],
        },
    }


def _url(context):
    return (
        f"{API}/ordenes-fabricacion/{context['order_id']}"
        f"/corridas/{context['run_id']}/formulacion-contextual"
    )


def test_contextual_recipe_rolls_back_material_and_color_on_validation_error(
    app,
    client,
    contextual_fixture,
):
    context = contextual_fixture
    with app.app_context():
        before_materials = ScmMaterial.query.count()
        before_colors = ColorProduccion.query.count()
    payload = _payload(context, new_color=True)
    payload["receta"]["lineas"] = [{"material_client_id": "missing", "cantidad": 1}]
    response = client.post(
        _url(context),
        headers=_headers(context["actor_id"]),
        json=payload,
    )
    assert response.status_code == 422, response.get_json()
    assert response.get_json()["error"]["code"] == "MATERIAL_CLIENT_NOT_FOUND"
    with app.app_context():
        assert ScmMaterial.query.count() == before_materials
        assert ColorProduccion.query.count() == before_colors


def test_contextual_recipe_draft_replay_and_approval_selects_run(
    app,
    client,
    contextual_fixture,
):
    context = contextual_fixture
    key = uuid4()
    payload = _payload(context, new_color=True)
    response = client.post(
        _url(context),
        headers=_headers(context["actor_id"], key),
        json=payload,
    )
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["receta"]["estado"] == "BORRADOR"
    assert body["seleccionada"] is False
    assert body["orden_fabricacion"]["version"] == 2
    assert body["orden_fabricacion"]["corridas"][0]["receta_revision_id"] is None

    replay = client.post(
        _url(context),
        headers=_headers(context["actor_id"], key),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.get_json() == body

    approve = _payload(context, action="APROBAR_SELECCIONAR")
    approve["version"] = 2
    approve["color"] = {"id": body["color"]["id"]}
    approve["materiales_nuevos"] = []
    approve["receta"]["nombre_variante"] = approve["receta"].pop("variante")
    approve["receta"]["producto_sku"] = approve["receta"].pop("alcance")
    approve["receta"]["lineas"] = [{
        "material_id": body["materiales_creados"][0]["id"],
        "cantidad": 1,
    }]
    approved = client.post(
        _url(context),
        headers=_headers(context["actor_id"]),
        json=approve,
    )
    assert approved.status_code == 200, approved.get_json()
    approved_body = approved.get_json()
    assert approved_body["receta"]["estado"] == "APROBADA"
    assert approved_body["seleccionada"] is True
    assert approved_body["orden_fabricacion"]["version"] == 3
    assert approved_body["orden_fabricacion"]["corridas"][0]["receta_revision_id"] == (
        approved_body["receta"]["id"]
    )


def test_contextual_recipe_requires_all_authorizations(app, client, contextual_fixture):
    context = contextual_fixture
    response = client.post(
        _url(context),
        headers=_headers(context["weak_actor_id"]),
        json=_payload(context),
    )
    assert response.status_code == 403, response.get_json()
    assert response.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"
    with app.app_context():
        assert ScmMaterial.query.filter(ScmMaterial.nombre.like("MP CONTEXTUAL%" )).count() == 0


def test_contextual_authorization_matrix_is_atomic(app, client, contextual_fixture):
    context = contextual_fixture
    with app.app_context():
        capabilities = {
            item.codigo: item
            for item in ScmCapacidad.query.all()
        }
        if "CATALOGO_MATERIAL_ADMINISTRAR" not in capabilities:
            capabilities["CATALOGO_MATERIAL_ADMINISTRAR"] = ScmCapacidad(
                codigo="CATALOGO_MATERIAL_ADMINISTRAR",
                nombre="Administrar catalogo de materiales",
            )
            db.session.add(capabilities["CATALOGO_MATERIAL_ADMINISTRAR"])
            db.session.flush()

        def actor_with(suffix, codes):
            role = RolOperativo(
                codigo=f"CTX-M-{suffix}-{uuid4().hex[:6].upper()}",
                nombre=f"Contextual matrix {suffix}",
                capacidades=[capabilities[code] for code in codes],
            )
            actor = Trabajador(
                codigo=f"TRB-CTX-M-{suffix}-{uuid4().hex[:6].upper()}",
                nombres="Contextual",
                apellidos=suffix,
                activo=True,
                roles=[role],
            )
            db.session.add(actor)
            db.session.flush()
            return actor.id

        no_article = actor_with("ARTICLE", {"OF_EDITAR_BORRADOR"})
        no_catalog = actor_with(
            "CATALOG",
            {"OF_EDITAR_BORRADOR", "ARTICULO_ADMINISTRAR"},
        )
        no_of_edit = actor_with(
            "NO-OF",
            {
                "ARTICULO_ADMINISTRAR",
                "CATALOGO_MATERIAL_ADMINISTRAR",
                "FORMULACION_PUBLICAR_DIRECTO",
            },
        )
        no_publish = actor_with(
            "PUBLISH",
            {"OF_EDITAR_BORRADOR", "ARTICULO_ADMINISTRAR"},
        )
        db.session.commit()

    with app.app_context():
        before_materials = ScmMaterial.query.count()
        before_order = db.session.get(ScmOrdenOperacion, UUID(context["order_id"]))
        assert before_order.version == 1

    missing_of_edit = client.post(
        _url(context),
        headers=_headers(no_of_edit),
        json=_payload(context),
    )
    assert missing_of_edit.status_code == 403
    assert missing_of_edit.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"

    missing_article = client.post(
        _url(context),
        headers=_headers(no_article),
        json=_payload(context),
    )
    assert missing_article.status_code == 403
    assert missing_article.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"

    missing_catalog = client.post(
        _url(context),
        headers=_headers(no_catalog),
        json=_payload(context),
    )
    assert missing_catalog.status_code == 403
    assert missing_catalog.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"
    with app.app_context():
        assert ScmMaterial.query.count() == before_materials
        assert db.session.get(
            ScmOrdenOperacion, UUID(context["order_id"])
        ).version == 1

    draft = client.post(
        _url(context),
        headers=_headers(context["actor_id"]),
        json=_payload(context),
    )
    assert draft.status_code == 200, draft.get_json()
    draft_body = draft.get_json()
    approve = _payload(context, action="APROBAR_SELECCIONAR")
    approve["version"] = draft_body["orden_fabricacion"]["version"]
    approve["color"] = {"id": draft_body["color"]["id"]}
    approve["materiales_nuevos"] = []
    approve["receta"]["lineas"] = [{
        "material_id": draft_body["materiales_creados"][0]["id"],
        "cantidad": 1,
    }]
    missing_publish = client.post(
        _url(context),
        headers=_headers(no_publish),
        json=approve,
    )
    assert missing_publish.status_code == 403
    assert missing_publish.get_json()["error"]["code"] == "CAPABILITY_REQUIRED"
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(context["order_id"]))
        recipe = db.session.get(RecetaColorMaestra, draft_body["receta"]["id"])
        assert order.version == 2
        assert recipe.estado == "BORRADOR"


def test_contextual_draft_allows_incomplete_fraction_but_approval_requires_full_sum(
    app,
    client,
    contextual_fixture,
):
    context = contextual_fixture
    draft = _payload(context, new_color=True)
    draft["receta"]["lineas"][0]["cantidad"] = 0.7
    created = client.post(
        _url(context),
        headers=_headers(context["actor_id"]),
        json=draft,
    )
    assert created.status_code == 200, created.get_json()
    body = created.get_json()
    assert body["receta"]["estado"] == "BORRADOR"
    assert body["receta"]["lineas"][0]["cantidad"] == 0.7

    approve = _payload(context, action="APROBAR_SELECCIONAR")
    approve["version"] = 2
    approve["color"] = {"id": body["color"]["id"]}
    approve["materiales_nuevos"] = []
    approve["receta"]["lineas"] = [{
        "material_id": body["materiales_creados"][0]["id"],
        "cantidad": 0.99,
    }]
    rejected = client.post(
        _url(context),
        headers=_headers(context["actor_id"]),
        json=approve,
    )
    assert rejected.status_code == 422, rejected.get_json()
    assert rejected.get_json()["error"]["code"] == "FRACCIONES_RECETA_INVALIDAS"
    with app.app_context():
        order = db.session.get(ScmOrdenOperacion, UUID(context["order_id"]))
        assert order.version == 2


def test_contextual_approval_rejects_recipe_scope_incompatible_with_outputs(
    app,
    client,
    contextual_fixture,
):
    context = contextual_fixture
    with app.app_context():
        output_product = ProductoTerminado(
            cod_sku_pt=f"PT-CTX-OUT-{uuid4().hex[:6].upper()}",
            producto="Producto de salida contextual",
            linea_id=1,
            familia_id=1,
        )
        other_product = ProductoTerminado(
            cod_sku_pt=f"PT-CTX-SCOPE-{uuid4().hex[:6].upper()}",
            producto="Producto de alcance distinto",
            linea_id=1,
            familia_id=1,
        )
        db.session.add_all([output_product, other_product])
        db.session.flush()
        article = ScmArticuloProducto.query.filter_by(
            producto_terminado_id=output_product.cod_sku_pt,
        ).one().articulo
        order = db.session.get(ScmOrdenOperacion, UUID(context["order_id"]))
        run = order.fabricacion.corridas[0]
        run.salidas.append(ScmOrdenOperacionSalida(
            orden_operacion=order,
            articulo=article,
            cantidad_objetivo=1,
        ))
        db.session.add(order)
        db.session.commit()
        other_sku = other_product.cod_sku_pt

    payload = _payload(context, action="APROBAR_SELECCIONAR")
    payload["receta"]["producto_sku"] = other_sku
    payload["receta"]["lineas"] = [{
        "material_client_id": "mp-base",
        "cantidad": 1,
    }]
    response = client.post(
        _url(context),
        headers=_headers(context["actor_id"]),
        json=payload,
    )
    assert response.status_code == 422, response.get_json()
    assert response.get_json()["error"]["code"] == "RECIPE_SCOPE_MISMATCH"
