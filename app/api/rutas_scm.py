from uuid import UUID

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models.scm_empaque import ScmTipoContenedor
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
from app.services.scm_auth import request_actor_id
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
from app.services.scm_article_service import (
    create_wip_article,
    get_article,
    list_articles,
    update_wip_article,
)
from app.services.scm_structure_service import (
    approve_structure,
    create_structure,
    discard_structure,
    get_structure,
    list_structures,
    publish_structure_directly,
    retire_structure,
    reject_structure,
    send_structure_for_approval,
    update_structure,
)
from app.services.scm_route_service import (
    approve_route,
    create_article_route,
    create_route,
    create_work_center,
    get_route,
    list_article_routes,
    list_routes,
    list_work_centers,
    publish_route_directly,
    retire_route,
    update_route,
    update_work_center,
)
from app.services.scm_packaging_service import (
    approve_packaging_rule,
    assign_article_profiles,
    calculate_packaging_plan,
    create_container_type,
    create_packable_profile,
    create_packaging_rule,
    deactivate_container_type,
    deactivate_packable_profile,
    get_article_profiles,
    get_packaging_rule,
    list_container_types,
    list_packable_profiles,
    list_packaging_rules,
    publish_packaging_rule_directly,
    update_container_type,
    update_packable_profile,
    update_packaging_rule,
)
from app.services.scm_commercial_presentation_service import (
    create_commercial_presentation,
    list_commercial_presentations,
    update_commercial_presentation,
)
from app.services.scm_ot_service import (
    add_color_work,
    add_normal_mangas,
    add_work_mangas,
    annul_manga,
    assign_color_work_worker,
    approve_extra_manga,
    create_fabrication_ot_header,
    create_ot,
    create_fabrication_ot,
    generate_prelabels,
    get_manga_plan,
    get_fabrication_manga_plan,
    get_ot,
    list_extra_manga_requests,
    list_plant_journeys,
    list_pending_manga_continuities,
    list_control_print_jobs,
    list_ots,
    recalculate_manga_plan,
    recalculate_fabrication_manga_plan,
    replace_prelabel,
    request_extra_manga,
    transition_color_work,
    transition_ot,
)
from app.services.scm_weighing_service import (
    annul_manga_weighing,
    approve_weighing_correction,
    get_manga_weighing,
    request_weighing_correction,
)
from app.services.scm_production_order_service import (
    adjust_production_plan_targets,
    approve_production_order,
    cancel_production_order,
    calculate_production_plan,
    confirm_production_plan,
    create_production_order,
    get_production_plan,
    get_production_order,
    list_production_orders,
    refresh_production_order_routes,
)
from app.services.scm_fabrication_order_service import (
    close_fabrication_order,
    create_exceptional_fabrication_order,
    get_fabrication_order,
    list_fabrication_orders,
    release_fabrication_order,
    update_fabrication_order,
)
from app.services.scm_assembly_order_service import (
    create_exceptional_assembly_order,
    get_assembly_order,
    list_assembly_orders,
    transition_assembly_order,
)
from app.services.scm_inventory_service import (
    list_inventory_balances,
    list_inventory_movements,
    register_inventory_movement,
)
from app.services.scm_inventory_opening_service import (
    create_inventory_opening,
    get_inventory_opening,
    list_inventory_openings,
    resolve_inventory_opening,
    submit_inventory_opening,
    update_inventory_opening,
)
from app.services.scm_material_execution_service import (
    confirm_premix,
    emit_reserved_material,
    generate_material_requirements,
    list_material_execution,
    reserve_run_materials,
    return_emitted_material,
)
from app.services.scm_internal_supply_service import (
    assign_non_exact_supply_source,
    assign_supply_manga,
    create_assembly_ot,
    create_supply_request,
    dispatch_supply,
    dispatch_pool_supply_return,
    dispatch_supply_return,
    get_supply_request,
    list_assembly_ots,
    list_supply_requests,
    mark_supply_ready,
    receive_supply,
    receive_pool_supply_return,
    receive_supply_return,
    request_supply_return,
    request_pool_supply_return,
)
from app.services.scm_assembly_execution_service import (
    approve_assembly_quantity_correction,
    assign_assembly_output_mangas,
    close_assembly_manga,
    get_assembly_manga_genealogy,
    get_assembly_manga_plan,
    request_assembly_quantity_correction,
    recalculate_assembly_manga_plan,
)
from app.services.scm_production_observability_service import (
    get_production_ot_observability,
    list_pending_production_documents,
    list_production_manga_observability,
    list_production_ot_observability,
    summarize_production_ot_observability,
)
from app.services.scm_product_onboarding_service import (
    apply_onboarding_image,
    apply_onboarding_step,
    create_onboarding_session,
    finalize_onboarding_session,
    get_onboarding_session,
    list_onboarding_sessions,
    restore_onboarding_colors_from_structure,
    update_onboarding_step,
    validate_onboarding_session,
)
from app.services.catalog_image_storage import MAX_CATALOG_IMAGE_BYTES


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
    try:
        return request_actor_id()
    except ValueError as error:
        raise ScmServiceError(
            "ACTOR_HEADER_REQUIRED",
            "X-Actor-Id debe identificar un trabajador valido.",
            status_code=400,
        ) from error


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


def _optional_idempotency_key():
    raw_value = request.headers.get("Idempotency-Key")
    if raw_value is None or not raw_value.strip():
        return None
    try:
        return UUID(raw_value.strip())
    except (TypeError, ValueError, AttributeError) as error:
        raise ScmServiceError(
            "INVALID_IDEMPOTENCY_KEY",
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


@scm_bp.get("/altas-producto")
def altas_producto_listar():
    return jsonify(list_onboarding_sessions(
        db.session,
        actor_id=_actor_id(),
        state=request.args.get("estado"),
    ))


@scm_bp.post("/altas-producto")
def alta_producto_crear():
    return jsonify(create_onboarding_session(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.get("/altas-producto/<uuid:session_id>")
def alta_producto_detalle(session_id):
    return jsonify(get_onboarding_session(
        db.session,
        actor_id=_actor_id(),
        session_id=session_id,
    ))


@scm_bp.put("/altas-producto/<uuid:session_id>/pasos/<step_code>")
def alta_producto_paso_guardar(session_id, step_code):
    return jsonify(update_onboarding_step(
        db.session,
        actor_id=_actor_id(),
        session_id=session_id,
        step_code=step_code,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post(
    "/altas-producto/<uuid:session_id>/pasos/<step_code>/aplicar"
)
def alta_producto_paso_aplicar(session_id, step_code):
    return jsonify(apply_onboarding_step(
        db.session,
        actor_id=_actor_id(),
        session_id=session_id,
        step_code=step_code,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post(
    "/altas-producto/<uuid:session_id>/pasos/COLORES/"
    "restaurar-desde-estructura"
)
def alta_producto_colores_restaurar_desde_estructura(session_id):
    return jsonify(restore_onboarding_colors_from_structure(
        db.session,
        actor_id=_actor_id(),
        session_id=session_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post(
    "/altas-producto/<uuid:session_id>/imagenes/"
    "<entity_type>/<entity_id>"
)
def alta_producto_imagen_aplicar(session_id, entity_type, entity_id):
    image = request.files.get("imagen")
    if image is None or not image.filename:
        raise ScmServiceError(
            "IMAGE_FILE_REQUIRED",
            "Seleccione una imagen para asociar.",
            status_code=400,
        )
    raw_version = request.form.get("expected_version")
    try:
        parsed_version = int(raw_version)
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "VERSION_REQUIRED",
            "expected_version debe ser un entero positivo.",
            status_code=400,
        ) from error
    content = image.stream.read(MAX_CATALOG_IMAGE_BYTES + 1)
    return jsonify(apply_onboarding_image(
        db.session,
        actor_id=_actor_id(),
        session_id=session_id,
        entity_type=entity_type,
        entity_id=entity_id,
        operation_id=_idempotency_key(),
        data={
            "expected_version": parsed_version,
            "application_key": request.form.get("application_key"),
        },
        mime_type=image.mimetype,
        content=content,
    ))


@scm_bp.post("/altas-producto/<uuid:session_id>/validar")
def alta_producto_validar(session_id):
    return jsonify(validate_onboarding_session(
        db.session,
        actor_id=_actor_id(),
        session_id=session_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/altas-producto/<uuid:session_id>/finalizar")
def alta_producto_finalizar(session_id):
    return jsonify(finalize_onboarding_session(
        db.session,
        actor_id=_actor_id(),
        session_id=session_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.get("/presentaciones-comerciales")
def presentaciones_comerciales_listar():
    return jsonify(list_commercial_presentations(
        db.session,
        actor_id=_actor_id(),
        product_id=request.args.get("producto_terminado_id") or None,
        active=_optional_active_filter(),
    ))


@scm_bp.post("/presentaciones-comerciales")
def presentacion_comercial_crear():
    return jsonify(create_commercial_presentation(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )), 201


@scm_bp.patch("/presentaciones-comerciales/<int:presentation_id>")
def presentacion_comercial_actualizar(presentation_id):
    return jsonify(update_commercial_presentation(
        db.session,
        actor_id=_actor_id(),
        presentation_id=presentation_id,
        data=_json_body(),
    ))


@scm_bp.get("/inventario/saldos")
def inventario_saldos_listar():
    return jsonify(list_inventory_balances(
        db.session,
        actor_id=_actor_id(),
    ))


@scm_bp.get("/inventario/movimientos")
def inventario_movimientos_listar():
    return jsonify(list_inventory_movements(
        db.session,
        actor_id=_actor_id(),
        limit=request.args.get("limite", 100),
    ))


@scm_bp.post("/inventario/movimientos")
def inventario_movimiento_registrar():
    return jsonify(register_inventory_movement(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.get("/inventario/aperturas")
def inventario_aperturas_listar():
    return jsonify(list_inventory_openings(db.session, actor_id=_actor_id()))


@scm_bp.get("/inventario/aperturas/<uuid:opening_id>")
def inventario_apertura_detalle(opening_id):
    return jsonify(get_inventory_opening(
        db.session, actor_id=_actor_id(), opening_id=opening_id,
    ))


@scm_bp.post("/inventario/aperturas")
def inventario_apertura_crear():
    return jsonify(create_inventory_opening(
        db.session, actor_id=_actor_id(), operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.put("/inventario/aperturas/<uuid:opening_id>")
def inventario_apertura_actualizar(opening_id):
    return jsonify(update_inventory_opening(
        db.session, actor_id=_actor_id(), opening_id=opening_id,
        operation_id=_idempotency_key(), data=_json_body(),
    ))


@scm_bp.post("/inventario/aperturas/<uuid:opening_id>/enviar")
def inventario_apertura_enviar(opening_id):
    payload = _json_body()
    return jsonify(submit_inventory_opening(
        db.session, actor_id=_actor_id(), opening_id=opening_id,
        operation_id=_idempotency_key(), version=payload.get("version"),
    ))


@scm_bp.post("/inventario/aperturas/<uuid:opening_id>/resolver")
def inventario_apertura_resolver(opening_id):
    return jsonify(resolve_inventory_opening(
        db.session, actor_id=_actor_id(), opening_id=opening_id,
        operation_id=_idempotency_key(), data=_json_body(),
    ))


@scm_bp.get("/materiales-ejecucion")
def materiales_ejecucion_listar():
    raw_order_id = request.args.get("orden_fabricacion_id")
    try:
        order_id = UUID(raw_order_id) if raw_order_id else None
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_OF_ID", "orden_fabricacion_id debe ser un UUID valido.",
            status_code=400,
        ) from error
    return jsonify(list_material_execution(
        db.session, actor_id=_actor_id(), fabrication_order_id=order_id,
    ))


@scm_bp.post(
    "/ordenes-fabricacion/<uuid:order_id>/requerimientos-material/generar"
)
def materiales_requerimientos_generar(order_id):
    return jsonify(generate_material_requirements(
        db.session, actor_id=_actor_id(), operation_id=_idempotency_key(),
        fabrication_order_id=order_id,
    )), 201


@scm_bp.post("/corridas-fabricacion/<uuid:run_id>/materiales/reservar")
def materiales_corrida_reservar(run_id):
    return jsonify(reserve_run_materials(
        db.session, actor_id=_actor_id(), operation_id=_idempotency_key(),
        run_id=run_id,
    ))


@scm_bp.post("/reservas-material/<uuid:reservation_id>/emitir")
def material_reserva_emitir(reservation_id):
    return jsonify(emit_reserved_material(
        db.session, actor_id=_actor_id(), operation_id=_idempotency_key(),
        reservation_id=reservation_id, data=_json_body(),
    )), 201


@scm_bp.post("/emisiones-material/<uuid:emission_id>/devolver")
def material_emision_devolver(emission_id):
    return jsonify(return_emitted_material(
        db.session, actor_id=_actor_id(), operation_id=_idempotency_key(),
        emission_id=emission_id, data=_json_body(),
    )), 201


@scm_bp.post("/corridas-fabricacion/<uuid:run_id>/premezclas")
def material_premezcla_confirmar(run_id):
    return jsonify(confirm_premix(
        db.session, actor_id=_actor_id(), operation_id=_idempotency_key(),
        run_id=run_id, data=_json_body(),
    )), 201


@scm_bp.post("/ordenes-produccion")
def orden_produccion_crear():
    response = create_production_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        data=_json_body(),
    )
    return jsonify(response), 201


@scm_bp.get("/ordenes-produccion")
def ordenes_produccion_listar():
    return jsonify(list_production_orders(
        db.session,
        actor_id=_actor_id(),
    ))


@scm_bp.get("/ordenes-produccion/<uuid:order_id>")
def orden_produccion_detalle(order_id):
    return jsonify(get_production_order(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
    ))


@scm_bp.get("/ordenes-produccion/<uuid:order_id>/plan")
def orden_produccion_plan_detalle(order_id):
    return jsonify(get_production_plan(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
    ))


@scm_bp.post("/ordenes-produccion/<uuid:order_id>/aprobar")
def orden_produccion_aprobar(order_id):
    payload = _json_body()
    return jsonify(approve_production_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        order_id=order_id,
        expected_resource_version=payload.get("version"),
    ))


@scm_bp.post("/ordenes-produccion/<uuid:order_id>/cancelar")
def orden_produccion_cancelar(order_id):
    return jsonify(cancel_production_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        order_id=order_id,
        data=_json_body(),
    ))


@scm_bp.post("/ordenes-produccion/<uuid:order_id>/calcular-plan")
def orden_produccion_plan_calcular(order_id):
    payload = _json_body()
    return jsonify(calculate_production_plan(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        order_id=order_id,
        expected_resource_version=payload.get("version"),
    )), 201


@scm_bp.post("/ordenes-produccion/<uuid:order_id>/confirmar-plan")
def orden_produccion_plan_confirmar(order_id):
    return jsonify(confirm_production_plan(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        order_id=order_id,
        data=_json_body(),
    )), 201


@scm_bp.post("/ordenes-produccion/<uuid:order_id>/ajustar-metas")
def orden_produccion_plan_ajustar_metas(order_id):
    return jsonify(adjust_production_plan_targets(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        order_id=order_id,
        data=_json_body(),
    )), 201


@scm_bp.post("/ordenes-fabricacion/excepcionales")
def orden_fabricacion_excepcional_crear():
    response = create_exceptional_fabrication_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        data=_json_body(),
    )
    return jsonify(response), 201


@scm_bp.get("/ordenes-fabricacion")
def ordenes_fabricacion_listar():
    return jsonify(list_fabrication_orders(
        db.session,
        actor_id=_actor_id(),
    ))


@scm_bp.get("/ordenes-fabricacion/<uuid:order_id>")
def orden_fabricacion_detalle(order_id):
    return jsonify(get_fabrication_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=order_id,
    ))


@scm_bp.patch("/ordenes-fabricacion/<uuid:order_id>")
def orden_fabricacion_actualizar(order_id):
    return jsonify(update_fabrication_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        operation_order_id=order_id,
        data=_json_body(),
    ))


@scm_bp.post("/ordenes-fabricacion/<uuid:order_id>/liberar")
def orden_fabricacion_liberar(order_id):
    payload = _json_body()
    return jsonify(release_fabrication_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        operation_order_id=order_id,
        expected_resource_version=payload.get("version"),
    ))


@scm_bp.post("/ordenes-fabricacion/<uuid:order_id>/cerrar")
def orden_fabricacion_cerrar(order_id):
    return jsonify(close_fabrication_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        operation_order_id=order_id,
        data=_json_body(),
    ))


@scm_bp.get("/ordenes-armado")
@scm_bp.get("/ordenes-ensamble")
def ordenes_armado_listar():
    return jsonify(list_assembly_orders(
        db.session,
        actor_id=_actor_id(),
    ))


@scm_bp.post("/ordenes-armado/excepcionales")
def orden_armado_excepcional_crear():
    payload, created = create_exceptional_assembly_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        data=_json_body(),
    )
    return jsonify(payload), 201 if created else 200


@scm_bp.get("/ordenes-armado/<uuid:order_id>")
@scm_bp.get("/ordenes-ensamble/<uuid:order_id>")
def orden_armado_detalle(order_id):
    return jsonify(get_assembly_order(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
    ))


@scm_bp.get("/ordenes-armado/<uuid:order_id>/ots")
@scm_bp.get("/ordenes-ensamble/<uuid:order_id>/ots")
def orden_armado_ots_listar(order_id):
    return jsonify(list_assembly_ots(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
    ))


@scm_bp.post("/ordenes-armado/<uuid:order_id>/ots")
@scm_bp.post("/ordenes-ensamble/<uuid:order_id>/ots")
def orden_armado_ot_crear(order_id):
    return jsonify(create_assembly_ot(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.get("/ordenes-armado/<uuid:order_id>/plan-mangas")
@scm_bp.get("/ordenes-ensamble/<uuid:order_id>/plan-mangas")
def orden_armado_plan_mangas_detalle(order_id):
    return jsonify(get_assembly_manga_plan(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
    ))


@scm_bp.post("/ordenes-armado/<uuid:order_id>/plan-mangas/recalcular")
@scm_bp.post("/ordenes-ensamble/<uuid:order_id>/plan-mangas/recalcular")
def orden_armado_plan_mangas_recalcular(order_id):
    return jsonify(recalculate_assembly_manga_plan(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/ordenes-armado/<uuid:order_id>/<action>")
@scm_bp.post("/ordenes-ensamble/<uuid:order_id>/<action>")
def orden_armado_transicionar(order_id, action):
    return jsonify(transition_assembly_order(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        order_id=order_id,
        action=action,
        data=_json_body(),
    ))


@scm_bp.post("/ots/<uuid:public_id>/abastecimiento")
def abastecimiento_crear(public_id):
    return jsonify(create_supply_request(
        db.session,
        actor_id=_actor_id(),
        ot_id=public_id,
        operation_id=_idempotency_key(),
    )), 201


@scm_bp.post("/ots/<uuid:public_id>/mangas-salida")
def orden_armado_mangas_salida_asignar(public_id):
    return jsonify(assign_assembly_output_mangas(
        db.session,
        actor_id=_actor_id(),
        ot_id=public_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/mangas/<uuid:manga_id>/cerrar-armado")
def manga_armado_cerrar(manga_id):
    return jsonify(close_assembly_manga(
        db.session,
        actor_id=_actor_id(),
        manga_id=manga_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.get("/mangas/<uuid:manga_id>/genealogia")
def manga_armado_genealogia(manga_id):
    return jsonify(get_assembly_manga_genealogy(
        db.session,
        actor_id=_actor_id(),
        manga_id=manga_id,
    ))


@scm_bp.post("/mangas/<uuid:manga_id>/correcciones-cantidad")
def manga_armado_correccion_solicitar(manga_id):
    return jsonify(request_assembly_quantity_correction(
        db.session, actor_id=_actor_id(), manga_id=manga_id,
        operation_id=_idempotency_key(), data=_json_body(),
    )), 201


@scm_bp.post("/correcciones-armado/<uuid:correction_id>/aprobar")
@scm_bp.post("/correcciones-ensamble/<uuid:correction_id>/aprobar")
def manga_armado_correccion_aprobar(correction_id):
    return jsonify(approve_assembly_quantity_correction(
        db.session, actor_id=_actor_id(), correction_id=correction_id,
        operation_id=_idempotency_key(), data=_json_body(),
    ))


@scm_bp.get("/abastecimiento")
def abastecimiento_listar():
    return jsonify(list_supply_requests(
        db.session,
        actor_id=_actor_id(),
        state=request.args.get("estado"),
    ))


@scm_bp.get("/abastecimiento/<uuid:request_id>")
def abastecimiento_detalle(request_id):
    return jsonify(get_supply_request(
        db.session,
        actor_id=_actor_id(),
        request_id=request_id,
    ))


@scm_bp.post("/abastecimiento/<uuid:request_id>/mangas")
def abastecimiento_asignar_manga(request_id):
    return jsonify(assign_supply_manga(
        db.session,
        actor_id=_actor_id(),
        request_id=request_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/abastecimiento/<uuid:request_id>/fuentes-no-exactas")
def abastecimiento_asignar_fuente_no_exacta(request_id):
    return jsonify(assign_non_exact_supply_source(
        db.session,
        actor_id=_actor_id(),
        request_id=request_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/abastecimiento/<uuid:request_id>/lista")
def abastecimiento_marcar_lista(request_id):
    return jsonify(mark_supply_ready(
        db.session,
        actor_id=_actor_id(),
        request_id=request_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/abastecimiento/<uuid:request_id>/despachar")
def abastecimiento_despachar(request_id):
    return jsonify(dispatch_supply(
        db.session,
        actor_id=_actor_id(),
        request_id=request_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/abastecimiento/<uuid:request_id>/recibir")
def abastecimiento_recibir(request_id):
    return jsonify(receive_supply(
        db.session,
        actor_id=_actor_id(),
        request_id=request_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/abastecimiento/asignaciones/<uuid:assignment_id>/retorno")
def abastecimiento_retorno_solicitar(assignment_id):
    return jsonify(request_supply_return(
        db.session,
        actor_id=_actor_id(),
        assignment_id=assignment_id,
        operation_id=_idempotency_key(),
    ))


@scm_bp.post(
    "/abastecimiento/asignaciones/<uuid:assignment_id>/despachar-retorno"
)
def abastecimiento_retorno_despachar(assignment_id):
    return jsonify(dispatch_supply_return(
        db.session,
        actor_id=_actor_id(),
        assignment_id=assignment_id,
        operation_id=_idempotency_key(),
    ))


@scm_bp.post(
    "/abastecimiento/asignaciones/<uuid:assignment_id>/recibir-retorno"
)
def abastecimiento_retorno_recibir(assignment_id):
    return jsonify(receive_supply_return(
        db.session,
        actor_id=_actor_id(),
        assignment_id=assignment_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/abastecimiento/asignaciones-pool/<uuid:assignment_id>/retorno")
def abastecimiento_pool_retorno_solicitar(assignment_id):
    return jsonify(request_pool_supply_return(
        db.session, actor_id=_actor_id(), assignment_id=assignment_id,
        operation_id=_idempotency_key(),
    ))


@scm_bp.post("/abastecimiento/asignaciones-pool/<uuid:assignment_id>/despachar-retorno")
def abastecimiento_pool_retorno_despachar(assignment_id):
    return jsonify(dispatch_pool_supply_return(
        db.session, actor_id=_actor_id(), assignment_id=assignment_id,
        operation_id=_idempotency_key(),
    ))


@scm_bp.post("/abastecimiento/asignaciones-pool/<uuid:assignment_id>/recibir-retorno")
def abastecimiento_pool_retorno_recibir(assignment_id):
    return jsonify(receive_pool_supply_return(
        db.session, actor_id=_actor_id(), assignment_id=assignment_id,
        operation_id=_idempotency_key(), data=_json_body(),
    ))


@scm_bp.get("/ordenes-produccion/<op_number>/plan-mangas")
def plan_mangas_detalle(op_number):
    return jsonify(get_manga_plan(
        db.session,
        actor_id=_actor_id(),
        op_number=op_number,
    ))


@scm_bp.post(
    "/ordenes-produccion/<op_number>/plan-mangas/recalcular"
)
def plan_mangas_recalcular(op_number):
    return jsonify(recalculate_manga_plan(
        db.session,
        actor_id=_actor_id(),
        op_number=op_number,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/ordenes-produccion/<op_number>/ots")
def ots_crear(op_number):
    return jsonify(create_ot(
        db.session,
        actor_id=_actor_id(),
        op_number=op_number,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.get("/ordenes-fabricacion/<uuid:order_id>/plan-mangas")
def plan_mangas_of_detalle(order_id):
    return jsonify(get_fabrication_manga_plan(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
    ))


@scm_bp.post(
    "/ordenes-fabricacion/<uuid:order_id>/plan-mangas/recalcular"
)
def plan_mangas_of_recalcular(order_id):
    return jsonify(recalculate_fabrication_manga_plan(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/ordenes-fabricacion/<uuid:order_id>/ots")
def ots_of_crear(order_id):
    return jsonify(create_fabrication_ot(
        db.session,
        actor_id=_actor_id(),
        order_id=order_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/ordenes-produccion/<uuid:order_id>/actualizar-rutas")
def orden_produccion_rutas_actualizar(order_id):
    payload = _json_body()
    return jsonify(refresh_production_order_routes(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        order_id=order_id,
        expected_resource_version=payload.get("version"),
    ))


@scm_bp.post("/ots/fabricacion")
def ots_fabricacion_cabecera_crear():
    return jsonify(create_fabrication_ot_header(
        db.session,
        actor_id=_actor_id(),
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/ots/<uuid:ot_id>/trabajos-color")
def trabajos_color_crear(ot_id):
    return jsonify(add_color_work(
        db.session,
        actor_id=_actor_id(),
        ot_id=ot_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.get("/ots/<uuid:ot_id>/continuidades-pendientes")
def continuidades_manga_pendientes(ot_id):
    return jsonify(list_pending_manga_continuities(
        db.session,
        actor_id=_actor_id(),
        ot_id=ot_id,
        corrida_fabricacion_id=request.args.get("corrida_fabricacion_id"),
    ))


@scm_bp.post("/trabajos-color/<uuid:work_id>/<action>")
def trabajos_color_transicionar(work_id, action):
    return jsonify(transition_color_work(
        db.session,
        actor_id=_actor_id(),
        work_id=work_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
        action=action,
    ))


@scm_bp.post("/trabajos-color/<uuid:work_id>/asignaciones")
def trabajos_color_asignar_personal(work_id):
    return jsonify(assign_color_work_worker(
        db.session,
        actor_id=_actor_id(),
        work_id=work_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/trabajos-color/<uuid:work_id>/mangas")
def trabajos_color_agregar_mangas(work_id):
    return jsonify(add_work_mangas(
        db.session,
        actor_id=_actor_id(),
        work_id=work_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.get("/ots/<uuid:public_id>")
def ots_detalle(public_id):
    return jsonify(get_ot(
        db.session,
        actor_id=_actor_id(),
        public_id=public_id,
    ))


@scm_bp.get("/ots")
def ots_listar():
    return jsonify(list_ots(
        db.session,
        actor_id=_actor_id(),
        op_number=request.args.get("orden_id"),
        operation_order_id=request.args.get("orden_operacion_id"),
        tipo_ot=request.args.get("tipo_ot"),
        operational_date=(
            request.args.get("fecha_operativa") or request.args.get("fecha")
        ),
        machine_id=request.args.get("maquina_id"),
        machine=request.args.get("maquina"),
        shift=request.args.get("turno"),
    ))


@scm_bp.get("/jornadas-planta")
def jornadas_planta_listar():
    return jsonify(list_plant_journeys(
        db.session,
        actor_id=_actor_id(),
        operational_date=(
            request.args.get("fecha_operativa") or request.args.get("fecha")
        ),
        shift=request.args.get("turno"),
    ))


@scm_bp.get("/observabilidad/mangas")
def observabilidad_mangas_listar():
    return jsonify(list_production_manga_observability(
        db.session,
        actor_id=_actor_id(),
        filters=request.args.to_dict(flat=True),
    ))



@scm_bp.get("/observabilidad/trabajos-impresion")
def observabilidad_trabajos_impresion_listar():
    return jsonify(list_control_print_jobs(
        db.session,
        actor_id=_actor_id(),
        filters=request.args.to_dict(flat=True),
    ))

@scm_bp.get("/observabilidad/ots")
def observabilidad_ots_listar():
    return jsonify(list_production_ot_observability(
        db.session,
        actor_id=_actor_id(),
        filters=request.args.to_dict(flat=True),
    ))


@scm_bp.get("/observabilidad/documentos-pendientes")
def observabilidad_documentos_pendientes_listar():
    return jsonify(list_pending_production_documents(
        db.session,
        actor_id=_actor_id(),
        filters=request.args.to_dict(flat=True),
    ))


@scm_bp.get("/observabilidad/ots/<uuid:public_id>")
def observabilidad_ots_detalle(public_id):
    return jsonify(get_production_ot_observability(
        db.session,
        actor_id=_actor_id(),
        public_id=public_id,
    ))


@scm_bp.get("/observabilidad/resumen")
def observabilidad_resumen():
    filters = request.args.to_dict(flat=True)
    granularity = filters.pop("granularidad", "DIA")
    return jsonify(summarize_production_ot_observability(
        db.session,
        actor_id=_actor_id(),
        filters=filters,
        granularity=granularity,
    ))


@scm_bp.post("/ots/<uuid:public_id>/iniciar")
def ots_iniciar(public_id):
    return jsonify(transition_ot(
        db.session,
        actor_id=_actor_id(),
        public_id=public_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
        action="iniciar",
    ))


@scm_bp.post("/ots/<uuid:public_id>/cerrar")
def ots_cerrar(public_id):
    return jsonify(transition_ot(
        db.session,
        actor_id=_actor_id(),
        public_id=public_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
        action="cerrar",
    ))


@scm_bp.post("/ots/<uuid:public_id>/mangas-extra/solicitudes")
def mangas_extra_solicitar(public_id):
    return jsonify(request_extra_manga(
        db.session,
        actor_id=_actor_id(),
        public_id=public_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.get("/mangas-extra/solicitudes")
def mangas_extra_listar():
    return jsonify(list_extra_manga_requests(
        db.session,
        actor_id=_actor_id(),
        op_number=request.args.get("orden_id"),
        operation_order_id=request.args.get("orden_operacion_id"),
        state=request.args.get("estado"),
    ))


@scm_bp.post("/ots/<uuid:public_id>/mangas")
def mangas_normales_agregar(public_id):
    return jsonify(add_normal_mangas(
        db.session,
        actor_id=_actor_id(),
        public_id=public_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post(
    "/mangas-extra/solicitudes/<uuid:request_id>/aprobar"
)
def mangas_extra_aprobar(request_id):
    return jsonify(approve_extra_manga(
        db.session,
        actor_id=_actor_id(),
        request_id=request_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/mangas/<uuid:manga_id>/etiquetas-prepesaje")
def mangas_etiqueta_prepesaje(manga_id):
    return jsonify(generate_prelabels(
        db.session,
        actor_id=_actor_id(),
        manga_id=manga_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.post("/mangas/<uuid:manga_id>/anular")
def mangas_anular(manga_id):
    return jsonify(annul_manga(
        db.session,
        actor_id=_actor_id(),
        manga_id=manga_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/etiquetas/<uuid:label_id>/reemplazos")
def etiquetas_reemplazar(label_id):
    return jsonify(replace_prelabel(
        db.session,
        actor_id=_actor_id(),
        label_id=label_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    )), 201


@scm_bp.get("/articulos")
def articulos_listar():
    return jsonify(list_articles(
        db.session,
        actor_id=_actor_id(),
        active=_optional_active_filter(),
    ))


@scm_bp.get("/mangas/<uuid:manga_id>/pesaje")
def mangas_pesaje_detalle(manga_id):
    return jsonify(get_manga_weighing(
        db.session,
        actor_id=_actor_id(),
        manga_id=manga_id,
    ))


@scm_bp.post("/pesajes/<uuid:weighing_id>/anular")
def pesajes_anular(weighing_id):
    return jsonify(annul_manga_weighing(
        db.session,
        actor_id=_actor_id(),
        weighing_id=weighing_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/pesajes/<uuid:weighing_id>/correcciones")
def pesajes_correccion_solicitar(weighing_id):
    return jsonify(request_weighing_correction(
        db.session,
        actor_id=_actor_id(),
        weighing_id=weighing_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post(
    "/correcciones-pesaje/<uuid:correction_id>/aprobar"
)
def pesajes_correccion_aprobar(correction_id):
    return jsonify(approve_weighing_correction(
        db.session,
        actor_id=_actor_id(),
        correction_id=correction_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.get("/articulos/<int:article_id>")
def articulos_detalle(article_id):
    return jsonify(get_article(
        db.session,
        actor_id=_actor_id(),
        article_id=article_id,
    ))


@scm_bp.post("/articulos/wip")
def articulos_wip_crear():
    payload = create_wip_article(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.patch("/articulos/wip/<int:article_id>")
def articulos_wip_actualizar(article_id):
    return jsonify(update_wip_article(
        db.session,
        actor_id=_actor_id(),
        article_id=article_id,
        data=_json_body(),
    ))


@scm_bp.get("/articulos/<int:article_id>/estructuras")
def estructuras_listar(article_id):
    return jsonify(list_structures(
        db.session,
        actor_id=_actor_id(),
        article_id=article_id,
    ))


@scm_bp.post("/articulos/<int:article_id>/estructuras")
def estructuras_crear(article_id):
    payload = create_structure(
        db.session,
        actor_id=_actor_id(),
        article_id=article_id,
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.get("/estructuras/<int:structure_id>")
def estructuras_detalle(structure_id):
    return jsonify(get_structure(
        db.session,
        actor_id=_actor_id(),
        structure_id=structure_id,
    ))


@scm_bp.put("/estructuras/<int:structure_id>")
def estructuras_actualizar(structure_id):
    return jsonify(update_structure(
        db.session,
        actor_id=_actor_id(),
        structure_id=structure_id,
        data=_json_body(),
    ))


@scm_bp.post("/estructuras/<int:structure_id>/enviar")
def estructuras_enviar(structure_id):
    return jsonify(send_structure_for_approval(
        db.session,
        actor_id=_actor_id(),
        structure_id=structure_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/estructuras/<int:structure_id>/aprobar")
def estructuras_aprobar(structure_id):
    return jsonify(approve_structure(
        db.session,
        actor_id=_actor_id(),
        structure_id=structure_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/estructuras/<int:structure_id>/publicar")
def estructuras_publicar(structure_id):
    return jsonify(publish_structure_directly(
        db.session,
        actor_id=_actor_id(),
        structure_id=structure_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/estructuras/<int:structure_id>/rechazar")
def estructuras_rechazar(structure_id):
    return jsonify(reject_structure(
        db.session,
        actor_id=_actor_id(),
        structure_id=structure_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/estructuras/<int:structure_id>/descartar")
def estructuras_descartar(structure_id):
    return jsonify(discard_structure(
        db.session,
        actor_id=_actor_id(),
        structure_id=structure_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/estructuras/<int:structure_id>/retirar")
def estructuras_retirar(structure_id):
    return jsonify(retire_structure(
        db.session,
        actor_id=_actor_id(),
        structure_id=structure_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.get("/centros-trabajo")
def centros_trabajo_listar():
    return jsonify(list_work_centers(
        db.session,
        actor_id=_actor_id(),
        active=_optional_active_filter(),
    ))


@scm_bp.post("/centros-trabajo")
def centros_trabajo_crear():
    payload = create_work_center(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.patch("/centros-trabajo/<int:center_id>")
def centros_trabajo_actualizar(center_id):
    return jsonify(update_work_center(
        db.session,
        actor_id=_actor_id(),
        center_id=center_id,
        data=_json_body(),
    ))


@scm_bp.get("/productos/<string:product_id>/rutas")
def rutas_listar(product_id):
    return jsonify(list_routes(
        db.session,
        actor_id=_actor_id(),
        product_id=product_id,
    ))


@scm_bp.get("/articulos/<int:article_id>/rutas")
def rutas_articulo_listar(article_id):
    return jsonify(list_article_routes(
        db.session,
        actor_id=_actor_id(),
        article_id=article_id,
    ))


@scm_bp.post("/productos/<string:product_id>/rutas")
def rutas_crear(product_id):
    payload = create_route(
        db.session,
        actor_id=_actor_id(),
        product_id=product_id,
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.post("/articulos/<int:article_id>/rutas")
def rutas_articulo_crear(article_id):
    payload = create_article_route(
        db.session,
        actor_id=_actor_id(),
        article_id=article_id,
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.get("/rutas/<int:route_id>")
def rutas_detalle(route_id):
    return jsonify(get_route(
        db.session,
        actor_id=_actor_id(),
        route_id=route_id,
    ))


@scm_bp.put("/rutas/<int:route_id>")
def rutas_actualizar(route_id):
    return jsonify(update_route(
        db.session,
        actor_id=_actor_id(),
        route_id=route_id,
        data=_json_body(),
    ))


@scm_bp.post("/rutas/<int:route_id>/aprobar")
def rutas_aprobar(route_id):
    return jsonify(approve_route(
        db.session,
        actor_id=_actor_id(),
        route_id=route_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/rutas/<int:route_id>/publicar")
def rutas_publicar(route_id):
    return jsonify(publish_route_directly(
        db.session,
        actor_id=_actor_id(),
        route_id=route_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/rutas/<int:route_id>/retirar")
def rutas_retirar(route_id):
    return jsonify(retire_route(
        db.session,
        actor_id=_actor_id(),
        route_id=route_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.get("/tipos-contenedor")
def tipos_contenedor_listar():
    return jsonify(list_container_types(
        db.session,
        actor_id=_actor_id(),
        active=_optional_active_filter(),
    ))


@scm_bp.post("/tipos-contenedor")
def tipos_contenedor_crear():
    payload = create_container_type(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.put("/tipos-contenedor/<int:container_id>")
def tipos_contenedor_actualizar(container_id):
    return jsonify(update_container_type(
        db.session,
        actor_id=_actor_id(),
        container_id=container_id,
        data=_json_body(),
    ))


@scm_bp.delete("/tipos-contenedor/<int:container_id>")
def tipos_contenedor_desactivar(container_id):
    return jsonify(deactivate_container_type(
        db.session,
        actor_id=_actor_id(),
        container_id=container_id,
        data=_json_body(),
    ))


def _require_manga_container(container_id):
    item = db.session.get(ScmTipoContenedor, container_id)
    if item is None or item.clase != "MANGA":
        raise ScmServiceError(
            "MANGA_TYPE_NOT_FOUND",
            "El tipo de manga no existe.",
            status_code=404,
        )


@scm_bp.get("/tipos-manga")
def tipos_manga_listar():
    result = list_container_types(
        db.session,
        actor_id=_actor_id(),
        active=_optional_active_filter(),
    )
    return jsonify({
        "items": [
            item for item in result["items"] if item["clase"] == "MANGA"
        ]
    })


@scm_bp.post("/tipos-manga")
def tipos_manga_crear():
    payload = _json_body()
    if str(payload.get("clase", "MANGA")).upper() != "MANGA":
        raise ScmServiceError(
            "INVALID_CONTAINER_CLASS",
            "Este endpoint solo crea tipos de manga.",
            status_code=422,
        )
    payload["clase"] = "MANGA"
    return jsonify(create_container_type(
        db.session,
        actor_id=_actor_id(),
        data=payload,
        capability="TIPO_MANGA_ADMINISTRAR",
    )), 201


@scm_bp.put("/tipos-manga/<int:container_id>")
def tipos_manga_actualizar(container_id):
    _require_manga_container(container_id)
    return jsonify(update_container_type(
        db.session,
        actor_id=_actor_id(),
        container_id=container_id,
        data=_json_body(),
        capability="TIPO_MANGA_ADMINISTRAR",
    ))


@scm_bp.delete("/tipos-manga/<int:container_id>")
def tipos_manga_desactivar(container_id):
    _require_manga_container(container_id)
    return jsonify(deactivate_container_type(
        db.session,
        actor_id=_actor_id(),
        container_id=container_id,
        data=_json_body(),
        capability="TIPO_MANGA_ADMINISTRAR",
    ))


@scm_bp.get("/perfiles-empacables")
def perfiles_empacables_listar():
    return jsonify(list_packable_profiles(
        db.session,
        actor_id=_actor_id(),
        active=_optional_active_filter(),
    ))


@scm_bp.post("/perfiles-empacables")
def perfiles_empacables_crear():
    payload = create_packable_profile(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.put("/perfiles-empacables/<int:profile_id>")
def perfiles_empacables_actualizar(profile_id):
    return jsonify(update_packable_profile(
        db.session,
        actor_id=_actor_id(),
        profile_id=profile_id,
        data=_json_body(),
    ))


@scm_bp.delete("/perfiles-empacables/<int:profile_id>")
def perfiles_empacables_desactivar(profile_id):
    return jsonify(deactivate_packable_profile(
        db.session,
        actor_id=_actor_id(),
        profile_id=profile_id,
        data=_json_body(),
    ))


@scm_bp.get("/articulos/<int:article_id>/perfiles-empaque")
def articulos_perfiles_empaque_detalle(article_id):
    return jsonify(get_article_profiles(
        db.session,
        actor_id=_actor_id(),
        article_id=article_id,
    ))


@scm_bp.put("/articulos/<int:article_id>/perfiles-empaque")
def articulos_perfiles_empaque_asignar(article_id):
    return jsonify(assign_article_profiles(
        db.session,
        actor_id=_actor_id(),
        article_id=article_id,
        data=_json_body(),
    ))


@scm_bp.get("/reglas-empaque")
def reglas_empaque_listar():
    profile_id = request.args.get("perfil_empacable_id")
    container_id = request.args.get("tipo_contenedor_id")
    try:
        profile_id = int(profile_id) if profile_id is not None else None
        container_id = (
            int(container_id) if container_id is not None else None
        )
    except ValueError as error:
        raise ScmServiceError(
            "POSITIVE_INTEGER_REQUIRED",
            "Los filtros de regla deben ser enteros.",
            status_code=400,
        ) from error
    return jsonify(list_packaging_rules(
        db.session,
        actor_id=_actor_id(),
        profile_id=profile_id,
        container_id=container_id,
    ))


@scm_bp.post("/reglas-empaque")
def reglas_empaque_crear():
    payload = create_packaging_rule(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    )
    return jsonify(payload), 201


@scm_bp.post("/reglas-empaque/calcular")
def reglas_empaque_calcular():
    return jsonify(calculate_packaging_plan(
        db.session,
        actor_id=_actor_id(),
        data=_json_body(),
    ))


@scm_bp.get("/reglas-empaque/<int:revision_id>")
def reglas_empaque_detalle(revision_id):
    return jsonify(get_packaging_rule(
        db.session,
        actor_id=_actor_id(),
        revision_id=revision_id,
    ))


@scm_bp.put("/reglas-empaque/<int:revision_id>")
def reglas_empaque_actualizar(revision_id):
    return jsonify(update_packaging_rule(
        db.session,
        actor_id=_actor_id(),
        revision_id=revision_id,
        data=_json_body(),
    ))


@scm_bp.post("/reglas-empaque/<int:revision_id>/aprobar")
def reglas_empaque_aprobar(revision_id):
    return jsonify(approve_packaging_rule(
        db.session,
        actor_id=_actor_id(),
        revision_id=revision_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


@scm_bp.post("/reglas-empaque/<int:revision_id>/publicar")
def reglas_empaque_publicar(revision_id):
    return jsonify(publish_packaging_rule_directly(
        db.session,
        actor_id=_actor_id(),
        revision_id=revision_id,
        operation_id=_idempotency_key(),
        data=_json_body(),
    ))


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
