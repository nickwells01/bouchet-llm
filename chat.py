#!/usr/bin/env python3
"""Interactive chat with vLLM on Bouchet."""
import sys, json, readline
from urllib.request import Request, urlopen

port = sys.argv[1] if len(sys.argv) > 1 else "27347"
base_url = f"http://localhost:{port}/v1"

try:
    with urlopen(f"{base_url}/models") as resp:
        model = json.loads(resp.read())["data"][0]["id"]
except Exception:
    print(f"Cannot reach server on localhost:{port}. Is the tunnel open?")
    sys.exit(1)

print(f"Connected to {model} on localhost:{port}")
print("Type your message and press Enter. Ctrl+C to quit.\n")

history = []

while True:
    try:
        user_input = input("\033[1;32mYou:\033[0m ")
    except (EOFError, KeyboardInterrupt):
        print("\nBye!")
        break

    if not user_input.strip():
        continue

    history.append({"role": "user", "content": user_input})

    payload = json.dumps({
        "model": model,
        "messages": history,
        "max_tokens": 1024,
    }).encode()

    req = Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urlopen(req) as resp:
            data = json.loads(resp.read())
        reply = data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"Error: {e}")
        history.pop()
        continue

    history.append({"role": "assistant", "content": reply})
    print(f"\033[1;34mAssistant:\033[0m {reply}\n")
