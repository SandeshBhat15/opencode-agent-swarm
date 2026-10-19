/**
 * Persistent task memory.
 *
 * One narrow interface, two backings:
 *  - `ctx.storage` when the plugin runtime exposes it (durable JSON owned by
 *    OpenCode),
 *  - a filesystem directory otherwise.
 * Tests use the in-memory adapter so nothing touches disk.
 *
 * All operations are async because the OpenCode storage interface is async.
 */

import { mkdir, readFile, readdir, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";

/** Volatile adapter. Used by tests and as the last-resort fallback. */
export function createMemoryAdapter(seed = {}) {
  const data = new Map(Object.entries(seed));
  return {
    kind: "memory",
    async get(key) {
      return data.has(key) ? clone(data.get(key)) : null;
    },
    async set(key, value) {
      data.set(key, clone(value));
    },
    async remove(key) {
      data.delete(key);
    },
    async keys() {
      return [...data.keys()];
    },
  };
}

/** Filesystem adapter: one JSON file per key, sanitised file names. */
export function createFsAdapter({ dir }) {
  if (!dir) throw new TypeError("createFsAdapter requires dir");
  return {
    kind: "fs",
    dir,
    async get(key) {
      try {
        return JSON.parse(await readFile(join(dir, `${safeName(key)}.json`), "utf8"));
      } catch (error) {
        if (error?.code === "ENOENT") return null;
        throw error;
      }
    },
    async set(key, value) {
      await mkdir(dir, { recursive: true });
      await writeFile(join(dir, `${safeName(key)}.json`), JSON.stringify(value, null, 2), "utf8");
    },
    async remove(key) {
      try {
        await unlink(join(dir, `${safeName(key)}.json`));
      } catch (error) {
        if (error?.code !== "ENOENT") throw error;
      }
    },
    async keys() {
      try {
        return (await readdir(dir))
          .filter((name) => name.endsWith(".json"))
          .map((name) => decodeName(name.slice(0, -5)))
          .filter((name) => name !== null);
      } catch (error) {
        if (error?.code === "ENOENT") return [];
        throw error;
      }
    },
  };
}

/** Adapter over the plugin's `ctx.storage` domain. */
export function createRuntimeAdapter(storage) {
  if (!storage || typeof storage.get !== "function") {
    throw new TypeError("createRuntimeAdapter requires a storage domain with get/set");
  }
  return {
    kind: "runtime",
    async get(key) {
      const value = await storage.get(key);
      return value ?? null;
    },
    async set(key, value) {
      await storage.set(key, value);
    },
    async remove(key) {
      if (typeof storage.remove === "function") await storage.remove(key);
    },
    async keys() {
      if (typeof storage.scan !== "function") return [];
      const names = [];
      let cursor;
      for (let page = 0; page < 20; page += 1) {
        const result = await storage.scan({ prefix: "", after: cursor, limit: 200 });
        const entries = Array.isArray(result) ? result : (result?.entries ?? []);
        if (!entries.length) break;
        for (const entry of entries) names.push(typeof entry === "string" ? entry : entry?.key);
        cursor = result?.next ?? entries[entries.length - 1]?.key ?? null;
        if (!cursor) break;
      }
      return names.filter(Boolean);
    },
  };
}

/**
 * @param {{adapter: object, namespace?: string, logger?: object}} options
 */
export function createStore({ adapter, namespace = "swarm", logger = null } = {}) {
  if (!adapter) throw new TypeError("createStore requires an adapter");
  const scoped = (key) => `${namespace}/${key}`;

  async function safe(operation, fallback) {
    try {
      return await operation();
    } catch (error) {
      logger?.warn?.(`store(${adapter.kind}) failure: ${error?.message ?? error}`);
      return fallback;
    }
  }

  const store = {
    adapter,
    namespace,
    async get(key, fallback = null) {
      const value = await safe(() => adapter.get(scoped(key)), null);
      return value === null || value === undefined ? fallback : value;
    },
    async set(key, value) {
      return safe(() => adapter.set(scoped(key), value), undefined);
    },
    async remove(key) {
      return safe(() => adapter.remove(scoped(key)), undefined);
    },
    async keys(prefix = "") {
      const names = await safe(() => adapter.keys(), []);
      return names
        .filter((name) => name.startsWith(`${namespace}/${prefix}`))
        .map((name) => name.slice(namespace.length + 1));
    },
    /** Append to a capped list stored under one key. Newest last. */
    async append(key, entry, { max = 500 } = {}) {
      const current = await store.get(key, []);
      const list = Array.isArray(current) ? current : [];
      list.push(entry);
      const trimmed = list.length > max ? list.slice(list.length - max) : list;
      await store.set(key, trimmed);
      return trimmed;
    },
    /** Read-modify-write helper. */
    async update(key, mutator, fallback = null) {
      const current = await store.get(key, fallback);
      const next = mutator(current);
      await store.set(key, next);
      return next;
    },
  };
  return store;
}

/**
 * File names must be reversible: the namespaced key contains `/`, and listing
 * has to be able to reconstruct the original key. Percent-encoding is safe on
 * both Windows and POSIX file systems, unlike the lossy `[^a-z0-9._-] -> _`.
 */
function safeName(key) {
  return encodeURIComponent(key);
}

function decodeName(name) {
  try {
    return decodeURIComponent(name);
  } catch {
    return null;
  }
}

function clone(value) {
  if (value === null || typeof value !== "object") return value;
  return JSON.parse(JSON.stringify(value));
}
