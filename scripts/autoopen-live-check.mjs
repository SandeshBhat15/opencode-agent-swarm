#!/usr/bin/env node
/**
 * Bounded live check: does the dashboard really open in this machine's browser?
 *
 * Unit tests inject the opener, so they prove the decision, never the launch.
 * This script uses the real opener and then waits for the evidence that only a
 * real browser can produce: a client fetching the page and holding its SSE
 * stream open. That is the difference between "we called start" and "a window
 * rendered it".
 *
 * It is deliberately strict about what it claims:
 *   launcher  - the platform launcher process actually started (observable here)
 *   browser   - a browser fetched the page and its script connected (observable here)
 *   visible   - whether a human can see the window (NOT observable; never claimed)
 *
 * Budget: 45 s total, then it stops and reports what it saw. Exit code 0 only
 * when a real browser client connected. It never prints the capability token or
 * the launch ticket.
 *
 *   node scripts/autoopen-live-check.mjs
 */

import { spawnSync } from "node:child_process";

import { createDashboard } from "../src/dashboard/index.js";

const BUDGET_MS = 45000;
const OPEN_WAIT_MS = 15000;
const CLIENT_WAIT_MS = 40000;

const startedAt = Date.now();
const report = {
  launcher: { attempted: false, opened: false, error: null, origin: null },
  browser: { connected: false, clients: 0, afterMs: null },
  defaultBrowser: { progId: null, error: null },
  visible: "not-observable",
  notes: [],
};

function remaining() {
  return Math.max(0, BUDGET_MS - (Date.now() - startedAt));
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, Math.min(ms, remaining())));
}

/** Which browser Windows will actually use. Best effort; a failure is reported, not invented. */
function readDefaultBrowser() {
  if (process.platform !== "win32") {
    report.defaultBrowser.error = `not implemented for ${process.platform}`;
    return;
  }
  const key = "HKCU\\Software\\Microsoft\\Windows\\Shell\\Associations\\UrlAssociations\\http\\UserChoice";
  const result = spawnSync("reg.exe", ["query", key, "/v", "ProgId"], { encoding: "utf8", shell: false, windowsHide: true });
  if (result.error) {
    report.defaultBrowser.error = String(result.error.code ?? result.error.message);
    return;
  }
  if (result.status !== 0) {
    report.defaultBrowser.error = `reg.exe exited ${result.status}`;
    return;
  }
  const match = /ProgId\s+REG_SZ\s+(\S+)/.exec(result.stdout ?? "");
  report.defaultBrowser.progId = match ? match[1] : null;
  if (!match) report.defaultBrowser.error = "ProgId not found in the registry output";
}

async function main() {
  readDefaultBrowser();

  const dashboard = createDashboard({ config: {} });
  const info = await dashboard.start();
  report.dashboard = { running: info.running, host: info.host, port: info.port, enable: info.enabled, autoOpenEnabled: info.autoOpen.enabled };
  report.launcher.attempted = true;
  report.launcher.origin = info.autoOpen.origin;

  const opened = await Promise.race([dashboard.opened(), sleep(OPEN_WAIT_MS).then(() => ({ timeout: true }))]);
  report.launcher.opened = opened?.opened === true;
  report.launcher.error = opened?.error ?? (opened?.timeout ? "timed out waiting for the launcher" : null);
  report.launcher.origin = opened?.origin ?? report.launcher.origin;

  if (report.launcher.opened) {
    const deadline = Math.min(CLIENT_WAIT_MS, remaining());
    const began = Date.now();
    while (Date.now() - began < deadline) {
      const clients = dashboard.info().clients;
      if (clients > 0) {
        report.browser.connected = true;
        report.browser.clients = clients;
        report.browser.afterMs = Date.now() - began;
        break;
      }
      await sleep(250);
    }
    if (!report.browser.connected) {
      report.browser.clients = dashboard.info().clients;
      report.notes.push("the launcher started but no browser client reached the page within the budget");
    }
  }

  await dashboard.stop();
}

try {
  await main();
} catch (cause) {
  report.notes.push(`error: ${String(cause?.message ?? cause)}`);
}

const verdict = report.browser.connected
  ? "browser opened the dashboard and connected"
  : report.launcher.opened
    ? "launcher started, browser not observed"
    : "browser was not opened";

console.log(JSON.stringify({ verdict, elapsedMs: Date.now() - startedAt, ...report }, null, 2));

if (report.browser.connected) {
  process.exit(0);
}
process.exit(report.launcher.opened ? 3 : 1);
