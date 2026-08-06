"""Generacion transaccional de codigos internos de catalogo.

El maximo codigo existente se consulta solamente durante la migracion que
inicializa los contadores. Este servicio nunca usa ``MAX + 1``.
"""

from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.extensions import db
from app.models.correlativo_catalogo import CorrelativoCatalogo


@dataclass(frozen=True)
class CatalogCodeSpec:
    prefix: str
    width: int = 6


CATALOG_CODE_SPECS = MappingProxyType({
    "PIEZA": CatalogCodeSpec("PZ"),
    "PIEZA_COLOR": CatalogCodeSpec("PC"),
    "PRODUCTO_TERMINADO": CatalogCodeSpec("PT"),
    "MOLDE": CatalogCodeSpec("ML"),
    "MATERIA_PRIMA": CatalogCodeSpec("MP"),
    "COLORANTE": CatalogCodeSpec("COL"),
    "ADITIVO": CatalogCodeSpec("ADT"),
    "PROVEEDOR": CatalogCodeSpec("PRV"),
    "CATEGORIA_RECEPCION": CatalogCodeSpec("CAT"),
    "TRABAJADOR": CatalogCodeSpec("TRB"),
    "MAQUINA": CatalogCodeSpec("MAQ"),
    "TIPO_MAQUINA": CatalogCodeSpec("TMQ"),
    "LINEA": CatalogCodeSpec("LIN"),
    "FAMILIA": CatalogCodeSpec("FAM"),
    "FAMILIA_COLOR": CatalogCodeSpec("FC"),
    "SUBENSAMBLE_WIP": CatalogCodeSpec("WIP"),
    "CENTRO_TRABAJO": CatalogCodeSpec("CT"),
    "TIPO_MANGA": CatalogCodeSpec("TMG"),
    "TIPO_CONTENEDOR": CatalogCodeSpec("TCO"),
    "PERFIL_EMPAQUE": CatalogCodeSpec("PEM"),
    "PRESENTACION_COMERCIAL": CatalogCodeSpec("PRE"),
    "ORDEN_TRABAJO": CatalogCodeSpec("OT"),
    "ORDEN_PRODUCCION": CatalogCodeSpec("OP"),
    "ORDEN_FABRICACION": CatalogCodeSpec("OF"),
    "ORDEN_ENSAMBLE": CatalogCodeSpec("OE"),
    "SOLICITUD_ABASTECIMIENTO": CatalogCodeSpec("SA"),
})


class CatalogCodeKeyError(ValueError):
    """La clave solicitada no pertenece al catalogo de correlativos."""


class UnsupportedCatalogCodeDialect(RuntimeError):
    """El motor no ofrece la operacion atomica requerida por el generador."""


def _normalize_key(key):
    normalized = str(key or "").strip().upper()
    if normalized not in CATALOG_CODE_SPECS:
        allowed = ", ".join(CATALOG_CODE_SPECS)
        raise CatalogCodeKeyError(
            f"Clave de correlativo desconocida: {key!r}. Permitidas: {allowed}"
        )
    return normalized


def _upsert_counter(session, key, spec, dialect_name):
    values = {
        "clave": key,
        "prefijo": spec.prefix,
        "siguiente_valor": 1,
        "ancho": spec.width,
    }
    if dialect_name == "postgresql":
        statement = postgresql_insert(CorrelativoCatalogo).values(**values)
    elif dialect_name == "sqlite":
        statement = sqlite_insert(CorrelativoCatalogo).values(**values)
    else:
        raise UnsupportedCatalogCodeDialect(
            f"Dialect no soportado para correlativos atomicos: {dialect_name}"
        )

    session.execute(
        statement.on_conflict_do_nothing(index_elements=["clave"])
    )


def generar_codigo_catalogo(clave, *, session=None):
    """Reserva y devuelve el siguiente codigo para ``clave``.

    Contrato publico::

        generar_codigo_catalogo(clave: str, *, session=None) -> str

    ``clave`` debe pertenecer a ``CATALOG_CODE_SPECS`` (sin distinguir
    mayusculas y espacios exteriores). ``session`` puede ser una
    ``sqlalchemy.orm.Session``; si se omite se usa ``app.extensions.db.session``.

    La funcion hace un upsert idempotente de la fila y un ``UPDATE ...
    RETURNING`` atomico. No hace ``commit``: el contador participa en la
    transaccion del recurso que recibira el codigo. Si esa transaccion se
    revierte, tambien se libera la reserva.
    """

    key = _normalize_key(clave)
    spec = CATALOG_CODE_SPECS[key]
    active_session = session if session is not None else db.session
    dialect_name = active_session.get_bind().dialect.name

    _upsert_counter(active_session, key, spec, dialect_name)
    row = active_session.execute(
        update(CorrelativoCatalogo)
        .where(CorrelativoCatalogo.clave == key)
        .values(
            siguiente_valor=CorrelativoCatalogo.siguiente_valor + 1,
        )
        .returning(
            CorrelativoCatalogo.prefijo,
            CorrelativoCatalogo.siguiente_valor,
            CorrelativoCatalogo.ancho,
        )
    ).one()

    assigned_value = int(row.siguiente_valor) - 1
    return f"{row.prefijo}-{assigned_value:0{int(row.ancho)}d}"


def generar_numero_catalogo(clave, *, session=None):
    """Reserva un correlativo y devuelve solo su componente numerico.

    Es el puente de compatibilidad para catalogos legacy cuyo ``codigo`` aun
    es entero. La API puede exponer el prefijo descriptivo sin cambiar sus
    claves foraneas ni el tipo persistido.
    """

    codigo = generar_codigo_catalogo(clave, session=session)
    return int(codigo.rsplit("-", 1)[1])
