import os
import sys

# Agregar el directorio raíz al path para poder importar módulos de la app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from app.extensions import db
from app.models.maquina import Maquina, TipoMaquina
from app.models.trabajador import Trabajador, RolOperativo
from app.models.registro import RegistroDiarioProduccion, DetalleProduccionHora
from app.models.orden import OrdenProduccion
from sqlalchemy import text

app = create_app()

def run_migration():
    with app.app_context():
        print("Iniciando migración de Trabajadores y Máquinas (US-009)...")

        # ---------------------------------------------------------
        # FASE 1: CREAR TABLAS NUEVAS
        # ---------------------------------------------------------
        print("1. Creando tablas nuevas (trabajador, rol_operativo, trabajador_rol, tipo_maquina)...")
        db.create_all()

        # ---------------------------------------------------------
        # FASE 2: ALTER TABLE EN TABLAS EXISTENTES
        # ---------------------------------------------------------
        print("2. Añadiendo columnas a tablas existentes...")
        
        # Maquina
        try:
            db.session.execute(text("ALTER TABLE maquina ADD COLUMN codigo VARCHAR(20) UNIQUE"))
            db.session.execute(text("ALTER TABLE maquina ADD COLUMN tipo_maquina_id INTEGER"))
            db.session.execute(text("ALTER TABLE maquina ADD COLUMN estado VARCHAR(50) DEFAULT 'OPERATIVA'"))
            db.session.execute(text("ALTER TABLE maquina ADD COLUMN activo BOOLEAN DEFAULT TRUE"))
            db.session.execute(text("ALTER TABLE maquina ADD COLUMN numero_serie VARCHAR(100)"))
            db.session.execute(text("ALTER TABLE maquina ADD COLUMN observaciones TEXT"))
            db.session.commit()
            print("  - Columnas añadidas a Maquina.")
        except Exception as e:
            db.session.rollback()
            print(f"  - (Maquina) Las columnas ya existían o error: {e}")

        # DetalleProduccionHora
        try:
            db.session.execute(text("ALTER TABLE detalle_produccion_hora ADD COLUMN trabajador_id INTEGER REFERENCES trabajador(id)"))
            # Renombramos 'maquinista' a 'maquinista_snapshot' en SQLite (que es tricky) o PostgreSQL.
            # En SQLite no soporta RENAME COLUMN directamente en todas las versiones, crearemos una nueva
            db.session.execute(text("ALTER TABLE detalle_produccion_hora ADD COLUMN maquinista_snapshot VARCHAR(100)"))
            # Copiar datos
            db.session.execute(text("UPDATE detalle_produccion_hora SET maquinista_snapshot = maquinista"))
            db.session.commit()
            print("  - Columnas añadidas a DetalleProduccionHora.")
        except Exception as e:
            db.session.rollback()
            print(f"  - (DetalleProduccionHora) Error o ya migradas: {e}")

        # RegistroDiarioProduccion
        try:
            db.session.execute(text("ALTER TABLE registro_diario_produccion ADD COLUMN maquina_codigo_snapshot VARCHAR(20)"))
            db.session.execute(text("ALTER TABLE registro_diario_produccion ADD COLUMN maquina_nombre_snapshot VARCHAR(100)"))
            db.session.commit()
            print("  - Columnas añadidas a RegistroDiarioProduccion.")
        except Exception as e:
            db.session.rollback()
            print(f"  - (RegistroDiarioProduccion) Error o ya migradas: {e}")
            
        # OrdenProduccion
        try:
            db.session.execute(text("ALTER TABLE orden_produccion ADD COLUMN maquina_codigo_snapshot VARCHAR(20)"))
            db.session.execute(text("ALTER TABLE orden_produccion ADD COLUMN maquina_nombre_snapshot VARCHAR(100)"))
            db.session.commit()
            print("  - Columnas añadidas a OrdenProduccion.")
        except Exception as e:
            db.session.rollback()
            print(f"  - (OrdenProduccion) Error o ya migradas: {e}")

        # ---------------------------------------------------------
        # FASE 3: DATOS MAESTROS BASE
        # ---------------------------------------------------------
        print("3. Insertando Roles Operativos y Tipos de Máquina...")
        roles_base = [
            ('MAQUINISTA', 'Maquinista'),
            ('OPERADOR_PESAJE', 'Operador de Pesaje'),
            ('MEZCLADOR', 'Mezclador'),
            ('AYUDANTE', 'Ayudante'),
            ('SUPERVISOR', 'Supervisor')
        ]
        roles_dict = {}
        for cod, nom in roles_base:
            rol = RolOperativo.query.filter_by(codigo=cod).first()
            if not rol:
                rol = RolOperativo(codigo=cod, nombre=nom)
                db.session.add(rol)
                db.session.flush()
            roles_dict[cod] = rol

        db.session.commit()

        # Tipos de Maquina
        # Vamos a leer los tipos actuales que hay en Maquina
        maquinas = Maquina.query.all()
        tipos_nombres = list(set([m.tipo for m in maquinas if m.tipo]))
        
        # Crear los tipos
        for idx, t_nom in enumerate(tipos_nombres):
            codigo = f"TM-{idx+1:02d}"
            t_obj = TipoMaquina.query.filter_by(nombre=t_nom).first()
            if not t_obj:
                t_obj = TipoMaquina(codigo=codigo, nombre=t_nom, proceso='INYECCION')
                db.session.add(t_obj)
        
        # Tipo Generico fallback
        tm_gen = TipoMaquina.query.filter_by(codigo='TM-GEN').first()
        if not tm_gen:
            tm_gen = TipoMaquina(codigo='TM-GEN', nombre='Generico', proceso='OTRO')
            db.session.add(tm_gen)

        db.session.commit()

        # Actualizar maquinas
        print("4. Normalizando Máquinas...")
        for i, m in enumerate(maquinas):
            if not m.codigo:
                m.codigo = f"M-{i+1:03d}"
            if not m.tipo_maquina_id:
                if m.tipo:
                    tm = TipoMaquina.query.filter_by(nombre=m.tipo).first()
                    m.tipo_maquina_id = tm.id if tm else tm_gen.id
                else:
                    m.tipo_maquina_id = tm_gen.id
        db.session.commit()

        # ---------------------------------------------------------
        # FASE 4: MIGRACIÓN DE DATOS TEXTUALES A SNAPSHOTS / MAESTROS
        # ---------------------------------------------------------
        print("5. Analizando trabajadores desde histórico...")
        
        # Leer todos los nombres únicos de maquinista_snapshot (antiguo maquinista)
        res = db.session.execute(text("SELECT DISTINCT maquinista_snapshot FROM detalle_produccion_hora WHERE maquinista_snapshot IS NOT NULL AND maquinista_snapshot != ''")).fetchall()
        
        nombres_unicos = [r[0].strip() for r in res if r[0] and r[0].strip()]
        nombres_unicos = list(set(nombres_unicos))
        
        # Crear trabajadores base
        for i, nom in enumerate(nombres_unicos):
            cod = f"TR-MIG-{i+1:03d}"
            # Dividir nombres y apellidos burdamente
            partes = nom.split(' ')
            nombres = partes[0] if len(partes) > 0 else nom
            apellidos = ' '.join(partes[1:]) if len(partes) > 1 else ''
            
            trabajador = Trabajador.query.filter_by(nombres=nombres, apellidos=apellidos).first()
            if not trabajador:
                # Comprobar si existe por nombre completo
                trabajador = Trabajador(codigo=cod, nombres=nombres, apellidos=apellidos, observaciones='Migrado desde RDP')
                trabajador.roles.append(roles_dict['MAQUINISTA'])
                db.session.add(trabajador)
        
        db.session.commit()

        # Vincular trabajador_id
        detalles = DetalleProduccionHora.query.all()
        for d in detalles:
            if d.maquinista_snapshot:
                nom_strip = d.maquinista_snapshot.strip()
                partes = nom_strip.split(' ')
                n_nom = partes[0]
                n_ape = ' '.join(partes[1:]) if len(partes) > 1 else ''
                
                t_match = Trabajador.query.filter_by(nombres=n_nom, apellidos=n_ape).first()
                if t_match:
                    d.trabajador_id = t_match.id

        db.session.commit()
        
        print("Migración completada exitosamente.")

if __name__ == '__main__':
    run_migration()
