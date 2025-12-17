from app.extensions import db

class MateriaPrima(db.Model):
    __tablename__ = 'materia_prima'

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(50)) # 'VIRGEN', 'SEGUNDA'
    # Eliminado: stock_kg

    def __repr__(self):
        return f'<MP {self.nombre}>'

class Colorante(db.Model):
    __tablename__ = 'colorante'

    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(100), nullable=False)
    # Eliminado: stock_gr

    def __repr__(self):
        return f'<Colorante {self.nombre}>'