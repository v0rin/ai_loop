"""Run the loop against real LLMs via OpenRouter.

    python play_llm.py

You still play the USER (clarifications, accept/reject, blocked, impasse); the
reviewer/worker/auditor are real models. supervised=True steps through every
model call: prints the prompt, prints the reply, waits for Enter. Set it False
for an unattended run.

The budget ceiling here is in DOLLARS. Adjust models per role below.
"""
from pathlib import Path

from controller import Controller, ControllerConfig, BudgetController, Tracer
from agents import ModelReviewer, ModelWorker, ModelAuditor, InteractiveUser
from llm_transport import OpenRouterTransport
from model_io import c_multiline

# Key at ../openrouter_api_key.txt relative to this file (matches the existing layout).
KEY_PATH = Path(__file__).resolve().parent.parent / "openrouter_api_key.txt"

ROLE_MODELS = {
    "reviewer": "z-ai/glm-5.2",
    "auditor":  "z-ai/glm-5.2",
    "worker":   "z-ai/glm-5.2",
}
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"


def _final(result):
    print("\n" + "=" * 70 + "\n  FINAL STATE\n" + "=" * 70)
    for t in result.tasks:
        extra = f"   [{t.note}]" if t.note else ""
        print(f"   {t.id:6} {t.status.value:11} retries={t.retry_count} "
              f"blocks={t.block_count}{extra}")
    print(f"\n   summary: {result.summary.text if result.summary else '(none)'}")
    print(f"   halted={result.halted}   spent=${result.spent:.4f}")


def main():
    api_key = KEY_PATH.read_text(encoding="utf-8").strip()
    request = c_multiline("Initial user request (you can paste multiple lines)").strip() \
        or "Produce a short analysed report from raw data."
    raw = input("Budget ceiling in $ [2.00]:\n> ").strip()
    ceiling = float(raw) if raw else 2.00

    budget = BudgetController(ceiling=ceiling, report_every=4)
    transport = OpenRouterTransport(
        budget,
        api_key=api_key,
        role_models=ROLE_MODELS,
        default_model=DEFAULT_MODEL,
        max_cost_per_call=0.1,
        supervised=False,          # set False for unattended
        use_fusion=False,
    )
    controller = Controller(
        reviewer=ModelReviewer(transport),
        worker=ModelWorker(transport),
        auditor=ModelAuditor(transport),
        user=InteractiveUser(),
        budget=budget,
        config=ControllerConfig(),
        tracer=Tracer(enabled=True),
    )
    _final(controller.run(request))


if __name__ == "__main__":
    main()
