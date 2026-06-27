"""Single OpenRouter call (optionally via the Fusion plugin), with a per-run report file.

Python port of simple_fusion_call.mjs using the `openrouter` SDK (0.10.x).

Note vs the JS version:
- The raw Python SDK's Fusion plugin uses `model` (judge) and `analysis_models`
  (panel), not `judge_model` / `panel_models`.
- The JS top-level `preset: "budget"` has no equivalent on `chat.send`; the budget
  intent is expressed via the Fusion plugin `preset="general-budget"` instead.
"""

from datetime import datetime, timezone
from pathlib import Path

from openrouter import OpenRouter
from openrouter.components.fusionplugin import FusionPlugin
from openrouter.components.chatusermessage import ChatUserMessage
from openrouter.components.stopservertoolswhenmaxcost import StopServerToolsWhenMaxCost
from openrouter.components.stopservertoolswhenstepcountis import (
    StopServerToolsWhenStepCountIs,
)

# --- API key (read from ai_loops/openrouter_api_key.txt) ---------------------
KEY_PATH = Path(__file__).resolve().parent.parent / "openrouter_api_key.txt"
OPENROUTER_API_KEY = KEY_PATH.read_text(encoding="utf-8").strip()

client = OpenRouter(api_key=OPENROUTER_API_KEY)

# --- Knobs -------------------------------------------------------------------
USE_FUSION = False
MODEL = "z-ai/glm-5.2" if USE_FUSION else "deepseek/deepseek-v4-flash"
FUSION_JUDGE_MODEL = "z-ai/glm-5.2"
FUSION_PANEL_MODELS = ["deepseek/deepseek-v4-flash", "qwen/qwen3.6-flash", "deepseek/deepseek-v4-pro"]
MAX_COST_PER_CALL = 0.05  # dollars, per individual call

TASK = (
    "what will happen when the input tokens exceed the context window? Also after "
    "the reply add which model of LLM you are. But if you are the aggregator on top of saying "
    "which model of LLM you are, also list all the LLMs you the reponse from!"
)

# --- Report file (a new one per run) -----------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPORTS_DIR = SCRIPT_DIR / "loop_reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

run_stamp = datetime.now(timezone.utc).isoformat().replace(":", "-").replace(".", "-")
REPORT_PATH = REPORTS_DIR / f"loop_report_{run_stamp}.md"

REPORT_PATH.write_text(
    f"# Loop report — {datetime.now(timezone.utc).isoformat()}\n\n"
    f"## TASK\n\n{TASK}\n\n",
    encoding="utf-8",
)
print(f"Report: {REPORT_PATH}")


def report(text: str) -> None:
    with REPORT_PATH.open("a", encoding="utf-8") as f:
        f.write(text)


def message_text(message) -> str:
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    # Multi-part content: concatenate any text parts.
    parts = []
    for part in content:
        text = getattr(part, "text", None)
        if text is not None:
            parts.append(text)
    return "".join(parts)


# --- The call ----------------------------------------------------------------
messages = [ChatUserMessage(role="user", content=TASK)]
total_in = 0
total_out = 0

plugins = (
    [
        FusionPlugin(
            id="fusion",
            model=FUSION_JUDGE_MODEL,
            analysis_models=FUSION_PANEL_MODELS,
            preset="general-budget",
        )
    ]
    if USE_FUSION
    else []
)

result = client.chat.send(
    model=MODEL,
    plugins=plugins,
    messages=messages,
    stop_server_tools_when=[
        StopServerToolsWhenStepCountIs(step_count=1, type="step_count_is"),
        StopServerToolsWhenMaxCost(max_cost_in_dollars=MAX_COST_PER_CALL, type="max_cost"),
    ],
)

draft = message_text(result.choices[0].message)
print(draft)

usage = result.usage
in_tokens = usage.prompt_tokens if usage else None
out_tokens = usage.completion_tokens if usage else None
total_in += in_tokens or 0
total_out += out_tokens or 0
print(f"\n[{MODEL}: {in_tokens if in_tokens is not None else '?'} in / "
      f"{out_tokens if out_tokens is not None else '?'} out]")
report(
    f"\n## ({MODEL})\n\n{draft}\n\n"
    f"> tokens: {in_tokens if in_tokens is not None else '?'} in / "
    f"{out_tokens if out_tokens is not None else '?'} out\n"
)

print(f"\n----- totals: {total_in} input + {total_out} output tokens -----")
report(f"\n## Totals\n\n{total_in} input + {total_out} output tokens\n")
