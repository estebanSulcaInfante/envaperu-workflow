"""Frozen product identity captured in a newly emitted POSTPESAJE label.

The legacy ``pieza_color`` and ``color`` payload fields remain strings for
existing consumers.  This module only builds the additive v1 object.  It is
deliberately independent from the reprint service: reprints must consume the
stored object and never resolve the live catalog again.
"""

from copy import deepcopy
import uuid


IDENTITY_VERSION = 1
CATALOG_MODE = "CATALOG_SNAPSHOT"
LEGACY_MODE = "LEGACY_MANGA_SNAPSHOT"


def _public_id(value):
    return str(value) if value is not None else None


def _text(value, *, required=True):
    if not isinstance(value, str):
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return False
    return bool(value) if required else True


def _positive_int(value, *, required=True):
    if value is None and not required:
        return True
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )


def _uuid_text(value, *, required=True):
    if value is None and not required:
        return True
    if not _text(value):
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def _work_color_snapshot(manga, current_work):
    work_color = getattr(current_work, "trabajo_color", None)
    if work_color is None:
        return None
    snapshot_name = getattr(work_color, "color_nombre_snapshot", None)
    manga_name = getattr(manga, "color_snapshot", None)
    if snapshot_name is None or manga_name is None or snapshot_name != manga_name:
        return None
    return getattr(work_color, "color_id_snapshot", None)


def _legacy_identity(manga, generated_at, current_work, *, article=None, reason=None):
    article_id = getattr(article, "id", None) if article is not None else None
    article_public_id = (
        _public_id(getattr(article, "public_id", None))
        if article is not None
        else None
    )
    manga_id = _public_id(getattr(manga, "public_id", None))
    provenance = {
        "piece_source": LEGACY_MODE,
        "manga_id": manga_id,
        "articulo_id": article_id,
        "articulo_public_id": article_public_id,
        "pieza_color_sku_snapshot": (
            getattr(manga, "pieza_color_sku_snapshot", None)
        ),
    }
    if reason is not None:
        provenance["reason"] = reason
    return {
        "version": IDENTITY_VERSION,
        "mode": LEGACY_MODE,
        "captured_at": generated_at,
        "pieza": None,
        "color": {
            "nombre": getattr(manga, "color_snapshot", None),
            "id": _work_color_snapshot(manga, current_work),
            "source": "MANGA_SNAPSHOT",
        },
        "variante": {
            "sku": getattr(manga, "pieza_color_sku_snapshot", None),
            "nombre": getattr(manga, "articulo_nombre_snapshot", None),
            "catalog_version": None,
            "catalog_version_source": "SCM_ARTICULO",
            "source": "MANGA_SNAPSHOT",
        },
        "provenance": provenance,
    }


def _identity_matches_snapshot(identity, manga):
    if (
        not isinstance(identity, dict)
        or identity.get("version") != IDENTITY_VERSION
        or not isinstance(identity.get("captured_at"), str)
        or not identity.get("captured_at")
    ):
        return False
    mode = identity.get("mode")
    if mode not in {CATALOG_MODE, LEGACY_MODE}:
        return False
    piece = identity.get("pieza")
    if mode == CATALOG_MODE:
        if (
            not isinstance(piece, dict)
            or not _positive_int(piece.get("id"))
            or not _text(piece.get("codigo"))
            or not _text(piece.get("nombre"))
            or not _positive_int(piece.get("version"))
        ):
            return False
    elif piece is not None:
        return False
    variant = identity.get("variante")
    if (
        not isinstance(variant, dict)
        or any(key not in variant for key in (
            "sku", "nombre", "catalog_version", "catalog_version_source", "source"
        ))
        or variant.get("catalog_version_source") != "SCM_ARTICULO"
        or variant.get("source") != "MANGA_SNAPSHOT"
        or not _text(variant.get("nombre"))
    ):
        return False
    snapshot_sku = getattr(manga, "pieza_color_sku_snapshot", None)
    if mode == CATALOG_MODE and not _text(variant.get("sku")):
        return False
    if mode == LEGACY_MODE and variant.get("sku") is not None and not _text(variant.get("sku")):
        return False
    if variant.get("sku") != snapshot_sku:
        return False
    if variant.get("nombre") != getattr(manga, "articulo_nombre_snapshot", None):
        return False
    if mode == CATALOG_MODE and not _positive_int(variant.get("catalog_version")):
        return False
    if mode == LEGACY_MODE and variant.get("catalog_version") is not None:
        return False
    color = identity.get("color")
    if (
        not isinstance(color, dict)
        or any(key not in color for key in ("nombre", "id", "source"))
        or color.get("source") != "MANGA_SNAPSHOT"
        or not _text(color.get("nombre"), required=False)
        or not _positive_int(color.get("id"), required=False)
    ):
        return False
    if color.get("nombre") != getattr(manga, "color_snapshot", None):
        return False
    provenance = identity.get("provenance")
    if (
        not isinstance(provenance, dict)
        or any(key not in provenance for key in (
            "piece_source", "manga_id", "articulo_id", "articulo_public_id",
            "pieza_color_sku_snapshot"
        ))
        or provenance.get("piece_source")
        not in {CATALOG_MODE.replace("_SNAPSHOT", "_AT_POST_EMISSION"), LEGACY_MODE}
    ):
        return False
    if mode == CATALOG_MODE and provenance.get("piece_source") != "CATALOG_AT_POST_EMISSION":
        return False
    if mode == LEGACY_MODE and provenance.get("piece_source") != LEGACY_MODE:
        return False
    if not _uuid_text(provenance.get("manga_id")):
        return False
    if mode == CATALOG_MODE:
        if (
            not _positive_int(provenance.get("articulo_id"))
            or not _uuid_text(provenance.get("articulo_public_id"))
            or not _positive_int(provenance.get("articulo_version"))
            or not _text(provenance.get("pieza_color_sku"))
            or not _positive_int(provenance.get("pieza_color_version"))
            or not _positive_int(provenance.get("color_id_snapshot"), required=False)
        ):
            return False
    elif provenance.get("articulo_id") is not None and not _positive_int(provenance.get("articulo_id")):
        return False
    if provenance.get("articulo_public_id") is not None and not _uuid_text(provenance.get("articulo_public_id")):
        return False
    if provenance.get("pieza_color_sku_snapshot") is not None and not _text(provenance.get("pieza_color_sku_snapshot")):
        return False
    if mode == CATALOG_MODE and any(
        key not in provenance
        for key in (
            "articulo_version", "pieza_color_sku", "pieza_color_version",
            "color_id_snapshot",
        )
    ):
        return False
    manga_id = _public_id(getattr(manga, "public_id", None))
    if provenance.get("manga_id") not in {None, manga_id}:
        return False
    if provenance.get("pieza_color_sku_snapshot") not in {None, snapshot_sku}:
        return False
    if provenance.get("pieza_color_sku") not in {None, snapshot_sku}:
        return False
    return True


def _stored_identity(manga):
    """Return only the latest prior identity and whether a POST exists.

    A prior POST without a matching v1 identity must not cause a correction to
    refresh the current catalog.  The caller turns that case into legacy mode.
    """
    labels = [
        label for label in (getattr(manga, "etiquetas", None) or [])
        if getattr(label, "tipo", None) == "POSTPESAJE"
    ]
    labels.sort(
        key=lambda label: (
            getattr(label, "version", 0), getattr(label, "id", 0)
        )
    )
    if not labels:
        return None, False
    payload = getattr(labels[-1], "payload_json", None)
    identity = payload.get("identidad_producto") if isinstance(payload, dict) else None
    if _identity_matches_snapshot(identity, manga):
        return deepcopy(identity), True
    return None, True


def build_postpesaje_identity(manga, generated_at, *, current_work=None):
    """Build the immutable product identity for a new POSTPESAJE payload.

    A catalog snapshot is accepted only when the canonical SCM article points
    to the same SKU frozen on the manga and the SKU has a normalized master
    ``Pieza``.  Every other case is explicit legacy mode; no name/SKU is
    fabricated by splitting display strings.
    """
    stored, has_prior_post = _stored_identity(manga)
    if has_prior_post:
        return (
            stored
            if stored is not None
            else _legacy_identity(
                manga,
                generated_at,
                current_work,
                article=(
                    getattr(getattr(manga, "lote_articulo", None), "articulo", None)
                ),
                reason="SOURCE_IDENTITY_CHANGED",
            )
        )

    lot_article = getattr(manga, "lote_articulo", None)
    article = getattr(lot_article, "articulo", None)
    article_link = getattr(article, "pieza_color", None)
    piece_color = getattr(article_link, "pieza_color", None)
    snapshot_sku = getattr(manga, "pieza_color_sku_snapshot", None)
    linked_sku = getattr(article_link, "pieza_color_sku", None)
    piece = getattr(piece_color, "pieza_rel", None)
    secure_catalog = bool(
        article is not None
        and getattr(article, "clase", None) == "PIEZA_COLOR"
        and article_link is not None
        and piece_color is not None
        and snapshot_sku is not None
        and linked_sku == snapshot_sku
        and getattr(piece_color, "sku", None) == snapshot_sku
        and piece is not None
    )
    if not secure_catalog:
        return _legacy_identity(
            manga, generated_at, current_work, article=article
        )

    manga_id = _public_id(getattr(manga, "public_id", None))
    article_id = getattr(article, "id", None)
    article_public_id = _public_id(getattr(article, "public_id", None))
    return {
        "version": IDENTITY_VERSION,
        "mode": CATALOG_MODE,
        "captured_at": generated_at,
        "pieza": {
            "id": piece.id,
            "codigo": piece.codigo,
            "nombre": piece.nombre,
            "version": piece.version,
        },
        "color": {
            "nombre": getattr(manga, "color_snapshot", None),
            "id": _work_color_snapshot(manga, current_work),
            "source": "MANGA_SNAPSHOT",
        },
        "variante": {
            "sku": snapshot_sku,
            "nombre": getattr(manga, "articulo_nombre_snapshot", None),
            "catalog_version": getattr(article, "version", None),
            "catalog_version_source": "SCM_ARTICULO",
            "source": "MANGA_SNAPSHOT",
        },
        "provenance": {
            "piece_source": "CATALOG_AT_POST_EMISSION",
            "manga_id": manga_id,
            "articulo_id": article_id,
            "articulo_public_id": article_public_id,
            "articulo_version": getattr(article, "version", None),
            "pieza_color_sku": snapshot_sku,
            "pieza_color_sku_snapshot": snapshot_sku,
            "pieza_color_version": getattr(piece_color, "version", None),
            "color_id_snapshot": _work_color_snapshot(manga, current_work),
        },
    }


def validate_stored_identity(identity, manga):
    """Return ``True`` only for a frozen identity bound to manga snapshots.

    This intentionally does not query catalog names or versions.  Reprint
    validation is against the immutable source snapshot only.
    """
    return identity is None or _identity_matches_snapshot(identity, manga)
