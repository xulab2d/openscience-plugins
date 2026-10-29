import {test, expect} from "claude-code/testing";

const command = "openscience-file";

test("malformed and capture requests do not read files or call MCP", async ($, on) => {
  let reads = 0;
  on("process.run", async () => { reads++; throw new Error("unexpected read"); });
  on("mcp.call", async () => { reads++; throw new Error("unexpected upload"); });
  for (const args of ["not json", '{"operation":"capture","path":"/synthetic"}',
    '{"operation":"upload","path":"/synthetic","server":"untrusted"}']) {
    const result = await $.command.run({command, args});
    expect(result.text).toContain("stopped");
  }
  expect(reads).toBe(0);
});

test("native connection carries bytes privately and returns only verified summary", async ($, on) => {
  const seen: string[] = [];
  let processes = 0;
  on("process.run", async ($, e) => {
    expect(e.argv[0]).toBe("python3");
    expect(e.argv.length).toBe(2);
    const input = JSON.parse(e.init?.stdin || "{}");
    const number = processes++;
    if (number === 0) {
      expect(input.request.operation).toBe("upload");
      return {value: {exitCode: 0, stderr: "", stdout: JSON.stringify({id: "id", action_id: "a",
        call: {name: "upload_chunk", arguments: {upload_id: "u", offset: 0, base64_data: "AP9vcmlnaW5hbA=="}}})}};
    }
    expect(input.response.next_offset).toBe(10);
    return {value: {exitCode: 0, stderr: "", stdout: JSON.stringify({done: true,
      summary: {ok: true, verified: true, local_path: "/synthetic/original.bin", bytes: 10}})}};
  });
  on("mcp.call", async ($, e) => {
    seen.push(e.server);
    expect(e.tool).toBe("upload_chunk");
    expect(e.args?.base64_data).toBe("AP9vcmlnaW5hbA==");
    return {value: {content: [], structuredContent: {next_offset: 10}, isError: false}};
  });
  const result = await $.command.run({command, args: '{"operation":"upload","path":"/synthetic/original.bin"}'});
  expect(seen).toEqual(["plugin:openscience-remote:openscience"]);
  expect(JSON.parse(result.text || "{}").verified).toBe(true);
  expect(result.text).not.toContain("AP9vcmlnaW5hbA==");
});

test("provider errors never copy private response text into the conversation", async ($, on) => {
  on("process.run", async () => ({value: {exitCode: 0, stderr: "", stdout: JSON.stringify({id: "id", action_id: "a",
    call: {name: "read_artifact", arguments: {sha256: "a".repeat(64), offset: 0}}})}}));
  on("mcp.call", async () => ({value: {isError: true, content: [{type: "text", text: "synthetic-private-original"}]}}));
  const result = await $.command.run({command, args: '{"operation":"download","path":"/synthetic/output","sha256":"' + "a".repeat(64) + '"}'});
  expect(result.text).toContain("stopped");
  expect(result.text).not.toContain("synthetic-private-original");
});

test("pending Drive receipt is retained and is not reported as verified", async ($, on) => {
  on("process.run", async () => ({value: {exitCode: 0, stderr: "", stdout: JSON.stringify({pending: true,
    summary: {ok: false, pending_cloud_sync: true}})}}));
  on("mcp.call", async () => { throw new Error("No additional request allowed"); });
  const result = await $.command.run({command, args: '{"operation":"upload","path":"/synthetic/original"}'});
  expect(JSON.parse(result.text || "{}")).toEqual({ok: false, pending_cloud_sync: true});
});

test("normal artifact tool requests use the same private byte handler", async ($, on) => {
  let processes = 0;
  on("process.run", async ($, e) => {
    const input = JSON.parse(e.init?.stdin || "{}");
    expect(input.request).toEqual({operation: "download", path: "/synthetic/output", sha256: "a".repeat(64)});
    processes++;
    return {value: {exitCode: 0, stderr: "", stdout: JSON.stringify({done: true,
      summary: {ok: true, verified: true, local_path: "/synthetic/output", bytes: 10}})}};
  });
  const result = await $.tool.call({tool: "mcp__openscience-remote__artifact_transfer", operation: "download",
    path: "/synthetic/output", sha256: "a".repeat(64)});
  expect(processes).toBe(1);
  expect(JSON.parse(result.result).verified).toBe(true);
});


test("native permission denials give scoped guidance without copying exceptions", async ($, on) => {
  let selected = "read_artifact";
  on("process.run", async () => ({value: {exitCode: 0, stderr: "", stdout: JSON.stringify({id: "id", action_id: "a",
    call: {name: selected, arguments: {}}})}}));
  on("mcp.call", async () => ({deny: "Claude requested permissions to use synthetic-private-original, but you haven't granted it yet."}));
  const download = await $.command.run({command, args: '{"operation":"download","path":"/synthetic/output","sha256":"' + "a".repeat(64) + '"}'});
  expect(download.text).toContain("/permissions");
  expect(download.text).toContain("mcp__plugin_openscience-remote_openscience__read_artifact");
  expect(download.text).not.toContain("begin_upload");
  expect(download.text).not.toContain("synthetic-private-original");
  expect(download.text).not.toContain("Python");
  selected = "begin_upload";
  const upload = await $.command.run({command, args: '{"operation":"upload","path":"/synthetic/original"}'});
  for (const name of ["begin_upload", "upload_status", "upload_chunk", "complete_upload"]) {
    expect(upload.text).toContain("mcp__plugin_openscience-remote_openscience__" + name);
  }
  expect(upload.text).not.toContain("read_artifact");
  expect(upload.text).not.toContain("synthetic-private-original");
});
