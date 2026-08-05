"""add SCM packaging containers, profiles and revisioned rules

Revision ID: f49b7e5a3d02
Revises: e38a6d4f2c91
Create Date: 2026-07-24 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "f49b7e5a3d02"
down_revision = "e38a6d4f2c91"
branch_labels = None
depends_on = None


def _create_postgres_guards(connection):
    if connection.dialect.name != "postgresql":
        return

    op.execute("""
        CREATE FUNCTION scm_packaging_rule_revision_guard()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.estado <> 'BORRADOR' THEN
                    RAISE EXCEPTION
                        'PACKAGING_RULE_IMMUTABLE: published revision';
                END IF;
                RETURN OLD;
            END IF;

            IF OLD.estado = 'APROBADA' THEN
                IF NEW.estado <> 'RETIRADA'
                   OR NEW.regla_id IS DISTINCT FROM OLD.regla_id
                   OR NEW.numero_revision
                        IS DISTINCT FROM OLD.numero_revision
                   OR NEW.medicion_fisica_probada
                        IS DISTINCT FROM OLD.medicion_fisica_probada
                   OR NEW.cantidad_objetivo_un
                        IS DISTINCT FROM OLD.cantidad_objetivo_un
                   OR NEW.cantidad_maxima_probada_un
                        IS DISTINCT FROM OLD.cantidad_maxima_probada_un
                   OR NEW.peso_neto_operativo_max_kg
                        IS DISTINCT FROM OLD.peso_neto_operativo_max_kg
                   OR NEW.margen_seguridad_kg
                        IS DISTINCT FROM OLD.margen_seguridad_kg
                   OR NEW.tolerancia_peso_abs_g
                        IS DISTINCT FROM OLD.tolerancia_peso_abs_g
                   OR NEW.tolerancia_peso_pct
                        IS DISTINCT FROM OLD.tolerancia_peso_pct
                   OR NEW.tara_nominal_g_snapshot
                        IS DISTINCT FROM OLD.tara_nominal_g_snapshot
                   OR NEW.tolerancia_tara_g_snapshot
                        IS DISTINCT FROM OLD.tolerancia_tara_g_snapshot
                   OR NEW.peso_bruto_max_kg_snapshot
                        IS DISTINCT FROM OLD.peso_bruto_max_kg_snapshot
                   OR NEW.notas IS DISTINCT FROM OLD.notas
                   OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
                   OR NEW.creada_por_id IS DISTINCT FROM OLD.creada_por_id
                   OR NEW.aprobada_por_id
                        IS DISTINCT FROM OLD.aprobada_por_id
                   OR NEW.aprobada_at IS DISTINCT FROM OLD.aprobada_at
                THEN
                    RAISE EXCEPTION
                        'PACKAGING_RULE_IMMUTABLE: approved content';
                END IF;
            ELSIF OLD.estado = 'RETIRADA' THEN
                RAISE EXCEPTION
                    'PACKAGING_RULE_IMMUTABLE: retired revision';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION scm_packaging_rule_identity_guard()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'PACKAGING_RULE_IMMUTABLE: rule identity';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_scm_packaging_rule_revision_guard
        BEFORE UPDATE OR DELETE ON scm_regla_empaque_revision
        FOR EACH ROW EXECUTE FUNCTION scm_packaging_rule_revision_guard()
    """)
    op.execute("""
        CREATE TRIGGER trg_scm_packaging_rule_identity_guard
        BEFORE UPDATE OR DELETE ON scm_regla_empaque
        FOR EACH ROW EXECUTE FUNCTION scm_packaging_rule_identity_guard()
    """)


def _seed_code_counters():
    for key, prefix in (
        ("TIPO_MANGA", "TMG"),
        ("TIPO_CONTENEDOR", "TCO"),
        ("PERFIL_EMPAQUE", "PEM"),
    ):
        op.execute(sa.text("""
            INSERT INTO correlativo_catalogo (
                clave, prefijo, siguiente_valor, ancho
            )
            SELECT :key, :prefix, 1, 6
            WHERE NOT EXISTS (
                SELECT 1
                FROM correlativo_catalogo
                WHERE clave = :key
            )
        """).bindparams(key=key, prefix=prefix))


def upgrade():
    connection = op.get_bind()
    op.create_table(
        "scm_tipo_contenedor",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("clase", sa.String(length=32), nullable=False),
        sa.Column("nombre", sa.String(length=160), nullable=False),
        sa.Column("material", sa.String(length=120), nullable=True),
        sa.Column("dimensiones_json", sa.JSON(), nullable=True),
        sa.Column(
            "tara_nominal_g",
            sa.Numeric(precision=12, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "tolerancia_tara_g",
            sa.Numeric(precision=12, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "peso_bruto_max_kg",
            sa.Numeric(precision=12, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
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
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_tipo_contenedor_codigo",
        ),
        sa.CheckConstraint(
            "clase IN ('MANGA', 'BOLSA', 'JABA', 'CAJA', 'OTRO')",
            name="ck_scm_tipo_contenedor_clase",
        ),
        sa.CheckConstraint(
            "tara_nominal_g >= 0",
            name="ck_scm_tipo_contenedor_tara",
        ),
        sa.CheckConstraint(
            "tolerancia_tara_g >= 0",
            name="ck_scm_tipo_contenedor_tolerancia",
        ),
        sa.CheckConstraint(
            "peso_bruto_max_kg >= 0",
            name="ck_scm_tipo_contenedor_bruto",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_tipo_contenedor_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "codigo",
            name="uq_scm_tipo_contenedor_codigo",
        ),
    )
    op.create_table(
        "scm_perfil_empacable",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("nombre", sa.String(length=160), nullable=False),
        sa.Column("descripcion_fisica", sa.Text(), nullable=True),
        sa.Column(
            "activo",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
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
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_perfil_empacable_codigo",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_perfil_empacable_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "codigo",
            name="uq_scm_perfil_empacable_codigo",
        ),
    )
    op.create_table(
        "scm_articulo_perfil",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("articulo_id", sa.Integer(), nullable=False),
        sa.Column("perfil_empacable_id", sa.Integer(), nullable=False),
        sa.Column(
            "es_predeterminado",
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
        sa.ForeignKeyConstraint(
            ["articulo_id"],
            ["scm_articulo.id"],
            name="fk_scm_articulo_perfil_articulo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["perfil_empacable_id"],
            ["scm_perfil_empacable.id"],
            name="fk_scm_articulo_perfil_perfil",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "articulo_id",
            "perfil_empacable_id",
            name="uq_scm_articulo_perfil",
        ),
    )
    op.create_index(
        "ux_scm_articulo_perfil_predeterminado",
        "scm_articulo_perfil",
        ["articulo_id"],
        unique=True,
        postgresql_where=sa.text("activo AND es_predeterminado"),
        sqlite_where=sa.text(
            "activo = 1 AND es_predeterminado = 1"
        ),
    )
    op.create_table(
        "scm_regla_empaque",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("perfil_empacable_id", sa.Integer(), nullable=False),
        sa.Column("tipo_contenedor_id", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["perfil_empacable_id"],
            ["scm_perfil_empacable.id"],
            name="fk_scm_regla_empaque_perfil",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tipo_contenedor_id"],
            ["scm_tipo_contenedor.id"],
            name="fk_scm_regla_empaque_contenedor",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "perfil_empacable_id",
            "tipo_contenedor_id",
            name="uq_scm_regla_empaque_combinacion",
        ),
    )
    op.create_table(
        "scm_regla_empaque_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("regla_id", sa.Integer(), nullable=False),
        sa.Column("numero_revision", sa.Integer(), nullable=False),
        sa.Column(
            "estado",
            sa.String(length=32),
            server_default="BORRADOR",
            nullable=False,
        ),
        sa.Column(
            "medicion_fisica_probada",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column("cantidad_objetivo_un", sa.Integer(), nullable=False),
        sa.Column(
            "cantidad_maxima_probada_un",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "peso_neto_operativo_max_kg",
            sa.Numeric(precision=12, scale=3),
            nullable=False,
        ),
        sa.Column(
            "margen_seguridad_kg",
            sa.Numeric(precision=12, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "tolerancia_peso_abs_g",
            sa.Numeric(precision=12, scale=3),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "tolerancia_peso_pct",
            sa.Numeric(precision=7, scale=4),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "tara_nominal_g_snapshot",
            sa.Numeric(precision=12, scale=3),
            nullable=True,
        ),
        sa.Column(
            "tolerancia_tara_g_snapshot",
            sa.Numeric(precision=12, scale=3),
            nullable=True,
        ),
        sa.Column(
            "peso_bruto_max_kg_snapshot",
            sa.Numeric(precision=12, scale=3),
            nullable=True,
        ),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("creada_por_id", sa.Integer(), nullable=False),
        sa.Column("aprobada_por_id", sa.Integer(), nullable=True),
        sa.Column("aprobada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retirada_por_id", sa.Integer(), nullable=True),
        sa.Column("retirada_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "version",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
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
            "estado IN ('BORRADOR', 'APROBADA', 'RETIRADA')",
            name="ck_scm_regla_empaque_revision_estado",
        ),
        sa.CheckConstraint(
            "numero_revision > 0",
            name="ck_scm_regla_empaque_revision_numero",
        ),
        sa.CheckConstraint(
            "cantidad_objetivo_un > 0",
            name="ck_scm_regla_empaque_objetivo",
        ),
        sa.CheckConstraint(
            "cantidad_maxima_probada_un > 0",
            name="ck_scm_regla_empaque_maxima",
        ),
        sa.CheckConstraint(
            "cantidad_objetivo_un <= cantidad_maxima_probada_un",
            name="ck_scm_regla_empaque_objetivo_maxima",
        ),
        sa.CheckConstraint(
            "peso_neto_operativo_max_kg > 0",
            name="ck_scm_regla_empaque_neto",
        ),
        sa.CheckConstraint(
            "margen_seguridad_kg >= 0",
            name="ck_scm_regla_empaque_margen",
        ),
        sa.CheckConstraint(
            "tolerancia_peso_abs_g >= 0",
            name="ck_scm_regla_empaque_tolerancia_abs",
        ),
        sa.CheckConstraint(
            "tolerancia_peso_pct >= 0 "
            "AND tolerancia_peso_pct < 100",
            name="ck_scm_regla_empaque_tolerancia_pct",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_regla_empaque_version",
        ),
        sa.CheckConstraint(
            "content_hash IS NULL OR length(content_hash) = 64",
            name="ck_scm_regla_empaque_hash",
        ),
        sa.ForeignKeyConstraint(
            ["regla_id"],
            ["scm_regla_empaque.id"],
            name="fk_scm_regla_empaque_revision_regla",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["creada_por_id"],
            ["trabajador.id"],
            name="fk_scm_regla_empaque_revision_creador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["aprobada_por_id"],
            ["trabajador.id"],
            name="fk_scm_regla_empaque_revision_aprobador",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["retirada_por_id"],
            ["trabajador.id"],
            name="fk_scm_regla_empaque_revision_retirador",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "regla_id",
            "numero_revision",
            name="uq_scm_regla_empaque_revision",
        ),
    )
    op.create_index(
        "ux_scm_regla_empaque_aprobada",
        "scm_regla_empaque_revision",
        ["regla_id"],
        unique=True,
        postgresql_where=sa.text("estado = 'APROBADA'"),
        sqlite_where=sa.text("estado = 'APROBADA'"),
    )
    _seed_code_counters()
    _create_postgres_guards(connection)


def downgrade():
    connection = op.get_bind()
    counts = [
        connection.execute(sa.text(
            f"SELECT count(*) FROM {table_name}"
        )).scalar_one()
        for table_name in (
            "scm_regla_empaque_revision",
            "scm_regla_empaque",
            "scm_articulo_perfil",
            "scm_perfil_empacable",
            "scm_tipo_contenedor",
        )
    ]
    if any(counts):
        raise RuntimeError(
            "Downgrade bloqueado: existen maestros o reglas de empaque"
        )

    if connection.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS "
            "trg_scm_packaging_rule_revision_guard "
            "ON scm_regla_empaque_revision"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS "
            "trg_scm_packaging_rule_identity_guard "
            "ON scm_regla_empaque"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "scm_packaging_rule_revision_guard()"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "scm_packaging_rule_identity_guard()"
        )

    op.drop_index(
        "ux_scm_regla_empaque_aprobada",
        table_name="scm_regla_empaque_revision",
    )
    op.drop_table("scm_regla_empaque_revision")
    op.drop_table("scm_regla_empaque")
    op.drop_index(
        "ux_scm_articulo_perfil_predeterminado",
        table_name="scm_articulo_perfil",
    )
    op.drop_table("scm_articulo_perfil")
    op.drop_table("scm_perfil_empacable")
    op.drop_table("scm_tipo_contenedor")
    op.execute("""
        DELETE FROM correlativo_catalogo
        WHERE clave IN (
            'TIPO_MANGA', 'TIPO_CONTENEDOR', 'PERFIL_EMPAQUE'
        )
          AND siguiente_valor = 1
    """)
