// Headless-browser check: loads the merchant portal against a host that already has a
// tour run against it, and asserts the DOM actually contains both evidence kinds --
// a sealed kernel receipt and an activity-log id -- rendered as visibly distinct
// components (web/portal/app/components/Evidence.tsx switches CSS class and label
// text on entry.kind, never on note text). This is the check docs/testing.md's
// "no automated check in this repo has watched a browser paint the result" line was
// about; it closes that gap for the portal, which is where the receipt-vs-log
// contrast -- the artifact this repo exists to show -- actually renders.
//
// No API key: run against a host started with .env absent, so `/capabilities`
// reports "unconfigured" and this asserts that panel too.
import { chromium } from "playwright-core";

const PORTAL_URL = process.env.PORTAL_URL ?? "http://localhost:3100";
const STOREFRONT_URL = process.env.STOREFRONT_URL ?? null;

function fail(message) {
  console.error(`FAIL: ${message}`);
  process.exitCode = 1;
}

const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const consoleErrors = [];
  page.on("pageerror", (err) => consoleErrors.push(String(err)));

  // Both apps poll the API to keep commerce state fresh. `networkidle` therefore
  // is not a meaningful readiness signal and can time out even when the page is
  // fully rendered. Navigation proves the document loaded; the selectors below
  // prove the application reached the state this smoke test actually requires.
  await page.goto(PORTAL_URL, { waitUntil: "domcontentloaded" });

  // Live store state loads as soon as a session exists, with no typing -- wait for
  // the two evidence rows the tour run left behind.
  await page.waitForSelector(".evidence.kernel", { timeout: 15_000 });
  await page.waitForSelector(".evidence.log", { timeout: 15_000 });

  const kernelText = await page.locator(".evidence.kernel .evidence-label").first().textContent();
  const logText = await page.locator(".evidence.log .evidence-label").first().textContent();

  if (!kernelText?.includes("Sealed kernel receipt")) {
    fail(`kernel receipt row missing expected label, got: ${kernelText}`);
  }
  if (!logText?.includes("Activity log")) {
    fail(`activity log row missing expected label, got: ${logText}`);
  }
  if (kernelText === logText) {
    fail("kernel and log rows rendered with identical text -- not visibly distinct");
  }

  const bodyText = await page.textContent("body");
  if (!bodyText?.includes("unconfigured")) {
    fail(`expected the unconfigured-assistant panel (no API key in CI), got body without "unconfigured"`);
  }

  if (consoleErrors.length) {
    fail(`uncaught page errors: ${consoleErrors.join("; ")}`);
  }

  if (!process.exitCode) {
    console.log("OK: portal rendered a sealed kernel receipt and an activity-log id, visibly distinct.");
  }

  // The storefront's order history is the cheap equivalent: a fresh browser session
  // has no orders of its own (the tour ran under a separate session id), so this only
  // asserts the panel actually renders live state -- reachable, no CORS/JS errors --
  // rather than the "API not reachable" fallback.
  if (STOREFRONT_URL) {
    const sfErrors = [];
    const sfPage = await browser.newPage();
    sfPage.on("pageerror", (err) => sfErrors.push(String(err)));
    await sfPage.goto(STOREFRONT_URL, { waitUntil: "domcontentloaded" });
    await sfPage
      .waitForSelector(".order-card, .orders-empty", { timeout: 15_000 })
      .catch(() => {});
    const sfBody = await sfPage.textContent("body");
    if (sfBody?.includes("not reachable")) {
      fail("storefront fell back to the unreachable-API panel -- cross-origin call to the host failed");
    }
    const sfOrderCardCount = await sfPage.locator(".order-card").count();
    if (!sfBody?.includes("No orders yet") && sfOrderCardCount === 0) {
      fail(`storefront did not render an orders panel at all, got: ${sfBody?.slice(0, 200)}`);
    }
    if (sfErrors.length) {
      fail(`storefront uncaught page errors: ${sfErrors.join("; ")}`);
    }
    if (!process.exitCode) {
      console.log("OK: storefront rendered live state (orders panel) with no reachability or JS errors.");
    }
  }
} finally {
  await browser.close();
}
