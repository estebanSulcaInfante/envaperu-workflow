import json
from uuid import UUID

import click

from app.extensions import db
from app.services.scm_kg_pilot_service import prepare_kg_pilot
from app.services.scm_service_support import ScmServiceError


def register_kg_pilot_commands(app):
    @app.cli.command("prepare-kg-pilot")
    @click.option("--actor-id", type=click.IntRange(min=1), required=True)
    @click.option("--article-id", "article_ids", multiple=True, type=click.IntRange(min=1), required=True)
    @click.option("--reason", required=True)
    @click.option("--operation-id", type=click.UUID, default=None)
    @click.option("--apply", is_flag=True, help="Aplica el opt-in auditado; por defecto verifica y revierte.")
    def prepare(actor_id, article_ids, reason, operation_id, apply):
        """Verifica o activa artículos KG explícitos sin convertir existencias UN."""
        if apply and operation_id is None:
            raise click.ClickException("--apply requiere --operation-id UUID; conserve la clave para reintentos.")
        try:
            result = prepare_kg_pilot(db.session, actor_id=actor_id, article_ids=list(article_ids),
                                     reason=reason, operation_id=operation_id, apply=apply)
        except ScmServiceError as error:
            raise click.ClickException(f"{error.code}: {error.message}") from error
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
