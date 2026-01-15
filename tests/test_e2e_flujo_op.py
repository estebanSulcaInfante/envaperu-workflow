"""
E2E Test: Flujo Completo de Orden de Producción

Este test simula el flujo completo desde la creación de una OP hasta
el cierre con pesajes sincronizados.

Flujo:
1. Central: Crear talonario + OP
2. Scale: Reponer cache de correlativos
3. Scale: Simular QR scan de OP, crear RDP
4. Scale: Registrar múltiples pesajes
5. Scale: Sincronizar pesajes al central
6. Central: Verificar pesajes recibidos
7. Central: Cerrar OP con historial

Nota: Los tests requieren ambos servidores corriendo.
"""
import pytest
import requests
from datetime import date
import time

# URLs de los servidores
CENTRAL_URL = 'http://127.0.0.1:5000'
SCALE_URL = 'http://127.0.0.1:5050'


class TestFlujoOPCompleto:
    """E2E Test del flujo completo de Orden de Producción"""
    
    def test_flujo_completo(self):
        """Ejecuta todos los pasos del flujo en secuencia"""
        
        # --- SETUP ---
        ts = int(time.time()) % 100000
        op_numero = f'OP-E2E-{ts}'
        talonario_desde = 80000 + (ts * 10)  # Rango dinámico
        talonario_hasta = talonario_desde + 99
        
        print(f"\nIniciando E2E con OP: {op_numero}, Talonario: {talonario_desde}-{talonario_hasta}")

        # 1. Crear talonario en central
        print("1. Creando talonario en central...")
        response = requests.post(f'{CENTRAL_URL}/api/talonarios', json={
            'desde': talonario_desde,
            'hasta': talonario_hasta,
            'descripcion': 'Talonario E2E Test'
        })
        assert response.status_code == 201, f"Error creando talonario: {response.text}"
        
        # 2. Crear Orden de Producción en central
        print("2. Creando OP en central...")
        response = requests.post(f'{CENTRAL_URL}/api/ordenes', json={
            'numero_op': op_numero,
            'producto': 'BALDE ROMANO E2E',
            'molde': 'BALDE PLAYERO',
            'tipo_estrategia': 'POR_PESO',
            'meta_total_kg': 500,
            'peso_unitario_gr': 87.0,
            'peso_inc_colada': 176.0,
            'cavidades': 2,
            'tiempo_ciclo': 30.0,
            'horas_turno': 12.0,
            'lotes': [{
                'color_nombre': 'AMARILLO',
                'personas': 1,
                'materiales': [],
                'pigmentos': []
            }]
        })
        assert response.status_code == 201, f"Error creando OP: {response.text}"
        data_op = response.json()
        assert data_op['numero_op'] == op_numero
        
        # 3. Reponer cache de correlativos en scale
        print("3. Reponiendo cache en scale...")
        response = requests.post(f'{SCALE_URL}/api/rdp/cache/reponer')
        assert response.status_code == 200, f"Error reponiendo cache: {response.text}"
        
        # 4. Crear RDP (simula escaneo de QR de OP)
        print("4. Generando RDP en scale...")
        rdp_data = {
            'nro_op': op_numero,
            'molde': 'BALDE PLAYERO',
            'maquina': 'INY-05',
            'turno': 'DIURNO',
            'fecha_ot': date.today().isoformat(),
            'operador': 'OPERADOR E2E'
        }
        response = requests.post(f'{SCALE_URL}/api/rdp/generar', json=rdp_data)
        assert response.status_code == 200, f"Error generando RDP: {response.text}"
        data_rdp = response.json()
        correlativo_rdp = data_rdp['correlativo']
        print(f"   RDP Generado: {correlativo_rdp}")
        
        # 5. Registrar pesajes en scale
        print("5. Registrando pesajes...")
        pesajes = [
            {'peso_kg': 10.5, 'color': 'AMARILLO'},
            {'peso_kg': 11.2, 'color': 'AMARILLO'},
            {'peso_kg': 9.8, 'color': 'AMARILLO'},
        ]
        
        for peso_data in pesajes:
            response = requests.post(f'{SCALE_URL}/api/pesajes', json={
                'peso_kg': peso_data['peso_kg'],
                'molde': 'BALDE PLAYERO',
                'maquina': 'INY-05',
                'nro_op': op_numero,
                'turno': 'DIURNO',
                'fecha_orden_trabajo': date.today().isoformat(),
                'nro_orden_trabajo': str(correlativo_rdp),
                'operador': 'OPERADOR E2E',
                'color': peso_data['color'],
                'pieza_sku': 'TEST-001',
                'pieza_nombre': 'Pieza Test'
            })
            assert response.status_code == 201, f"Error registrando pesaje: {response.text}"
        
        # 6. Sincronizar pesajes
        print("6. Sincronizando...")
        response = requests.post(f'{SCALE_URL}/api/sync/trigger')
        assert response.status_code == 200, f"Error sync: {response.text}"
        
        # 7. Verificar en central (opcional, depende de si sync fue exitoso inmediato)
        # Podríamos consultar los registros de la OP
        print("7. Verificando en central...")
        # Get OP details from Central to check calculations
        # Esperar un momento para asegurar que el proceso Async o Sync termino (si fuera async)
        time.sleep(1) 
        
        resp_op = requests.get(f'{CENTRAL_URL}/api/ordenes/{op_numero}')
        assert resp_op.status_code == 200
        data_op_central = resp_op.json()
        
        real_kg = data_op_central.get('avance_real_kg', 0)
        expected_kg = 10.5 + 11.2 + 9.8
        print(f"   Avance Real Kg en Central: {real_kg} (Esperado: {expected_kg})")
        
        # Check exactness with small float tolerance
        assert abs(real_kg - expected_kg) < 0.01, \
            f"CALCULO INCORRECTO: Central tiene {real_kg}, se esperaba {expected_kg}"

        # 8. Cerrar OP con historial
        print("8. Cerrando OP...")
        response = requests.put(f'{CENTRAL_URL}/api/ordenes/{op_numero}/estado', json={
            'activa': False,
            'usuario': 'test_e2e',
            'motivo': 'Fin de prueba E2E'
        })
        assert response.status_code == 200, f"Error cerrando OP: {response.text}"
        data_cierre = response.json()
        assert data_cierre['activa'] == False
        
        print("9. Verificando historial...")
        response = requests.get(f'{CENTRAL_URL}/api/ordenes/{op_numero}/historial')
        assert response.status_code == 200
        data_hist = response.json()
        assert len(data_hist['historial']) >= 1
        assert data_hist['historial'][0]['accion'] == 'CERRADA'
        
        print("\n✅ E2E TEST COMPLETADO EXITOSAMENTE")


class TestFlujoOPOffline:
    """E2E Test del flujo offline del scale module"""
    
    def test_generar_rdp_sin_conexion_central(self):
        """Generar RDP usando cache local"""
        # Verificar estado del cache primero
        try:
            status = requests.get(f'{SCALE_URL}/api/rdp/cache/status').json()
            if status.get('disponibles', 0) == 0:
                pytest.skip("Cache vacío, no se puede probar offline")
        except:
            pytest.skip("No se pudo conectar al scale module")
        
        # Simular generación
        response = requests.post(f'{SCALE_URL}/api/rdp/generar', json={
            'nro_op': 'OP-OFFLINE-TEST',
            'molde': 'MOLDE TEST',
            'maquina': 'INY-01',
            'turno': 'NOCTURNO',
            'fecha_ot': date.today().isoformat(),
            'operador': 'OPERADOR OFFLINE'
        })
        
        assert response.status_code == 200
        assert response.json()['success'] == True

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
