"""Logging in, and telling a misconfigured server from a wrong password.

The distinction matters: a server with no GUI_PASSWORD set rejected every
attempt as "Incorrect password", which sends you hunting through .env for a
typo that was never there. The real cause is usually that .env is only read
when the process starts, so an edit without a restart changes nothing.
"""

import pytest
from fastapi.testclient import TestClient

from app import auth, config, main


@pytest.fixture
def client(temp_db):
    with TestClient(main.app) as c:
        yield c


def test_no_password_configured_says_so(client, monkeypatch):
    monkeypatch.setattr(config, "GUI_PASSWORD", "")

    response = client.post("/login", data={"password": "anything"})

    assert response.status_code == 503
    assert "no GUI_PASSWORD set" in response.text
    # Names the actual fix, since the edit alone does nothing.
    assert "restart" in response.text
    assert "Incorrect password" not in response.text


def test_a_wrong_password_is_still_an_ordinary_401(client, monkeypatch):
    monkeypatch.setattr(config, "GUI_PASSWORD", "correct-horse")

    response = client.post("/login", data={"password": "nope"})

    assert response.status_code == 401
    assert "Incorrect password" in response.text


def test_the_right_password_logs_you_in(client, monkeypatch):
    monkeypatch.setattr(config, "GUI_PASSWORD", "correct-horse")

    response = client.post(
        "/login", data={"password": "correct-horse"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert config.SESSION_COOKIE in response.cookies


def test_passwords_with_awkward_characters_work(client, monkeypatch):
    # Generated passwords routinely contain these; none should need escaping.
    for password in ("Xk4/9vQ+2mZp$3aB", "a=b=c", "bang!pass", "has#hash"):
        monkeypatch.setattr(config, "GUI_PASSWORD", password)
        response = client.post(
            "/login", data={"password": password}, follow_redirects=False
        )
        assert response.status_code == 303, f"{password!r} failed to authenticate"


def test_is_configured_reflects_the_setting(monkeypatch):
    monkeypatch.setattr(config, "GUI_PASSWORD", "")
    assert not auth.is_configured()
    monkeypatch.setattr(config, "GUI_PASSWORD", "x")
    assert auth.is_configured()


def test_an_unconfigured_server_never_authenticates(monkeypatch):
    monkeypatch.setattr(config, "GUI_PASSWORD", "")
    assert not auth.check_password("")
    assert not auth.check_password("anything")
