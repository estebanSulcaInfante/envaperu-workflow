"""Human-readable color identity shared by SCM API projections."""


def serialize_color_identity(
    color,
    *,
    color_id=None,
    name_snapshot=None,
):
    """Return an honest, code-free identity for a production color.

    Historical projections may retain an id/name snapshot even when the
    catalog row is unavailable. Missing catalog attributes remain ``None``;
    the API must not fabricate a color code or placeholder name.
    """

    resolved_id = color.id if color is not None else color_id
    base = color.color_base_rel if color is not None else None
    family = color.familia_color_rel if color is not None else None
    catalog_name = (
        " ".join(
            part
            for part in (
                base.nombre if base is not None else None,
                family.nombre if family is not None else None,
            )
            if part
        )
        or None
    )
    resolved_name = name_snapshot if name_snapshot is not None else catalog_name

    if resolved_id is None and resolved_name is None and color is None:
        return None

    return {
        "id": resolved_id,
        "nombre": resolved_name,
        "base": (
            {"id": base.id, "nombre": base.nombre}
            if base is not None else None
        ),
        "familia": (
            {"id": family.id, "nombre": family.nombre}
            if family is not None else None
        ),
        "hex": color.hex_referencia if color is not None else None,
    }
