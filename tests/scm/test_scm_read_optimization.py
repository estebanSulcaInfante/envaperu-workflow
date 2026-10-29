from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.extensions import db
from app.models.producto import PiezaColor, ProductoTerminado
from app.models.scm_articulos import (
    CLASE_PIEZA_COLOR,
    CLASE_PRODUCTO_TERMINADO,
    CLASE_SUBENSAMBLE_WIP,
    ScmArticulo,
    ScmDefinicionWip,
)
from app.models.scm_empaque import (
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.trabajador import RolOperativo, Trabajador
from app.services.scm_article_service import list_articles
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_packaging_service import (
    _serialize_rule,
    list_packaging_rules,
)
from app.services.scm_service_support import ScmServiceError, load_actor


def _actor_with_capability(capability):
    ensure_initial_scm_configuration()
    actor = Trabajador.query.filter_by(codigo="TRB-01").one()
    role = RolOperativo.query.filter_by(codigo="INGENIERIA_SCM").one()
    if role not in actor.roles:
        actor.roles.append(role)
    db.session.commit()
    assert actor.tiene_capacidad(capability)
    return actor


def _count_selects(callback):
    count = 0

    def before(_conn, _cursor, statement, _params, _context, _many):
        nonlocal count
        if statement.lstrip().upper().startswith("SELECT"):
            count += 1

    event.listen(db.engine, "before_cursor_execute", before)
    try:
        result = callback()
    finally:
        event.remove(db.engine, "before_cursor_execute", before)
    return result, count


def _reference_articles(session, actor_id, active=None):
    load_actor(session, actor_id, capability="ARTICULO_VER")
    statement = select(ScmArticulo)
    if active is not None:
        statement = statement.where(ScmArticulo.activo.is_(active))
    articles = session.scalars(
        statement.order_by(ScmArticulo.codigo)
    ).all()
    return {"items": [article.to_dict() for article in articles]}


def _reference_rules(session, actor_id, profile_id=None, container_id=None):
    load_actor(session, actor_id, capability="EMPAQUE_VER")
    statement = select(ScmReglaEmpaqueRevision).join(ScmReglaEmpaque)
    if profile_id is not None:
        statement = statement.where(
            ScmReglaEmpaque.perfil_empacable_id == profile_id
        )
    if container_id is not None:
        statement = statement.where(
            ScmReglaEmpaque.tipo_contenedor_id == container_id
        )
    items = session.scalars(
        statement.order_by(
            ScmReglaEmpaqueRevision.regla_id,
            ScmReglaEmpaqueRevision.numero_revision.desc(),
        )
    ).all()
    return {"items": [_serialize_rule(item) for item in items]}


def test_list_articles_prefetches_all_subtypes_without_contract_drift(app):
    with app.app_context():
        actor = _actor_with_capability("ARTICULO_VER")
        actor_id = actor.id
        piece_rows = [
            PiezaColor(
                sku=f"PC-READ-{index:03d}",
                linea_id=1,
                familia_id=1,
                piezas=f"Pieza lectura {index}",
            )
            for index in range(25)
        ]
        product_rows = [
            ProductoTerminado(
                cod_sku_pt=f"PT-READ-{index:03d}",
                linea_id=1,
                familia_id=1,
                producto=f"PT lectura {index}",
                um="UN",
            )
            for index in range(25)
        ]
        wip_rows = [
            ScmArticulo(
                codigo=f"WIP-READ-{index:03d}",
                nombre=f"WIP lectura {index}",
                clase=CLASE_SUBENSAMBLE_WIP,
                definicion_wip=ScmDefinicionWip(
                    descripcion=f"Subtipo precargado {index}",
                    requiere_calidad=index % 2 == 0,
                ),
            )
            for index in range(25)
        ]
        db.session.add_all([
            *piece_rows,
            *product_rows,
            *wip_rows,
        ])
        db.session.flush()
        product = db.session.get(ScmArticulo, db.session.scalar(
            select(ScmArticulo.id).where(ScmArticulo.codigo == "PT-READ-000")
        ))
        product.activo = False
        db.session.commit()

        with Session(db.engine) as reference_session:
            expected, baseline_sql_count = _count_selects(
                lambda: _reference_articles(reference_session, actor_id)
            )
        with Session(db.engine) as optimized_session:
            actual, sql_count = _count_selects(
                lambda: list_articles(optimized_session, actor_id=actor_id)
            )

        assert actual == expected
        assert baseline_sql_count > sql_count
        assert sql_count <= 10
        with Session(db.engine) as reference_session:
            filtered_expected = _reference_articles(
                reference_session, actor_id, active=False
            )
        with Session(db.engine) as optimized_session:
            filtered_actual = list_articles(
                optimized_session, actor_id=actor_id, active=False
            )
        assert filtered_actual == filtered_expected


def test_list_packaging_rules_joins_rule_profile_and_container(app):
    with app.app_context():
        actor = _actor_with_capability("EMPAQUE_VER")
        actor_id = actor.id
        profiles = [
            ScmPerfilEmpacable(
                codigo=f"PERF-READ-{index:03d}",
                nombre=f"Perfil lectura {index}",
                descripcion_fisica="Fixture",
            )
            for index in range(25)
        ]
        containers = [
            ScmTipoContenedor(
                codigo=f"TMG-READ-{index:03d}",
                clase="MANGA",
                nombre=f"Contenedor lectura {index}",
                material="PE",
                dimensiones_json={"ancho_mm": "500"},
                tara_nominal_g=100,
                tolerancia_tara_g=10,
                peso_bruto_max_kg=10,
            )
            for index in range(25)
        ]
        db.session.add_all([*profiles, *containers])
        db.session.flush()
        profile_ids = [profile.id for profile in profiles]
        container_ids = [container.id for container in containers]
        rules = [
            ScmReglaEmpaque(
                perfil_empacable_id=profile_ids[index],
                tipo_contenedor_id=container_ids[index],
            )
            for index in range(25)
        ]
        db.session.add_all(rules)
        db.session.flush()
        db.session.add_all([
            ScmReglaEmpaqueRevision(
                regla_id=rule.id,
                numero_revision=1,
                medicion_fisica_probada=True,
                cantidad_objetivo_un=8,
                cantidad_maxima_probada_un=10,
                peso_neto_operativo_max_kg="9",
                margen_seguridad_kg="0.5",
                tolerancia_peso_abs_g="50",
                tolerancia_peso_pct="2.5",
                creada_por_id=actor_id,
            )
            for rule in rules
        ])
        db.session.commit()

        with Session(db.engine) as reference_session:
            expected, baseline_sql_count = _count_selects(
                lambda: _reference_rules(reference_session, actor_id)
            )
        with Session(db.engine) as optimized_session:
            actual, sql_count = _count_selects(
                lambda: list_packaging_rules(
                    optimized_session,
                    actor_id=actor_id,
                )
            )

        assert actual == expected
        assert baseline_sql_count > sql_count
        assert sql_count <= 8
        with Session(db.engine) as reference_session:
            filtered_expected = _reference_rules(
                reference_session,
                actor_id,
                profile_ids[0],
                container_ids[0],
            )
        with Session(db.engine) as optimized_session:
            filtered_actual = list_packaging_rules(
                optimized_session,
                actor_id=actor_id,
                profile_id=profile_ids[0],
                container_id=container_ids[0],
            )
        assert filtered_actual == filtered_expected


def test_optimized_reads_keep_negative_authorization(app):
    with app.app_context():
        ensure_initial_scm_configuration()
        actor_id = Trabajador.query.filter_by(codigo="TRB-01").one().id

        for callback in (
            lambda: list_articles(db.session, actor_id=actor_id),
            lambda: list_packaging_rules(db.session, actor_id=actor_id),
        ):
            try:
                callback()
            except ScmServiceError as error:
                assert error.code == "CAPABILITY_REQUIRED"
            else:
                raise AssertionError("una lectura sin permiso fue aceptada")
