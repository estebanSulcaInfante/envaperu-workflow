"""add open manga continuity between shifts

Revision ID: f88c3e5a7b42
Revises: f87b2d4e6a31
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa


revision = "f88c3e5a7b42"
down_revision = "f87b2d4e6a31"
branch_labels = None
depends_on = None


CAPABILITIES = (
    (
        "MANGA_CONTROL_PESO_REGISTRAR",
        "Registrar un corte acumulado de una manga que continua abierta",
        ("MAQUINISTA", "OPERADOR_PESAJE", "SUPERVISOR", "JEFE_PRODUCCION", "GERENTE_GENERAL"),
    ),
    (
        "MANGA_TRANSFERIR_OT",
        "Vincular una manga abierta a una OT compatible",
        ("SUPERVISOR", "JEFE_PRODUCCION", "GERENTE_GENERAL"),
    ),
)


def _seed_capabilities():
    for code, name, roles in CAPABILITIES:
        op.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
            WHERE NOT EXISTS (
                SELECT 1 FROM scm_capacidad WHERE codigo = :code
            )
        """).bindparams(code=code, name=name))
        for role in roles:
            op.execute(sa.text("""
                INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
                SELECT rol.id, capacidad.id
                FROM rol_operativo rol
                JOIN scm_capacidad capacidad ON capacidad.codigo = :capability
                WHERE rol.codigo = :role
                  AND NOT EXISTS (
                    SELECT 1 FROM scm_rol_capacidad relacion
                    WHERE relacion.rol_operativo_id = rol.id
                      AND relacion.capacidad_id = capacidad.id
                  )
            """).bindparams(role=role, capability=code))


def upgrade():
    with op.batch_alter_table("scm_manga") as batch:
        batch.drop_constraint("ck_scm_manga_estado", type_="check")
        batch.create_check_constraint(
            "ck_scm_manga_estado",
            "estado IN ('PLANIFICADA', 'PREETIQUETADA', 'EN_ARMADO', "
            "'CONTINUIDAD_PENDIENTE', 'EN_LLENADO', "
            "'CERRADA_ARMADO_PENDIENTE_PESAJE', 'PESADA', "
            "'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN', "
            "'RECIBIDA', 'ANULADA')",
        )

    op.create_table(
        "scm_tramo_manga_trabajo",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("trabajo_ot_id", sa.Uuid(), nullable=False),
        sa.Column("asignacion_personal_trabajo_id", sa.Uuid(), nullable=False),
        sa.Column("asignacion_plan_id", sa.Integer(), nullable=True),
        sa.Column("secuencia", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(length=20), server_default="PROGRAMADO", nullable=False),
        sa.Column("cantidad_inicio_un", sa.Numeric(15, 3), nullable=False),
        sa.Column("cantidad_fin_un", sa.Numeric(15, 3), nullable=True),
        sa.Column("cantidad_atribuida_un", sa.Numeric(15, 3), server_default="0", nullable=False),
        sa.Column("iniciada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cerrada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("motivo_cierre", sa.String(length=500), nullable=True),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "estado IN ('PROGRAMADO', 'ACTIVO', 'CERRADO', 'ANULADO')",
            name="ck_scm_tramo_manga_estado",
        ),
        sa.CheckConstraint(
            "secuencia > 0 AND cantidad_inicio_un >= 0",
            name="ck_scm_tramo_manga_inicio",
        ),
        sa.CheckConstraint(
            "cantidad_fin_un IS NULL OR cantidad_fin_un > cantidad_inicio_un",
            name="ck_scm_tramo_manga_fin",
        ),
        sa.CheckConstraint(
            "cantidad_atribuida_un >= 0",
            name="ck_scm_tramo_manga_atribuida",
        ),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["trabajo_ot_id"], ["scm_trabajo_ot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["asignacion_personal_trabajo_id"],
            ["scm_asignacion_personal_trabajo_ot.id"],
            name="fk_scm_tramo_manga_asignacion_personal",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["asignacion_plan_id"], ["scm_asignacion_plan_manga_ot.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["created_by_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manga_id", "secuencia", name="uq_scm_tramo_manga_secuencia"),
    )
    op.create_index("ix_scm_tramo_manga_trabajo", "scm_tramo_manga_trabajo", ["trabajo_ot_id"])
    op.create_index(
        "ix_scm_tramo_manga_asignacion",
        "scm_tramo_manga_trabajo",
        ["asignacion_personal_trabajo_id"],
    )
    op.create_index(
        "uq_scm_tramo_manga_abierto",
        "scm_tramo_manga_trabajo",
        ["manga_id"],
        unique=True,
        postgresql_where=sa.text("estado IN ('PROGRAMADO', 'ACTIVO')"),
        sqlite_where=sa.text("estado IN ('PROGRAMADO', 'ACTIVO')"),
    )

    op.create_table(
        "scm_control_peso_manga",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("tramo_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("source_system", sa.String(length=40), nullable=False),
        sa.Column("station_id", sa.String(length=36), nullable=False),
        sa.Column("capture_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(length=24), server_default="CORTE_TURNO", nullable=False),
        sa.Column("peso_bruto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("tara_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("peso_neto_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("tara_fuente", sa.String(length=28), nullable=False),
        sa.Column("conteo_acumulado_un", sa.Numeric(15, 3), nullable=False),
        sa.Column("motivo", sa.String(length=500), nullable=False),
        sa.Column("pesado_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone_snapshot", sa.String(length=64), server_default="America/Lima", nullable=False),
        sa.Column("fecha_local_pesaje", sa.Date(), nullable=False),
        sa.Column("pesado_por_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("tipo = 'CORTE_TURNO'", name="ck_scm_control_peso_manga_tipo"),
        sa.CheckConstraint(
            "peso_bruto_kg > 0 AND tara_kg >= 0 AND peso_neto_kg > 0",
            name="ck_scm_control_peso_manga_pesos",
        ),
        sa.CheckConstraint(
            "peso_neto_kg = peso_bruto_kg - tara_kg",
            name="ck_scm_control_peso_manga_neto",
        ),
        sa.CheckConstraint("conteo_acumulado_un > 0", name="ck_scm_control_peso_manga_conteo"),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tramo_id"], ["scm_tramo_manga_trabajo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["station_id"], ["estacion_pesaje.station_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["pesado_por_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_control_peso_manga_public"),
        sa.UniqueConstraint("tramo_id", name="uq_scm_control_peso_manga_tramo"),
        sa.UniqueConstraint("operation_id", name="uq_scm_control_peso_manga_operation"),
        sa.UniqueConstraint("source_system", "capture_id", name="uq_scm_control_peso_manga_capture"),
    )
    op.create_index("ix_scm_control_peso_manga_manga", "scm_control_peso_manga", ["manga_id"])
    _seed_capabilities()


def downgrade():
    for code, _name, _roles in CAPABILITIES:
        op.execute(sa.text("""
            DELETE FROM scm_rol_capacidad
            WHERE capacidad_id IN (
                SELECT id FROM scm_capacidad WHERE codigo = :code
            )
        """).bindparams(code=code))
        op.execute(sa.text("DELETE FROM scm_capacidad WHERE codigo = :code").bindparams(code=code))

    op.drop_index("ix_scm_control_peso_manga_manga", table_name="scm_control_peso_manga")
    op.drop_table("scm_control_peso_manga")
    op.drop_index("uq_scm_tramo_manga_abierto", table_name="scm_tramo_manga_trabajo")
    op.drop_index("ix_scm_tramo_manga_asignacion", table_name="scm_tramo_manga_trabajo")
    op.drop_index("ix_scm_tramo_manga_trabajo", table_name="scm_tramo_manga_trabajo")
    op.drop_table("scm_tramo_manga_trabajo")

    with op.batch_alter_table("scm_manga") as batch:
        batch.drop_constraint("ck_scm_manga_estado", type_="check")
        batch.create_check_constraint(
            "ck_scm_manga_estado",
            "estado IN ('PLANIFICADA', 'PREETIQUETADA', 'EN_ARMADO', "
            "'CERRADA_ARMADO_PENDIENTE_PESAJE', 'PESADA', "
            "'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN', "
            "'RECIBIDA', 'ANULADA')",
        )

