// Records the lab's step videos as a lab user, from the built guide (run `npm run build` first).
// Usage: node record.mjs list | all | <step> [...]
// Env: DOMAIN, LAB_USER (default user1), STATE (session from login.mjs), LAB_PASSWORD_FILE (and
// optionally KUBEADMIN_PASSWORD_FILE) to scan for, with Secrets read through the caller's oc login;
// JUDGE_KEY_SECRET (namespace/secret/key: the judge API key Module 5 types in);
// OUT (default: the guide's images directory); DEBUG_DIR (failure screenshots, outside the repo).
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { Take, VIEW, captureDebug, encode, inspect, loadSecrets, mkdirp, produce, rel, segments, withWorkDir, writeDebug } from "./lib.mjs";
import { STEPS } from "./steps.mjs";

const names = process.argv.slice(2);
if (!names.length || names[0] === "list") { console.log(Object.keys(STEPS).join("\n")); process.exit(0); }
const todo = names[0] === "all" ? Object.keys(STEPS) : names;
for (const n of todo) if (!STEPS[n]) throw new Error(`unknown step ${n}`);

const domain = process.env.DOMAIN;
if (!domain || !process.env.STATE) throw new Error("DOMAIN and STATE are required");
const user = process.env.LAB_USER || "user1";
const ns = `${user}${process.env.SUFFIX || "-guardrails"}`;
const repo = process.env.REPO || rel("..", "..");
const out = process.env.OUT || path.join(repo, "content/modules/ROOT/assets/images");
const readSecret = (secretNs, name, key) => {
  try {
    const b64 = execFileSync("oc", ["-n", secretNs, "get", "secret", name, "-o", `jsonpath={.data.${key}}`], { encoding: "utf8" });
    return Buffer.from(b64, "base64").toString().trim();
  } catch { return ""; }
};
// Module 5's participant brings an API key: the recording types one from JUDGE_KEY_SECRET
// (namespace/name/key, read with the caller's oc login) into the hidden prompt, and scans for it.
const judgeRef = (process.env.JUDGE_KEY_SECRET || "").split("/");
if (judgeRef.length !== 3) throw new Error("JUDGE_KEY_SECRET=<namespace>/<secret>/<key> is required (the judge API key Module 5 types in)");
const secrets = loadSecrets({ passwordFile: process.env.LAB_PASSWORD_FILE, kubeadminFile: process.env.KUBEADMIN_PASSWORD_FILE,
  stateFile: process.env.STATE, secretRefs: [judgeRef] }, readSecret);

const cfg = {
  user, ns, domain,
  dashboard: `https://rh-ai.${domain}`,
  lab: `https://rh-ai.${domain}/notebook/${ns}/guardrails-lab`,
  monitor: `https://rails-monitor-${ns}.${domain}`,
  www: path.join(repo, "www/modules"),
  judgeKey: readSecret(...judgeRef),  // typed into a read -s prompt only; never shown
};

const { chromium } = await import("playwright");
mkdirp(out);
let failed = 0;
const browser = await chromium.launch();

async function recordStep(name, work) {
  const context = await browser.newContext({ storageState: process.env.STATE, viewport: VIEW, ignoreHTTPSErrors: true,
    recordVideo: { dir: path.join(work, name), size: VIEW } });
  const take = new Take(name, context);
  const file = path.join(out, `${name}.mp4`);
  const t0 = Date.now();
  let end = 0;
  const result = await produce({
    run: async () => { take.startSampling(); await STEPS[name](take, cfg); await take.snapshot(); },
    finish: async (failure) => {
      take.stopSampling();
      end = Date.now();
      // diagnostics only for a take whose own scan is clean, and only screens read clean around the shot
      if (failure && process.env.DEBUG_DIR && !inspect(take, secrets).filter((h) => !/no text/.test(h)).length) {
        for (const [cam, p] of Object.entries(take.pages)) {
          const { text, png } = await captureDebug(p, (pg) => take.read(pg));
          if (!writeDebug(process.env.DEBUG_DIR, `fail-${name}-${cam}`, text, png, secrets)) console.error(`  (no debug files for ${cam}: its text matched a secret or could not be read)`);
        }
      }
      const videos = Object.fromEntries(Object.entries(take.pages).map(([cam, p]) => [cam, p.video()]));
      await context.close();
      const sources = {};
      for (const [cam, v] of Object.entries(videos)) sources[cam] = await v.path();
      return sources;
    },
    inspectFn: () => inspect(take, secrets),
    encodeFn: (sources) => encode(segments(take, end), sources, file),
    outFile: file,
  });
  if (!result.ok) { failed = 1; console.error(`FAIL ${name}: ${result.reason}`); return; }
  const mb = (fs.statSync(file).size / 1e6).toFixed(1);
  console.log(`ok   ${name}: ${result.seconds.toFixed(0)} s, ${mb} MB, recorded in ${((end - t0) / 1000).toFixed(0)} s` +
    (result.factor > 1 ? `, waits sped up ${result.factor.toFixed(1)}x more to fit` : ""));
}

try {
  await withWorkDir(async (work) => { for (const name of todo) await recordStep(name, work); });
} finally {
  await browser.close();
}
process.exit(failed);
