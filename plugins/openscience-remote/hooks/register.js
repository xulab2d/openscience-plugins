// Requested artifact transfers only. No history hooks, token access, or model call.
const SERVER = "plugin:openscience-remote:openscience";
const ALLOWED = ["begin_upload", "upload_status", "upload_chunk", "complete_upload", "read_artifact",
  "read_scientific_file"];
const REQUEST_KEYS = ["operation", "path", "sha256", "project_id", "source", "relative_path"];
const RETAINED = "Original files and any pending checkpoint are retained.";

// A failure carries only a fixed stage name and a short class name: never a
// provider message or helper stdout, which can contain original bytes.
class TransferError extends Error {
  constructor(stage, detail) {
    super(stage);
    this.stage = stage;
    this.detail = detail;
  }
}

const SAFE_NAME = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;

function safeName(value) {
  return typeof value === "string" && SAFE_NAME.test(value) ? value : undefined;
}

function unpack(result, tool) {
  if (!result) throw new TransferError("server_no_result", tool);
  if (result.isError) throw new TransferError("server_error", tool);
  if (result.structuredContent) return result.structuredContent;
  const block = result.content?.find(item => item.type === "text");
  if (!block) throw new TransferError("server_no_structured_result", tool);
  try { return JSON.parse(block.text); }
  catch { throw new TransferError("server_unparsed_result", tool); }
}

function helperError(stderr) {
  // The helper writes only {"error_type": <exception class>} to stderr.
  try { return safeName(JSON.parse(stderr || "{}").error_type); }
  catch { return undefined; }
}

function failureText(error) {
  const stage = error instanceof TransferError ? error.stage : "unexpected";
  const detail = error instanceof TransferError ? safeName(error.detail) : safeName(error?.name);
  const explain = {
    invalid_request: "the request was malformed (use operation, an absolute path, and sha256 for downloads)",
    helper_unavailable: "the local Python 3.9+ helper could not be started",
    helper_failed: "the local helper rejected a step",
    helper_bad_output: "the local helper returned unreadable output",
    invalid_step: "the local helper asked for a call this plugin does not allow",
    mcp_call_failed: "the native OpenScience connection call failed",
    server_no_result: "the OpenScience server returned nothing",
    server_error: "the OpenScience server returned an error",
    server_no_structured_result: "the OpenScience server returned no structured result",
    server_unparsed_result: "the OpenScience server returned an unreadable result (a reply too large for Claude Code's tool-result limit arrives this way)",
    unexpected: "an unexpected error occurred",
  }[stage] || "an unexpected error occurred";
  return `OpenScience file transfer stopped at ${stage}${detail ? ` (${detail})` : ""}: ${explain}. ${RETAINED} Retry the same request after fixing the cause.`;
}

function isPermissionDenial(error) {
  const message = typeof error?.message === "string" ? error.message : "";
  return error?.name === "HooksError" ||
    /permission/i.test(message) && /(grant|allow|denied|deny)/i.test(message);
}

async function transfer($, request, budget) {
    let input;
    let pendingTool;
    try {
      // No general-purpose RPC forwarding or conversation upload route.
      if (!request || !["upload", "download"].includes(request.operation) ||
          typeof request.path !== "string" || !request.path ||
          Object.keys(request).some(key => !REQUEST_KEYS.includes(key))) {
        throw new TransferError("invalid_request");
      }
      input = {request};
      while (true) {
        let run;
        try {
          run = await $.process.run(["python3", `${$.plugin.root}/scripts/transfer_step.py`], {
            stdin: JSON.stringify(input), timeoutMs: 600000,
          });
        } catch (error) {
          throw new TransferError("helper_unavailable", error?.name);
        }
        if (run.exitCode !== 0) throw new TransferError("helper_failed", helperError(run.stderr));
        if (run.isStdoutTruncated || run.isStderrTruncated) throw new TransferError("helper_bad_output");
        let step;
        try { step = JSON.parse(run.stdout); }
        catch { throw new TransferError("helper_bad_output"); }
        if (step.done || step.pending) {
          return {text: JSON.stringify(step.summary)};
        }
        if (!ALLOWED.includes(step.call?.name) || typeof step.id !== "string" ||
            typeof step.action_id !== "string") throw new TransferError("invalid_step");
        if (budget && budget.remainingMs < 500) {
          return {text: "Transfer checkpoint saved. Run the same /openscience-file command to resume."};
        }
        pendingTool = step.call.name;
        let result;
        try {
          result = await $.mcp.call(SERVER, step.call.name, step.call.arguments);
        } catch (error) {
          if (isPermissionDenial(error)) throw error;
          throw new TransferError("mcp_call_failed", error?.name);
        }
        const response = unpack(result, step.call.name);
        pendingTool = undefined;
        input = {id: step.id, action_id: step.action_id, response};
      }
    } catch (error) {
      // Classify the observed native permission gate without echoing its text.
      // Never grant a permission or mistake provider content for an approval.
      if (pendingTool && ALLOWED.includes(pendingTool) && !(error instanceof TransferError) &&
          isPermissionDenial(error)) {
        const tools = request.operation === "download" ? [pendingTool] :
          ["begin_upload", "upload_status", "upload_chunk", "complete_upload"];
        const rules = tools.map(name => `mcp__plugin_openscience-remote_openscience__${name}`).join(", ");
        return {text: `OpenScience file transfer needs native tool permission. Signing in does not grant this permission. If this transfer is authorized, open /permissions and review Allow rules for only: ${rules}. Then retry the identical transfer. Follow organization policy; do not override a denial. ${RETAINED}`};
      }
      // Neither provider exceptions nor helper stdout may be copied to the chat:
      // they can contain original bytes. Existing checkpoints remain available.
      return {text: failureText(error)};
    }
}

export function register(on) {
  on("session.start", async ($, e, next) => {
    await $.tool.register({
      name: "artifact_transfer",
      description: "Upload or download an explicitly user-requested OpenScience original without putting file bytes into the model context. Download a catalog artifact by its SHA-256, or a mapped source file (for example a Dropbox file) by source, relative_path and the sha256 that resolve_scientific_file reports. Use the exact selected absolute local path. Preserve originals; never use for conversation capture, credential files, directory scans, or unsolicited transfers. Requires Python 3.9+ and the connected OpenScience server. Returns verified metadata, a retained pending status, or the stage at which a transfer stopped.",
      inputSchema: {
        type: "object", additionalProperties: false, required: ["operation", "path"],
        properties: {
          operation: {type: "string", enum: ["upload", "download"]},
          path: {type: "string", description: "Explicit absolute local source or destination path"},
          sha256: {type: "string", pattern: "^[0-9a-f]{64}$", description: "Required for download: the artifact digest, or the source file's sha256 from resolve_scientific_file"},
          project_id: {type: "string", description: "Existing project identifier for an upload, if relevant"},
          source: {type: "string", description: "Download only: mapped source name, e.g. dropbox"},
          relative_path: {type: "string", description: "Download only: file path within the source"},
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
    for (const key of ["sha256", "project_id", "source", "relative_path"]) {
      if (e[key] !== undefined) request[key] = e[key];
    }
    const result = await transfer($, request, next.budget);
    return {result: result.text};
  }).catch(() => ({result: "OpenScience file transfer interrupted; pending checkpoints are retained."}));
}
