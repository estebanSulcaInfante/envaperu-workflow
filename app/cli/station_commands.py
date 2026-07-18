import click

from app.services.station_auth import (
    StationProvisioningConflict,
    provision_station,
)


def register_station_commands(app):
    @app.cli.command("provision-weighing-station")
    @click.option("--station-id", required=True, help="UUID persistido por la estacion")
    @click.option("--code", required=True, help="Codigo humano visible")
    @click.option("--name", required=True, help="Nombre visible")
    @click.option("--location", required=True, help="Ubicacion logica")
    def provision_weighing_station(station_id, code, name, location):
        """Registra una estacion y muestra su token exactamente una vez."""
        try:
            station, token = provision_station(
                station_id,
                code,
                name,
                location,
            )
        except (ValueError, StationProvisioningConflict) as exc:
            raise click.ClickException(str(exc)) from exc

        click.echo(
            f"STATION_PROVISIONED station_id={station.station_id} "
            f"code={station.codigo}"
        )
        click.echo(f"TOKEN_ONCE={token}")
