from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.services.scm_fulfillment_service import (
    _line_fulfillment,
    credit_output_allocations,
)


def _allocation(planned, committed, satisfied, state="COMPROMETIDA"):
    return SimpleNamespace(
        id=f"a-{planned}-{committed}-{satisfied}",
        created_at=datetime.now(timezone.utc),
        cantidad_planificada=Decimal(planned),
        cantidad_comprometida=Decimal(committed),
        cantidad_satisfecha=Decimal(satisfied),
        estado=state,
        version=1,
    )


def test_partial_output_reprojects_satisfaction_without_inventing_unit_conversion():
    first = _allocation("100", "100", "20")
    second = _allocation("30", "30", "30", "SATISFECHA")
    output = SimpleNamespace(asignaciones=[first, second])

    result = credit_output_allocations(output, Decimal("50"))

    assert result["excedente_no_asignado"] == "0"
    assert first.cantidad_satisfecha == Decimal("50")
    assert first.cantidad_comprometida == Decimal("50")
    assert second.cantidad_satisfecha == Decimal("0")
    assert second.cantidad_comprometida == Decimal("0")


def test_cancelled_allocation_keeps_satisfied_history_but_releases_residual():
    satisfied = _allocation("100", "40", "40", "CANCELADA")
    residual = _allocation("60", "60", "0", "CANCELADA")
    line = SimpleNamespace(asignaciones=[satisfied, residual])

    covered, committed, operational = _line_fulfillment(line)

    assert covered == Decimal("40")
    assert committed == Decimal("0")
    assert operational is False

