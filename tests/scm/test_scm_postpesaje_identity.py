from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services.scm_postpesaje_identity import (
    CATALOG_MODE,
    LEGACY_MODE,
    build_postpesaje_identity,
    validate_stored_identity,
)


def _manga(*, labels=None, sku="PC-0001"):
    piece = SimpleNamespace(
        id=7, codigo="PZ-ASA", nombre="Asa piloto", version=3
    )
    piece_color = SimpleNamespace(
        sku=sku, version=4, pieza_rel=piece
    )
    article_link = SimpleNamespace(
        pieza_color_sku=sku, pieza_color=piece_color
    )
    article = SimpleNamespace(
        id=11,
        public_id=UUID("11111111-1111-1111-1111-111111111111"),
        clase="PIEZA_COLOR",
        version=8,
        pieza_color=article_link,
    )
    lot_article = SimpleNamespace(articulo=article)
    return SimpleNamespace(
        public_id=UUID("22222222-2222-2222-2222-222222222222"),
        lote_articulo=lot_article,
        pieza_color_sku_snapshot=sku,
        articulo_nombre_snapshot="Asa piloto fucsia",
        color_snapshot="FUCSIA C SOLIDO C",
        etiquetas=labels or [],
    )


def test_catalog_identity_captures_piece_and_explicit_provenance():
    manga = _manga()
    work = SimpleNamespace(
        trabajo_color=SimpleNamespace(
            color_id_snapshot=19,
            color_nombre_snapshot="FUCSIA C SOLIDO C",
        )
    )

    identity = build_postpesaje_identity(
        manga, "2026-10-07T16:00:00+00:00", current_work=work
    )

    assert identity["version"] == 1
    assert identity["mode"] == CATALOG_MODE
    assert identity["captured_at"] == "2026-10-07T16:00:00+00:00"
    assert identity["pieza"] == {
        "id": 7,
        "codigo": "PZ-ASA",
        "nombre": "Asa piloto",
        "version": 3,
    }
    assert identity["color"] == {
        "nombre": "FUCSIA C SOLIDO C",
        "id": 19,
        "source": "MANGA_SNAPSHOT",
    }
    assert identity["variante"] == {
        "sku": "PC-0001",
        "nombre": "Asa piloto fucsia",
        "catalog_version": 8,
        "catalog_version_source": "SCM_ARTICULO",
        "source": "MANGA_SNAPSHOT",
    }
    assert identity["provenance"]["piece_source"] == "CATALOG_AT_POST_EMISSION"
    assert identity["provenance"]["pieza_color_version"] == 4


def test_mismatched_sku_is_explicit_legacy_without_name_split():
    manga = _manga(sku="PC-SNAPSHOT")
    manga.lote_articulo.articulo.pieza_color.pieza_color.sku = "PC-CATALOG"
    manga.lote_articulo.articulo.pieza_color.pieza_color.pieza_rel.nombre = (
        "No debe entrar"
    )

    identity = build_postpesaje_identity(manga, "2026-10-07T16:00:00+00:00")

    assert identity["mode"] == LEGACY_MODE
    assert identity["pieza"] is None
    assert identity["variante"]["sku"] == "PC-SNAPSHOT"
    assert identity["variante"]["nombre"] == "Asa piloto fucsia"


def test_color_id_is_omitted_when_work_color_name_differs_from_manga_snapshot():
    manga = _manga()
    work = SimpleNamespace(
        trabajo_color=SimpleNamespace(
            color_id_snapshot=99, color_nombre_snapshot="OTRA VARIANTE"
        )
    )

    identity = build_postpesaje_identity(
        manga, "2026-10-07T16:00:00+00:00", current_work=work
    )

    assert identity["color"] == {
        "nombre": "FUCSIA C SOLIDO C",
        "id": None,
        "source": "MANGA_SNAPSHOT",
    }


def test_correction_reuses_frozen_identity_even_if_catalog_changed():
    original = _manga()
    frozen = build_postpesaje_identity(original, "2026-10-07T15:00:00+00:00")
    previous = SimpleNamespace(
        tipo="POSTPESAJE",
        version=1,
        id=10,
        payload_json={"identidad_producto": frozen},
    )
    corrected = _manga(labels=[previous])
    corrected.lote_articulo.articulo.pieza_color.pieza_color.pieza_rel.nombre = (
        "Nombre catalogo posterior"
    )
    corrected.lote_articulo.articulo.version = 99

    identity = build_postpesaje_identity(
        corrected, "2026-10-07T16:30:00+00:00"
    )

    assert identity == frozen
    assert identity["captured_at"] == "2026-10-07T15:00:00+00:00"


def test_pre_feature_post_correction_stays_legacy_without_catalog_refresh():
    previous = SimpleNamespace(
        tipo="POSTPESAJE", version=1, id=10, payload_json={"color": "AZUL"}
    )
    manga = _manga(labels=[previous])
    manga.lote_articulo.articulo.pieza_color.pieza_color.pieza_rel.nombre = (
        "Catalogo posterior"
    )

    identity = build_postpesaje_identity(manga, "2026-10-07T16:30:00+00:00")

    assert identity["mode"] == LEGACY_MODE
    assert identity["pieza"] is None


def test_only_latest_changed_identity_is_not_rescued_from_an_older_post():
    original = _manga()
    frozen = build_postpesaje_identity(original, "2026-10-07T15:00:00+00:00")
    latest = dict(frozen)
    latest["variante"] = dict(frozen["variante"], sku="PC-CHANGED")
    labels = [
        SimpleNamespace(
            tipo="POSTPESAJE", version=1, id=10,
            payload_json={"identidad_producto": frozen},
        ),
        SimpleNamespace(
            tipo="POSTPESAJE", version=2, id=11,
            payload_json={"identidad_producto": latest},
        ),
    ]
    corrected = _manga(labels=labels)

    identity = build_postpesaje_identity(
        corrected, "2026-10-07T16:30:00+00:00"
    )

    assert identity["mode"] == LEGACY_MODE
    assert identity["pieza"] is None


def test_malformed_v1_identity_is_not_accepted_for_reprint():
    manga = _manga()
    malformed = {
        "version": 1,
        "mode": CATALOG_MODE,
        "captured_at": "2026-10-07T16:00:00+00:00",
        "pieza": {"id": 7},
        "color": {"nombre": manga.color_snapshot, "id": None, "source": "MANGA_SNAPSHOT"},
        "variante": {"sku": manga.pieza_color_sku_snapshot, "nombre": manga.articulo_nombre_snapshot},
        "provenance": {"piece_source": "CATALOG_AT_POST_EMISSION", "manga_id": str(manga.public_id)},
    }

    assert validate_stored_identity(malformed, manga) is False


@pytest.mark.parametrize(
    "field, value",
    [
        ("pieza", {"id": 0, "codigo": "PZ-ASA", "nombre": "Asa", "version": 1}),
        ("pieza", {"id": 7, "codigo": "PZ-ASA", "nombre": 123, "version": 1}),
        ("pieza", {"id": 7, "codigo": "PZ-ASA", "nombre": "Asa", "version": 0}),
    ],
)
def test_catalog_identity_rejects_invalid_piece_scalars(field, value):
    manga = _manga()
    identity = build_postpesaje_identity(manga, "2026-10-07T16:00:00+00:00")
    identity[field] = value

    assert validate_stored_identity(identity, manga) is False


def test_catalog_identity_requires_positive_article_version_and_manga_uuid():
    manga = _manga()
    identity = build_postpesaje_identity(manga, "2026-10-07T16:00:00+00:00")
    identity["variante"]["catalog_version"] = None
    assert validate_stored_identity(identity, manga) is False
    identity = build_postpesaje_identity(manga, "2026-10-07T16:00:00+00:00")
    identity["provenance"]["manga_id"] = None
    assert validate_stored_identity(identity, manga) is False
