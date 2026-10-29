// Requested artifact transfers only. No history hooks, token access, or model call.
const SERVER = "plugin:openscience-remote:openscience";
const ALLOWED = ["begin_upload", "upload_status", "upload_chunk", "complete_upload", "read_artifact"];

function unpack(result) {
  if (!result || result.isError) throw new Error("OpenScience request failed");
  if (result.structuredContent) return result.structuredContent;
  const block = result.content?.find(item => item.type === "text");
  if (!block) throw new Error("OpenScience returned no structured result");
  return JSON.parse(block.text);
}

async function transfer($, request, budget) {
    let input;
    let pendingTool;
    try {
      // No general-purpose RPC forwarding or conversation upload route.
      if (!request || !["upload", "download"].includes(request.operation) ||
          typeof request.path !== "string" || !request.path ||
          Object.keys(request).some(key => !["operation", "path", "sha256", "project_id"].includes(key))) {
        throw new Error("Invalid artifact request");
      }
      input = {request};
      while (true) {
        const run = await $.process.run(["python3", `${$.plugin.root}/scripts/transfer_step.py`], {
          stdin: JSON.stringify(input), timeoutMs: 600000,
        });
        if (run.exitCode !== 0 || run.isStdoutTruncated || run.isStderrTruncated) {
          throw new Error("Local transfer helper failed");
        }
        const step = JSON.parse(run.stdout);
        if (step.done || step.pending) {
          return {text: JSON.stringify(step.summary)};
        }
        if (!ALLOWED.includes(step.call?.name) || typeof step.id !== "string" ||
            typeof step.action_id !== "string") throw new Error("Invalid transfer step");
        if (budget && budget.remainingMs < 500) {
          return {text: "Transfer checkpoint saved. Run the same /openscience-file command to resume."};
        }
        pendingTool = step.call.name;
        const response = unpack(await $.mcp.call(SERVER, step.call.name, step.call.arguments));
        pendingTool = undefined;
        input = {id: step.id, action_id: step.action_id, response};
      }
    } catch (error) {
      // Classify the observed native permission gate without echoing its text.
      // Never grant a permission or mistake provider content for an approval.
      if (pendingTool && ALLOWED.includes(pendingTool) && error?.name === "HooksError" &&
          typeof error.message === "string" &&
          error.message.includes("requested permissions to use") &&
          error.message.includes("haven't granted it yet")) {
        const tools = request.operation === "download" ? ["read_artifact"] :
          ["begin_upload", "upload_status", "upload_chunk", "complete_upload"];
        const rules = tools.map(name => `mcp__plugin_openscience-remote_openscience__${name}`).join(", ");
        return {text: `OpenScience file transfer needs native tool permission. Signing in does not grant this permission. If this transfer is authorized, open /permissions and review Allow rules for only: ${rules}. Then retry the identical transfer. Follow organization policy; do not override a denial. Original files and any pending checkpoint are retained.`};
      }
      // Neither provider exceptions nor helper stdout may be copied to the chat:
      // they can contain original bytes. Existing checkpoints remain available.
      return {text: "OpenScience file transfer stopped. Original files and any pending checkpoint are retained. Check the native OpenScience connection and local Python 3.9+ availability, then retry the same command."};
    }
}

export function register(on) {
  on("session.start", async ($, e, next) => {
    await $.tool.register({
      name: "artifact_transfer",
      description: "Upload or download an explicitly user-requested OpenScience original artifact without putting file bytes into the model context. Use the exact selected absolute local path, and an existing artifact SHA-256 for download. Preserve originals; never use for conversation capture, credential files, directory scans, or unsolicited transfers. Requires Python 3.9+ and the connected OpenScience server. Returns verified metadata or a retained pending status.",
      inputSchema: {
        type: "object", additionalProperties: false, required: ["operation", "path"],
        properties: {
          operation: {type: "string", enum: ["upload", "download"]},
          path: {type: "string", description: "Explicit absolute local source or destination path"},
          sha256: {type: "string", pattern: "^[0-9a-f]{64}$", description: "Required existing artifact digest for download"},
          project_id: {type: "string", description: "Existing project identifier for an upload, if relevant"},
        },
      },
    });
    await $.command.register({
      name: "openscience-file",
      description: "Transfer an explicitly requested original artifact through OpenScience",
      argumentHint: '{"operation":"upload|download","path":"/absolute/path",...}',
    });
    return next(e);
  });
  on("command.run", {command: "openscience-file"}, async ($, e, next) => {
    let request;
    try { request = JSON.parse(e.args || "{}"); }
    catch { return {text: "OpenScience file transfer stopped: use an artifact request with an absolute path."}; }
    return transfer($, request, next.budget);
  }).catch(() => ({text: "OpenScience file transfer interrupted; pending checkpoints are retained."}));
  on("tool.call", {tool: "mcp__openscience-remote__artifact_transfer"}, async ($, e, next) => {
    const request = {operation: e.operation, path: e.path};
    if (e.sha256 !== undefined) request.sha256 = e.sha256;
    if (e.project_id !== undefined) request.project_id = e.project_id;
    const result = await transfer($, request, next.budget);
    return {result: result.text};
  }).catch(() => ({result: "OpenScience file transfer interrupted; pending checkpoints are retained."}));
}
