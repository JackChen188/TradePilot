/**
 * clawbot_bridge.mjs
 *
 * ClawBot 用户原话 -> 本地 Codex CLI / OpenAI Codex Responses API -> PushPlus 回复
 *
 * TradePilot.exe 收到微信消息后写入 TP_AI_QUEUE_PATH 队列；
 * 本脚本消费队列，把未命中本地规则的自然语言交给 Codex 对话。
 *
 * 环境变量：
 *   TP_CODEX_PATH      - 可选，本地 codex.exe 完整路径
 *   OPENAI_API_KEY     - 可选；本地 Codex CLI 不可用时走 OpenAI API
 *   TP_CODEX_MODEL     - 可选；CLI 不设则用 Codex 默认模型，API 默认 gpt-5.1-codex
 *   TP_CODEX_BASE_URL  - 可选；API 默认 https://api.openai.com/v1
 *   PUSHPLUS_TOKEN     - PushPlus token
 *   TP_CLAWBOT_REPLY_CHANNEL - 回复渠道，默认 clawbot
 *   TP_AI_QUEUE_PATH   - 队列文件绝对路径（TradePilot 启动时注入）
 *   TP_PROJECT_ROOT    - TradePilot 项目根目录
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, unlinkSync } from "node:fs";
import { spawn } from "node:child_process";
import { tmpdir } from "node:os";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dir = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = process.env.TP_PROJECT_ROOT || __dir;

const CONFIG_OVERRIDE_KEYS = new Set(["TP_PUSHPLUS_CHANNEL", "TP_CLAWBOT_REPLY_CHANNEL"]);

function loadSecretsEnv() {
  const paths = [
    process.env.TP_AI_QUEUE_PATH ? resolve(dirname(process.env.TP_AI_QUEUE_PATH), "secrets.env") : "",
    resolve(PROJECT_ROOT, "dist", "logs", "secrets.env"),
    resolve(PROJECT_ROOT, "logs", "secrets.env"),
    resolve(PROJECT_ROOT, "secrets.env"),
  ].filter(Boolean);

  for (const p of paths) {
    if (!existsSync(p)) continue;
    try {
      for (const line of readFileSync(p, "utf-8").split(/\r?\n/)) {
        const s = line.trim();
        if (!s || s.startsWith("#") || !s.includes("=")) continue;
        const i = s.indexOf("=");
        const k = s.slice(0, i).trim();
        const v = s.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
        if (!k || !v) continue;
        if (CONFIG_OVERRIDE_KEYS.has(k) || !process.env[k]) process.env[k] = v;
      }
    } catch {
      /* ignore malformed secrets file */
    }
  }
}

loadSecretsEnv();

const QUEUE_PATH =
  process.env.TP_AI_QUEUE_PATH || resolve(PROJECT_ROOT, "dist", "logs", "clawbot_ai_queue.json");
const CODEX_STATE_PATH = resolve(dirname(QUEUE_PATH), "clawbot_codex_state.json");
const BRIDGE_LOCK_PATH = resolve(dirname(QUEUE_PATH), "clawbot_bridge.lock");
const POLL_INTERVAL_MS = Number.parseInt(process.env.TP_CODEX_POLL_INTERVAL_MS || "2000", 10);

const OPENAI_API_KEY = process.env.OPENAI_API_KEY || "";
const OPENAI_BASE_URL = (process.env.TP_CODEX_BASE_URL || "https://api.openai.com/v1").replace(/\/+$/, "");
const CODEX_MODEL = process.env.TP_CODEX_MODEL || "";
const API_CODEX_MODEL = CODEX_MODEL || "gpt-5.1-codex";
const CODEX_REASONING_EFFORT = process.env.TP_CODEX_REASONING_EFFORT || "low";
const CODEX_TIMEOUT_MS = Number.parseInt(process.env.TP_CODEX_TIMEOUT_MS || "180000", 10);
const PUSHPLUS_TOKEN = process.env.PUSHPLUS_TOKEN || "";
const PUSHPLUS_CHANNEL = process.env.TP_CLAWBOT_REPLY_CHANNEL || "clawbot";

if (!PUSHPLUS_TOKEN) {
  console.error("[bridge] 缺少 PUSHPLUS_TOKEN");
  process.exit(1);
}

function isPidAlive(pid) {
  if (!pid || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e?.code === "EPERM";
  }
}

function acquireBridgeSingleton() {
  if (existsSync(BRIDGE_LOCK_PATH)) {
    try {
      const raw = readFileSync(BRIDGE_LOCK_PATH, "utf-8").trim();
      const oldPid = Number.parseInt(raw, 10);
      if (isPidAlive(oldPid)) {
        console.error(`[bridge] 已有实例在运行 (pid=${oldPid})，本进程退出`);
        process.exit(0);
      }
    } catch {
      /* stale lock */
    }
    try {
      unlinkSync(BRIDGE_LOCK_PATH);
    } catch {
      /* ignore */
    }
  }

  writeFileSync(BRIDGE_LOCK_PATH, String(process.pid), "utf-8");
  const release = () => {
    try {
      if (!existsSync(BRIDGE_LOCK_PATH)) return;
      const raw = readFileSync(BRIDGE_LOCK_PATH, "utf-8").trim();
      if (Number.parseInt(raw, 10) === process.pid) unlinkSync(BRIDGE_LOCK_PATH);
    } catch {
      /* ignore */
    }
  };
  process.on("exit", release);
  process.on("SIGINT", () => {
    release();
    process.exit(0);
  });
  process.on("SIGTERM", () => {
    release();
    process.exit(0);
  });
}

async function pushToClawBot(title, content) {
  const payload = {
    token: PUSHPLUS_TOKEN,
    title,
    content: String(content).slice(0, 4000),
    template: "txt",
    channel: PUSHPLUS_CHANNEL,
  };
  const resp = await fetch("https://www.pushplus.plus/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const json = await resp.json().catch(() => ({}));
  if (json?.code === 200) {
    console.log(`[bridge] 已回复: ${title}`);
  } else {
    console.warn(`[bridge] PushPlus 失败: ${JSON.stringify(json).slice(0, 200)}`);
  }
}

function readJson(path, fallback) {
  if (!existsSync(path)) return fallback;
  try {
    return JSON.parse(readFileSync(path, "utf-8")) || fallback;
  } catch {
    return fallback;
  }
}

function writeJson(path, data) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, JSON.stringify(data, null, 2), "utf-8");
}

function readQueue() {
  return readJson(QUEUE_PATH, []);
}

function saveQueue(queue) {
  writeJson(QUEUE_PATH, queue);
}

function readCodexState() {
  return readJson(CODEX_STATE_PATH, {});
}

function saveCodexState(state) {
  writeJson(CODEX_STATE_PATH, state);
}

const CODEX_INSTRUCTIONS = `你是 TradePilot 交易助手的微信远程对话接口，运行在 Codex 风格的对话模式中。
项目目录：${PROJECT_ROOT}

你会收到 ClawBot 用户发来的自然语言，以及 TradePilot 提供的账户快照。请用中文回复。

能力边界：
- 你可以解释策略、风控、持仓、日志含义、配置项和运维步骤。
- 你不能直接下单；真实交易必须继续走 TradePilot 的确认码和人工确认流程。
- 你不能假装已经修改本地文件或执行命令；需要修改代码时，给出清晰建议，让本地 Codex 维护线程执行。
- 涉及交易建议时，必须说明这是辅助分析，不是保证收益的投资承诺。

回复要求：
- 先给结论，再给关键理由。
- 简洁，适合微信阅读。
- 如果问题含股票代码，优先结合快照中的持仓、现金、候选排名和风险状态。`;

function buildUserInput(text, context) {
  let ctxBlock = "";
  if (context && typeof context === "object") {
    try {
      ctxBlock = `\n\n【TradePilot 实时账户快照】\n${JSON.stringify(context, null, 2)}`;
    } catch {
      ctxBlock = "";
    }
  }
  return `${ctxBlock}\n\n【用户微信原话】\n${text}`;
}

function extractResponseText(json) {
  if (typeof json?.output_text === "string" && json.output_text.trim()) {
    return json.output_text.trim();
  }
  const parts = [];
  for (const item of json?.output || []) {
    for (const c of item?.content || []) {
      if (typeof c?.text === "string" && c.text.trim()) parts.push(c.text.trim());
    }
  }
  return parts.join("\n").trim();
}

function shouldRetryWithoutPreviousResponse(err) {
  const msg = String(err?.message || err || "");
  return (
    msg.includes("previous_response_id") ||
    msg.includes("No response found") ||
    msg.includes("not found") ||
    msg.includes("expired")
  );
}

function candidateCodexPaths() {
  const out = [];
  if (process.env.TP_CODEX_PATH) out.push(process.env.TP_CODEX_PATH);
  const local = process.env.LOCALAPPDATA || "";
  if (local) {
    out.push(resolve(local, "OpenAI", "Codex", "bin", "codex.exe"));
  }
  out.push("codex");
  return out;
}

function findCodexExecutable() {
  for (const p of candidateCodexPaths()) {
    if (p.includes("\\") || p.includes("/") || p.endsWith(".exe")) {
      if (existsSync(p)) return p;
      continue;
    }
    return p;
  }
  return "";
}

function runProcess(file, args, { input = "", timeoutMs = CODEX_TIMEOUT_MS } = {}) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(file, args, {
      cwd: PROJECT_ROOT,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"],
      env: process.env,
    });
    let stdout = "";
    let stderr = "";
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      try {
        child.kill("SIGTERM");
      } catch {
        /* ignore */
      }
      reject(new Error(`Codex CLI timeout after ${Math.round(timeoutMs / 1000)}s`));
    }, timeoutMs);

    child.stdout.on("data", (b) => {
      stdout += b.toString("utf-8");
    });
    child.stderr.on("data", (b) => {
      stderr += b.toString("utf-8");
    });
    child.on("error", (err) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(err);
    });
    child.on("close", (code) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (code === 0) {
        resolvePromise({ stdout, stderr });
      } else {
        reject(new Error(`Codex CLI exit ${code}: ${(stderr || stdout).slice(0, 1200)}`));
      }
    });
    child.stdin.end(input, "utf-8");
  });
}

async function handleMessageWithCodexCli(text, context) {
  const codex = findCodexExecutable();
  if (!codex) throw new Error("Codex CLI not found");

  const stamp = `${Date.now()}_${Math.random().toString(16).slice(2)}`;
  const outPath = resolve(tmpdir(), `tradepilot_codex_reply_${stamp}.txt`);
  const input = `${CODEX_INSTRUCTIONS}\n\n${buildUserInput(text, context)}`;
  const args = [
    "--ask-for-approval",
    "never",
    "exec",
    "--ephemeral",
    "--skip-git-repo-check",
    "--sandbox",
    "read-only",
    "-C",
    PROJECT_ROOT,
    "-o",
    outPath,
  ];
  if (CODEX_MODEL) args.splice(2, 0, "--model", CODEX_MODEL);
  args.push("-");

  console.log(`[bridge] -> Codex CLI: ${text.slice(0, 80)}`);
  await runProcess(codex, args, { input });
  try {
    const reply = readFileSync(outPath, "utf-8").trim();
    return reply || "（Codex CLI 已完成，但未返回文字，请换种问法）";
  } finally {
    try {
      unlinkSync(outPath);
    } catch {
      /* ignore */
    }
  }
}

async function createCodexResponse(input, previousResponseId) {
  const body = {
    model: API_CODEX_MODEL,
    instructions: CODEX_INSTRUCTIONS,
    input,
    reasoning: { effort: CODEX_REASONING_EFFORT },
  };
  if (previousResponseId) body.previous_response_id = previousResponseId;

  const resp = await fetch(`${OPENAI_BASE_URL}/responses`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${OPENAI_API_KEY}`,
    },
    body: JSON.stringify(body),
  });
  const json = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = json?.error?.message || JSON.stringify(json).slice(0, 500) || resp.statusText;
    throw new Error(`OpenAI Responses API ${resp.status}: ${detail}`);
  }
  return json;
}

async function handleMessage(text, context) {
  try {
    return await handleMessageWithCodexCli(text, context);
  } catch (err) {
    if (!OPENAI_API_KEY) throw err;
    console.warn(`[bridge] Codex CLI 不可用，回退 Responses API: ${String(err?.message || err).slice(0, 200)}`);
  }

  const state = readCodexState();
  const input = buildUserInput(text, context);
  console.log(`[bridge] -> Codex API(${API_CODEX_MODEL}): ${text.slice(0, 80)}`);

  let json;
  try {
    json = await createCodexResponse(input, state.previous_response_id || "");
  } catch (err) {
    if (!state.previous_response_id || !shouldRetryWithoutPreviousResponse(err)) throw err;
    console.warn("[bridge] previous_response_id 失效，重置对话后重试");
    json = await createCodexResponse(input, "");
  }

  const reply = extractResponseText(json) || "（Codex 已处理，但未返回文字，请换种问法）";
  saveCodexState({
    previous_response_id: json?.id || "",
    model: API_CODEX_MODEL,
    updated_at: new Date().toISOString(),
  });
  return reply;
}

let processing = false;

async function tick() {
  if (processing) return;
  processing = true;
  try {
    const queue = readQueue();
    const pending = queue.filter((m) => m.status === "pending");
    for (const msg of pending) {
      try {
        const reply = await handleMessage(msg.text, msg.context);
        const preview = String(msg.text).slice(0, 30).replace(/\n/g, " ");
        await pushToClawBot(`Codex回复「${preview}」`, reply);
      } catch (err) {
        const errMsg = err?.message || String(err);
        console.error("[bridge] 处理失败:", errMsg);
        await pushToClawBot(
          "Codex处理失败",
          `${errMsg.slice(0, 700)}\n\n提示：可发「帮助」查看本地指令，或确认 OPENAI_API_KEY / TP_CODEX_MODEL 配置。`
        );
      }

      const idx = queue.findIndex((m) => m.msg_id === msg.msg_id);
      if (idx >= 0) {
        queue[idx].status = "done";
        queue[idx].replied_at = new Date().toISOString();
      }
      saveQueue(queue);
    }
  } finally {
    processing = false;
  }
}

acquireBridgeSingleton();
console.log("[bridge] ClawBot -> Codex 桥接已启动");
console.log(`[bridge] 队列: ${QUEUE_PATH}`);
console.log(`[bridge] 项目: ${PROJECT_ROOT}`);
console.log(`[bridge] Codex CLI: ${findCodexExecutable() || "not found"}`);
console.log(`[bridge] API模型: ${API_CODEX_MODEL}`);
console.log(`[bridge] 推送渠道: ${PUSHPLUS_CHANNEL}`);

tick();
setInterval(tick, POLL_INTERVAL_MS);
