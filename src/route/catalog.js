/**
 * Model catalog.
 *
 * Normalises the `Model.Info` records OpenCode reports into a flat shape the
 * router can score. Unknown fields stay `null`; they are never defaulted to a
 * favourable value, because that would make the router lie to itself.
 */

/** @param {object} info a Model.Info record */
export function normalizeModel(info = {}) {
  const cost = Array.isArray(info.cost) ? info.cost : [];
  // Tiers exist for long-context pricing; the router compares the base tier.
  const base = cost.find((entry) => !entry?.tier) ?? cost[0] ?? null;

  return {
    id: info.id ?? info.modelID ?? null,
    modelID: info.modelID ?? info.id ?? null,
    providerID: info.providerID ?? null,
    canonical: info.canonical ?? null,
    name: info.name ?? info.id ?? "unknown",
    family: info.family ?? null,
    status: info.status ?? "active",
    enabled: info.enabled !== false,
    capabilities: {
      tools: info.capabilities?.tools === true,
      input: info.capabilities?.input ?? [],
      output: info.capabilities?.output ?? [],
      temperature: info.capabilities?.temperature ?? null,
    },
    limit: {
      context: Number.isFinite(info.limit?.context) ? info.limit.context : null,
      output: Number.isFinite(info.limit?.output) ? info.limit.output : null,
    },
    cost: base
      ? {
          input: numOrNull(base.input),
          output: numOrNull(base.output),
          cacheRead: numOrNull(base.cache?.read),
          cacheWrite: numOrNull(base.cache?.write),
          tiers: cost.length,
        }
      : null,
    released: Number.isFinite(info.time?.released) ? info.time.released : null,
    variants: (info.variants ?? []).map((variant) => variant?.id ?? variant?.name ?? String(variant)),
  };
}

function numOrNull(value) {
  return Number.isFinite(value) ? value : null;
}

/** @param {object[]} models Model.Info records from `ctx.model.list()` */
export function buildCatalog(models = []) {
  const entries = models.map(normalizeModel).filter((model) => model.id && model.providerID);
  const byRef = new Map(entries.map((model) => [ref(model), model]));

  return {
    all: entries,
    byRef,
    get(ref_) {
      if (!ref_) return null;
      if (typeof ref_ === "string") return byRef.get(ref_) ?? null;
      return byRef.get(`${ref_.providerID}/${ref_.id}`) ?? null;
    },
    usable({ requireTools = true } = {}) {
      return entries.filter((model) => {
        if (!model.enabled) return false;
        if (model.status === "deprecated") return false;
        if (requireTools && !model.capabilities.tools) return false;
        return true;
      });
    },
    get size() {
      return entries.length;
    },
  };
}

export function ref(model) {
  if (!model?.providerID || !model?.id) return null;
  return `${model.providerID}/${model.id}`;
}

export function parseRef(value) {
  if (typeof value !== "string") return null;
  const hash = value.indexOf("#");
  const variant = hash >= 0 ? value.slice(hash + 1) : undefined;
  const body = hash >= 0 ? value.slice(0, hash) : value;
  const slash = body.indexOf("/");
  if (slash <= 0) return null;
  return { providerID: body.slice(0, slash), id: body.slice(slash + 1), ...(variant ? { variant } : {}) };
}
