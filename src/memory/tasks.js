/**
 * Persistent task memory.
 *
 * Finished tasks are stored so a later, similar request does not repeat
 * research that was already verified. Similarity is a deterministic keyword
 * overlap — no embedding call, no model round-trip.
 */

const KEY = "memory/tasks";

export function createTaskMemory({ store = null, logger = null } = {}) {
  return {
    async save(record) {
      if (!store) return record;
      const entry = {
        id: record.id,
        at: record.at ?? Date.now(),
        task: record.task,
        kind: record.kind ?? null,
        workflow: record.workflow ?? null,
        verified: record.verified === true,
        keywords: record.keywords ?? keywordsOf(record.task),
        outputs: trimOutputs(record.outputs ?? {}),
        summary: record.summary ?? null,
      };
      try {
        await store.append(KEY, entry, { max: 200 });
      } catch (error) {
        logger?.warn?.(`task memory save failed: ${error?.message ?? error}`);
      }
      return entry;
    },
    async recent(limit = 20) {
      if (!store) return [];
      const all = await store.get(KEY, []);
      return Array.isArray(all) ? all.slice(-limit).reverse() : [];
    },
    /**
     * Best prior task by keyword overlap. Returns `{entry, overlap}` or null.
     */
    async similar(task, { kind = null, limit = 50 } = {}) {
      if (!store) return null;
      const all = await store.get(KEY, []);
      if (!Array.isArray(all) || all.length === 0) return null;
      const wanted = new Set(keywordsOf(task));
      if (wanted.size === 0) return null;

      let best = null;
      for (const entry of all.slice(-limit)) {
        const entryKeywords = entry.keywords ?? keywordsOf(entry.task);
        if (!entryKeywords?.length) continue;
        const shared = entryKeywords.filter((word) => wanted.has(word)).length;
        const overlap = shared / Math.max(1, Math.min(wanted.size, entryKeywords.length));
        const kindBonus = kind && entry.kind === kind ? 0.1 : 0;
        const score = overlap + kindBonus;
        if (!best || score > best.score) best = { entry, overlap, score };
      }
      return best;
    },
  };
}

const STOPWORDS = new Set([
  "the","a","an","and","or","but","if","then","than","that","this","these","those","to","of","in","on","for","with","as","at","by","from","is","are","was","were","be","been","it","its","you","your","i","we","me","my","our","do","does","did","can","could","should","would","will","please","make","made","using","use","used","into","out","up","down","over","under","about","all","any","some","more","most","not","no","yes","just","also","very","them","they","he","she","his","her","have","has","had","when","where","which","who","what","how","why","new","get","got","add",
]);

export function keywordsOf(text) {
  const counts = new Map();
  for (const word of String(text ?? "").toLowerCase().match(/[a-z][a-z0-9_-]{2,}/g) ?? []) {
    if (STOPWORDS.has(word)) continue;
    counts.set(word, (counts.get(word) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 12)
    .map(([word]) => word);
}

function trimOutputs(outputs) {
  const out = {};
  for (const [key, value] of Object.entries(outputs)) {
    const text = typeof value === "string" ? value : JSON.stringify(value ?? null);
    out[key] = text && text.length > 4000 ? text.slice(0, 4000) : text;
  }
  return out;
}
