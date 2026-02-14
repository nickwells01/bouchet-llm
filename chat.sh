#!/bin/bash
# Simple interactive chat with vLLM on Bouchet
# Usage: ./chat.sh [port]

PORT="${1:-27347}"
BASE="http://localhost:${PORT}/v1"

MODEL=$(curl -s "${BASE}/models" | python3 -c "import sys,json; print(json.load(sys.stdin)['data'][0]['id'])" 2>/dev/null)

if [ -z "$MODEL" ]; then
    echo "Cannot reach server on localhost:${PORT}. Is the tunnel open?"
    exit 1
fi

echo "Connected to ${MODEL} on localhost:${PORT}"
echo "Type your message and press Enter. Ctrl+C to quit."
echo ""

python3 - "$BASE" "$MODEL" <<'PYEOF'
import sys, json, readline

base_url = sys.argv[1]
model = sys.argv[2]
history = []

try:
    from urllib.request import Request, urlopen
except ImportError:
    print("Python urllib not available")
    sys.exit(1)

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
        "max_tokens": 1024
    }).encode()

    req = Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"}
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
PYEOF
