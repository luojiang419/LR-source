// 使用已注册的启动命令，通过真实 MCP STDIO 连接验证；不修改 Codex 或分析目标。
import { createRequire } from 'node:module';
import { readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const [launchPath, targetPath, outputPath] = process.argv.slice(2);
if (!launchPath || !targetPath || !outputPath) {
  throw new Error('Usage: node verify_mcp.mjs launch.json target.dll output.json');
}
const launch = JSON.parse(await readFile(launchPath, 'utf8'));
const packageRequire = createRequire(pathToFileURL(path.resolve(launch.packageJson)));
const { Client } = await import(pathToFileURL(packageRequire.resolve('@modelcontextprotocol/client')).href);
const { StdioClientTransport } = await import(pathToFileURL(packageRequire.resolve('@modelcontextprotocol/client/stdio')).href);
const client = new Client({ name: 'rea-deployment-verifier', version: '1.0.0' });
const transport = new StdioClientTransport({
  command: launch.command,
  args: launch.args,
  ...(launch.env ? { env: { ...process.env, ...launch.env } } : {}),
  stderr: 'pipe',
});
let diagnostics = '';
transport.stderr?.on('data', (chunk) => { diagnostics += chunk.toString(); });
const result = { observed_at: new Date().toISOString(), launch, target: path.resolve(targetPath), calls: {} };
try {
  await client.connect(transport, { timeout: 30000 });
  result.server = client.getServerVersion();
  const tools = await client.listTools(undefined, { timeout: 30000 });
  result.tool_count = tools.tools.length;
  for (const [name, args] of [
    ['binary_session', {}],
    ['inspect_pe_resources', { path: path.resolve(targetPath) }],
  ]) {
    const tool = tools.tools.find((item) => item.name === name);
    if (!tool) throw new Error(`Missing tool: ${name}`);
    const call = await client.callTool({ name, arguments: args }, { toolDefinition: tool, timeout: 30000 });
    result.calls[name] = call;
    if (call.isError) throw new Error(`MCP tool returned an error: ${name}`);
  }
  result.success = true;
} catch (error) {
  result.success = false;
  result.error = String(error);
  process.exitCode = 1;
} finally {
  await client.close();
  await transport.close();
  result.stderr = diagnostics;
  await writeFile(outputPath, JSON.stringify(result, null, 2) + '\n');
}
console.log(JSON.stringify({ success: result.success, server: result.server, tool_count: result.tool_count, error: result.error, output: path.resolve(outputPath) }, null, 2));
