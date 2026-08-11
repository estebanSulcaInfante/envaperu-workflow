"""Canonical commands for finished-product master identities."""

import copy
import math

from app.models.producto import ProductoPieza, ProductoTerminado
from app.models.scm_commercial import ScmPresentacionComercial
from app.services.catalog_classification_service import (
    ClassificationError,
    validate_linea_familia,
)
from app.services.catalog_code_generator import generar_codigo_catalogo


class CatalogProductError(ValueError):
    def __init__(
        self,
        message,
        *,
        code="PRODUCTO_INVALIDO",
        status=400,
        details=None,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.details = details or {}


def _normalize_numeric_fields(payload):
    specs = {
        "peso_g": (False, 0),
        "precio_estimado": (False, 0),
        "precio_sin_igv": (False, 0),
        "doc_x_paq": (True, 1),
        "doc_x_bulto": (True, 1),
    }
    for field, (integer, minimum) in specs.items():
        if field not in payload:
            continue
        raw = payload[field]
        if raw in (None, ""):
            payload[field] = None
            continue
        try:
            if isinstance(raw, bool):
                raise ValueError
            value = float(raw)
        except (TypeError, ValueError) as error:
            raise CatalogProductError(
                f"{field} debe ser numerico.",
                code="VALOR_INVALIDO",
                details={"field": field},
            ) from error
        if (
            not math.isfinite(value)
            or value < minimum
            or (integer and not value.is_integer())
        ):
            raise CatalogProductError(
                f"{field} tiene un valor fuera de rango.",
                code="VALOR_INVALIDO",
                details={"field": field},
            )
        payload[field] = int(value) if integer else value


def create_finished_product(session, data):
    """Build a new PT plus its default presentation without committing."""

    payload = copy.deepcopy(data or {})
    if str(payload.get("cod_sku_pt") or "").strip():
        raise CatalogProductError(
            "cod_sku_pt es automatico y no admite asignacion manual.",
            code="CODIGO_MANUAL_NO_PERMITIDO",
            details={"field": "cod_sku_pt"},
        )
    name = " ".join(str(payload.get("producto") or "").strip().split())
    if not name:
        raise CatalogProductError(
            "producto es obligatorio.",
            details={"field": "producto"},
        )
    _normalize_numeric_fields(payload)
    try:
        linea, familia, _ = validate_linea_familia(
            linea_id=payload.get("linea_id"),
            familia_id=payload.get("familia_id"),
            session=session,
        )
    except ClassificationError as error:
        raise CatalogProductError(
            str(error),
            code=error.code,
            status=error.status,
        ) from error

    product = ProductoTerminado(
        cod_sku_pt=generar_codigo_catalogo(
            "PRODUCTO_TERMINADO", session=session
        ),
        producto=name,
        linea_id=linea.id,
        familia_id=familia.id,
        peso_g=payload.get("peso_g"),
        precio_estimado=payload.get("precio_estimado"),
        precio_sin_igv=payload.get("precio_sin_igv"),
        doc_x_paq=payload.get("doc_x_paq"),
        doc_x_bulto=payload.get("doc_x_bulto"),
        status=payload.get("status", "Activo"),
        codigo_barra=payload.get("codigo_barra"),
        marca=payload.get("marca"),
        um=payload.get("um", "Unidad"),
    )
    session.add(product)
    session.flush()
    session.add(ScmPresentacionComercial(
        codigo=generar_codigo_catalogo(
            "PRESENTACION_COMERCIAL", session=session
        ),
        producto_terminado_id=product.cod_sku_pt,
        nombre="Unidad",
        unidades_base=1,
        codigo_barra=payload.get("codigo_barra"),
        predeterminada=True,
    ))
    for item in payload.get("piezas", []):
        session.add(ProductoPieza(
            producto_terminado_id=product.cod_sku_pt,
            pieza_sku=item["pieza_sku"],
            cantidad=item.get("cantidad", 1),
        ))
    session.flush()
    return product
