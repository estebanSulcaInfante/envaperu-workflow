from app import create_app
from app.extensions import db
from datetime import datetime, timezone

# Importamos TODOS los modelos necesarios
# Asegúrate de que lote.py, materiales.py y recetas.py existan en tu carpeta models/
from app.models.materiales import MateriaPrima, Colorante
from app.models.orden import OrdenProduccion
from app.models.lote import LoteColor
from app.models.recetas import SeCompone, SeColorea

app = create_app()

def inicializar_bd():
    with app.app_context():
        print("🗑️  Borrando base de datos antigua...")
        try:
            db.drop_all()
            print("🏗️  Creando tablas nuevas con la estructura actualizada...")
            db.create_all()
        except UnicodeDecodeError as e:
            print("\n❌ ERROR DE CODIFICACIÓN EN LA CONEXIÓN A LA BASE DE DATOS")
            print("   Parece que tu contraseña o usuario en '.env' tiene caracteres especiales (tildes, ñ, etc).")
            print("   Por favor, reemplaza esos caracteres con su código URL (ej: 'ó' -> '%C3%B3').")
            print(f"   Detalle del error: {e}")
            return
        except Exception as e:
            print(f"\n❌ Ocurrió un error inesperado al conectar con la BD: {e}")
            return

        print("🌱 Insertando datos semilla (Seed Data)...")

        # ---------------------------------------------------------
        # 1. CATALOGO DE MATERIALES (Sin Stock, solo nombres)
        # ---------------------------------------------------------
        mp_pp = MateriaPrima(nombre="POLIPROPILENO ALTA FLUIDEZ", tipo="VIRGEN")
        mp_recuperado = MateriaPrima(nombre="MOLIDO PP NEGRO", tipo="MOLIDO")
        pigmento_azul = Colorante(nombre="MASTERBATCH AZUL")
        
        db.session.add_all([mp_pp, mp_recuperado, pigmento_azul])
        db.session.commit()

        # ---------------------------------------------------------
        # 2. ORDEN DE PRODUCCIÓN (Con tus nuevos atributos)
        # ---------------------------------------------------------
        # Escenario: Molde de 4 cavidades, ciclo rápido de 25s.
        orden = OrdenProduccion(
            numero_op="OP-2025-DEMO",
            maquina_id="INY-05", 
            fecha_inicio=datetime.now(timezone.utc), # Add start date
            tipo_maquina="HAI TIAN 350T",
            producto="TAPA BALDE 20L",
            molde="MOLDE TAPA 4 CAV",
            
            # --- PARAMETROS TÉCNICOS (SNAPSHOT) ---
            peso_unitario_gr=45.0,        # Cada tapa pesa 45g
            peso_inc_colada=190.0,        # (45g * 4) + 10g de ramal = 190g el tiro
            cavidades=4,
            
            # --- TIEMPOS ---
            tiempo_ciclo=25.5,            # Segundos por golpe
            horas_turno=12.0,             # Turno de 12 horas
            ciclos=0.0,                   # Inicializamos en 0 (o la meta estimada)

            # --- ESTRATEGIA (Por Peso) ---
            tipo_estrategia="POR_PESO",
            meta_total_kg=1000.0,         # Queremos fabricar 1 tonelada de tapas
            meta_total_doc=None
        )
        db.session.add(orden)
        db.session.commit()

        # ---------------------------------------------------------
        # 3. LOTE DE COLOR (Hijo de la Orden)
        # ---------------------------------------------------------
        # Digamos que de esos 1000kg, 200kg serán Azules.
        lote_azul = LoteColor(
            orden_id=orden.id,
            color_nombre="AZUL ELECTRICO",
            personas=2
        )
        db.session.add(lote_azul)
        db.session.commit()

        # ---------------------------------------------------------
        # 4. RECETA DEL LOTE (Consumos calculados)
        # ---------------------------------------------------------
        # Mezcla: 50% Virgen, 50% Molido
        consumo_mp1 = SeCompone(
            lote_id=lote_azul.id, 
            materia_prima_id=mp_pp.id, 
            fraccion=0.5
        )
        consumo_mp2 = SeCompone(
            lote_id=lote_azul.id, 
            materia_prima_id=mp_recuperado.id, 
            fraccion=0.5
        )
        
        # Colorante: 50g por saco
        consumo_col = SeColorea(
            lote_id=lote_azul.id, 
            colorante_id=pigmento_azul.id, 
            gramos=50.0 
        )
        
        db.session.add_all([consumo_mp1, consumo_mp2, consumo_col])
        db.session.commit()

        # ---------------------------------------------------------
        # VERIFICACIÓN FINAL
        # ---------------------------------------------------------
        print("\n✅ ¡Base de Datos Inicializada con Éxito!")
        print("-" * 50)
        print(f"📄 Orden Generada: {orden.numero_op}")
        print(f"   Producto: {orden.producto} ({orden.cavidades} cavidades)")
        print(f"   Peso Tiro (inc. colada): {orden.peso_inc_colada} gr")
        print(f"   T/C: {orden.tiempo_ciclo} seg")
        print("-" * 50)
        print(f"🎨 Lote Generado: {lote_azul.color_nombre}")
        
        # Aquí probamos si tu propiedad calculada en lote.py funciona
        if hasattr(lote_azul, 'cantidad_coladas_calculada'):
            coladas = lote_azul.cantidad_coladas_calculada
            print(f"   ⚙️  Coladas Calculadas (Automático): {coladas} golpes")
        else:
            print("   ⚠️  (Nota: Asegúrate de actualizar lote.py con 'peso_inc_colada' en la fórmula)")

if __name__ == "__main__":
    inicializar_bd()