"""
Seeder: Recrea tablas y siembra data de catálogo para desarrollo.

Uso:
    source venv/bin/activate && python seed.py

Crea:
    - Lineas, Familias, FamiliaColor, ColorProducto
    - Molde "Balde Romano 20L" (1 pieza, 1 cav)
    - Molde "Jarra Regadera" (2 piezas: Tapa 2cav + Base 2cav)
      con variantes coloreadas vinculadas vía molde_pieza_id
"""
from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.extensions import db
from app.models.producto import (
    Linea, Familia, FamiliaColor, ColorProducto,
    Pieza, PiezaComponente
)
from app.models.molde import Molde, MoldePieza

app = create_app()

with app.app_context():
    print("🗑️  Eliminando tablas con CASCADE...")
    for table in reversed(db.metadata.sorted_tables):
        try:
            db.session.execute(db.text(f"DROP TABLE IF EXISTS {table.name} CASCADE"))
        except Exception as e:
            db.session.rollback()
            print(f"Ignorando drop de {table.name}: {e}")
    db.session.commit()
    
    print("🔨 Creando tablas...")
    db.create_all()

    # ── LINEAS ──
    lineas = [
        Linea(codigo=1, nombre='HOGAR'),
        Linea(codigo=2, nombre='JUGUETES'),
        Linea(codigo=3, nombre='INDUSTRIAL'),
    ]
    db.session.add_all(lineas)
    db.session.flush()
    hogar = lineas[0]
    industrial = lineas[2]

    # ── FAMILIAS ──
    familias = [
        Familia(codigo=10, nombre='JARRAS'),
        Familia(codigo=11, nombre='TAZONES'),
        Familia(codigo=14, nombre='PLAYEROS'),
        Familia(codigo=15, nombre='BALDES'),
    ]
    db.session.add_all(familias)
    db.session.flush()
    jarras = familias[0]
    baldes = familias[3]

    # ── FAMILIAS DE COLOR ──
    fam_colores = [
        FamiliaColor(codigo=1, nombre='SOLIDO'),
        FamiliaColor(codigo=2, nombre='CARAMELO'),
        FamiliaColor(codigo=3, nombre='TRANSPARENTE'),
        FamiliaColor(codigo=4, nombre='PASTEL'),
    ]
    db.session.add_all(fam_colores)
    db.session.flush()
    solido = fam_colores[0]

    # ── COLORES DE PRODUCTO ──
    colores = [
        ColorProducto(codigo=1, nombre='Amarillo', familia_id=solido.id),
        ColorProducto(codigo=2, nombre='Rojo', familia_id=solido.id),
        ColorProducto(codigo=3, nombre='Azul', familia_id=solido.id),
        ColorProducto(codigo=4, nombre='Magenta', familia_id=solido.id),
        ColorProducto(codigo=5, nombre='Verde', familia_id=solido.id),
        ColorProducto(codigo=6, nombre='Lila', familia_id=solido.id),
    ]
    db.session.add_all(colores)
    db.session.flush()

    # ══════════════════════════════════════════════
    # MOLDE 1: Balde Romano 20L (simple: 1 pieza)
    # ══════════════════════════════════════════════
    molde_balde = Molde(
        codigo='MOL-BALDE-20L',
        nombre='Balde Romano 20L',
        peso_tiro_gr=610,
        tiempo_ciclo_std=35,
        activo=True
    )
    db.session.add(molde_balde)
    db.session.flush()

    # Forma única: Cuerpo Balde 20L
    forma_cuerpo = MoldePieza(
        molde_id=molde_balde.codigo,
        nombre='Cuerpo Balde 20L',
        cavidades=1,
        peso_unitario_gr=600
    )
    db.session.add(forma_cuerpo)
    db.session.flush()

    # Pieza simple (sin color por ahora)
    pieza_balde = Pieza(
        sku='10101-BALDE',
        piezas='Cuerpo Balde 20L',
        peso=600,
        cavidad=1,
        tipo='SIMPLE',
        linea_id=industrial.id,
        familia_id=baldes.id,
        molde_pieza_id=forma_cuerpo.id
    )
    db.session.add(pieza_balde)

    # ══════════════════════════════════════════════
    # MOLDE 2: Jarra Regadera (multi-pieza: Tapa + Base)
    # ══════════════════════════════════════════════
    molde_jarra = Molde(
        codigo='MOL-JARRA-REG',
        nombre='Jarra Regadera',
        peso_tiro_gr=100,
        tiempo_ciclo_std=30,
        activo=True
    )
    db.session.add(molde_jarra)
    db.session.flush()

    # Forma 1: Tapa Regadera
    forma_tapa = MoldePieza(
        molde_id=molde_jarra.codigo,
        nombre='Tapa Regadera',
        cavidades=2,
        peso_unitario_gr=15
    )
    # Forma 2: Base Regadera
    forma_base = MoldePieza(
        molde_id=molde_jarra.codigo,
        nombre='Base Regadera',
        cavidades=2,
        peso_unitario_gr=35
    )
    db.session.add_all([forma_tapa, forma_base])
    db.session.flush()

    # Crear variantes coloreadas para cada forma × color
    colores_inyeccion = [colores[0], colores[1], colores[2]]  # Amarillo, Rojo, Azul

    for color in colores_inyeccion:
        # Tapa coloreada
        tapa = Pieza(
            sku=f'JARRA-REG-TAPA-C{color.codigo}',
            piezas=f'Tapa Regadera {color.nombre}',
            peso=forma_tapa.peso_unitario_gr,
            cavidad=forma_tapa.cavidades,
            tipo='SIMPLE',
            linea_id=hogar.id,
            familia_id=jarras.id,
            color_id=color.id,
            cod_color=color.codigo,
            color=color.nombre,
            molde_pieza_id=forma_tapa.id
        )
        # Base coloreada
        base = Pieza(
            sku=f'JARRA-REG-BASE-C{color.codigo}',
            piezas=f'Base Regadera {color.nombre}',
            peso=forma_base.peso_unitario_gr,
            cavidad=forma_base.cavidades,
            tipo='SIMPLE',
            linea_id=hogar.id,
            familia_id=jarras.id,
            color_id=color.id,
            cod_color=color.codigo,
            color=color.nombre,
            molde_pieza_id=forma_base.id
        )
        db.session.add_all([tapa, base])

        # Kit por color
        kit = Pieza(
            sku=f'JARRA-REG-KIT-C{color.codigo}',
            piezas=f'Jarra Regadera Completa {color.nombre}',
            peso=forma_tapa.peso_unitario_gr + forma_base.peso_unitario_gr,
            cavidad=1,
            tipo='KIT',
            linea_id=hogar.id,
            familia_id=jarras.id,
            color_id=color.id,
            cod_color=color.codigo,
            color=color.nombre
        )
        db.session.add(kit)
        db.session.flush()

        # Componentes del kit
        db.session.add(PiezaComponente(kit_sku=kit.sku, componente_sku=tapa.sku, cantidad=1))
        db.session.add(PiezaComponente(kit_sku=kit.sku, componente_sku=base.sku, cantidad=1))

    db.session.commit()

    # ── Resumen ──
    print(f"✅ Lineas: {Linea.query.count()}")
    print(f"✅ Familias: {Familia.query.count()}")
    print(f"✅ FamiliasColor: {FamiliaColor.query.count()}")
    print(f"✅ Colores: {ColorProducto.query.count()}")
    print(f"✅ Moldes: {Molde.query.count()}")
    print(f"✅ Formas (MoldePieza): {MoldePieza.query.count()}")
    print(f"✅ Piezas: {Pieza.query.count()}")
    print()
    for m in Molde.query.all():
        print(f"  {m.codigo}: {m.nombre} — {m.cavidades_totales} cavidades")
        for mp in m.piezas:
            variantes = len(mp.variantes) if hasattr(mp, 'variantes') else 0
            print(f"    └ {mp.nombre}: {mp.cavidades} cav, {mp.peso_unitario_gr}g ({variantes} variantes color)")
    print()
    print("🎉 Seed completo!")
