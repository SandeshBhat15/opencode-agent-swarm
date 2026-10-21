/**
 * Open the dashboard in the user's default browser.
 *
 * Constraints, each enforced here rather than by convention:
 *  - no shell: the command and its single argument go to `spawn` as an argv
 *    array with `shell: false`, so no part of the URL is ever interpreted;
 *  - the URL must be one this plugin built for its own loopback server; the
 *    pattern is checked before anything is spawned, so no other URL — and no
 *    `file:`, `javascript:` or remote address — can be opened;
 *  - the child is detached and unref'd, so startup never waits for a browser
 *    and a hung browser cannot keep the host process alive;
 *  - failures are returned as data, never thrown: the dashboard server keeps
 *    running and the caller decides what to say.
 *
 * The launch URL carries a single-use ticket, never the capability token, so
 * the argv a local process can read contains nothing durable. See
 * `handshake.js`.
 */

import { spawn } from "node:child_process";
import { BIND_HOST } from "./server.js";

/** Characters a dashboard URL can contain. Deliberately excludes `&`, `|`, `;`, `$`, quotes and spaces. */
const PATH_CHARS = "[A-Za-z0-9%._~:@/+=*-]";

const LOOPBACK_URL = new RegExp(`^http://${escapeRegExp(BIND_HOST)}:\\d{1,5}(?:/${PATH_CHARS}*)?(?:\\?${PATH_CHARS}*)?$`);

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function isOpenableUrl(url) {
  return typeof url === "string" && url.length > 0 && url.length <= 2048 && LOOPBACK_URL.test(url);
}

/**
 * The platform's own "open this URL" invocation. macOS and Linux take the URL
 * as an argv entry; Windows needs `start`, whose first quoted argument is a
 * window title, hence the empty string.
 */
export function browserCommand(platform, url) {
  if (platform === "win32") return { command: "cmd.exe", args: ["/d", "/s", "/c", "start", "", url] };
  if (platform === "darwin") return { command: "open", args: [url] };
  return { command: "xdg-open", args: [url] };
}

/**
 * @returns {Promise<{opened: boolean, error?: string}>} `opened` means the
 * platform launcher process actually started — not that a browser rendered
 * anything, which this process cannot see.
 */
export function openInBrowser(url, { platform = process.platform, spawn: spawnImpl = spawn, timeoutMs = 5000 } = {}) {
  return new Promise((resolve) => {
    if (!isOpenableUrl(url)) {
      resolve({ opened: false, error: "refused: not a loopback dashboard URL" });
      return;
    }

    let child;
    try {
      const { command, args } = browserCommand(platform, url);
      child = spawnImpl(command, args, { detached: true, stdio: "ignore", shell: false, windowsHide: true });
    } catch (error) {
      resolve({ opened: false, error: String(error?.code ?? error?.message ?? error) });
      return;
    }

    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        child.removeListener?.("error", onError);
      } catch {
        // a child that is already gone needs no cleanup
      }
      resolve(result);
    };

    const onError = (error) => finish({ opened: false, error: String(error?.code ?? error?.message ?? error) });

    // The launcher is synchronous in practice; the timer only exists so a
    // pathological environment cannot leave this promise pending forever.
    const timer = setTimeout(() => finish({ opened: false, error: "timeout" }), timeoutMs);
    timer.unref?.();

    child.once("error", onError);
    child.once("spawn", () => {
      try {
        child.unref();
      } catch {
        // already detached
      }
      finish({ opened: true });
    });
  });
}
