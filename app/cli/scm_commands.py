import click

from app.services.scm_configuration import ensure_initial_scm_configuration


def register_scm_commands(app):
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
