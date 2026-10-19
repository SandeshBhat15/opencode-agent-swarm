/**
 * opencode-swarm — a manager-led autonomous swarm for OpenCode.
 *
 * The default export is the OpenCode plugin. Load it by adding this file to the
 * `plugins` array of your OpenCode configuration, or by placing it in a
 * `plugins/` directory OpenCode discovers.
 *
 * Zero dependencies, no build step: OpenCode's own Node runtime executes this
 * file directly.
 */

export { bootstrap, createPlugin, PLUGIN_ID } from "./plugin/runtime.js";
export { createManager, DEFAULT_POLICY } from "./engine.js";
export { createRuntime } from "./plugin/adapter.js";
export { registerTools } from "./plugin/tools.js";
export { shouldActivate, managerDirective } from "./plugin/activation.js";

import { createPlugin } from "./plugin/runtime.js";

export default createPlugin();
