"""Layout guards.

The dashboard used to overflow a phone viewport by 160px: a five-column
table pushed the actions off-screen and the pipe-joined criteria string
wrapped into an unreadable stack. These pin the fixes so a later change
cannot quietly bring the sideways scroll back.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config, db, main
from app.models import Watch

TEMPLATES = Path(__file__).parent.parent / "app" / "templates"


@pytest.fixture
def client(temp_db, monkeypatch):
    monkeypatch.setattr(config, "GUI_PASSWORD", "demo")
    db.save_watch(
        Watch(
            label="Weekend mornings",
            courses=["weequahic", "byrne", "hendricks"],
            days=["sat", "sun"],
            time_start="07:00",
            time_end="10:30",
            min_players=2,
            holes="18",
            ntfy_topic="simon-abc123",
        )
    )
    with TestClient(main.app) as c:
        c.post("/login", data={"password": "demo"})
        yield c


def test_every_table_scrolls_inside_its_own_container():
    """A bare <table> is what pushed the page sideways on a phone."""
    offenders = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text()
        for match in re.finditer(r"<table[ >]", text):
            before = text[: match.start()]
            # The nearest preceding wrapper must still be open.
            if before.rfind('<div class="table-wrap">') <= before.rfind("</div>"):
                line = before.count("\n") + 1
                offenders.append(f"{path.name}:{line}")

    assert not offenders, (
        "these <table>s are not inside a .table-wrap scroll container, so they "
        f"can push the page sideways on a narrow screen: {offenders}"
    )


def test_the_watch_list_is_not_a_table():
    # Watches are configured items with actions, not tabular data. As a table
    # the actions column simply fell off a 390px screen.
    dashboard = (TEMPLATES / "dashboard.html").read_text()
    watch_list = dashboard.split('class="watch-list"')[1].split("</ul>")[0]
    assert "<table" not in watch_list


def test_criteria_render_as_chips_not_a_pipe_joined_string(client):
    body = client.get("/").text

    assert 'class="criteria-chips"' in body
    assert "chip-course" in body
    # The old rendering: "Weequahic, Byrne | Sat/Sun (next 14d) | 07:00-10:30"
    assert "07:00-10:30" not in body
    assert "7:00 - 10:30 AM" in body


def test_each_course_gets_its_own_chip(client):
    body = client.get("/").text
    # Three watched courses -> three separate course chips, not one joined
    # "Weequahic, Byrne, Hendricks" cell.
    assert body.count("chip-course") == 3
    for name in ("Weequahic", "Francis A. Byrne", "Hendricks Field"):
        assert name in body


def test_the_detail_page_shows_chips_too(client):
    body = client.get("/watches/1").text
    assert 'class="criteria-chips"' in body
    assert "7:00 - 10:30 AM" in body


def test_notifications_still_use_the_single_line_description():
    # describe() stays: a push notification wants one line, not markup.
    watch = Watch(id=1, courses=["weequahic"], min_players=2, time_start="07:00")
    assert "|" in watch.describe()


# --------------------------------------------------------------------------
# first-run guide
# --------------------------------------------------------------------------


def test_the_guide_requires_a_login(temp_db):
    # It names the ntfy topic, which is the only thing keeping someone else's
    # alerts private, so it must not be readable without logging in.
    with TestClient(main.app) as anonymous:
        response = anonymous.get("/help", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_guide_covers_both_talking_points(client):
    body = client.get("/help").text
    assert "Getting the notifications" in body
    assert "Setting up a watch" in body


def test_the_guide_names_the_topic_to_subscribe_to(client, monkeypatch):
    # Without the exact string to type, the ntfy step is unguessable.
    monkeypatch.setattr(config, "NTFY_TOPIC", "teetimes-abc123xyz")
    assert "teetimes-abc123xyz" in client.get("/help").text


def test_the_guide_links_to_the_ntfy_apps(client):
    body = client.get("/help").text
    assert "apps.apple.com" in body
    assert "play.google.com" in body


def test_the_guide_says_it_never_books_for_you(client):
    # The single most important expectation to set.
    assert "never books anything for" in client.get("/help").text


def test_the_guide_offers_a_test_notification(client):
    assert 'action="/test-notification"' in client.get("/help").text


def test_an_empty_dashboard_points_at_the_guide(client, temp_db):
    # The empty dashboard is the actual first-run moment.
    for w in db.list_watches():
        db.delete_watch(w.id)
    body = client.get("/").text
    assert 'href="/help"' in body
    assert "Nothing being watched yet" in body


def test_the_guide_stays_reachable_once_watches_exist(client):
    assert 'href="/help"' in client.get("/").text
