"""Baseline local y repetible para el recorrido de validacion SCM.

La semilla termina deliberadamente antes del primer documento operativo. La
persona que valida debe crear y aprobar el lote de apertura, la OP y sus
documentos desde la interfaz. De ese modo el recorrido ejercita el producto y
no un resultado precocinado.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import uuid

from sqlalchemy import select, update
from sqlalchemy.engine import make_url

from app.models.estacion_pesaje import EstacionPesaje
from app.models.maquina import Maquina, TipoMaquina
from app.models.materiales import MateriaPrima
from app.models.molde import Molde, MoldePieza, Pieza
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    Familia,
    FamiliaColor,
    Linea,
    LineaFamilia,
    PiezaColor,
    ProductoPieza,
    ProductoTerminado,
)
from app.models.receta_color import RecetaColorLinea, RecetaColorMaestra
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_articulos import ScmArticuloPiezaColor, ScmArticuloProducto
from app.models.scm_catalogos import ScmCategoriaRecepcion, ScmMaterial
from app.models.scm_commercial import ScmPresentacionComercial
from app.models.scm_empaque import (
    ScmArticuloPerfil,
    ScmPerfilEmpacable,
    ScmReglaEmpaque,
    ScmReglaEmpaqueRevision,
    ScmTipoContenedor,
)
from app.models.scm_estructuras import (
    ScmEstructuraComponente,
    ScmEstructuraRevision,
)
from app.models.scm_inventory import (
    ScmLoteAperturaInventario,
    ScmMovimientoInventario,
    ScmMovimientoMaterialInventario,
    ScmSaldoInventario,
    ScmSaldoMaterialInventario,
    ScmUbicacionInventario,
)
from app.models.scm_inventory_operations import (
    ScmAlmacen,
    ScmAlmacenTrabajador,
)
from app.models.scm_ot import (
    ScmEtiquetaManga,
    ScmManga,
    ScmPesajeManga,
    ScmTrabajoImpresionManga,
    ScmTrabajoOt,
)
from app.models.scm_production_orders import (
    ScmOrdenOperacion,
    ScmOrdenProduccion,
)
from app.models.scm_rutas import (
    ScmCentroTrabajo,
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.trabajador import RolOperativo, Trabajador, trabajador_rol
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_packaging_service import _rule_content_hash
from app.services.scm_route_service import _content_hash as route_content_hash
from app.services.scm_structure_service import _content_hash as structure_content_hash
from app.services.station_auth import hash_station_token


ALEMBIC_HEAD = "f84d3a7c9e21"
DATABASE_NAME = "enva_uat_recorrido"
WALKTHROUGH_MARKER = "LOCAL_UAT_RECORRIDO_JARRA_REAL_6L_V1"
STATION_TOKEN = "recorrido-local-balanza-planta-1"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class LocalWalkthroughSeedError(RuntimeError):
    """El baseline no puede escribirse de forma segura o coherente."""


def _stable_uuid(suffix: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"{WALKTHROUGH_MARKER}:{suffix}")


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def assert_local_walkthrough_database(
    database_url: str,
    *,
    connection_database: str,
    migration_revision: str,
) -> None:
    """Falla cerrado: solo PostgreSQL loopback, base exclusiva y head exacto."""

    try:
        parsed = make_url(database_url)
    except (TypeError, ValueError) as error:
        raise LocalWalkthroughSeedError("La URL de base no es valida.") from error
    if parsed.get_backend_name() != "postgresql":
        raise LocalWalkthroughSeedError("El recorrido exige PostgreSQL local.")
    if (parsed.host or "").lower() not in _LOOPBACK_HOSTS:
        raise LocalWalkthroughSeedError("La base debe residir en loopback.")
    configured = parsed.database or ""
    if configured != DATABASE_NAME:
        raise LocalWalkthroughSeedError(
            f"Base local no autorizada: {configured or '(vacia)'}"
        )
    if connection_database != configured:
        raise LocalWalkthroughSeedError(
            "La base conectada no coincide con la URL configurada."
        )
    if migration_revision != ALEMBIC_HEAD:
        raise LocalWalkthroughSeedError(
            "La base no tiene las migraciones requeridas en head."
        )


def _one(session, model, **filters):
    return session.scalar(select(model).filter_by(**filters))


def _ensure_classification(session):
    line = _one(session, Linea, nombre="HOGAR")
    if line is None:
        line = Linea(codigo=8101, nombre="HOGAR")
        session.add(line)
    family = _one(session, Familia, nombre="JARRAS")
    if family is None:
        family = Familia(codigo=8101, nombre="JARRAS")
        session.add(family)
    session.flush()
    relation = _one(
        session,
        LineaFamilia,
        linea_id=line.id,
        familia_id=family.id,
    )
    if relation is None:
        session.add(LineaFamilia(linea_id=line.id, familia_id=family.id))
    elif not relation.activo:
        relation.activo = True
        relation.version += 1
    return line, family


def _ensure_actors(session):
    ensure_initial_scm_configuration()
    definitions = {
        "gerencia": ("TRB-001", "Carla", "Mendoza", "Gerente General", "GERENTE_GENERAL"),
        "planificacion": ("TRB-002", "Andrea", "Torres", "Planificadora", "PLANIFICACION"),
        "jefe_produccion": ("TRB-003", "Luis", "Rojas", "Jefe de Produccion", "JEFE_PRODUCCION"),
        "supervisor": ("TRB-004", "Mario", "Salazar", "Supervisor de Planta", "SUPERVISOR"),
        "maquinista": ("TRB-005", "Jose", "Quispe", "Maquinista", "MAQUINISTA"),
        "maquinista_relevo": ("TRB-006", "Pedro", "Huaman", "Maquinista de Relevo", "MAQUINISTA"),
        "operador_pesaje": ("TRB-007", "Rosa", "Flores", "Operadora de Pesaje", "OPERADOR_PESAJE"),
        "almacen": ("TRB-008", "Ana", "Vega", "Almacenera", "ALMACEN_RECEPCION"),
        "calidad": ("TRB-009", "Claudia", "Herrera", "Inspectora de Calidad", "CALIDAD"),
    }
    actors = {}
    for key, (code, names, surnames, short_name, role_code) in definitions.items():
        actor = _one(session, Trabajador, codigo=code)
        if actor is None:
            actor = Trabajador(
                codigo=code,
                nombres=names,
                apellidos=surnames,
                nombre_corto=short_name,
                activo=True,
                observaciones=WALKTHROUGH_MARKER,
            )
            session.add(actor)
        role = _one(session, RolOperativo, codigo=role_code)
        if role is None:
            raise LocalWalkthroughSeedError(f"Falta el rol {role_code}.")
        if role not in actor.roles:
            actor.roles.append(role)
        session.flush()
        session.execute(
            update(trabajador_rol)
            .where(trabajador_rol.c.trabajador_id == actor.id)
            .values(es_principal=False)
        )
        session.execute(
            update(trabajador_rol)
            .where(
                trabajador_rol.c.trabajador_id == actor.id,
                trabajador_rol.c.rol_operativo_id == role.id,
            )
            .values(es_principal=True)
        )
        session.expire(actor, ["rol_principal"])
        actors[key] = actor
    return actors


def _ensure_material_and_recipe(session, *, product, color):
    category = _one(session, ScmCategoriaRecepcion, codigo="RESINA_VIRGEN")
    if category is None:
        raise LocalWalkthroughSeedError("Falta la categoria RESINA_VIRGEN.")
    material = _one(session, ScmMaterial, codigo="MP-PP-CLARIFICADO")
    if material is None:
        material = ScmMaterial(
            codigo="MP-PP-CLARIFICADO",
            nombre="PP clarificado",
            clase="MATERIA_PRIMA",
            categoria_recepcion_id=category.id,
        )
        session.add(material)
        session.flush()
        session.add(MateriaPrima(
            nombre="PP clarificado",
            tipo="VIRGEN",
            scm_material_id=material.id,
        ))
    recipe = session.scalar(select(RecetaColorMaestra).where(
        RecetaColorMaestra.color_produccion_id == color.id,
        RecetaColorMaestra.producto_scope == product.cod_sku_pt,
        RecetaColorMaestra.nombre_variante == "Jarra Real 6 L Transparente",
        RecetaColorMaestra.revision == 1,
    ))
    if recipe is None:
        recipe = RecetaColorMaestra(
            color_produccion_id=color.id,
            producto_sku=product.cod_sku_pt,
            producto_scope=product.cod_sku_pt,
            nombre_variante="Jarra Real 6 L Transparente",
            revision=1,
            estado="APROBADA",
            es_default=True,
            base_virgen_kg=Decimal("25"),
            notas="Sin pigmento: solo PP clarificado virgen.",
            origen="RECORRIDO_LOCAL",
        )
        recipe.lineas.append(RecetaColorLinea(
            material_id=material.id,
            tipo_componente="MATERIA_PRIMA",
            cantidad=Decimal("1"),
            unidad="FRACCION",
            base_kg=None,
            orden=1,
        ))
        session.add(recipe)
    return material, recipe


def _ensure_product_engineering(session, *, actor, line, family):
    color_family = _one(session, FamiliaColor, nombre="TRANSPARENTE")
    if color_family is None:
        color_family = FamiliaColor(codigo=8101, nombre="TRANSPARENTE")
        session.add(color_family)
    color_base = _one(session, ColorBase, nombre="TRANSPARENTE")
    if color_base is None:
        color_base = ColorBase(nombre="TRANSPARENTE")
        session.add(color_base)
    session.flush()
    color = session.scalar(select(ColorProduccion).where(
        ColorProduccion.color_base_id == color_base.id,
        ColorProduccion.familia_color_id == color_family.id,
    ))
    if color is None:
        color = ColorProduccion(
            color_base_rel=color_base,
            familia_color_rel=color_family,
            # Referencia visual de resina transparente; no representa pigmento.
            hex_referencia="#EAF7F7",
        )
        session.add(color)

    piece = _one(session, Pieza, codigo="PZ-JARRA-REAL-6L")
    if piece is None:
        piece = Pieza(
            codigo="PZ-JARRA-REAL-6L",
            nombre="Cuerpo de Jarra Real 6 L",
            linea_id=line.id,
            familia_id=family.id,
            peso_nominal_gr=240,
            activo=True,
        )
        session.add(piece)
    mold = session.get(Molde, "ML-JARRA-REAL-6L")
    if mold is None:
        mold = Molde(
            codigo="ML-JARRA-REAL-6L",
            nombre="Molde Jarra Real 6 L",
            peso_tiro_gr=250,
            tiempo_ciclo_std=30,
            activo=True,
            notas="Peso neto 240 g; 10 g de colada por ciclo.",
        )
        session.add(mold)
    session.flush()
    composition = _one(
        session,
        MoldePieza,
        molde_id=mold.codigo,
        pieza_id=piece.id,
    )
    if composition is None:
        session.add(MoldePieza(
            molde_id=mold.codigo,
            pieza_id=piece.id,
            cavidades=1,
            peso_unitario_gr=240,
            activo=True,
        ))
    piece_color = session.get(PiezaColor, "PC-JARRA-REAL-6L-TRANSPARENTE")
    if piece_color is None:
        piece_color = PiezaColor(
            sku="PC-JARRA-REAL-6L-TRANSPARENTE",
            pieza_rel=piece,
            piezas="Cuerpo de Jarra Real 6 L Transparente",
            color_produccion_rel=color,
            linea_id=line.id,
            familia_id=family.id,
            peso=240,
            tipo_extruccion="INYECCION",
            estado_revision="VERIFICADO",
            notas_revision="Ingenieria validada para el recorrido local.",
        )
        session.add(piece_color)
    product = session.get(ProductoTerminado, "PT-JARRA-REAL-6L-TRANSPARENTE")
    if product is None:
        product = ProductoTerminado(
            cod_sku_pt="PT-JARRA-REAL-6L-TRANSPARENTE",
            producto="Jarra Real 6 L Transparente",
            linea_id=line.id,
            familia_id=family.id,
            um="UN",
            peso_g=240,
            status="ACTIVO",
            estado_revision="VERIFICADO",
            notas_revision="Producto monocomponente terminado en la OF.",
        )
        session.add(product)
    session.flush()
    if _one(
        session,
        ProductoPieza,
        producto_terminado_id=product.cod_sku_pt,
        pieza_sku=piece_color.sku,
    ) is None:
        session.add(ProductoPieza(
            producto_terminado_id=product.cod_sku_pt,
            pieza_sku=piece_color.sku,
            cantidad=1,
        ))
    if _one(
        session,
        ScmPresentacionComercial,
        producto_terminado_id=product.cod_sku_pt,
        nombre="Unidad",
    ) is None:
        session.add(ScmPresentacionComercial(
            codigo="PRE-JARRA-REAL-6L",
            producto_terminado_id=product.cod_sku_pt,
            nombre="Unidad",
            unidades_base=1,
            predeterminada=True,
        ))
    session.flush()
    piece_article = _one(
        session,
        ScmArticuloPiezaColor,
        pieza_color_sku=piece_color.sku,
    ).articulo
    product_article = _one(
        session,
        ScmArticuloProducto,
        producto_terminado_id=product.cod_sku_pt,
    ).articulo
    structure = session.scalar(select(ScmEstructuraRevision).where(
        ScmEstructuraRevision.articulo_resultado_id == product_article.id,
        ScmEstructuraRevision.estado == "APROBADA",
    ))
    if structure is None:
        approved_at = datetime.now(timezone.utc)
        structure = ScmEstructuraRevision(
            articulo_resultado_id=product_article.id,
            numero_revision=1,
            estado="BORRADOR",
            notas="Una unidad terminada contiene una pieza moldeada.",
            creada_por_id=actor.id,
            componentes=[ScmEstructuraComponente(
                secuencia=1,
                articulo_componente_id=piece_article.id,
                cantidad=1,
                unidad="UN",
                merma_tecnica_pct=Decimal("0"),
            )],
        )
        session.add(structure)
        # PostgreSQL congela los componentes apenas la revision deja de ser
        # borrador. Persistimos contenido y solo entonces la publicamos.
        session.flush()
        structure.content_hash = structure_content_hash(structure)
        structure.estado = "APROBADA"
        structure.enviada_at = approved_at
        structure.aprobada_por_id = actor.id
        structure.aprobada_at = approved_at
        session.flush()

    center = _one(session, ScmCentroTrabajo, codigo="CT-INYECCION-JARRAS")
    if center is None:
        center = ScmCentroTrabajo(
            codigo="CT-INYECCION-JARRAS",
            nombre="Inyeccion de Jarras",
            tipo="INYECCION",
        )
        session.add(center)
    route = session.scalar(select(ScmRutaRevision).where(
        ScmRutaRevision.articulo_objetivo_id == product_article.id,
        ScmRutaRevision.estado == "APROBADA",
    ))
    if route is None:
        approved_at = datetime.now(timezone.utc)
        route = ScmRutaRevision(
            articulo_objetivo_id=product_article.id,
            numero_revision=1,
            estado="BORRADOR",
            notas="La inyeccion produce directamente el producto terminado.",
            creada_por_id=actor.id,
        )
        session.add(route)
        session.flush()
        route.operaciones.append(ScmOperacionRuta(
            clave="FABRICAR",
            secuencia_visible=1,
            nombre="Fabricar Jarra Real 6 L",
            tipo="INYECCION",
            executor_kind="OP_OT",
            centro_trabajo_id=center.id,
            articulo_salida_id=product_article.id,
        ))
        # Las operaciones de una ruta aprobada son inmutables en PostgreSQL.
        session.flush()
        route.content_hash = route_content_hash(route)
        route.estado = "APROBADA"
        route.aprobada_por_id = actor.id
        route.aprobada_at = approved_at
        session.flush()
    return {
        "color": color,
        "piece": piece,
        "piece_color": piece_color,
        "product": product,
        "mold": mold,
        "piece_article": piece_article,
        "product_article": product_article,
        "structure": structure,
        "route": route,
        "center": center,
    }


def _ensure_packaging(session, *, actor, article):
    container = _one(session, ScmTipoContenedor, codigo="MANGA-JARRA-REAL-6L")
    if container is None:
        container = ScmTipoContenedor(
            codigo="MANGA-JARRA-REAL-6L",
            clase="MANGA",
            nombre="Manga para Jarra Real 6 L",
            material="Polietileno",
            tara_nominal_g=Decimal("30"),
            tolerancia_tara_g=Decimal("10"),
            peso_bruto_max_kg=Decimal("20"),
        )
        session.add(container)
    profile = _one(session, ScmPerfilEmpacable, codigo="PERFIL-JARRA-REAL-6L")
    if profile is None:
        profile = ScmPerfilEmpacable(
            codigo="PERFIL-JARRA-REAL-6L",
            nombre="Jarra Real 6 L por 50 unidades",
            descripcion_fisica="Manga de 50 jarras transparentes.",
        )
        session.add(profile)
    session.flush()
    link = _one(
        session,
        ScmArticuloPerfil,
        articulo_id=article.id,
        perfil_empacable_id=profile.id,
    )
    if link is None:
        session.add(ScmArticuloPerfil(
            articulo_id=article.id,
            perfil_empacable_id=profile.id,
            es_predeterminado=True,
            activo=True,
        ))
    rule = _one(
        session,
        ScmReglaEmpaque,
        perfil_empacable_id=profile.id,
        tipo_contenedor_id=container.id,
    )
    if rule is None:
        rule = ScmReglaEmpaque(
            perfil_empacable_id=profile.id,
            tipo_contenedor_id=container.id,
        )
        session.add(rule)
    session.flush()
    revision = _one(
        session,
        ScmReglaEmpaqueRevision,
        regla_id=rule.id,
        estado="APROBADA",
    )
    if revision is None:
        approved_at = datetime.now(timezone.utc)
        revision = ScmReglaEmpaqueRevision(
            regla_id=rule.id,
            numero_revision=1,
            estado="BORRADOR",
            medicion_fisica_probada=True,
            cantidad_objetivo_un=50,
            cantidad_maxima_probada_un=50,
            peso_neto_operativo_max_kg=Decimal("12"),
            margen_seguridad_kg=Decimal("0"),
            tolerancia_peso_abs_g=Decimal("0"),
            tolerancia_peso_pct=Decimal("0"),
            tara_nominal_g_snapshot=container.tara_nominal_g,
            tolerancia_tara_g_snapshot=container.tolerancia_tara_g,
            peso_bruto_max_kg_snapshot=container.peso_bruto_max_kg,
            notas="Regla base para el recorrido local.",
            creada_por_id=actor.id,
        )
        session.add(revision)
        session.flush()
        revision.content_hash = _rule_content_hash(revision)
        revision.estado = "APROBADA"
        revision.aprobada_por_id = actor.id
        revision.aprobada_at = approved_at
        session.flush()
    return container, profile, revision


def _ensure_machine_and_station(session):
    machine_type = _one(session, TipoMaquina, codigo="INYECTORA")
    if machine_type is None:
        machine_type = TipoMaquina(
            codigo="INYECTORA",
            nombre="Inyectora",
            proceso="INYECCION",
        )
        session.add(machine_type)
    session.flush()
    machine = _one(session, Maquina, codigo="INY-01")
    if machine is None:
        machine = Maquina(
            codigo="INY-01",
            nombre="Inyectora 1",
            tipo_maquina_id=machine_type.id,
            estado="OPERATIVA",
            activo=True,
            tipo="INYECCION",
        )
        session.add(machine)
    station_id = str(_stable_uuid("station"))
    station = session.get(EstacionPesaje, station_id)
    if station is None:
        station = EstacionPesaje(
            station_id=station_id,
            codigo="BAL-PLANTA-01",
            nombre="Balanza de Planta 1",
            ubicacion="Area de pesaje de produccion",
            estado_admin="ACTIVA",
            token_hash=hash_station_token(STATION_TOKEN),
        )
        session.add(station)
    return machine, station


def _ensure_warehouses(session, *, actors):
    definitions = (
        (
            "A-ENVA-MP",
            "Almacen de Materias Primas",
            "MATERIAS_PRIMAS",
            "A-ENVA-MP-GEN",
            "Zona General de Materias Primas",
            "ZONA",
            ["MATERIA_PRIMA", "COLORANTE"],
        ),
        (
            "A-ENVA-PZ",
            "Almacen de Piezas y WIP",
            "PIEZAS_WIP",
            "A-ENVA-PZ-REC",
            "Recepcion de Piezas y WIP",
            "RECEPCION",
            ["PIEZA_COLOR", "SUBENSAMBLE_WIP"],
        ),
        (
            "A-ENVA-PT",
            "Almacen de Producto Terminado",
            "PRODUCTO_TERMINADO",
            "A-ENVA-PT-REC",
            "Recepcion de Producto Terminado",
            "RECEPCION",
            ["PRODUCTO_TERMINADO"],
        ),
    )
    result = {}
    for (
        warehouse_code,
        warehouse_name,
        warehouse_type,
        location_code,
        location_name,
        location_type,
        classes,
    ) in definitions:
        warehouse = _one(session, ScmAlmacen, codigo=warehouse_code)
        if warehouse is None:
            warehouse = ScmAlmacen(
                codigo=warehouse_code,
                nombre=warehouse_name,
                tipo=warehouse_type,
                configuracion_json={"escenario": "recorrido_local"},
            )
            session.add(warehouse)
        session.flush()
        location = _one(session, ScmUbicacionInventario, codigo=location_code)
        if location is None:
            location = ScmUbicacionInventario(
                almacen_id=warehouse.id,
                codigo=location_code,
                nombre=location_name,
                tipo=location_type,
                clases_articulo_json=classes,
                permite_saldo_libre=True,
            )
            session.add(location)
        else:
            location.almacen_id = warehouse.id
            location.tipo = location_type
            location.clases_articulo_json = classes
        assignment = _one(
            session,
            ScmAlmacenTrabajador,
            almacen_id=warehouse.id,
            trabajador_id=actors["almacen"].id,
        )
        if assignment is None:
            session.add(ScmAlmacenTrabajador(
                almacen_id=warehouse.id,
                trabajador_id=actors["almacen"].id,
                clases_articulo_json=classes,
                asignado_por_id=actors["gerencia"].id,
            ))
        result[warehouse_code] = {"warehouse": warehouse, "location": location}
    return result


def _assert_pre_document_baseline(session):
    counts = {
        "openings": session.query(ScmLoteAperturaInventario).count(),
        "production_orders": session.query(ScmOrdenProduccion).count(),
        "operation_orders": session.query(ScmOrdenOperacion).count(),
        "work_orders": session.query(RegistroDiarioProduccion).count(),
        "color_work": session.query(ScmTrabajoOt).count(),
        "mangas": session.query(ScmManga).count(),
        "labels": session.query(ScmEtiquetaManga).count(),
        "print_jobs": session.query(ScmTrabajoImpresionManga).count(),
        "weighings": session.query(ScmPesajeManga).count(),
        "article_balances": session.query(ScmSaldoInventario).count(),
        "material_balances": session.query(ScmSaldoMaterialInventario).count(),
        "article_movements": session.query(ScmMovimientoInventario).count(),
        "material_movements": session.query(ScmMovimientoMaterialInventario).count(),
    }
    # Las operaciones idempotentes pueden existir por configuracion tecnica,
    # pero ningun documento o movimiento del recorrido debe estar precargado.
    if any(counts.values()):
        raise LocalWalkthroughSeedError(
            "La base exclusiva no esta limpia antes del recorrido: "
            f"{counts}"
        )
    return counts


def seed_uat_walkthrough(
    session,
    *,
    database_url: str,
    connection_database: str,
    migration_revision: str,
    operational_date: date,
    validate_environment: bool = True,
):
    """Crea maestros reales, sin abrir inventario ni documentos operativos."""

    if validate_environment:
        assert_local_walkthrough_database(
            database_url,
            connection_database=connection_database,
            migration_revision=migration_revision,
        )
    _assert_pre_document_baseline(session)
    actors = _ensure_actors(session)
    line, family = _ensure_classification(session)
    catalogs = _ensure_product_engineering(
        session,
        actor=actors["jefe_produccion"],
        line=line,
        family=family,
    )
    material, recipe = _ensure_material_and_recipe(
        session,
        product=catalogs["product"],
        color=catalogs["color"],
    )
    container, profile, packaging = _ensure_packaging(
        session,
        actor=actors["jefe_produccion"],
        article=catalogs["product_article"],
    )
    machine, station = _ensure_machine_and_station(session)
    warehouses = _ensure_warehouses(session, actors=actors)
    session.commit()
    empty_counts = _assert_pre_document_baseline(session)
    return {
        "status": "SCM_UAT_RECORRIDO_OK",
        "marker": WALKTHROUGH_MARKER,
        "fecha_operativa": operational_date.isoformat(),
        # La estacion confirma el pesaje con su operador. El maquinista queda
        # disponible por separado para la asignacion de la OT y sus relevos.
        "operator_id": actors["operador_pesaje"].id,
        "station_id": station.station_id,
        "station_token": STATION_TOKEN,
        "actor_ids": {key: value.id for key, value in actors.items()},
        "product": {
            "code": catalogs["product"].cod_sku_pt,
            "name": catalogs["product"].producto,
            "article_id": catalogs["product_article"].id,
            "piece_code": catalogs["piece"].codigo,
            "piece_color_code": catalogs["piece_color"].sku,
            "color_id": catalogs["color"].id,
            "mold_code": catalogs["mold"].codigo,
            "structure_revision_id": catalogs["structure"].id,
            "route_revision_id": catalogs["route"].id,
            "packaging_rule_revision_id": packaging.id,
        },
        "material": {
            "id": material.id,
            "code": material.codigo,
            "name": material.nombre,
            "recipe_id": recipe.id,
            "base_virgen_kg": format(Decimal(recipe.base_virgen_kg), "f"),
        },
        "machine": {"id": machine.id, "code": machine.codigo},
        "packaging": {
            "container_code": container.codigo,
            "profile_code": profile.codigo,
            "units_per_manga": packaging.cantidad_objetivo_un,
        },
        "warehouse_codes": {
            "raw_material": "A-ENVA-MP",
            "pieces_wip": "A-ENVA-PZ",
            "finished_product": "A-ENVA-PT",
        },
        "location_codes": {
            "raw_material": warehouses["A-ENVA-MP"]["location"].codigo,
            "pieces_wip": warehouses["A-ENVA-PZ"]["location"].codigo,
            "finished_product": warehouses["A-ENVA-PT"]["location"].codigo,
        },
        "opening_suggestion": {
            "preparer_actor_id": actors["almacen"].id,
            "approver_actor_id": actors["jefe_produccion"].id,
            "material_code": material.codigo,
            "cantidad_kg": "500.000",
            # Copia exactamente el contrato que envia InventoryOpeningScm.
            "payload": {
                "fecha_corte": operational_date.isoformat(),
                "motivo": "Conteo inicial de PP clarificado",
                "lineas": [{
                    "material_scm_id": material.id,
                    "cantidad": "500.000",
                    "ubicacion_codigo": (
                        warehouses["A-ENVA-MP"]["location"].codigo
                    ),
                    "ubicacion_nombre": (
                        warehouses["A-ENVA-MP"]["location"].nombre
                    ),
                    "estado_calidad": "LIBERADO",
                    "observacion": "Sacos verificados para el inicio del recorrido",
                }],
            },
        },
        "urls": {
            "inventory_opening": "/almacen/kardex",
            "production_orders": "/planificacion",
            "fabrication_orders": "/produccion/ordenes-fabricacion",
            "plant_work_orders": "/produccion/ots-planta",
            "weighing_station": "http://127.0.0.1:5051/?tab=scm-weighing",
            "warehouse_operations": "/almacen/operaciones",
        },
        "empty_operational_counts": empty_counts,
    }
