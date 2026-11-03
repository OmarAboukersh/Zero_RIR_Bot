"""
AI Coach — a chat that knows your training (targets, recent sessions, settings,
weekly summary) and answers like a coach texting you.

Uses Google's Gemini API (free tier is plenty for one person):
  1. Get a key at https://aistudio.google.com/apikey (sign in with Google, "Create API key")
  2. Add to .env:   GEMINI_API_KEY="your key"
  3. Restart the web app.
Optional: GEMINI_MODEL (default "gemini-2.5-flash", Google's stable fast model).
If a model is busy (503) the coach retries once, then falls back to the newest
fast model and then the lite one, so a busy server rarely means no answer.

Note: your training data (exercise names, weights, reps, RPE) is sent to Google
with each question so the coach can answer about it.

The provider is isolated in ask_llm(), so switching to another AI later only
means changing that one function.
"""

import datetime
import os
import time

import requests

GEMINI_URL = os.getenv("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent")
MAX_TURNS = 20           # conversation turns sent with each question
MAX_MESSAGE_CHARS = 4000


class CoachError(Exception):
    """A problem the page should show to you as-is (missing key, limit reached...)."""


RULES = """How Omar's program works (the app computes targets with these rules; you don't change them yourself):
- Training split: Push (Tue), Pull (Wed), Push + Quads (Sat), Pull + Ham (Sun). All working sets are taken to 0 RIR (failure).
- Double progression: each exercise has a rep ceiling. When EVERY working set of the last session hits the ceiling, weight goes up by the exercise's step and reps restart (5 for 8-rep-ceiling lifts, 10 otherwise). Otherwise weight stays and the rep goal moves.
- RPE drives reps: RPE 9.5-10 = hold reps; RPE 8 = +1 rep; RPE 7 or less = +2 reps; RPE 8.5-9 or no RPE = a new rep best must be matched twice over ~2 weeks before adding a rep. Ceiling cleared at RPE 9.5+ = hold weight one more time (except "strict" lifts, which go up anyway).
- Max weight: a maxed-out machine stops adding weight and keeps adding reps.
- Deloads (tagged "deload", or detected automatically) are ignored for progression.
- Skipping an exercise once doesn't remove it; it leaves a day's plan after 3 sessions in a row without it.
- ML "hints" are shown only where they beat simply repeating last session.
- What Omar can change himself in the app: Settings tab (rep ceiling, weight step, max weight per exercise), "Swap" on an exercise to see targets for a substitute, and logging RPE honestly in Hevy (he finds RPE hard to rate consistently)."""

PERSONA = """You are Omar's strength coach inside his own training app ("Zero RIR"). Talk like a coach texting him: short, direct, encouraging but honest, no fluff. Use kg.
Base every number on the training data below; never invent sessions, weights or reps. If something isn't in the data, say so.
When he says he's injured, sore, tired, short on time, or taking a break: give a concrete plan for today/this week (which exercises to skip, swap or lighten, using exercises from his list where possible), and tell him exactly what to change in the app (Settings values or Swap) if anything. Sharp or worsening pain means stop that movement and see a physio or doctor; don't diagnose.
When he asks why a lift is stuck or what to change: look at his recent sets, RPEs and settings, explain in plain words, and suggest at most 2-3 changes.
Keep answers under ~150 words unless he asks for detail. Plain text with short lists is fine; no tables."""


def _fmt_sets(weights, reps):
    return "  ".join(f"{w:g}x{r}" for w, r in zip(weights, reps))


def build_context(next_split, splits, settings, weekly_text, history):
    """
    Text block describing Omar's current training, sent with every question.
      next_split: dict from web_app.build_split() plus "date_label"
      splits:     list of build_split() dicts (all 4 days)
      settings:   list of web_app._settings_row() dicts
      weekly_text: weekly_summary.build_weekly_summary() output
      history:    {exercise: [ {date, workout, sets: [[w, r]...], rpe, notes} ... ]} (oldest first)
    """
    lines = [f"Today is {datetime.date.today():%A, %d %b %Y}.", ""]

    if next_split:
        lines.append(f"NEXT WORKOUT: {next_split['key']} on {next_split.get('date_label', '')}")
    lines.append("")
    lines.append("CURRENT TARGETS (what the app tells him to do next, per day):")
    for s in splits:
        lines.append(f"[{s['key']} - {s['day']}]")
        for e in s["exercises"]:
            extra = " | WEIGHT UP" if e["increase"] else ""
            tags = f" | {'; '.join(e['tags'])}" if e["tags"] else ""
            lines.append(f"- {e['exercise']}: {e['weight']:g}kg x {e['reps']} reps x {e['sets']} sets{extra}. "
                         f"Why: {e['reason']}{tags}. Trend: {e['trend']}")
    lines.append("")

    lines.append("RECENT SESSIONS (oldest to newest, weight x reps per set):")
    for ex, sessions in history.items():
        lines.append(f"{ex}:")
        for h in sessions:
            rpe = f", RPE {h['rpe']:g}" if h.get("rpe") is not None else ""
            notes = f', notes: "{h["notes"]}"' if h.get("notes") else ""
            lines.append(f"  {h['date']} ({h['workout']}): {_fmt_sets([s[0] for s in h['sets']], [s[1] for s in h['sets']])}{rpe}{notes}")
    lines.append("")

    lines.append("EXERCISE SETTINGS (rep ceiling / weight step / max weight):")
    for s in settings:
        mx = f"{s['max_weight']:g}kg" if s["max_weight"] is not None else "none"
        flags = " (strict ceiling)" if s["strict"] else ""
        lines.append(f"- {s['exercise']}: ceiling {s['ceiling']}, step {s['step']:g}kg, max {mx}{flags}")
    lines.append("")

    lines.append("LAST 7 DAYS:")
    lines.append(weekly_text.replace("*", "").replace("_", ""))
    return "\n".join(lines)


def clean_messages(messages):
    """Validate the chat history sent by the page; keep the last MAX_TURNS turns."""
    if not isinstance(messages, list) or not messages:
        raise CoachError("Type a message first.")
    out = []
    for m in messages[-MAX_TURNS:]:
        role = m.get("role") if isinstance(m, dict) else None
        text = str(m.get("text", "")).strip() if isinstance(m, dict) else ""
        if role not in ("user", "model") or not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["text"] = (out[-1]["text"] + "\n\n" + text)[:MAX_MESSAGE_CHARS]
        else:
            out.append({"role": role, "text": text[:MAX_MESSAGE_CHARS]})
    # Gemini wants the conversation to start with the user and end with the user
    while out and out[0]["role"] != "user":
        out.pop(0)
    if not out or out[-1]["role"] != "user":
        raise CoachError("Type a message first.")
    return out


# Tried in order. Google's free tier often answers 503 ("overloaded") for its
# newest model, so a busy model falls back to the next one instead of failing.
FALLBACK_MODELS = ["gemini-flash-latest", "gemini-2.5-flash-lite"]
RETRY_STATUSES = (500, 502, 503, 504)


class _TryNext(Exception):
    """This model can't answer right now; try the next one."""


def _google_message(r):
    try:
        return str(r.json().get("error", {}).get("message", ""))[:160]
    except ValueError:
        return r.text[:160]


def _call_model(model, body, api_key, timeout):
    try:
        r = requests.post(GEMINI_URL.format(model=model), json=body,
                          headers={"x-goog-api-key": api_key}, timeout=timeout)
    except requests.Timeout:
        raise _TryNext(f"{model}: timed out")
    except requests.RequestException as e:
        raise CoachError(f"Couldn't reach Gemini ({type(e).__name__}). Check the laptop's internet connection.")

    if r.status_code in RETRY_STATUSES:
        raise _TryNext(f"{model}: busy ({r.status_code}) {_google_message(r)}")
    if r.status_code == 429:
        raise _TryNext(f"{model}: free limit reached (429) {_google_message(r)}")
    if r.status_code == 404:
        raise _TryNext(f"{model}: unknown model (404) {_google_message(r)}")
    if r.status_code in (400, 401, 403):
        detail = ""
        try:
            detail = r.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        if "API key" in detail or r.status_code in (401, 403):
            raise CoachError("Gemini rejected the API key. Check GEMINI_API_KEY in .env and restart the app.")
        raise CoachError(f"Gemini couldn't handle that request: {detail or r.status_code}")
    if not r.ok:
        raise _TryNext(f"{model}: error {r.status_code} {_google_message(r)}")

    data = r.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = data.get("promptFeedback", {}).get("blockReason")
        raise CoachError(f"Gemini didn't answer{f' ({reason})' if reason else ''}. Try rephrasing.")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise _TryNext(f"{model}: empty answer")
    if candidates[0].get("finishReason") == "MAX_TOKENS":
        text += "\n\n(cut off; ask me to continue)"
    return text


def ask_llm(system_text, messages, api_key=None, model=None, timeout=45, retry_wait=1.5):
    """Send the conversation to Gemini and return the coach's reply text.
    Tries your model (GEMINI_MODEL, default gemini-2.5-flash), retrying once if
    it's busy, then the fallback models."""
    api_key = (api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")).strip()
    first = (model or os.getenv("GEMINI_MODEL", "") or "gemini-2.5-flash").strip()
    if not api_key:
        raise CoachError("NO_KEY")

    body = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": m["role"], "parts": [{"text": m["text"]}]} for m in messages],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 4096},
    }
    models = [first] + [m for m in FALLBACK_MODELS if m != first]
    problems = []
    for i, m in enumerate(models):
        for attempt in range(2 if i == 0 else 1):
            try:
                return _call_model(m, body, api_key, timeout)
            except _TryNext as e:
                problems.append(str(e))
                if attempt == 0 and i == 0 and retry_wait:
                    time.sleep(retry_wait)
    print("⚠️ Coach: every Gemini model failed: " + "; ".join(problems))
    if all("free limit" in p for p in problems):
        raise CoachError("Gemini's free limit is used up for now. Try again in a few minutes.")
    if all("unknown model" in p for p in problems):
        raise CoachError(f"Gemini doesn't know the model '{first}'. Set GEMINI_MODEL in .env to a current model name.")
    if all("empty answer" in p for p in problems):
        raise CoachError("Gemini sent an empty answer. Try again.")
    raise CoachError("Gemini couldn't answer right now. Try again in a minute. What Google said: " + " | ".join(dict.fromkeys(problems)))


def ask_coach(messages, context_text, **kwargs):
    return ask_llm(PERSONA + "\n\n" + RULES + "\n\nTRAINING DATA\n" + context_text, clean_messages(messages), **kwargs)
