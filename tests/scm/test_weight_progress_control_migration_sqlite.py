import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from tests.scm.test_weight_control_label_migration_sqlite import (
    K1_MODULE,
    K2_MODULE,
    _apply,
    _base_schema,
)


K3_MODULE = (
    "migrations.versions."
    "f91b2d4e6c83_add_repeatable_weight_progress_controls"
)


def _insert_operation(connection, operation_id):
    connection.execute(text(
        "INSERT INTO scm_operacion (operation_id) VALUES (:operation_id)"
    ), {"operation_id": operation_id})


def _insert_control(
    connection,
    *,
    public_id,
    operation_id,
    capture_id,
    control_type,
    count,
    net,
):
    connection.execute(text("""
        INSERT INTO scm_control_peso_manga (
            public_id, manga_id, tramo_id, operation_id,
            source_system, station_id, capture_id, tipo,
            peso_bruto_kg, tara_kg, peso_neto_kg,
            aporte_desde_control_anterior_kg, tara_fuente,
            conteo_acumulado_un, motivo, pesado_at,
            fecha_local_pesaje, pesado_por_id
        ) VALUES (
            :public_id, 100, 'segment-1', :operation_id,
            'SCM_STATION', 'station-1', :capture_id, :control_type,
            :net + 0.1, 0.1, :net, :net, 'TIPO_MANGA',
            :count, 'MANGA_INCOMPLETA', '2026-08-31T10:00:00+00:00',
            '2026-08-31', 10
        )
    """), {
        "public_id": public_id,
        "operation_id": operation_id,
        "capture_id": capture_id,
        "control_type": control_type,
        "count": count,
        "net": net,
    })


def test_k3_migration_admite_avances_kg_repetibles_sin_conteo(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _base_schema(connection)
        _apply(connection, monkeypatch, K1_MODULE, "upgrade")
        _apply(connection, monkeypatch, K2_MODULE, "upgrade")
        connection.execute(text("""
            INSERT INTO scm_tramo_manga_trabajo (
                id, manga_id, trabajo_ot_id,
                asignacion_personal_trabajo_id, asignacion_plan_id,
                secuencia, estado, cantidad_inicio_un,
                cantidad_atribuida_un, created_by_id
            ) VALUES (
                'segment-1', 100, 'work-1', 'assignment-1', 1,
                1, 'ACTIVO', 0, 0, 10
            )
        """))

        _apply(connection, monkeypatch, K3_MODULE, "upgrade")
        inspector = inspect(connection)
        count_column = next(
            item for item in inspector.get_columns("scm_control_peso_manga")
            if item["name"] == "conteo_acumulado_un"
        )
        assert count_column["nullable"] is True
        assert "uq_scm_control_peso_manga_tramo" not in {
            item["name"]
            for item in inspector.get_unique_constraints(
                "scm_control_peso_manga"
            )
        }
        checks = " ".join(
            item["sqltext"]
            for item in inspector.get_check_constraints(
                "scm_control_peso_manga"
            )
        )
        assert "AVANCE_KG" in checks

        _insert_operation(connection, "operation-k3-1")
        _insert_operation(connection, "operation-k3-2")
        _insert_control(
            connection,
            public_id="control-k3-1",
            operation_id="operation-k3-1",
            capture_id="capture-k3-1",
            control_type="AVANCE_KG",
            count=None,
            net=3.5,
        )
        _insert_control(
            connection,
            public_id="control-k3-2",
            operation_id="operation-k3-2",
            capture_id="capture-k3-2",
            control_type="AVANCE_KG",
            count=None,
            net=7.5,
        )
        assert connection.execute(text("""
            SELECT COUNT(*) FROM scm_control_peso_manga
            WHERE tramo_id = 'segment-1'
        """)).scalar_one() == 2

        _insert_operation(connection, "operation-k3-invalid")
        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                _insert_control(
                    connection,
                    public_id="control-k3-invalid",
                    operation_id="operation-k3-invalid",
                    capture_id="capture-k3-invalid",
                    control_type="AVANCE_KG",
                    count=5,
                    net=8.0,
                )

        with pytest.raises(RuntimeError, match="AVANCE_KG"):
            _apply(connection, monkeypatch, K3_MODULE, "downgrade")
