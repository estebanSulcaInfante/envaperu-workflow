import importlib

from sqlalchemy import create_engine, text


def test_migracion_oe_a_oa_conserva_relaciones_y_correlativo(monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.f76d5e0a3b87_rename_oe_to_oa"
    )
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE scm_capacidad (id INTEGER PRIMARY KEY, codigo TEXT UNIQUE)",
            "CREATE TABLE scm_rol_capacidad (rol_operativo_id INTEGER, capacidad_id INTEGER)",
            "CREATE TABLE correlativo_catalogo (clave TEXT PRIMARY KEY, prefijo TEXT UNIQUE, siguiente_valor INTEGER, ancho INTEGER)",
            "CREATE TABLE scm_orden_operacion (id TEXT PRIMARY KEY, codigo TEXT UNIQUE, tipo TEXT)",
            "CREATE TABLE scm_manga (id INTEGER PRIMARY KEY, codigo TEXT UNIQUE)",
            "CREATE TABLE scm_evento (id INTEGER PRIMARY KEY, aggregate_type TEXT, tipo TEXT)",
        ):
            connection.execute(text(statement))

        connection.execute(text(
            "INSERT INTO scm_capacidad (id, codigo) VALUES "
            "(1, 'OE_VER'), (2, 'OE_LIBERAR'), (3, 'OE_EJECUTAR'), (4, 'OE_ANULAR')"
        ))
        connection.execute(text(
            "INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id) "
            "VALUES (10, 1), (10, 2), (20, 3)"
        ))
        connection.execute(text(
            "INSERT INTO correlativo_catalogo (clave, prefijo, siguiente_valor, ancho) "
            "VALUES ('ORDEN_ENSAMBLE', 'OE', 17, 6)"
        ))
        connection.execute(text(
            "INSERT INTO scm_orden_operacion (id, codigo, tipo) "
            "VALUES ('assembly', 'OE-000016', 'ENSAMBLE'), "
            "('manufacturing', 'OF-000003', 'FABRICACION')"
        ))
        connection.execute(text(
            "INSERT INTO scm_manga (id, codigo) VALUES "
            "(1, 'OE000016-OT001-M001'), (2, 'OF000003-OT001-M001')"
        ))
        connection.execute(text(
            "INSERT INTO scm_evento (id, aggregate_type, tipo) VALUES "
            "(1, 'ORDEN_ENSAMBLE', 'OE_LIBERAR'), (2, 'ORDEN_FABRICACION', 'OF_LIBERAR')"
        ))

        monkeypatch.setattr(migration.op, "execute", connection.execute)
        migration.upgrade()

        assert connection.execute(text(
            "SELECT codigo FROM scm_capacidad ORDER BY id"
        )).scalars().all() == [
            "OA_VER", "OA_LIBERAR", "OA_EJECUTAR", "OA_ANULAR"
        ]
        assert connection.execute(text(
            "SELECT rol_operativo_id, capacidad_id FROM scm_rol_capacidad "
            "ORDER BY rol_operativo_id, capacidad_id"
        )).tuples().all() == [(10, 1), (10, 2), (20, 3)]
        assert connection.execute(text(
            "SELECT clave, prefijo, siguiente_valor FROM correlativo_catalogo"
        )).one() == ("ORDEN_ARMADO", "OA", 17)
        assert connection.execute(text(
            "SELECT codigo FROM scm_orden_operacion ORDER BY id"
        )).scalars().all() == ["OA-000016", "OF-000003"]
        assert connection.execute(text(
            "SELECT codigo FROM scm_manga ORDER BY id"
        )).scalars().all() == ["OA000016-OT001-M001", "OF000003-OT001-M001"]
        assert connection.execute(text(
            "SELECT aggregate_type, tipo FROM scm_evento WHERE id = 1"
        )).one() == ("ORDEN_ARMADO", "OA_LIBERAR")

        migration.downgrade()
        assert connection.execute(text(
            "SELECT clave, prefijo, siguiente_valor FROM correlativo_catalogo"
        )).one() == ("ORDEN_ENSAMBLE", "OE", 17)
