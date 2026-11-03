"""The web app's API, run against a small fake workouts.csv."""

import pytest

import bot
import csv_trend_engine as engine
from conftest import write_csv


@pytest.fixture
def client(tmp_path, monkeypatch):
    csv_path = tmp_path / "workouts.csv"
    write_csv(csv_path, [
        (8, "Push day", {"Bench Press (Smith Machine)": [(90, 7, 9), (90, 7, 9)],
                         "Triceps Pushdown": [(50, 8, 8), (50, 8, 8)]}),
        (1, "Push day", {"Bench Press (Smith Machine)": [(90, 8, 9), (90, 8, 9)],
                         "Triceps Pushdown": [(50, 8, 8), (50, 7, 9)]}),
    ])
    monkeypatch.setattr(engine, "CSV_FILE", str(csv_path))
    monkeypatch.setattr(bot, "SETTINGS_FILE", str(tmp_path / "exercise_settings.json"))
    import web_app
    monkeypatch.setattr(web_app, "APP_PIN", "")
    web_app.app.config["TESTING"] = True
    yield web_app.app.test_client()
    bot.apply_settings_overrides()  # undo any settings changed during the test


def test_split_targets(client):
    r = client.get("/api/split/Push")
    assert r.status_code == 200
    names = [e["exercise"] for e in r.get_json()["exercises"]]
    assert names == ["Bench Press (Smith Machine)", "Triceps Pushdown"]
    bench = r.get_json()["exercises"][0]
    assert bench["weight"] == 92.5 and bench["increase"] is True


def test_stats_autocorrects_the_name(client):
    r = client.get("/api/stats?exercise=bench")
    assert r.status_code == 200
    d = r.get_json()
    assert d["exercise"] == "Bench Press (Smith Machine)" and len(d["sessions"]) == 2


def test_unknown_exercise_is_a_clear_error(client):
    r = client.get("/api/stats?exercise=zzzzqq")
    assert r.status_code == 400 and "Couldn't find" in r.get_json()["error"]


def test_settings_change_and_reset(client):
    r = client.post("/api/settings", json={"exercise": "bench", "key": "step", "value": "5"})
    assert r.status_code == 200 and r.get_json()["step"] == 5 and "step" in r.get_json()["changed"]
    r = client.post("/api/settings/reset", json={"exercise": "bench"})
    assert r.get_json()["step"] == 2.5 and r.get_json()["changed"] == []


@pytest.mark.parametrize("key,value", [("ceiling", "7.5"), ("step", "0"), ("step", "abc"), ("ceiling", "")])
def test_bad_settings_are_rejected(client, key, value):
    r = client.post("/api/settings", json={"exercise": "bench", "key": key, "value": value})
    assert r.status_code == 400


def test_pin_protects_the_api(client, monkeypatch):
    import web_app
    monkeypatch.setattr(web_app, "APP_PIN", "4321")
    assert client.get("/api/exercises").status_code == 401
    assert client.get("/api/exercises", headers={"X-Pin": "4321"}).status_code == 200
    assert client.get("/api/ping").status_code == 200  # the page needs this to know a PIN is required


def test_autosync_without_hevy_key_does_nothing(client, monkeypatch):
    monkeypatch.setattr(bot, "HEVY_API_KEY", "")
    r = client.post("/api/autosync")
    assert r.get_json() == {"synced": False, "changed": False, "reason": "no Hevy key"}
