"""AI coach: request building, reply parsing and error handling, with a fake Gemini."""

import pytest

import coach


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.ok = 200 <= status < 300
        self.text = str(payload)

    def json(self):
        return self._payload


def reply(text, finish="STOP"):
    return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}]}


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(coach.time, "sleep", lambda s: None)


@pytest.fixture
def fake_gemini(monkeypatch):
    calls = []

    def install(status=200, payload=None):
        def fake_post(url, json, headers, timeout):
            calls.append({"url": url, "json": json, "headers": headers})
            return FakeResponse(status, payload if payload is not None else reply("Hold 95kg, nail 6 reps."))
        monkeypatch.setattr(coach.requests, "post", fake_post)
        return calls
    return install


HELLO = [{"role": "user", "text": "How's my bench?"}]


def test_sends_system_prompt_history_and_key(fake_gemini):
    calls = fake_gemini()
    text = coach.ask_coach(HELLO, "Bench Press (Smith Machine): 95kg x 6", api_key="k", model="m")
    assert text == "Hold 95kg, nail 6 reps."
    body = calls[0]["json"]
    assert calls[0]["headers"]["x-goog-api-key"] == "k"
    assert "/models/m:generateContent" in calls[0]["url"]
    system = body["system_instruction"]["parts"][0]["text"]
    assert "Bench Press (Smith Machine): 95kg x 6" in system and "Double progression" in system
    assert body["contents"] == [{"role": "user", "parts": [{"text": "How's my bench?"}]}]


def test_missing_key_is_reported(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(coach.CoachError, match="NO_KEY"):
        coach.ask_coach(HELLO, "ctx")


@pytest.mark.parametrize("status,payload,msg", [
    (429, {}, "free limit"),
    (503, {}, "overloaded"),
    (403, {"error": {"message": "denied"}}, "rejected the API key"),
    (400, {"error": {"message": "API key not valid"}}, "rejected the API key"),
    (404, {}, "doesn't know the model"),
    (500, {}, "overloaded"),
    (200, {"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}, "SAFETY"),
    (200, reply(""), "empty answer"),
])
def test_errors_become_readable_messages(fake_gemini, status, payload, msg):
    fake_gemini(status, payload)
    with pytest.raises(coach.CoachError, match=msg):
        coach.ask_coach(HELLO, "ctx", api_key="k")


def test_cut_off_answers_are_marked(fake_gemini):
    fake_gemini(200, reply("Part of an answer", finish="MAX_TOKENS"))
    assert "cut off" in coach.ask_coach(HELLO, "ctx", api_key="k")


def test_thinking_parts_are_hidden(fake_gemini):
    fake_gemini(200, {"candidates": [{"content": {"parts": [
        {"text": "internal reasoning", "thought": True}, {"text": "Real answer"}]}}]})
    assert coach.ask_coach(HELLO, "ctx", api_key="k") == "Real answer"


def test_history_is_trimmed_and_must_end_with_user():
    long = [{"role": "user" if i % 2 == 0 else "model", "text": f"m{i}"} for i in range(31)]
    out = coach.clean_messages(long)
    assert len(out) <= coach.MAX_TURNS and out[0]["role"] == "user" and out[-1]["text"] == "m30"
    with pytest.raises(coach.CoachError):
        coach.clean_messages([{"role": "user", "text": "hi"}, {"role": "model", "text": "yo"}])
    with pytest.raises(coach.CoachError):
        coach.clean_messages([])


def test_busy_model_falls_back_to_the_next_one(monkeypatch):
    tried = []

    def fake_post(url, json, headers, timeout):
        model = url.split("/models/")[1].split(":")[0]
        tried.append(model)
        if model == "gemini-2.5-flash":
            return FakeResponse(503, {"error": {"message": "The model is overloaded."}})
        return FakeResponse(200, reply(f"answer from {model}"))
    monkeypatch.setattr(coach.requests, "post", fake_post)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert coach.ask_coach(HELLO, "ctx", api_key="k") == "answer from gemini-flash-latest"
    assert tried == ["gemini-2.5-flash", "gemini-2.5-flash", "gemini-flash-latest"]   # one retry, then fallback


def test_unanswered_questions_are_merged():
    out = coach.clean_messages([{"role": "user", "text": "How did my last workout go?"},
                                {"role": "user", "text": "test"}])
    assert out == [{"role": "user", "text": "How did my last workout go?\n\ntest"}]
