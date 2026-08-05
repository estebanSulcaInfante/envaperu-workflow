from decimal import Decimal, InvalidOperation

from app.models.trabajador import Trabajador


KG_QUANTUM = Decimal("0.001")
KG_MAX = Decimal("999999999999.999")


class ScmServiceError(RuntimeError):
    def __init__(
        self,
        code,
        message,
        *,
        status_code=422,
        details=None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self):
        payload = {
            "code": self.code,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        return payload


def reject_unknown_fields(data, *, allowed):
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ScmServiceError(
            "UNKNOWN_FIELDS",
            "La solicitud contiene campos no reconocidos.",
            status_code=400,
            details={"fields": unknown},
        )


def require_patch_field(data, *, mutable):
    if not set(data).intersection(mutable):
        raise ScmServiceError(
            "PATCH_FIELD_REQUIRED",
            "La solicitud no contiene ningun campo modificable.",
            status_code=400,
            details={"fields": sorted(mutable)},
        )


def reject_no_changes():
    raise ScmServiceError(
        "NO_CHANGES",
        "La solicitud no produce ningun cambio.",
        status_code=400,
    )


def required_text(value, *, field, max_length):
    if value is not None and not isinstance(value, str):
        raise ScmServiceError(
            "TEXT_FIELD_REQUIRED",
            f"El campo {field} debe ser texto.",
            status_code=400,
            details={"field": field},
        )
    normalized = (value or "").strip()
    if not normalized:
        raise ScmServiceError(
            "REQUIRED_FIELD",
            f"El campo {field} es obligatorio.",
            status_code=400,
            details={"field": field},
        )
    if len(normalized) > max_length:
        raise ScmServiceError(
            "FIELD_TOO_LONG",
            f"El campo {field} supera la longitud permitida.",
            status_code=400,
            details={"field": field, "max_length": max_length},
        )
    return normalized


def stable_code(value, *, field="codigo", max_length=64):
    return required_text(
        value,
        field=field,
        max_length=max_length,
    ).upper()


def expected_version(value):
    parsed = (
        value
        if isinstance(value, int) and not isinstance(value, bool)
        else None
    )
    if parsed is None or parsed <= 0:
        raise ScmServiceError(
            "VERSION_REQUIRED",
            "Se requiere una version positiva del recurso.",
            status_code=400,
        )
    return parsed


def positive_integer(value, *, field):
    parsed = (
        value
        if isinstance(value, int) and not isinstance(value, bool)
        else None
    )
    if parsed is None or parsed <= 0:
        raise ScmServiceError(
            "POSITIVE_INTEGER_REQUIRED",
            f"El campo {field} debe ser un entero positivo.",
            status_code=400,
            details={"field": field},
        )
    return parsed


def positive_kg(value, *, field="cantidad_autorizada_kg"):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        parsed = None
    if parsed is None or not parsed.is_finite() or parsed <= 0:
        raise ScmServiceError(
            "INVALID_QUANTITY",
            f"El campo {field} debe ser una cantidad positiva.",
            status_code=422,
            details={"field": field},
        )
    try:
        quantized = parsed.quantize(KG_QUANTUM)
    except InvalidOperation as error:
        raise ScmServiceError(
            "QUANTITY_OUT_OF_RANGE",
            f"El campo {field} excede Numeric(15, 3).",
            status_code=422,
            details={"field": field},
        ) from error
    if parsed != quantized:
        raise ScmServiceError(
            "INVALID_QUANTITY_SCALE",
            f"El campo {field} admite como maximo tres decimales.",
            status_code=422,
            details={"field": field},
        )
    if quantized > KG_MAX:
        raise ScmServiceError(
            "QUANTITY_OUT_OF_RANGE",
            f"El campo {field} excede Numeric(15, 3).",
            status_code=422,
            details={"field": field},
        )
    return quantized


def load_actor(session, actor_id, *, capability=None):
    actor = session.get(Trabajador, actor_id)
    if actor is None or not actor.activo:
        raise ScmServiceError(
            "ACTOR_NOT_AUTHORIZED",
            "El actor declarado no existe o no esta activo.",
            status_code=403,
        )
    if capability and not actor.tiene_capacidad(capability):
        raise ScmServiceError(
            "CAPABILITY_REQUIRED",
            f"El actor requiere la capacidad {capability}.",
            status_code=403,
            details={"capability": capability},
        )
    return actor


def load_actor_any(session, actor_id, *, capabilities):
    actor = load_actor(session, actor_id)
    required = tuple(capabilities or ())
    if required and not any(actor.tiene_capacidad(code) for code in required):
        raise ScmServiceError(
            "CAPABILITY_REQUIRED",
            f"El actor requiere una de las capacidades: {', '.join(required)}.",
            status_code=403,
            details={"capabilities_any": list(required)},
        )
    return actor


def actor_snapshot(actor):
    return {
        "id": actor.id,
        "codigo": actor.codigo,
        "nombre": actor.nombre_completo,
    }
