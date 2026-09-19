"""add protected kg subledger for piece/WIP receiving

Revision ID: f94a1b2c3d04
Revises: f93d4e6a8c02
"""

from alembic import op
import sqlalchemy as sa


revision = "f94a1b2c3d04"
down_revision = "f93d4e6a8c02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "scm_articulo",
        sa.Column("unidad_inventario", sa.String(length=10), nullable=False, server_default="UN"),
    )
    op.create_check_constraint(
        "ck_scm_articulo_unidad_inventario",
        "scm_articulo",
        "unidad_inventario IN ('UN', 'KG')",
    )
    op.create_check_constraint(
        "ck_scm_articulo_kg_class",
        "scm_articulo",
        "unidad_inventario <> 'KG' OR clase IN ('PIEZA_COLOR', 'SUBENSAMBLE_WIP')",
    )
    op.create_table(
        "scm_saldo_inventario_kg",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=False),
        sa.Column("ubicacion_id", sa.Integer(), nullable=False),
        sa.Column("cantidad_fisica_kg", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("cantidad_reservada_kg", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("cantidad_no_disponible_kg", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "cantidad_fisica_kg >= 0 AND cantidad_reservada_kg >= 0 AND cantidad_no_disponible_kg >= 0 AND cantidad_reservada_kg + cantidad_no_disponible_kg <= cantidad_fisica_kg",
            name="ck_scm_saldo_inventario_kg_cantidades",
        ),
        sa.ForeignKeyConstraint(["articulo_scm_id"], ["scm_articulo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("articulo_scm_id", "ubicacion_id", name="uq_scm_saldo_inventario_kg_articulo_ubicacion"),
    )
    op.create_table(
        "scm_movimiento_inventario_kg",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.String(32), nullable=False),
        sa.Column("cantidad_delta_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("saldo_fisico_resultante_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("motivo", sa.String(240), nullable=False),
        sa.Column("referencia_tipo", sa.String(40)), sa.Column("referencia_id", sa.String(100)),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("pesaje_public_id", sa.Uuid()), sa.Column("correccion_aplicada_public_id", sa.Uuid()),
        sa.Column("projection_sha256", sa.String(64), nullable=False),
        sa.Column("peso_neto_snapshot_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("pesada_at_snapshot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("tipo IN ('INGRESO_PRODUCCION', 'AJUSTE_POSITIVO', 'AJUSTE_NEGATIVO', 'TRASLADO_SALIDA', 'TRASLADO_ENTRADA', 'RETORNO_ENTRADA')", name="ck_scm_movimiento_inventario_kg_tipo"),
        sa.CheckConstraint("cantidad_delta_kg <> 0 AND saldo_fisico_resultante_kg >= 0", name="ck_scm_movimiento_inventario_kg_cantidad"),
        sa.ForeignKeyConstraint(["saldo_id"], ["scm_saldo_inventario_kg.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_id"], ["trabajador.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("operation_id", name="uq_scm_movimiento_inventario_kg_operation"),
    )
    op.create_table(
        "scm_existencia_manga_kg",
        sa.Column("id", sa.Uuid(), nullable=False), sa.Column("manga_id", sa.Integer(), nullable=False),
        sa.Column("sesion_id", sa.Uuid()), sa.Column("etiqueta_resuelta_id", sa.Integer(), nullable=False),
        sa.Column("articulo_scm_id", sa.Integer(), nullable=False), sa.Column("saldo_id", sa.Uuid(), nullable=False),
        sa.Column("ubicacion_id", sa.Integer(), nullable=False), sa.Column("movimiento_ingreso_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False), sa.Column("resuelta_por", sa.String(24), nullable=False),
        sa.Column("estado_logistico", sa.String(32), nullable=False, server_default="RECIBIDA_ALMACEN"),
        sa.Column("estado_calidad", sa.String(20), nullable=False, server_default="PENDIENTE"),
        sa.Column("cantidad_fisica_kg", sa.Numeric(15, 3), nullable=False), sa.Column("cantidad_reservada_kg", sa.Numeric(15, 3), nullable=False, server_default="0"),
        sa.Column("peso_neto_snapshot_kg", sa.Numeric(15, 3), nullable=False), sa.Column("pesaje_public_id", sa.Uuid(), nullable=False),
        sa.Column("correccion_aplicada_public_id", sa.Uuid()), sa.Column("projection_sha256", sa.String(64), nullable=False),
        sa.Column("pesada_at_snapshot", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recibida_por_id", sa.Integer(), nullable=False), sa.Column("recibida_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("estado_logistico IN ('RECIBIDA_ALMACEN', 'REVERSADA')", name="ck_scm_existencia_manga_kg_logistica"),
        sa.CheckConstraint("estado_calidad IN ('PENDIENTE', 'LIBERADA', 'BLOQUEADA', 'RECHAZADA')", name="ck_scm_existencia_manga_kg_calidad"),
        sa.CheckConstraint("cantidad_fisica_kg > 0 AND cantidad_reservada_kg >= 0 AND cantidad_reservada_kg <= cantidad_fisica_kg", name="ck_scm_existencia_manga_kg_cantidad"),
        sa.ForeignKeyConstraint(["manga_id"], ["scm_manga.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["sesion_id"], ["scm_sesion_recepcion_manga.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["etiqueta_resuelta_id"], ["scm_etiqueta_manga.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["articulo_scm_id"], ["scm_articulo.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["saldo_id"], ["scm_saldo_inventario_kg.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["ubicacion_id"], ["scm_ubicacion_inventario.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["movimiento_ingreso_id"], ["scm_movimiento_inventario_kg.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["operation_id"], ["scm_operacion.operation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recibida_por_id"], ["trabajador.id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manga_id", name="uq_scm_existencia_manga_kg_manga"), sa.UniqueConstraint("movimiento_ingreso_id", name="uq_scm_existencia_manga_kg_movimiento"), sa.UniqueConstraint("operation_id", name="uq_scm_existencia_manga_kg_operation"),
    )

    # The database is the last authority boundary. Every direct SQL/ORM path
    # must agree with the article marker before it can create a ledger row.
    bind = op.get_bind()
    schema = bind.execute(sa.text("SELECT current_schema()" )).scalar_one()
    # current_schema() is supplied by the isolated migration connection, so
    # quote it before embedding it in the trigger function search path.
    quoted_schema = bind.dialect.identifier_preparer.quote(schema)
    op.execute(sa.text(f"""
    CREATE OR REPLACE FUNCTION scm_kg_article_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, {quoted_schema} AS $$
    DECLARE article_unit text; article_class text;
    BEGIN
      SELECT unidad_inventario, clase INTO article_unit, article_class
        FROM scm_articulo WHERE id = NEW.articulo_scm_id FOR UPDATE;
      IF article_unit IS NULL THEN RAISE EXCEPTION 'ARTICLE_NOT_FOUND'; END IF;
      IF TG_TABLE_NAME IN ('scm_saldo_inventario_kg','scm_existencia_manga_kg') AND article_unit <> 'KG' THEN RAISE EXCEPTION 'KG_ARTICLE_REQUIRED'; END IF;
      IF TG_TABLE_NAME IN ('scm_saldo_inventario','scm_existencia_manga') AND article_unit = 'KG' THEN RAISE EXCEPTION 'KG_OPERATION_NOT_ENABLED'; END IF;
      IF TG_TABLE_NAME = 'scm_existencia_manga_kg' AND article_class NOT IN ('PIEZA_COLOR','SUBENSAMBLE_WIP') THEN RAISE EXCEPTION 'KG_CLASS_NOT_ALLOWED'; END IF;
      RETURN NEW;
    END $$;
    CREATE OR REPLACE FUNCTION scm_kg_movement_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, {quoted_schema} AS $$
    DECLARE article_unit text;
    BEGIN
      SELECT a.unidad_inventario INTO article_unit FROM scm_saldo_inventario_kg s JOIN scm_articulo a ON a.id=s.articulo_scm_id WHERE s.id=NEW.saldo_id FOR UPDATE;
      IF article_unit <> 'KG' THEN RAISE EXCEPTION 'KG_ARTICLE_REQUIRED'; END IF;
      RETURN NEW;
    END $$;
    CREATE OR REPLACE FUNCTION scm_kg_logistic_unit_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, {quoted_schema} AS $$
    DECLARE old_unit text; new_unit text;
    BEGIN
      IF TG_OP = 'UPDATE' AND OLD.articulo_scm_id IS DISTINCT FROM NEW.articulo_scm_id THEN
        PERFORM id FROM scm_articulo
          WHERE id IN (OLD.articulo_scm_id, NEW.articulo_scm_id)
          ORDER BY id FOR UPDATE;
      ELSIF NEW.articulo_scm_id IS NOT NULL THEN
        PERFORM id FROM scm_articulo WHERE id = NEW.articulo_scm_id FOR UPDATE;
      END IF;
      IF TG_OP = 'UPDATE' AND OLD.articulo_scm_id IS NOT NULL THEN
        SELECT unidad_inventario INTO old_unit FROM scm_articulo WHERE id=OLD.articulo_scm_id;
      END IF;
      IF NEW.articulo_scm_id IS NOT NULL THEN
        SELECT unidad_inventario INTO new_unit FROM scm_articulo WHERE id=NEW.articulo_scm_id;
      END IF;
      IF old_unit = 'KG' OR new_unit = 'KG' THEN RAISE EXCEPTION 'KG_OPERATION_NOT_ENABLED'; END IF;
      RETURN NEW;
    END $$;
    CREATE OR REPLACE FUNCTION scm_kg_article_marker_guard() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, {quoted_schema} AS $$
    BEGIN
      IF NEW.unidad_inventario = OLD.unidad_inventario THEN RETURN NEW; END IF;
      IF NEW.unidad_inventario = 'KG' THEN
        IF NEW.clase NOT IN ('PIEZA_COLOR','SUBENSAMBLE_WIP') THEN RAISE EXCEPTION 'KG_CLASS_NOT_ALLOWED'; END IF;
        IF EXISTS (SELECT 1 FROM scm_saldo_inventario WHERE articulo_scm_id=NEW.id AND (cantidad_fisica <> 0 OR cantidad_reservada <> 0 OR cantidad_no_disponible <> 0)) THEN RAISE EXCEPTION 'KG_DOWNGRADE_LEGACY_BALANCE'; END IF;
        IF EXISTS (SELECT 1 FROM scm_existencia_manga e WHERE e.articulo_scm_id=NEW.id AND e.estado_logistico <> 'REVERSADA') THEN RAISE EXCEPTION 'KG_DOWNGRADE_LEGACY_EXISTENCE'; END IF;
        IF EXISTS (SELECT 1 FROM scm_unidad_logistica_inventario WHERE articulo_scm_id=NEW.id) THEN RAISE EXCEPTION 'KG_DOWNGRADE_LOGISTIC_UNIT'; END IF;
      ELSIF NEW.unidad_inventario = 'UN' THEN
        IF EXISTS (SELECT 1 FROM scm_saldo_inventario_kg WHERE articulo_scm_id=NEW.id AND (cantidad_fisica_kg <> 0 OR cantidad_reservada_kg <> 0 OR cantidad_no_disponible_kg <> 0)) THEN RAISE EXCEPTION 'KG_DOWNGRADE_NONZERO'; END IF;
        IF EXISTS (SELECT 1 FROM scm_existencia_manga_kg e WHERE e.articulo_scm_id=NEW.id AND e.estado_logistico <> 'REVERSADA') THEN RAISE EXCEPTION 'KG_DOWNGRADE_ACTIVE_EXISTENCE'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    """))
    for table in ("scm_saldo_inventario_kg", "scm_existencia_manga_kg", "scm_saldo_inventario", "scm_existencia_manga"):
        op.execute(sa.text(f"CREATE TRIGGER {table}_unit_guard BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION scm_kg_article_guard()"))
    op.execute(sa.text("CREATE TRIGGER scm_movimiento_inventario_kg_unit_guard BEFORE INSERT OR UPDATE ON scm_movimiento_inventario_kg FOR EACH ROW EXECUTE FUNCTION scm_kg_movement_guard()"))
    op.execute(sa.text("CREATE TRIGGER scm_unidad_logistica_kg_guard BEFORE INSERT OR UPDATE ON scm_unidad_logistica_inventario FOR EACH ROW EXECUTE FUNCTION scm_kg_logistic_unit_guard()"))
    op.execute(sa.text("CREATE TRIGGER scm_articulo_kg_marker_guard BEFORE UPDATE OF unidad_inventario ON scm_articulo FOR EACH ROW EXECUTE FUNCTION scm_kg_article_marker_guard()"))


def downgrade():
    bind = op.get_bind()
    blockers = bind.execute(sa.text("""
        SELECT
          (SELECT count(*) FROM scm_articulo WHERE unidad_inventario = 'KG'),
          (SELECT count(*) FROM scm_saldo_inventario_kg),
          (SELECT count(*) FROM scm_movimiento_inventario_kg),
          (SELECT count(*) FROM scm_existencia_manga_kg)
    """)).one()
    if any(blockers):
        raise RuntimeError(
            "downgrade KG bloqueado: existen articulos KG o filas del sublibro"
        )
    op.execute(sa.text("DROP TRIGGER IF EXISTS scm_articulo_kg_marker_guard ON scm_articulo"))
    for table in ("scm_unidad_logistica_inventario", "scm_movimiento_inventario_kg", "scm_saldo_inventario_kg", "scm_existencia_manga_kg", "scm_saldo_inventario", "scm_existencia_manga"):
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {table}_unit_guard ON {table}"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS scm_movimiento_inventario_kg_unit_guard ON scm_movimiento_inventario_kg"))
    op.execute(sa.text("DROP TRIGGER IF EXISTS scm_unidad_logistica_kg_guard ON scm_unidad_logistica_inventario"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS scm_kg_article_guard()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS scm_kg_movement_guard()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS scm_kg_logistic_unit_guard()"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS scm_kg_article_marker_guard()"))
    op.drop_table("scm_existencia_manga_kg")
    op.drop_table("scm_movimiento_inventario_kg")
    op.drop_table("scm_saldo_inventario_kg")
    op.drop_constraint("ck_scm_articulo_unidad_inventario", "scm_articulo", type_="check")
    op.drop_constraint("ck_scm_articulo_kg_class", "scm_articulo", type_="check")
    op.drop_column("scm_articulo", "unidad_inventario")
