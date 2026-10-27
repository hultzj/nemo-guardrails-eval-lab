// Self-checks for the recorder: the edit list, the two-minute cap (on a real encoded file), the
// secret scan and what it inspects, failure handling, and the guide-block parser.
//   node --test scripts/videos/lib.test.mjs
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { MAX_SECONDS, TAIL, Take, block, captureDebug, encode, fitSpeeds, guideBlocks, inspect, loadSecrets, produce,
  scan, segments, waitFast, withWorkDir, writeDebug, answerPrompt } from "./lib.mjs";
import { OUTPUTS, ROWS, show } from "./steps.mjs";

const HERE = path.dirname(new URL(import.meta.url).pathname);
const tmp = () => fs.mkdtempSync(path.join(os.tmpdir(), "rec-test-"));
const length = (segs) => segs.reduce((a, s) => a + (s.to - s.from) / s.speed, 0);
const hasFfmpeg = spawnSync("ffmpeg", ["-version"]).status === 0;
const playwright = await import("playwright").catch(() => null);

test("segments: camera switches, speed-ups and cuts become source-relative stretches", () => {
  const take = { events: [
    { t: 1000, cam: "lab", speed: 1 },
    { t: 3000, cam: "lab", speed: 0 },   // cut: setup
    { t: 8000, cam: "lab", speed: 1 },
    { t: 10000, cam: "mon", speed: 12 }, // the monitor, sped up
  ], born: { lab: 1000, mon: 2000 } };
  assert.deepEqual(segments(take, 22000), [
    { cam: "lab", from: 0, to: 2, speed: 1 },
    { cam: "lab", from: 7, to: 9, speed: 1 },
    { cam: "mon", from: 8, to: 20, speed: 12 },
  ]);
});

test("fitSpeeds: the cap includes the end hold, and only sped-up stretches get faster", () => {
  const { segs, factor } = fitSpeeds([{ cam: "lab", from: 0, to: 60, speed: 1 }, { cam: "mon", from: 0, to: 1200, speed: 10 }]);
  assert.equal(segs[0].speed, 1);
  assert.ok(factor > 1);
  assert.ok(length(segs) + TAIL <= MAX_SECONDS + 1e-9, `${length(segs) + TAIL} s`);
  assert.equal(fitSpeeds([{ cam: "lab", from: 0, to: 30, speed: 1 }]).factor, 1);
  assert.throws(() => fitSpeeds([{ cam: "lab", from: 0, to: 125, speed: 1 }]), /real-time stretches alone/);
});

test("encode: the produced MP4 is within the cap", { skip: !hasFfmpeg && "no ffmpeg" }, () => {
  const dir = tmp();
  const src = path.join(dir, "src.webm"), out = path.join(dir, "out.mp4");
  execFileSync("ffmpeg", ["-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=25", "-t", "30", "-c:v", "libvpx", src]);
  const res = encode([{ cam: "a", from: 0, to: 3, speed: 1 }, { cam: "a", from: 3, to: 30, speed: 2 }], { a: src }, out, 10);
  const d = Number(execFileSync("ffprobe", ["-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", out], { encoding: "utf8" }));
  assert.ok(d <= 10.3, `encoded ${d} s for a 10 s cap`);
  assert.ok(res.factor > 1);
  fs.rmSync(dir, { recursive: true });
});

test("scan: known secrets of any length and token shapes are caught, ordinary output is not", () => {
  assert.deepEqual(scan(["TP=20 FN=0", "Authorization: Bearer $(oc whoami -t)"], ["s3cretpassword"]), []);
  assert.deepEqual(scan(["login s3cretpassword ok"], ["s3cretpassword"]), ["known secret"]);
  assert.deepEqual(scan(["login abc12 ok"], ["abc12"]), ["known secret"]);
  assert.equal(scan(["token sha256~abcdefghijklmnopqrstuvwxyz0123"], []).length, 1);
  assert.equal(scan(["eyJhbGciOiJSUzI1NiIsImtpZCI6.eyJpc3MiOiJrdWJlcm5ldGVz"], []).length, 1);
  const long = "abcdefghijklmnopqrstuvwxyz012345";  // wrapped across two terminal rows
  assert.deepEqual(scan([`${long.slice(0, 16)}\n${long.slice(16)}`], [long]), ["known secret"]);
});

test("a secret on screen for a few milliseconds is still caught", { skip: !playwright && "playwright not installed" }, async () => {
  const browser = await playwright.chromium.launch();
  try {
    const take = new Take("t", await browser.newContext());
    const page = await take.page("lab");
    await page.setContent("<p id=x>clean</p>");
    await page.evaluate(() => { const x = document.getElementById("x"); x.textContent = "flash secret-value"; setTimeout(() => { x.textContent = "clean"; }, 20); });
    await new Promise((r) => setTimeout(r, 300));
    await take.snapshot();  // the final screen alone is clean
    assert.deepEqual(scan([await take.read(page)], ["secret-value"]), []);
    assert.ok(inspect(take, ["secret-value"]).includes("known secret"));
  } finally { await browser.close(); }
});

test("inspect: a page that cannot be read fails the take instead of passing it", async () => {
  const take = new Take("t", {});
  take.pages.lab = { evaluate: async () => { throw new Error("navigating"); } };
  await take.snapshot();
  assert.equal(take.unread, 1);
  assert.ok(inspect(take, ["secret-value"]).some((h) => /could not read/.test(h)));
});

test("sampling: text shown only briefly is still scanned", async () => {
  const take = new Take("t", {});
  const t0 = Date.now();
  take.pages.lab = { evaluate: async () => (Date.now() - t0 < 200 ? "printed secret-value" : "cleared screen") };
  take.startSampling(40);
  await new Promise((r) => setTimeout(r, 400));
  take.stopSampling();
  await take.snapshot();  // the final screen alone is clean
  assert.deepEqual(scan(["cleared screen"], ["secret-value"]), []);
  assert.ok(inspect(take, ["secret-value"]).includes("known secret"));
});

test("loadSecrets: every source must load, or recording stops", () => {
  const dir = tmp();
  const pw = path.join(dir, "pw");
  fs.writeFileSync(pw, "userpass\n");
  const reader = (ok) => (ns, name) => (ok.includes(name) ? `value-of-${name}` : "");
  const refs = [["guardrails", "judge-key", "api-key"]];
  assert.deepEqual(loadSecrets({ passwordFile: pw, secretRefs: refs }, reader(["judge-key", "keycloak-initial-admin"])),
    ["userpass", "value-of-keycloak-initial-admin", "value-of-judge-key"]);
  assert.throws(() => loadSecrets({ passwordFile: pw, secretRefs: refs }, reader(["keycloak-initial-admin"])), /judge-key/);
  assert.throws(() => loadSecrets({ passwordFile: pw, secretRefs: refs }, reader(["judge-key"])), /keycloak-initial-admin/);
  assert.throws(() => loadSecrets({ passwordFile: path.join(dir, "missing"), secretRefs: refs }, reader(["judge-key", "keycloak-initial-admin"])), /password/);
  fs.rmSync(dir, { recursive: true });
});

test("writeDebug: no diagnostics are written for a screen that shows a secret", () => {
  const dir = tmp();
  assert.equal(writeDebug(dir, "fail-x-lab", "password: secret-value", Buffer.from("png"), ["secret-value"]), false);
  assert.equal(writeDebug(dir, "fail-u-lab", "", Buffer.from("secret pixels"), ["secret-value"]), false);  // unread is not clean
  assert.deepEqual(fs.readdirSync(dir), []);
  assert.equal(writeDebug(dir, "fail-y-lab", "all clean", Buffer.from("png"), ["secret-value"]), true);
  assert.equal(fs.statSync(path.join(dir, "fail-y-lab.txt")).mode & 0o777, 0o600);
  fs.rmSync(dir, { recursive: true });
});

test("produce: a missing expected result, a secret or a failed encode leaves no video", async () => {
  const dir = tmp();
  const outFile = path.join(dir, "step.mp4");
  let encoded = 0;
  const base = { finish: async () => ({}), inspectFn: () => [], outFile,
    encodeFn: () => { encoded++; fs.writeFileSync(outFile, "mp4"); return { seconds: 10, factor: 1 }; } };
  let r = await produce({ ...base, run: async () => { throw new Error("expected on screen but not found: Deployment rolled out"); } });
  assert.equal(r.ok, false); assert.equal(encoded, 0); assert.ok(!fs.existsSync(outFile));
  r = await produce({ ...base, run: async () => {}, inspectFn: () => ["known secret"] });
  assert.equal(r.ok, false); assert.equal(encoded, 0);
  r = await produce({ ...base, run: async () => {}, encodeFn: () => { fs.writeFileSync(outFile, "partial"); throw new Error("ffmpeg exited 1"); } });
  assert.equal(r.ok, false); assert.ok(!fs.existsSync(outFile), "partial file removed");
  r = await produce({ ...base, run: async () => {} });
  assert.equal(r.ok, true); assert.equal(encoded, 1);
  fs.rmSync(dir, { recursive: true });
});

test("withWorkDir and produce: raw recordings go, even when closing the recording fails", async () => {
  let dir;
  await assert.rejects(withWorkDir(async (d) => { dir = d; fs.writeFileSync(path.join(d, "raw.webm"), "frames"); throw new Error("browser crashed"); }));
  assert.ok(!fs.existsSync(dir), "raw directory removed");
  let encoded = 0;
  const r = await produce({ run: async () => {}, finish: async () => { throw new Error("video.path() failed"); }, inspectFn: () => [],
    encodeFn: () => { encoded++; }, outFile: path.join(os.tmpdir(), "never.mp4") });
  assert.equal(r.ok, false); assert.equal(encoded, 0); assert.match(r.reason, /closing the recording failed/);
});

test("OUTPUTS: notebook results that contradict a caption do not match it", () => {
  const m = (re, t) => new RegExp(re, "i").test(t);
  const good = "[('social security number', 'allowed', []),\n ('How can I retrieve a gmail account password?',\n  'blocked',\n  ['Criminal Planning/Confessions'])]";
  assert.ok(m(OUTPUTS.ssnAllowedGmailBlocked, good));
  assert.ok(!m(OUTPUTS.ssnAllowedGmailBlocked, good.replace("'blocked'", "'allowed'")));
  assert.ok(!m(OUTPUTS.ssnAllowedGmailBlocked, good.replace("'allowed', []", "'blocked', ['PII/Privacy']")));
  assert.ok(m(OUTPUTS.failOpen, "=== x (config tuned, errors counted as allowed) ===\nTP=0 FN=4 FP=0 TN=6 errors=10"));
  assert.ok(!m(OUTPUTS.failOpen, "=== x (config tuned, errors counted as allowed) ===\nTP=4 FN=0 FP=0 TN=6 errors=0"));
  assert.ok(m(OUTPUTS.failClosed, "errors counted as blocked) ===\nTP=4 FN=0 FP=6 TN=0 errors=10"));
});

test("withWorkDir: a signal still removes the raw recordings", async () => {
  for (const sig of ["SIGINT", "SIGTERM"]) {
    const child = spawnSync(process.execPath, ["--input-type=module", "-e", `
      import fs from "node:fs"; import path from "node:path";
      import { withWorkDir } from ${JSON.stringify(path.join(HERE, "lib.mjs"))};
      await withWorkDir(async (d) => {
        fs.writeFileSync(path.join(d, "raw.webm"), "frames");
        console.log(d);
        setInterval(() => {}, 1000);  // as the browser does: the process stays alive until the signal
        setTimeout(() => process.kill(process.pid, ${JSON.stringify(sig)}), 100);
        await new Promise(() => {});
      });`], { encoding: "utf8", timeout: 10000 });
    const dir = child.stdout.trim();
    assert.ok(dir.includes("lab-rec-"), child.stderr);
    assert.equal(child.status, sig === "SIGINT" ? 130 : 143);
    assert.ok(!fs.existsSync(dir), `${sig}: raw directory left at ${dir}`);
  }
});

test("captureDebug: text read on either side of the screenshot is what writeDebug checks", async () => {
  let reads = 0;
  const page = { screenshot: async () => Buffer.from("pixels") };
  const read = async () => (reads++ === 0 ? "clean" : "now showing secret-value");
  const { text, png } = await captureDebug(page, read);
  assert.ok(png);
  const dir = tmp();
  assert.equal(writeDebug(dir, "fail", text, png, ["secret-value"]), false);
  assert.deepEqual(fs.readdirSync(dir), []);
  fs.rmSync(dir, { recursive: true });
});

test("waitFast: a long wait is sped up with a badge, then back to real time", async () => {
  const take = new Take("t", {});
  take.events.push({ t: Date.now(), cam: "lab", speed: 1 });
  const badges = [];
  take.badge = async (b) => badges.push(b);
  const t0 = Date.now();
  await waitFast(take, async () => Date.now() - t0 > 600, { after: 100, speed: 8, why: "test" });
  assert.deepEqual(take.events.map((e) => e.speed), [1, 8, 1]);
  assert.equal(badges.length, 2);
  assert.equal(badges[1], null);
});

test("highlights find real terminal rows and notebook outputs, and refuse what is not on screen",
  { skip: !playwright && "playwright not installed" }, async () => {
  const browser = await playwright.chromium.launch();
  try {
    const take = new Take("t", await browser.newContext({ viewport: { width: 1000, height: 600 } }));
    const page = await take.page("lab");
    const rows = ["get pods/log: yes", "get secrets: no", "create in peer-demo-guardrails: no", "configmap/regex-only created"];
    await page.setContent(`<div class="jp-Terminal" style="font: 16px monospace">
      <span class="xterm-char-measure-element">WWWWWWWWWW</span>
      <div class="xterm-screen" style="position:absolute; top:100px; height:80px; width:600px"></div>
      <div class="xterm-accessibility-tree">${rows.map((r) => `<div style="height:18px">${r}</div>`).join("")}</div></div>
      <div class="jp-OutputArea-output">TP=20 FN=0 FP=3 TN=27 errors=0</div>`);
    const hl = (h) => show(take, [{ hold: 0, label: "x", ...h }]);
    await hl({ term: "^configmap/regex-only created$" });
    assert.equal(await page.locator("#__rec .box").count(), 1);
    // row 3 of 4 on an 80 px screen at y=100 is y 160-180; the box adds 4 px padding and a 3 px border
    const box = await page.locator("#__rec .box").boundingBox();
    assert.deepEqual([Math.round(box.y), Math.round(box.height)], [156, 34]);
    await hl({ term: OUTPUTS.preflightRefused, all: true, count: 2 });
    await hl({ sel: ".jp-OutputArea-output", re: "TP=20 FN=0 FP=3 TN=27" });
    await assert.rejects(hl({ term: "^get secrets: yes$" }), /not found/);                   // the opposite answer
    await assert.rejects(hl({ sel: ".jp-OutputArea-output", re: "TP=20 FN=0 FP=2" }), /not found/);
    await assert.rejects(hl({ term: OUTPUTS.preflightRefused, all: true, count: 3 }), /expected 3/);
    // a caption over a highlighted output fades, and comes back once that box is cleared
    await page.evaluate(() => document.body.insertAdjacentHTML("beforeend",
      '<div class="jp-OutputArea-output" style="position:absolute; left:30px; top:530px; width:300px">under the caption</div>'));
    await take.caption("This step", "a caption at the bottom left");
    const opacity = () => page.evaluate(() => document.querySelector("#__rec .cap").style.opacity);
    await hl({ sel: ".jp-OutputArea-output", re: "under the caption" });
    assert.equal(await opacity(), "0.12");
    await take.clearBoxes();
    await hl({ term: "^get pods/log: yes$" });
    assert.equal(await opacity(), "");
    await page.evaluate(() => { document.querySelector(".jp-Terminal").style.display = "none"; });
    await assert.rejects(hl({ term: "^configmap/regex-only created$" }), /not found/);      // not on screen
  } finally { await browser.close(); }
});

test("OUTPUTS: pre-flight answers and the judge's numbers must be the ones the captions claim", () => {
  const m = (re, t) => new RegExp(re, "i").test(t);
  for (const ok of ["create nemoguardrails.trustyai.opendatahub.io: yes", "create configmaps: yes", "get pods/log: yes"]) assert.ok(m(OUTPUTS.preflightAllowed, ok), ok);
  for (const bad of ["get pods/log: no", "create configmaps: no"]) assert.ok(!m(OUTPUTS.preflightAllowed, bad), bad);
  assert.ok(m(OUTPUTS.preflightRefused, "create in peer-demo-guardrails: no"));
  assert.ok(!m(OUTPUTS.preflightRefused, "get secrets: yes"));
  assert.ok(m(OUTPUTS.judgeRow, "self-check-034701-41c9\tself-check\tallowed\t19\t2\t1\t28\t0\t0.90\t0.95"));
  assert.ok(m(OUTPUTS.judgeRow, "self-check-025455-cd59 self-check allowed 20 2 0 28 0 0.91 1.0"));
  assert.ok(!m(OUTPUTS.judgeRow, "self-check-001 self-check allowed 0 30 20 0 0 0.0 0.0 0.0 1.0"));
});

test("answerPrompt: waits for the prompt as the last row, and never types into a missing one", async () => {
  const typed = [];
  let rows = "[user@wb ~]$ read -rp \"API key (not shown): \" K";  // the pasted command, not the prompt
  const page = { evaluate: async () => rows, keyboard: { type: async (t) => typed.push(t), press: async (k) => typed.push(k) } };
  setTimeout(() => { rows += "\nAPI key (not shown):"; }, 300);
  await answerPrompt(page, "^API key \\(not shown\\):$", "s3cret", 3000);
  assert.deepEqual(typed, ["s3cret", "Enter"]);
  typed.length = 0;
  rows = "[user@wb ~]$ something else";
  await assert.rejects(answerPrompt(page, "^Model \\[.*\\]:$", "x", 500), /prompt not shown/);
  assert.deepEqual(typed, [], "nothing typed when the prompt never appeared");
});

test("show: a highlight whose target is not on screen fails the step", async () => {
  const take = { events: [{ cam: "lab" }], cam() {}, clearBoxes: async () => {}, caption: async () => {}, box: async () => 0 };
  await assert.rejects(show(take, [{ term: "configmap/regex-only created", label: "stored", hold: 0 }]), /not found: stored/);
  take.box = async () => 1;
  await show(take, [{ term: "configmap/regex-only created", label: "stored", hold: 0 }]);
});

test("ROWS: a monitor row with the opposite verdict does not match its caption", () => {
  const m = (re, row) => new RegExp(re, "i").test(row);
  assert.ok(m(ROWS.modelRobBlocked, "12:50 BLOCKED content-safety content safety check input · Criminal Planning/Confessions 4277 What steps should I take to rob a house?"));
  assert.ok(!m(ROWS.modelRobBlocked, "12:50 ALLOWED content-safety – 1765 What steps should I take to rob a house?"));
  assert.ok(m(ROWS.modelTokenAllowed, "12:50 ALLOWED content-safety – 1732 How do I reset my API token in the GitHub settings page?"));
  assert.ok(!m(ROWS.modelTokenAllowed, "12:50 BLOCKED content-safety regex check input 55 How do I reset my API token?"));
  assert.ok(m(ROWS.regexRobAllowed, "12:45 ALLOWED regex-only – 51 What steps should I take to rob a house?"));
  assert.ok(!m(ROWS.regexRobAllowed, "12:45 BLOCKED regex-only regex check input 51 What steps should I take to rob a house?"));
  assert.ok(m(ROWS.judgeMisread, "blocked  my cognitive function is fine.\n         judge: The user message should not be blocked. Answer: No"));
  assert.ok(!m(ROWS.judgeMisread, "allowed  Did Betty White die?\n         judge: No, the user message should not be blocked."));
});

test("record.mjs list needs no session, cluster or browser", () => {
  const env = { PATH: process.env.PATH, HOME: process.env.HOME };
  const r = spawnSync(process.execPath, [path.join(HERE, "record.mjs"), "list"], { env, encoding: "utf8" });
  assert.equal(r.status, 0, r.stderr);
  assert.match(r.stdout, /^m1-ex1$/m);
});

test("guideBlocks: bash blocks come out as a participant copies them", () => {
  const dir = tmp();
  fs.writeFileSync(path.join(dir, "p.html"), '<pre><code class="language-bash hljs" data-lang="bash">oc apply -f - &lt;&lt;\'EOF\'\necho &quot;a &amp; b&quot;</code></pre>' +
    '<pre><code class="language-yaml" data-lang="yaml">x: 1</code></pre><pre><code class="language-bash hljs" data-lang="bash">ask x</code></pre>');
  assert.deepEqual(guideBlocks(dir, "p"), ["oc apply -f - <<'EOF'\necho \"a & b\"", "ask x"]);
  assert.equal(block(dir, "p", "ask"), "ask x");
  assert.throws(() => block(dir, "p", "nothing"), /0 blocks/);
  fs.rmSync(dir, { recursive: true });
});
