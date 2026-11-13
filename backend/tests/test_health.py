from fastapi.testclient import TestClient

from app.main import app


def test_liveness_contract() -> None:
    with TestClient(app) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_business_routes_are_not_exposed() -> None:
    with TestClient(app) as client:
        assert client.get("/documents").status_code == 404
