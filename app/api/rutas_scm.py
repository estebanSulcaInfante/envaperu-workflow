from uuid import UUID

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.services.scm_provider_service import (
    create_provider,
    get_provider,
    list_providers,
    update_provider,
)
from app.services.scm_material_catalog_service import (
    create_material,
    create_reception_category,
    get_material,
    get_reception_category,
    list_materials,
    list_reception_categories,
    update_material,
    update_reception_category,
)
from app.services.scm_purchase_service import (
    approve_purchase_order,
    create_purchase_order,
    create_purchase_order_revision,
    get_purchase_order,
    list_purchase_orders,
    send_purchase_order_for_approval,
    update_purchase_order_revision,
)
from app.services.scm_service_support import ScmServiceError
from app.services.scm_reception_draft_service import (
    create_reception_draft,
    create_supplier_document,
    get_reception_draft,
    get_supplier_document,
    list_reception_drafts,
    list_supplier_documents,
    update_reception_draft,
    update_supplier_document,
)


scm_bp = Blueprint("scm", __name__)


@scm_bp.errorhandler(ScmServiceError)
def handle_scm_service_error(error):
    return jsonify({"error": error.to_dict()}), error.status_code


def _json_body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ScmServiceError(
            "JSON_OBJECT_REQUIRED",
            "Se requiere un objeto JSON.",
            status_code=400,
        )
    return payload


def _actor_id():
    raw_value = request.headers.get("X-Actor-Id")
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = None
    if value is None or value <= 0:
        raise ScmServiceError(
            "ACTOR_HEADER_REQUIRED",
            "X-Actor-Id debe identificar un trabajador valido.",
            status_code=400,
        )
    return value


def _idempotency_key():
    raw_value = request.headers.get("Idempotency-Key")
    try:
        return UUID(str(raw_value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key debe contener un UUID valido.",
            status_code=400,
        ) from error


def _optional_active_filter():
    raw_value = request.args.get("activo")
    if raw_value is None:
        return None
    normalized = raw_value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ScmServiceError(
        "INVALID_ACTIVE_FILTER",
        "El filtro activo debe ser true o false.",
        status_code=400,
    )


@scm_bp.get("/config/categorias-recepcion")
def categorias_recepcion_listar():
    return jsonify(list_reception_categories(
        db.session,
        actor_id=_actor_id(),
        active=_optional_active_filter(),
    ))


@scm_bp.post("/config/categorias-recepcion")
def categorias_recepcion_crear():
    payload = create_reception_category(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.get("/config/categorias-recepcion/<int:category_id>")
def categorias_recepcion_detalle(category_id):
    return jsonify(get_reception_category(
        db.session,
        actor_id=_actor_id(),
        category_id=category_id,
    ))


@scm_bp.patch("/config/categorias-recepcion/<int:category_id>")
def categorias_recepcion_actualizar(category_id):
    return jsonify(update_reception_category(
        db.session,
        actor_id=_actor_id(),
        category_id=category_id,
        data=_json_body(),
    ))


@scm_bp.get("/materiales")
def materiales_listar():
    return jsonify(list_materials(
        db.session,
        actor_id=_actor_id(),
        active=_optional_active_filter(),
    ))


@scm_bp.post("/materiales")
def materiales_crear():
    payload = create_material(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.get("/materiales/<int:material_id>")
def materiales_detalle(material_id):
    return jsonify(get_material(
        db.session,
        actor_id=_actor_id(),
        material_id=material_id,
    ))


@scm_bp.patch("/materiales/<int:material_id>")
def materiales_actualizar(material_id):
    return jsonify(update_material(
        db.session,
        actor_id=_actor_id(),
        material_id=material_id,
        data=_json_body(),
    ))


@scm_bp.get("/proveedores")
def proveedores_listar():
    return jsonify(list_providers(
        db.session,
        actor_id=_actor_id(),
        active=_optional_active_filter(),
    ))


@scm_bp.post("/proveedores")
def proveedores_crear():
    payload = create_provider(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.get("/proveedores/<int:provider_id>")
def proveedores_detalle(provider_id):
    return jsonify(get_provider(
        db.session,
        actor_id=_actor_id(),
        provider_id=provider_id,
    ))


@scm_bp.patch("/proveedores/<int:provider_id>")
def proveedores_actualizar(provider_id):
    return jsonify(update_provider(
        db.session,
        actor_id=_actor_id(),
        provider_id=provider_id,
        data=_json_body(),
    ))


@scm_bp.get("/documentos-proveedor")
def documentos_proveedor_listar():
    return jsonify(list_supplier_documents(
        db.session,
        actor_id=_actor_id(),
        provider_id=request.args.get("proveedor_id"),
    ))


@scm_bp.post("/documentos-proveedor")
def documentos_proveedor_crear():
    payload = create_supplier_document(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.get("/documentos-proveedor/<int:document_id>")
def documentos_proveedor_detalle(document_id):
    return jsonify(get_supplier_document(
        db.session,
        actor_id=_actor_id(),
        document_id=document_id,
    ))


@scm_bp.patch("/documentos-proveedor/<int:document_id>")
def documentos_proveedor_actualizar(document_id):
    return jsonify(update_supplier_document(
        db.session,
        actor_id=_actor_id(),
        document_id=document_id,
        data=_json_body(),
    ))


@scm_bp.get("/recepciones/materiales")
def recepciones_materiales_listar():
    return jsonify(list_reception_drafts(
        db.session,
        actor_id=_actor_id(),
    ))


@scm_bp.post("/recepciones/materiales")
def recepciones_materiales_crear():
    payload = create_reception_draft(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.get("/recepciones/materiales/<int:reception_id>")
def recepciones_materiales_detalle(reception_id):
    return jsonify(get_reception_draft(
        db.session,
        actor_id=_actor_id(),
        reception_id=reception_id,
    ))


@scm_bp.patch("/recepciones/materiales/<int:reception_id>")
def recepciones_materiales_actualizar(reception_id):
    return jsonify(update_reception_draft(
        db.session,
        actor_id=_actor_id(),
        reception_id=reception_id,
        data=_json_body(),
    ))


@scm_bp.get("/ordenes-compra-material")
def ordenes_compra_listar():
    return jsonify(list_purchase_orders(
        db.session,
        actor_id=_actor_id(),
    ))


@scm_bp.post("/ordenes-compra-material")
def ordenes_compra_crear():
    payload = create_purchase_order(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.get("/ordenes-compra-material/<int:order_id>")
def ordenes_compra_detalle(order_id):
    return jsonify(get_purchase_order(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
    ))


@scm_bp.post("/ordenes-compra-material/<int:order_id>/revisiones")
def ordenes_compra_nueva_revision(order_id):
    return jsonify(create_purchase_order_revision(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
        data=_json_body(),
    )), 201


@scm_bp.patch(
    "/ordenes-compra-material/<int:order_id>/revisiones/"
    "<int:revision_number>"
)
def ordenes_compra_editar_revision(order_id, revision_number):
    return jsonify(update_purchase_order_revision(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
        revision_number=revision_number,
        data=_json_body(),
    ))


@scm_bp.post(
    "/ordenes-compra-material/<int:order_id>/enviar-aprobacion"
)
def ordenes_compra_enviar_aprobacion(order_id):
    return jsonify(send_purchase_order_for_approval(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/ordenes-compra-material/<int:order_id>/aprobar")
def ordenes_compra_aprobar(order_id):
    return jsonify(approve_purchase_order(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))
