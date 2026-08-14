from datetime import date
import json

import click
from flask import current_app
from sqlalchemy import text

from app.extensions import db
from app.services.scm_configuration import ensure_initial_scm_configuration
from app.services.scm_demo_seed_service import (
    LocalDemoSeedError,
    seed_alcancia_pablo_demo,
)
from app.services.scm_uat_walkthrough_seed_service import (
    LocalWalkthroughSeedError,
    seed_uat_walkthrough,
)
from app.services.scm_portfolio_demo_service import (
    PortfolioDemoError,
    prepare_portfolio_demo,
)


def register_scm_commands(app):
    @app.cli.command("prepare-portfolio-demo")
    @click.option(
        "--confirm-portfolio",
        is_flag=True,
        help="Confirma el reinicio total de la base SQLite del portafolio.",
    )
    def prepare_public_portfolio_demo(confirm_portfolio):
        """Reconstruye la demo publica aislada con datos sinteticos."""

        if not confirm_portfolio:
            raise click.ClickException(
                "Use --confirm-portfolio; este comando elimina y recrea "
                "la base SQLite exclusiva de la demo."
            )
        try:
            result = prepare_portfolio_demo(
                database_url=current_app.config["SQLALCHEMY_DATABASE_URI"],
                demo_mode=current_app.config["SCM_DEMO_MODE"],
            )
        except PortfolioDemoError as error:
            raise click.ClickException(str(error)) from error
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))

    @app.cli.command("seed-scm-config")
    def seed_scm_config():
        """Crea catálogos técnicos y roles SCM sin asignar personas."""
        result = ensure_initial_scm_configuration()
        click.echo(
            "SCM_CONFIG_OK "
            f"capacidades={result.capacidades_creadas} "
            f"roles={result.roles_creados} "
            f"categorias={result.categorias_creadas} "
            f"relaciones={result.relaciones_creadas}"
        )

    @app.cli.command("seed-demo-alcancia-pablo")
    @click.option(
        "--confirm-local",
        is_flag=True,
        help="Confirma que el destino es una base UAT local descartable.",
    )
    @click.option(
        "--fecha-operativa",
        type=click.DateTime(formats=["%Y-%m-%d"]),
        default=None,
        help="Fecha de la OT mock en formato AAAA-MM-DD.",
    )
    def seed_demo_alcancia_pablo(confirm_local, fecha_operativa):
        """Crea el escenario local Alcancia Pablo sin datos de pesaje."""
        if not confirm_local:
            raise click.ClickException(
                "Use --confirm-local; este comando solo admite una base UAT local."
            )
        try:
            connection_database = db.session.execute(
                text("SELECT current_database()")
            ).scalar_one()
            migration_revision = db.session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            result = seed_alcancia_pablo_demo(
                db.session,
                database_url=current_app.config["SQLALCHEMY_DATABASE_URI"],
                connection_database=connection_database,
                migration_revision=migration_revision,
                operational_date=(
                    fecha_operativa.date() if fecha_operativa else date.today()
                ),
            )
        except LocalDemoSeedError as error:
            raise click.ClickException(str(error)) from error
        click.echo(json.dumps({
            "status": "SCM_DEMO_ALCANCIA_OK",
            **result,
        }, ensure_ascii=False, sort_keys=True))

    @app.cli.command("seed-uat-recorrido")
    @click.option(
        "--confirm-local",
        is_flag=True,
        help="Confirma que el destino es la base exclusiva local del recorrido.",
    )
    @click.option(
        "--fecha-operativa",
        type=click.DateTime(formats=["%Y-%m-%d"]),
        default=None,
        help="Fecha base del recorrido en formato AAAA-MM-DD.",
    )
    def seed_uat_recorrido(confirm_local, fecha_operativa):
        """Prepara maestros para recorrer MP -> Kardex PT desde la interfaz."""

        if not confirm_local:
            raise click.ClickException(
                "Use --confirm-local; este comando solo admite "
                "enva_uat_recorrido en PostgreSQL loopback."
            )
        try:
            connection_database = db.session.execute(
                text("SELECT current_database()")
            ).scalar_one()
            migration_revision = db.session.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
            result = seed_uat_walkthrough(
                db.session,
                database_url=current_app.config["SQLALCHEMY_DATABASE_URI"],
                connection_database=connection_database,
                migration_revision=migration_revision,
                operational_date=(
                    fecha_operativa.date() if fecha_operativa else date.today()
                ),
            )
        except LocalWalkthroughSeedError as error:
            raise click.ClickException(str(error)) from error
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
