from unittest.mock import patch


def test_health_is_a_database_free_liveness_probe(client):
    with patch('app.extensions.db.session.execute') as execute:
        response = client.get('/api/health')

    assert response.status_code == 200
    assert response.get_json() == {'status': 'ok'}
    execute.assert_not_called()


def test_readiness_reports_database_available(client):
    response = client.get('/api/ready')

    assert response.status_code == 200
    assert response.get_json() == {
        'status': 'ok',
        'database': 'available',
    }


def test_readiness_reports_database_unavailable(client):
    with patch(
        'app.extensions.db.session.execute',
        side_effect=RuntimeError('database unavailable'),
    ):
        response = client.get('/api/ready')

    assert response.status_code == 503
    assert response.get_json() == {
        'status': 'degraded',
        'database': 'unavailable',
    }
