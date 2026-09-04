"""Read model canonico para supervision de produccion SCM.

La consulta pagina primero cabeceras OT y carga sus hijos en lotes. No usa las
relaciones ORM durante la serializacion, para que el numero de consultas no
crezca con la cantidad de OT, trabajos o mangas de la pagina.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import String, and_, cast, exists, func, or_, select
from sqlalchemy.orm import aliased, noload

from app.models.maquina import Maquina
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_ot import (
    ScmAnulacionPesajeManga,
    ScmAsignacionPersonalTrabajoOt,
    ScmCorreccionPesajeManga,
    ScmEtiquetaManga,
    ScmManga,
    ScmPesajeManga,
    ScmTrabajoColor,
    ScmTrabajoOt,
)
from app.models.scm_production_orders import (
    ScmOrdenFabricacion,
    ScmOrdenOperacion,
    ScmOrdenOperacionSalida,
    ScmOrdenProduccion,
    ScmPlanProduccion,
)
from app.models.scm_articulos import ScmArticulo
from app.models.scm_reproceso import ScmAlertaOperativa
from app.models.scm_rutas import ScmCentroTrabajo
from app.models.scm_warehouse import ScmExistenciaManga
from app.models.trabajador import Trabajador
from app.services.scm_service_support import ScmServiceError, load_actor


DEFAULT_LIMIT = 25
MAX_LIMIT = 100
CURSOR_VERSION = 1
OPEN_ALERT_STATES = ("ABIERTA", "RECONOCIDA")
PENDING_WEIGHING_STATES = (
    "PREETIQUETADA",
    "CERRADA_ARMADO_PENDIENTE_PESAJE",
)
PENDING_RECEIPT_STATES = ("PENDIENTE_RECEPCION_ALMACEN",)
OPEN_MANGA_STATES = (
    "PLANIFICADA",
    "PREETIQUETADA",
    "EN_ARMADO",
    "CERRADA_ARMADO_PENDIENTE_PESAJE",
)
STAGE_VALUES = {
    "PLANIFICADA",
    "EN_EJECUCION",
    "PAUSADA",
    "PENDIENTE_PESAJE",
    "PENDIENTE_RECEPCION",
    "RECIBIDA",
    "CERRADA",
    "ANULADA",
}


def utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _decimal(value, default=Decimal("0")):
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _number(value):
    value = _decimal(value)
    return float(value)


def _parse_date(value, *, field):
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_DATE",
            f"El filtro {field} debe usar YYYY-MM-DD.",
            status_code=400,
            details={"field": field},
        ) from error


def _parse_bool(value, *, field):
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "si", "sí"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ScmServiceError(
        "INVALID_OBSERVABILITY_BOOLEAN",
        f"El filtro {field} debe ser true o false.",
        status_code=400,
        details={"field": field},
    )


def _text(value, *, upper=False):
    normalized = str(value).strip() if value not in (None, "") else None
    return normalized.upper() if normalized and upper else normalized


def _normalize_type(value):
    normalized = _text(value, upper=True)
    if normalized is None:
        return None
    aliases = {
        "FABRICACION": "FABRICACION",
        "OF": "FABRICACION",
        "ARMADO": "ENSAMBLE",
        "ENSAMBLE": "ENSAMBLE",
        "OA": "ENSAMBLE",
    }
    if normalized not in aliases:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_OT_TYPE",
            "El tipo de OT debe ser FABRICACION o ARMADO.",
            status_code=400,
        )
    return aliases[normalized]


def _normalize_filters(filters):
    raw = dict(filters or {})
    start = _parse_date(
        raw.get("fecha_desde") or raw.get("desde"), field="fecha_desde"
    )
    end = _parse_date(
        raw.get("fecha_hasta") or raw.get("hasta"), field="fecha_hasta"
    )
    if start and end and start > end:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_DATE_RANGE",
            "fecha_desde no puede ser posterior a fecha_hasta.",
            status_code=400,
        )
    try:
        limit = int(raw.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError) as error:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_LIMIT",
            "limit debe ser un entero entre 1 y 100.",
            status_code=400,
        ) from error
    if limit < 1 or limit > MAX_LIMIT:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_LIMIT",
            "limit debe estar entre 1 y 100.",
            status_code=400,
        )
    sort = _text(raw.get("sort"), upper=True) or "FECHA_DESC"
    sort_aliases = {
        "FECHA_DESC": "FECHA_DESC",
        "FECHA_OPERATIVA_DESC": "FECHA_DESC",
        "FECHA_ASC": "FECHA_ASC",
        "FECHA_OPERATIVA_ASC": "FECHA_ASC",
    }
    if sort not in sort_aliases:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_SORT",
            "sort debe ser FECHA_DESC o FECHA_ASC.",
            status_code=400,
        )
    quick = _text(raw.get("quick"), upper=True)
    if quick and quick not in {
        "EN_EJECUCION",
        "PAUSADAS",
        "PENDIENTES_PESAJE",
        "ATRASADAS",
    }:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_QUICK_FILTER",
            "El filtro rapido no es valido.",
            status_code=400,
        )
    return {
        "fecha_desde": start,
        "fecha_hasta": end,
        "tipo_ot": _normalize_type(raw.get("tipo_ot") or raw.get("tipo")),
        "estado_documental": _text(
            raw.get("estado_documental") or raw.get("estado"), upper=True
        ),
        "estado_operativo": _text(raw.get("estado_operativo"), upper=True),
        "turno": _text(raw.get("turno"), upper=True),
        "recurso": _text(raw.get("recurso_id") or raw.get("recurso")),
        "responsable": _text(
            raw.get("responsable_id") or raw.get("responsable")
        ),
        "op": _text(raw.get("op")),
        "orden": _text(raw.get("orden") or raw.get("of_oa")),
        "ot": _text(raw.get("ot")),
        "color": _text(raw.get("color")),
        "manga": _text(raw.get("manga") or raw.get("codigo_manga")),
        "estado_manga": _text(raw.get("estado_manga"), upper=True),
        "articulo": _text(raw.get("articulo")),
        "q": _text(raw.get("q") or raw.get("busqueda")),
        "pendientes_pesaje": _parse_bool(
            raw.get("pendientes_pesaje"), field="pendientes_pesaje"
        ),
        "en_ejecucion": _parse_bool(
            raw.get("en_ejecucion"), field="en_ejecucion"
        ),
        "pausadas": _parse_bool(raw.get("pausadas"), field="pausadas"),
        "atrasadas": _parse_bool(raw.get("atrasadas"), field="atrasadas"),
        "pendientes_almacen": _parse_bool(
            raw.get("pendientes_almacen")
            if "pendientes_almacen" in raw
            else raw.get("almacen"),
            field="pendientes_almacen",
        ),
        "alertas": _parse_bool(raw.get("alertas"), field="alertas"),
        "quick": quick,
        "cursor": _text(raw.get("cursor")),
        "limit": limit,
        "sort": sort_aliases[sort],
    }


def _fingerprint(filters):
    keys = (
        "fecha_desde",
        "fecha_hasta",
        "tipo_ot",
        "estado_documental",
        "estado_operativo",
        "turno",
        "recurso",
        "responsable",
        "op",
        "orden",
        "ot",
        "color",
        "manga",
        "estado_manga",
        "articulo",
        "q",
        "pendientes_pesaje",
        "en_ejecucion",
        "pausadas",
        "atrasadas",
        "pendientes_almacen",
        "alertas",
        "quick",
        "sort",
    )
    stable = {
        key: (
            filters[key].isoformat()
            if isinstance(filters.get(key), date)
            else filters.get(key)
        )
        for key in keys
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _encode_position_cursor(
    *, operational_date, item_id, filters, as_of
):
    payload = {
        "v": CURSOR_VERSION,
        "fecha": operational_date.isoformat(),
        "id": item_id,
        "f": _fingerprint(filters),
        "as_of": _iso(as_of),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode()
    return encoded.rstrip("=")


def _encode_cursor(*, item, filters, as_of):
    return _encode_position_cursor(
        operational_date=item.fecha,
        item_id=item.id,
        filters=filters,
        as_of=as_of,
    )


def _decode_cursor(value, *, filters):
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if payload.get("v") != CURSOR_VERSION:
            raise ValueError("version")
        operational_date = date.fromisoformat(payload["fecha"])
        item_id = int(payload["id"])
        as_of = datetime.fromisoformat(payload["as_of"])
        cursor_fingerprint = payload["f"]
    except (
        KeyError,
        TypeError,
        ValueError,
        UnicodeDecodeError,
        binascii.Error,
        json.JSONDecodeError,
    ) as error:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_CURSOR",
            "El cursor de observabilidad no es valido.",
            status_code=400,
        ) from error
    if cursor_fingerprint != _fingerprint(filters):
        raise ScmServiceError(
            "OBSERVABILITY_CURSOR_FILTER_MISMATCH",
            "El cursor pertenece a un conjunto de filtros diferente.",
            status_code=409,
        )
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    return operational_date, item_id, as_of.astimezone(timezone.utc)


def _visible_type(value):
    return "ARMADO" if value == "ENSAMBLE" else "FABRICACION"


def _operation_document_type(value):
    return "OA" if value == "ENSAMBLE" else "OF"


def _work_exists(*conditions):
    return exists(
        select(1)
        .select_from(ScmTrabajoOt)
        .where(
            ScmTrabajoOt.orden_trabajo_id == RegistroDiarioProduccion.id,
            *conditions,
        )
    )


def _manga_exists(*conditions):
    return exists(
        select(1)
        .select_from(ScmManga)
        .where(
            ScmManga.ot_id == RegistroDiarioProduccion.id,
            *conditions,
        )
    )


def _operation_exists(*conditions):
    direct = exists(
        select(1)
        .select_from(ScmOrdenOperacion)
        .where(
            ScmOrdenOperacion.id
            == RegistroDiarioProduccion.orden_operacion_id,
            *conditions,
        )
    )
    through_work = exists(
        select(1)
        .select_from(ScmTrabajoOt)
        .join(
            ScmOrdenOperacion,
            ScmOrdenOperacion.id == ScmTrabajoOt.orden_operacion_id,
        )
        .where(
            ScmTrabajoOt.orden_trabajo_id
            == RegistroDiarioProduccion.id,
            *conditions,
        )
    )
    return or_(direct, through_work)


def _upstream_op_exists(search_value=None, *, overdue=False):
    direct = (
        select(1)
        .select_from(ScmOrdenOperacion)
        .join(
            ScmPlanProduccion,
            ScmPlanProduccion.id == ScmOrdenOperacion.plan_produccion_id,
        )
        .join(
            ScmOrdenProduccion,
            ScmOrdenProduccion.id
            == ScmPlanProduccion.orden_produccion_id,
        )
        .where(
            ScmOrdenOperacion.id
            == RegistroDiarioProduccion.orden_operacion_id
        )
    )
    through_work = (
        select(1)
        .select_from(ScmTrabajoOt)
        .join(
            ScmOrdenOperacion,
            ScmOrdenOperacion.id == ScmTrabajoOt.orden_operacion_id,
        )
        .join(
            ScmPlanProduccion,
            ScmPlanProduccion.id == ScmOrdenOperacion.plan_produccion_id,
        )
        .join(
            ScmOrdenProduccion,
            ScmOrdenProduccion.id
            == ScmPlanProduccion.orden_produccion_id,
        )
        .where(
            ScmTrabajoOt.orden_trabajo_id
            == RegistroDiarioProduccion.id
        )
    )
    if search_value is not None:
        pattern = f"%{search_value}%"
        direct = direct.where(ScmOrdenProduccion.codigo.ilike(pattern))
        through_work = through_work.where(
            ScmOrdenProduccion.codigo.ilike(pattern)
        )
    if overdue:
        today = datetime.now(ZoneInfo("America/Lima")).date()
        direct = direct.where(ScmOrdenProduccion.fecha_necesidad < today)
        through_work = through_work.where(
            ScmOrdenProduccion.fecha_necesidad < today
        )
    return or_(exists(direct), exists(through_work))


def _responsible_exists(search_value):
    worker = aliased(Trabajador)
    pattern = f"%{search_value}%"
    worker_match = or_(
        worker.codigo.ilike(pattern),
        worker.nombres.ilike(pattern),
        worker.apellidos.ilike(pattern),
        worker.nombre_corto.ilike(pattern),
    )
    try:
        worker_id = int(search_value)
    except (TypeError, ValueError):
        worker_id = None
    if worker_id is not None:
        worker_match = or_(worker.id == worker_id, worker_match)
    header_worker = exists(
        select(1)
        .select_from(worker)
        .where(
            worker_match,
            or_(
                worker.id == RegistroDiarioProduccion.responsable_id,
                worker.id == RegistroDiarioProduccion.maquinista_previsto_id,
            ),
        )
    )
    assigned_worker = aliased(Trabajador)
    assigned_match = or_(
        assigned_worker.codigo.ilike(pattern),
        assigned_worker.nombres.ilike(pattern),
        assigned_worker.apellidos.ilike(pattern),
        assigned_worker.nombre_corto.ilike(pattern),
    )
    if worker_id is not None:
        assigned_match = or_(assigned_worker.id == worker_id, assigned_match)
    assignment = exists(
        select(1)
        .select_from(ScmTrabajoOt)
        .join(
            ScmAsignacionPersonalTrabajoOt,
            ScmAsignacionPersonalTrabajoOt.trabajo_ot_id == ScmTrabajoOt.id,
        )
        .join(
            assigned_worker,
            assigned_worker.id
            == ScmAsignacionPersonalTrabajoOt.trabajador_id,
        )
        .where(
            ScmTrabajoOt.orden_trabajo_id == RegistroDiarioProduccion.id,
            ScmAsignacionPersonalTrabajoOt.estado.in_(("ACTIVA", "PREVISTA")),
            assigned_match,
        )
    )
    return or_(header_worker, assignment)


def _uuid_text_matches(text_column, uuid_column):
    """Compara UUID de forma portable (SQLite almacena hex sin guiones)."""
    return func.replace(text_column, "-", "") == func.replace(
        cast(uuid_column, String), "-", ""
    )


def _open_alert_exists():
    direct = exists(
        select(1)
        .select_from(ScmAlertaOperativa)
        .where(
            ScmAlertaOperativa.estado.in_(OPEN_ALERT_STATES),
            or_(
                and_(
                    ScmAlertaOperativa.agregado_tipo == "ORDEN_TRABAJO",
                    or_(
                        _uuid_text_matches(
                            ScmAlertaOperativa.agregado_id,
                            RegistroDiarioProduccion.public_id,
                        ),
                        ScmAlertaOperativa.agregado_id
                        == cast(RegistroDiarioProduccion.id, String),
                    ),
                ),
                and_(
                    ScmAlertaOperativa.agregado_tipo == "OT",
                    _uuid_text_matches(
                        ScmAlertaOperativa.agregado_id,
                        RegistroDiarioProduccion.public_id,
                    ),
                ),
            ),
        )
    )
    work = exists(
        select(1)
        .select_from(ScmTrabajoOt)
        .join(
            ScmAlertaOperativa,
            and_(
                ScmAlertaOperativa.agregado_tipo.in_((
                    "TRABAJO_OT",
                    "TRABAJO_COLOR",
                )),
                _uuid_text_matches(
                    ScmAlertaOperativa.agregado_id, ScmTrabajoOt.id
                ),
            ),
        )
        .where(
            ScmTrabajoOt.orden_trabajo_id == RegistroDiarioProduccion.id,
            ScmAlertaOperativa.estado.in_(OPEN_ALERT_STATES),
        )
    )
    manga = exists(
        select(1)
        .select_from(ScmManga)
        .join(
            ScmAlertaOperativa,
            and_(
                ScmAlertaOperativa.agregado_tipo == "MANGA",
                _uuid_text_matches(
                    ScmAlertaOperativa.agregado_id, ScmManga.public_id
                ),
            ),
        )
        .where(
            ScmManga.ot_id == RegistroDiarioProduccion.id,
            ScmAlertaOperativa.estado.in_(OPEN_ALERT_STATES),
        )
    )
    weighing = exists(
        select(1)
        .select_from(ScmManga)
        .join(ScmPesajeManga, ScmPesajeManga.manga_id == ScmManga.id)
        .join(
            ScmAlertaOperativa,
            and_(
                ScmAlertaOperativa.agregado_tipo == "PESAJE_MANGA",
                _uuid_text_matches(
                    ScmAlertaOperativa.agregado_id,
                    ScmPesajeManga.public_id,
                ),
            ),
        )
        .where(
            ScmManga.ot_id == RegistroDiarioProduccion.id,
            ScmAlertaOperativa.estado.in_(OPEN_ALERT_STATES),
        )
    )
    correction = exists(
        select(1)
        .select_from(ScmManga)
        .join(ScmPesajeManga, ScmPesajeManga.manga_id == ScmManga.id)
        .join(
            ScmCorreccionPesajeManga,
            ScmCorreccionPesajeManga.pesaje_id == ScmPesajeManga.id,
        )
        .join(
            ScmAlertaOperativa,
            and_(
                ScmAlertaOperativa.agregado_tipo
                == "CORRECCION_PESAJE_MANGA",
                _uuid_text_matches(
                    ScmAlertaOperativa.agregado_id,
                    ScmCorreccionPesajeManga.public_id,
                ),
            ),
        )
        .where(
            ScmManga.ot_id == RegistroDiarioProduccion.id,
            ScmAlertaOperativa.estado.in_(OPEN_ALERT_STATES),
        )
    )
    return or_(direct, work, manga, weighing, correction)


def _base_statement(filters):
    machine = aliased(Maquina)
    center = aliased(ScmCentroTrabajo)
    statement = (
        select(RegistroDiarioProduccion, machine, center)
        .options(noload("*"))
        .outerjoin(machine, machine.id == RegistroDiarioProduccion.maquina_id)
        .outerjoin(
            center,
            center.id == RegistroDiarioProduccion.centro_trabajo_id,
        )
        .where(RegistroDiarioProduccion.codigo_ot_sintetico.is_(False))
    )
    if filters["fecha_desde"]:
        statement = statement.where(
            RegistroDiarioProduccion.fecha >= filters["fecha_desde"]
        )
    if filters["fecha_hasta"]:
        statement = statement.where(
            RegistroDiarioProduccion.fecha <= filters["fecha_hasta"]
        )
    if filters["tipo_ot"]:
        statement = statement.where(
            RegistroDiarioProduccion.tipo_ot == filters["tipo_ot"]
        )
    if filters["estado_documental"]:
        statement = statement.where(
            RegistroDiarioProduccion.estado == filters["estado_documental"]
        )
    if filters["turno"]:
        statement = statement.where(
            RegistroDiarioProduccion.turno == filters["turno"]
        )
    if filters["ot"]:
        statement = statement.where(
            RegistroDiarioProduccion.codigo_ot.ilike(f"%{filters['ot']}%")
        )
    if filters["recurso"]:
        pattern = f"%{filters['recurso']}%"
        conditions = [
            machine.codigo.ilike(pattern),
            machine.nombre.ilike(pattern),
            center.codigo.ilike(pattern),
            center.nombre.ilike(pattern),
        ]
        try:
            resource_id = int(filters["recurso"])
        except (TypeError, ValueError):
            resource_id = None
        if resource_id is not None:
            conditions.extend([
                RegistroDiarioProduccion.maquina_id == resource_id,
                RegistroDiarioProduccion.centro_trabajo_id == resource_id,
            ])
        statement = statement.where(or_(*conditions))
    if filters["responsable"]:
        statement = statement.where(_responsible_exists(filters["responsable"]))
    if filters["orden"]:
        statement = statement.where(
            _operation_exists(
                ScmOrdenOperacion.codigo.ilike(f"%{filters['orden']}%")
            )
        )
    if filters["op"]:
        statement = statement.where(_upstream_op_exists(filters["op"]))
    if filters["color"]:
        statement = statement.where(exists(
            select(1)
            .select_from(ScmTrabajoOt)
            .join(
                ScmTrabajoColor,
                ScmTrabajoColor.trabajo_ot_id == ScmTrabajoOt.id,
            )
            .where(
                ScmTrabajoOt.orden_trabajo_id
                == RegistroDiarioProduccion.id,
                ScmTrabajoColor.color_nombre_snapshot.ilike(
                    f"%{filters['color']}%"
                ),
            )
        ))
    if filters["estado_operativo"]:
        desired = filters["estado_operativo"]
        if desired == "PAUSADA":
            desired = "PAUSADO"
        statement = statement.where(or_(
            RegistroDiarioProduccion.estado == desired,
            _work_exists(ScmTrabajoOt.estado == desired),
        ))
    if filters["pendientes_pesaje"] is not None:
        condition = _manga_exists(ScmManga.estado.in_(PENDING_WEIGHING_STATES))
        statement = statement.where(
            condition if filters["pendientes_pesaje"] else ~condition
        )
    if filters["pendientes_almacen"] is not None:
        condition = _manga_exists(ScmManga.estado.in_(PENDING_RECEIPT_STATES))
        statement = statement.where(
            condition if filters["pendientes_almacen"] else ~condition
        )
    if filters["alertas"] is not None:
        condition = _open_alert_exists()
        statement = statement.where(condition if filters["alertas"] else ~condition)
    if filters["quick"] == "EN_EJECUCION":
        statement = statement.where(or_(
            RegistroDiarioProduccion.estado == "EN_EJECUCION",
            _work_exists(ScmTrabajoOt.estado == "EN_EJECUCION"),
        ))
    elif filters["quick"] == "PAUSADAS":
        statement = statement.where(_work_exists(ScmTrabajoOt.estado == "PAUSADO"))
    elif filters["quick"] == "PENDIENTES_PESAJE":
        statement = statement.where(
            _manga_exists(ScmManga.estado.in_(PENDING_WEIGHING_STATES))
        )
    elif filters["quick"] == "ATRASADAS":
        statement = statement.where(
            RegistroDiarioProduccion.estado.notin_(("CERRADA", "ANULADA")),
            _upstream_op_exists(overdue=True),
        )
    if filters["en_ejecucion"] is True:
        statement = statement.where(or_(
            RegistroDiarioProduccion.estado == "EN_EJECUCION",
            _work_exists(ScmTrabajoOt.estado == "EN_EJECUCION"),
        ))
    if filters["pausadas"] is True:
        statement = statement.where(
            _work_exists(ScmTrabajoOt.estado == "PAUSADO")
        )
    if filters["atrasadas"] is True:
        statement = statement.where(
            RegistroDiarioProduccion.estado.notin_(("CERRADA", "ANULADA")),
            _upstream_op_exists(overdue=True),
        )
    if filters["q"]:
        pattern = f"%{filters['q']}%"
        color_match = exists(
            select(1)
            .select_from(ScmTrabajoOt)
            .outerjoin(
                ScmTrabajoColor,
                ScmTrabajoColor.trabajo_ot_id == ScmTrabajoOt.id,
            )
            .where(
                ScmTrabajoOt.orden_trabajo_id
                == RegistroDiarioProduccion.id,
                or_(
                    ScmTrabajoOt.codigo.ilike(pattern),
                    ScmTrabajoColor.color_nombre_snapshot.ilike(pattern),
                ),
            )
        )
        manga_match = _manga_exists(or_(
            ScmManga.codigo.ilike(pattern),
            ScmManga.articulo_codigo_snapshot.ilike(pattern),
            ScmManga.articulo_nombre_snapshot.ilike(pattern),
            ScmManga.pieza_color_sku_snapshot.ilike(pattern),
            ScmManga.color_snapshot.ilike(pattern),
        ))
        statement = statement.where(or_(
            RegistroDiarioProduccion.codigo_ot.ilike(pattern),
            RegistroDiarioProduccion.maquina_codigo_snapshot.ilike(pattern),
            RegistroDiarioProduccion.maquina_nombre_snapshot.ilike(pattern),
            machine.codigo.ilike(pattern),
            machine.nombre.ilike(pattern),
            center.codigo.ilike(pattern),
            center.nombre.ilike(pattern),
            color_match,
            manga_match,
            _operation_exists(ScmOrdenOperacion.codigo.ilike(pattern)),
            _upstream_op_exists(filters["q"]),
            _responsible_exists(filters["q"]),
        ))
    return statement


def _worker_dict(worker):
    if worker is None:
        return None
    return {
        "id": worker.id,
        "codigo": worker.codigo,
        "nombre": worker.nombre_corto or worker.nombre_completo,
    }


def _effective_weighing(weighing, correction, annulment):
    if annulment is not None:
        return {
            "public_id": str(weighing.public_id),
            "estado": "ANULADO",
            "peso_fisico_neto_kg": None,
            "neto_kg": None,
            "kg_produccion_estandar": None,
            "cantidad_confirmada_un": None,
            "pesada_at": _iso(weighing.pesada_at),
            "corregido": False,
            "corregida_at": None,
            "anulada_at": _iso(annulment.anulada_at),
        }
    projection = (
        dict(correction.result_projection_json or {})
        if correction is not None
        else {
            "peso_fisico_neto_kg": weighing.peso_fisico_neto_kg,
            "kg_produccion_ot": weighing.kg_produccion_ot,
            "cantidad_confirmada": weighing.cantidad_confirmada,
            "pesada_at": _iso(weighing.pesada_at),
        }
    )
    physical = _number(projection.get("peso_fisico_neto_kg"))
    standard = _number(projection.get("kg_produccion_ot"))
    confirmed = _number(projection.get("cantidad_confirmada"))
    return {
        "public_id": str(weighing.public_id),
        "estado": "EFECTIVO",
        "peso_fisico_neto_kg": physical,
        "neto_kg": physical,
        "kg_produccion_estandar": standard,
        "cantidad_confirmada_un": confirmed,
        "pesada_at": projection.get("pesada_at") or _iso(weighing.pesada_at),
        "corregido": correction is not None,
        "corregida_at": _iso(correction.resolved_at) if correction else None,
        "anulada_at": None,
    }


def _latest(values):
    candidates = [value for value in values if value is not None]
    if not candidates:
        return None
    normalized = [
        value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        for value in candidates
    ]
    return max(normalized)


def _build_item(
    *,
    row,
    works,
    mangas,
    assignments,
    assignments_by_id,
    operations,
    workers,
    weighings,
    corrections,
    annulments,
    existences,
    alerts,
    labels,
    visibility,
    as_of,
    detail,
):
    ot, machine, center = row
    ot_works = sorted(works.get(ot.id, []), key=lambda value: value[0].secuencia)
    ot_mangas = sorted(mangas.get(ot.id, []), key=lambda value: value.secuencia_ot)
    work_states = Counter(work.estado for work, _color in ot_works)
    active_pair = next(
        (pair for pair in ot_works if pair[0].estado == "EN_EJECUCION"),
        None,
    )
    if active_pair is None:
        active_pair = next(
            (pair for pair in ot_works if pair[0].estado == "PAUSADO"),
            None,
        )
    if active_pair is None:
        active_pair = next(
            (pair for pair in ot_works if pair[0].estado == "PLANIFICADO"),
            None,
        )
    next_pair = None
    if active_pair is not None:
        next_pair = next(
            (
                pair
                for pair in ot_works
                if pair[0].secuencia > active_pair[0].secuencia
                and pair[0].estado == "PLANIFICADO"
            ),
            None,
        )

    operation_ids = []
    if ot.orden_operacion_id:
        operation_ids.append(ot.orden_operacion_id)
    operation_ids.extend(work.orden_operacion_id for work, _color in ot_works)
    deduped_operation_ids = list(dict.fromkeys(operation_ids))
    chosen_operation_id = (
        active_pair[0].orden_operacion_id
        if active_pair is not None
        else (deduped_operation_ids[0] if deduped_operation_ids else None)
    )
    operation_rows = [
        operations[item_id]
        for item_id in deduped_operation_ids
        if item_id in operations
    ]
    chosen = operations.get(chosen_operation_id)
    upstream_warnings = []
    if not deduped_operation_ids:
        upstream_warnings.append({
            "codigo": "UPSTREAM_ORDEN_AUSENTE",
            "mensaje": "La OT no tiene OF/OA resoluble.",
        })
    elif not operation_rows:
        upstream_warnings.append({
            "codigo": "UPSTREAM_ORDEN_NO_RESOLUBLE",
            "mensaje": "La referencia de OF/OA de la OT no existe.",
        })
    elif len(operation_rows) != len(deduped_operation_ids):
        upstream_warnings.append({
            "codigo": "UPSTREAM_ORDEN_PARCIAL",
            "mensaje": "Una o mas referencias de OF/OA no son resolubles.",
        })
    if chosen_operation_id is not None and chosen is None:
        upstream_warnings.append({
            "codigo": "UPSTREAM_ORDEN_ACTUAL_NO_RESOLUBLE",
            "mensaje": "La OF/OA del trabajo actual no es resoluble.",
        })
    if len(operation_rows) > 1:
        upstream_warnings.append({
            "codigo": "UPSTREAM_MULTIPLE_ORDENES",
            "mensaje": "La jornada contiene trabajos de mas de una OF/OA.",
        })
    if chosen is not None and chosen[1] is None:
        upstream_warnings.append({
            "codigo": "UPSTREAM_PLAN_AUSENTE",
            "mensaje": "La OF/OA no tiene un plan de produccion resoluble.",
        })
    elif chosen is not None and chosen[2] is None:
        upstream_warnings.append({
            "codigo": "UPSTREAM_OP_NO_RESOLUBLE",
            "mensaje": "El plan de la OF/OA no tiene una OP resoluble.",
        })
    chosen_operation, chosen_plan, chosen_op = chosen or (None, None, None)
    order_dicts = [
        {
            "tipo": _operation_document_type(operation.tipo),
            "id": str(operation.id),
            "codigo": operation.codigo,
            "estado": operation.estado,
        }
        for operation, _plan, _op in operation_rows
    ]
    upstream = {
        "op": (
            {
                "id": str(chosen_op.id),
                "codigo": chosen_op.codigo,
                "estado": chosen_op.estado,
                "fecha_necesidad": chosen_op.fecha_necesidad.isoformat(),
            }
            if chosen_op is not None
            else None
        ),
        "orden": (
            {
                "tipo": _operation_document_type(chosen_operation.tipo),
                "id": str(chosen_operation.id),
                "codigo": chosen_operation.codigo,
                "estado": chosen_operation.estado,
            }
            if chosen_operation is not None
            else None
        ),
        "ordenes": order_dicts,
        "plan": (
            {
                "id": str(chosen_plan.id),
                "revision": chosen_plan.revision,
            }
            if chosen_plan is not None
            else None
        ),
        "warnings": upstream_warnings,
    }

    manga_states = Counter(manga.estado for manga in ot_mangas)
    received_count = manga_states["RECIBIDA"]
    pending_weighing_count = sum(
        manga_states[state] for state in PENDING_WEIGHING_STATES
    )
    pending_receipt_count = sum(
        manga_states[state] for state in PENDING_RECEIPT_STATES
    )
    manga_summary = {
        "total": len(ot_mangas),
        "abiertas": sum(manga_states[state] for state in OPEN_MANGA_STATES),
        "planificadas": manga_states["PLANIFICADA"],
        "preetiquetadas": manga_states["PREETIQUETADA"],
        "pesadas": sum(
            manga_states[state]
            for state in (
                "PESADA",
                "ETIQUETADA_FINAL",
                "PENDIENTE_RECEPCION_ALMACEN",
                "RECIBIDA",
            )
        ),
        "pendientes_pesaje": pending_weighing_count,
        "pendientes_recepcion": pending_receipt_count,
        "recibidas": received_count,
        "anuladas": manga_states["ANULADA"],
    }

    effective_weighings = []
    weighing_event_times = []
    if visibility["pesaje"]:
        for manga in ot_mangas:
            weighing = weighings.get(manga.id)
            if weighing is None:
                continue
            effective = _effective_weighing(
                weighing,
                corrections.get(weighing.id),
                annulments.get(weighing.id),
            )
            if effective["estado"] == "EFECTIVO":
                effective_weighings.append(effective)
            weighing_event_times.extend([
                weighing.pesada_at,
                corrections.get(weighing.id).resolved_at
                if corrections.get(weighing.id)
                else None,
                annulments.get(weighing.id).anulada_at
                if annulments.get(weighing.id)
                else None,
            ])
        physical = sum(
            (_decimal(item["peso_fisico_neto_kg"]) for item in effective_weighings),
            Decimal("0"),
        )
        standard = sum(
            (_decimal(item["kg_produccion_estandar"]) for item in effective_weighings),
            Decimal("0"),
        )
        last_weighed = _latest([
            weighing.pesada_at
            for manga in ot_mangas
            for weighing in [weighings.get(manga.id)]
            if weighing is not None and weighing.id not in annulments
        ])
        weighing_summary = {
            "cantidad": len(effective_weighings),
            "neto_kg": _number(physical),
            "peso_fisico_neto_kg": _number(physical),
            "kg_produccion_estandar": _number(standard),
            "ultimo_pesaje_at": _iso(last_weighed),
        }
    else:
        weighing_summary = None

    existence_values = [
        existences[manga.id]
        for manga in ot_mangas
        if manga.id in existences
    ]
    last_receipt = _latest([item.recibida_at for item in existence_values])
    warehouse_summary = {
        "pendientes_recepcion": pending_receipt_count,
        "recibidas": received_count,
        "ultima_recepcion_at": (
            _iso(last_receipt) if visibility["almacen"] else None
        ),
    }

    ot_alerts = alerts.get(ot.id, []) if visibility["alertas"] else []
    open_alerts = [item for item in ot_alerts if item.estado in OPEN_ALERT_STATES]
    alerts_summary = (
        {
            "abiertas": len(open_alerts),
            "criticas": sum(item.severidad == "CRITICA" for item in open_alerts),
        }
        if visibility["alertas"]
        else None
    )
    severity = None
    if open_alerts:
        severity = max(
            (item.severidad for item in open_alerts),
            key={"INFO": 1, "ADVERTENCIA": 2, "CRITICA": 3}.get,
        )

    event_times = [ot.created_at, ot.updated_at, ot.iniciada_at, ot.cerrada_at]
    for work, _color in ot_works:
        event_times.extend([
            work.created_at,
            work.updated_at,
            work.iniciada_at,
            work.pausada_at,
            work.completada_at,
            work.anulada_at,
        ])
    for manga in ot_mangas:
        event_times.extend([manga.created_at, manga.anulada_at])
    ot_work_ids = {work.id for work, _color in ot_works}
    for assignment in assignments_by_id.values():
        if assignment.trabajo_ot_id in ot_work_ids:
            event_times.extend([
                assignment.asignada_at,
                assignment.iniciada_at,
                assignment.finalizada_at,
            ])
    event_times.extend(weighing_event_times)
    for existence in existence_values:
        event_times.extend([existence.recibida_at, existence.calidad_at])
    event_times.extend(item.updated_at for item in ot_alerts)
    last_event = _latest(event_times)
    elapsed = None
    if last_event is not None:
        elapsed = max(
            0.0,
            round((as_of - last_event.astimezone(timezone.utc)).total_seconds() / 3600, 3),
        )
    overdue = bool(
        chosen_op
        and chosen_op.fecha_necesidad
        < datetime.now(ZoneInfo("America/Lima")).date()
        and ot.estado not in {"CERRADA", "ANULADA"}
    )

    operational_state = ot.estado
    if active_pair is not None:
        operational_state = active_pair[0].estado
    if operational_state == "PAUSADO":
        operational_state = "PAUSADA"
    if ot.estado == "ANULADA":
        stage = "ANULADA"
    elif (
        len(ot_mangas) - manga_states["ANULADA"] > 0
        and received_count == len(ot_mangas) - manga_states["ANULADA"]
    ):
        stage = "RECIBIDA"
    elif pending_receipt_count:
        stage = "PENDIENTE_RECEPCION"
    elif pending_weighing_count:
        stage = "PENDIENTE_PESAJE"
    elif operational_state == "EN_EJECUCION":
        stage = "EN_EJECUCION"
    elif operational_state == "PAUSADA":
        stage = "PAUSADA"
    elif ot.estado == "CERRADA":
        stage = "CERRADA"
    else:
        stage = "PLANIFICADA"
    assert stage in STAGE_VALUES

    blockers = []
    for warning in upstream_warnings:
        blockers.append({
            **warning,
            "severidad": "ADVERTENCIA",
        })
    if pending_weighing_count:
        blockers.append({
            "codigo": "PESAJE_PENDIENTE",
            "mensaje": f"{pending_weighing_count} manga(s) esperan pesaje.",
            "severidad": "ADVERTENCIA",
        })
    if pending_receipt_count:
        blockers.append({
            "codigo": "RECEPCION_PENDIENTE",
            "mensaje": f"{pending_receipt_count} manga(s) esperan recepcion.",
            "severidad": "ADVERTENCIA",
        })
    if alerts_summary and alerts_summary["criticas"]:
        blockers.append({
            "codigo": "ALERTA_CRITICA",
            "mensaje": "La OT tiene alertas criticas abiertas.",
            "severidad": "CRITICA",
        })

    if machine is not None:
        resource = {
            "tipo": "MAQUINA",
            "id": machine.id,
            "codigo": machine.codigo,
            "nombre": machine.nombre,
        }
    elif center is not None:
        resource = {
            "tipo": "CENTRO",
            "id": center.id,
            "codigo": center.codigo,
            "nombre": center.nombre,
        }
    else:
        resource = None
    active_assignment = (
        assignments.get(active_pair[0].id) if active_pair is not None else None
    )
    responsible = (
        workers.get(active_assignment.trabajador_id)
        if active_assignment is not None
        else workers.get(ot.responsable_id or ot.maquinista_previsto_id)
    )

    def work_dict(pair, *, include_quantities=True):
        if pair is None:
            return None
        work, color = pair
        assignment = assignments.get(work.id)
        payload = {
            "id": str(work.id),
            "codigo": work.codigo,
            "secuencia": work.secuencia,
            "color": color.color_nombre_snapshot if color else None,
            "estado": work.estado,
            "responsable": _worker_dict(
                workers.get(assignment.trabajador_id) if assignment else None
            ),
        }
        if include_quantities:
            payload.update({
                "objetivo_un": _number(work.cantidad_objetivo_un),
                "confirmado_un": _number(work.cantidad_confirmada_un),
            })
        return payload

    if ot_works:
        objective = sum(
            (_decimal(work.cantidad_objetivo_un) for work, _color in ot_works),
            Decimal("0"),
        )
        confirmed = sum(
            (_decimal(work.cantidad_confirmada_un) for work, _color in ot_works),
            Decimal("0"),
        )
    else:
        objective = _decimal(ot.cantidad_objetivo)
        confirmed = _decimal(ot.cantidad_confirmada)

    item = {
        "ot": {
            "public_id": str(ot.public_id),
            "codigo": ot.codigo_ot,
            "tipo": _visible_type(ot.tipo_ot),
            "estado_documental": ot.estado,
            "estado_operativo": operational_state,
            "fecha_operativa": ot.fecha.isoformat(),
            "turno": ot.turno,
            "version": ot.version,
        },
        "recurso": resource,
        "responsable": _worker_dict(responsible),
        "upstream": upstream,
        "trabajo_actual": work_dict(active_pair),
        "trabajo_siguiente": work_dict(next_pair, include_quantities=False),
        "trabajos_resumen": {
            "total": len(ot_works),
            "por_estado": dict(sorted(work_states.items())),
        },
        "cantidades_resumen": {
            "objetivo_un": _number(objective),
            "confirmado_un": _number(confirmed),
        },
        "mangas_resumen": manga_summary,
        "pesaje_resumen": weighing_summary,
        "almacen_resumen": warehouse_summary,
        "riesgo": {
            "atrasada": overdue,
            "horas_sin_actividad": elapsed,
            "severidad": severity,
        },
        "alertas_resumen": alerts_summary,
        "bloqueos": blockers,
        "etapa_actual": stage,
        "ultimo_evento_at": _iso(last_event),
        "visibilidad": dict(visibility),
    }

    if detail:
        mangas_by_work = defaultdict(list)
        for manga in ot_mangas:
            weighing = weighings.get(manga.id)
            weighing_payload = None
            if visibility["pesaje"] and weighing is not None:
                weighing_payload = _effective_weighing(
                    weighing,
                    corrections.get(weighing.id),
                    annulments.get(weighing.id),
                )
            existence = existences.get(manga.id)
            warehouse_payload = None
            if (
                visibility["almacen"] or visibility["calidad"]
            ) and existence is not None:
                warehouse_payload = {
                    "estado_logistico": (
                        existence.estado_logistico
                        if visibility["almacen"]
                        else None
                    ),
                    "recibida_at": (
                        _iso(existence.recibida_at)
                        if visibility["almacen"]
                        else None
                    ),
                    "cantidad_fisica_un": (
                        _number(existence.cantidad_fisica)
                        if visibility["almacen"]
                        else None
                    ),
                    "peso_neto_snapshot_kg": (
                        _number(existence.peso_neto_snapshot_kg)
                        if visibility["almacen"]
                        else None
                    ),
                    "estado_calidad": (
                        existence.estado_calidad
                        if visibility["calidad"]
                        else None
                    ),
                    "calidad_at": (
                        _iso(existence.calidad_at)
                        if visibility["calidad"]
                        else None
                    ),
                }
            latest_label = labels.get(manga.id)
            label_payload = (
                {
                    "public_id": str(latest_label.public_id),
                    "tipo": latest_label.tipo,
                    "estado": latest_label.estado,
                    "version": latest_label.version,
                }
                if latest_label is not None
                else None
            )
            historical_assignment = assignments_by_id.get(
                manga.asignacion_personal_trabajo_id
            )
            responsible_worker = workers.get(
                historical_assignment.trabajador_id
                if historical_assignment is not None
                else manga.maquinista_previsto_id
            )
            manga_payload = {
                "public_id": str(manga.public_id),
                "codigo": manga.codigo,
                "tipo": manga.tipo,
                "articulo": {
                    "codigo": manga.articulo_codigo_snapshot,
                    "nombre": manga.articulo_nombre_snapshot,
                    "sku_pieza_color": manga.pieza_color_sku_snapshot,
                },
                "color": manga.color_snapshot,
                "contenedor": {
                    "codigo": manga.tipo_contenedor_codigo_snapshot,
                    "nombre": manga.tipo_contenedor_nombre_snapshot,
                },
                "created_at": _iso(manga.created_at),
                "estado_operativo": manga.estado,
                "estado_logistico": (
                    existence.estado_logistico
                    if visibility["almacen"] and existence is not None
                    else (
                        "RECIBIDA_ALMACEN"
                        if manga.estado == "RECIBIDA"
                        else (
                            "PENDIENTE_RECEPCION"
                            if manga.estado in PENDING_RECEIPT_STATES
                            else "NO_INGRESADA"
                        )
                    )
                ),
                "cantidad_objetivo_un": _number(manga.cantidad_asignada_un),
                "cantidad_confirmada_un": (
                    _number(manga.cantidad_confirmada_un)
                    if manga.cantidad_confirmada_un is not None
                    else None
                ),
                "responsable": _worker_dict(responsible_worker),
                "etiqueta": label_payload,
                "pesaje": weighing_payload,
                "almacen": warehouse_payload,
            }
            mangas_by_work[manga.trabajo_ot_id].append(manga_payload)
        detailed_works = []
        for pair in ot_works:
            payload = work_dict(pair)
            payload["tipo"] = "COLOR"
            payload["mangas"] = mangas_by_work.pop(pair[0].id, [])
            detailed_works.append(payload)
        unassigned = mangas_by_work.pop(None, [])
        if unassigned or (not ot_works and ot.tipo_ot == "ENSAMBLE"):
            detailed_works.append({
                "id": str(ot.public_id),
                "codigo": ot.codigo_ot,
                "tipo": "ARMADO",
                "secuencia": 1,
                "color": None,
                "estado": operational_state,
                "responsable": _worker_dict(responsible),
                "objetivo_un": _number(objective),
                "confirmado_un": _number(confirmed),
                "mangas": unassigned,
            })
        for work_id, orphan_mangas in mangas_by_work.items():
            detailed_works.append({
                "id": str(work_id),
                "codigo": None,
                "tipo": "NO_RESOLUBLE",
                "secuencia": None,
                "color": None,
                "estado": None,
                "responsable": None,
                "objetivo_un": 0.0,
                "confirmado_un": 0.0,
                "mangas": orphan_mangas,
            })
        item["trabajos"] = detailed_works
    return item


def _hydrate(session, *, rows, actor, as_of, detail=False):
    visibility = {
        "pesaje": actor.tiene_capacidad("MANGA_PESAJE_VER"),
        "alertas": actor.tiene_capacidad("ALERTA_VER"),
        "almacen": actor.tiene_capacidad("RECEPCION_MANGA_VER"),
        "calidad": actor.tiene_capacidad("CALIDAD_MANGA_VER"),
    }
    ot_ids = [row[0].id for row in rows]
    if not ot_ids:
        return []

    work_rows = session.execute(
        select(ScmTrabajoOt, ScmTrabajoColor)
        .options(noload("*"))
        .outerjoin(
            ScmTrabajoColor,
            ScmTrabajoColor.trabajo_ot_id == ScmTrabajoOt.id,
        )
        .where(ScmTrabajoOt.orden_trabajo_id.in_(ot_ids))
        .order_by(ScmTrabajoOt.orden_trabajo_id, ScmTrabajoOt.secuencia)
    ).all()
    works = defaultdict(list)
    work_ids = []
    for work, color in work_rows:
        works[work.orden_trabajo_id].append((work, color))
        work_ids.append(work.id)

    assignment_by_work = {}
    assignment_by_id = {}
    if work_ids:
        assignment_rows = session.scalars(
            select(ScmAsignacionPersonalTrabajoOt)
            .options(noload("*"))
            .where(ScmAsignacionPersonalTrabajoOt.trabajo_ot_id.in_(work_ids))
            .order_by(
                ScmAsignacionPersonalTrabajoOt.trabajo_ot_id,
                ScmAsignacionPersonalTrabajoOt.estado,
                ScmAsignacionPersonalTrabajoOt.asignada_at.desc(),
            )
        ).all()
        for assignment in assignment_rows:
            assignment_by_id[assignment.id] = assignment
            if assignment.estado not in {"ACTIVA", "PREVISTA"}:
                continue
            current = assignment_by_work.get(assignment.trabajo_ot_id)
            if current is None or (
                current.estado != "ACTIVA" and assignment.estado == "ACTIVA"
            ):
                assignment_by_work[assignment.trabajo_ot_id] = assignment

    operation_ids = {
        row[0].orden_operacion_id
        for row in rows
        if row[0].orden_operacion_id is not None
    }
    operation_ids.update(work.orden_operacion_id for work, _color in work_rows)
    operations = {}
    if operation_ids:
        operation_rows = session.execute(
            select(
                ScmOrdenOperacion,
                ScmPlanProduccion,
                ScmOrdenProduccion,
            )
            .options(noload("*"))
            .outerjoin(
                ScmPlanProduccion,
                ScmPlanProduccion.id
                == ScmOrdenOperacion.plan_produccion_id,
            )
            .outerjoin(
                ScmOrdenProduccion,
                ScmOrdenProduccion.id
                == ScmPlanProduccion.orden_produccion_id,
            )
            .where(ScmOrdenOperacion.id.in_(operation_ids))
        ).all()
        operations = {
            operation.id: (operation, plan, op)
            for operation, plan, op in operation_rows
        }

    manga_rows = session.scalars(
        select(ScmManga)
        .options(noload("*"))
        .where(ScmManga.ot_id.in_(ot_ids))
        .order_by(ScmManga.ot_id, ScmManga.secuencia_ot)
    ).all()
    mangas = defaultdict(list)
    for manga in manga_rows:
        mangas[manga.ot_id].append(manga)
    manga_ids = [manga.id for manga in manga_rows]

    weighing_by_manga = {}
    correction_by_weighing = {}
    annulment_by_weighing = {}
    if manga_ids and (visibility["pesaje"] or visibility["alertas"]):
        weighing_rows = session.scalars(
            select(ScmPesajeManga)
            .options(noload("*"))
            .where(ScmPesajeManga.manga_id.in_(manga_ids))
        ).all()
        for item in weighing_rows:
            current = weighing_by_manga.get(item.manga_id)
            if current is None or item.estado == "VIGENTE":
                weighing_by_manga[item.manga_id] = item
        weighing_ids = [item.id for item in weighing_rows]
        if weighing_ids:
            correction_rows = session.scalars(
                select(ScmCorreccionPesajeManga)
                .options(noload("*"))
                .where(
                    ScmCorreccionPesajeManga.pesaje_id.in_(weighing_ids),
                    ScmCorreccionPesajeManga.estado == "APLICADA",
                )
                .order_by(
                    ScmCorreccionPesajeManga.pesaje_id,
                    ScmCorreccionPesajeManga.id.desc(),
                )
            ).all()
            for correction in correction_rows:
                correction_by_weighing.setdefault(
                    correction.pesaje_id, correction
                )
            annulment_rows = session.scalars(
                select(ScmAnulacionPesajeManga).options(noload("*")).where(
                    ScmAnulacionPesajeManga.pesaje_id.in_(weighing_ids)
                )
            ).all()
            annulment_by_weighing = {
                item.pesaje_id: item for item in annulment_rows
            }

    existence_by_manga = {}
    if manga_ids and (visibility["almacen"] or visibility["calidad"]):
        existence_rows = session.scalars(
            select(ScmExistenciaManga).options(noload("*")).where(
                ScmExistenciaManga.manga_id.in_(manga_ids)
            )
        ).all()
        existence_by_manga = {item.manga_id: item for item in existence_rows}

    label_by_manga = {}
    if detail and manga_ids:
        label_rows = session.scalars(
            select(ScmEtiquetaManga)
            .options(noload("*"))
            .where(
                ScmEtiquetaManga.manga_id.in_(manga_ids),
                ScmEtiquetaManga.estado != "INVALIDADA",
            )
            .order_by(
                ScmEtiquetaManga.manga_id,
                ScmEtiquetaManga.version.desc(),
                ScmEtiquetaManga.id.desc(),
            )
        ).all()
        for label in label_rows:
            current = label_by_manga.get(label.manga_id)
            label_score = (
                1 if label.tipo == "POSTPESAJE" else 0,
                label.version,
                label.id,
            )
            current_score = (
                (
                    1 if current.tipo == "POSTPESAJE" else 0,
                    current.version,
                    current.id,
                )
                if current is not None
                else (-1, -1, -1)
            )
            if label_score > current_score:
                label_by_manga[label.manga_id] = label

    worker_ids = {
        value
        for row in rows
        for value in (
            row[0].responsable_id,
            row[0].maquinista_previsto_id,
        )
        if value is not None
    }
    worker_ids.update(
        assignment.trabajador_id for assignment in assignment_by_id.values()
    )
    worker_ids.update(manga.maquinista_previsto_id for manga in manga_rows)
    workers = {}
    if worker_ids:
        workers = {
            worker.id: worker
            for worker in session.scalars(
                select(Trabajador)
                .options(noload("*"))
                .where(Trabajador.id.in_(worker_ids))
            ).all()
        }

    alerts_by_ot = defaultdict(list)
    if visibility["alertas"]:
        id_to_ot = {}
        aggregate_ids = defaultdict(set)
        for row in rows:
            ot = row[0]
            for aggregate_type, aggregate_id in (
                ("ORDEN_TRABAJO", str(ot.public_id)),
                ("ORDEN_TRABAJO", str(ot.id)),
                ("OT", str(ot.public_id)),
            ):
                id_to_ot[(aggregate_type, aggregate_id)] = ot.id
                aggregate_ids[aggregate_type].add(aggregate_id)
        work_to_ot = {
            str(work.id): work.orden_trabajo_id for work, _color in work_rows
        }
        for work_id, ot_id in work_to_ot.items():
            for aggregate_type in ("TRABAJO_OT", "TRABAJO_COLOR"):
                id_to_ot[(aggregate_type, work_id)] = ot_id
                aggregate_ids[aggregate_type].add(work_id)
        manga_to_ot = {str(item.public_id): item.ot_id for item in manga_rows}
        for manga_id, ot_id in manga_to_ot.items():
            id_to_ot[("MANGA", manga_id)] = ot_id
            aggregate_ids["MANGA"].add(manga_id)
        manga_by_id = {item.id: item for item in manga_rows}
        weighing_by_id = {
            item.id: item for item in weighing_by_manga.values()
        }
        weighing_to_ot = {
            str(item.public_id): manga_by_id[item.manga_id].ot_id
            for item in weighing_by_manga.values()
        }
        for weighing_id, ot_id in weighing_to_ot.items():
            id_to_ot[("PESAJE_MANGA", weighing_id)] = ot_id
            aggregate_ids["PESAJE_MANGA"].add(weighing_id)
        correction_to_ot = {}
        for correction in correction_by_weighing.values():
            weighing = weighing_by_id[correction.pesaje_id]
            correction_to_ot[str(correction.public_id)] = weighing_to_ot[
                str(weighing.public_id)
            ]
        for correction_id, ot_id in correction_to_ot.items():
            id_to_ot[("CORRECCION_PESAJE_MANGA", correction_id)] = ot_id
            aggregate_ids["CORRECCION_PESAJE_MANGA"].add(correction_id)
        aggregate_conditions = [
            and_(
                ScmAlertaOperativa.agregado_tipo == aggregate_type,
                ScmAlertaOperativa.agregado_id.in_(sorted(ids)),
            )
            for aggregate_type, ids in aggregate_ids.items()
            if ids
        ]
        if aggregate_conditions:
            alert_rows = session.scalars(
                select(ScmAlertaOperativa)
                .options(noload("*"))
                .where(or_(*aggregate_conditions))
            ).all()
            for alert in alert_rows:
                ot_id = id_to_ot.get((alert.agregado_tipo, alert.agregado_id))
                if ot_id is not None:
                    alerts_by_ot[ot_id].append(alert)

    return [
        _build_item(
            row=row,
            works=works,
            mangas=mangas,
            assignments=assignment_by_work,
            assignments_by_id=assignment_by_id,
            operations=operations,
            workers=workers,
            weighings=weighing_by_manga,
            corrections=correction_by_weighing,
            annulments=annulment_by_weighing,
            existences=existence_by_manga,
            alerts=alerts_by_ot,
            labels=label_by_manga,
            visibility=visibility,
            as_of=as_of,
            detail=detail,
        )
        for row in rows
    ]


def _authorize(session, *, actor_id, filters):
    actor = load_actor(session, actor_id, capability="OT_VER")
    # PENDIENTES_PESAJE se deriva del estado visible de la manga y no revela
    # lecturas fisicas; pesos, correcciones y fechas siguen bajo
    # MANGA_PESAJE_VER. Alertas y custodia de Almacen si son filtros sensibles.
    if filters.get("alertas") is not None and not actor.tiene_capacidad("ALERTA_VER"):
        raise ScmServiceError(
            "CAPABILITY_REQUIRED",
            "El actor requiere la capacidad ALERTA_VER.",
            status_code=403,
            details={"capability": "ALERTA_VER"},
        )
    if (
        filters.get("pendientes_almacen") is not None
        and not actor.tiene_capacidad("RECEPCION_MANGA_VER")
    ):
        raise ScmServiceError(
            "CAPABILITY_REQUIRED",
            "El actor requiere la capacidad RECEPCION_MANGA_VER.",
            status_code=403,
            details={"capability": "RECEPCION_MANGA_VER"},
        )
    return actor


def list_pending_production_documents(session, *, actor_id, filters=None):
    """Lista OF/OA liberadas que todavía no tienen una OT operativa.

    Estos documentos no pertenecen al read model por OT porque aún no poseen
    fecha operativa ni jornada. Se exponen por separado para no ocultar trabajo
    liberado ni inflar los KPI de ejecución.
    """

    normalized = _normalize_filters(filters)
    _authorize(session, actor_id=actor_id, filters=normalized)
    as_of = utc_now()
    if any((
        normalized["estado_operativo"],
        normalized["turno"],
        normalized["responsable"],
        normalized["ot"],
        normalized["color"],
        normalized["quick"],
        normalized["pendientes_pesaje"],
        normalized["pendientes_almacen"],
        normalized["alertas"],
    )):
        return {"items": [], "count": 0, "as_of": _iso(as_of)}
    event_at = func.coalesce(
        ScmOrdenOperacion.released_at,
        ScmOrdenOperacion.created_at,
    )
    statement = (
        select(
            ScmOrdenOperacion,
            ScmOrdenFabricacion,
            ScmPlanProduccion,
            ScmOrdenProduccion,
            Maquina,
        )
        .outerjoin(
            ScmOrdenFabricacion,
            ScmOrdenFabricacion.orden_operacion_id == ScmOrdenOperacion.id,
        )
        .outerjoin(
            Maquina,
            Maquina.id == ScmOrdenFabricacion.maquina_prevista_id,
        )
        .outerjoin(
            ScmPlanProduccion,
            ScmPlanProduccion.id == ScmOrdenOperacion.plan_produccion_id,
        )
        .outerjoin(
            ScmOrdenProduccion,
            ScmOrdenProduccion.id == ScmPlanProduccion.orden_produccion_id,
        )
        .where(
            ScmOrdenOperacion.estado.in_(("LIBERADA", "PROGRAMADA")),
            ScmOrdenOperacion.created_at <= as_of,
            ~exists(
                select(RegistroDiarioProduccion.id).where(
                    RegistroDiarioProduccion.orden_operacion_id
                    == ScmOrdenOperacion.id
                )
            ),
        )
    )
    if normalized["fecha_desde"]:
        statement = statement.where(
            func.date(event_at) >= normalized["fecha_desde"]
        )
    if normalized["fecha_hasta"]:
        statement = statement.where(
            func.date(event_at) <= normalized["fecha_hasta"]
        )
    if normalized["tipo_ot"]:
        statement = statement.where(
            ScmOrdenOperacion.tipo == normalized["tipo_ot"]
        )
    if normalized["estado_documental"]:
        statement = statement.where(
            ScmOrdenOperacion.estado == normalized["estado_documental"]
        )
    if normalized["orden"]:
        statement = statement.where(
            ScmOrdenOperacion.codigo.ilike(f"%{normalized['orden']}%")
        )
    if normalized["op"]:
        statement = statement.where(
            ScmOrdenProduccion.codigo.ilike(f"%{normalized['op']}%")
        )
    if normalized["recurso"]:
        resource_term = f"%{normalized['recurso']}%"
        statement = statement.where(or_(
            ScmOrdenFabricacion.molde_id.ilike(resource_term),
            Maquina.codigo.ilike(resource_term),
            Maquina.nombre.ilike(resource_term),
        ))
    if normalized["q"]:
        term = f"%{normalized['q']}%"
        output_match = exists(
            select(ScmOrdenOperacionSalida.id)
            .join(
                ScmArticulo,
                ScmArticulo.id == ScmOrdenOperacionSalida.articulo_scm_id,
            )
            .where(
                ScmOrdenOperacionSalida.orden_operacion_id
                == ScmOrdenOperacion.id,
                or_(
                    ScmArticulo.codigo.ilike(term),
                    ScmArticulo.nombre.ilike(term),
                ),
            )
        )
        statement = statement.where(or_(
            ScmOrdenOperacion.codigo.ilike(term),
            ScmOrdenOperacion.motivo.ilike(term),
            ScmOrdenProduccion.codigo.ilike(term),
            ScmOrdenFabricacion.molde_id.ilike(term),
            Maquina.codigo.ilike(term),
            Maquina.nombre.ilike(term),
            output_match,
        ))
    rows = session.execute(
        statement.order_by(event_at.desc(), ScmOrdenOperacion.codigo.desc())
        .limit(normalized["limit"])
    ).all()
    order_ids = [row[0].id for row in rows]
    outputs_by_order = defaultdict(list)
    if order_ids:
        output_rows = session.execute(
            select(ScmOrdenOperacionSalida, ScmArticulo)
            .join(
                ScmArticulo,
                ScmArticulo.id == ScmOrdenOperacionSalida.articulo_scm_id,
            )
            .where(ScmOrdenOperacionSalida.orden_operacion_id.in_(order_ids))
            .order_by(
                ScmOrdenOperacionSalida.orden_operacion_id,
                ScmArticulo.codigo,
            )
        ).all()
        for output, article in output_rows:
            outputs_by_order[output.orden_operacion_id].append({
                "articulo": {
                    "id": article.id,
                    "codigo": article.codigo,
                    "nombre": article.nombre,
                    "clase": article.clase,
                },
                "cantidad_objetivo": _number(output.cantidad_objetivo),
                "unidad": article.unidad_base,
            })
    items = []
    for order, fabrication, _plan, production_order, machine in rows:
        outputs = outputs_by_order.get(order.id, [])
        items.append({
            "id": str(order.id),
            "codigo": order.codigo,
            "tipo": "ARMADO" if order.tipo == "ENSAMBLE" else order.tipo,
            "estado": order.estado,
            "origen": order.origen_demanda,
            "motivo": order.motivo,
            "created_at": _iso(order.created_at),
            "released_at": _iso(order.released_at),
            "op": ({"codigo": production_order.codigo}
                   if production_order else None),
            "recurso": {
                "molde_codigo": fabrication.molde_id if fabrication else None,
                "maquina_codigo": machine.codigo if machine else None,
                "maquina_nombre": machine.nombre if machine else None,
            },
            "salidas": outputs,
            "cantidad_objetivo": sum(
                item["cantidad_objetivo"] for item in outputs
            ),
            "siguiente_accion": "PROGRAMAR_OT",
            "situacion": "SIN_OT",
        })
    return {
        "items": items,
        "count": len(items),
        "as_of": _iso(as_of),
    }


def _execute_list(session, *, actor_id, filters, paginate, detail=False):
    normalized = _normalize_filters(filters)
    actor = _authorize(session, actor_id=actor_id, filters=normalized)
    as_of = utc_now()
    cursor_values = None
    if normalized["cursor"]:
        cursor_values = _decode_cursor(
            normalized["cursor"], filters=normalized
        )
        as_of = cursor_values[2]
    statement = _base_statement(normalized)
    statement = statement.where(or_(
        RegistroDiarioProduccion.created_at.is_(None),
        RegistroDiarioProduccion.created_at <= as_of,
    ))
    descending = normalized["sort"] == "FECHA_DESC"
    if cursor_values:
        last_date, last_id, _cursor_as_of = cursor_values
        cursor_condition = or_(
            RegistroDiarioProduccion.fecha < last_date,
            and_(
                RegistroDiarioProduccion.fecha == last_date,
                RegistroDiarioProduccion.id < last_id,
            ),
        ) if descending else or_(
            RegistroDiarioProduccion.fecha > last_date,
            and_(
                RegistroDiarioProduccion.fecha == last_date,
                RegistroDiarioProduccion.id > last_id,
            ),
        )
        statement = statement.where(cursor_condition)
    if descending:
        statement = statement.order_by(
            RegistroDiarioProduccion.fecha.desc(),
            RegistroDiarioProduccion.id.desc(),
        )
    else:
        statement = statement.order_by(
            RegistroDiarioProduccion.fecha.asc(),
            RegistroDiarioProduccion.id.asc(),
        )
    if paginate:
        statement = statement.limit(normalized["limit"] + 1)
    rows = session.execute(statement).all()
    has_more = paginate and len(rows) > normalized["limit"]
    if has_more:
        rows = rows[: normalized["limit"]]
    items = _hydrate(
        session,
        rows=rows,
        actor=actor,
        as_of=as_of,
        detail=detail,
    )
    next_cursor = None
    if has_more and rows:
        next_cursor = _encode_cursor(
            item=rows[-1][0], filters=normalized, as_of=as_of
        )
    return {
        "items": items,
        "page": {
            "next_cursor": next_cursor,
            "limit": normalized["limit"],
            "has_more": bool(has_more),
        },
        "as_of": _iso(as_of),
        "_normalized_filters": normalized,
    }


def _base_manga_statement(filters):
    machine = aliased(Maquina)
    center = aliased(ScmCentroTrabajo)
    statement = (
        select(ScmManga, RegistroDiarioProduccion, machine, center)
        .options(noload("*"))
        .join(
            RegistroDiarioProduccion,
            RegistroDiarioProduccion.id == ScmManga.ot_id,
        )
        .outerjoin(machine, machine.id == RegistroDiarioProduccion.maquina_id)
        .outerjoin(
            center,
            center.id == RegistroDiarioProduccion.centro_trabajo_id,
        )
        .where(RegistroDiarioProduccion.codigo_ot_sintetico.is_(False))
    )
    if filters["fecha_desde"]:
        statement = statement.where(
            RegistroDiarioProduccion.fecha >= filters["fecha_desde"]
        )
    if filters["fecha_hasta"]:
        statement = statement.where(
            RegistroDiarioProduccion.fecha <= filters["fecha_hasta"]
        )
    if filters["tipo_ot"]:
        statement = statement.where(
            RegistroDiarioProduccion.tipo_ot == filters["tipo_ot"]
        )
    if filters["estado_documental"]:
        statement = statement.where(
            RegistroDiarioProduccion.estado == filters["estado_documental"]
        )
    if filters["turno"]:
        statement = statement.where(
            RegistroDiarioProduccion.turno == filters["turno"]
        )
    if filters["ot"]:
        statement = statement.where(
            RegistroDiarioProduccion.codigo_ot.ilike(f"%{filters['ot']}%")
        )
    if filters["manga"]:
        statement = statement.where(
            ScmManga.codigo.ilike(f"%{filters['manga']}%")
        )
    if filters["estado_manga"]:
        statement = statement.where(ScmManga.estado == filters["estado_manga"])
    if filters["articulo"]:
        pattern = f"%{filters['articulo']}%"
        statement = statement.where(or_(
            ScmManga.articulo_codigo_snapshot.ilike(pattern),
            ScmManga.articulo_nombre_snapshot.ilike(pattern),
            ScmManga.pieza_color_sku_snapshot.ilike(pattern),
        ))
    if filters["color"]:
        statement = statement.where(
            ScmManga.color_snapshot.ilike(f"%{filters['color']}%")
        )
    if filters["recurso"]:
        pattern = f"%{filters['recurso']}%"
        statement = statement.where(or_(
            machine.codigo.ilike(pattern),
            machine.nombre.ilike(pattern),
            center.codigo.ilike(pattern),
            center.nombre.ilike(pattern),
        ))
    if filters["responsable"]:
        statement = statement.where(_responsible_exists(filters["responsable"]))
    if filters["orden"]:
        statement = statement.where(_operation_exists(
            ScmOrdenOperacion.codigo.ilike(f"%{filters['orden']}%")
        ))
    if filters["op"]:
        statement = statement.where(_upstream_op_exists(filters["op"]))
    if filters["estado_operativo"]:
        desired = filters["estado_operativo"]
        if desired == "PAUSADA":
            desired = "PAUSADO"
        statement = statement.where(or_(
            RegistroDiarioProduccion.estado == desired,
            _work_exists(ScmTrabajoOt.estado == desired),
        ))
    if filters["pendientes_pesaje"] is not None:
        condition = ScmManga.estado.in_(PENDING_WEIGHING_STATES)
        statement = statement.where(
            condition if filters["pendientes_pesaje"] else ~condition
        )
    if filters["pendientes_almacen"] is not None:
        condition = ScmManga.estado.in_(PENDING_RECEIPT_STATES)
        statement = statement.where(
            condition if filters["pendientes_almacen"] else ~condition
        )
    if filters["alertas"] is not None:
        condition = _open_alert_exists()
        statement = statement.where(condition if filters["alertas"] else ~condition)
    if filters["quick"] == "EN_EJECUCION":
        statement = statement.where(or_(
            RegistroDiarioProduccion.estado == "EN_EJECUCION",
            _work_exists(ScmTrabajoOt.estado == "EN_EJECUCION"),
        ))
    elif filters["quick"] == "PAUSADAS":
        statement = statement.where(_work_exists(ScmTrabajoOt.estado == "PAUSADO"))
    elif filters["quick"] == "PENDIENTES_PESAJE":
        statement = statement.where(ScmManga.estado.in_(PENDING_WEIGHING_STATES))
    elif filters["quick"] == "ATRASADAS":
        statement = statement.where(
            RegistroDiarioProduccion.estado.notin_(("CERRADA", "ANULADA")),
            _upstream_op_exists(overdue=True),
        )
    if filters["q"]:
        pattern = f"%{filters['q']}%"
        statement = statement.where(or_(
            ScmManga.codigo.ilike(pattern),
            ScmManga.articulo_codigo_snapshot.ilike(pattern),
            ScmManga.articulo_nombre_snapshot.ilike(pattern),
            ScmManga.pieza_color_sku_snapshot.ilike(pattern),
            ScmManga.color_snapshot.ilike(pattern),
            RegistroDiarioProduccion.codigo_ot.ilike(pattern),
            machine.codigo.ilike(pattern),
            machine.nombre.ilike(pattern),
            center.codigo.ilike(pattern),
            center.nombre.ilike(pattern),
            _work_exists(ScmTrabajoOt.codigo.ilike(pattern)),
            _operation_exists(ScmOrdenOperacion.codigo.ilike(pattern)),
            _upstream_op_exists(filters["q"]),
            _responsible_exists(filters["q"]),
        ))
    return statement


def _flatten_manga_items(detail_items, selected_public_ids):
    by_manga_id = {}
    for item in detail_items:
        for work in item.get("trabajos", []):
            work_payload = {
                key: value for key, value in work.items() if key != "mangas"
            }
            for manga in work.get("mangas", []):
                by_manga_id[manga["public_id"]] = {
                    "manga": manga,
                    "ot": item["ot"],
                    "recurso": item["recurso"],
                    "upstream": item["upstream"],
                    "trabajo": work_payload,
                    "visibilidad": item["visibilidad"],
                    "alertas_resumen": item["alertas_resumen"],
                    "ultimo_evento_at": item["ultimo_evento_at"],
                }
    return [
        by_manga_id[public_id]
        for public_id in selected_public_ids
        if public_id in by_manga_id
    ]


def list_production_manga_observability(session, *, actor_id, filters=None):
    normalized = _normalize_filters(filters or {})
    actor = _authorize(session, actor_id=actor_id, filters=normalized)
    as_of = utc_now()
    cursor_values = None
    if normalized["cursor"]:
        cursor_values = _decode_cursor(
            normalized["cursor"], filters=normalized
        )
        as_of = cursor_values[2]

    statement = _base_manga_statement(normalized).where(
        ScmManga.created_at <= as_of
    )
    descending = normalized["sort"] == "FECHA_DESC"
    if cursor_values:
        last_date, last_id, _cursor_as_of = cursor_values
        cursor_condition = or_(
            RegistroDiarioProduccion.fecha < last_date,
            and_(
                RegistroDiarioProduccion.fecha == last_date,
                ScmManga.id < last_id,
            ),
        ) if descending else or_(
            RegistroDiarioProduccion.fecha > last_date,
            and_(
                RegistroDiarioProduccion.fecha == last_date,
                ScmManga.id > last_id,
            ),
        )
        statement = statement.where(cursor_condition)
    ordering = (
        (RegistroDiarioProduccion.fecha.desc(), ScmManga.id.desc())
        if descending
        else (RegistroDiarioProduccion.fecha.asc(), ScmManga.id.asc())
    )
    rows = session.execute(
        statement.order_by(*ordering).limit(normalized["limit"] + 1)
    ).all()
    has_more = len(rows) > normalized["limit"]
    if has_more:
        rows = rows[: normalized["limit"]]

    ot_rows_by_id = {}
    selected_public_ids = []
    for manga, ot, machine, center in rows:
        selected_public_ids.append(str(manga.public_id))
        ot_rows_by_id.setdefault(ot.id, (ot, machine, center))
    details = _hydrate(
        session,
        rows=list(ot_rows_by_id.values()),
        actor=actor,
        as_of=as_of,
        detail=True,
    )
    items = _flatten_manga_items(details, selected_public_ids)
    next_cursor = None
    if has_more and rows:
        last_manga, last_ot, _machine, _center = rows[-1]
        next_cursor = _encode_position_cursor(
            operational_date=last_ot.fecha,
            item_id=last_manga.id,
            filters=normalized,
            as_of=as_of,
        )
    return {
        "items": items,
        "page": {
            "next_cursor": next_cursor,
            "limit": normalized["limit"],
            "has_more": bool(has_more),
        },
        "as_of": _iso(as_of),
    }


def list_production_ot_observability(session, *, actor_id, filters=None):
    payload = _execute_list(
        session,
        actor_id=actor_id,
        filters=filters or {},
        paginate=True,
    )
    payload.pop("_normalized_filters", None)
    return payload


def get_production_ot_observability(session, *, actor_id, public_id):
    normalized = _normalize_filters({})
    actor = _authorize(session, actor_id=actor_id, filters=normalized)
    machine = aliased(Maquina)
    center = aliased(ScmCentroTrabajo)
    row = session.execute(
        select(RegistroDiarioProduccion, machine, center)
        .options(noload("*"))
        .outerjoin(machine, machine.id == RegistroDiarioProduccion.maquina_id)
        .outerjoin(
            center,
            center.id == RegistroDiarioProduccion.centro_trabajo_id,
        )
        .where(
            RegistroDiarioProduccion.public_id == public_id,
            RegistroDiarioProduccion.codigo_ot_sintetico.is_(False),
        )
    ).first()
    if row is None:
        raise ScmServiceError(
            "OBSERVABILITY_OT_NOT_FOUND",
            "La OT solicitada no existe en observabilidad.",
            status_code=404,
        )
    as_of = utc_now()
    item = _hydrate(
        session,
        rows=[row],
        actor=actor,
        as_of=as_of,
        detail=True,
    )[0]
    return {"item": item, "as_of": _iso(as_of)}


def _empty_metrics(*, weighing_visible, alerts_visible):
    return {
        "ots": 0,
        "objetivo_un": 0.0,
        "confirmado_un": 0.0,
        "mangas_total": 0,
        "mangas_pendientes_pesaje": 0,
        "mangas_pendientes_recepcion": 0,
        "mangas_recibidas": 0,
        "peso_fisico_neto_kg": 0.0 if weighing_visible else None,
        "kg_produccion_estandar": 0.0 if weighing_visible else None,
        "alertas_abiertas": 0 if alerts_visible else None,
    }


def _add_item_metrics(target, item):
    target["ots"] += 1
    target["objetivo_un"] += item["cantidades_resumen"]["objetivo_un"]
    target["confirmado_un"] += item["cantidades_resumen"]["confirmado_un"]
    mangas = item["mangas_resumen"]
    target["mangas_total"] += mangas["total"]
    target["mangas_pendientes_pesaje"] += mangas["pendientes_pesaje"]
    target["mangas_pendientes_recepcion"] += mangas["pendientes_recepcion"]
    target["mangas_recibidas"] += mangas["recibidas"]
    if item["pesaje_resumen"] is not None:
        target["peso_fisico_neto_kg"] += item["pesaje_resumen"][
            "peso_fisico_neto_kg"
        ]
        target["kg_produccion_estandar"] += item["pesaje_resumen"][
            "kg_produccion_estandar"
        ]
    if item["alertas_resumen"] is not None:
        target["alertas_abiertas"] += item["alertas_resumen"]["abiertas"]


def summarize_production_ot_observability(
    session, *, actor_id, filters=None, granularity="DIA"
):
    normalized_granularity = _text(granularity, upper=True) or "DIA"
    if normalized_granularity not in {"DIA", "MES"}:
        raise ScmServiceError(
            "INVALID_OBSERVABILITY_GRANULARITY",
            "granularidad debe ser DIA o MES.",
            status_code=400,
        )
    raw_filters = dict(filters or {})
    raw_filters.pop("cursor", None)
    raw_filters.pop("limit", None)
    normalized = _normalize_filters(raw_filters)
    actor = _authorize(session, actor_id=actor_id, filters=normalized)
    visibility = {
        "pesaje": actor.tiene_capacidad("MANGA_PESAJE_VER"),
        "alertas": actor.tiene_capacidad("ALERTA_VER"),
    }
    totals = _empty_metrics(
        weighing_visible=visibility["pesaje"],
        alerts_visible=visibility["alertas"],
    )
    totals["por_estado_documental"] = {}
    totals["por_estado_operativo"] = {}
    documental = Counter()
    operativo = Counter()
    series = {}
    observed_start = None
    observed_end = None
    response_as_of = None
    page_filters = {**raw_filters, "limit": MAX_LIMIT}
    while True:
        payload = _execute_list(
            session,
            actor_id=actor_id,
            filters=page_filters,
            paginate=True,
        )
        if response_as_of is None:
            response_as_of = payload["as_of"]
        for item in payload["items"]:
            _add_item_metrics(totals, item)
            documental[item["ot"]["estado_documental"]] += 1
            operativo[item["ot"]["estado_operativo"]] += 1
            operational_date = item["ot"]["fecha_operativa"]
            parsed_date = date.fromisoformat(operational_date)
            observed_start = (
                parsed_date
                if observed_start is None
                else min(observed_start, parsed_date)
            )
            observed_end = (
                parsed_date
                if observed_end is None
                else max(observed_end, parsed_date)
            )
            period = (
                operational_date[:7]
                if normalized_granularity == "MES"
                else operational_date
            )
            if period not in series:
                series[period] = _empty_metrics(
                    weighing_visible=visibility["pesaje"],
                    alerts_visible=visibility["alertas"],
                )
            _add_item_metrics(series[period], item)
        if not payload["page"]["has_more"]:
            break
        page_filters["cursor"] = payload["page"]["next_cursor"]
    totals["por_estado_documental"] = dict(sorted(documental.items()))
    totals["por_estado_operativo"] = dict(sorted(operativo.items()))
    start = normalized["fecha_desde"] or observed_start
    end = normalized["fecha_hasta"] or observed_end
    return {
        "granularidad": normalized_granularity,
        "periodo": {
            "fecha_desde": start.isoformat() if start else None,
            "fecha_hasta": end.isoformat() if end else None,
        },
        "totales": totals,
        "series": [
            {"periodo": period, **metrics}
            for period, metrics in sorted(series.items())
        ],
        "as_of": response_as_of or _iso(utc_now()),
    }
