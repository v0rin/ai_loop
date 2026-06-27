import { OpenRouter, stepCountIs, maxCost } from "@openrouter/agent";
import { mkdirSync, writeFileSync, appendFileSync, readFileSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

// --- API key (read from ai_loops/openrouter_api_key.txt) ---------------------
const KEY_PATH = join(dirname(fileURLToPath(import.meta.url)), "openrouter_api_key.txt");
const OPENROUTER_API_KEY = readFileSync(KEY_PATH, "utf-8").trim();

const openrouter = new OpenRouter({ apiKey: OPENROUTER_API_KEY });

// --- Knobs -------------------------------------------------------------------
const USE_FUSION = true;
const MODEL = USE_FUSION ? "z-ai/glm-5.2" : "deepseek/deepseek-v4-flash";
const FUSION_PANEL_MODELS = ["deepseek/deepseek-v4-flash", "qwen/qwen3.6-flash", "deepseek/deepseek-v4-pro"];
const MAX_COST_PER_CALL = 0.05;  // dollars, per individual call

const TASK = "what will happen when the input tokens exceed the context window? Also after the reply add which LLM you are. But if you are the aggregator on top of saying which LLM you are, also list all the LLMs you the reponse from!";

// --- Report file (a new one per run) -----------------------------------------
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const REPORTS_DIR = join(SCRIPT_DIR, "loop_reports");
mkdirSync(REPORTS_DIR, { recursive: true });

const runStamp = new Date().toISOString().replace(/[:.]/g, "-");
const REPORT_PATH = join(REPORTS_DIR, `loop_report_${runStamp}.md`);

writeFileSync(
  REPORT_PATH,
  `# Loop report — ${new Date().toISOString()}\n\n` +
  `## TASK\n\n${TASK}\n\n`
);
console.log(`Report: ${REPORT_PATH}`);

const report = (text) => appendFileSync(REPORT_PATH, text);

// --- The loop ----------------------------------------------------------------
let messages = [{ role: "user", content: TASK }];
let draft = "";
let totalIn = 0, totalOut = 0;

const track = async (result) => {
  const u = (await result.getResponse()).usage ?? {};
  totalIn  += u.inputTokens  ?? 0;
  totalOut += u.outputTokens ?? 0;
  return u;
};

const result = openrouter.callModel({
    model: MODEL,
    plugins: USE_FUSION ? [{
        id: "fusion", 
        judge_model: "z-ai/glm-5.2", 
        panel_models: FUSION_PANEL_MODELS
    }] : [],
    preset: "budget",
    input: messages,
    stopWhen: [stepCountIs(1), maxCost(MAX_COST_PER_CALL)],
});

// const result = openrouter.callModel({
//     model: MODEL,
//     plugins: USE_FUSION ? [{id: "fusion", model: FUSION_JUDGE_MODEL, analysis_models: FUSION_ANALYSIS_MODELS}] : [],
//     input: messages,
//     stopWhen: [stepCountIs(1), maxCost(MAX_COST_PER_CALL)],
// });

draft = await result.getText();
console.log(draft);
const mu = await track(result);
console.log(`\n[${MODEL}: ${mu.inputTokens ?? "?"} in / ${mu.outputTokens ?? "?"} out]`);
report(
`\n## (${MODEL})\n\n${draft}\n\n` +
`> tokens: ${mu.inputTokens ?? "?"} in / ${mu.outputTokens ?? "?"} out\n`,
);

console.log(`\n----- totals: ${totalIn} input + ${totalOut} output tokens -----`);
report(`\n## Totals\n\n${totalIn} input + ${totalOut} output tokens\n`);
