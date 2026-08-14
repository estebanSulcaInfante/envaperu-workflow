from datetime import date

import pytest

from app import db
from app.models.scm_inventory import (
    ScmLoteAperturaInventario,
    ScmMovimientoMaterialInventario,
)
from app.models.scm_ot import ScmManga, ScmPesajeManga
from app.models.scm_warehouse import ScmExistenciaManga
from app.services.scm_portfolio_demo_service import (
    PORTFOLIO_DATABASE_FILENAME,
    PortfolioDemoError,
    assert_portfolio_demo_database,
    prepare_portfolio_demo,
)


PORTFOLIO_URL = f"sqlite:///C:/tmp/{PORTFOLIO_DATABASE_FILENAME}"


def test_portfolio_guard_only_accepts_the_dedicated_sqlite_file():
    with pytest.raises(PortfolioDemoError, match="SCM_DEMO_MODE"):
        assert_portfolio_demo_database(PORTFOLIO_URL, demo_mode="")
    with pytest.raises(PortfolioDemoError, match="SQLite"):
        assert_portfolio_demo_database(
            "postgresql://localhost/envaperu", demo_mode="portfolio"
        )
    with pytest.raises(PortfolioDemoError, match=PORTFOLIO_DATABASE_FILENAME):
        assert_portfolio_demo_database(
            "sqlite:///C:/tmp/otro.db", demo_mode="portfolio"
        )


def test_portfolio_seed_rebuilds_a_cross_module_scenario(app):
    with app.app_context():
        result = prepare_portfolio_demo(
            database_url=PORTFOLIO_URL,
            demo_mode="portfolio",
            operational_date=date(2026, 8, 14),
        )

        assert result["status"] == "ready"
        assert result["synthetic_data"] is True
        assert result["hardware_mode"] == "simulated"
        assert result["counts"]["actors"] == 10
        assert result["counts"]["production_orders"] == 1
        assert result["counts"]["operation_orders"] == 2
        assert result["counts"]["mangas"] == 2
        assert result["counts"]["weighings"] == 1
        assert result["counts"]["warehouse_existences"] == 1
        assert result["counts"]["inventory_movements"] == 2
        assert result["manga_states"] == {
            "PREETIQUETADA": 1,
            "RECIBIDA": 1,
        }

        assert ScmLoteAperturaInventario.query.one().estado == "APLICADO"
        assert ScmMovimientoMaterialInventario.query.count() == 1
        assert ScmPesajeManga.query.count() == 1
        assert ScmExistenciaManga.query.one().estado_calidad == "LIBERADA"
        assert {item.estado for item in ScmManga.query.all()} == {
            "PREETIQUETADA",
            "RECIBIDA",
        }

        rebuilt = prepare_portfolio_demo(
            database_url=PORTFOLIO_URL,
            demo_mode="portfolio",
            operational_date=date(2026, 8, 14),
        )
        assert rebuilt["counts"] == result["counts"]
        assert ScmPesajeManga.query.count() == 1


def test_portfolio_cli_requires_destructive_confirmation(app, runner):
    with app.app_context():
        app.config.update({
            "SQLALCHEMY_DATABASE_URI": PORTFOLIO_URL,
            "SCM_DEMO_MODE": "portfolio",
        })
        response = runner.invoke(args=["prepare-portfolio-demo"])
        assert response.exit_code != 0
        assert "--confirm-portfolio" in response.output
