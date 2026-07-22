"""Reglas autoritativas para la clasificación Línea–Familia."""

from dataclasses import dataclass

from app.extensions import db
from app.models.molde import Pieza
from app.models.producto import (
    Familia,
    Linea,
    LineaFamilia,
    PiezaColor,
    ProductoTerminado,
)


@dataclass(frozen=True)
class ClassificationError(ValueError):
    """Error de dominio serializable por las rutas HTTP."""

    message: str
    code: str
    status: int = 400

    def __str__(self):
        return self.message


def _as_positive_int(value, field):
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        raise ClassificationError(
            f"{field} debe ser un entero positivo",
            "CLASIFICACION_INVALIDA",
        )
    if normalized <= 0:
        raise ClassificationError(
            f"{field} debe ser un entero positivo",
            "CLASIFICACION_INVALIDA",
        )
    return normalized


def validate_linea_familia(
    *,
    linea_id,
    familia_id,
    session=None,
    allow_unclassified=False,
    require_active=True,
):
    """Valida y devuelve ``(Linea, Familia, LineaFamilia)``.

    Una clasificación opcional debe omitir ambos IDs. Nunca se admite que solo
    uno de ellos esté presente.
    """

    active_session = session if session is not None else db.session
    missing_linea = linea_id in (None, "")
    missing_familia = familia_id in (None, "")
    if missing_linea and missing_familia and allow_unclassified:
        return None, None, None
    if missing_linea or missing_familia:
        raise ClassificationError(
            "linea_id y familia_id deben informarse juntos",
            "CLASIFICACION_INCOMPLETA",
        )

    normalized_linea_id = _as_positive_int(linea_id, "linea_id")
    normalized_familia_id = _as_positive_int(familia_id, "familia_id")
    linea = active_session.get(Linea, normalized_linea_id)
    if not linea:
        raise ClassificationError(
            f"Línea {normalized_linea_id} no encontrada",
            "LINEA_NO_ENCONTRADA",
        )
    familia = active_session.get(Familia, normalized_familia_id)
    if not familia:
        raise ClassificationError(
            f"Familia {normalized_familia_id} no encontrada",
            "FAMILIA_NO_ENCONTRADA",
        )
    if require_active and not linea.activo:
        raise ClassificationError(
            f"La línea {linea.nombre} está inactiva",
            "LINEA_INACTIVA",
            409,
        )
    if require_active and not familia.activo:
        raise ClassificationError(
            f"La familia {familia.nombre} está inactiva",
            "FAMILIA_INACTIVA",
            409,
        )

    relacion = active_session.query(LineaFamilia).filter_by(
        linea_id=normalized_linea_id,
        familia_id=normalized_familia_id,
    ).first()
    if not relacion or (require_active and not relacion.activo):
        raise ClassificationError(
            f"La familia {familia.nombre} no está asociada a la línea {linea.nombre}",
            "LINEA_FAMILIA_NO_ASOCIADA",
            409,
        )
    return linea, familia, relacion


def ensure_linea_familia(*, linea_id, familia_id, session=None):
    """Crea o reactiva una asociación observada por un flujo interno/importado."""

    active_session = session if session is not None else db.session
    normalized_linea_id = _as_positive_int(linea_id, "linea_id")
    normalized_familia_id = _as_positive_int(familia_id, "familia_id")
    linea = active_session.get(Linea, normalized_linea_id)
    familia = active_session.get(Familia, normalized_familia_id)
    if not linea or not familia:
        raise ClassificationError(
            "No se puede asociar una línea o familia inexistente",
            "CLASIFICACION_INVALIDA",
        )
    relacion = active_session.query(LineaFamilia).filter_by(
        linea_id=normalized_linea_id,
        familia_id=normalized_familia_id,
    ).first()
    if not relacion:
        relacion = LineaFamilia(
            linea_id=normalized_linea_id,
            familia_id=normalized_familia_id,
            activo=True,
        )
        active_session.add(relacion)
    elif not relacion.activo:
        relacion.activo = True
        relacion.version += 1
    return relacion


def classification_usage(*, linea_id=None, familia_id=None, session=None):
    """Cuenta consumidores que usan el maestro o par indicado."""

    if linea_id is None and familia_id is None:
        raise ValueError("Debe indicarse linea_id o familia_id")
    active_session = session if session is not None else db.session
    filters = []
    if linea_id is not None:
        filters.append(("linea_id", int(linea_id)))
    if familia_id is not None:
        filters.append(("familia_id", int(familia_id)))

    def count(model):
        query = active_session.query(model)
        for field, value in filters:
            query = query.filter(getattr(model, field) == value)
        return query.count()

    counts = {
        "productos": count(ProductoTerminado),
        "piezas": count(Pieza),
        "piezas_color": count(PiezaColor),
    }
    counts["total"] = sum(counts.values())
    return counts
