import { Agent } from "@cursor/sdk";
import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
for (const p of [
  resolve(root, "dist", "logs", "secrets.env"),
  resolve(root, "logs", "secrets.env"),
  resolve(root, "secrets.env"),
]) {
  if (!existsSync(p)) continue;
  for (const line of readFileSync(p, "utf-8").split(/\r?\n/)) {
    const s = line.trim();
    if (!s || s.startsWith("#") || !s.includes("=")) continue;
    const i = s.indexOf("=");
    const k = s.slice(0, i).trim();
    let v = s.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
    if (k && v && !process.env[k]) process.env[k] = v;
  }
}

const key = process.env.CURSOR_API_KEY || "";
if (!key) {
  console.error("no CURSOR_API_KEY");
  process.exit(1);
}

try {
  const agent = await Agent.create({
    apiKey: key,
    model: { id: "composer-2" },
    local: { cwd: root },
  });
  const run = await agent.send("Reply with exactly: pong");
  const result = await run.wait();
  console.log("status:", result.status);
  console.log("result:", JSON.stringify(result, null, 2).slice(0, 2000));
  await agent[Symbol.asyncDispose]();
} catch (e) {
  console.error("ERR", e?.name, e?.message);
  if (e?.cause) console.error("cause", e.cause);
  process.exit(2);
}
