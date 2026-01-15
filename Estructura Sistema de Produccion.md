# 📄 Informe de Definición de Estructura: Sistema de Producción (Completo y Normalizado)

Este documento define la estructura de datos, lógica de negocio y contrato de interfaz (API) para la vista completa de Órdenes de Producción, basada en el archivo *OP1322-BALDE ROMANO.xlsm*.

---

## 1. Entidad: Orden de Producción (Cabecera Global)
Es la entidad padre (`OrdenProduccion`). Contiene la configuración técnica de la máquina, el molde y la estrategia de producción seleccionada.

| Atributo / Campo | Origen de Dato | Descripción | Fórmula / Lógica |
| :--- | :--- | :--- | :--- |
| **Nº OrdenProduccion** | Input (Sistema) | Identificador único (ej. OP-1322). | - |
| **Fecha Creación** | Automático | Fecha de registro en BD (Timestamp). | `NOW()` |
| **F. Inicio** | **Input (Usuario)** | Fecha planificada para el arranque de producción. | - |
| **Producto** | Input (Usuario) | Nombre del producto a fabricar. | - |
| **Molde** | Input (Usuario) | Código o nombre del molde. | - |
| **Máquina** | Input (Usuario) | ID de la máquina asignada. | - |
| **Tipo Máquina** | Input (Usuario) | Clasificación técnica (ej. Hidráulica 500T). | - |
| **Tipo de Orden** | Input (Select) | Estrategia: *Por Peso, Por Cantidad, Stock*. | - |
| **Activa** | Automático | Estado de la orden (abierta/cerrada). Default: `true`. Una OP cerrada no permite crear nuevos Registros Diarios. | `Boolean` |
| **Snapshot T. Ciclo (seg)** | Input (Técnico) | Duración de un ciclo de inyección en segundos (Snapshot). | - |
| **Snapshot P Unit (g)** | Input (Técnico) | Peso de una pieza limpia (Snapshot). | - |
| **Snapshot Cavidades** | Input (Técnico) | Número de piezas por golpe (Snapshot). | - |
| **Snapshot P Inc Cola (g)** | Input (Técnico) | Peso total del tiro (Piezas + Colada) (Snapshot). | - |
| **Ciclos** | Input (Técnico) | Cantidad de ciclos teóricos (si aplica). | - |
| **Snapshot Horas Turno** | Input (Técnico) | Horas laborales por día (ej. 23 o 24) (Snapshot). | - |
| **T/C (Tipo Cambio)** | Input (Finanzas) | Tipo de cambio USD/PEN al crear la orden (para costeo). | - |

---

## 2. Entidad: Lote de Color (Detalle Polimórfico)
Entidad hija (`LoteColor`). Su comportamiento es dinámico: las columnas de "Peso" o "Cantidad" se calculan o habilitan según la estrategia del padre.

| Atributo Visual | Tipo | Descripción | Fórmula / Lógica |
| :--- | :--- | :--- | :--- |
| **Color** | Input | Nombre del color (ej. Amarillo). | - |
| **Por Cantidad (Kg)** | Calculado | *Activo solo en "Por Cantidad".* Muestra la equivalencia en Kg de las docenas. | `(MetaDoc * 12 * P.Unit / 1000) / #Colores` |
| **Peso (Kg)** | Calculado | *Activo solo en "Por Peso".* División equitativa de la meta global. | `MetaKg / #Colores` |
| **Stock (Kg)** | **Input Manual** | *Activo solo en "Stock".* Único campo editable por lote. | `Input Usuario` |
| **Extra (Kg)** | Calculado | Parte proporcional del desperdicio asignada a este color. | `TotalExtraOrden / #Colores` |
| **TOTAL + EXTRA (Kg)** | Calculado | Peso final a entregar a máquina (Base + Extra). | `PesoBase + ExtraKg` |

---

## 3. Entidad: Tabla Auxiliar de Cálculos (Resumen Totales)
Es una vista calculada (`resumen_totales`) que no se guarda en BD, sino que se procesa en tiempo real para mostrar los tiempos y materiales totales.

| Atributo / Etiqueta | Descripción | Fórmula / Lógica (Python Style) |
| :--- | :--- | :--- |
| **Peso(Kg) PRODUCCION** | Meta neta de producción (Base de cálculo). | `MetaKg` (si Peso), `Doc*12*PUnit/1000` (si Cant), `Sum(Lotes)` (si Stock) |
| **Peso (Kg) Inc. Merma** | Producción + Merma Natural (sin reglas de cobro). | `Produccion * (1 + %Merma)` |
| **% Merma** | Porcentaje de desperdicio real del tiro. | `(PesoTiro - (P.Unit * Cav)) / PesoTiro` |
| **Merma Natural Kg** | Kilos físicos de desperdicio generados. | `PesoIncMerma - PesoProduccion` |
| **Cantidad DOC** | Producción neta en Docenas (con decimales). | `MetaDoc` (si Cant) o `(Produccion * 1000) / P.Unit / 12` |
| **Total DOC** | Producción neta en Docenas (redondeo visual). | `ROUND(Cantidad DOC, 0)` |
| **% EXTRA** | Porcentaje cobrable al cliente (según reglas). | `IF(%Merma < 5%, %Merma, IF(%Merma <= 10%, %Merma/2, 0))` |
| **EXTRA** | Kilos adicionales cobrables agregados a la orden. | `PesoProduccion * %Extra` |
| **Peso REAL A ENTREGAR** | Total materia prima requerida (Prod + Extra). | `PesoProduccion + EXTRA` |
| **Horas** | Tiempo estimado de inyección en horas. | `Dias * HorasTurno` |
| **Días** | Tiempo estimado en días (según turno). | `(TotalGolpes * Ciclo) / 3600 / HorasTurno` |
| **F. Fin** | Fecha estimada de finalización. | `WORKDAY(F.Inicio, Dias)` (Sumar días laborales) |

---

## 4. Entidad: Composición de Materiales (Lista Dinámica)
Define la mezcla de materia prima para cada lote. Se modela como una lista de componentes, eliminando las columnas fijas (Virgen 1, 2...).

| Atributo | Origen | Descripción | Lógica |
| :--- | :--- | :--- | :--- |
| **Material** | Input (Select) | Nombre del material seleccionado de la BD. | - |
| **Tipo** | Automático | Clasificación (Virgen, Segunda) traída del Material. | - |
| **Fracción** | Input (Manual) | Porcentaje de participación en la mezcla (0.0 a 1.0). | - |
| **Peso (Kg)** | Calculado | Kilos de material requeridos para este lote. | `PesoTotalLote * Fracción` |

> **Validación:** La suma de las fracciones de la lista `materiales` debe ser **1.0**.

---

## 5. Entidad: Receta de Colorantes (Lista Dinámica)
Define la lista de pigmentos necesarios para el color.

| Atributo | Origen | Descripción | Lógica |
| :--- | :--- | :--- | :--- |
| **Pigmento** | Input (Select) | Nombre del pigmento o masterbatch. | - |
| **Dosis** | Input (Manual) | Cantidad a usar (Gramos por bolsa). | - |

**Mano de Obra (Por Lote):**
| Atributo | Origen | Descripción | Fórmula |
| :--- | :--- | :--- | :--- |
| **# Personas** | Input (Manual) | Operarios asignados a la mezcla. | - |
| **HH** | Calculado | Horas Hombre requeridas. | `(TotalDias * HorasTurno * #Personas) / #Colores` |

---

## 6. Estructura JSON para Frontend (Referencia)

Estructura de respuesta sugerida para el endpoint `GET /api/ordenes/<id>`. Incluye **todas** las secciones necesarias para renderizar la vista completa de Órdenes.

```json
{
  "cabecera": {
    "numero_op": "OP-1322",
    "fecha_creacion": "2023-11-20T08:00:00",
    "fecha_inicio": "2023-11-21T07:00:00",
    "producto": "BALDE ROMANO",
    "molde": "MOLDE-BALDE-01",
    "maquina": "M1",
    "tipo_maquina": "Hidráulica 500T",
    "tipo_orden": "POR_PESO",
    "tiempo_ciclo": 30.0,
    "peso_unitario": 87.0,
    "cavidades": 2,
    "peso_tiro": 176.0,
    "horas_turno": 23.0
  },
  
  "lotes": [
    {
      "colores": "Amarillo",
      
      // --- SECCIÓN 1: POLIMORFISMO ---
      "Por cantidad": null,
      "Peso (Kg)": 175.0,
      "Stock (Kg)": null,
      "Extra (kg)": 1.99,
      "TOTAL + EXTRA Kg": 176.99,

      // --- SECCIÓN 2: MATERIALES (LISTA DINÁMICA) ---
      "materiales": [
        { "nombre": "PP Clarif", "tipo": "Virgen", "fraccion": 0.5, "peso_kg": 88.49 },
        { "nombre": "Molido", "tipo": "Segunda", "fraccion": 0.5, "peso_kg": 88.50 }
      ],

      // --- SECCIÓN 3: COLORANTES (LISTA DINÁMICA) ---
      "pigmentos": [
         { "nombre": "Amarillo CH 1041", "dosis_gr": 30 },
         { "nombre": "Dioxido Titanio", "dosis_gr": 5 }
      ],
      
      "mano_obra": {
         "personas": 1,
         "horas_hombre": 18.3
      }
    }
  ],
  
  "tabla_auxiliar": {
    "F.Fin": "2023-11-23",
    "Dias": 2.19,
    "Horas": 50.28,
    "Peso(Kg) PRODUCCION": 1050.00,
    "Peso (Kg) Inc. Merma": 1061.93,
    "%Merma": 0.0114,
    "Merma Natural Kg": 11.93,
    "Cantidad DOC": 1005.75,
    "% EXTRA": 0.0114,
    "EXTRA": 11.93,
    "Total DOC": 1006.0,
    "Peso Kg REAL PARA ENTREGAR A MAQUINA": 1062.00
  }
}
```

---

## 7. Entidad: Registro Diario de Producción (Hoja de Producción)

Representa la "Hoja de Producción" física que se llena por turno. Es hija de `OrdenProduccion` y contiene la producción real reportada por los maquinistas.

| Atributo / Campo | Origen de Dato | Descripción | Fórmula / Lógica |
| :--- | :--- | :--- | :--- |
| **ID Registro** | Auto (BD) | Identificador único del registro diario. | `AUTOINCREMENT` |
| **Orden ID (FK)** | Selección | Referencia a la Orden de Producción padre. | - |
| **Máquina ID (FK)** | Selección | Máquina donde se ejecutó la producción. | - |
| **Fecha** | Input (Manual) | Fecha del turno de producción. | - |
| **Turno** | Input (Select) | DIURNO, NOCTURNO, o EXTRA. | - |
| **Hora Inicio** | Input (Manual) | Hora de arranque (ej. 07:00). | - |
| **Colada Inicial** | Input (Manual) | Contador de la máquina al inicio del turno. | - |
| **Colada Final** | Input (Manual) | Contador de la máquina al final del turno. | - |
| **Tiempo Ciclo Reportado** | Input (Manual) | T/C observado en el panel (segundos). | - |
| **Tiempo Enfriamiento** | Input (Manual) | Tiempo de enfriamiento observado (seg). | - |

### Snapshots (Captura al Crear)

Se copian de la `OrdenProduccion` al momento de crear el registro para mantener consistencia histórica.

| Atributo | Origen | Descripción |
| :--- | :--- | :--- |
| **snapshot_cavidades** | Orden | Número de cavidades al crear el registro. |
| **snapshot_peso_neto_gr** | Orden | Peso unitario de pieza (gramos). |
| **snapshot_peso_colada_gr** | Orden | Peso del ramal/colada (gramos). |

### Totalizadores (Calculados)

| Atributo | Descripción | Fórmula |
| :--- | :--- | :--- |
| **Total Coladas** | Ciclos realizados en el turno. | `colada_final - colada_inicial` |
| **Total Piezas** | Piezas buenas producidas. | `total_coladas * snapshot_cavidades` |
| **Total Kg Real** | Peso teórico producido (antes de pesaje). | `(total_coladas * peso_tiro_gr) / 1000` |

> **Nota:** `peso_tiro_gr = (snapshot_peso_neto_gr * snapshot_cavidades) + snapshot_peso_colada_gr`

---

## 8. Entidad: Detalle Producción Hora (Tabla Interna)

Cada `RegistroDiarioProduccion` tiene N filas de detalle, una por cada hora trabajada. Permite el seguimiento hora-a-hora.

| Atributo | Origen | Descripción |
| :--- | :--- | :--- |
| **Hora** | Auto | Franja horaria (ej. "07:00 - 08:00"). |
| **Maquinista** | Input | Nombre del operador en esa hora. |
| **Color** | Input | Color producido (puede cambiar por hora). |
| **Coladas Realizadas** | Input | Cantidad de ciclos en esa hora. |
| **Observación** | Input | Notas (parada, cambio de molde, etc.). |
| **Cantidad Piezas** | Calculado | `coladas_realizadas * cavidades` |
| **Kg Producidos** | Calculado | `(coladas_realizadas * peso_tiro_gr) / 1000` |

---

## 9. Entidad: Control de Peso (Doble Verificación)

Sistema de pesaje individual de "bultos" para contrastar con la producción reportada. Sirve como doble verificación y control de calidad.

| Atributo | Origen | Descripción |
| :--- | :--- | :--- |
| **ID** | Auto | Identificador del pesaje. |
| **Registro ID (FK)** | Sistema | Vinculado al Registro Diario padre. |
| **Peso Real (Kg)** | Input/Balanza | Peso medido del bulto. |
| **Color** | Input | Color o identificador del bulto. |
| **Hora Registro** | Automático | Timestamp del pesaje. |

### Validación de Peso

| Métrica | Descripción | Fórmula |
| :--- | :--- | :--- |
| **Total Pesado** | Suma de todos los bultos. | `SUM(peso_real_kg)` |
| **Peso Teórico** | Peso calculado del registro. | `registro.total_kg_real` |
| **Diferencia** | Discrepancia entre ambos. | `total_pesado - peso_teorico` |
| **Coincide** | Validación con tolerancia. | `ABS(diferencia) < 5 Kg` |

---

## 10. Estructura JSON: Registro Diario (Referencia)

Estructura de respuesta sugerida para `GET /api/ordenes/<op>/registros`.

```json
{
  "ID Registro": 1,
  "FECHA": "2023-11-21",
  "Turno": "DIURNO",
  "Maquina": "INY-05",
  "Hora Inicio": "07:00",
  "Colada Ini": 1000,
  "Colada Fin": 1500,
  "Total Coladas (Calc)": 500,
  "Total Piezas (Est)": 1000,
  "Total Kg (Est)": 87.0,
  "detalles": [
    {
      "hora": "07:00 - 08:00",
      "maquinista": "Juan Perez",
      "color": "AMARILLO",
      "coladas": 50,
      "piezas": 100,
      "kg": 8.7
    }
  ]
}
```