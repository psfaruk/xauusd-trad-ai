"""GET /api/health — Phase 0 AC."""

from fastapi.testclient import TestClient

from app.main import app


def test_health_ok() -> None:
    with TestClient(app) as client:  # context manager runs the lifespan
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["data_source"] == "mock"  # DATA_SOURCE default (C7)
        assert "db" in body and "version" in body


def test_health_no_auth_required() -> None:
    with TestClient(app) as client:
        # No Authorization header anywhere (SPEC §7.1: /health is public).
        resp = client.get("/api/health", headers={})
        assert resp.status_code == 200
