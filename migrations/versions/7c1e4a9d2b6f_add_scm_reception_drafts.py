"""add scm supplier documents and reception drafts

Revision ID: 7c1e4a9d2b6f
Revises: 23a5f8a99a0b
Create Date: 2026-07-21 18:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "7c1e4a9d2b6f"
down_revision = "23a5f8a99a0b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "scm_documento_proveedor",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("proveedor_id", sa.Integer(), nullable=False),
        sa.Column("tipo", sa.String(length=24), nullable=False),
        sa.Column("serie_normalizada", sa.String(length=32), nullable=False),
        sa.Column("numero_normalizado", sa.String(length=64), nullable=False),
        sa.Column("fecha_emision", sa.Date(), nullable=False),
        sa.Column("cantidad_total_documental_kg", sa.Numeric(15, 3), nullable=True),
        sa.Column("referencia", sa.String(length=128), nullable=True),
        sa.Column("observacion", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("tipo IN ('GUIA_REMISION', 'FACTURA', 'OTRO')", name="ck_scm_documento_proveedor_tipo"),
        sa.CheckConstraint("serie_normalizada = upper(trim(serie_normalizada)) AND length(serie_normalizada) > 0", name="ck_scm_documento_proveedor_serie_normalizada"),
        sa.CheckConstraint("numero_normalizado = upper(trim(numero_normalizado)) AND length(numero_normalizado) > 0", name="ck_scm_documento_proveedor_numero_normalizado"),
        sa.CheckConstraint("cantidad_total_documental_kg IS NULL OR cantidad_total_documental_kg > 0", name="ck_scm_documento_proveedor_cantidad_positiva"),
        sa.CheckConstraint("version > 0", name="ck_scm_documento_proveedor_version"),
        sa.ForeignKeyConstraint(["proveedor_id"], ["scm_proveedor.id"], name="fk_scm_documento_proveedor_proveedor", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("proveedor_id", "tipo", "serie_normalizada", "numero_normalizado", name="uq_scm_documento_proveedor_identidad"),
    )
    op.create_table(
        "scm_recepcion",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("proveedor_id", sa.Integer(), nullable=False),
        sa.Column("estado", sa.String(length=30), server_default="BORRADOR", nullable=False),
        sa.Column("recibida_por_id", sa.Integer(), nullable=False),
        sa.Column("confirmada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observacion", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("codigo = upper(trim(codigo)) AND length(codigo) > 0", name="ck_scm_recepcion_codigo_normalizado"),
        sa.CheckConstraint("estado IN ('BORRADOR', 'CONFIRMADA', 'RECHAZADA_PRE_CUSTODIA')", name="ck_scm_recepcion_estado"),
        sa.CheckConstraint("estado <> 'CONFIRMADA' OR confirmada_at IS NOT NULL", name="ck_scm_recepcion_confirmacion_coherente"),
        sa.CheckConstraint("version > 0", name="ck_scm_recepcion_version"),
        sa.ForeignKeyConstraint(["proveedor_id"], ["scm_proveedor.id"], name="fk_scm_recepcion_proveedor", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recibida_por_id"], ["trabajador.id"], name="fk_scm_recepcion_recibida_por", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_scm_recepcion_codigo"),
    )
    op.create_table(
        "scm_recepcion_documento",
        sa.Column("recepcion_id", sa.Integer(), nullable=False),
        sa.Column("documento_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["documento_id"], ["scm_documento_proveedor.id"], name="fk_scm_recepcion_documento_documento", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recepcion_id"], ["scm_recepcion.id"], name="fk_scm_recepcion_documento_recepcion", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("recepcion_id", "documento_id"),
    )
    op.create_table(
        "scm_recepcion_linea",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recepcion_id", sa.Integer(), nullable=False),
        sa.Column("numero_linea", sa.Integer(), nullable=False),
        sa.Column("material_id", sa.Integer(), nullable=False),
        sa.Column("modalidad", sa.String(length=40), nullable=False),
        sa.Column("bultos_recibidos", sa.Integer(), nullable=False),
        sa.Column("cantidad_documental_kg", sa.Numeric(15, 3), nullable=True),
        sa.Column("cantidad_medida_kg", sa.Numeric(15, 3), nullable=True),
        sa.Column("observacion", sa.Text(), nullable=True),
        sa.CheckConstraint("numero_linea > 0", name="ck_scm_recepcion_linea_numero"),
        sa.CheckConstraint("modalidad IN ('VIRGEN_CONFIANZA_PROVEEDOR', 'SEGUNDA_PESAJE_BOLSA')", name="ck_scm_recepcion_linea_modalidad"),
        sa.CheckConstraint("bultos_recibidos > 0", name="ck_scm_recepcion_linea_bultos_positivos"),
        sa.CheckConstraint("cantidad_documental_kg IS NULL OR cantidad_documental_kg > 0", name="ck_scm_recepcion_linea_documental_positiva"),
        sa.CheckConstraint("cantidad_medida_kg IS NULL OR cantidad_medida_kg > 0", name="ck_scm_recepcion_linea_medida_positiva"),
        sa.ForeignKeyConstraint(["material_id"], ["scm_material.id"], name="fk_scm_recepcion_linea_material", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["recepcion_id"], ["scm_recepcion.id"], name="fk_scm_recepcion_linea_recepcion", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recepcion_id", "numero_linea", name="uq_scm_recepcion_linea_recepcion_numero"),
    )
    op.create_table(
        "scm_pesaje_bolsa",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("recepcion_linea_id", sa.Integer(), nullable=False),
        sa.Column("secuencia", sa.Integer(), nullable=False),
        sa.Column("peso_kg", sa.Numeric(15, 3), nullable=False),
        sa.Column("balanza_codigo_snapshot", sa.String(length=64), nullable=True),
        sa.Column("registrado_por_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint("secuencia > 0", name="ck_scm_pesaje_bolsa_secuencia"),
        sa.CheckConstraint("peso_kg > 0", name="ck_scm_pesaje_bolsa_peso_positivo"),
        sa.ForeignKeyConstraint(["recepcion_linea_id"], ["scm_recepcion_linea.id"], name="fk_scm_pesaje_bolsa_linea", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["registrado_por_id"], ["trabajador.id"], name="fk_scm_pesaje_bolsa_registrado_por", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("recepcion_linea_id", "secuencia", name="uq_scm_pesaje_bolsa_linea_secuencia"),
    )

    op.execute(sa.text("""
        INSERT INTO scm_capacidad (codigo, nombre, activo)
        SELECT 'DOCUMENTO_PROVEEDOR_REGISTRAR',
               'Registrar documentos externos de proveedor', true
        WHERE NOT EXISTS (
            SELECT 1 FROM scm_capacidad
            WHERE codigo = 'DOCUMENTO_PROVEEDOR_REGISTRAR'
        )
    """))
    op.execute(sa.text("""
        INSERT INTO scm_rol_capacidad (rol_operativo_id, capacidad_id)
        SELECT rol.id, capacidad.id
        FROM rol_operativo AS rol
        CROSS JOIN scm_capacidad AS capacidad
        WHERE rol.codigo IN ('COMPRAS', 'ALMACEN_RECEPCION')
          AND capacidad.codigo = 'DOCUMENTO_PROVEEDOR_REGISTRAR'
          AND NOT EXISTS (
              SELECT 1 FROM scm_rol_capacidad AS existente
              WHERE existente.rol_operativo_id = rol.id
                AND existente.capacidad_id = capacidad.id
          )
    """))

    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text("""
            CREATE FUNCTION scm_validar_mutacion_detalle_recepcion()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                id_recepcion integer;
                estado_recepcion varchar(30);
            BEGIN
                IF TG_TABLE_NAME = 'scm_recepcion_documento' THEN
                    id_recepcion := CASE WHEN TG_OP = 'DELETE' THEN OLD.recepcion_id ELSE NEW.recepcion_id END;
                ELSIF TG_TABLE_NAME = 'scm_recepcion_linea' THEN
                    id_recepcion := CASE WHEN TG_OP = 'DELETE' THEN OLD.recepcion_id ELSE NEW.recepcion_id END;
                ELSE
                    SELECT recepcion_id INTO id_recepcion
                    FROM scm_recepcion_linea
                    WHERE id = CASE WHEN TG_OP = 'DELETE' THEN OLD.recepcion_linea_id ELSE NEW.recepcion_linea_id END;
                END IF;

                SELECT estado INTO estado_recepcion
                FROM scm_recepcion
                WHERE id = id_recepcion;
                IF estado_recepcion <> 'BORRADOR' THEN
                    RAISE EXCEPTION 'el detalle de una recepcion no borrador es inmutable'
                        USING ERRCODE = '55000';
                END IF;
                RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
            END
            $$
        """))
        for table in (
            "scm_recepcion_documento",
            "scm_recepcion_linea",
            "scm_pesaje_bolsa",
        ):
            op.execute(sa.text(
                f"CREATE TRIGGER trg_{table}_borrador "
                f"BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION scm_validar_mutacion_detalle_recepcion()"
            ))


def downgrade():
    connection = op.get_bind()
    populated = any(
        connection.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")).scalar()
        for table in (
            "scm_documento_proveedor",
            "scm_recepcion",
            "scm_recepcion_documento",
            "scm_recepcion_linea",
            "scm_pesaje_bolsa",
        )
    )
    if populated:
        raise RuntimeError(
            "US010A recepcion contiene datos; downgrade destructivo bloqueado"
        )

    if connection.dialect.name == "postgresql":
        for table in (
            "scm_recepcion_documento",
            "scm_recepcion_linea",
            "scm_pesaje_bolsa",
        ):
            op.execute(sa.text(f"DROP TRIGGER trg_{table}_borrador ON {table}"))
        op.execute(sa.text("DROP FUNCTION scm_validar_mutacion_detalle_recepcion()"))

    op.drop_table("scm_pesaje_bolsa")
    op.drop_table("scm_recepcion_linea")
    op.drop_table("scm_recepcion_documento")
    op.drop_table("scm_recepcion")
    op.drop_table("scm_documento_proveedor")
    op.execute(sa.text("""
        DELETE FROM scm_rol_capacidad
        WHERE capacidad_id IN (
            SELECT id FROM scm_capacidad
            WHERE codigo = 'DOCUMENTO_PROVEEDOR_REGISTRAR'
        )
    """))
    op.execute(sa.text("""
        DELETE FROM scm_capacidad
        WHERE codigo = 'DOCUMENTO_PROVEEDOR_REGISTRAR'
    """))
