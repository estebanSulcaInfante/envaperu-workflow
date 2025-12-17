from app.extensions import db

# Rombo "Se Compone" (Lote <-> Materia Prima)
class SeCompone(db.Model):
    __tablename__ = 'se_compone'

    id = db.Column(db.Integer, primary_key=True)
    lote_id = db.Column(db.Integer, db.ForeignKey('lote_color.id'), nullable=False)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=False)
    
    # INPUT: Participación en la mezcla (0.0 - 1.0)
    fraccion = db.Column(db.Float, nullable=False, default=0.0)

    # CALCULADO: Kilos reales requeridos
    @property
    def peso_kg(self):
        if not self.lote: return 0.0
        # Peso Total del Lote (Base + Extra)
        peso_total = self.lote.peso_total_objetivo + self.lote.extra_kg_asignado
        return peso_total * self.fraccion

    # Relaciones
    materia = db.relationship('MateriaPrima')

# Rombo "Se Colorea" (Lote <-> Colorante)
class SeColorea(db.Model):
    __tablename__ = 'se_colorea'

    id = db.Column(db.Integer, primary_key=True)
    lote_id = db.Column(db.Integer, db.ForeignKey('lote_color.id'), nullable=False)
    colorante_id = db.Column(db.Integer, db.ForeignKey('colorante.id'), nullable=False)
    
    # INPUT: Gramos por bolsa (Dosis)
    gramos = db.Column(db.Float, nullable=False)

    # Relaciones
    pigmento = db.relationship('Colorante')