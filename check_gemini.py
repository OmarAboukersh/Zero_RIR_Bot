"""
Checks your Gemini API key and which models it can use.

Run it from the Zero_RIR_Bot folder:   python check_gemini.py
It prints what Google says for each step; send that output if the coach isn't working.
The key itself is never printed.
"""

import os
import sys

import requests
from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

load_dotenv()
key = os.getenv("GEMINI_API_KEY", "").strip().strip('"').strip("'")
BASE = "https://generativelanguage.googleapis.com/v1beta"

print("1) Key in .env")
if not key:
    print("   ❌ No GEMINI_API_KEY found in .env")
    sys.exit(1)
looks = "looks like a normal Gemini key" if key.startswith("AIza") and len(key) == 39 else \
        "does NOT look like a normal Gemini key (those start with AIza and are 39 characters)"
print(f"   Found a key, {len(key)} characters, {looks}.")

print("\n2) Asking Google which models this key can use")
try:
    r = requests.get(f"{BASE}/models", headers={"x-goog-api-key": key}, params={"pageSize": 200}, timeout=20)
except requests.RequestException as e:
    print(f"   ❌ Couldn't reach Google: {e}")
    sys.exit(1)
if not r.ok:
    print(f"   ❌ Google said {r.status_code}: {r.text[:300]}")
    print("   → Make a new key at https://aistudio.google.com/apikey and paste it into .env")
    sys.exit(1)
names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
         if "generateContent" in m.get("supportedGenerationMethods", [])]
flash = [n for n in names if "flash" in n]
print(f"   ✅ Key works. {len(names)} chat models available. Flash models: {', '.join(flash) or 'none'}")

print("\n3) Sending a tiny test message to each model the coach uses")
for model in ["gemini-2.5-flash", "gemini-flash-latest", "gemini-2.5-flash-lite"] + [n for n in flash if n.startswith("gemini-3")][:2]:
    try:
        r = requests.post(f"{BASE}/models/{model}:generateContent", headers={"x-goog-api-key": key}, timeout=40,
                          json={"contents": [{"role": "user", "parts": [{"text": "Say OK"}]}]})
        if r.ok:
            print(f"   ✅ {model}: works")
        else:
            msg = r.json().get("error", {}).get("message", r.text) if r.headers.get("content-type", "").startswith("application/json") else r.text
            print(f"   ❌ {model}: {r.status_code} {str(msg)[:200]}")
    except requests.RequestException as e:
        print(f"   ❌ {model}: {e}")

print("\nIf one model works but the coach still fails, put it in .env as GEMINI_MODEL=\"that-model\" and restart the app.")
