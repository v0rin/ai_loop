// loop.mjs — autonomous loop with SEPARATED maker and reviewer.
//
// Run with:  node loop.mjs   (after pasting your key below)
//
// The maker writes and revises in its own running thread.
// The reviewer is a FRESH call each round — no maker history, no knowledge
// that it wrote the draft — so it reviews with cold, unanchored eyes.
// The reviewer (not the maker) owns the [DONE] decision.

import { OpenRouter, stepCountIs, maxCost } from "@openrouter/agent";
import { mkdirSync, writeFileSync, appendFileSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// --- API key (read from ai_loops/openrouter_api_key.txt) ---------------------
const KEY_PATH = join(dirname(fileURLToPath(import.meta.url)), "..", "openrouter_api_key.txt");
const OPENROUTER_API_KEY = readFileSync(KEY_PATH, "utf-8").trim();

const openrouter = new OpenRouter({ apiKey: OPENROUTER_API_KEY });

// --- Knobs -------------------------------------------------------------------
const MAKER_MODEL    = "openrouter/fusion";
const REVIEWER_MODEL = "openrouter/fusion";
// const FUSION_JUDGE_MODEL    = "deepseek/deepseek-v4-pro"; // I will use GLM-5.2 here later on and also as one of the analysis models
const FUSION_JUDGE_MODEL    = "deepseek/deepseek-v4-flash";
// const FUSION_ANALYSIS_MODELS = ["deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash", "moonshotai/kimi-k2.6"];
const FUSION_ANALYSIS_MODELS = ["deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-flash"];
const MAX_ITERATIONS    = 2;     // hard ceiling on rounds (the outer gate)
const MAX_COST_PER_CALL = 0.05;  // dollars, per individual call

const TASK = `Produce a concrete decision framework for choosing a cataract
surgery clinic for an elderly relative. Include: the evaluation criteria, how
to weight them, the specific questions to ask each clinic, and the red flags
that should disqualify one. Be specific and practical, not generic.`;

const REVIEWER_INSTRUCTIONS =
  "You are a skeptical domain expert. You are reviewing a document written " +
  "by someone else. You did not write it and have no attachment to it. Your " +
  "job is to find what is wrong, missing, or unjustified — not to be kind. " +
  "Are there gaps, ambiguities, or unverified claims?";

// --- Report file (a new one per run) -----------------------------------------
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const REPORTS_DIR = join(SCRIPT_DIR, "loop_reports");
mkdirSync(REPORTS_DIR, { recursive: true });

const runStamp = new Date().toISOString().replace(/[:.]/g, "-");
const REPORT_PATH = join(REPORTS_DIR, `loop_report_${runStamp}.md`);

writeFileSync(
  REPORT_PATH,
  `# Loop report — ${new Date().toISOString()}\n\n` +
  `- Maker model: ${MAKER_MODEL}\n` +
  `- Reviewer model: ${REVIEWER_MODEL}\n` +
  `- Max iterations: ${MAX_ITERATIONS}\n\n` +
  `## TASK\n\n${TASK}\n\n` +
  `## Reviewer instructions\n\n${REVIEWER_INSTRUCTIONS}\n`,
);
console.log(`Report: ${REPORT_PATH}`);

const report = (text) => appendFileSync(REPORT_PATH, text);

// --- The loop ----------------------------------------------------------------
let makerMessages = [{ role: "user", content: TASK }];
let draft = "";
let totalIn = 0, totalOut = 0;

const track = async (result) => {
  const u = (await result.getResponse()).usage ?? {};
  totalIn  += u.inputTokens  ?? 0;
  totalOut += u.outputTokens ?? 0;
  return u;
};

for (let i = 1; i <= MAX_ITERATIONS; i++) {
  console.log(`\n========== ROUND ${i} — MAKER (${MAKER_MODEL}) ==========`);

  // 1) MAKE — the writer drafts or revises in its own accumulating thread.
  const makeResult = openrouter.callModel({
    model: MAKER_MODEL,
    plugins: [{id: "fusion", model: FUSION_JUDGE_MODEL, analysis_models: FUSION_ANALYSIS_MODELS}],
    input: makerMessages,
    stopWhen: [stepCountIs(1), maxCost(MAX_COST_PER_CALL)],
  });
  draft = await makeResult.getText();
  console.log(draft);
  const mu = await track(makeResult);
  console.log(`\n[maker: ${mu.inputTokens ?? "?"} in / ${mu.outputTokens ?? "?"} out]`);
  report(
    `\n## Round ${i} — Maker (${MAKER_MODEL})\n\n${draft}\n\n` +
    `> tokens: ${mu.inputTokens ?? "?"} in / ${mu.outputTokens ?? "?"} out\n`,
  );

  // 2) REVIEW — a fresh, stateless call. It sees only the draft and is told,
  //    via the system instruction, that it did not write it.
  console.log(`\n---------- ROUND ${i} — REVIEWER (${REVIEWER_MODEL}, fresh context) ----------`);
  const reviewResult = openrouter.callModel({
    model: REVIEWER_MODEL,
    plugins: [{id: "fusion", model: FUSION_JUDGE_MODEL, analysis_models: FUSION_ANALYSIS_MODELS}],
    instructions: REVIEWER_INSTRUCTIONS,
    input:
      `Here is a draft decision framework for choosing a cataract surgery clinic:\n\n` +
      `<draft>\n${draft}\n</draft>\n\n` +
      `Attack it adversarially. If — and only if — it is genuinely complete and ` +
      `rigorous, reply with exactly [DONE] and nothing else. Otherwise, list the ` +
      `most important weaknesses or omissions and explain why they matter.`,
    stopWhen: [stepCountIs(1), maxCost(MAX_COST_PER_CALL)],
  });
  const critique = await reviewResult.getText();
  console.log(critique);
  const ru = await track(reviewResult);
  console.log(`\n[reviewer: ${ru.inputTokens ?? "?"} in / ${ru.outputTokens ?? "?"} out]`);
  report(
    `\n### Round ${i} — Reviewer (${REVIEWER_MODEL}, fresh context)\n\n${critique}\n\n` +
    `> tokens: ${ru.inputTokens ?? "?"} in / ${ru.outputTokens ?? "?"} out\n`,
  );

  // 3) GATE — the independent reviewer decides whether we're done.
  if (critique.includes("[DONE]")) {
    console.log(`\n✅ Reviewer passed the draft on round ${i}.`);
    report(`\n**Reviewer passed the draft on round ${i}.**\n`);
    break;
  }

  // 4) FEED BACK — hand the critique to the maker for the next revision.
  makerMessages.push({ role: "assistant", content: draft });
  makerMessages.push({
    role: "user",
    content:
      `An independent reviewer (who did not write your draft) raised this:\n\n` +
      `${critique}\n\nRevise the framework to fix the most important issue.`,
  });

  if (i === MAX_ITERATIONS) {
    console.log(`\n⛔ Hit the ceiling (${MAX_ITERATIONS} rounds) with no pass. Stopping.`);
    report(`\n**Hit the ceiling (${MAX_ITERATIONS} rounds) with no pass.**\n`);
  }
}

console.log(`\n----- totals: ${totalIn} input + ${totalOut} output tokens (two calls per round) -----`);
report(`\n## Totals\n\n${totalIn} input + ${totalOut} output tokens (two calls per round)\n`);
