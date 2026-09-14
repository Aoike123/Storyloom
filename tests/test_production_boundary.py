from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from backend.production_boundary import LOCAL_WORKSPACE_ORIGIN, ProductionOriginBoundary, normalize_public_origin


def protected_client():
    inner = FastAPI()

    @inner.post('/probe')
    async def probe(request: Request):
        return {'origin': request.headers.get('origin')}

    return TestClient(ProductionOriginBoundary(inner, 'https://demo.example.com'))


def test_production_origin_is_checked_then_adapted_to_local_guard():
    with protected_client() as client:
        response = client.post('/probe', headers={'Origin': 'https://demo.example.com'})
    assert response.status_code == 200
    assert response.json()['origin'] == LOCAL_WORKSPACE_ORIGIN.decode()


def test_cross_origin_production_write_is_rejected():
    with protected_client() as client:
        response = client.post('/probe', headers={'Origin': 'https://evil.example'})
    assert response.status_code == 403
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('value', ['http://demo.example.com', 'https://user@demo.example.com', 'https://demo.example.com/path'])
def test_public_origin_requires_one_clean_https_origin(value):
    with pytest.raises(ValueError):
        normalize_public_origin(value)
