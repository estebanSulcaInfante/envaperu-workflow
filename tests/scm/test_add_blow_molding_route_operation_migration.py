import importlib
from unittest.mock import call, patch


def _migration():
    return importlib.import_module(
        "migrations.versions.f77e6f1b4c98_add_blow_molding_route_operation"
    )


def test_upgrade_extiende_ambas_restricciones_con_soplado():
    migration = _migration()

    with patch.object(migration.op, "drop_constraint") as drop, patch.object(
        migration.op,
        "create_check_constraint",
    ) as create:
        migration.upgrade()

    assert drop.call_args_list == [
        call(
            "ck_scm_centro_trabajo_tipo",
            "scm_centro_trabajo",
            type_="check",
        ),
        call(
            "ck_scm_operacion_ruta_tipo",
            "scm_operacion_ruta",
            type_="check",
        ),
    ]
    assert len(create.call_args_list) == 2
    assert all("SOPLADO" in item.args[2] for item in create.call_args_list)


def test_downgrade_normaliza_soplado_antes_de_restringir():
    migration = _migration()

    with patch.object(migration.op, "execute") as execute, patch.object(
        migration.op,
        "drop_constraint",
    ), patch.object(migration.op, "create_check_constraint") as create:
        migration.downgrade()

    assert execute.call_count == 2
    assert all("SOPLADO" in item.args[0] for item in execute.call_args_list)
    assert all("SOPLADO" not in item.args[2] for item in create.call_args_list)
