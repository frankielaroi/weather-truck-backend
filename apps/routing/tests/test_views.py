from django.test import Client


def test_health(client: Client) -> None:
    res = client.get("/api/health/")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_health_check_answers_for_any_host_header(client: Client) -> None:
    # Hosts probe the container by its private IP, which ALLOWED_HOSTS wouldn't accept.
    res = client.get("/api/health/", headers={"Host": "172.26.4.7:8000"})
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_other_paths_still_validate_the_host(client: Client) -> None:
    assert client.get("/api/trips/1/", headers={"Host": "evil.example"}).status_code == 400


def test_health_check_only_answers_get_and_head(client: Client) -> None:
    assert client.head("/api/health/").status_code == 200
    assert client.post("/api/health/", {}, headers={"Host": "evil.example"}).status_code == 400
