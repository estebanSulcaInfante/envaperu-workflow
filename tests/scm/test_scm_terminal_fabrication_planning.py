from datetime import date

from app import db
from app.models.molde import Pieza
from app.models.producto import (
    ColorBase,
    ColorProduccion,
    Familia,
    FamiliaColor,
    Linea,
    PiezaColor,
    ProductoTerminado,
)
from app.models.scm_articulos import (
    ScmArticuloPiezaColor,
    ScmArticuloProducto,
)
from app.models.scm_estructuras import (
    ScmEstructuraComponente,
    ScmEstructuraRevision,
)
from app.models.scm_production_orders import (
    ScmOrdenProduccion,
    ScmOrdenProduccionLinea,
)
from app.models.scm_rutas import (
    ScmCentroTrabajo,
    ScmOperacionPrecedencia,
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.trabajador import Trabajador
from app.services.scm_production_order_service import _build_plan_proposal


def _masters(*, suffix):
    actor = Trabajador.query.filter_by(codigo="TRB-01").one()
    line = Linea.query.first()
    family = Familia.query.first()
    piece = Pieza(
        codigo=f"PZ-{suffix}",
        nombre=f"Pieza {suffix}",
        linea_id=line.id,
        familia_id=family.id,
        peso_nominal_gr=240,
    )
    base = ColorBase(nombre=f"Transparente {suffix}")
    finish = FamiliaColor(nombre=f"Transparente {suffix}")
    color = ColorProduccion(
        color_base_rel=base,
        familia_color_rel=finish,
        hex_referencia="#EAF7F7",
    )
    piece_color = PiezaColor(
        sku=f"PC-{suffix}",
        pieza_rel=piece,
        piezas=f"Pieza transparente {suffix}",
        color_produccion_rel=color,
        linea_id=line.id,
        familia_id=family.id,
        peso=240,
    )
    product = ProductoTerminado(
        cod_sku_pt=f"PT-{suffix}",
        producto=f"Producto {suffix}",
        linea_id=line.id,
        familia_id=family.id,
        peso_g=240,
    )
    db.session.add_all([piece, base, finish, color, piece_color, product])
    db.session.flush()
    piece_article = ScmArticuloPiezaColor.query.filter_by(
        pieza_color_sku=piece_color.sku,
    ).one().articulo
    product_article = ScmArticuloProducto.query.filter_by(
        producto_terminado_id=product.cod_sku_pt,
    ).one().articulo
    structure = ScmEstructuraRevision(
        articulo_resultado_id=product_article.id,
        numero_revision=1,
        estado="APROBADA",
        content_hash="a" * 64,
        creada_por_id=actor.id,
        aprobada_por_id=actor.id,
        componentes=[ScmEstructuraComponente(
            secuencia=1,
            articulo_componente_id=piece_article.id,
            cantidad=1,
            unidad="UN",
        )],
    )
    center = ScmCentroTrabajo(
        codigo=f"CT-{suffix}",
        nombre=f"Centro {suffix}",
        tipo="INYECCION",
    )
    route = ScmRutaRevision(
        articulo_objetivo_id=product_article.id,
        numero_revision=1,
        estado="APROBADA",
        content_hash="b" * 64,
        creada_por_id=actor.id,
        aprobada_por_id=actor.id,
    )
    db.session.add_all([structure, center, route])
    db.session.flush()
    return actor, product, product_article, piece_article, structure, center, route


def _order(*, actor, product, structure, route):
    order = ScmOrdenProduccion(
        codigo=f"OP-{product.cod_sku_pt}",
        origen="PLANIFICACION",
        fecha_necesidad=date(2026, 8, 20),
        estado="APROBADA",
        created_by_id=actor.id,
        approved_by_id=actor.id,
    )
    order.lineas.append(ScmOrdenProduccionLinea(
        producto_terminado_id=product.cod_sku_pt,
        cantidad_solicitada=100,
        estructura_revision_id=structure.id,
        estructura_hash=structure.content_hash,
        ruta_revision_id=route.id,
        ruta_hash=route.content_hash,
    ))
    db.session.add(order)
    db.session.flush()
    return order


def test_terminal_op_ot_can_finish_a_monocomponent_product_without_fake_assembly(app):
    """La BOM sigue siendo maestra tecnica; la OF acredita directamente el PT."""

    with app.app_context():
        actor, product, product_article, _piece_article, structure, center, route = (
            _masters(suffix="TERMINAL")
        )
        db.session.add(ScmOperacionRuta(
            ruta=route,
            clave="FABRICAR",
            secuencia_visible=1,
            nombre="Fabricar producto terminado",
            tipo="INYECCION",
            executor_kind="OP_OT",
            centro_trabajo_id=center.id,
            articulo_salida_id=product_article.id,
        ))
        order = _order(
            actor=actor,
            product=product,
            structure=structure,
            route=route,
        )

        proposal = _build_plan_proposal(db.session, order)

        assert proposal["bloqueos"] == []
        assert [
            (item["tipo"], item["articulo"]["codigo"], item["cantidad_objetivo"])
            for item in proposal["documentos"]
        ] == [("FABRICACION", product.cod_sku_pt, "100")]


def test_multistage_piece_fabrication_and_product_assembly_still_expand_the_bom(app):
    with app.app_context():
        actor, product, product_article, piece_article, structure, center, route = (
            _masters(suffix="MULTI")
        )
        fabrication = ScmOperacionRuta(
            ruta=route,
            clave="FABRICAR",
            secuencia_visible=1,
            nombre="Fabricar pieza",
            tipo="INYECCION",
            executor_kind="OP_OT",
            centro_trabajo_id=center.id,
            articulo_salida_id=piece_article.id,
        )
        assembly = ScmOperacionRuta(
            ruta=route,
            clave="ARMAR",
            secuencia_visible=2,
            nombre="Armar producto",
            tipo="ENSAMBLE",
            executor_kind="ORDEN_OPERACION",
            centro_trabajo_id=center.id,
            articulo_salida_id=product_article.id,
            estructura_revision_id=structure.id,
        )
        db.session.add_all([fabrication, assembly])
        db.session.flush()
        db.session.add(ScmOperacionPrecedencia(
            ruta_id=route.id,
            operacion_anterior_id=fabrication.id,
            operacion_siguiente_id=assembly.id,
        ))
        order = _order(
            actor=actor,
            product=product,
            structure=structure,
            route=route,
        )

        proposal = _build_plan_proposal(db.session, order)

        assert proposal["bloqueos"] == []
        assert [item["tipo"] for item in proposal["documentos"]] == [
            "FABRICACION",
            "ENSAMBLE",
        ]
        assert [item["cantidad_objetivo"] for item in proposal["documentos"]] == [
            "100",
            "100",
        ]
