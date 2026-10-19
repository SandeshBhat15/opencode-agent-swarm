/**
 * Minimal structured logger. Output goes to stderr through an injectable sink
 * so the plugin never pollutes a session's rendered output.
 */

const LEVELS = { silent: 0, error: 1, warn: 2, info: 3, debug: 4 };

export function createLogger({ level = "warn", sink, scope = "swarm" } = {}) {
  const threshold = LEVELS[level] ?? LEVELS.warn;
  const write =
    sink ??
    ((line) => {
      process.stderr.write(`${line}\n`);
    });

  const emit = (name, args) => {
    if (LEVELS[name] > threshold) return;
    const time = new Date().toISOString();
    const rest = args.length ? ` ${args.map(format).join(" ")}` : "";
    write(`${time} ${name.toUpperCase().padEnd(5)} [${scope}]${rest}`);
  };

  return {
    level,
    error: (...args) => emit("error", args),
    warn: (...args) => emit("warn", args),
    info: (...args) => emit("info", args),
    debug: (...args) => emit("debug", args),
    child(childScope) {
      return createLogger({ level, sink, scope: `${scope}:${childScope}` });
    },
  };
}

export const silentLogger = createLogger({ level: "silent", sink: () => {} });

function format(value) {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
