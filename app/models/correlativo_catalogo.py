"""Contadores transaccionales para codigos internos de catalogo."""

from app.extensions import db


class CorrelativoCatalogo(db.Model):
    """Siguiente numero disponible para una clase de codigo de catalogo.

    La fila se actualiza dentro de la misma transaccion que crea el recurso.
    PostgreSQL serializa los ``UPDATE`` concurrentes sobre cada clave, por lo
    que dos solicitudes no pueden recibir el mismo numero.
    """

    __tablename__ = "correlativo_catalogo"
    __table_args__ = (
        db.CheckConstraint(
            "clave = upper(trim(clave)) AND length(clave) > 0",
            name="ck_correlativo_catalogo_clave_normalizada",
        ),
        db.CheckConstraint(
            "prefijo = upper(trim(prefijo)) AND length(prefijo) > 0",
            name="ck_correlativo_catalogo_prefijo_normalizado",
        ),
        db.CheckConstraint(
            "siguiente_valor > 0",
            name="ck_correlativo_catalogo_siguiente_positivo",
        ),
        db.CheckConstraint(
            "ancho > 0",
            name="ck_correlativo_catalogo_ancho_positivo",
        ),
        db.UniqueConstraint(
            "prefijo",
            name="uq_correlativo_catalogo_prefijo",
        ),
    )

    clave = db.Column(db.String(32), primary_key=True)
    prefijo = db.Column(db.String(8), nullable=False)
    siguiente_valor = db.Column(db.BigInteger, nullable=False)
    ancho = db.Column(db.SmallInteger, nullable=False, default=6, server_default="6")

    def to_dict(self):
        return {
            "clave": self.clave,
            "prefijo": self.prefijo,
            "siguiente_valor": self.siguiente_valor,
            "ancho": self.ancho,
        }

