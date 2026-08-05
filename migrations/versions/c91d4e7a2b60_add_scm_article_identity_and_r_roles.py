"""add SCM article identity, subtypes and production authorization seeds

Revision ID: c91d4e7a2b60
Revises: b7e9f1a4d510
Create Date: 2026-07-24 17:10:00.000000

"""
import hashlib
import uuid

from alembic import op
import sqlalchemy as sa


revision = "c91d4e7a2b60"
down_revision = "b7e9f1a4d510"
branch_labels = None
depends_on = None


CAPABILITIES = (
    ("AUTORIZACION_SCM_ADMINISTRAR", "Administrar autorizaciones SCM"),
    ("ARTICULO_VER", "Consultar articulos SCM"),
    ("ARTICULO_ADMINISTRAR", "Administrar articulos SCM"),
    ("ESTRUCTURA_VER", "Consultar estructuras de producto"),
    ("ESTRUCTURA_ADMINISTRAR", "Administrar borradores de estructura"),
    ("ESTRUCTURA_APROBAR", "Aprobar estructuras"),
    ("RUTA_VER", "Consultar rutas de produccion"),
    ("RUTA_ADMINISTRAR", "Administrar borradores de ruta"),
    ("RUTA_APROBAR", "Aprobar rutas de produccion"),
    ("EMPAQUE_VER", "Consultar perfiles y reglas de empaque"),
    ("EMPAQUE_ADMINISTRAR", "Administrar empaque"),
    ("EMPAQUE_APROBAR", "Aprobar reglas de empaque"),
    ("OPERACION_PLANIFICAR", "Planificar operaciones"),
    ("OPERACION_EJECUTAR", "Ejecutar operaciones"),
    ("OPERACION_CORREGIR", "Corregir operaciones"),
    ("WIP_VER", "Consultar WIP y genealogia"),
    ("WIP_LIBERAR", "Liberar WIP por Calidad"),
    ("TIPO_MANGA_ADMINISTRAR", "Administrar tipos de manga"),
    ("OT_VER", "Consultar ordenes de trabajo"),
    ("OT_CREAR", "Crear ordenes de trabajo"),
    ("OT_INICIAR", "Iniciar ordenes de trabajo"),
    ("OT_CERRAR", "Cerrar ordenes de trabajo"),
    ("PLAN_MANGA_VER", "Consultar planes de manga"),
    ("PLAN_MANGA_ADMINISTRAR", "Administrar planes de manga"),
    ("MANGA_PLANIFICAR", "Planificar mangas"),
    ("MANGA_ANULAR", "Anular mangas"),
    ("MANGA_EXTRA_SOLICITAR", "Solicitar mangas extra"),
    ("MANGA_EXTRA_APROBAR", "Aprobar mangas extra"),
    ("MANGA_ETIQUETA_PRE_GENERAR", "Generar etiqueta prepesaje"),
    (
        "MANGA_ETIQUETA_REEMPLAZAR_SOLICITAR",
        "Solicitar reemplazo de etiqueta",
    ),
    (
        "MANGA_ETIQUETA_REEMPLAZAR_APROBAR",
        "Aprobar reemplazo de etiqueta",
    ),
    ("MANGA_PESAR", "Confirmar pesaje de manga"),
    ("MANGA_PESAJE_VER", "Consultar pesajes de manga"),
    ("MANGA_ETIQUETA_POST_IMPRIMIR", "Imprimir etiqueta final"),
    ("PESAJE_CORRECCION_SOLICITAR", "Solicitar correccion de pesaje"),
    ("PESAJE_CORRECCION_APROBAR", "Aprobar correccion de pesaje"),
    ("PESAJE_TARA_OVERRIDE", "Autorizar tara distinta del snapshot"),
)


VIEWS = (
    "ARTICULO_VER",
    "ESTRUCTURA_VER",
    "RUTA_VER",
    "EMPAQUE_VER",
    "OT_VER",
    "PLAN_MANGA_VER",
    "MANGA_PESAJE_VER",
    "WIP_VER",
)


ROLE_CAPABILITIES = (
    (
        "PLANIFICACION",
        "Planificacion",
        (
            "ARTICULO_VER",
            "ESTRUCTURA_VER",
            "RUTA_VER",
            "EMPAQUE_VER",
            "OPERACION_PLANIFICAR",
            "OT_VER",
            "OT_CREAR",
            "PLAN_MANGA_VER",
            "PLAN_MANGA_ADMINISTRAR",
            "MANGA_PLANIFICAR",
            "WIP_VER",
        ),
    ),
    (
        "INGENIERIA_SCM",
        "Ingenieria SCM",
        (
            "ARTICULO_VER",
            "ARTICULO_ADMINISTRAR",
            "ESTRUCTURA_VER",
            "ESTRUCTURA_ADMINISTRAR",
            "RUTA_VER",
            "RUTA_ADMINISTRAR",
            "EMPAQUE_VER",
            "EMPAQUE_ADMINISTRAR",
            "TIPO_MANGA_ADMINISTRAR",
        ),
    ),
    (
        "JEFE_PRODUCCION",
        "Jefe de Produccion",
        (
            *VIEWS,
            "ESTRUCTURA_APROBAR",
            "RUTA_APROBAR",
            "EMPAQUE_APROBAR",
            "OT_CREAR",
            "OT_INICIAR",
            "OT_CERRAR",
            "PLAN_MANGA_ADMINISTRAR",
            "MANGA_PLANIFICAR",
            "MANGA_ANULAR",
            "MANGA_EXTRA_APROBAR",
            "MANGA_ETIQUETA_REEMPLAZAR_APROBAR",
            "OPERACION_PLANIFICAR",
            "OPERACION_EJECUTAR",
            "OPERACION_CORREGIR",
            "PESAJE_CORRECCION_APROBAR",
            "PESAJE_TARA_OVERRIDE",
        ),
    ),
    (
        "SUPERVISOR",
        "Supervisor",
        (
            *VIEWS,
            "OT_CREAR",
            "OT_INICIAR",
            "OT_CERRAR",
            "PLAN_MANGA_ADMINISTRAR",
            "MANGA_PLANIFICAR",
            "MANGA_EXTRA_SOLICITAR",
            "MANGA_ETIQUETA_PRE_GENERAR",
            "MANGA_ETIQUETA_REEMPLAZAR_SOLICITAR",
            "OPERACION_EJECUTAR",
            "PESAJE_CORRECCION_SOLICITAR",
        ),
    ),
    (
        "MAQUINISTA",
        "Maquinista",
        ("OT_VER", "MANGA_PESAR", "MANGA_PESAJE_VER", "WIP_VER"),
    ),
    (
        "OPERADOR_PESAJE",
        "Operador de Pesaje",
        (
            "OT_VER",
            "MANGA_PESAR",
            "MANGA_PESAJE_VER",
            "MANGA_ETIQUETA_POST_IMPRIMIR",
            "MANGA_ETIQUETA_REEMPLAZAR_SOLICITAR",
            "PESAJE_CORRECCION_SOLICITAR",
            "WIP_VER",
        ),
    ),
    (
        "CALIDAD",
        "Calidad",
        (
            "ARTICULO_VER",
            "ESTRUCTURA_VER",
            "RUTA_VER",
            "WIP_VER",
            "WIP_LIBERAR",
        ),
    ),
    (
        "CONFIGURACION_SCM",
        "Configuracion SCM",
        (
            "ARTICULO_VER",
            "ARTICULO_ADMINISTRAR",
            "EMPAQUE_VER",
            "EMPAQUE_ADMINISTRAR",
            "TIPO_MANGA_ADMINISTRAR",
        ),
    ),
    (
        "GERENCIA",
        "Gerencia",
        ("AUTORIZACION_SCM_ADMINISTRAR", *VIEWS),
    ),
    ("AUDITORIA_CONSULTA", "Auditoria / Consulta", VIEWS),
)


def _seed_authorization(connection):
    for code, name in CAPABILITIES:
        connection.execute(sa.text("""
            INSERT INTO scm_capacidad (codigo, nombre, activo)
            SELECT :code, :name, true
            WHERE NOT EXISTS (
                SELECT 1 FROM scm_capacidad WHERE codigo = :code
            )
        """), {"code": code, "name": name})

    for role_code, role_name, capabilities in ROLE_CAPABILITIES:
        connection.execute(sa.text("""
            INSERT INTO rol_operativo (codigo, nombre, activo)
            SELECT :code, :name, true
            WHERE NOT EXISTS (
                SELECT 1 FROM rol_operativo WHERE codigo = :code
            )
        """), {"code": role_code, "name": role_name})
        for capability_code in capabilities:
            connection.execute(sa.text("""
                INSERT INTO scm_rol_capacidad (
                    rol_operativo_id,
                    capacidad_id
                )
                SELECT rol.id, capacidad.id
                FROM rol_operativo AS rol
                JOIN scm_capacidad AS capacidad
                  ON capacidad.codigo = :capability_code
                WHERE rol.codigo = :role_code
                  AND NOT EXISTS (
                      SELECT 1
                      FROM scm_rol_capacidad AS existing
                      WHERE existing.rol_operativo_id = rol.id
                        AND existing.capacidad_id = capacidad.id
                  )
            """), {
                "role_code": role_code,
                "capability_code": capability_code,
            })


def _backfill_articles(connection):
    article = sa.table(
        "scm_articulo",
        sa.column("id", sa.Integer()),
        sa.column("public_id", sa.Uuid()),
        sa.column("codigo", sa.String()),
        sa.column("nombre", sa.String()),
        sa.column("clase", sa.String()),
        sa.column("unidad_base", sa.String()),
        sa.column("activo", sa.Boolean()),
        sa.column("version", sa.Integer()),
    )
    piece_link = sa.table(
        "scm_articulo_pieza_color",
        sa.column("articulo_id", sa.Integer()),
        sa.column("pieza_color_sku", sa.String()),
    )
    product_link = sa.table(
        "scm_articulo_producto",
        sa.column("articulo_id", sa.Integer()),
        sa.column("producto_terminado_id", sa.String()),
    )

    def create_article(code, name, article_class):
        raw_code = str(code or "")
        normalized_code = raw_code.strip().upper()
        class_prefix = {
            "PIEZA_COLOR": "PC",
            "PRODUCTO_TERMINADO": "PT",
        }[article_class]
        if not normalized_code:
            normalized_code = f"{class_prefix}-LEGACY-SIN-CODIGO"

        conflict = connection.execute(
            sa.select(article.c.id).where(
                article.c.codigo == normalized_code
            )
        ).scalar_one_or_none()
        if conflict is not None:
            digest = hashlib.sha256(
                (
                    f"{article_class}\0{raw_code}\0"
                    f"{str(name or '').strip()}"
                ).encode("utf-8")
            ).hexdigest()[:12].upper()
            normalized_code = f"{class_prefix}-LEGACY-{digest}"
            suffix = 2
            while connection.execute(
                sa.select(article.c.id).where(
                    article.c.codigo == normalized_code
                )
            ).scalar_one_or_none() is not None:
                normalized_code = (
                    f"{class_prefix}-LEGACY-{digest}-{suffix}"
                )
                suffix += 1

        article_id = connection.execute(
            article.insert()
            .values(
                public_id=uuid.uuid4(),
                codigo=normalized_code,
                nombre=(
                    str(name or "").strip() or normalized_code
                ),
                clase=article_class,
                unidad_base="UN",
                activo=True,
                version=1,
            )
            .returning(article.c.id)
        ).scalar_one()
        return article_id

    pieces = connection.execute(sa.text("""
        SELECT sku, piezas
        FROM pieza_color
        ORDER BY sku
    """)).all()
    for code, name in pieces:
        article_id = create_article(code, name, "PIEZA_COLOR")
        connection.execute(piece_link.insert().values(
            articulo_id=article_id,
            pieza_color_sku=code,
        ))

    products = connection.execute(sa.text("""
        SELECT cod_sku_pt, producto
        FROM producto_terminado
        ORDER BY cod_sku_pt
    """)).all()
    for code, name in products:
        article_id = create_article(code, name, "PRODUCTO_TERMINADO")
        connection.execute(product_link.insert().values(
            articulo_id=article_id,
            producto_terminado_id=code,
        ))


def _create_postgres_guards(connection):
    if connection.dialect.name != "postgresql":
        return
    op.execute("""
        CREATE FUNCTION scm_assert_article_subtype(target_id integer)
        RETURNS void AS $$
        DECLARE
            article_class varchar(32);
            piece_count integer;
            wip_count integer;
            product_count integer;
        BEGIN
            SELECT clase INTO article_class
            FROM scm_articulo
            WHERE id = target_id;
            IF article_class IS NULL THEN
                RETURN;
            END IF;

            SELECT count(*) INTO piece_count
            FROM scm_articulo_pieza_color WHERE articulo_id = target_id;
            SELECT count(*) INTO wip_count
            FROM scm_definicion_wip WHERE articulo_id = target_id;
            SELECT count(*) INTO product_count
            FROM scm_articulo_producto WHERE articulo_id = target_id;

            IF piece_count + wip_count + product_count <> 1 THEN
                RAISE EXCEPTION
                    'ARTICLE_SUBTYPE_MISMATCH: article % requires one subtype',
                    target_id;
            END IF;
            IF (article_class = 'PIEZA_COLOR' AND piece_count <> 1)
               OR (article_class = 'SUBENSAMBLE_WIP' AND wip_count <> 1)
               OR (
                   article_class = 'PRODUCTO_TERMINADO'
                   AND product_count <> 1
               ) THEN
                RAISE EXCEPTION
                    'ARTICLE_SUBTYPE_MISMATCH: article % class %',
                    target_id, article_class;
            END IF;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION scm_article_parent_guard()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                NEW.codigo <> OLD.codigo
                OR NEW.clase <> OLD.clase
                OR NEW.unidad_base <> OLD.unidad_base
            ) THEN
                RAISE EXCEPTION
                    'IMMUTABLE_ARTICLE_IDENTITY: code, class and unit';
            END IF;
            PERFORM scm_assert_article_subtype(NEW.id);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION scm_article_child_guard()
        RETURNS trigger AS $$
        BEGIN
            PERFORM scm_assert_article_subtype(
                CASE WHEN TG_OP = 'DELETE'
                     THEN OLD.articulo_id
                     ELSE NEW.articulo_id
                END
            );
            RETURN COALESCE(NEW, OLD);
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_scm_article_identity_immutable
        BEFORE UPDATE ON scm_articulo
        FOR EACH ROW EXECUTE FUNCTION scm_article_parent_guard()
    """)
    op.execute("""
        CREATE CONSTRAINT TRIGGER ctr_scm_article_parent_subtype
        AFTER INSERT OR UPDATE ON scm_articulo
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION scm_article_parent_guard()
    """)
    for table_name in (
        "scm_articulo_pieza_color",
        "scm_definicion_wip",
        "scm_articulo_producto",
    ):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER ctr_{table_name}_subtype
            AFTER INSERT OR UPDATE OR DELETE ON {table_name}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION scm_article_child_guard()
        """)


def upgrade():
    connection = op.get_bind()
    op.create_table(
        "scm_articulo",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("nombre", sa.String(length=200), nullable=False),
        sa.Column("clase", sa.String(length=32), nullable=False),
        sa.Column(
            "unidad_base",
            sa.String(length=10),
            server_default="UN",
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
            "clase IN "
            "('PIEZA_COLOR', 'SUBENSAMBLE_WIP', 'PRODUCTO_TERMINADO')",
            name="ck_scm_articulo_clase",
        ),
        sa.CheckConstraint(
            "unidad_base = 'UN'",
            name="ck_scm_articulo_unidad_base",
        ),
        sa.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_scm_articulo_codigo_normalizado",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_scm_articulo_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("public_id", name="uq_scm_articulo_public_id"),
        sa.UniqueConstraint("codigo", name="uq_scm_articulo_codigo"),
    )
    op.create_table(
        "scm_articulo_pieza_color",
        sa.Column("articulo_id", sa.Integer(), nullable=False),
        sa.Column("pieza_color_sku", sa.String(length=50), nullable=False),
        sa.ForeignKeyConstraint(
            ["articulo_id"],
            ["scm_articulo.id"],
            name="fk_scm_articulo_pieza_articulo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pieza_color_sku"],
            ["pieza_color.sku"],
            name="fk_scm_articulo_pieza_pieza_color",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("articulo_id"),
        sa.UniqueConstraint(
            "pieza_color_sku",
            name="uq_scm_articulo_pieza_color_sku",
        ),
    )
    op.create_table(
        "scm_definicion_wip",
        sa.Column("articulo_id", sa.Integer(), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column(
            "requiere_calidad",
            sa.Boolean(),
            server_default=sa.false(),
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
        sa.ForeignKeyConstraint(
            ["articulo_id"],
            ["scm_articulo.id"],
            name="fk_scm_definicion_wip_articulo",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("articulo_id"),
    )
    op.create_table(
        "scm_articulo_producto",
        sa.Column("articulo_id", sa.Integer(), nullable=False),
        sa.Column(
            "producto_terminado_id",
            sa.String(length=50),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["articulo_id"],
            ["scm_articulo.id"],
            name="fk_scm_articulo_producto_articulo",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["producto_terminado_id"],
            ["producto_terminado.cod_sku_pt"],
            name="fk_scm_articulo_producto_producto",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("articulo_id"),
        sa.UniqueConstraint(
            "producto_terminado_id",
            name="uq_scm_articulo_producto_producto",
        ),
    )

    connection.execute(sa.text("""
        INSERT INTO correlativo_catalogo (
            clave, prefijo, siguiente_valor, ancho
        )
        SELECT 'SUBENSAMBLE_WIP', 'WIP', 1, 6
        WHERE NOT EXISTS (
            SELECT 1
            FROM correlativo_catalogo
            WHERE clave = 'SUBENSAMBLE_WIP'
        )
    """))
    _seed_authorization(connection)
    _backfill_articles(connection)
    _create_postgres_guards(connection)


def downgrade():
    connection = op.get_bind()
    wip_count = connection.execute(
        sa.text("SELECT count(*) FROM scm_definicion_wip")
    ).scalar_one()
    if wip_count:
        raise RuntimeError(
            "Downgrade bloqueado: existen subensambles WIP en scm_articulo"
        )

    if connection.dialect.name == "postgresql":
        for table_name in (
            "scm_articulo_pieza_color",
            "scm_definicion_wip",
            "scm_articulo_producto",
        ):
            op.execute(
                f"DROP TRIGGER IF EXISTS ctr_{table_name}_subtype "
                f"ON {table_name}"
            )
        op.execute(
            "DROP TRIGGER IF EXISTS ctr_scm_article_parent_subtype "
            "ON scm_articulo"
        )
        op.execute(
            "DROP TRIGGER IF EXISTS trg_scm_article_identity_immutable "
            "ON scm_articulo"
        )
        op.execute("DROP FUNCTION IF EXISTS scm_article_child_guard()")
        op.execute("DROP FUNCTION IF EXISTS scm_article_parent_guard()")
        op.execute(
            "DROP FUNCTION IF EXISTS scm_assert_article_subtype(integer)"
        )

    op.drop_table("scm_articulo_producto")
    op.drop_table("scm_definicion_wip")
    op.drop_table("scm_articulo_pieza_color")
    op.drop_table("scm_articulo")
    connection.execute(sa.text("""
        DELETE FROM correlativo_catalogo
        WHERE clave = 'SUBENSAMBLE_WIP'
    """))
