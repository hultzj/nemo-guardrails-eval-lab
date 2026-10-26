// Logs a lab user in once, never on camera, and saves the browser session for the recordings.
// Usage: LAB_USER=user1 LAB_PASSWORD_FILE=~/.lab-rec/user1.pw DOMAIN=apps.<cluster> node login.mjs <state.json>
// The session file is a credential: it is written mode 600, outside the repository.
import fs from "node:fs";
import { chromium } from "playwright";

const [stateFile] = process.argv.slice(2);
const user = process.env.LAB_USER, domain = process.env.DOMAIN;
const password = fs.readFileSync(process.env.LAB_PASSWORD_FILE, "utf8").trim();
if (!stateFile || !user || !domain || !password) throw new Error("state file, LAB_USER, DOMAIN and LAB_PASSWORD_FILE are required");
const ns = `${user}${process.env.SUFFIX || "-guardrails"}`;

const browser = await chromium.launch();
const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1600, height: 900 } });
const page = await context.newPage();

// Whatever OpenShift OAuth asks for, in any order: identity provider, Keycloak form, authorize page.
async function signIn(done) {
  for (let i = 0; i < 20; i++) {
    if (await done.isVisible().catch(() => false)) return;
    const idp = page.getByRole("link", { name: /^keycloak$/i });
    const form = page.locator("#kc-form-login #username");
    const allow = page.getByRole("button", { name: /Allow selected permissions/i });
    if (await idp.isVisible().catch(() => false)) await idp.click();
    else if (await form.isVisible().catch(() => false)) {
      await form.fill(user);
      await page.locator("#password").fill(password);
      await page.locator("#kc-login, [type=submit]").first().click();
    } else if (await allow.isVisible().catch(() => false)) await allow.click();
    await page.waitForTimeout(1500);
  }
  await done.waitFor({ timeout: 60000 });
}

await page.goto(`https://rh-ai.${domain}/`);
await signIn(page.getByRole("link", { name: /Projects|Data science projects/i }).first());
// The first-visit tour modal: dismissed here, off camera (the dashboard remembers it).
const close = page.getByRole("button", { name: /^Close$/ });
if (await close.isVisible({ timeout: 5000 }).catch(() => false)) await close.click();
console.log("dashboard:", await page.title());

await page.goto(`https://rh-ai.${domain}/notebook/${ns}/guardrails-lab/lab`);
await signIn(page.locator("#jp-main-dock-panel, .jp-LabShell").first());
console.log("workbench: JupyterLab loaded");

await page.goto(`https://rails-monitor-${ns}.${domain}/`);
await signIn(page.locator("#decisions"));
console.log("monitor: loaded");

await context.storageState({ path: stateFile });
fs.chmodSync(stateFile, 0o600);
await browser.close();
