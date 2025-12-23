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
        Calcula kilos reales requeridos usando el peso total del lote.
        """
        lote_padre = contexto_lote or self.lote
        if not lote_padre:
            return

        # Peso Total del Lote (Base + Extra)
        peso_base_mas_extra = lote_padre.calculo_peso_base + lote_padre.calculo_extra_kg
        
        # Ajuste Discrepancia F28: Excel suma "Merma a Recuperar" al total de materiales
        # Pero ojo: el lote NO tiene merma_pct directa, hay que sacarla del padre.
        merma_pct = 0.0
        # Intentamos obtener contexto del padre del lote si es posible, o usamos relaciones
        orden = lote_padre.orden # Ojo: aqui podria ser lazy load si no vieine en contexto
        if orden:
             merma_pct = orden.calculo_merma_pct or 0.0

        peso_total = peso_base_mas_extra * (1 + merma_pct)
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