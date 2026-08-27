"""Modelos de moldes, piezas abstractas y su composición física."""

from app.extensions import db


class Molde(db.Model):
    """Molde físico de inyección con una composición N:M de piezas."""

    __tablename__ = "molde"

    codigo = db.Column(db.String(50), primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    peso_tiro_gr = db.Column(db.Float, nullable=False)
    tiempo_ciclo_std = db.Column(db.Float, default=30.0)
    activo = db.Column(db.Boolean, default=True)
    notas = db.Column(db.Text, nullable=True)

    # Se conserva el nombre ``piezas`` en el API interno para minimizar el
    # impacto del refactor. Cada elemento es una composición MoldePieza, no el
    # maestro Pieza directamente.
    piezas = db.relationship(
        "MoldePieza",
        back_populates="molde",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="MoldePieza.id",
    )

    @property
    def peso_neto_gr(self):
        """Peso neto del golpe: suma de cavidades × peso de la pieza."""
        return sum(
            item.peso_unitario_gr * item.cavidades
            for item in self.piezas
            if item.activo
        )

    @property
    def peso_colada_gr(self):
        return self.peso_tiro_gr - self.peso_neto_gr

    @property
    def cavidades_totales(self):
        return sum(item.cavidades for item in self.piezas if item.activo)

    @property
    def merma_pct(self):
        if self.peso_tiro_gr and self.peso_tiro_gr > 0:
            return (self.peso_tiro_gr - self.peso_neto_gr) / self.peso_tiro_gr
        return 0.0

    def to_dict(self, include_variantes=False):
        return {
            "codigo": self.codigo,
            "nombre": self.nombre,
            "peso_tiro_gr": self.peso_tiro_gr,
            "tiempo_ciclo_std": self.tiempo_ciclo_std,
            "activo": self.activo,
            "notas": self.notas,
            "peso_neto_gr": self.peso_neto_gr,
            "peso_colada_gr": self.peso_colada_gr,
            "cavidades_totales": self.cavidades_totales,
            "merma_pct": self.merma_pct,
            "formas": [
                item.to_dict(include_variantes=include_variantes)
                for item in self.piezas
                if item.activo
            ],
        }

    def __repr__(self):
        return f"<Molde {self.codigo}: {self.nombre}>"


class Pieza(db.Model):
    """Maestro global de una pieza/forma, independiente del molde y color."""

    __tablename__ = "pieza"
    __table_args__ = (
        db.CheckConstraint(
            "codigo = upper(trim(codigo)) AND length(codigo) > 0",
            name="ck_pieza_codigo_normalizado",
        ),
        db.CheckConstraint("version > 0", name="ck_pieza_version"),
        db.UniqueConstraint("codigo", name="uq_pieza_codigo"),
    )

    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(64), nullable=False)
    nombre = db.Column(db.String(200), nullable=False)
    linea_id = db.Column(db.Integer, db.ForeignKey("linea.id"), nullable=True)
    familia_id = db.Column(db.Integer, db.ForeignKey("familia.id"), nullable=True)
    # Especificación nominal del maestro. El valor operativo por molde vive en
    # MoldePieza para admitir herramientas alternativas con pequeñas variaciones.
    peso_nominal_gr = db.Column(db.Float, nullable=False)
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    molde_piezas = db.relationship(
        "MoldePieza",
        back_populates="pieza",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="MoldePieza.id",
    )

    def to_dict(
        self,
        include_variantes=False,
        include_moldes=True,
        include_inactive_variantes=False,
    ):
        variantes = list(self.variantes) if hasattr(self, "variantes") else []
        variantes_visibles = (
            variantes
            if include_inactive_variantes
            else [item for item in variantes if item.activo]
        )
        data = {
            "id": self.id,
            "codigo": self.codigo,
            "nombre": self.nombre,
            "linea_id": self.linea_id,
            "familia_id": self.familia_id,
            "peso_nominal_gr": self.peso_nominal_gr,
            "activo": self.activo,
            "version": self.version,
            "variantes_count": len(variantes_visibles),
        }
        if include_moldes:
            data["moldes"] = [
                item.to_summary_dict() for item in self.molde_piezas if item.activo
            ]
        if include_variantes:
            data["variantes"] = [item.to_dict() for item in variantes_visibles]
        return data

    def __repr__(self):
        return f"<Pieza {self.codigo}: {self.nombre}>"


class MoldePieza(db.Model):
    """Composición física: cuántas cavidades de una pieza tiene un molde."""

    __tablename__ = "molde_pieza"
    __table_args__ = (
        db.CheckConstraint(
            "cavidades > 0",
            name="ck_molde_pieza_cavidades_positivas",
        ),
        db.CheckConstraint(
            "peso_unitario_gr > 0",
            name="ck_molde_pieza_peso_positivo",
        ),
        db.CheckConstraint("version > 0", name="ck_molde_pieza_version"),
        db.UniqueConstraint(
            "molde_id",
            "pieza_id",
            name="uq_molde_pieza_molde_pieza",
        ),
        db.Index("ix_molde_pieza_pieza_id", "pieza_id"),
    )

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    molde_id = db.Column(
        db.String(50),
        db.ForeignKey("molde.codigo", ondelete="RESTRICT"),
        nullable=False,
    )
    pieza_id = db.Column(
        db.Integer,
        db.ForeignKey("pieza.id", ondelete="RESTRICT"),
        nullable=False,
    )
    cavidades = db.Column(db.Integer, nullable=False, default=1)
    peso_unitario_gr = db.Column(db.Float, nullable=False)
    activo = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    version = db.Column(db.Integer, nullable=False, default=1, server_default="1")

    molde = db.relationship("Molde", back_populates="piezas")
    pieza = db.relationship("Pieza", back_populates="molde_piezas")

    @property
    def nombre(self):
        return self.pieza.nombre

    @property
    def linea_id(self):
        return self.pieza.linea_id

    @property
    def familia_id(self):
        return self.pieza.familia_id

    @property
    def variantes(self):
        return self.pieza.variantes if hasattr(self.pieza, "variantes") else []

    @property
    def peso_total_gr(self):
        return self.peso_unitario_gr * self.cavidades

    def to_summary_dict(self):
        return {
            "composicion_id": self.id,
            "molde_pieza_id": self.id,
            "molde_id": self.molde_id,
            "molde_nombre": self.molde.nombre if self.molde else None,
            "pieza_id": self.pieza_id,
            "pieza_codigo": self.pieza.codigo if self.pieza else None,
            "pieza_nombre": self.pieza.nombre if self.pieza else None,
            "cavidades": self.cavidades,
            "peso_unitario_gr": self.peso_unitario_gr,
            "activo": self.activo,
            "version": self.version,
        }

    def to_dict(self, include_variantes=False):
        data = {
            # ``id`` identifica la composición para editar/desvincularla.
            "id": self.id,
            "molde_pieza_id": self.id,
            "pieza_id": self.pieza_id,
            "pieza_codigo": self.pieza.codigo,
            "molde_id": self.molde_id,
            "nombre": self.nombre,
            "linea_id": self.linea_id,
            "familia_id": self.familia_id,
            "cavidades": self.cavidades,
            "peso_unitario_gr": self.peso_unitario_gr,
            "peso_nominal_gr": self.pieza.peso_nominal_gr,
            "activo": self.activo,
            "version": self.version,
            "peso_total_gr": self.peso_total_gr,
            "variantes_count": len(self.variantes),
        }
        if include_variantes:
            data["variantes"] = [item.to_dict() for item in self.variantes]
        return data

    def __repr__(self):
        return (
            f"<MoldePieza {self.molde_id}/{self.pieza_id}: "
            f"{self.cavidades} cav>"
        )
