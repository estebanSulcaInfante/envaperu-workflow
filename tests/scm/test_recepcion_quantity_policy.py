from decimal import Decimal

import pytest

from app.domain.scm.quantity_authority import (
    InternalWeightStatus,
    QuantityAuthorityError,
    QuantityMode,
    QuantitySource,
    ToleranceResult,
    resolve_quantity_authority,
)


def test_rec_44_virgin_matching_bag_count_uses_document_without_internal_weight():
    decision = resolve_quantity_authority(
        mode=QuantityMode.VIRGEN_CONFIANZA_PROVEEDOR,
        documented_quantity_kg=Decimal("5000.000"),
        documented_packages=200,
        received_packages=200,
        nominal_package_weight_kg=Decimal("25.000"),
        bag_weights_kg=(),
    )

    assert decision.accepted_quantity_kg == Decimal("5000.000")
    assert decision.source is QuantitySource.DOCUMENTO_PROVEEDOR
    assert decision.internal_weight_status is InternalWeightStatus.NO_MEDIDO
    assert decision.measured_quantity_kg is None


def test_rec_45_second_material_uses_each_bag_weight_and_keeps_difference_visible():
    decision = resolve_quantity_authority(
        mode=QuantityMode.SEGUNDA_PESAJE_BOLSA,
        documented_quantity_kg=Decimal("50.000"),
        documented_packages=2,
        received_packages=2,
        nominal_package_weight_kg=None,
        bag_weights_kg=(Decimal("24.830"), Decimal("25.120")),
    )

    assert decision.accepted_quantity_kg == Decimal("49.950")
    assert decision.source is QuantitySource.PESAJE_INTERNO_BOLSAS
    assert decision.internal_weight_status is InternalWeightStatus.MEDIDO
    assert decision.measured_quantity_kg == Decimal("49.950")
    assert decision.difference_kg == Decimal("-0.050")
    assert decision.tolerance_result is ToleranceResult.SIN_POLITICA


def test_virgin_bag_count_discrepancy_requires_an_explicit_management_decision():
    try:
        resolve_quantity_authority(
            mode=QuantityMode.VIRGEN_CONFIANZA_PROVEEDOR,
            documented_quantity_kg=Decimal("5000.000"),
            documented_packages=200,
            received_packages=199,
            nominal_package_weight_kg=Decimal("25.000"),
            bag_weights_kg=(),
        )
    except QuantityAuthorityError as error:
        assert error.code == "VIRGIN_BAG_COUNT_DECISION_REQUIRED"
    else:
        raise AssertionError("A virgin bag-count discrepancy must not be accepted silently")


@pytest.mark.parametrize(
    ("documented_quantity_kg", "expected_code"),
    [
        (Decimal("0.000"), "INVALID_QUANTITY"),
        (Decimal("-1.000"), "INVALID_QUANTITY"),
        (Decimal("1.0001"), "INVALID_QUANTITY_SCALE"),
    ],
)
def test_rec_05_rejects_invalid_documented_quantities(documented_quantity_kg, expected_code):
    with pytest.raises(QuantityAuthorityError) as captured:
        resolve_quantity_authority(
            mode=QuantityMode.VIRGEN_CONFIANZA_PROVEEDOR,
            documented_quantity_kg=documented_quantity_kg,
            documented_packages=1,
            received_packages=1,
            nominal_package_weight_kg=Decimal("25.000"),
            bag_weights_kg=(),
        )

    assert captured.value.code == expected_code


def test_second_material_requires_one_positive_weight_per_received_bag():
    with pytest.raises(QuantityAuthorityError) as captured:
        resolve_quantity_authority(
            mode=QuantityMode.SEGUNDA_PESAJE_BOLSA,
            documented_quantity_kg=Decimal("50.000"),
            documented_packages=2,
            received_packages=2,
            nominal_package_weight_kg=None,
            bag_weights_kg=(Decimal("24.830"),),
        )

    assert captured.value.code == "BAG_WEIGHT_COUNT_MISMATCH"


def test_virgin_supplier_trust_rejects_internal_bag_weights():
    with pytest.raises(QuantityAuthorityError) as captured:
        resolve_quantity_authority(
            mode=QuantityMode.VIRGEN_CONFIANZA_PROVEEDOR,
            documented_quantity_kg=Decimal("25.000"),
            documented_packages=1,
            received_packages=1,
            nominal_package_weight_kg=Decimal("25.000"),
            bag_weights_kg=(Decimal("25.000"),),
        )

    assert captured.value.code == "UNEXPECTED_INTERNAL_WEIGHT"
