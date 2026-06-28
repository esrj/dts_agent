from __future__ import annotations

import os
import sys

# Allow running this file directly: `python src/llm_provider/test.py`
# by putting the `src` directory on the import path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_provider import get_provider, Message, Role

SYSTEM_PROMPT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "system_prompt.md"
)


def load_system_prompt() -> str:
    try:
        with open(SYSTEM_PROMPT_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def main() -> None:
    provider = get_provider()  # "auto": pick the first available provider
    model = getattr(provider, "model_name", "?")
    print(f"Connected: provider={provider.name}, model={model}")
    print("Chat away. Commands: /exit to quit, /reset to clear history.\n")

    system_prompt = load_system_prompt()

    def fresh_history() -> list[Message]:
        h: list[Message] = []
        if system_prompt:
            h.append(Message(role=Role.SYSTEM, content=system_prompt))
        return h

    history = fresh_history()

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            print("Bye.")
            break
        if user_input == "/reset":
            history = fresh_history()
            print("(history cleared)\n")
            continue

        history.append(Message(role=Role.USER, content=user_input))

        try:
            response = provider.complete(history, temperature=0.2, max_tokens=2048)
        except Exception as exc:
            print(f"[error] {exc}\n")
            history.pop()  # drop the user message that failed
            continue

        print(f"\n{provider.name}: {response.content}\n")
        history.append(Message(role=Role.ASSISTANT, content=response.content))


if __name__ == "__main__":
    main()
