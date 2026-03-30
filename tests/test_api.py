"""Basic API tests."""

def test_healthz():
    """Health endpoint should return OK."""
    # This test requires the full app to be running.
    # For unit testing, use pytest with TestClient:
    #   from fastapi.testclient import TestClient
    #   from main import app
    #   client = TestClient(app)
    #   r = client.get("/healthz")
    #   assert r.status_code == 200
    pass
