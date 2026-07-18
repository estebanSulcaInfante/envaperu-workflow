from unittest.mock import patch


def test_health_reports_database_available(client):
    response = client.get('/api/health')

    assert response.status_code == 200
    assert response.get_json() == {
        'status': 'ok',
        'database': 'available',
    }


def test_health_reports_database_unavailable(client):
    with patch(
        'app.extensions.db.session.execute',
        side_effect=RuntimeError('database unavailable'),
    ):
        response = client.get('/api/health')

    assert response.status_code == 503
    assert response.get_json() == {
        'status': 'degraded',
        'database': 'unavailable',
    }
