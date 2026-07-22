from app.extensions import db

class MateriaPrima(db.Model):
    __tablename__ = 'materia_prima'
    __table_args__ = (
        db.UniqueConstraint(
            'scm_material_id',
            name='uq_materia_prima_scm_material_id',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(50)) # 'VIRGEN', 'SEGUNDA'
    scm_material_id = db.Column(
        db.Integer,
        db.ForeignKey(
            'scm_material.id',
            name='fk_materia_prima_scm_material',
            ondelete='RESTRICT',
        ),
        nullable=False,
    )
    scm_material = db.relationship(
        'ScmMaterial',
        back_populates='materia_prima',
        uselist=False,
    )
    # Eliminado: stock_kg

    def __repr__(self):
        return f'<MP {self.nombre}>'

class Colorante(db.Model):
    __tablename__ = 'colorante'
    __table_args__ = (
        db.CheckConstraint(
            "tipo IN ('COLORANTE', 'ADITIVO')",
            name='ck_colorante_tipo',
        ),
        db.UniqueConstraint(
            'scm_material_id',
            name='uq_colorante_scm_material_id',
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    tipo = db.Column(
        db.String(20),
        nullable=False,
        default='COLORANTE',
        server_default='COLORANTE',
    )
    scm_material_id = db.Column(
        db.Integer,
        db.ForeignKey(
            'scm_material.id',
            name='fk_colorante_scm_material',
            ondelete='RESTRICT',
        ),
        nullable=False,
    )
    scm_material = db.relationship(
        'ScmMaterial',
        back_populates='colorante',
        uselist=False,
    )
    # Eliminado: stock_gr

    def __repr__(self):
        return f'<Colorante {self.nombre}>'
