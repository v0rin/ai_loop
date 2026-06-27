"""Interactive runner -- you are the models (and the user).

    python play.py

For each model call you'll see the prompt, then choose:
  guided  -> answer the fields, the harness assembles a valid [$KEY] reply
  raw     -> type the literal wire reply yourself (use this to test bad input:
             a malformed reply triggers exactly one re-prompt, then an impasse)

The user-channel prompts (clarifications, accept, blocked, impasse) are you as
the end user -- those stay plain, they're never a model.
"""
from controller import Controller, ControllerConfig, BudgetController, Tracer
from model_io import HumanModel, c_multiline
from agents import ModelReviewer, ModelWorker, ModelAuditor, InteractiveUser


def _final(result):
    print("\n" + "=" * 70 + "\n  FINAL STATE\n" + "=" * 70)
    for t in result.tasks:
        extra = f"   [{t.note}]" if t.note else ""
        print(f"   {t.id:6} {t.status.value:11} retries={t.retry_count} "
              f"blocks={t.block_count}{extra}")
    print(f"\n   summary: {result.summary.text if result.summary else '(none)'}")
    print(f"   halted={result.halted}   spent={result.spent:.1f}")


def main():
    print("Interactive agent-loop. You are every model + the user.\n")
    request = c_multiline("Initial user request (you can paste multiple lines)").strip() \
        or "Produce an analysed report from raw data."
    raw = input("Budget ceiling [1000]:\n> ").strip()
    ceiling = float(raw) if raw else 1000.0

    budget = BudgetController(ceiling=ceiling, report_every=4)
    transport = HumanModel(budget)
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
