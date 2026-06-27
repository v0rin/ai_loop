"""Deterministic scenarios over ScriptedTransport.

Unlike the old typed mocks, the scripts here are raw WIRE STRINGS, so every run
exercises the real parse path. Scenarios:
  A  rich happy path (ASK->PLAN, reject+fix, low-conf accept + auditor + override,
     block+resume, coherence patch, summary audit + revise)
  B  impasse -> abandon -> dependency cascade-cancel
  C  malformed reply -> one re-prompt -> still bad -> ModelProtocolError -> impasse
  D  hard budget ceiling halts mid-task
"""
from controller import Controller, ControllerConfig, BudgetController, Tracer
from model_io import ScriptedTransport
from agents import ModelReviewer, ModelWorker, ModelAuditor, ScriptedUser
from models import Task, ImpasseDecision, ImpasseAction
from wire import block, task_block

# ---- wire-string builders (what a model would emit) ----------------------- #
def plan_more(msg): return block("STATUS","CONTINUE")+block("MESSAGE",msg)+block("TASKS","none")
def plan_done(ts, msg="here is the plan"): return block("STATUS","DONE")+block("MESSAGE",msg)+block("TASKS","\n".join(task_block(t) for t in ts))
def work(out, a="none"): return block("OUTPUT",out)+block("ASSUMPTIONS",a)
def accept(conf="HIGH"): return block("VERDICT","ACCEPT")+block("CONFIDENCE",conf)+block("DEFECTS","none")+block("CONTEXT","none")+block("QUESTION","none")
def reject(defs, ctx="none"): return block("VERDICT","REJECT")+block("CONFIDENCE","HIGH")+block("DEFECTS","\n".join(defs))+block("CONTEXT",ctx)+block("QUESTION","none")
def blocked(q): return block("VERDICT","BLOCKED")+block("CONFIDENCE","HIGH")+block("DEFECTS","none")+block("CONTEXT","none")+block("QUESTION",q)
def recon(ok=True, defs=None): return block("VERDICT","ACCEPT")+block("DEFECTS","none")+block("CONTEXT","none") if ok else block("VERDICT","REJECT")+block("DEFECTS","\n".join(defs or ["x"]))+block("CONTEXT","none")
def cohere(ts=None): return block("PATCHES","none" if not ts else "\n".join(task_block(t) for t in ts))
def summ(t): return block("SUMMARY",t)
def audit(defs=None): return block("DEFECTS","none" if not defs else "\n".join(defs))


def run(name, replies, user, *, ceiling=1000.0, report_every=6):
    print(f"\n\n#################### {name} ####################")
    budget = BudgetController(ceiling=ceiling, report_every=report_every)
    tr = ScriptedTransport(budget, replies)
    c = Controller(reviewer=ModelReviewer(tr), worker=ModelWorker(tr), auditor=ModelAuditor(tr),
                   user=user, budget=budget, config=ControllerConfig(), tracer=Tracer(True))
    res = c.run("demo request")
    print("\n   ---- final ----")
    for t in res.tasks:
        print(f"   {t.id:6} {t.status.value:11} retries={t.retry_count}"
              + (f"  [{t.note}]" if t.note else ""))
    print(f"   summary: {res.summary.text if res.summary else '(none)'}")
    print(f"   halted={res.halted}  spent={res.spent:.1f}")


def scenario_a():
    t1 = Task("t1","Gather data","CSV >=100 rows")
    t2 = Task("t2","Analyse","stats with stddev", deps=["t1"])
    tp = Task("tp","Reconcile","report matches stats", deps=["t1","t2"])
    replies = {
        "reviewer": [
            plan_more("which date range?"), plan_done([t1,t2]),
            reject(["only 12 rows, need >=100"], ctx="pull the full year"), accept("LOW"), recon(True),
            blocked("sample or population stddev?"), accept("HIGH"),
            cohere([tp]), accept("HIGH"), cohere(None),
            summ("Full-year analysis complete; report drafted."),
            summ("Full-year analysis complete; reconciled and verified."),
        ],
        "worker": [work("data.csv 12 rows"), work("data.csv 140 rows"),
                   work("stats table"), work("stats table, sample stddev"),
                   work("reconciled report")],
        "auditor": [audit(["stddev may be population not sample"]), audit(["omits reconciliation"])],
    }
    run("SCENARIO A: rich happy path",
        replies, ScriptedUser(planner_replies=["full year 2024"], accept=True, blocked={"t2": ["use sample stddev"]}))


def scenario_b():
    t1 = Task("t1","Impossible spec","internally consistent")
    t2 = Task("t2","Build from spec","passes tests", deps=["t1"])
    replies = {
        "reviewer": [plan_done([t1,t2]), reject(["contradiction A"]),
                     reject(["contradiction B"]), reject(["contradiction C"])],
        "worker": [work("v1"), work("v2"), work("v3")],
        "auditor": [],
    }
    run("SCENARIO B: impasse + cascade",
        replies, ScriptedUser(impasse={"t1": ImpasseDecision(ImpasseAction.ABANDON)}))


def scenario_c():
    t1 = Task("t1","Do thing","exists")
    bad = block("OUTPUT","x")  # missing ASSUMPTIONS block -> ParseError, twice
    replies = {
        "reviewer": [plan_done([t1])],
        "worker": [bad, bad],          # original + re-prompt both malformed
        "auditor": [],
    }
    run("SCENARIO C: parse failure -> re-prompt -> impasse",
        replies, ScriptedUser(impasse={"t1": ImpasseDecision(ImpasseAction.ABANDON)}))


def scenario_d():
    t1 = Task("t1","Step one","ok")
    t2 = Task("t2","Step two","ok", deps=["t1"])
    t3 = Task("t3","Step three","ok", deps=["t2"])
    replies = {
        "reviewer": [plan_done([t1,t2,t3]), accept("HIGH")],
        "worker": [work("done one")],
        "auditor": [],
    }
    # plan 5 + t1 worker 6 + judge 11, then t2 worker -> 12 == ceiling -> halt
    run("SCENARIO D: budget halt", replies, ScriptedUser(), ceiling=12.0, report_every=3)


if __name__ == "__main__":
    scenario_a(); scenario_b(); scenario_c(); scenario_d()
