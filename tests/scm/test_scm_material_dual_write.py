import pytest

from app.extensions import db
from app.models.materiales import Colorante, MateriaPrima
from app.services.scm_material_service import (
    ScmMaterialConfigurationError,
    create_colorante_with_scm,
    create_materia_prima_with_scm,
)


def test_dual_write_exige_categorias_tecnicas_configuradas(app):
    with app.app_context():
        with pytest.raises(ScmMaterialConfigurationError) as error:
            create_materia_prima_with_scm(
                session=db.session,
                nombre="PP sin configuración",
                tipo="VIRGEN",
            )

        assert error.value.code == "SCM_CATEGORY_NOT_CONFIGURED"
        assert MateriaPrima.query.filter_by(nombre="PP sin configuración").first() is None


def test_dual_write_materia_prima_clasifica_solo_valores_inequivocos(
    app,
    runner,
):
    assert runner.invoke(args=["seed-scm-config"]).exit_code == 0

    with app.app_context():
        virgen = create_materia_prima_with_scm(
            session=db.session,
            nombre="PP virgen nuevo",
            tipo=" virgen ",
        )
        ambiguo = create_materia_prima_with_scm(
            session=db.session,
            nombre="Material molido",
            tipo="MOLIDO",
        )
        db.session.commit()

        assert virgen.tipo == "VIRGEN"
        assert virgen.scm_material.codigo.startswith("MP-AUTO-")
        assert virgen.scm_material.clase == "MATERIA_PRIMA"
        assert (
            virgen.scm_material.categoria_recepcion.codigo
            == "RESINA_VIRGEN"
        )

        assert ambiguo.tipo == "MOLIDO"
        assert (
            ambiguo.scm_material.categoria_recepcion.codigo
            == "LEGACY_POR_CONFIGURAR"
        )
        assert (
            ambiguo.scm_material.categoria_recepcion.recepcion_habilitada
            is False
        )
        assert virgen.scm_material.codigo != ambiguo.scm_material.codigo


def test_dual_write_colorante_permanece_no_recibible_hasta_us006(app, runner):
    assert runner.invoke(args=["seed-scm-config"]).exit_code == 0

    with app.app_context():
        colorante = create_colorante_with_scm(
            session=db.session,
            nombre="Masterbatch azul nuevo",
        )
        db.session.commit()

        assert colorante.scm_material.codigo.startswith("COL-AUTO-")
        assert colorante.scm_material.clase == "COLORANTE"
        assert (
            colorante.scm_material.categoria_recepcion.codigo
            == "LEGACY_POR_CONFIGURAR"
        )
        assert isinstance(db.session.get(Colorante, colorante.id), Colorante)


def test_dual_write_respeta_rollback_del_llamador(app, runner):
    assert runner.invoke(args=["seed-scm-config"]).exit_code == 0

    with app.app_context():
        materia = create_materia_prima_with_scm(
            session=db.session,
            nombre="PP rollback",
            tipo="SEGUNDA",
        )
        codigo_scm = materia.scm_material.codigo
        db.session.flush()
        db.session.rollback()

        assert MateriaPrima.query.filter_by(nombre="PP rollback").first() is None
        from app.models.scm_catalogos import ScmMaterial

        assert ScmMaterial.query.filter_by(codigo=codigo_scm).first() is None
