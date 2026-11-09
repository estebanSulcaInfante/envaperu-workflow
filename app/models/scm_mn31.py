"""Models for the real MN31 direct-to-client dispatch ledger."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Uuid

from app.extensions import db


def utc_now():
    return datetime.now(timezone.utc)


class ScmClienteMn31(db.Model):
    __tablename__ = "scm_cliente_mn31"
    __table_args__ = (
        db.CheckConstraint("length(trim(ruc)) > 0", name="ck_scm_cliente_mn31_ruc"),
        db.CheckConstraint("length(trim(nombre)) > 0", name="ck_scm_cliente_mn31_nombre"),
        db.UniqueConstraint("public_id", name="uq_scm_cliente_mn31_public_id"),
        db.UniqueConstraint("ruc", name="uq_scm_cliente_mn31_ruc"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    public_id = db.Column(Uuid(as_uuid=True), nullable=False, default=uuid.uuid4)
    ruc = db.Column(db.String(20), nullable=False)
    nombre = db.Column(db.String(240), nullable=False)
    contrato = db.Column(db.String(120), nullable=True)
    grupo = db.Column(db.String(80), nullable=True)
    cantidad_fuente = db.Column(db.Numeric(15, 3), nullable=True)
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    def to_dict(self):
        return {
            "id": self.id,
            "public_id": str(self.public_id),
            "ruc": self.ruc,
            "nombre": self.nombre,
            "contrato": self.contrato,
            "grupo": self.grupo,
            "cantidad_fuente": format(self.cantidad_fuente, "f") if self.cantidad_fuente is not None else None,
            "activo": self.activo,
        }


class ScmMn31LoteAsignacion(db.Model):
    __tablename__ = "scm_mn31_lote_asignacion"
    __table_args__ = (
        db.UniqueConstraint("manga_id", name="uq_scm_mn31_lote_manga"),
        db.UniqueConstraint("manga_public_id", name="uq_scm_mn31_lote_manga_public"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    manga_id = db.Column(db.Integer, db.ForeignKey("scm_manga.id", ondelete="RESTRICT"), nullable=False)
    manga_public_id = db.Column(Uuid(as_uuid=True), nullable=False)
    manga_codigo = db.Column(db.String(80), nullable=False)
    lote_public_id = db.Column(Uuid(as_uuid=True), nullable=False)
    lote_codigo = db.Column(db.String(80), nullable=False)
    fuente = db.Column(db.String(80), nullable=False, default="DOCUMENTAL_MN31", server_default="DOCUMENTAL_MN31")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now, server_default=db.func.now())

    manga = db.relationship("ScmManga")

    def to_dict(self):
        return {"id": str(self.lote_public_id), "codigo": self.lote_codigo, "manga_id": str(self.manga_public_id), "manga_codigo": self.manga_codigo, "fuente": self.fuente}
