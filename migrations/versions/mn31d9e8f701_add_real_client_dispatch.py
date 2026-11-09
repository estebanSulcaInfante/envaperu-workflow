"""Real MN31 direct client dispatch ledger and authorization."""

from alembic import op
import sqlalchemy as sa


revision = "mn31d9e8f701"
down_revision = "fc4e5f607182"
branch_labels = None
depends_on = None


def _replace_check(table, name, expression):
    with op.batch_alter_table(table) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, expression)


def upgrade():
    op.create_table(
        "scm_cliente_mn31",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("ruc", sa.String(length=20), nullable=False),
        sa.Column("nombre", sa.String(length=240), nullable=False),
        sa.Column("contrato", sa.String(length=120), nullable=True),
        sa.Column("grupo", sa.String(length=80), nullable=True),
        sa.Column("cantidad_fuente", sa.Numeric(15, 3), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(trim(ruc)) > 0", name="ck_scm_cliente_mn31_ruc"),
        sa.CheckConstraint("length(trim(nombre)) > 0", name="ck_scm_cliente_mn31_nombre"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_cliente_mn31_public_id"),
        sa.UniqueConstraint("ruc", name="uq_scm_cliente_mn31_ruc"),
    )
    op.create_table(
        "scm_mn31_lote_asignacion",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("manga_public_id", sa.Uuid(), nullable=False),
        sa.Column("manga_codigo", sa.String(length=80), nullable=False),
        sa.Column("lote_public_id", sa.Uuid(), nullable=False),
        sa.Column("lote_codigo", sa.String(length=80), nullable=False),
        sa.Column("fuente", sa.String(length=80), nullable=False, server_default="DOCUMENTAL_MN31"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manga_id", name="uq_scm_mn31_lote_manga"),
        sa.UniqueConstraint("manga_public_id", name="uq_scm_mn31_lote_manga_public"),
    )
    op.bulk_insert(
        sa.table("scm_capacidad", sa.column("codigo", sa.String), sa.column("nombre", sa.String), sa.column("descripcion", sa.String), sa.column("activo", sa.Boolean)),
        [{"codigo": "DESPACHO_MN31_CONFIRMAR", "nombre": "Confirmar despacho MN31 a cliente", "descripcion": "Autoriza salida directa MN31 por identidad KG", "activo": True}],
    )
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, cap.id FROM rol_operativo rol CROSS JOIN scm_capacidad cap
        WHERE rol.codigo IN ('ALMACEN_RECEPCION', 'GERENTE_GENERAL', 'GERENCIA')
          AND cap.codigo = 'DESPACHO_MN31_CONFIRMAR'
          AND NOT EXISTS (SELECT 1 FROM scm_rol_capacidad rc WHERE rc.rol_operativo_id = rol.id AND rc.capacidad_id = cap.id)
    """))
    _replace_check("scm_movimiento_inventario_kg", "ck_scm_movimiento_inventario_kg_tipo", "tipo IN ('INGRESO_PRODUCCION', 'AJUSTE_POSITIVO', 'AJUSTE_NEGATIVO', 'TRASLADO_SALIDA', 'TRASLADO_ENTRADA', 'RETIRO_ARMADO', 'RETORNO_ENTRADA', 'SALIDA_CLIENTE_MN31')")
    _replace_check("scm_existencia_manga_kg", "ck_scm_existencia_manga_kg_logistica", "estado_logistico IN ('EN_PRODUCCION', 'DISPONIBLE_PRODUCCION', 'RECIBIDA_ALMACEN', 'REVERSADA', 'DESPACHADA_CLIENTE_MN31')")
    _replace_check("scm_unidad_fisica_kg", "ck_scm_unidad_fisica_kg_logistica", "estado_logistico IN ('EN_PRODUCCION', 'DISPONIBLE_PRODUCCION', 'RECIBIDA_ALMACEN', 'ALMACENADA_CONTROLADA', 'RESERVADA', 'RETIRADA_ARMADO', 'RETIRADA_CLIENTE_MN31', 'REPESADA_PENDIENTE_RECEPCION', 'PENDIENTE_CALIDAD', 'PENDIENTE_VERIFICACION', 'REVERSADA')")
    _replace_check("scm_manga", "ck_scm_manga_estado", "estado IN ('PLANIFICADA', 'PREETIQUETADA', 'EN_ARMADO', 'CONTINUIDAD_PENDIENTE', 'EN_LLENADO', 'CERRADA_ARMADO_PENDIENTE_PESAJE', 'PESADA', 'ETIQUETADA_FINAL', 'PENDIENTE_RECEPCION_ALMACEN', 'RECIBIDA', 'DESPACHADA_CLIENTE_MN31', 'ANULADA')")


def downgrade():
    raise RuntimeError("MN31 client dispatch is append-only and is not downgraded")
