"""add canonical KG custody identities, reservations and measured returns

Revision ID: f95a1b2c3d05
Revises: f94a1b2c3d04
"""

from alembic import op
import sqlalchemy as sa
import hashlib
import json
from uuid import uuid4


revision = "f95a1b2c3d05"
down_revision = "f94a1b2c3d04"
branch_labels = None
depends_on = None


def upgrade():
    # Existing KG receipts remain valid.  The nullable bridge is filled below
    # before the service starts requiring a canonical identity.
    op.add_column("scm_existencia_manga_kg", sa.Column("unidad_fisica_kg_id", sa.Uuid(), nullable=True))
    op.add_column("scm_existencia_manga_kg", sa.Column("origen_tipo", sa.String(16), nullable=False, server_default="PRODUCCION"))
    op.add_column("scm_existencia_manga_kg", sa.Column("calidad_actor_id", sa.Integer(), nullable=True))
    op.add_column("scm_existencia_manga_kg", sa.Column("calidad_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("scm_existencia_manga_kg", sa.Column("calidad_motivo", sa.String(500), nullable=True))
    op.add_column("scm_existencia_manga_kg", sa.Column("calidad_evidencia", sa.String(500), nullable=True))
    op.add_column("scm_movimiento_inventario_kg", sa.Column("fuente_tipo", sa.String(32), nullable=False, server_default="KG001"))
    op.add_column("scm_movimiento_inventario_kg", sa.Column("medicion_unidad_kg_id", sa.Uuid(), nullable=True))
    op.alter_column("scm_existencia_manga_kg", "manga_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("scm_existencia_manga_kg", "etiqueta_resuelta_id", existing_type=sa.Integer(), nullable=True)
    op.alter_column("scm_existencia_manga_kg", "pesaje_public_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column("scm_existencia_manga_kg", "projection_sha256", existing_type=sa.String(64), nullable=True)

    op.create_table(
        "scm_unidad_fisica_kg",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(120), nullable=False), sa.Column("articulo_scm_id", sa.Integer(), nullable=False),
        sa.Column("existencia_manga_kg_id", sa.Uuid(), nullable=True), sa.Column("unidad_padre_id", sa.Uuid(), nullable=True),
        sa.Column("unidad_raiz_id", sa.Uuid(), nullable=True), sa.Column("division_id", sa.Uuid(), nullable=True),
        sa.Column("intencion", sa.String(16), nullable=True), sa.Column("estado", sa.String(16), nullable=False, server_default="ACTIVA"),
        sa.Column("estado_logistico", sa.String(40), nullable=False, server_default="RECIBIDA_ALMACEN"),
        sa.Column("estado_calidad", sa.String(20), nullable=False, server_default="PENDIENTE"),
        sa.Column("saldo_id", sa.Uuid(), nullable=True), sa.Column("ubicacion_id", sa.Integer(), nullable=True),
        sa.Column("recepcion_vigente_id", sa.Uuid(), nullable=True), sa.Column("medicion_vigente_id", sa.Uuid(), nullable=True),
        sa.Column("modo_lectura", sa.String(32), nullable=True), sa.Column("tara_contexto_json", sa.JSON(), nullable=True),
        sa.Column("kg_entregado", sa.Numeric(15, 3), nullable=True), sa.Column("kg_verificados", sa.Numeric(15, 3), nullable=True),
        sa.Column("kg_verificados_at", sa.DateTime(timezone=True), nullable=True), sa.Column("almacen_responsable_id", sa.Uuid(), nullable=True),
        sa.Column("tenedor_fisico_id", sa.Integer(), nullable=True), sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("estado IN ('ACTIVA', 'HISTORICA')", name="ck_scm_unidad_fisica_kg_estado"),
        sa.CheckConstraint("estado_logistico IN ('RECIBIDA_ALMACEN', 'ALMACENADA_CONTROLADA', 'RESERVADA', 'RETIRADA_ARMADO', 'REPESADA_PENDIENTE_RECEPCION', 'PENDIENTE_CALIDAD', 'PENDIENTE_VERIFICACION', 'REVERSADA')", name="ck_scm_unidad_fisica_kg_logistica"),
        sa.CheckConstraint("(kg_entregado IS NULL OR kg_entregado > 0) AND (kg_verificados IS NULL OR kg_verificados > 0)", name="ck_scm_unidad_fisica_kg_kg"),
        sa.ForeignKeyConstraint(["articulo_scm_id"], ["scm_articulo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["existencia_manga_kg_id"], ["scm_existencia_manga_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["unidad_padre_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["unidad_raiz_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["saldo_id"], ["scm_saldo_inventario_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenedor_fisico_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["almacen_responsable_id"], ["scm_almacen.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("public_id", name="uq_scm_unidad_fisica_kg_public_id"),
        sa.UniqueConstraint("codigo", name="uq_scm_unidad_fisica_kg_codigo"),
    )
    op.create_table(
        "scm_division_unidad_kg",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("padre_id", sa.Uuid(), nullable=False),
        sa.Column("estado", sa.String(16), nullable=False, server_default="CONFIRMADA"), sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("estado IN ('CONFIRMADA', 'REVERSADA')", name="ck_scm_division_unidad_kg_estado"),
        sa.ForeignKeyConstraint(["padre_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("padre_id", name="uq_scm_division_unidad_kg_parent"), sa.UniqueConstraint("operation_id", name="uq_scm_division_unidad_kg_operation"),
    )
    op.create_foreign_key("fk_scm_unidad_fisica_kg_division", "scm_unidad_fisica_kg", "scm_division_unidad_kg", ["division_id"], ["id"], ondelete="RESTRICT")
    op.create_table(
        "scm_reserva_unidad_kg",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("unidad_id", sa.Uuid(), nullable=False), sa.Column("cantidad_snapshot_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("documento_destino_tipo", sa.String(32)), sa.Column("documento_destino_id", sa.String(120)), sa.Column("motivo_operativo", sa.String(500)),
        sa.Column("estado", sa.String(16), nullable=False, server_default="ACTIVA"), sa.Column("actor_id", sa.Integer(), nullable=False), sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("estado IN ('ACTIVA', 'RETIRADA', 'LIBERADA')", name="ck_scm_reserva_unidad_kg_estado"), sa.ForeignKeyConstraint(["unidad_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "scm_retiro_armado_kg",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("codigo", sa.String(40), nullable=False), sa.Column("documento_destino_tipo", sa.String(32)), sa.Column("documento_destino_id", sa.String(120)),
        sa.Column("almacen_responsable_id", sa.Uuid()), sa.Column("actor_id", sa.Integer(), nullable=False), sa.Column("tenedor_fisico_id", sa.Integer()), sa.Column("motivo_operativo", sa.String(500)),
        sa.Column("estado", sa.String(32), nullable=False, server_default="ABIERTO"), sa.Column("operation_id", sa.Uuid(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("estado IN ('ABIERTO', 'RETORNADO_TOTAL', 'CONCILIACION_PENDIENTE')", name="ck_scm_retiro_armado_kg_estado"), sa.ForeignKeyConstraint(["almacen_responsable_id"], ["scm_almacen.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["tenedor_fisico_id"], ["trabajador.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("codigo"), sa.UniqueConstraint("operation_id", name="uq_scm_retiro_armado_kg_operation"),
    )
    op.create_table(
        "scm_retiro_armado_kg_item",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("retiro_id", sa.Uuid(), nullable=False), sa.Column("unidad_id", sa.Uuid(), nullable=False), sa.Column("reserva_id", sa.Uuid(), nullable=False), sa.Column("neto_entregado_kg", sa.Numeric(15, 3), nullable=False), sa.Column("movimiento_id", sa.Uuid()),
        sa.ForeignKeyConstraint(["retiro_id"], ["scm_retiro_armado_kg.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["unidad_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["reserva_id"], ["scm_reserva_unidad_kg.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["movimiento_id"], ["scm_movimiento_inventario_kg.id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("retiro_id", "unidad_id", name="uq_scm_retiro_armado_kg_item_unit"),
    )
    op.create_table(
        "scm_medicion_unidad_kg",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("unidad_id", sa.Uuid(), nullable=False), sa.Column("retiro_id", sa.Uuid(), nullable=True), sa.Column("intencion", sa.String(16), nullable=False), sa.Column("station_id", sa.String(80), nullable=False), sa.Column("reading_id", sa.String(120), nullable=False), sa.Column("captured_at_utc", sa.DateTime(timezone=True), nullable=False), sa.Column("reading_stable", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("modo_lectura", sa.String(32), nullable=False), sa.Column("bruto_kg", sa.Numeric(15, 3)), sa.Column("tara_kg", sa.Numeric(15, 3)), sa.Column("neto_kg", sa.Numeric(15, 3), nullable=False), sa.Column("station_version", sa.String(80)), sa.Column("payload_hash", sa.String(64), nullable=False), sa.Column("actor_id", sa.Integer(), nullable=False), sa.Column("operation_id", sa.Uuid(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("intencion IN ('RETORNO', 'DIVISION')", name="ck_scm_medicion_unidad_kg_intencion"), sa.CheckConstraint("neto_kg > 0", name="ck_scm_medicion_unidad_kg_neto"), sa.ForeignKeyConstraint(["unidad_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["retiro_id"], ["scm_retiro_armado_kg.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("station_id", "reading_id", name="uq_scm_medicion_unidad_kg_station_reading"), sa.UniqueConstraint("operation_id", name="uq_scm_medicion_unidad_kg_operation"),
    )
    op.create_table(
        "scm_etiqueta_unidad_kg",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("public_id", sa.Uuid(), nullable=False), sa.Column("unidad_id", sa.Uuid(), nullable=False), sa.Column("version", sa.Integer(), nullable=False, server_default="1"), sa.Column("estado", sa.String(24), nullable=False, server_default="GENERADA"), sa.Column("payload_json", sa.JSON(), nullable=False, server_default="{}"), sa.Column("payload_hash", sa.String(64), nullable=False), sa.Column("print_job_id", sa.String(120)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("estado IN ('GENERADA', 'IMPRESA', 'EMISION_INCIERTA', 'INVALIDADA')", name="ck_scm_etiqueta_unidad_kg_estado"), sa.ForeignKeyConstraint(["unidad_id"], ["scm_unidad_fisica_kg.id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("public_id", name="uq_scm_etiqueta_unidad_kg_public_id"), sa.UniqueConstraint("unidad_id", "version", name="uq_scm_etiqueta_unidad_kg_version"),
    )
    op.create_foreign_key("fk_scm_existencia_manga_kg_unit", "scm_existencia_manga_kg", "scm_unidad_fisica_kg", ["unidad_fisica_kg_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_scm_existencia_manga_kg_quality_actor", "scm_existencia_manga_kg", "trabajador", ["calidad_actor_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_scm_unidad_fisica_kg_recepcion", "scm_unidad_fisica_kg", "scm_existencia_manga_kg", ["recepcion_vigente_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_scm_unidad_fisica_kg_measurement", "scm_unidad_fisica_kg", "scm_medicion_unidad_kg", ["medicion_vigente_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_scm_movimiento_kg_measurement", "scm_movimiento_inventario_kg", "scm_medicion_unidad_kg", ["medicion_unidad_kg_id"], ["id"], ondelete="RESTRICT")

    bind = op.get_bind()
    # Deterministic backfill: one root per existing KG receipt; no new stock facts.
    rows = bind.execute(sa.text("SELECT id, manga_id, articulo_scm_id, saldo_id, ubicacion_id, cantidad_fisica_kg, peso_neto_snapshot_kg, pesada_at_snapshot, estado_logistico, estado_calidad, version FROM scm_existencia_manga_kg ORDER BY id")).mappings().all()
    for row in rows:
        root_id = bind.execute(sa.text("SELECT gen_random_uuid()")).scalar_one()
        public_id = bind.execute(sa.text("SELECT gen_random_uuid()")).scalar_one()
        code = f"KG-{row['manga_id']}-{str(public_id)[:8].upper()}"
        warehouse_id = bind.execute(sa.text("SELECT almacen_id FROM scm_ubicacion_inventario WHERE id=:id"), {"id": row["ubicacion_id"]}).scalar_one_or_none()
        bind.execute(sa.text("INSERT INTO scm_unidad_fisica_kg (id,public_id,codigo,articulo_scm_id,existencia_manga_kg_id,unidad_raiz_id,estado,estado_logistico,estado_calidad,saldo_id,ubicacion_id,recepcion_vigente_id,kg_entregado,kg_verificados,kg_verificados_at,almacen_responsable_id,version) VALUES (:id,:public,:code,:article,:exist,:root,:state,:logistic,:quality,:saldo,:location,:exist,:kg,:verified,:verified_at,:warehouse,:version)"), {"id":root_id,"public":public_id,"code":code,"article":row["articulo_scm_id"],"exist":row["id"],"root":root_id,"state":"ACTIVA","logistic":row["estado_logistico"],"quality":row["estado_calidad"],"saldo":row["saldo_id"],"location":row["ubicacion_id"],"kg":row["cantidad_fisica_kg"],"verified":row["peso_neto_snapshot_kg"] or row["cantidad_fisica_kg"],"verified_at":row["pesada_at_snapshot"],"warehouse":warehouse_id,"version":row["version"]})
        bind.execute(sa.text("UPDATE scm_existencia_manga_kg SET unidad_fisica_kg_id=:unit WHERE id=:exist"), {"unit":root_id,"exist":row["id"]})
        label_public_id = uuid4()
        payload = {"v": 1, "label_id": str(label_public_id), "qr_value": json.dumps({"v": 1, "label_id": str(label_public_id)}, separators=(",", ":"))}
        bind.execute(sa.text("INSERT INTO scm_etiqueta_unidad_kg (id,public_id,unidad_id,version,estado,payload_json,payload_hash) VALUES (:id,:public,:unit,1,'GENERADA',CAST(:payload AS json),:hash)"), {"id": uuid4(), "public": label_public_id, "unit": root_id, "payload": json.dumps(payload), "hash": hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()})

    op.add_column("scm_sesion_operacion_item", sa.Column("unidad_fisica_kg_id", sa.Uuid(), nullable=True))
    op.add_column("scm_transferencia_item", sa.Column("unidad_fisica_kg_id", sa.Uuid(), nullable=True))
    op.add_column("scm_transferencia_inventario", sa.Column("unidad_inventario", sa.String(10), nullable=False, server_default="UN"))
    op.add_column("scm_transferencia_inventario", sa.Column("almacen_responsable_id", sa.Uuid(), nullable=True))
    op.add_column("scm_transferencia_inventario", sa.Column("tenedor_fisico_id", sa.Integer(), nullable=True))
    op.alter_column("scm_sesion_operacion_item", "existencia_manga_id", existing_type=sa.Uuid(), nullable=True)
    op.alter_column("scm_transferencia_item", "existencia_manga_id", existing_type=sa.Uuid(), nullable=True)
    op.create_foreign_key("fk_scm_session_item_kg_unit", "scm_sesion_operacion_item", "scm_unidad_fisica_kg", ["unidad_fisica_kg_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_scm_transfer_item_kg_unit", "scm_transferencia_item", "scm_unidad_fisica_kg", ["unidad_fisica_kg_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_scm_transfer_kg_warehouse", "scm_transferencia_inventario", "scm_almacen", ["almacen_responsable_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_scm_transfer_kg_holder", "scm_transferencia_inventario", "trabajador", ["tenedor_fisico_id"], ["id"], ondelete="RESTRICT")

    if bind.dialect.name == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION scm_kg_custody_append_only()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'KG_CUSTODY_APPEND_ONLY: %', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql
        """))
        for table in ("scm_medicion_unidad_kg", "scm_retiro_armado_kg_item"):
            op.execute(sa.text(
                f"CREATE TRIGGER {table}_append_only "
                f"BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION scm_kg_custody_append_only()"
            ))


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in ("scm_medicion_unidad_kg", "scm_retiro_armado_kg_item"):
            op.execute(sa.text(
                f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}"
            ))
        op.execute(sa.text(
            "DROP FUNCTION IF EXISTS scm_kg_custody_append_only()"
        ))
    op.drop_constraint("fk_scm_existencia_manga_kg_quality_actor", "scm_existencia_manga_kg", type_="foreignkey")
    op.drop_constraint("fk_scm_unidad_fisica_kg_measurement", "scm_unidad_fisica_kg", type_="foreignkey")
    op.drop_constraint("fk_scm_unidad_fisica_kg_recepcion", "scm_unidad_fisica_kg", type_="foreignkey")
    for table in ("scm_reserva_unidad_kg", "scm_retiro_armado_kg_item", "scm_retiro_armado_kg", "scm_medicion_unidad_kg", "scm_division_unidad_kg", "scm_etiqueta_unidad_kg"):
        if bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one():
            raise RuntimeError("downgrade KG custody bloqueado: existen datos de custodia")
    for name, table in (("fk_scm_transfer_kg_holder", "scm_transferencia_inventario"), ("fk_scm_transfer_kg_warehouse", "scm_transferencia_inventario"), ("fk_scm_transfer_item_kg_unit", "scm_transferencia_item"), ("fk_scm_session_item_kg_unit", "scm_sesion_operacion_item"), ("fk_scm_movimiento_kg_measurement", "scm_movimiento_inventario_kg"), ("fk_scm_existencia_manga_kg_unit", "scm_existencia_manga_kg"), ("fk_scm_unidad_fisica_kg_division", "scm_unidad_fisica_kg")):
        op.drop_constraint(name, table, type_="foreignkey")
    op.alter_column("scm_sesion_operacion_item", "existencia_manga_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("scm_transferencia_item", "existencia_manga_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("scm_existencia_manga_kg", "projection_sha256", existing_type=sa.String(64), nullable=False)
    op.alter_column("scm_existencia_manga_kg", "pesaje_public_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("scm_existencia_manga_kg", "etiqueta_resuelta_id", existing_type=sa.Integer(), nullable=False)
    op.alter_column("scm_existencia_manga_kg", "manga_id", existing_type=sa.Integer(), nullable=False)
    for table, column in (("scm_transferencia_inventario", "tenedor_fisico_id"), ("scm_transferencia_inventario", "almacen_responsable_id"), ("scm_transferencia_inventario", "unidad_inventario"), ("scm_transferencia_item", "unidad_fisica_kg_id"), ("scm_sesion_operacion_item", "unidad_fisica_kg_id"), ("scm_movimiento_inventario_kg", "medicion_unidad_kg_id"), ("scm_movimiento_inventario_kg", "fuente_tipo"), ("scm_existencia_manga_kg", "calidad_evidencia"), ("scm_existencia_manga_kg", "calidad_motivo"), ("scm_existencia_manga_kg", "calidad_at"), ("scm_existencia_manga_kg", "calidad_actor_id"), ("scm_existencia_manga_kg", "origen_tipo"), ("scm_existencia_manga_kg", "unidad_fisica_kg_id")):
        op.drop_column(table, column)
    for table in ("scm_etiqueta_unidad_kg", "scm_division_unidad_kg", "scm_medicion_unidad_kg", "scm_retiro_armado_kg_item", "scm_retiro_armado_kg", "scm_reserva_unidad_kg", "scm_unidad_fisica_kg"):
        op.drop_table(table)
