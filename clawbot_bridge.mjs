/**
 * clawbot_bridge.mjs
 *
 * ClawBot 用户原话 → Cursor Agent（多轮对话）→ PushPlus/ClawBot 回复
 *
 * TradePilot.exe 收到微信消息后写入 TP_AI_QUEUE_PATH 队列；
 * 本脚本消费队列，把用户原话交给 Cursor Agent 理解，不再依赖关键词硬匹配。
 *
 * 环境变量：
 *   CURSOR_API_KEY      - Cursor Cloud Agents API Key
 *   PUSHPLUS_TOKEN      - PushPlus token
 *   TP_PUSHPLUS_CHANNEL - 默认 clawbot
 *   TP_AI_QUEUE_PATH    - 队列文件绝对路径（TradePilot 启动时注入）
 *   TP_PROJECT_ROOT     - TradePilot 项目根目录
 */

import { Agent } from "@cursor/sdk";
import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dir = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = process.env.TP_PROJECT_ROOT || __dir;

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
        if (k && v && !process.env[k]) process.env[k] = v;
      }
    } catch {}
  }
}

loadSecretsEnv();
const QUEUE_PATH =
  process.env.TP_AI_QUEUE_PATH || resolve(PROJECT_ROOT, "dist", "logs", "clawbot_ai_queue.json");
const AGENT_STATE_PATH = resolve(PROJECT_ROOT, "logs", "clawbot_agent_state.json");
const POLL_INTERVAL_MS = 2_000;

const CURSOR_API_KEY = process.env.CURSOR_API_KEY || "";
const PUSHPLUS_TOKEN = process.env.PUSHPLUS_TOKEN || "";
const PUSHPLUS_CHANNEL = process.env.TP_PUSHPLUS_CHANNEL || "clawbot";

if (!CURSOR_API_KEY) {
  console.error("[bridge] 缺少 CURSOR_API_KEY");
  process.exit(1);
}
if (!PUSHPLUS_TOKEN) {
  console.error("[bridge] 缺少 PUSHPLUS_TOKEN");
  process.exit(1);
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

function loadAgentId() {
  if (!existsSync(AGENT_STATE_PATH)) return "";
  try {
    return String(JSON.parse(readFileSync(AGENT_STATE_PATH, "utf-8")).agentId || "");
  } catch {
    return "";
  }
}

function saveAgentId(agentId) {
  mkdirSync(dirname(AGENT_STATE_PATH), { recursive: true });
  writeFileSync(AGENT_STATE_PATH, JSON.stringify({ agentId }, null, 2), "utf-8");
}

const SYSTEM_CONTEXT = `你是 TradePilot 交易助手的远程对话接口（用户通过微信 ClawBot 发消息）。
项目目录：${PROJECT_ROOT}
你可以读取/修改 TradePilot 代码与配置，查询 logs/ 日志，总结股票新闻，解释策略。

用户会用自然语言提问，不要要求固定指令格式。常见需求：
- 持仓、余额、某只股票新闻/价格
- 修改策略参数、推送设置
- 分析日志或解释代码

回复要求：中文、简洁、先给结论；若修改了文件请说明路径与改动。`;

/** @type {import('@cursor/sdk').Agent | null} */
let agent = null;

async function getAgent() {
  if (agent) return agent;

  const savedId = loadAgentId();
  const opts = {
    apiKey: CURSOR_API_KEY,
    model: { id: "composer-2" },
    local: { cwd: PROJECT_ROOT },
  };

  if (savedId) {
    try {
      agent = Agent.resume(savedId, opts);
      console.log(`[bridge] 恢复 Agent 会话: ${savedId}`);
      return agent;
    } catch (e) {
      console.warn(`[bridge] 恢复 Agent 失败，创建新会话: ${e.message}`);
    }
  }

  agent = Agent.create(opts);
  saveAgentId(agent.agentId);
  console.log(`[bridge] 新建 Agent 会话: ${agent.agentId}`);
  return agent;
}

function extractReply(result, run) {
  if (result?.result && typeof result.result === "string" && result.result.trim()) {
    return result.result.trim();
  }
  const conv = result?.conversation || (run?.supports?.("conversation") ? null : null);
  if (Array.isArray(conv) && conv.length > 0) {
    const assistantMsgs = conv.filter((m) => m.role === "assistant");
    if (assistantMsgs.length > 0) {
      const last = assistantMsgs[assistantMsgs.length - 1];
      const text = (last.content || [])
        .filter((b) => b.type === "text")
        .map((b) => b.text)
        .join("\n")
        .trim();
      if (text) return text;
    }
  }
  return "";
}

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

async function handleMessage(text, context) {
  console.log(`[bridge] → Cursor: ${text.slice(0, 80)}`);
  const a = await getAgent();
  const run = await a.send(buildPrompt(text, context));
  const result = await run.wait();

  if (result.status === "finished") {
    let reply = extractReply(result, run);
    if (!reply) reply = "（Agent 已完成，但未返回文字，请换种问法或稍后再试）";
    return reply;
  }
  return `Agent 状态: ${result.status}`;
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
        await pushToClawBot("🤖 AI处理失败", errMsg.slice(0, 500));
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

console.log("[bridge] ClawBot → Cursor Agent 桥接已启动");
console.log(`[bridge] 队列: ${QUEUE_PATH}`);
console.log(`[bridge] 项目: ${PROJECT_ROOT}`);

tick();
setInterval(tick, POLL_INTERVAL_MS);
