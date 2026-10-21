/**
 * Installer: the one script that edits the user's live configuration.
 *
 * These are black-box tests. Each one runs `scripts/install.mjs --write` against
 * a throwaway config directory (OPENCODE_CONFIG_DIR), so what is asserted is
 * the file left on disk, not an internal helper.
 *
 * The regression this file exists for: inserting `default_agent` after an
 * existing `plugins` array used to splice text in without a separating comma,
 * producing `... ]  "default_agent": "swarm"` — invalid JSON, written to the
 * user's real config. OpenCode then failed to load it.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, mkdir, readFile, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { promisify } from "node:util";

const run = promisify(execFile);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const INSTALLER = path.join(ROOT, "scripts", "install.mjs");

async function sandbox() {
  const dir = await mkdtemp(path.join(tmpdir(), "swarm-install-"));
  return dir;
}

async function install(cfg) {
  return run(process.execPath, [INSTALLER, "--write"], {
    env: { ...process.env, OPENCODE_CONFIG_DIR: cfg },
    windowsHide: true,
  });
}

async function json(file) {
  return JSON.parse(await readFile(file, "utf8"));
}

test("installer: a config that already has plugins keeps its shape and stays valid JSON", async () => {
  const cfg = await sandbox();
  const file = path.join(cfg, "opencode.json");
  await writeFile(
    file,
    `{
  "mcp": {
    "example": { "type": "local", "command": ["node", "x"], "enabled": true }
  },
  "plugins": [
    "left-alone@1.0.0"
  ]
}
`,
    "utf8",
  );

  await install(cfg);
  const config = await json(file); // throws on the old bug

  assert.equal(config.default_agent, "swarm");
  assert.deepEqual(config.plugins, ["left-alone@1.0.0", expectSwarmSpec()]);
  assert.deepEqual(config.mcp.example.command, ["node", "x"]);
});

test("installer: a config with no plugins key gains one that parses", async () => {
  const cfg = await sandbox();
  const file = path.join(cfg, "opencode.json");
  await writeFile(file, `{\n  "$schema": "https://opencode.ai/config.json"\n}\n`, "utf8");

  await install(cfg);
  const config = await json(file);

  assert.equal(config.default_agent, "swarm");
  assert.deepEqual(config.plugins, [expectSwarmSpec()]);
});

test("installer: an empty config file becomes a complete valid config", async () => {
  const cfg = await sandbox();
  const file = path.join(cfg, "opencode.json");
  await writeFile(file, "", "utf8");

  await install(cfg);
  const config = await json(file);

  assert.equal(config.default_agent, "swarm");
  assert.deepEqual(config.plugins, [expectSwarmSpec()]);
});

test("installer: comments in opencode.jsonc survive untouched", async () => {
  const cfg = await sandbox();
  const file = path.join(cfg, "opencode.jsonc");
  const original = `{
  // keep this comment
  "model": "some/model"
}\n`;
  await writeFile(file, original, "utf8");

  await install(cfg);
  const text = await readFile(file, "utf8");

  assert.match(text, /\/\/ keep this comment/);
  assert.match(text, /"model"\s*:\s*"some\/model"/);
  assert.ok(text.includes(expectSwarmSpec()), "plugin spec is registered verbatim");
  assert.match(text, /"default_agent"\s*:\s*"swarm"/);
});

test("installer: a second run changes nothing it already settled", async () => {
  const cfg = await sandbox();
  const file = path.join(cfg, "opencode.json");
  await writeFile(file, `{\n  "plugins": [\n    "left-alone@1.0.0"\n  ]\n}\n`, "utf8");

  await install(cfg);
  const first = await readFile(file, "utf8");
  await install(cfg);
  const second = await readFile(file, "utf8");

  assert.equal(second, first);
  assert.equal((await json(file)).default_agent, "swarm");
});

test("installer: locally accumulated CHANGELOG history is never overwritten", async () => {
  const cfg = await sandbox();
  const target = path.join(cfg, "agents", "swarm", "CHANGELOG.md");
  await mkdir(path.dirname(target), { recursive: true });
  const history = "- 2026-01-01 | tune | router | raise sample floor | evidence: 12, fails 4 -> 1\n";
  await writeFile(target, history, "utf8");

  await install(cfg);

  assert.equal(await readFile(target, "utf8"), history);
});

test("installer: stale swarm pages are pruned, unrelated agents are not", async () => {
  const cfg = await sandbox();
  const agents = path.join(cfg, "agents");
  await mkdir(path.join(agents, "swarm"), { recursive: true });
  await writeFile(path.join(agents, "swarm", "retired-specialist.md"), "---\nmode: subagent\n---\n", "utf8");
  await writeFile(path.join(agents, "my-own-agent.md"), "---\nmode: subagent\n---\n", "utf8");
  await writeFile(path.join(cfg, "opencode.json"), "{}\n", "utf8");

  await install(cfg);

  assert.equal(existsSync(path.join(agents, "swarm", "retired-specialist.md")), false);
  assert.equal(existsSync(path.join(agents, "my-own-agent.md")), true);
  assert.equal(existsSync(path.join(agents, "swarm.md")), true);
  assert.equal(existsSync(path.join(agents, "swarm", "builder.md")), true);
});

test("installer: the previous swarm's commands and lessons file are removed", async () => {
  const cfg = await sandbox();
  await mkdir(path.join(cfg, "commands"), { recursive: true });
  await writeFile(path.join(cfg, "commands", "swarm-status.md"), "old\n", "utf8");
  await writeFile(path.join(cfg, "swarm-lessons.md"), "old\n", "utf8");
  await writeFile(path.join(cfg, "opencode.json"), "{}\n", "utf8");

  await install(cfg);

  assert.equal(existsSync(path.join(cfg, "commands", "swarm-status.md")), false);
  assert.equal(existsSync(path.join(cfg, "swarm-lessons.md")), false);
});

test("installer: a dry run reports and touches nothing", async () => {
  const cfg = await sandbox();
  const file = path.join(cfg, "opencode.json");
  const original = `{\n  "plugins": [\n    "left-alone@1.0.0"\n  ]\n}\n`;
  await writeFile(file, original, "utf8");

  const { stdout } = await run(process.execPath, [INSTALLER], {
    env: { ...process.env, OPENCODE_CONFIG_DIR: cfg },
    windowsHide: true,
  });

  assert.match(stdout, /dry run/);
  assert.equal(await readFile(file, "utf8"), original);
  assert.equal(existsSync(path.join(cfg, "agents")), false);
});

test("installer: an already-broken plain-JSON config is refused, not silently rewritten", async () => {
  const cfg = await sandbox();
  const file = path.join(cfg, "opencode.json");
  const broken = `{\n  // comments belong in .jsonc\n  "model": "some/model"\n}\n`;
  await writeFile(file, broken, "utf8");

  await assert.rejects(install(cfg), /not valid JSON to begin with/);
  assert.equal(await readFile(file, "utf8"), broken);
});

test("installer: with both config files present only opencode.json is edited", async () => {
  const cfg = await sandbox();
  const plain = path.join(cfg, "opencode.json");
  const jsonc = path.join(cfg, "opencode.jsonc");
  await writeFile(plain, `{\n  "plugins": []\n}\n`, "utf8");
  const jsoncText = `{\n  // hand-written formatting, keep it\n  "model": "some/model"\n}\n`;
  await writeFile(jsonc, jsoncText, "utf8");

  await install(cfg);

  assert.deepEqual((await json(plain)).plugins, [expectSwarmSpec()]);
  assert.equal(await readFile(jsonc, "utf8"), jsoncText);
});

test("installer: a swarm spec pointing at a moved clone is canonicalized, not duplicated", async () => {
  const cfg = await sandbox();
  const file = path.join(cfg, "opencode.json");
  await writeFile(
    file,
    `{\n  "plugins": [\n    "file:///C:/old/place/opencode-agent-swarm/src",\n    "other@1.0.0"\n  ]\n}\n`,
    "utf8",
  );

  await install(cfg);

  assert.deepEqual((await json(file)).plugins, [expectSwarmSpec(), "other@1.0.0"]);
});

function expectSwarmSpec() {
  // The installer registers the directory, never the entry file. Use the same
  // conversion it uses: hand-building "file:///" breaks on paths with spaces,
  // which percent-encode to %20.
  return pathToFileURL(path.join(ROOT, "src")).href;
}
