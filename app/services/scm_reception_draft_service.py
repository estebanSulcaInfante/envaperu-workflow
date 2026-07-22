from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.scm_auditoria import ScmEvento
from app.models.scm_catalogos import (
    MODALIDAD_SEGUNDA,
    MODALIDAD_VIRGEN,
    ScmMaterial,
    ScmProveedor,
)
from app.models.scm_recepcion import (
    ESTADO_RECEPCION_BORRADOR,
    ScmDocumentoProveedor,
    ScmPesajeBolsa,
    ScmRecepcion,
    ScmRecepcionDocumento,
    ScmRecepcionLinea,
    TIPOS_DOCUMENTO_PROVEEDOR,
)
from app.services.scm_service_support import (
    ScmServiceError,
    actor_snapshot,
    expected_version,
    load_actor,
    positive_integer,
    positive_kg,
    reject_no_changes,
    reject_unknown_fields,
    require_patch_field,
    required_text,
    stable_code,
)


DOCUMENT_MUTABLE_FIELDS = {
    "fecha_emision",
    "cantidad_total_documental_kg",
    "referencia",
    "observacion",
}


def _optional_text(value, *, field, max_length):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScmServiceError(
            "TEXT_FIELD_REQUIRED",
            f"El campo {field} debe ser texto.",
            status_code=400,
            details={"field": field},
        )
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ScmServiceError(
            "FIELD_TOO_LONG",
            f"El campo {field} supera la longitud permitida.",
            status_code=400,
            details={"field": field, "max_length": max_length},
        )
    return normalized


def _date(value, *, field):
    if not isinstance(value, str):
        raise ScmServiceError(
            "INVALID_DATE",
            f"El campo {field} debe usar YYYY-MM-DD.",
            status_code=400,
            details={"field": field},
        )
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ScmServiceError(
            "INVALID_DATE",
            f"El campo {field} debe usar YYYY-MM-DD.",
            status_code=400,
            details={"field": field},
        ) from error


def _document_type(value):
    normalized = stable_code(value, field="tipo", max_length=24)
    if normalized not in TIPOS_DOCUMENTO_PROVEEDOR:
        raise ScmServiceError(
            "INVALID_DOCUMENT_TYPE",
            "El tipo de documento de proveedor no es valido.",
            status_code=422,
            details={"allowed": list(TIPOS_DOCUMENTO_PROVEEDOR)},
        )
    return normalized


def _load_active_provider(session, provider_id):
    provider = session.get(
        ScmProveedor,
        positive_integer(provider_id, field="proveedor_id"),
    )
    if provider is None:
        raise ScmServiceError(
            "PROVIDER_NOT_FOUND",
            "El proveedor no existe.",
            status_code=404,
        )
    if not provider.activo:
        raise ScmServiceError(
            "INACTIVE_PROVIDER",
            "El proveedor debe estar activo.",
            status_code=422,
        )
    return provider


def _event(aggregate, actor, event_type, *, before=None, after=None):
    return ScmEvento(
        aggregate_type=(
            "SCM_DOCUMENTO_PROVEEDOR"
            if isinstance(aggregate, ScmDocumentoProveedor)
            else "SCM_RECEPCION"
        ),
        aggregate_id=aggregate.id,
        tipo=event_type,
        actor_id=actor.id,
        actor_snapshot=actor_snapshot(actor),
        before_json=before,
        after_json=after,
    )


def list_supplier_documents(session, *, actor_id, provider_id=None):
    load_actor(session, actor_id)
    statement = select(ScmDocumentoProveedor)
    if provider_id is not None:
        try:
            parsed_provider_id = int(provider_id)
        except (TypeError, ValueError):
            parsed_provider_id = None
        if parsed_provider_id is None or parsed_provider_id <= 0:
            raise ScmServiceError(
                "INVALID_PROVIDER_FILTER",
                "El filtro proveedor_id debe ser un entero positivo.",
                status_code=400,
            )
        statement = statement.where(
            ScmDocumentoProveedor.proveedor_id
            == parsed_provider_id
        )
    documents = session.scalars(
        statement.order_by(
            ScmDocumentoProveedor.fecha_emision.desc(),
            ScmDocumentoProveedor.id.desc(),
        )
    ).all()
    return {"items": [item.to_dict() for item in documents]}


def get_supplier_document(session, *, actor_id, document_id):
    load_actor(session, actor_id)
    document = session.get(ScmDocumentoProveedor, document_id)
    if document is None:
        raise ScmServiceError(
            "SUPPLIER_DOCUMENT_NOT_FOUND",
            "El documento del proveedor no existe.",
            status_code=404,
        )
    return document.to_dict()


def create_supplier_document(session, *, actor_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="DOCUMENTO_PROVEEDOR_REGISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed={
                "proveedor_id",
                "tipo",
                "serie",
                "numero",
                "fecha_emision",
                "cantidad_total_documental_kg",
                "referencia",
                "observacion",
            },
        )
        provider = _load_active_provider(session, data.get("proveedor_id"))
        document_type = _document_type(data.get("tipo"))
        series = stable_code(data.get("serie"), field="serie", max_length=32)
        number = stable_code(data.get("numero"), field="numero", max_length=64)
        issued = _date(data.get("fecha_emision"), field="fecha_emision")
        total = (
            positive_kg(
                data["cantidad_total_documental_kg"],
                field="cantidad_total_documental_kg",
            )
            if data.get("cantidad_total_documental_kg") is not None
            else None
        )
        conflict = session.scalar(
            select(ScmDocumentoProveedor.id).where(
                ScmDocumentoProveedor.proveedor_id == provider.id,
                ScmDocumentoProveedor.tipo == document_type,
                ScmDocumentoProveedor.serie_normalizada == series,
                ScmDocumentoProveedor.numero_normalizado == number,
            )
        )
        if conflict is not None:
            raise ScmServiceError(
                "SUPPLIER_DOCUMENT_CONFLICT",
                "El documento del proveedor ya existe.",
                status_code=409,
            )

        document = ScmDocumentoProveedor(
            proveedor_id=provider.id,
            tipo=document_type,
            serie_normalizada=series,
            numero_normalizado=number,
            fecha_emision=issued,
            cantidad_total_documental_kg=total,
            referencia=_optional_text(
                data.get("referencia"), field="referencia", max_length=128
            ),
            observacion=_optional_text(
                data.get("observacion"), field="observacion", max_length=2000
            ),
        )
        session.add(document)
        session.flush()
        payload = document.to_dict()
        session.add(_event(document, actor, "DOCUMENTO_PROVEEDOR_CREADO", after=payload))
        session.commit()
        return payload
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "SUPPLIER_DOCUMENT_CONFLICT",
            "El documento entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_supplier_document(session, *, actor_id, document_id, data):
    try:
        actor = load_actor(
            session,
            actor_id,
            capability="DOCUMENTO_PROVEEDOR_REGISTRAR",
        )
        reject_unknown_fields(
            data,
            allowed=DOCUMENT_MUTABLE_FIELDS
            | {"version", "proveedor_id", "tipo", "serie", "numero"},
        )
        document = session.scalar(
            select(ScmDocumentoProveedor)
            .where(ScmDocumentoProveedor.id == document_id)
            .with_for_update()
        )
        if document is None:
            raise ScmServiceError(
                "SUPPLIER_DOCUMENT_NOT_FOUND",
                "El documento del proveedor no existe.",
                status_code=404,
            )
        received_version = expected_version(data.get("version"))
        if document.version != received_version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version del documento esta desactualizada.",
                status_code=409,
                details={"expected": document.version, "received": received_version},
            )
        immutable_checks = {
            "proveedor_id": document.proveedor_id,
            "tipo": document.tipo,
            "serie": document.serie_normalizada,
            "numero": document.numero_normalizado,
        }
        for field, current in immutable_checks.items():
            if field not in data:
                continue
            value = data[field]
            normalized = (
                positive_integer(value, field=field)
                if field == "proveedor_id"
                else _document_type(value)
                if field == "tipo"
                else stable_code(value, field=field, max_length=64)
            )
            if normalized != current:
                raise ScmServiceError(
                    "IMMUTABLE_DOCUMENT_IDENTITY",
                    "La identidad externa del documento no puede modificarse.",
                    status_code=422,
                    details={"field": field},
                )
        require_patch_field(data, mutable=DOCUMENT_MUTABLE_FIELDS)
        before = document.to_dict()
        issued = (
            _date(data["fecha_emision"], field="fecha_emision")
            if "fecha_emision" in data
            else document.fecha_emision
        )
        total = (
            positive_kg(
                data["cantidad_total_documental_kg"],
                field="cantidad_total_documental_kg",
            )
            if data.get("cantidad_total_documental_kg") is not None
            else None
            if "cantidad_total_documental_kg" in data
            else document.cantidad_total_documental_kg
        )
        reference = (
            _optional_text(data["referencia"], field="referencia", max_length=128)
            if "referencia" in data
            else document.referencia
        )
        observation = (
            _optional_text(data["observacion"], field="observacion", max_length=2000)
            if "observacion" in data
            else document.observacion
        )
        if not any((
            issued != document.fecha_emision,
            total != document.cantidad_total_documental_kg,
            reference != document.referencia,
            observation != document.observacion,
        )):
            reject_no_changes()
        document.fecha_emision = issued
        document.cantidad_total_documental_kg = total
        document.referencia = reference
        document.observacion = observation
        document.version += 1
        session.flush()
        payload = document.to_dict()
        session.add(_event(document, actor, "DOCUMENTO_PROVEEDOR_ACTUALIZADO", before=before, after=payload))
        session.commit()
        return payload
    except ScmServiceError:
        session.rollback()
        raise


def _document_ids(value):
    if not isinstance(value, list) or not value:
        raise ScmServiceError(
            "DOCUMENTS_REQUIRED",
            "La recepcion requiere al menos un documento de proveedor.",
            status_code=422,
        )
    result = [positive_integer(item, field="documentos_ids") for item in value]
    if len(result) != len(set(result)):
        raise ScmServiceError(
            "DUPLICATE_DOCUMENT",
            "Un documento no puede repetirse en la misma recepcion.",
            status_code=422,
        )
    return result


def _load_documents(session, ids, *, provider_id):
    documents = session.scalars(
        select(ScmDocumentoProveedor).where(ScmDocumentoProveedor.id.in_(ids))
    ).all()
    by_id = {item.id: item for item in documents}
    missing = [item for item in ids if item not in by_id]
    if missing:
        raise ScmServiceError(
            "SUPPLIER_DOCUMENT_NOT_FOUND",
            "Uno o mas documentos no existen.",
            status_code=404,
            details={"ids": missing},
        )
    if any(item.proveedor_id != provider_id for item in documents):
        raise ScmServiceError(
            "DOCUMENT_PROVIDER_MISMATCH",
            "Todos los documentos deben pertenecer al proveedor de la recepcion.",
            status_code=422,
        )
    return [by_id[item] for item in ids]


def _build_lines(session, line_data, *, actor_id):
    if not isinstance(line_data, list) or not line_data:
        raise ScmServiceError(
            "RECEPTION_LINES_REQUIRED",
            "La recepcion requiere al menos una linea.",
            status_code=422,
        )
    line_numbers = []
    result = []
    for raw in line_data:
        if not isinstance(raw, dict):
            raise ScmServiceError(
                "RECEPTION_LINE_OBJECT_REQUIRED",
                "Cada linea de recepcion debe ser un objeto.",
                status_code=400,
            )
        reject_unknown_fields(
            raw,
            allowed={
                "numero_linea",
                "material_id",
                "bultos_recibidos",
                "cantidad_documental_kg",
                "observacion",
                "pesajes_bolsa",
            },
        )
        number = positive_integer(raw.get("numero_linea"), field="numero_linea")
        line_numbers.append(number)
        material_id = positive_integer(raw.get("material_id"), field="material_id")
        material = session.get(ScmMaterial, material_id)
        if material is None:
            raise ScmServiceError("MATERIAL_NOT_FOUND", "El material no existe.", status_code=404)
        category = material.categoria_recepcion
        if (
            not material.activo
            or category is None
            or not category.activo
            or not category.recepcion_habilitada
            or category.modalidad_default not in (MODALIDAD_VIRGEN, MODALIDAD_SEGUNDA)
        ):
            raise ScmServiceError(
                "MATERIAL_NOT_RECEIVABLE",
                "El material no esta habilitado para recepcion.",
                status_code=422,
                details={"material_id": material.id},
            )
        packages = positive_integer(raw.get("bultos_recibidos"), field="bultos_recibidos")
        documentary = (
            positive_kg(raw["cantidad_documental_kg"], field="cantidad_documental_kg")
            if raw.get("cantidad_documental_kg") is not None
            else None
        )
        weights = raw.get("pesajes_bolsa", [])
        if not isinstance(weights, list):
            raise ScmServiceError(
                "BAG_WEIGHTS_ARRAY_REQUIRED",
                "pesajes_bolsa debe ser una lista.",
                status_code=400,
            )
        line = ScmRecepcionLinea(
            numero_linea=number,
            material_id=material.id,
            modalidad=category.modalidad_default,
            bultos_recibidos=packages,
            cantidad_documental_kg=documentary,
            observacion=_optional_text(raw.get("observacion"), field="observacion", max_length=2000),
        )
        if category.modalidad_default == MODALIDAD_VIRGEN:
            if weights:
                raise ScmServiceError(
                    "VIRGIN_BAG_WEIGHTS_FORBIDDEN",
                    "El material virgen no se repesa bolsa por bolsa.",
                    status_code=422,
                )
            line.cantidad_medida_kg = None
        else:
            if len(weights) != packages:
                raise ScmServiceError(
                    "BAG_WEIGHT_COUNT_MISMATCH",
                    "Segunda requiere un pesaje por cada bolsa recibida.",
                    status_code=422,
                    details={"bultos_recibidos": packages, "pesajes": len(weights)},
                )
            seen = set()
            total = Decimal("0.000")
            for weight_data in weights:
                if not isinstance(weight_data, dict):
                    raise ScmServiceError(
                        "BAG_WEIGHT_OBJECT_REQUIRED",
                        "Cada pesaje debe ser un objeto.",
                        status_code=400,
                    )
                reject_unknown_fields(
                    weight_data,
                    allowed={"secuencia", "peso_kg", "balanza_codigo_snapshot"},
                )
                sequence = positive_integer(weight_data.get("secuencia"), field="secuencia")
                if sequence in seen:
                    raise ScmServiceError(
                        "DUPLICATE_BAG_SEQUENCE",
                        "La secuencia de bolsa no puede repetirse.",
                        status_code=422,
                    )
                seen.add(sequence)
                weight = positive_kg(weight_data.get("peso_kg"), field="peso_kg")
                total += weight
                line.pesajes.append(ScmPesajeBolsa(
                    secuencia=sequence,
                    peso_kg=weight,
                    balanza_codigo_snapshot=_optional_text(
                        weight_data.get("balanza_codigo_snapshot"),
                        field="balanza_codigo_snapshot",
                        max_length=64,
                    ),
                    registrado_por_id=actor_id,
                ))
            if seen != set(range(1, packages + 1)):
                raise ScmServiceError(
                    "NON_CONTIGUOUS_BAG_SEQUENCE",
                    "Las bolsas deben numerarse consecutivamente desde 1.",
                    status_code=422,
                )
            line.cantidad_medida_kg = positive_kg(
                total,
                field="cantidad_medida_kg",
            )
        result.append(line)
    if len(line_numbers) != len(set(line_numbers)):
        raise ScmServiceError(
            "DUPLICATE_RECEPTION_LINE",
            "El numero de linea no puede repetirse.",
            status_code=422,
        )
    return result


def serialize_reception(reception):
    payload = reception.to_dict()
    payload["documentos"] = [link.documento.to_dict() for link in reception.documentos]
    payload["lineas"] = []
    for line in reception.lineas:
        item = line.to_dict()
        item["material_codigo"] = line.material.codigo
        item["material_nombre"] = line.material.nombre
        payload["lineas"].append(item)
    return payload


def _line_shape(line):
    return {
        "numero_linea": line.numero_linea,
        "material_id": line.material_id,
        "modalidad": line.modalidad,
        "bultos_recibidos": line.bultos_recibidos,
        "cantidad_documental_kg": (
            str(line.cantidad_documental_kg)
            if line.cantidad_documental_kg is not None
            else None
        ),
        "cantidad_medida_kg": (
            str(line.cantidad_medida_kg)
            if line.cantidad_medida_kg is not None
            else None
        ),
        "observacion": line.observacion,
        "pesajes_bolsa": [
            {
                "secuencia": item.secuencia,
                "peso_kg": str(item.peso_kg),
                "balanza_codigo_snapshot": item.balanza_codigo_snapshot,
            }
            for item in sorted(line.pesajes, key=lambda value: value.secuencia)
        ],
    }


def list_reception_drafts(session, *, actor_id):
    load_actor(session, actor_id)
    receptions = session.scalars(
        select(ScmRecepcion).order_by(ScmRecepcion.id.desc())
    ).all()
    return {"items": [serialize_reception(item) for item in receptions]}


def get_reception_draft(session, *, actor_id, reception_id):
    load_actor(session, actor_id)
    reception = session.get(ScmRecepcion, reception_id)
    if reception is None:
        raise ScmServiceError("RECEPTION_NOT_FOUND", "La recepcion no existe.", status_code=404)
    return serialize_reception(reception)


def create_reception_draft(session, *, actor_id, data):
    try:
        actor = load_actor(session, actor_id, capability="RECEPCION_CONFIRMAR")
        reject_unknown_fields(
            data,
            allowed={"codigo", "proveedor_id", "documentos_ids", "observacion", "lineas"},
        )
        code = stable_code(data.get("codigo"))
        provider = _load_active_provider(session, data.get("proveedor_id"))
        if session.scalar(select(ScmRecepcion.id).where(ScmRecepcion.codigo == code)) is not None:
            raise ScmServiceError("RECEPTION_CODE_CONFLICT", "El codigo de recepcion ya existe.", status_code=409)
        documents = _load_documents(
            session,
            _document_ids(data.get("documentos_ids")),
            provider_id=provider.id,
        )
        lines = _build_lines(session, data.get("lineas"), actor_id=actor.id)
        reception = ScmRecepcion(
            codigo=code,
            proveedor_id=provider.id,
            recibida_por_id=actor.id,
            observacion=_optional_text(data.get("observacion"), field="observacion", max_length=2000),
        )
        reception.documentos = [ScmRecepcionDocumento(documento=item) for item in documents]
        reception.lineas = lines
        session.add(reception)
        session.flush()
        payload = serialize_reception(reception)
        session.add(_event(reception, actor, "RECEPCION_BORRADOR_CREADA", after=payload))
        session.commit()
        return payload
    except ScmServiceError:
        session.rollback()
        raise
    except IntegrityError as error:
        session.rollback()
        raise ScmServiceError(
            "RECEPTION_CONFLICT",
            "La recepcion entra en conflicto con otro registro.",
            status_code=409,
        ) from error


def update_reception_draft(session, *, actor_id, reception_id, data):
    try:
        actor = load_actor(session, actor_id, capability="RECEPCION_CONFIRMAR")
        reject_unknown_fields(
            data,
            allowed={
                "version", "codigo", "proveedor_id", "documentos_ids", "observacion", "lineas"
            },
        )
        reception = session.scalar(
            select(ScmRecepcion).where(ScmRecepcion.id == reception_id).with_for_update()
        )
        if reception is None:
            raise ScmServiceError("RECEPTION_NOT_FOUND", "La recepcion no existe.", status_code=404)
        if reception.estado != ESTADO_RECEPCION_BORRADOR:
            raise ScmServiceError(
                "RECEPTION_NOT_EDITABLE",
                "Solo una recepcion en BORRADOR puede modificarse.",
                status_code=409,
            )
        received_version = expected_version(data.get("version"))
        if reception.version != received_version:
            raise ScmServiceError(
                "STALE_VERSION",
                "La version de la recepcion esta desactualizada.",
                status_code=409,
                details={"expected": reception.version, "received": received_version},
            )
        if "codigo" in data and stable_code(data["codigo"]) != reception.codigo:
            raise ScmServiceError(
                "IMMUTABLE_RECEPTION_CODE",
                "El codigo estable de la recepcion no puede modificarse.",
                status_code=422,
            )
        if "proveedor_id" in data and positive_integer(data["proveedor_id"], field="proveedor_id") != reception.proveedor_id:
            raise ScmServiceError(
                "IMMUTABLE_RECEPTION_PROVIDER",
                "El proveedor de la recepcion no puede modificarse.",
                status_code=422,
            )
        require_patch_field(data, mutable={"documentos_ids", "observacion", "lineas"})
        before = serialize_reception(reception)
        changed = False
        if "documentos_ids" in data:
            ids = _document_ids(data["documentos_ids"])
            documents = _load_documents(session, ids, provider_id=reception.proveedor_id)
            if sorted(ids) != sorted(item.documento_id for item in reception.documentos):
                for link in list(reception.documentos):
                    session.delete(link)
                session.flush()
                reception.documentos = [ScmRecepcionDocumento(documento=item) for item in documents]
                changed = True
        if "lineas" in data:
            replacement = _build_lines(session, data["lineas"], actor_id=actor.id)
            current_shape = [_line_shape(line) for line in reception.lineas]
            replacement_shape = [_line_shape(line) for line in replacement]
            if current_shape != replacement_shape:
                for line in list(reception.lineas):
                    for weight in list(line.pesajes):
                        session.delete(weight)
                    session.flush()
                    session.delete(line)
                session.flush()
                reception.lineas = replacement
                changed = True
        if "observacion" in data:
            observation = _optional_text(data["observacion"], field="observacion", max_length=2000)
            if observation != reception.observacion:
                reception.observacion = observation
                changed = True
        if not changed:
            reject_no_changes()
        reception.version += 1
        session.flush()
        payload = serialize_reception(reception)
        session.add(_event(reception, actor, "RECEPCION_BORRADOR_ACTUALIZADA", before=before, after=payload))
        session.commit()
        return payload
    except ScmServiceError:
        session.rollback()
        raise
