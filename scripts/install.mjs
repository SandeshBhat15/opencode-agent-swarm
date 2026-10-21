#!/usr/bin/env node
// Additive, idempotent installer for opencode-swarm.
//
// Default is a dry run. Pass --write to apply.
//
//   node scripts/install.mjs           # show what would change
//   node scripts/install.mjs --write   # apply
//
// It will:
//   * copy the agent pages into <config>/agents/
//   * add this directory to the global `plugins` list
//   * set `default_agent` to "swarm"
//   * remove the previous swarm's agents, commands, plugin and lessons file
//
// It never deletes anything it did not create, and it preserves every unrelated setting.

import { readFile, writeFile, mkdir, readdir, rm, copyFile, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const WRITE = process.argv.includes('--write');

const SCHEMA = 'https://opencode.ai/config.json';
// OpenCode resolves a plugin's entrypoint (`index.js`) *inside* the directory it is
// given, so the configured value must be that directory, never the file itself.
const PLUGIN_DIR = pathToFileURL(path.join(ROOT, 'src')).href;
const CONFIG_NAMES = ['opencode.json', 'opencode.jsonc'];

// The previous swarm's footprint. Only these names are removed.
const LEGACY = {
  agents: ['swarm.md', 'swarm'],
  commands: ['swarm.md', 'swarm-resume.md', 'swarm-status.md'],
  plugins: ['swarm-lifecycle.js'],
  files: ['swarm-lessons.md'],
};

// Pages that accumulate local history. Copying the pristine page over one of
// these would silently delete that history, so an existing copy always wins.
const PRESERVE_LOCAL = new Set(['swarm/CHANGELOG.md']);

// Everything this installer owns lives under the `swarm` agent namespace.
function inSwarmNamespace(rel) {
  return rel === 'swarm.md' || rel === 'swarm' || rel.startsWith('swarm/');
}

const log = [];
function record(action, target) {
  log.push(`  ${action.padEnd(8)} ${target}`);
}

function configDir() {
  if (process.env.OPENCODE_CONFIG_DIR) return process.env.OPENCODE_CONFIG_DIR;
  const xdg = process.env.XDG_CONFIG_HOME;
  if (xdg) return path.join(xdg, 'opencode');
  return path.join(homedir(), '.config', 'opencode');
}

async function listMarkdown(dir) {
  try {
    const entries = await readdir(dir, { withFileTypes: true });
    const out = [];
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      // Always report posix-style relative paths: they are compared against
      // PRESERVE_LOCAL and the legacy names, which are written with `/`.
      if (entry.isDirectory()) out.push(...(await listMarkdown(full)).map((p) => path.posix.join(entry.name, p)));
      else if (entry.name.endsWith('.md')) out.push(entry.name);
    }
    return out;
  } catch {
    return [];
  }
}

async function installAgents(cfg) {
  const sourceRoot = path.join(ROOT, 'agents');
  const targetRoot = path.join(cfg, 'agents');
  const files = await listMarkdown(sourceRoot);
  for (const rel of files) {
    const from = path.join(sourceRoot, rel);
    const to = path.join(targetRoot, rel);
    const same = existsSync(to) && (await readFile(from, 'utf8')) === (await readFile(to, 'utf8'));
    if (!same && existsSync(to) && PRESERVE_LOCAL.has(rel)) {
      record('keep', `${to} (local history kept; template differs)`);
      continue;
    }
    record(same ? 'keep' : 'write', to);
    if (!WRITE || same) continue;
    await mkdir(path.dirname(to), { recursive: true });
    await copyFile(from, to);
  }
}

// Prune agent pages this version no longer ships (a specialist that was renamed
// or dropped), so an upgrade does not leave a stale agent registered. Only the
// swarm namespace is touched: an unrelated agent page is never removed.
async function pruneStale(cfg) {
  const supplied = new Set(await listMarkdown(path.join(ROOT, 'agents')));
  const targetRoot = path.join(cfg, 'agents');
  for (const rel of await listMarkdown(targetRoot)) {
    if (supplied.has(rel) || !inSwarmNamespace(rel)) continue;
    const target = path.join(targetRoot, rel);
    record('remove', target);
    if (WRITE) await rm(target, { force: true });
  }
}

async function removeLegacy(cfg) {
  // A legacy name that this install also supplies is settled by the copy step,
  // not by deletion: removing it first only churns files that are about to be
  // rewritten, and throws away anything added inside the directory.
  const supplied = await listMarkdown(path.join(ROOT, 'agents'));
  const suppliedPath = (name) => supplied.includes(name) || supplied.some((rel) => rel.startsWith(`${name}/`));
  for (const name of LEGACY.agents) {
    const target = path.join(cfg, 'agents', name);
    if (!existsSync(target)) continue;
    if (suppliedPath(name)) {
      record('keep', `${target} (supplied by this install)`);
      continue;
    }
    record('remove', target);
    if (WRITE) await rm(target, { recursive: true, force: true });
  }
  for (const name of LEGACY.commands) {
    const target = path.join(cfg, 'commands', name);
    if (!existsSync(target)) continue;
    record('remove', target);
    if (WRITE) await rm(target, { force: true });
  }
  for (const name of LEGACY.plugins) {
    const target = path.join(cfg, 'plugins', name);
    if (!existsSync(target)) continue;
    record('remove', target);
    if (WRITE) await rm(target, { force: true });
  }
  for (const name of LEGACY.files) {
    const target = path.join(cfg, name);
    if (!existsSync(target)) continue;
    record('remove', target);
    if (WRITE) await rm(target, { force: true });
  }
}

// A legacy entry is an agent *definition*. The same key names are also used for
// per-agent permission overrides, and those must survive: the old swarm's page
// names are still the names of the agents this plugin installs.
function isLegacyAgentDefinition(value) {
  return Boolean(value) && typeof value === 'object' && typeof value.prompt === 'string';
}

function stripSwarmAgents(config) {
  const agents = config.agents;
  if (!agents || typeof agents !== 'object') return false;
  let changed = false;
  for (const key of Object.keys(agents)) {
    if (key !== 'swarm' && !key.startsWith('swarm/')) continue;
    if (!isLegacyAgentDefinition(agents[key])) {
      record('keep', `agents.${key} (configures an agent, not a legacy definition)`);
      continue;
    }
    delete agents[key];
    changed = true;
  }
  if (changed && Object.keys(agents).length === 0) delete config.agents;
  return changed;
}

// The plugin's identity, for recognising a config that already carries it.
// The directory name is only two of the shapes it can take: a clone can live
// anywhere, and the spec then is a `file://` URL to that path. Matching the
// exact spec keeps the installer idempotent from any directory.
const SWARM_NAMES = ['opencode-agent-swarm', 'opencode-swarm'];
const mentionsSwarm = (text) =>
  text.includes(PLUGIN_DIR) || SWARM_NAMES.some((name) => text.includes(name));

function isSwarmSpec(entry) {
  const spec = typeof entry === 'string' ? entry : entry?.package;
  if (typeof spec !== 'string') return false;
  return spec === PLUGIN_DIR || SWARM_NAMES.some((name) => spec.includes(name));
}

// Index of the `]` closing the array opened at `open`, skipping over strings.
function closingBracket(text, open) {
  let depth = 0;
  let inString = false;
  for (let i = open; i < text.length; i += 1) {
    const ch = text[i];
    if (inString) {
      if (ch === '\\') i += 1;
      else if (ch === '"') inString = false;
    } else if (ch === '"') inString = true;
    else if (ch === '[') depth += 1;
    else if (ch === ']') {
      depth -= 1;
      if (depth === 0) return i;
    }
  }
  return -1;
}

// Swap an existing swarm spec for the canonical directory form. Surgical, so
// comments and formatting in `opencode.jsonc` survive untouched.
function canonicalizeSpec(text) {
  if (text.includes(PLUGIN_DIR)) return { text, changed: false };
  const quoted = new RegExp(`"[^"]*(?:${SWARM_NAMES.join('|')})[^"]*"`);
  const match = quoted.exec(text);
  if (!match) return { text, changed: false };
  const next = text.replace(match[0], JSON.stringify(PLUGIN_DIR));
  return { text: next, changed: next !== text };
}

// Insert a property before the closing `}` of a JSON object, adding the
// separating comma when the object already has members. Both insertion sites
// below used to splice text in without it, which produced `... ] "key": ...`
// — invalid JSON, accepted by nobody and silently applied to the user's config.
function insertProperty(text, line) {
  const close = text.lastIndexOf('}');
  if (close === -1) return { text, changed: false };
  const body = text.slice(0, close).replace(/\s+$/, '');
  const needsComma = body.length > 0 && !/[{,]$/.test(body);
  return { text: `${body}${needsComma ? ',' : ''}\n${line}\n${text.slice(close)}`, changed: true };
}

function addPluginSpec(text) {
  if (!text.trim()) {
    const fresh = { $schema: SCHEMA, default_agent: 'swarm', plugins: [PLUGIN_DIR] };
    return { text: `${JSON.stringify(fresh, null, 2)}\n`, changed: true };
  }
  const key = /"(plugins?|plugin)"\s*:\s*\[/.exec(text);
  if (key) {
    const open = key.index + key[0].length - 1;
    const close = closingBracket(text, open);
    if (close !== -1) {
      const head = text.slice(0, close).replace(/\s*$/, '');
      const body = text.slice(open + 1, close).trim();
      const tail = text.slice(close);
      const next = body
        ? `${head}, ${JSON.stringify(PLUGIN_DIR)}\n  ${tail}`
        : `${head}\n    ${JSON.stringify(PLUGIN_DIR)}\n  ${tail}`;
      return { text: next, changed: true };
    }
  }
  return insertProperty(text, `  "plugins": [\n    ${JSON.stringify(PLUGIN_DIR)}\n  ]`);
}

function ensureDefaultAgent(text) {
  if (!/"default_agent"/.test(text)) return insertProperty(text, '  "default_agent": "swarm"');
  const next = text.replace(/("default_agent"\s*:\s*)"[^"]*"/, '$1"swarm"');
  return { text: next, changed: next !== text };
}

function assertValidJson(file, text, before) {
  const problem = (value) => {
    try {
      JSON.parse(value);
      return null;
    } catch (error) {
      return error.message;
    }
  };
  const after = problem(text);
  if (!after) return;
  const already = before ? problem(before) : null;
  throw new Error(
    already
      ? `refusing to touch ${file}: it is not valid JSON to begin with (${already}). Fix it or rename it to .jsonc, then re-run; nothing was written`
      : `refusing to write ${file}: the edit would make it invalid JSON (${after}); nothing was written`,
  );
}

async function updateConfig(cfg) {
  const present = [];
  for (const name of CONFIG_NAMES) {
    const file = path.join(cfg, name);
    if (existsSync(file)) present.push({ name, path: file, text: await readFile(file, 'utf8') });
  }
  if (present.length > 1) {
    record('note', `OpenCode reads and merges both ${CONFIG_NAMES.join(' and ')}`);
  }

  // Whichever file already mentions the swarm plugin keeps owning the setting.
  const owner = present.find((file) => mentionsSwarm(file.text)) ?? null;
  const target =
    owner ??
    present.find((file) => file.name === 'opencode.json') ??
    present[0] ?? { name: 'opencode.json', path: path.join(cfg, 'opencode.json'), text: '' };

  let changed = false;
  let text = target.text;

  if (owner) {
    const canonical = canonicalizeSpec(text);
    record(canonical.changed ? 'write' : 'keep', `${target.path} plugins (${PLUGIN_DIR})`);
    text = canonical.text;
    changed = changed || canonical.changed;
  } else {
    const added = addPluginSpec(text);
    record('write', `${target.path} plugins (+ ${PLUGIN_DIR})`);
    text = added.text;
    changed = true;
  }

  const agent = ensureDefaultAgent(text);
  record(agent.changed ? 'write' : 'keep', `${target.path} default_agent = swarm`);
  text = agent.text;
  changed = changed || agent.changed;

  // Legacy `agents.swarm*` overrides only survive in a plain-JSON config.
  try {
    const parsed = JSON.parse(text);
    const legacyAgents = Array.isArray(parsed.plugins) && parsed.plugins.some((entry) => isSwarmSpec(entry) && entry !== PLUGIN_DIR);
    if (legacyAgents) {
      parsed.plugins = parsed.plugins.map((entry) => (isSwarmSpec(entry) ? PLUGIN_DIR : entry));
      changed = true;
    }
    if (stripSwarmAgents(parsed)) {
      record('write', `${target.path} agents (removed legacy swarm entries)`);
      changed = true;
    }
    if (changed) text = `${JSON.stringify(parsed, null, 2)}\n`;
  } catch {
    /* opencode.jsonc: leave unrelated structure and comments untouched */
  }

  if (WRITE && changed) {
    // A config the running OpenCode cannot parse takes the whole install down
    // with it, so a plain-JSON target is validated before anything is written.
    if (target.name.endsWith('.json')) assertValidJson(target.path, text, target.text);
    await mkdir(path.dirname(target.path), { recursive: true });
    await writeFile(target.path, text.endsWith('\n') ? text : `${text}\n`, 'utf8');
  }
}

async function main() {
  const cfg = configDir();
  console.log(`opencode-swarm installer${WRITE ? '' : ' (dry run, pass --write to apply)'}`);
  console.log(`config dir: ${cfg}`);
  console.log(`plugin:     ${PLUGIN_DIR}`);

  const agentsDir = path.join(ROOT, 'agents');
  const agentsStat = await stat(agentsDir).catch(() => null);
  if (!agentsStat?.isDirectory()) throw new Error(`missing agent pages at ${agentsDir}`);

  console.log('\nactions:');
  await removeLegacy(cfg);
  await installAgents(cfg);
  await pruneStale(cfg);
  await updateConfig(cfg);

  console.log(log.join('\n') || '  (nothing to do)');
  console.log(
    WRITE
      ? '\ndone. OpenCode watches its config and hot-reloads plugins, so this normally applies without a restart.'
      : '\ndry run only. re-run with --write to apply.',
  );
}

main().catch((error) => {
  console.error(`install failed: ${error.message}`);
  process.exitCode = 1;
});
