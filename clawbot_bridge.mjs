/**
 * clawbot_bridge.mjs
 *
 * ClawBot 用户原话 → Cursor Agent（多轮对话）→ PushPlus 回复
 *
 * TradePilot.exe 收到微信消息后写入 TP_AI_QUEUE_PATH 队列；
 * 本脚本消费队列，把用户原话交给 Cursor Agent 理解，不再依赖关键词硬匹配。
 *
 * 环境变量：
 *   CURSOR_API_KEY      - Cursor Cloud Agents API Key
 *   PUSHPLUS_TOKEN      - PushPlus token
 *   TP_CLAWBOT_REPLY_CHANNEL - 回复渠道，默认 clawbot
 *   TP_AI_QUEUE_PATH    - 队列文件绝对路径（TradePilot 启动时注入）
 *   TP_PROJECT_ROOT     - TradePilot 项目根目录
 */

import { Agent } from "@cursor/sdk";
import { readFileSync, writeFileSync, existsSync, mkdirSync, unlinkSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dir = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = process.env.TP_PROJECT_ROOT || __dir;

const _CONFIG_OVERRIDE_KEYS = new Set(["TP_PUSHPLUS_CHANNEL", "TP_CLAWBOT_REPLY_CHANNEL"]);

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
        let v = s.slice(i + 1).trim().replace(/^['"]|['"]$/g, "");
        if (!k || !v) continue;
        if (_CONFIG_OVERRIDE_KEYS.has(k) || !process.env[k]) process.env[k] = v;
      }
    } catch {}
  }
}

loadSecretsEnv();
const QUEUE_PATH =
  process.env.TP_AI_QUEUE_PATH || resolve(PROJECT_ROOT, "dist", "logs", "clawbot_ai_queue.json");
const AGENT_STATE_PATH = resolve(dirname(QUEUE_PATH), "clawbot_agent_state.json");
const BRIDGE_LOCK_PATH = resolve(dirname(QUEUE_PATH), "clawbot_bridge.lock");
const POLL_INTERVAL_MS = 2_000;

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
      if (existsSync(BRIDGE_LOCK_PATH)) {
        const raw = readFileSync(BRIDGE_LOCK_PATH, "utf-8").trim();
        if (Number.parseInt(raw, 10) === process.pid) unlinkSync(BRIDGE_LOCK_PATH);
      }
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

const CURSOR_API_KEY = process.env.CURSOR_API_KEY || "";
const PUSHPLUS_TOKEN = process.env.PUSHPLUS_TOKEN || "";
// ClawBot 对话回复走 clawbot；系统舆情推送仍用 TP_PUSHPLUS_CHANNEL=wechat
const PUSHPLUS_CHANNEL = process.env.TP_CLAWBOT_REPLY_CHANNEL || "clawbot";

if (!CURSOR_API_KEY) {
  console.error("[bridge] 缺少 CURSOR_API_KEY");
  process.exit(1);
}
if (!PUSHPLUS_TOKEN) {
  console.error("[bridge] 缺少 PUSHPLUS_TOKEN");
  process.exit(1);
}

const AGENT_OPTS = {
  apiKey: CURSOR_API_KEY,
  model: { id: "composer-2" },
  local: { cwd: PROJECT_ROOT },
};

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

function readQueue() {
  if (!existsSync(QUEUE_PATH)) return [];
  try {
    return JSON.parse(readFileSync(QUEUE_PATH, "utf-8")) || [];
  } catch {
    return [];
  }
}

function saveQueue(queue) {
  mkdirSync(dirname(QUEUE_PATH), { recursive: true });
  writeFileSync(QUEUE_PATH, JSON.stringify(queue, null, 2), "utf-8");
}

function clearLegacyAgentState() {
  try {
    if (existsSync(AGENT_STATE_PATH)) unlinkSync(AGENT_STATE_PATH);
  } catch {
    /* ignore */
  }
}

const SYSTEM_CONTEXT = `你是 TradePilot 交易助手的远程对话接口（用户通过微信发消息）。
项目目录：${PROJECT_ROOT}
你可以读取/修改 TradePilot 代码与配置，查询 logs/ 日志，总结股票新闻，解释策略。

用户会用自然语言提问，不要要求固定指令格式。常见需求：
- 持仓、余额、某只股票新闻/价格
- 修改策略参数、推送设置
- 分析日志或解释代码

回复要求：中文、简洁、先给结论；若修改了文件请说明路径与改动。`;

function buildPrompt(text, context) {
  let ctxBlock = "";
  if (context && typeof context === "object") {
    try {
      ctxBlock = `\n\n【TradePilot 实时账户快照（供参考）】\n${JSON.stringify(context, null, 2)}`;
    } catch {
      ctxBlock = "";
    }
  }
  return `${SYSTEM_CONTEXT}${ctxBlock}\n\n【用户微信原话】\n${text}`;
}

function isRetryableAgentError(err) {
  const m = String(err?.message || err || "");
  return (
    m.includes("SQLITE_CONSTRAINT") ||
    m.includes("UNIQUE constraint") ||
    m.includes("AgentRunConflict") ||
    m.includes("already has an active run")
  );
}

function extractReply(result) {
  if (result?.result && typeof result.result === "string" && result.result.trim()) {
    return result.result.trim();
  }
  return "";
}

async function formatRunError(result) {
  const runId = result?.id ? ` run=${result.id}` : "";
  return `处理未完成（${result?.status || "unknown"}${runId}）。请换种问法或稍后再试。`;
}

/**
 * 每条消息用 Agent.prompt 一次性会话，避免 resume 多轮时
 * runs.agent_id + turn_number UNIQUE 冲突。
 */
async function handleMessage(text, context) {
  const prompt = buildPrompt(text, context);
  console.log(`[bridge] → Cursor: ${text.slice(0, 80)}`);

  let lastErr = null;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      if (attempt > 0) {
        console.warn("[bridge] Agent 重试...");
        clearLegacyAgentState();
        await new Promise((r) => setTimeout(r, 500));
      }
      const result = await Agent.prompt(prompt, AGENT_OPTS);
      if (result.status === "finished") {
        const reply = extractReply(result);
        return reply || "（已完成，但未返回文字，请换种问法）";
      }
      return await formatRunError(result);
    } catch (err) {
      lastErr = err;
      if (attempt === 0 && isRetryableAgentError(err)) continue;
      throw err;
    }
  }
  throw lastErr;
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
        await pushToClawBot(`🤖 AI回复「${preview}」`, reply);
      } catch (err) {
        const errMsg = err?.message || String(err);
        console.error("[bridge] 处理失败:", errMsg);
        await pushToClawBot(
          "🤖 AI处理失败",
          `${errMsg.slice(0, 400)}\n\n提示：可发「帮助」查看指令，或带股票代码如「AAPL 新闻」。`
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
clearLegacyAgentState();
console.log("[bridge] ClawBot → Cursor Agent 桥接已启动（每条消息独立会话）");
console.log(`[bridge] 队列: ${QUEUE_PATH}`);
console.log(`[bridge] 项目: ${PROJECT_ROOT}`);
console.log(`[bridge] 推送渠道: ${PUSHPLUS_CHANNEL}`);

tick();
setInterval(tick, POLL_INTERVAL_MS);
