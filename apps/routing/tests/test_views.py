from django.test import Client


def test_health(client: Client) -> None:
    res = client.get("/api/health/")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}
