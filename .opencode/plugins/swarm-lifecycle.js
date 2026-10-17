/**
 * Per-project install for the swarm lifecycle plugin.
 *
 * This file deliberately contains no logic. It re-exports the one real
 * implementation from `plugin/`, so a project that installs it here and a
 * machine that installs it globally share a single module instance - and
 * therefore a single install guard.
 *
 * That sharing is not cosmetic. OpenCode hot-reloads plugins, and a measured
 * 2.0.32 server called `setup()` four times per plugin file. Two *copies* would
 * be two module instances with two independent guards, so both would install
 * and every lifecycle action would fire twice. One module, one guard.
 *
 * Installing the same file in both `~/.config/opencode/plugins/` and a
 * project's `.opencode/plugins/` reintroduces that duplication. Pick one.
 */

export { default, __testing } from "../../plugin/swarm-lifecycle.js"