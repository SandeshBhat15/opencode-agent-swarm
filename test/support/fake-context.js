/**
 * A fake OpenCode plugin context.
 *
 * It implements exactly the interface surface the swarm uses, with the same
 * shapes as `@opencode/plugin@2.0.32`. Tests drive it directly; nothing here
 * touches a real server, the network, files or credentials.
 */

export const FAKE_MODELS = [
  model("opencode", "deepseek-v4.1-flash-free", { context: 128000 }),
  model("opencode", "glm-5.3-flash-free", { context: 200000 }),
  model("opencode", "nemotron-3-ultra-free", { context: 256000 }),
  model("opencode", "nemotron-3.5-lightning-free", { context: 32000 }),
  model("opencode", "nemotron-3.5-super-vl-free", { context: 64000, modalities: ["text", "image"] }),
  model("opencode", "longcat-2.5-preview-free", { context: 100000 }),
  model("opencode", "mimo-v2.6-flash-free", { context: 64000 }),
  model("opencode", "big-pickle", { context: 64000 }),
  model("opencode", "beam-free", { context: 64000 }),
  model("opencode", "space-bunny-free", { context: 64000 }),
];

function model(providerID, id, { context = 128000, tools = true, modalities = ["text"], costInput = 0, costOutput = 0, status = "active" } = {}) {
  return {
    id,
    modelID: id,
    providerID,
    canonical: `${providerID}/${id}`,
    name: id,
    status,
    enabled: true,
    capabilities: { tools, input: modalities, output: ["text"], temperature: true },
    variants: [],
    time: { released: 0 },
    cost: [{ input: costInput, output: costOutput, cache: { read: 0, write: 0 } }],
    limit: { context, output: 8192 },
  };
}

/** `StorageDomain` with `scan` returning `{entries, next}` — the real shape. */
export function createFakeStorage(seed = {}) {
  const data = new Map(Object.entries(seed));
  return {
    async get(key) {
      return data.has(key) ? structuredClone(data.get(key)) : undefined;
    },
    async set(key, value) {
      data.set(key, structuredClone(value));
    },
    async remove(key) {
      data.delete(key);
    },
    async scan({ prefix = "", after, limit = 100 } = {}) {
      const all = [...data.entries()]
        .filter(([key]) => key.startsWith(prefix))
        .map(([key, value]) => ({ key, value }))
        .sort((a, b) => a.key.localeCompare(b.key));
      const start = after ? all.findIndex((entry) => entry.key > after) : 0;
      const slice = all.slice(start < 0 ? all.length : start, (start < 0 ? all.length : start) + limit);
      const last = slice[slice.length - 1];
      const more = last ? all.some((entry) => entry.key > last.key) : false;
      return { entries: slice, ...(more ? { next: last.key } : {}) };
    },
    __data: data,
  };
}

/**
 * @param {object} options
 * @param {Function} [options.worker]   `(prompt, {agent, model, label}) => response`
 */
export function createFakeContext({
  version = "2.0.32",
  directory = "E:\\AI_Exp\\fake-project",
  models = FAKE_MODELS,
  storage = createFakeStorage(),
  worker = null,
  modelListFails = false,
} = {}) {
  const calls = { prompt: [], created: [], interrupts: [], removed: [], toolHooks: {}, hookNames: [] };
  const registrations = [];
  const sessionData = new Map();
  const toolEditor = createFakeToolEditor();
  let counter = 0;

  const respond = worker ?? defaultWorker;

  const session = {
    async create(input = {}) {
      counter += 1;
      const id = `ses_fake_${counter.toString(36)}`;
      const info = {
        id,
        projectID: "prj_fake",
        agent: input.agent ?? null,
        model: input.model ?? null,
        cost: 0,
        tokens: zeroTokens(),
        time: { created: Date.now() },
        title: input.title ?? null,
        parentID: input.parentID ?? null,
      };
      calls.created.push({ ...input, id });
      sessionData.set(id, { info, messages: [] });
      return info;
    },
    async get({ sessionID }) {
      const entry = sessionData.get(sessionID);
      if (!entry) throw new Error(`unknown session ${sessionID}`);
      return entry.info;
    },
    async prompt(input) {
      const entry = sessionData.get(input.sessionID);
      if (!entry) throw new Error(`unknown session ${input.sessionID}`);
      calls.prompt.push({ ...input });
      const result = await respond(input.text, {
        agent: entry.info.agent,
        model: entry.info.model,
        label: entry.info.title,
        sessionID: input.sessionID,
      });
      if (result?.throw) {
        entry.info.outcome = "failed";
        throw result.throw;
      }
      entry.messages.push({
        id: `msg_${entry.messages.length}`,
        type: "assistant",
        agent: entry.info.agent,
        model: entry.info.model,
        time: { created: Date.now(), completed: Date.now() },
        finish: "stop",
        content: [{ type: "text", text: result?.text ?? "" }],
        tokens: result?.tokens ?? { input: 100, output: 50, reasoning: 0, cache: { read: 0, write: 0 } },
        cost: result?.cost ?? 0,
        ...(result?.error ? { error: result.error } : {}),
      });
      entry.info.tokens = sumTokens(entry.messages);
      entry.info.cost = entry.messages.reduce((total, message) => total + (message.cost ?? 0), 0);
      entry.info.outcome = result?.error ? "failed" : "succeeded";
      return { id: `inbox_${entry.messages.length}` };
    },
    async wait() {
      return undefined;
    },
    async context({ sessionID }) {
      const entry = sessionData.get(sessionID);
      return entry ? [...entry.messages] : [];
    },
    async interrupt({ sessionID }) {
      calls.interrupts.push(sessionID);
      return { interrupted: true };
    },
    async remove({ sessionID }) {
      calls.removed.push(sessionID);
      sessionData.delete(sessionID);
    },
    async hook(name, callback) {
      calls.hookNames.push(name);
      const registration = {
        name,
        callback,
        dispose: async () => {
          const index = registrations.indexOf(registration);
          if (index >= 0) registrations.splice(index, 1);
        },
      };
      registrations.push(registration);
      return registration;
    },
  };

  const ctx = {
    app: { version },
    location: { directory, project: { id: "prj_fake", directory, canonical: directory } },
    options: {},
    storage,
    model: {
      async list() {
        if (modelListFails) throw new Error("catalog unreachable");
        return structuredClone(models);
      },
    },
    agent: {
      async list() {
        return [
          { id: "swarm", name: "swarm", mode: "primary", hidden: false },
          { id: "swarm/researcher", name: "researcher", mode: "subagent", hidden: false },
          { id: "swarm/builder", name: "builder", mode: "subagent", hidden: false },
          { id: "swarm/visual", name: "visual", mode: "subagent", hidden: false },
          { id: "swarm/qc", name: "qc", mode: "subagent", hidden: false },
          { id: "swarm/improve", name: "improve", mode: "subagent", hidden: false },
        ];
      },
    },
    tool: {
      async transform(callback) {
        callback(toolEditor);
        const registration = { dispose: async () => toolEditor.tools.clear() };
        registrations.push(registration);
        return registration;
      },
      async hook(name, callback) {
        calls.toolHooks[name] = callback;
        const registration = { dispose: async () => delete calls.toolHooks[name] };
        registrations.push(registration);
        return registration;
      },
      async list() {
        return [...toolEditor.tools.values()];
      },
    },
    session,
    generate: {
      async text() {
        return { text: "" };
      },
    },
    __swarm: {
      calls,
      registrations,
      tools: toolEditor.tools,
      sessionData,
      /** Fire the registered hook `name`, if the plugin registered one. */
      async fire(name, event) {
        const found = registrations.filter((registration) => registration.name === name);
        for (const registration of found) await registration.callback(event);
        return found.length;
      },
      toolNames() {
        return [...toolEditor.tools.keys()];
      },
      async callTool(fullName, input, context = {}) {
        const tool = toolEditor.tools.get(fullName);
        if (!tool) throw new Error(`no tool ${fullName}`);
        return tool.execute(input, toolContext(context));
      },
    },
    disposeAll: async () => {
      for (const registration of [...registrations]) await registration.dispose?.();
    },
  };

  return ctx;
}

function createFakeToolEditor() {
  const tools = new Map();
  return {
    tools,
    // Declares the namespace only. As on the real server, a tool joins it
    // solely through `options.namespace` — assuming otherwise here hid a
    // defect where every swarm tool registered under its bare name.
    namespace() {},
    add(tool) {
      const ns = tool.options?.namespace ?? null;
      const full = ns ? `${ns}_${tool.name}` : tool.name;
      tools.set(full, { ...tool, id: full });
    },
    list() {
      return [...tools.values()];
    },
    get(id) {
      return tools.get(id);
    },
    update(id, callback) {
      const tool = tools.get(id);
      if (tool) callback(tool);
    },
    remove(id) {
      tools.delete(id);
    },
  };
}

export function toolContext({ signal = null, sessionID = "ses_parent", agent = "swarm" } = {}) {
  const controller = new AbortController();
  return {
    signal: signal ?? controller.signal,
    sessionID,
    agent,
    messageID: "msg_parent",
    id: "call_1",
    async progress() {},
    controller,
  };
}

function defaultWorker(text, { agent, label }) {
  return { text: `[${agent ?? "worker"}] ${String(text).slice(0, 200)}`, tokens: { input: 120, output: 80, reasoning: 0, cache: { read: 0, write: 0 } }, cost: 0.001 };
}

function zeroTokens() {
  return { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } };
}

function sumTokens(messages) {
  const total = zeroTokens();
  for (const message of messages) {
    total.input += message.tokens?.input ?? 0;
    total.output += message.tokens?.output ?? 0;
    total.reasoning += message.tokens?.reasoning ?? 0;
    total.cache.read += message.tokens?.cache?.read ?? 0;
    total.cache.write += message.tokens?.cache?.write ?? 0;
  }
  return total;
}
