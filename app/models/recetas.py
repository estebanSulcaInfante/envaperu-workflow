from app.extensions import db

# Rombo "Se Compone" (Lote <-> Materia Prima)
class SeCompone(db.Model):
    __tablename__ = 'se_compone'

    id = db.Column(db.Integer, primary_key=True)
    lote_id = db.Column(db.Integer, db.ForeignKey('lote_color.id'), nullable=False)
    materia_prima_id = db.Column(db.Integer, db.ForeignKey('materia_prima.id'), nullable=False)
    
    # INPUT: Participación en la mezcla (0.0 - 1.0)
    fraccion = db.Column(db.Float, nullable=False, default=0.0)

    # INPUT: Participación en la mezcla (0.0 - 1.0)
    fraccion = db.Column(db.Float, nullable=False, default=0.0)

    # PERSISTENCIA
    calculo_peso_kg = db.Column(db.Float, default=0.0)

    def actualizar_metricas(self, contexto_lote=None):
        """
        Calcula kilos reales requeridos usando meta_kg del lote,
        incluyendo la merma de colada de la orden padre.
        """
        lote_padre = contexto_lote or self.lote
        if not lote_padre:
            return

        meta_kg = lote_padre.meta_kg or 0.0

        merma_pct = 0.0
        orden = lote_padre.orden
        if orden:
            merma_pct = orden.calculo_merma_pct or 0.0

        peso_total = meta_kg * (1 + merma_pct)
        self.calculo_peso_kg = peso_total * self.fraccion

    @property
    def peso_kg(self):
        return self.calculo_peso_kg or 0.0

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