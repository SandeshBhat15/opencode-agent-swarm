#!/usr/bin/env node
// Bounded live check against a running OpenCode server.
//
//   SWARM_LIVE=1 node scripts/live-check.mjs
//
// It is deliberately NOT under test/ so `npm test` can never run it.
// Without SWARM_LIVE=1 it exits 0 having done nothing.
//
// Steps:
//   1. server identity            (opencode api get /api/info)
//   2. live model catalog         (opencode models)            -- fails if empty
//   3. one real round trip        (create session -> prompt -> wait -> context)
//      and it reports the ACTUAL tokens/cost it read back, or says they were
//      unreported. It never prints a missing measurement as zero.

import { execFile } from 'node:child_process';
import { existsSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

const run = promisify(execFile);
const TIMEOUT_MS = 180000;
const PROMPT = 'Reply with the single word: pong';

function findCli() {
  if (process.env.OPENCODE_BIN && existsSync(process.env.OPENCODE_BIN)) return process.env.OPENCODE_BIN;
  if (process.platform === 'win32') {
    const base = path.join(process.env.APPDATA ?? path.join(homedir(), 'AppData', 'Roaming'), 'ai.opencode.desktop', 'cli');
    if (existsSync(base)) {
      for (const name of readdirSync(base)) {
        const candidate = path.join(base, name, 'opencode-cli.exe');
        if (existsSync(candidate)) return candidate;
      }
    }
  }
  return 'opencode';
}

const CLI = findCli();
const failures = [];

async function cli(args, { timeout = TIMEOUT_MS } = {}) {
  const { stdout } = await run(CLI, args, { timeout, maxBuffer: 8 * 1024 * 1024, windowsHide: true });
  return stdout.trim();
}

async function api(method, target, body) {
  const args = ['api', method, target];
  if (body !== undefined) args.push('--data', JSON.stringify(body));
  const out = await cli(args);
  if (!out) return null;
  try {
    const parsed = JSON.parse(out);
    return parsed?.data ?? parsed;
  } catch {
    return out;
  }
}

function report(label, ok, detail) {
  console.log(`${ok ? 'ok  ' : 'FAIL'}  ${label}${detail ? ` -- ${detail}` : ''}`);
  if (!ok) failures.push(label);
}

function tokensOf(usage) {
  if (!usage) return null;
  const total = usage.input + usage.output + (usage.reasoning ?? 0);
  return Number.isFinite(total) ? total : null;
}

async function main() {
  console.log(`opencode CLI: ${CLI}`);
  console.log('');

  const version = await cli(['--version']).catch(() => null);
  report('cli reachable', Boolean(version), version ?? 'no version reported');

  const info = await api('get', '/api/info').catch((error) => ({ error: error.message }));
  const serverOk = Boolean(info) && !info.error;
  report('server /api/info', serverOk, serverOk ? `version ${info.version ?? 'unknown'} pid ${info.pid ?? '?'}` : info?.error ?? 'no response');

  const modelsRaw = await cli(['models']).catch(() => '');
  const models = modelsRaw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line && !line.startsWith('-') && line.includes('/'));
  report('live model catalog', models.length > 0, models.length > 0 ? `${models.length} models (e.g. ${models[0]})` : 'no models reachable');

  const agentsRaw = await api('get', '/api/agent').catch(() => null);
  const agents = (Array.isArray(agentsRaw) ? agentsRaw : (agentsRaw?.data ?? [])).map((a) => a?.id);
  const swarmAgents = agents.filter((id) => typeof id === 'string' && (id === 'swarm' || id.startsWith('swarm/')));
  report(
    'swarm agents registered',
    swarmAgents.includes('swarm'),
    swarmAgents.length > 0 ? `${swarmAgents.length}: ${swarmAgents.join(', ')}` : 'agent "swarm" not found',
  );

  if (failures.length > 0) {
    console.log('\nlive check FAILED: provider access is not available, so nothing was verified.');
    process.exitCode = 1;
    return;
  }

  // One bounded round trip. This is the real integration test.
  let sessionID = null;
  try {
    const created = await api('post', '/api/session', { agent: 'swarm', title: 'swarm live check' });
    sessionID = created?.id ?? null;
    report('session create with agent "swarm"', Boolean(sessionID), sessionID ?? 'no session id returned');
    if (!sessionID) throw new Error('cannot continue without a session');

    await api('post', `/api/session/${sessionID}/prompt`, { text: PROMPT });
    await api('post', `/api/experimental/session/${sessionID}/wait`);

    const messages = await api('get', `/api/session/${sessionID}/context`);
    const list = Array.isArray(messages) ? messages : (messages?.data ?? []);
    const assistants = list.filter((m) => m?.type === 'assistant');
    const text = assistants
      .map((m) => (m.content ?? []).filter((c) => c.type === 'text').map((c) => c.text).join(' '))
      .join(' ')
      .trim();
    report('assistant replied', assistants.length > 0 && text.length > 0, text.slice(0, 80) || 'no assistant text');

    const tokens = assistants.map((m) => tokensOf(m.tokens)).filter((n) => Number.isFinite(n));
    const costs = assistants.map((m) => m.cost).filter((n) => Number.isFinite(n));
    const tokenTotal = tokens.reduce((a, b) => a + b, 0);
    const costTotal = costs.reduce((a, b) => a + b, 0);
    report(
      'telemetry read back',
      tokens.length > 0,
      tokens.length > 0
        ? `${tokenTotal} tokens over ${tokens.length} assistant messages, cost ${costTotal} USD`
        : 'tokens unreported (not zero)',
    );
  } catch (error) {
    report('round trip', false, error.message);
  } finally {
    if (sessionID) {
      await api('delete', `/api/session/${sessionID}`).catch(() => {
        console.log(`note  left session ${sessionID} in place (delete not available)`);
      });
    }
  }

  console.log('');
  if (failures.length > 0) {
    console.log(`live check FAILED (${failures.length}): ${failures.join(', ')}`);
    process.exitCode = 1;
  } else {
    console.log('live check passed: server, catalog, agent session and telemetry all real.');
  }
}

if (process.env.SWARM_LIVE !== '1') {
  console.log('skipped: set SWARM_LIVE=1 to run the bounded live check (it spends provider credit).');
  process.exit(0);
}

main().catch((error) => {
  console.error(`live check errored: ${error.message}`);
  process.exitCode = 1;
});
