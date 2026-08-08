import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema


pytestmark = pytest.mark.postgres

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "f78a7b3c9d20"
WORKSPACE_ROLE_REVISION = "f79b8c4d0e31"


def _isolated_postgres_url():
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL tests")
    base_url = make_url(raw_url)
    assert base_url.database in {"envaperu_test", "enva_test"}
    assert base_url.host in {"localhost", "127.0.0.1"}

    schema = f"scm_n2_{uuid4().hex[:12]}"
    admin_engine = create_engine(base_url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        connection.execute(CreateSchema(schema))
    query = dict(base_url.query)
    query["options"] = f"-csearch_path={schema}"
    return admin_engine, schema, base_url.set(query=query)


def _run_flask_db(schema_url, *args):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = schema_url.render_as_string(
        hide_password=False
    )
    environment.pop("ALEMBIC_LEGACY_BASELINE", None)
    result = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "app", "db", *args],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _drop_schema(admin_engine, schema):
    try:
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
    finally:
        admin_engine.dispose()


def test_workspace_roles_backfill_security_constraints_and_concurrency():
    from app.services.scm_service_support import ScmServiceError
    from app.services.scm_workspace_role_service import update_role

    admin_engine, schema, schema_url = _isolated_postgres_url()
    schema_engine = create_engine(schema_url, pool_pre_ping=True)
    try:
        _run_flask_db(schema_url, "upgrade", PREVIOUS_REVISION)
        with schema_engine.begin() as connection:
            role_ids = dict(connection.execute(text("""
                INSERT INTO rol_operativo (codigo, nombre, activo)
                VALUES
                  ('N2_UNICO', 'N2 unico', true),
                  ('N2_DOBLE_A', 'N2 doble A', true),
                  ('N2_DOBLE_B', 'N2 doble B', true),
                  ('N2_INACTIVO', 'N2 inactivo', false)
                RETURNING codigo, id
            """)).tuples().all())
            worker_ids = dict(connection.execute(text("""
                INSERT INTO trabajador (
                    codigo, nombres, apellidos, activo
                )
                VALUES
                  ('N2-UNO', 'Uno', 'Activo', true),
                  ('N2-DOS', 'Dos', 'Activos', true),
                  ('N2-MIXTO', 'Uno', 'Mas inactivo', true)
                RETURNING codigo, id
            """)).tuples().all())
            connection.execute(text("""
                INSERT INTO trabajador_rol (
                    trabajador_id, rol_operativo_id
                ) VALUES
                  (:one_worker, :one_role),
                  (:two_worker, :two_role_a),
                  (:two_worker, :two_role_b),
                  (:mixed_worker, :one_role),
                  (:mixed_worker, :inactive_role)
            """), {
                "one_worker": worker_ids["N2-UNO"],
                "one_role": role_ids["N2_UNICO"],
                "two_worker": worker_ids["N2-DOS"],
                "two_role_a": role_ids["N2_DOBLE_A"],
                "two_role_b": role_ids["N2_DOBLE_B"],
                "mixed_worker": worker_ids["N2-MIXTO"],
                "inactive_role": role_ids["N2_INACTIVO"],
            })

        _run_flask_db(schema_url, "upgrade", WORKSPACE_ROLE_REVISION)

        with schema_engine.begin() as connection:
            assert connection.execute(text("""
                SELECT trabajador_id, rol_operativo_id
                FROM trabajador_rol
                WHERE es_principal
                ORDER BY trabajador_id
            """)).tuples().all() == [
                (worker_ids["N2-UNO"], role_ids["N2_UNICO"]),
                (worker_ids["N2-MIXTO"], role_ids["N2_UNICO"]),
            ]

            security = dict(connection.execute(text("""
                SELECT class.relname, class.relrowsecurity
                FROM pg_class AS class
                JOIN pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = current_schema()
                  AND class.relname IN (
                    'rol_operativo', 'trabajador_rol', 'scm_capacidad',
                    'scm_rol_capacidad',
                    'scm_rol_workspace_preferencia'
                  )
            """)).tuples().all())
            assert security == {
                "rol_operativo": True,
                "trabajador_rol": True,
                "scm_capacidad": True,
                "scm_rol_capacidad": True,
                "scm_rol_workspace_preferencia": True,
            }

            api_roles = connection.execute(text("""
                SELECT rolname
                FROM pg_roles
                WHERE rolname IN ('anon', 'authenticated')
            """)).scalars().all()
            for api_role in api_roles:
                for table_name in security:
                    assert connection.execute(text("""
                        SELECT has_table_privilege(
                            :role_name, :qualified,
                            'SELECT,INSERT,UPDATE,DELETE'
                        )
                    """), {
                        "role_name": api_role,
                        "qualified": f"{schema}.{table_name}",
                    }).scalar_one() is False

            role_constraints = {
                item["name"]
                for item in inspect(connection).get_check_constraints(
                    "rol_operativo"
                )
            }
            assert "ck_rol_operativo_workspace_version" in role_constraints
            preference_constraints = {
                item["name"]
                for item in inspect(connection).get_check_constraints(
                    "scm_rol_workspace_preferencia"
                )
            }
            assert (
                "ck_scm_rol_workspace_preferencia_prioridad"
                in preference_constraints
            )
            preference_columns = {
                item["name"]: item
                for item in inspect(connection).get_columns(
                    "scm_rol_workspace_preferencia"
                )
            }
            assert preference_columns["created_by_id"]["nullable"] is False
            assert preference_columns["updated_by_id"]["nullable"] is False
            preference_indexes = {
                item["name"]
                for item in inspect(connection).get_indexes(
                    "scm_rol_workspace_preferencia"
                )
            }
            assert {
                "ix_scm_rol_workspace_preferencia_orden",
                "ix_scm_rol_workspace_preferencia_created_by",
                "ix_scm_rol_workspace_preferencia_updated_by",
            } <= preference_indexes

            principal_indexes = {
                item["name"]: item
                for item in inspect(connection).get_indexes(
                    "trabajador_rol"
                )
            }
            assert principal_indexes[
                "uq_trabajador_rol_principal"
            ]["unique"] is True
            preference_fks = {
                item["name"]: item
                for item in inspect(connection).get_foreign_keys(
                    "scm_rol_workspace_preferencia"
                )
            }
            assert set(preference_fks) == {
                "fk_scm_rol_workspace_preferencia_rol",
                "fk_scm_rol_workspace_preferencia_creador",
                "fk_scm_rol_workspace_preferencia_actualizador",
            }
            assert all(
                fk["options"].get("ondelete") == "RESTRICT"
                for fk in preference_fks.values()
            )

            current_user = connection.execute(text(
                "SELECT current_user"
            )).scalar_one()
            table_owners = set(connection.execute(text("""
                SELECT owner.rolname
                FROM pg_class AS class
                JOIN pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                JOIN pg_roles AS owner ON owner.oid = class.relowner
                WHERE namespace.nspname = current_schema()
                  AND class.relname IN (
                    'rol_operativo', 'trabajador_rol', 'scm_capacidad',
                    'scm_rol_capacidad',
                    'scm_rol_workspace_preferencia'
                  )
            """)).scalars())
            assert table_owners == {current_user}

            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text("""
                    UPDATE trabajador_rol
                    SET es_principal = true
                    WHERE trabajador_id = :worker_id
                      AND rol_operativo_id = :role_id
                """), {
                    "worker_id": worker_ids["N2-DOS"],
                    "role_id": role_ids["N2_DOBLE_A"],
                })
                connection.execute(text("""
                    UPDATE trabajador_rol
                    SET es_principal = true
                    WHERE trabajador_id = :worker_id
                      AND rol_operativo_id = :role_id
                """), {
                    "worker_id": worker_ids["N2-DOS"],
                    "role_id": role_ids["N2_DOBLE_B"],
                })

            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text("""
                    INSERT INTO scm_rol_workspace_preferencia (
                        rol_operativo_id, feature_key, prioridad, fijada,
                        created_by_id, updated_by_id
                    ) VALUES (
                        :role_id, 'invalid.priority', 1000, false,
                        :worker_id, :worker_id
                    )
                """), {
                    "role_id": role_ids["N2_UNICO"],
                    "worker_id": worker_ids["N2-UNO"],
                })

            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text("""
                    UPDATE rol_operativo
                    SET version = 0
                    WHERE id = :role_id
                """), {"role_id": role_ids["N2_UNICO"]})

            audit_worker_id = connection.execute(text("""
                INSERT INTO trabajador (
                    codigo, nombres, apellidos, activo
                ) VALUES ('N2-AUDIT', 'Audit', 'Actor', true)
                RETURNING id
            """)).scalar_one()
            preference_role_id = connection.execute(text("""
                INSERT INTO rol_operativo (
                    codigo, nombre, activo, version
                ) VALUES ('N2_PREF_FK', 'N2 preference FK', true, 1)
                RETURNING id
            """)).scalar_one()
            connection.execute(text("""
                INSERT INTO scm_rol_workspace_preferencia (
                    rol_operativo_id, feature_key, prioridad, fijada,
                    created_by_id, updated_by_id
                ) VALUES (
                    :role_id, 'warehouse.kardex', 1, true,
                    :worker_id, :worker_id
                )
            """), {
                "role_id": preference_role_id,
                "worker_id": audit_worker_id,
            })
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text(
                    "DELETE FROM rol_operativo WHERE id = :role_id"
                ), {"role_id": preference_role_id})
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text(
                    "DELETE FROM trabajador WHERE id = :worker_id"
                ), {"worker_id": audit_worker_id})

            manager_role_id = connection.execute(text("""
                SELECT id FROM rol_operativo
                WHERE codigo = 'GERENTE_GENERAL'
            """)).scalar_one()
            manager_id = connection.execute(text("""
                INSERT INTO trabajador (
                    codigo, nombres, apellidos, activo
                ) VALUES ('N2-ADMIN', 'Admin', 'N2', true)
                RETURNING id
            """)).scalar_one()
            connection.execute(text("""
                INSERT INTO trabajador_rol (
                    trabajador_id, rol_operativo_id, es_principal
                ) VALUES (:worker_id, :role_id, true)
            """), {
                "worker_id": manager_id,
                "role_id": manager_role_id,
            })
            target_role_id = connection.execute(text("""
                INSERT INTO rol_operativo (
                    codigo, nombre, activo, version
                ) VALUES ('N2_CONCURRENTE', 'N2 concurrente', true, 1)
                RETURNING id
            """)).scalar_one()

        barrier = Barrier(2)

        def concurrent_update(name):
            with Session(schema_engine) as session:
                barrier.wait(timeout=10)
                try:
                    result = update_role(
                        session,
                        actor_id=manager_id,
                        role_id=target_role_id,
                        data={
                            "nombre": name,
                            "expected_version": 1,
                        },
                    )
                    return ("ok", result["version"])
                except ScmServiceError as error:
                    return ("error", error.code)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                concurrent_update,
                ("Primera escritura", "Segunda escritura"),
            ))
        assert sorted(results) == [
            ("error", "VERSION_CONFLICT"),
            ("ok", 2),
        ]

        _run_flask_db(schema_url, "downgrade", PREVIOUS_REVISION)
        assert "scm_rol_workspace_preferencia" not in inspect(
            schema_engine
        ).get_table_names()
        assert "es_principal" not in {
            item["name"]
            for item in inspect(schema_engine).get_columns("trabajador_rol")
        }
        with schema_engine.connect() as connection:
            retained_security = dict(connection.execute(text("""
                SELECT class.relname, class.relrowsecurity
                FROM pg_class AS class
                JOIN pg_namespace AS namespace
                  ON namespace.oid = class.relnamespace
                WHERE namespace.nspname = current_schema()
                  AND class.relname IN (
                    'rol_operativo', 'trabajador_rol',
                    'scm_capacidad', 'scm_rol_capacidad'
                  )
            """)).tuples().all())
            assert all(retained_security.values())
        _run_flask_db(schema_url, "upgrade", WORKSPACE_ROLE_REVISION)
    finally:
        schema_engine.dispose()
        _drop_schema(admin_engine, schema)
