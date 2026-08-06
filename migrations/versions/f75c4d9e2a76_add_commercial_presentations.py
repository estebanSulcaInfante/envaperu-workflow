"""add commercial presentations

Revision ID: f75c4d9e2a76
Revises: f74b3c8d1e65
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa


revision = "f75c4d9e2a76"
down_revision = "f74b3c8d1e65"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_presentacion_comercial",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(length=32), nullable=False),
        sa.Column("producto_terminado_id", sa.String(length=50), nullable=False),
        sa.Column("nombre", sa.String(length=100), nullable=False),
        sa.Column("unidades_base", sa.Integer(), nullable=False),
        sa.Column("codigo_barra", sa.String(length=50), nullable=True),
        sa.Column(
            "predeterminada",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "unidades_base > 0",
            name="ck_scm_presentacion_comercial_unidades_base",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_presentacion_comercial_version",
        ),
        sa.ForeignKeyConstraint(
            ["producto_terminado_id"],
            ["producto_terminado.cod_sku_pt"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "codigo",
            name="uq_scm_presentacion_comercial_codigo",
        ),
        sa.UniqueConstraint(
            "producto_terminado_id",
            "nombre",
            name="uq_scm_presentacion_comercial_producto_nombre",
        ),
    )
    op.create_index(
        "uq_scm_presentacion_comercial_predeterminada_activa",
        "scm_presentacion_comercial",
        ["producto_terminado_id"],
        unique=True,
        postgresql_where=sa.text("predeterminada AND activo"),
        sqlite_where=sa.text("predeterminada = 1 AND activo = 1"),
    )
    op.create_index(
        "uq_scm_presentacion_comercial_codigo_barra",
        "scm_presentacion_comercial",
        ["codigo_barra"],
        unique=True,
        postgresql_where=sa.text("codigo_barra IS NOT NULL"),
        sqlite_where=sa.text("codigo_barra IS NOT NULL"),
    )

    op.add_column(
        "scm_orden_produccion_linea",
        sa.Column("presentacion_comercial_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "scm_orden_produccion_linea",
        sa.Column("cantidad_presentaciones", sa.Integer(), nullable=True),
    )
    op.add_column(
        "scm_orden_produccion_linea",
        sa.Column("snapshot_presentacion_codigo", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "scm_orden_produccion_linea",
        sa.Column("snapshot_presentacion_nombre", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "scm_orden_produccion_linea",
        sa.Column("snapshot_unidades_por_presentacion", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_scm_op_linea_presentacion_comercial",
        "scm_orden_produccion_linea",
        "scm_presentacion_comercial",
        ["presentacion_comercial_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_scm_op_linea_presentacion_snapshot",
        "scm_orden_produccion_linea",
        "(presentacion_comercial_id IS NULL "
        "AND cantidad_presentaciones IS NULL "
        "AND snapshot_presentacion_codigo IS NULL "
        "AND snapshot_presentacion_nombre IS NULL "
        "AND snapshot_unidades_por_presentacion IS NULL) OR "
        "(presentacion_comercial_id IS NOT NULL "
        "AND cantidad_presentaciones > 0 "
        "AND snapshot_presentacion_codigo IS NOT NULL "
        "AND snapshot_presentacion_nombre IS NOT NULL "
        "AND snapshot_unidades_por_presentacion > 0)",
    )

    connection = op.get_bind()
    products = connection.execute(sa.text(
        "SELECT cod_sku_pt, codigo_barra "
        "FROM producto_terminado ORDER BY cod_sku_pt"
    )).mappings().all()
    presentation_table = sa.table(
        "scm_presentacion_comercial",
        sa.column("codigo", sa.String),
        sa.column("producto_terminado_id", sa.String),
        sa.column("nombre", sa.String),
        sa.column("unidades_base", sa.Integer),
        sa.column("codigo_barra", sa.String),
        sa.column("predeterminada", sa.Boolean),
        sa.column("activo", sa.Boolean),
        sa.column("version", sa.Integer),
    )
    if products:
        op.bulk_insert(presentation_table, [
            {
                "codigo": f"PRE-{index:06d}",
                "producto_terminado_id": product["cod_sku_pt"],
                "nombre": "Unidad",
                "unidades_base": 1,
                "codigo_barra": product["codigo_barra"],
                "predeterminada": True,
                "activo": True,
                "version": 1,
            }
            for index, product in enumerate(products, start=1)
        ])

    next_value = len(products) + 1
    existing_counter = connection.execute(sa.text(
        "SELECT clave FROM correlativo_catalogo "
        "WHERE clave = 'PRESENTACION_COMERCIAL'"
    )).first()
    if existing_counter:
        connection.execute(sa.text(
            "UPDATE correlativo_catalogo "
            "SET prefijo = 'PRE', siguiente_valor = :next_value, ancho = 6 "
            "WHERE clave = 'PRESENTACION_COMERCIAL'"
        ), {"next_value": next_value})
    else:
        connection.execute(sa.text(
            "INSERT INTO correlativo_catalogo "
            "(clave, prefijo, siguiente_valor, ancho) "
            "VALUES ('PRESENTACION_COMERCIAL', 'PRE', :next_value, 6)"
        ), {"next_value": next_value})

    if connection.dialect.name == "postgresql":
        op.execute("ALTER TABLE scm_presentacion_comercial ENABLE ROW LEVEL SECURITY")


def downgrade():
    op.drop_constraint(
        "ck_scm_op_linea_presentacion_snapshot",
        "scm_orden_produccion_linea",
        type_="check",
    )
    op.drop_constraint(
        "fk_scm_op_linea_presentacion_comercial",
        "scm_orden_produccion_linea",
        type_="foreignkey",
    )
    op.drop_column("scm_orden_produccion_linea", "snapshot_unidades_por_presentacion")
    op.drop_column("scm_orden_produccion_linea", "snapshot_presentacion_nombre")
    op.drop_column("scm_orden_produccion_linea", "snapshot_presentacion_codigo")
    op.drop_column("scm_orden_produccion_linea", "cantidad_presentaciones")
    op.drop_column("scm_orden_produccion_linea", "presentacion_comercial_id")
    op.drop_index(
        "uq_scm_presentacion_comercial_codigo_barra",
        table_name="scm_presentacion_comercial",
    )
    op.drop_index(
        "uq_scm_presentacion_comercial_predeterminada_activa",
        table_name="scm_presentacion_comercial",
    )
    op.drop_table("scm_presentacion_comercial")
    op.execute(
        "DELETE FROM correlativo_catalogo "
        "WHERE clave = 'PRESENTACION_COMERCIAL'"
    )
