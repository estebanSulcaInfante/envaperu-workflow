import pytest
from sqlalchemy import text

from app.extensions import db
from app.models.producto import PiezaColor
from app.services.scm_legacy_kit_precondition import (
    LEGACY_KIT_PRECONDITION_FAILED,
    LegacyKitPreconditionError,
    assert_legacy_kit_contract_ready,
    inspect_legacy_kit_precondition,
)


def test_contract_precondition_is_ready_without_legacy_rows(app):
    with app.app_context():
        inspection = assert_legacy_kit_contract_ready(db.session)

    assert inspection.to_dict() == {
        "ready": True,
        "kit_count": 0,
        "component_count": 0,
        "kit_samples": [],
        "kit_column_exists": False,
        "component_table_exists": False,
        "contract_applied": True,
    }


def test_contract_precondition_aborts_and_reports_unexpected_kit(app):
    with app.app_context():
        db.session.execute(text(
            "ALTER TABLE pieza_color ADD COLUMN tipo VARCHAR(20)"
        ))
        db.session.execute(text("""
            CREATE TABLE pieza_componente (
                id INTEGER PRIMARY KEY,
                kit_sku VARCHAR(50) NOT NULL,
                componente_sku VARCHAR(50) NOT NULL,
                cantidad INTEGER
            )
        """))
        pieza = PiezaColor(
            sku="LEGACY-KIT-001",
            piezas="Kit inesperado",
            linea_id=1,
            familia_id=1,
        )
        db.session.add(pieza)
        db.session.flush()
        db.session.execute(
            text("""
                UPDATE pieza_color
                SET tipo = 'KIT'
                WHERE sku = 'LEGACY-KIT-001'
            """)
        )
        db.session.execute(text("""
            INSERT INTO pieza_componente (
                id, kit_sku, componente_sku, cantidad
            ) VALUES (
                1, 'LEGACY-KIT-001', 'LEGACY-KIT-001', 1
            )
        """))

        inspection = inspect_legacy_kit_precondition(db.session)
        with pytest.raises(LegacyKitPreconditionError) as captured:
            assert_legacy_kit_contract_ready(db.session)

    assert inspection.ready is False
    assert inspection.kit_count == 1
    assert inspection.component_count == 1
    assert inspection.kit_samples == ("LEGACY-KIT-001",)
    assert inspection.contract_applied is False
    assert captured.value.code == LEGACY_KIT_PRECONDITION_FAILED
