"""Standalone test: does OpenRouter web search actually engage?

    python search_test.py

Makes ONE call with the web plugin and a question that needs current info, then
prints the answer, token/cost usage, and -- the real proof -- any URL citations
(annotations) the search returned. If annotations/citations are present, search
ran. The full message is dumped at the end so you can see exactly what shape the
search results came back in for this model.

Nothing here depends on the agent_loop code; it's a clean probe of the SDK call.
"""
import json
from pathlib import Path

from openrouter import OpenRouter
from openrouter.components.chatusermessage import ChatUserMessage
from openrouter.components.stopservertoolswhenmaxcost import StopServerToolsWhenMaxCost
from openrouter.components.stopservertoolswhenstepcountis import (
    StopServerToolsWhenStepCountIs,
)

# --- knobs ------------------------------------------------------------------
KEY_PATH = Path(__file__).resolve().parent.parent / "openrouter_api_key.txt"
MODEL = "deepseek/deepseek-v4-flash"   # change to whatever you want to test
MAX_COST = 0.10                        # $ per-call guard (search costs more than text)
WEB_MAX_RESULTS = 5

# A question that CANNOT be answered from training data — it needs a live search,
# and we explicitly ask for the source URL so citations should appear if search ran.
QUESTION = (
    "Use web search. What is the current latest stable release of Python on "
    "python.org, and on what date was it released? Give the version number, the "
    "release date, and the exact source URL you got it from."
)


def _text(message) -> str:
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(getattr(p, "text", "") or "" for p in content)


def main():
    client = OpenRouter(api_key=KEY_PATH.read_text(encoding="utf-8").strip())

    result = client.chat.send(
        model=MODEL,
        plugins=[{"id": "web", "max_results": WEB_MAX_RESULTS}],   # dict form (supported)
        messages=[ChatUserMessage(role="user", content=QUESTION)],
        stop_server_tools_when=[
            StopServerToolsWhenStepCountIs(step_count=4, type="step_count_is"),
            StopServerToolsWhenMaxCost(max_cost_in_dollars=MAX_COST, type="max_cost"),
        ],
    )

    msg = result.choices[0].message

    print("\n================ ANSWER ================")
    print(_text(msg) or "(empty)")

    usage = result.usage
    print("\n================ USAGE ================")
    print(f"prompt={getattr(usage, 'prompt_tokens', None)}  "
          f"completion={getattr(usage, 'completion_tokens', None)}  "
          f"cost=${getattr(usage, 'cost', None)}")

    # The proof: web search returns URL citations as message annotations.
    annotations = getattr(msg, "annotations", None)
    print("\n================ SEARCH PROOF (annotations) ================")
    if annotations:
        for a in annotations:
            print("  -", a)
        print(f"\n=> {len(annotations)} citation(s) present: WEB SEARCH RAN.")
    else:
        print("  No `annotations` field populated.")
        print("  Either search didn't run, or this model returns citations under a")
        print("  different field — check the full dump below for url_citation / sources.")

    # Full dump so you can see the real shape regardless of field naming.
    print("\n================ FULL message (for inspection) ================")
    try:
        print(json.dumps(msg.model_dump(), indent=2, default=str)[:6000])
    except Exception as exc:
        print("(model_dump failed:", exc, ")")
        print("dir(msg):", [a for a in dir(msg) if not a.startswith("_")])


if __name__ == "__main__":
    main()
