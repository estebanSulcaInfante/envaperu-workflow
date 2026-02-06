@produccion_bp.route('/ordenes/<numero_op>/metricas', methods=['PUT'])
def actualizar_metricas_orden(numero_op):
    """
    Permite editar metricas tecnicas de una orden ACTIVA.
    Caso de uso: Molde Dañado (reduccion de cavidades), ajuste de ciclo real.
    """
    orden = db.session.get(OrdenProduccion, numero_op)
    if not orden:
        return jsonify({'error': 'Orden no encontrada'}), 404
        
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Payload requerido'}), 400
        
    try:
        # Solo permitimos editar ciertos campos tecnicos
        if 'snapshot_cavidades' in data:
            orden.snapshot_cavidades = data['snapshot_cavidades']
            # OJO: Si cambia cavidades, cambian calculos base?
            # Si, deberiamos llamar a orden.actualizar_metricas()
            
        if 'snapshot_tiempo_ciclo' in data:
            orden.snapshot_tiempo_ciclo = data['snapshot_tiempo_ciclo']
            
        if 'snapshot_peso_inc_colada' in data:
            orden.snapshot_peso_inc_colada = data['snapshot_peso_inc_colada']
            
        # Recalcular todo
        orden.actualizar_metricas()
        db.session.commit()
        
        return jsonify(orden.to_dict()), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500
