from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.materiales import MateriaPrima
from app.models.scm_catalogos import (
    MODALIDAD_POR_CONFIGURAR,
    MODALIDAD_VIRGEN,
    ScmCapacidad,
    ScmCategoriaRecepcion,
    ScmMaterial,
)
from app.models.trabajador import RolOperativo, Trabajador


CAPACIDADES_MINIMAS = {
    "PROVEEDOR_ADMINISTRAR",
    "OC_CREAR",
    "OC_APROBAR",
    "RECEPCION_CONFIRMAR",
    "ENTRADA_EXCEPCIONAL_REGULARIZAR",
    "CALIDAD_RESOLVER",
    "LIBERACION_DIRECTA_ADMINISTRAR",
    "CORRECCION_SOLICITAR",
    "CORRECCION_APROBAR",
    "DEVOLUCION_REGISTRAR",
    "CONFIG_RECEPCION_ADMINISTRAR",
}

ROLES_SCM_INICIALES = {
    "COMPRAS",
    "ALMACEN_RECEPCION",
    "CALIDAD",
    "GERENTE_GENERAL",
    "GERENCIA",
    "SUPERVISOR",
    "CONFIGURACION_SCM",
    "AUDITORIA_CONSULTA",
}


def test_app_inicializa_flask_migrate(app, runner):
    assert "migrate" in app.extensions
    assert app.extensions["migrate"].db is db
    assert app.config["SCM_RECEPCION_ENABLED"] is False
    result = runner.invoke(args=["db", "heads"])
    assert result.exit_code == 0, result.output

    migrations_dir = Path(__file__).resolve().parents[2] / "migrations"
    config = Config(str(migrations_dir / "alembic.ini"))
    config.set_main_option("script_location", str(migrations_dir))
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1


def test_material_comun_mantiene_identidad_unica_con_materia_prima(app):
    with app.app_context():
        categoria = ScmCategoriaRecepcion(
            codigo="VIRGEN",
            nombre="Material virgen",
            modalidad_default=MODALIDAD_VIRGEN,
            lote_externo_obligatorio=False,
            recepcion_habilitada=True,
        )
        material = ScmMaterial(
            codigo="MP-000001",
            nombre="PP virgen",
            clase="MATERIA_PRIMA",
            categoria_recepcion=categoria,
        )
        materia_prima = MateriaPrima(
            nombre="PP virgen",
            tipo="VIRGEN",
            scm_material=material,
        )
        db.session.add(materia_prima)
        db.session.commit()

        assert materia_prima.scm_material_id == material.id
        assert material.materia_prima is materia_prima
        assert material.unidad_base == "KG"

        duplicada = MateriaPrima(
            nombre="Otro nombre legacy",
            tipo="VIRGEN",
            scm_material_id=material.id,
        )
        db.session.add(duplicada)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_categoria_rechaza_modalidad_fuera_del_contrato(app):
    with app.app_context():
        db.session.add(
            ScmCategoriaRecepcion(
                codigo="INVALIDA",
                nombre="Inválida",
                modalidad_default="PESO_ESTIMADO",
            )
        )

        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_capacidades_efectivas_se_derivan_solo_de_roles_y_catalogos_activos(app):
    with app.app_context():
        capacidad = ScmCapacidad(
            codigo="RECEPCION_CONFIRMAR",
            nombre="Confirmar recepción",
        )
        capacidad_oc = ScmCapacidad(
            codigo="OC_CREAR",
            nombre="Crear OC",
        )
        rol = RolOperativo(
            codigo="ALMACEN_TEST",
            nombre="Almacén de prueba",
            capacidades=[capacidad],
        )
        segundo_rol = RolOperativo(
            codigo="COMPRAS_TEST",
            nombre="Compras de prueba",
            capacidades=[capacidad, capacidad_oc],
        )
        trabajador = Trabajador(
            codigo="TRB-SCM-01",
            nombres="Ana",
            apellidos="Prueba",
            roles=[rol, segundo_rol],
        )
        db.session.add(trabajador)
        db.session.commit()

        assert trabajador.capacidades_efectivas == {
            "RECEPCION_CONFIRMAR",
            "OC_CREAR",
        }
        assert trabajador.tiene_capacidad("RECEPCION_CONFIRMAR") is True
        assert trabajador.tiene_capacidad("OC_APROBAR") is False

        capacidad.activo = False
        assert trabajador.capacidades_efectivas == {"OC_CREAR"}

        capacidad.activo = True
        rol.activo = False
        assert trabajador.capacidades_efectivas == {
            "RECEPCION_CONFIRMAR",
            "OC_CREAR",
        }

        segundo_rol.activo = False
        assert trabajador.capacidades_efectivas == set()

        segundo_rol.activo = True
        trabajador.activo = False
        assert trabajador.capacidades_efectivas == set()


def test_seed_scm_es_idempotente_y_no_asigna_personas(app, runner):
    with app.app_context():
        personas_antes = {
            item.id: (
                item.codigo,
                item.activo,
                tuple(sorted(rol.id for rol in item.roles)),
            )
            for item in Trabajador.query.all()
        }

    primera = runner.invoke(args=["seed-scm-config"])
    assert primera.exit_code == 0, primera.output

    with app.app_context():
        compras = db.session.scalar(
            db.select(RolOperativo).where(RolOperativo.codigo == "COMPRAS")
        )
        capacidad = db.session.scalar(
            db.select(ScmCapacidad).where(
                ScmCapacidad.codigo == "PROVEEDOR_ADMINISTRAR"
            )
        )
        compras.nombre = "Compras configurado localmente"
        compras.capacidades.remove(capacidad)
        capacidad.activo = False
        db.session.commit()

        conteos_antes = (
            ScmCapacidad.query.count(),
            RolOperativo.query.count(),
            ScmCategoriaRecepcion.query.count(),
        )

    segunda = runner.invoke(args=["seed-scm-config"])
    assert segunda.exit_code == 0, segunda.output

    with app.app_context():
        capacidades = {item.codigo for item in ScmCapacidad.query.all()}
        roles = {item.codigo: item for item in RolOperativo.query.all()}
        categorias = {
            item.codigo: item for item in ScmCategoriaRecepcion.query.all()
        }

        assert CAPACIDADES_MINIMAS <= capacidades
        assert ROLES_SCM_INICIALES <= set(roles)
        assert {
            item.codigo for item in roles["GERENTE_GENERAL"].capacidades
        } == capacidades
        assert "OA_EXCEPCIONAL_CREAR" not in capacidades
        assert {
            item.codigo for item in roles["GERENCIA"].capacidades
        } >= {"OC_APROBAR", "CORRECCION_APROBAR"}
        assert {
            item.codigo for item in roles["CONFIGURACION_SCM"].capacidades
        } >= {"CONFIG_RECEPCION_ADMINISTRAR"}
        assert {
            item.codigo for item in roles["JEFE_PRODUCCION"].capacidades
        } >= {"MANGA_ETIQUETA_PRE_GENERAR"}
        assert categorias["RESINA_VIRGEN"].modalidad_default == MODALIDAD_VIRGEN
        assert (
            categorias["LEGACY_POR_CONFIGURAR"].modalidad_default
            == MODALIDAD_POR_CONFIGURAR
        )
        assert categorias["LEGACY_POR_CONFIGURAR"].recepcion_habilitada is False
        assert roles["COMPRAS"].nombre == "Compras configurado localmente"
        assert "PROVEEDOR_ADMINISTRAR" in {
            item.codigo for item in roles["COMPRAS"].capacidades
        }
        assert db.session.scalar(
            db.select(ScmCapacidad.activo).where(
                ScmCapacidad.codigo == "PROVEEDOR_ADMINISTRAR"
            )
        ) is False
        assert conteos_antes == (
            ScmCapacidad.query.count(),
            RolOperativo.query.count(),
            ScmCategoriaRecepcion.query.count(),
        )
        personas_despues = {
            item.id: (
                item.codigo,
                item.activo,
                tuple(sorted(rol.id for rol in item.roles)),
            )
            for item in Trabajador.query.all()
        }
        assert personas_despues == personas_antes
