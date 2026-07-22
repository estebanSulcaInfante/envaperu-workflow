"""Authority-of-quantity rules for material reception.

This module is intentionally independent from Flask and SQLAlchemy. Application
services will persist its decision, but they must never replace the calculation.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Iterable


KG_QUANTUM = Decimal("0.001")


class QuantityMode(StrEnum):
    VIRGEN_CONFIANZA_PROVEEDOR = "VIRGEN_CONFIANZA_PROVEEDOR"
    SEGUNDA_PESAJE_BOLSA = "SEGUNDA_PESAJE_BOLSA"


class QuantitySource(StrEnum):
    DOCUMENTO_PROVEEDOR = "DOCUMENTO_PROVEEDOR"
    PESAJE_INTERNO_BOLSAS = "PESAJE_INTERNO_BOLSAS"


class InternalWeightStatus(StrEnum):
    NO_MEDIDO = "NO_MEDIDO"
    MEDIDO = "MEDIDO"


class ToleranceResult(StrEnum):
    SIN_POLITICA = "SIN_POLITICA"


class QuantityAuthorityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class QuantityDecision:
    accepted_quantity_kg: Decimal
    source: QuantitySource
    internal_weight_status: InternalWeightStatus
    measured_quantity_kg: Decimal | None
    difference_kg: Decimal | None
    tolerance_result: ToleranceResult


def _positive_kg(value: Decimal, *, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise QuantityAuthorityError("INVALID_QUANTITY", f"{field} must be a positive Decimal")
    if value.as_tuple().exponent < -3:
        raise QuantityAuthorityError("INVALID_QUANTITY_SCALE", f"{field} supports at most three decimals")
    return value.quantize(KG_QUANTUM)


def _positive_package_count(value: int, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QuantityAuthorityError("INVALID_PACKAGE_COUNT", f"{field} must be a positive integer")
    return value


def resolve_quantity_authority(
    *,
    mode: QuantityMode,
    documented_quantity_kg: Decimal,
    documented_packages: int,
    received_packages: int,
    nominal_package_weight_kg: Decimal | None,
    bag_weights_kg: Iterable[Decimal],
) -> QuantityDecision:
    """Resolve the authoritative quantity without inventing an internal weight."""

    documented_quantity = _positive_kg(documented_quantity_kg, field="documented_quantity_kg")
    _positive_package_count(documented_packages, field="documented_packages")
    received_count = _positive_package_count(received_packages, field="received_packages")
    bag_weights = tuple(bag_weights_kg)

    if mode is QuantityMode.VIRGEN_CONFIANZA_PROVEEDOR:
        if nominal_package_weight_kg is None:
            raise QuantityAuthorityError(
                "NOMINAL_WEIGHT_REQUIRED",
                "Virgin supplier-trust reception requires a nominal package weight",
            )
        _positive_kg(nominal_package_weight_kg, field="nominal_package_weight_kg")

        if documented_packages != received_count:
            raise QuantityAuthorityError(
                "VIRGIN_BAG_COUNT_DECISION_REQUIRED",
                "Virgin material with a bag-count discrepancy requires a management decision",
            )

        if bag_weights:
            raise QuantityAuthorityError(
                "UNEXPECTED_INTERNAL_WEIGHT",
                "Virgin supplier-trust reception cannot include internal bag weights",
            )

        return QuantityDecision(
            accepted_quantity_kg=documented_quantity,
            source=QuantitySource.DOCUMENTO_PROVEEDOR,
            internal_weight_status=InternalWeightStatus.NO_MEDIDO,
            measured_quantity_kg=None,
            difference_kg=None,
            tolerance_result=ToleranceResult.SIN_POLITICA,
        )

    if mode is QuantityMode.SEGUNDA_PESAJE_BOLSA:
        if len(bag_weights) != received_count:
            raise QuantityAuthorityError(
                "BAG_WEIGHT_COUNT_MISMATCH",
                "Second-material reception requires one weight for every received bag",
            )

        measured_quantity = sum(
            (_positive_kg(weight, field="bag_weights_kg") for weight in bag_weights),
            start=Decimal("0.000"),
        ).quantize(KG_QUANTUM)

        return QuantityDecision(
            accepted_quantity_kg=measured_quantity,
            source=QuantitySource.PESAJE_INTERNO_BOLSAS,
            internal_weight_status=InternalWeightStatus.MEDIDO,
            measured_quantity_kg=measured_quantity,
            difference_kg=(measured_quantity - documented_quantity).quantize(KG_QUANTUM),
            tolerance_result=ToleranceResult.SIN_POLITICA,
        )

    raise QuantityAuthorityError("UNSUPPORTED_QUANTITY_MODE", f"Unsupported quantity mode: {mode}")
