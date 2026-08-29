"""add inline WIP reservations and exceptional OA capability

Revision ID: f92c7d9e1f86
Revises: f91b6c8d0e75
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa


revision = "f92c7d9e1f86"
down_revision = "f91b6c8d0e75"
branch_labels = None
depends_on = None


CAPABILITY = "OA_EXCEPCIONAL_CREAR"
CAPABILITY_MARKER = "Creada por f92c7d9e1f86; grants espejo de OF excepcional."
OF_CLOSE_CAPABILITY = "OF_CERRAR"
OF_CLOSE_CAPABILITY_MARKER = (
    "Creada por f92c7d9e1f86; cierre OF para acreditar salida WIP."
)
OF_CLOSE_ROLES = ("GERENTE_GENERAL", "JEFE_PRODUCCION")
NEW_TABLES = (
    "scm_saldo_wip_salida",
    "scm_reserva_wip_salida",
    "scm_movimiento_wip_salida",
)


def _seed_capability():
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (
            codigo, nombre, descripcion, activo
        )
        SELECT :code, :name, :marker, true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad WHERE codigo = :code
        )
    """).bindparams(
        code=CAPABILITY,
        name="Crear OA excepcional de reposición WIP",
        marker=CAPABILITY_MARKER,
    ))
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT of_grant.rol_operativo_id, oa.id
        FROM scm_capacidad AS of_cap
        JOIN scm_rol_capacidad AS of_grant
          ON of_grant.capacidad_id = of_cap.id
        JOIN scm_capacidad AS oa
          ON oa.codigo = :oa_code
        WHERE of_cap.codigo = :of_code
          AND NOT EXISTS (
            SELECT 1
            FROM scm_rol_capacidad AS current_grant
            WHERE current_grant.rol_operativo_id = of_grant.rol_operativo_id
              AND current_grant.capacidad_id = oa.id
          )
    """).bindparams(
        oa_code=CAPABILITY,
        of_code="OF_EXCEPCIONAL_CREAR",
    ))
    op.execute(sa.text("""
        INSERT INTO scm_capacidad (
            codigo, nombre, descripcion, activo
        )
        SELECT :code, :name, :marker, true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad WHERE codigo = :code
        )
    """).bindparams(
        code=OF_CLOSE_CAPABILITY,
        name="Cerrar orden de fabricación",
        marker=OF_CLOSE_CAPABILITY_MARKER,
    ))
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT role.id, capability.id
        FROM rol_operativo AS role
        JOIN scm_capacidad AS capability
          ON capability.codigo = :capability_code
        WHERE role.codigo IN :role_codes
          AND NOT EXISTS (
            SELECT 1
            FROM scm_rol_capacidad AS current_grant
            WHERE current_grant.rol_operativo_id = role.id
              AND current_grant.capacidad_id = capability.id
          )
    """).bindparams(
        sa.bindparam("role_codes", expanding=True),
        capability_code=OF_CLOSE_CAPABILITY,
        role_codes=OF_CLOSE_ROLES,
    ))


def _protect_tables_on_postgres(connection):
    if connection.dialect.name != "postgresql":
        return
    schema = connection.execute(
        sa.text("SELECT current_schema()")
    ).scalar_one()
    preparer = connection.dialect.identifier_preparer
    quoted_schema = preparer.quote(schema)
    for table_name in NEW_TABLES:
        qualified = f"{quoted_schema}.{preparer.quote(table_name)}"
        op.execute(
            f"ALTER TABLE {qualified} ENABLE ROW LEVEL SECURITY"
        )
        op.execute(
            f"ALTER TABLE {qualified} FORCE ROW LEVEL SECURITY"
        )
        op.execute(sa.text(f"""
            DO $body$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL PRIVILEGES ON TABLE {qualified} FROM anon;
              END IF;
              IF EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'authenticated'
              ) THEN
                REVOKE ALL PRIVILEGES ON TABLE {qualified}
                FROM authenticated;
              END IF;
            END
            $body$;
        """))


def upgrade():
    op.create_table(
        "scm_saldo_wip_salida",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("trabajo_color_id", sa.Uuid(), nullable=False),
        sa.Column("orden_operacion_salida_id", sa.Uuid(), nullable=False),
        sa.Column("articulo_id", sa.Integer(), nullable=False),
        sa.Column(
            "cantidad_acreditada",
            sa.Numeric(15, 3),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cantidad_consumida",
            sa.Numeric(15, 3),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "cantidad_acreditada >= 0 AND cantidad_consumida >= 0 "
            "AND cantidad_consumida <= cantidad_acreditada",
            name="ck_scm_saldo_wip_salida_cantidades",
        ),
        sa.CheckConstraint(
            "version > 0", name="ck_scm_saldo_wip_salida_version"
        ),
        sa.ForeignKeyConstraint(
            ["trabajo_color_id"],
            ["scm_trabajo_ot.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["orden_operacion_salida_id"],
            ["scm_orden_operacion_salida.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_id"], ["scm_articulo.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trabajo_color_id",
            "orden_operacion_salida_id",
            name="uq_scm_saldo_wip_salida_trabajo_salida",
        ),
    )
    op.create_index(
        "ix_scm_saldo_wip_salida_trabajo",
        "scm_saldo_wip_salida",
        ["trabajo_color_id"],
    )
    op.create_index(
        "ix_scm_saldo_wip_salida_orden_salida",
        "scm_saldo_wip_salida",
        ["orden_operacion_salida_id"],
    )
    op.create_index(
        "ix_scm_saldo_wip_salida_articulo",
        "scm_saldo_wip_salida",
        ["articulo_id"],
    )

    op.create_table(
        "scm_reserva_wip_salida",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("asignacion_plan_id", sa.Integer(), nullable=False),
        sa.Column("articulo_componente_id", sa.Integer(), nullable=False),
        sa.Column(
            "cantidad_reservada", sa.Numeric(15, 3), nullable=False
        ),
        sa.Column(
            "cantidad_aplicada",
            sa.Numeric(15, 3),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "estado",
            sa.String(36),
            nullable=False,
            server_default="CREDITO_EN_LINEA_PENDIENTE",
        ),
        sa.Column("creada_por_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("aplicada_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "estado IN ('CREDITO_EN_LINEA_PENDIENTE', 'APLICADA', "
            "'LIBERADA', 'CANCELADA')",
            name="ck_scm_reserva_wip_salida_estado",
        ),
        sa.CheckConstraint(
            "cantidad_reservada > 0 AND cantidad_aplicada >= 0 "
            "AND cantidad_aplicada <= cantidad_reservada",
            name="ck_scm_reserva_wip_salida_cantidades",
        ),
        sa.ForeignKeyConstraint(
            ["saldo_id"],
            ["scm_saldo_wip_salida.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["asignacion_plan_id"],
            ["scm_asignacion_plan_manga_ot.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["articulo_componente_id"],
            ["scm_articulo.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["creada_por_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["scm_operacion.operation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "manga_id",
            "articulo_componente_id",
            name="uq_scm_reserva_wip_salida_manga_componente",
        ),
        sa.UniqueConstraint(
            "id",
            "saldo_id",
            name="uq_scm_reserva_wip_salida_id_saldo",
        ),
    )
    for name, columns in (
        ("ix_scm_reserva_wip_salida_saldo", ["saldo_id"]),
        ("ix_scm_reserva_wip_salida_manga", ["manga_id"]),
        (
            "ix_scm_reserva_wip_salida_asignacion_plan",
            ["asignacion_plan_id"],
        ),
        (
            "ix_scm_reserva_wip_salida_articulo",
            ["articulo_componente_id"],
        ),
        ("ix_scm_reserva_wip_salida_creada_por", ["creada_por_id"]),
        ("ix_scm_reserva_wip_salida_operacion", ["operation_id"]),
    ):
        op.create_index(name, "scm_reserva_wip_salida", columns)
    op.create_index(
        "ix_scm_reserva_wip_salida_pendiente",
        "scm_reserva_wip_salida",
        ["saldo_id"],
        postgresql_where=sa.text(
            "estado = 'CREDITO_EN_LINEA_PENDIENTE'"
        ),
        sqlite_where=sa.text(
            "estado = 'CREDITO_EN_LINEA_PENDIENTE'"
        ),
    )

    with op.batch_alter_table("scm_consumo_componente_armado") as batch:
        batch.add_column(
            sa.Column("reserva_wip_salida_id", sa.Uuid(), nullable=True)
        )
        batch.add_column(sa.Column(
            "procedencia",
            sa.String(32),
            nullable=False,
            server_default="CONSUMIDO_STOCK_PREVIO",
        ))
        batch.drop_constraint(
            "ck_scm_consumo_armado_fuente_unica", type_="check"
        )
        batch.create_foreign_key(
            "fk_scm_consumo_armado_reserva_wip",
            "scm_reserva_wip_salida",
            ["reserva_wip_salida_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_unique_constraint(
            "uq_scm_consumo_armado_reserva_wip",
            ["confirmacion_id", "reserva_wip_salida_id"],
        )
        batch.create_check_constraint(
            "ck_scm_consumo_armado_fuente_unica",
            "(asignacion_abastecimiento_id IS NOT NULL AND "
            "asignacion_pool_id IS NULL AND reserva_wip_salida_id IS NULL) "
            "OR (asignacion_abastecimiento_id IS NULL AND "
            "asignacion_pool_id IS NOT NULL AND reserva_wip_salida_id IS NULL) "
            "OR (asignacion_abastecimiento_id IS NULL AND "
            "asignacion_pool_id IS NULL AND reserva_wip_salida_id IS NOT NULL)",
        )
        batch.create_check_constraint(
            "ck_scm_consumo_armado_procedencia",
            "procedencia IN ('PRODUCIDO_OT_ACTUAL', "
            "'CONSUMIDO_STOCK_PREVIO')",
        )
        batch.create_check_constraint(
            "ck_scm_consumo_armado_procedencia_fuente",
            "(reserva_wip_salida_id IS NOT NULL AND "
            "procedencia = 'PRODUCIDO_OT_ACTUAL') OR "
            "(reserva_wip_salida_id IS NULL AND "
            "procedencia = 'CONSUMIDO_STOCK_PREVIO')",
        )
        batch.create_index(
            "ix_scm_consumo_armado_reserva_wip",
            ["reserva_wip_salida_id"],
        )

    op.create_table(
        "scm_movimiento_wip_salida",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("reserva_id", sa.Uuid(), nullable=False),
        sa.Column("confirmacion_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(40), nullable=False),
        sa.Column("cantidad", sa.Numeric(15, 3), nullable=False),
        sa.Column("effect_key", sa.String(160), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "tipo IN ('SALIDA_BUENA_CONFIRMADA', "
            "'CONSUMO_EN_LINEA_ARMADO', 'REVERSO_SALIDA_BUENA', "
            "'REVERSO_CONSUMO_EN_LINEA_ARMADO')",
            name="ck_scm_movimiento_wip_salida_tipo",
        ),
        sa.CheckConstraint(
            "cantidad > 0", name="ck_scm_movimiento_wip_salida_cantidad"
        ),
        sa.ForeignKeyConstraint(
            ["saldo_id"],
            ["scm_saldo_wip_salida.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reserva_id", "saldo_id"],
            [
                "scm_reserva_wip_salida.id",
                "scm_reserva_wip_salida.saldo_id",
            ],
            name="fk_scm_movimiento_wip_reserva_saldo",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmacion_id"],
            ["scm_confirmacion_manga_armado.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["trabajador.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["operation_id"],
            ["scm_operacion.operation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "effect_key", name="uq_scm_movimiento_wip_salida_effect_key"
        ),
    )
    for name, columns in (
        ("ix_scm_movimiento_wip_salida_saldo", ["saldo_id"]),
        ("ix_scm_movimiento_wip_salida_reserva", ["reserva_id"]),
        (
            "ix_scm_movimiento_wip_salida_confirmacion",
            ["confirmacion_id"],
        ),
        ("ix_scm_movimiento_wip_salida_actor", ["actor_id"]),
        ("ix_scm_movimiento_wip_salida_operacion", ["operation_id"]),
        (
            "ix_scm_movimiento_wip_salida_created_id",
            ["created_at", "id"],
        ),
    ):
        op.create_index(name, "scm_movimiento_wip_salida", columns)
    _protect_tables_on_postgres(op.get_bind())
    _seed_capability()


def downgrade():
    bind = op.get_bind()
    balance_count = bind.execute(
        sa.text("SELECT count(*) FROM scm_saldo_wip_salida")
    ).scalar_one()
    movement_count = bind.execute(
        sa.text("SELECT count(*) FROM scm_movimiento_wip_salida")
    ).scalar_one()
    reservation_count = bind.execute(
        sa.text("SELECT count(*) FROM scm_reserva_wip_salida")
    ).scalar_one()
    inline_consumption_count = bind.execute(sa.text("""
        SELECT count(*)
        FROM scm_consumo_componente_armado
        WHERE reserva_wip_salida_id IS NOT NULL
    """)).scalar_one()
    if (
        balance_count
        or movement_count
        or reservation_count
        or inline_consumption_count
    ):
        raise RuntimeError(
            "Downgrade bloqueado: existen hechos de producción en línea; "
            "aplique un forward fix para conservar la genealogía."
        )

    capability = bind.execute(sa.text("""
        SELECT id, descripcion
        FROM scm_capacidad
        WHERE codigo = :code
    """), {"code": CAPABILITY}).mappings().first()
    if capability and capability["descripcion"] == CAPABILITY_MARKER:
        oa_roles = {
            row[0]
            for row in bind.execute(sa.text("""
                SELECT role.codigo
                FROM scm_rol_capacidad AS relation
                JOIN rol_operativo AS role
                  ON role.id = relation.rol_operativo_id
                WHERE relation.capacidad_id = :capability_id
            """), {"capability_id": capability["id"]})
        }
        of_roles = {
            row[0]
            for row in bind.execute(sa.text("""
                SELECT role.codigo
                FROM scm_rol_capacidad AS relation
                JOIN rol_operativo AS role
                  ON role.id = relation.rol_operativo_id
                JOIN scm_capacidad AS capability
                  ON capability.id = relation.capacidad_id
                WHERE capability.codigo = 'OF_EXCEPCIONAL_CREAR'
            """))
        }
        if oa_roles != of_roles:
            raise RuntimeError(
                "Downgrade bloqueado: los grants OA ya no reflejan los grants "
                "OF de la migración; preserve la autorización con forward fix."
            )
        op.execute(sa.text("""
            DELETE FROM scm_rol_capacidad
            WHERE capacidad_id = :capability_id
        """).bindparams(capability_id=capability["id"]))
        op.execute(sa.text("""
            DELETE FROM scm_capacidad
            WHERE id = :capability_id AND descripcion = :marker
        """).bindparams(
            capability_id=capability["id"],
            marker=CAPABILITY_MARKER,
        ))

    close_capability = bind.execute(sa.text("""
        SELECT id, descripcion
        FROM scm_capacidad
        WHERE codigo = :code
    """), {"code": OF_CLOSE_CAPABILITY}).mappings().first()
    if (
        close_capability
        and close_capability["descripcion"] == OF_CLOSE_CAPABILITY_MARKER
    ):
        close_roles = {
            row[0]
            for row in bind.execute(sa.text("""
                SELECT role.codigo
                FROM scm_rol_capacidad AS relation
                JOIN rol_operativo AS role
                  ON role.id = relation.rol_operativo_id
                WHERE relation.capacidad_id = :capability_id
            """), {"capability_id": close_capability["id"]})
        }
        if close_roles != set(OF_CLOSE_ROLES):
            raise RuntimeError(
                "Downgrade bloqueado: los grants OF_CERRAR cambiaron desde "
                "la migración; preserve la autorización con forward fix."
            )
        op.execute(sa.text("""
            DELETE FROM scm_rol_capacidad
            WHERE capacidad_id = :capability_id
        """).bindparams(capability_id=close_capability["id"]))
        op.execute(sa.text("""
            DELETE FROM scm_capacidad
            WHERE id = :capability_id AND descripcion = :marker
        """).bindparams(
            capability_id=close_capability["id"],
            marker=OF_CLOSE_CAPABILITY_MARKER,
        ))

    op.drop_table("scm_movimiento_wip_salida")
    with op.batch_alter_table("scm_consumo_componente_armado") as batch:
        batch.drop_index("ix_scm_consumo_armado_reserva_wip")
        batch.drop_constraint(
            "ck_scm_consumo_armado_procedencia_fuente", type_="check"
        )
        batch.drop_constraint(
            "ck_scm_consumo_armado_procedencia", type_="check"
        )
        batch.drop_constraint(
            "ck_scm_consumo_armado_fuente_unica", type_="check"
        )
        batch.drop_constraint(
            "uq_scm_consumo_armado_reserva_wip", type_="unique"
        )
        batch.drop_constraint(
            "fk_scm_consumo_armado_reserva_wip", type_="foreignkey"
        )
        batch.create_check_constraint(
            "ck_scm_consumo_armado_fuente_unica",
            "(asignacion_abastecimiento_id IS NOT NULL) <> "
            "(asignacion_pool_id IS NOT NULL)",
        )
        batch.drop_column("procedencia")
        batch.drop_column("reserva_wip_salida_id")
    op.drop_table("scm_reserva_wip_salida")
    op.drop_table("scm_saldo_wip_salida")
