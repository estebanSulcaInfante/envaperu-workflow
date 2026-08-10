from app.services.scm_color_identity import serialize_color_identity


def test_color_identity_preserva_snapshot_si_el_maestro_no_esta_disponible():
    assert serialize_color_identity(
        None,
        color_id=37,
        name_snapshot="VERDE SÓLIDO",
    ) == {
        "id": 37,
        "nombre": "VERDE SÓLIDO",
        "base": None,
        "familia": None,
        "hex": None,
    }


def test_color_identity_vacia_no_inventa_placeholder():
    assert serialize_color_identity(None) is None
