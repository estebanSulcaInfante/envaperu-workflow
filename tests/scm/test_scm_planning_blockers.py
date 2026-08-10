from datetime import date

from app import db
from app.models.producto import ProductoTerminado
from app.models.scm_articulos import ScmArticulo, ScmArticuloProducto
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
    ScmOperacionRuta,
    ScmRutaRevision,
)
from app.models.trabajador import Trabajador
from app.services.scm_production_order_service import _build_plan_proposal


def test_route_output_blocker_exposes_required_article_and_reason(app):
    with app.app_context():
        actor = Trabajador.query.filter_by(codigo="TRB-01").one()
        product = ProductoTerminado(
            cod_sku_pt="PT-BLOCKER",
            producto="Producto con componente sin operacion",
            linea_id=1,
            familia_id=1,
        )
        missing_article = ScmArticulo(
            codigo="WIP-BLOCKER",
            nombre="Subensamble requerido",
            clase="SUBENSAMBLE_WIP",
        )
        db.session.add_all([product, missing_article])
        db.session.flush()
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
                articulo_componente_id=missing_article.id,
                cantidad=1,
                unidad="UN",
            )],
        )
        center = ScmCentroTrabajo(
            codigo="CT-BLOCKER",
            nombre="Armado del producto",
            tipo="ENSAMBLE",
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
        db.session.add(ScmOperacionRuta(
            ruta=route,
            clave="ARMAR",
            secuencia_visible=1,
            nombre="Armar producto",
            tipo="ENSAMBLE",
            executor_kind="ORDEN_OPERACION",
            centro_trabajo_id=center.id,
            articulo_salida_id=product_article.id,
            estructura_revision_id=structure.id,
        ))
        order = ScmOrdenProduccion(
            codigo="OP-BLOCKER",
            origen="PLANIFICACION",
            fecha_necesidad=date(2026, 8, 17),
            estado="APROBADA",
            created_by_id=actor.id,
            approved_by_id=actor.id,
        )
        line = ScmOrdenProduccionLinea(
            producto_terminado_id=product.cod_sku_pt,
            cantidad_solicitada=12,
            estructura_revision_id=structure.id,
            estructura_hash=structure.content_hash,
            ruta_revision_id=route.id,
            ruta_hash=route.content_hash,
        )
        order.lineas.append(line)
        db.session.add(order)
        db.session.commit()

        proposal = _build_plan_proposal(db.session, order)

        assert proposal["bloqueos"] == [{
            "codigo": "ROUTE_OUTPUT_MISSING",
            "linea_id": str(line.id),
            "producto_terminado_id": "PT-BLOCKER",
            "ruta_revision_id": route.id,
            "articulo_id": missing_article.id,
            "articulo_codigo": "WIP-BLOCKER",
            "articulo": {
                "codigo": "WIP-BLOCKER",
                "nombre": "Subensamble requerido",
                "clase": "SUBENSAMBLE_WIP",
            },
            "cantidad": "12",
            "motivo_codigo": "SIN_OPERACION_DE_RUTA",
            "motivo": (
                "La BOM requiere este articulo, pero la ruta aprobada no "
                "incluye una operacion cuya salida sea este articulo."
            ),
        }]
