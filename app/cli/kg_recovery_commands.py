import json
from uuid import UUID

import click

from app.extensions import db
from app.services.scm_kg_recovery_service import recover_kg_pesajes
from app.services.scm_service_support import ScmServiceError


def register_kg_recovery_commands(app):
    @app.cli.command("recover-kg-pesajes")
    @click.option("--actor-id", type=click.IntRange(min=1), required=True)
    @click.option(
        "--article-id",
        "article_ids",
        multiple=True,
        type=click.IntRange(min=1),
        required=True,
    )
    @click.option(
        "--pesaje-id",
        "source_pesaje_ids",
        multiple=True,
        type=click.UUID,
        required=True,
        help="Pesaje final fuente explícito; no se explora todo el historial.",
    )
    @click.option(
        "--source-hash",
        "source_hashes",
        multiple=True,
        help="Hash PUBLIC_ID=SHA256; obligatorio para --apply y opcional en dry-run.",
    )
    @click.option("--reason", required=True)
    @click.option("--operation-id", type=click.UUID, default=None)
    @click.option(
        "--apply",
        is_flag=True,
        help="Aplica solo el manifiesto simple y vigente; por defecto hace dry-run.",
    )
    def recover(actor_id, article_ids, source_pesaje_ids, source_hashes, reason, operation_id, apply):
        """Genera o aplica un manifiesto auditado de recuperación KG009."""
        if apply and operation_id is None:
            raise click.ClickException(
                "--apply requiere --operation-id UUID; conserve la clave para reintentos."
            )
        try:
            parsed_hashes = {}
            for pair in source_hashes:
                if "=" not in pair:
                    raise click.ClickException("--source-hash requiere PUBLIC_ID=SHA256.")
                public_id, digest = pair.split("=", 1)
                try:
                    public_id = str(UUID(public_id.strip()))
                except (TypeError, ValueError, AttributeError) as error:
                    raise click.ClickException(
                        "--source-hash requiere un PUBLIC_ID UUID válido."
                    ) from error
                if not digest.strip():
                    raise click.ClickException(
                        "--source-hash requiere un SHA256 no vacío."
                    )
                parsed_hashes[public_id] = digest.strip()
            result = recover_kg_pesajes(
                db.session,
                actor_id=actor_id,
                article_ids=list(article_ids),
                source_pesaje_ids=list(source_pesaje_ids),
                reason=reason,
                operation_id=operation_id,
                source_snapshot_hashes=parsed_hashes,
                apply=apply,
            )
        except ScmServiceError as error:
            raise click.ClickException(f"{error.code}: {error.message}") from error
        click.echo(json.dumps(result, ensure_ascii=False, sort_keys=True))
