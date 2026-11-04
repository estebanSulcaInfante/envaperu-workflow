import importlib
from unittest.mock import patch


def _migration():
    return importlib.import_module(
        "migrations.versions.fb7c8d9e0a12_add_output_route_operation_reader_index"
    )


def test_output_route_reader_index_is_reversible_and_matches_keyset_order():
    migration = _migration()

    with patch.object(migration.op, "create_index") as create:
        migration.upgrade()

    create.assert_called_once_with(
        "ix_scm_operacion_ruta_output_route_id",
        "scm_operacion_ruta",
        ["articulo_salida_id", "ruta_id", "id"],
    )

    with patch.object(migration.op, "drop_index") as drop:
        migration.downgrade()

    drop.assert_called_once_with(
        "ix_scm_operacion_ruta_output_route_id",
        table_name="scm_operacion_ruta",
    )
