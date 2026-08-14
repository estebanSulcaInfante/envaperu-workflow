from datetime import date

import pytest

from app import db
from app.models.estacion_pesaje import EstacionPesaje
from app.models.maquina import Maquina, TipoMaquina
from app.models.materiales import MateriaPrima
from app.models.molde import Molde, Pieza
from app.models.producto import (
    ColorBase,
    Familia,
    FamiliaColor,
    Linea,
    PiezaColor,
    ProductoTerminado,
)
from app.models.receta_color import RecetaColorMaestra
from app.models.registro import RegistroDiarioProduccion
from app.models.scm_commercial import ScmPresentacionComercial
from app.models.scm_empaque import ScmPerfilEmpacable, ScmTipoContenedor
from app.models.scm_inventory import (
    ScmLoteAperturaInventario,
    ScmMovimientoInventario,
    ScmMovimientoMaterialInventario,
    ScmSaldoInventario,
    ScmSaldoMaterialInventario,
    ScmUbicacionInventario,
)
from app.models.scm_inventory_operations import ScmAlmacen
from app.models.scm_ot import (
    ScmEtiquetaManga,
    ScmManga,
    ScmPesajeManga,
    ScmTrabajoImpresionManga,
)
from app.models.scm_production_orders import (
    ScmOrdenOperacion,
    ScmOrdenProduccion,
)
from app.models.scm_rutas import ScmCentroTrabajo
from app.models.trabajador import Trabajador
from app.services.scm_production_order_service import _build_plan_proposal
from app.services.scm_uat_walkthrough_seed_service import (
    ALEMBIC_HEAD,
    LocalWalkthroughSeedError,
    assert_local_walkthrough_database,
    seed_uat_walkthrough,
)


LOCAL_URL = (
    "postgresql+psycopg2://postgres:secret@localhost/"
    "enva_uat_recorrido"
)


def test_walkthrough_guard_only_accepts_the_exclusive_loopback_database():
    with pytest.raises(LocalWalkthroughSeedError, match="loopback"):
        assert_local_walkthrough_database(
            "postgresql://postgres:secret@db.example.com/enva_uat_recorrido",
            connection_database="enva_uat_recorrido",
            migration_revision=ALEMBIC_HEAD,
        )
    with pytest.raises(LocalWalkthroughSeedError, match="no autorizada"):
        assert_local_walkthrough_database(
            "postgresql://postgres:secret@localhost/enva_test",
            connection_database="enva_test",
            migration_revision=ALEMBIC_HEAD,
        )
    with pytest.raises(LocalWalkthroughSeedError, match="migraciones"):
        assert_local_walkthrough_database(
            LOCAL_URL,
            connection_database="enva_uat_recorrido",
            migration_revision="anterior",
        )


def test_walkthrough_seed_creates_real_masters_and_zero_operational_documents(app):
    with app.app_context():
        result = seed_uat_walkthrough(
            db.session,
            database_url=LOCAL_URL,
            connection_database="enva_uat_recorrido",
            migration_revision=ALEMBIC_HEAD,
            operational_date=date(2026, 8, 13),
        )

        assert result["status"] == "SCM_UAT_RECORRIDO_OK"
        assert result["product"]["name"] == "Jarra Real 6 L Transparente"
        assert result["material"]["name"] == "PP clarificado"
        assert result["opening_suggestion"]["cantidad_kg"] == "500.000"
        assert result["opening_suggestion"]["payload"] == {
            "fecha_corte": "2026-08-13",
            "motivo": "Conteo inicial de PP clarificado",
            "lineas": [{
                "material_scm_id": result["material"]["id"],
                "cantidad": "500.000",
                "ubicacion_codigo": "A-ENVA-MP-GEN",
                "ubicacion_nombre": "Zona General de Materias Primas",
                "estado_calidad": "LIBERADO",
                "observacion": "Sacos verificados para el inicio del recorrido",
            }],
        }
        assert result["operator_id"] == result["actor_ids"]["operador_pesaje"]
        assert result["operator_id"] != result["actor_ids"]["maquinista"]
        assert result["station_token"]
        assert len(result["actor_ids"]) == 9
        assert len(set(result["actor_ids"].values())) == 9
        actors = {
            key: db.session.get(Trabajador, actor_id)
            for key, actor_id in result["actor_ids"].items()
        }
        assert all(len(actor.roles) == 1 for actor in actors.values())
        expected_capabilities = {
            "almacen": {"INVENTARIO_APERTURA_PREPARAR"},
            "jefe_produccion": {"INVENTARIO_APERTURA_APROBAR"},
            "planificacion": {"OP_CREAR", "PLANIFICACION_CALCULAR"},
            "gerencia": {"OP_APROBAR"},
            "supervisor": {"OT_CREAR"},
            "calidad": {"CALIDAD_MANGA_LIBERAR"},
        }
        for key, capabilities in expected_capabilities.items():
            assert capabilities <= actors[key].capacidades_efectivas

        visible_values = [
            result["product"]["code"],
            result["product"]["name"],
            result["material"]["code"],
            result["material"]["name"],
            *(
                value
                for actor in actors.values()
                for value in (
                    actor.codigo,
                    actor.nombres,
                    actor.apellidos,
                    actor.nombre_corto,
                )
            ),
            *(
                value
                for warehouse in ScmAlmacen.query.all()
                for value in (warehouse.codigo, warehouse.nombre)
            ),
        ]
        visible_models = (
            (Linea, ("nombre",)),
            (Familia, ("nombre",)),
            (FamiliaColor, ("nombre",)),
            (ColorBase, ("nombre",)),
            (Pieza, ("codigo", "nombre")),
            (Molde, ("codigo", "nombre")),
            (PiezaColor, ("sku", "piezas")),
            (ProductoTerminado, ("cod_sku_pt", "producto")),
            (RecetaColorMaestra, ("nombre_variante",)),
            (ScmPresentacionComercial, ("codigo", "nombre")),
            (ScmCentroTrabajo, ("codigo", "nombre")),
            (ScmTipoContenedor, ("codigo", "nombre")),
            (ScmPerfilEmpacable, ("codigo", "nombre")),
            (TipoMaquina, ("codigo", "nombre")),
            (Maquina, ("codigo", "nombre")),
            (EstacionPesaje, ("codigo", "nombre", "ubicacion")),
            (ScmUbicacionInventario, ("codigo", "nombre")),
        )
        visible_values.extend(
            getattr(row, attribute)
            for model, attributes in visible_models
            for row in model.query.all()
            for attribute in attributes
        )
        forbidden = ("MOCK", "DEMO", "UAT")
        assert all(
            token not in str(value).upper()
            for value in visible_values
            for token in forbidden
        )

        recipe = db.session.get(RecetaColorMaestra, result["material"]["recipe_id"])
        assert recipe.estado == "APROBADA"
        assert recipe.base_virgen_kg == 25
        assert len(recipe.lineas) == 1
        assert recipe.lineas[0].tipo_componente == "MATERIA_PRIMA"
        assert recipe.lineas[0].cantidad == 1
        assert MateriaPrima.query.filter_by(
            scm_material_id=result["material"]["id"], tipo="VIRGEN"
        ).one()
        assert db.session.get(EstacionPesaje, result["station_id"])
        assert {item.codigo for item in ScmAlmacen.query.all()} == {
            "A-ENVA-MP",
            "A-ENVA-PZ",
            "A-ENVA-PT",
        }
        assert result["urls"] == {
            "inventory_opening": "/almacen/kardex",
            "production_orders": "/planificacion",
            "fabrication_orders": "/produccion/ordenes-fabricacion",
            "plant_work_orders": "/produccion/ots-planta",
            "weighing_station": "http://127.0.0.1:5051/?tab=scm-weighing",
            "warehouse_operations": "/almacen/operaciones",
        }

        assert ScmLoteAperturaInventario.query.count() == 0
        assert ScmOrdenProduccion.query.count() == 0
        assert ScmOrdenOperacion.query.count() == 0
        assert RegistroDiarioProduccion.query.count() == 0
        assert ScmManga.query.count() == 0
        assert ScmEtiquetaManga.query.count() == 0
        assert ScmTrabajoImpresionManga.query.count() == 0
        assert ScmPesajeManga.query.count() == 0
        assert ScmSaldoInventario.query.count() == 0
        assert ScmSaldoMaterialInventario.query.count() == 0
        assert ScmMovimientoInventario.query.count() == 0
        assert ScmMovimientoMaterialInventario.query.count() == 0

        # La ruta queda lista para que una OP creada en la interfaz produzca
        # una unica OF terminal, no una OA ficticia.
        from app.models.scm_production_orders import ScmOrdenProduccionLinea
        from app.models.scm_estructuras import ScmEstructuraRevision
        from app.models.scm_rutas import ScmRutaRevision

        planner = db.session.get(Trabajador, result["actor_ids"]["planificacion"])
        structure = db.session.get(
            ScmEstructuraRevision,
            result["product"]["structure_revision_id"],
        )
        route = db.session.get(
            ScmRutaRevision,
            result["product"]["route_revision_id"],
        )
        order = ScmOrdenProduccion(
            codigo="OP-VERIFICACION",
            origen="PLANIFICACION",
            fecha_necesidad=date(2026, 8, 20),
            estado="APROBADA",
            created_by_id=planner.id,
            approved_by_id=planner.id,
            lineas=[ScmOrdenProduccionLinea(
                producto_terminado_id=result["product"]["code"],
                cantidad_solicitada=100,
                estructura_revision_id=structure.id,
                estructura_hash=structure.content_hash,
                ruta_revision_id=route.id,
                ruta_hash=route.content_hash,
            )],
        )
        db.session.add(order)
        db.session.flush()
        proposal = _build_plan_proposal(db.session, order)
        assert proposal["bloqueos"] == []
        assert [item["tipo"] for item in proposal["documentos"]] == [
            "FABRICACION"
        ]
        assert proposal["documentos"][0]["articulo"]["codigo"] == (
            result["product"]["code"]
        )


def test_walkthrough_cli_requires_explicit_confirmation(app, runner):
    with app.app_context():
        response = runner.invoke(
            args=["seed-uat-recorrido", "--fecha-operativa", "2026-08-13"]
        )
        assert response.exit_code != 0
        assert "--confirm-local" in response.output
        assert ScmAlmacen.query.count() == 0
