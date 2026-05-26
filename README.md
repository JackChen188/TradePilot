# TradePilot

美股量化辅助交易系统：富途 OpenD 行情/持仓、策略信号、PushPlus/ClawBot 微信推送与人工确认下单。

## 功能概览

- 多标的 Alpha 扫描与买卖信号（不自动下单，需 PushPlus 确认）
- 舆情 RSS 监控推送（英文标题附中文翻译）
- ClawBot 微信查询持仓/新闻/价格
- Cursor Agent 桥接（`clawbot_bridge.mjs`）理解自然语言指令

## 环境要求

| 组件 | 用途 | 是否必须 |
|------|------|----------|
| Python 3.9+ | 主程序 | 是 |
| 富途 OpenD | 行情 / 持仓 / 下单 | 是 |
| PushPlus Token | 推送与 ClawBot 收发 | 是 |
| Node.js | 仅用于 AI 桥接 `clawbot_bridge.mjs` | 可选（见下文） |
| `CURSOR_API_KEY` | ClawBot 复杂自然语言 → Cursor AI | 可选 |

---

## Git 里有什么 / 没有什么

**已在 GitHub（clone 即可用）：**

- 全部 Python 源码、`config/*.json`
- `clawbot_bridge.mjs`、`package.json`、`requirements.txt`、`TradePilot.spec`
- `secrets.env.example`（模板，无真实密钥）

**不会上传（`.gitignore`），需在新环境自行准备：**

| 路径 | 说明 |
|------|------|
| `logs/secrets.env` 或 `dist/logs/secrets.env` | Token、API Key、渠道配置 |
| `node_modules/` | `npm install` 生成 |
| `dist/TradePilot.exe`、`build/` | `PyInstaller` 本地打包 |
| `logs/*.log`、`*_state.json`、`positions.json` 等 | 运行日志与状态（首次运行会自动创建） |

---

## 新环境部署

### 1. 拉取代码

```powershell
git clone https://github.com/JackChen188/TradePilot.git
cd TradePilot
```

### 2. 安装依赖

```powershell
pip install -r requirements.txt
pip install pyinstaller   # 仅打包 exe 时需要
npm install               # ClawBot AI 桥接需要 @cursor/sdk
```

### 3. 配置密钥

根据运行方式选择目录（二选一）：

| 运行方式 | `secrets.env` 位置 |
|----------|-------------------|
| `python main.py`（源码） | `TradePilot\logs\secrets.env` |
| `dist\TradePilot.exe` | `TradePilot\dist\logs\secrets.env` |

```powershell
mkdir logs -Force
copy secrets.env.example logs\secrets.env
notepad logs\secrets.env
```

至少填写：

```env
PUSHPLUS_TOKEN=你的token
CURSOR_API_KEY=你的key          # 不用 AI 对话可不填
TP_PUSHPLUS_CHANNEL=clawbot
TP_CLAWBOT_REPLY_CHANNEL=clawbot
```

富途相关变量（如 `FUTU_HOST`、`FUTU_PORT` 等）可写在同一文件或系统环境变量，详见 `config.py` 中的 `TP_*` / `FUTU_*`。

### 4. 外部服务

1. **富途 OpenD**：本机登录并开启 API（默认 `127.0.0.1:11111`）。
2. **PushPlus**：在 [个人中心](https://www.pushplus.plus/) 配置；使用 ClawBot 需绑定 [微信 ClawBot 渠道](https://www.pushplus.plus/doc/channel/clawbot.html)，状态为「已激活」。
3. **ClawBot 激活规则**：绑定后需先在 ClawBot 里发一条消息；约每推送 10 条或每 24 小时需在 ClawBot 里再主动发一次，否则 API 可能成功但微信收不到。

### 5. 运行

**源码：**

```powershell
python main.py
```

**打包 exe：**

```powershell
py -m PyInstaller TradePilot.spec --noconfirm
mkdir dist\logs -Force
copy logs\secrets.env dist\logs\secrets.env
.\dist\TradePilot.exe
```

> exe 运行时，日志与配置在 `dist\logs\`；`clawbot_bridge.mjs` 与 `node_modules` 仍在**项目根目录**（未打进 exe），目录结构应为：
>
> ```
> TradePilot\
>   clawbot_bridge.mjs
>   node_modules\
>   dist\
>     TradePilot.exe
>     logs\
>       secrets.env
> ```

### 6. 快速自检

```powershell
# 在项目根执行；若用 exe，可先设运行目录
$env:TP_RUNTIME_DIR = "C:\path\to\TradePilot\dist"
python -c "import sys; sys.argv[0]=r'C:\path\to\TradePilot\dist\TradePilot.exe'; from secrets_loader import load_secrets_env; load_secrets_env(); import os; t=os.getenv('PUSHPLUS_TOKEN',''); print('OK' if t else 'MISSING TOKEN', t[:8]+'...' if t else '')"
```

---

## 从旧电脑迁移

只需手动拷贝**未进 Git** 且需要保留的内容（U 盘 / 加密网盘，勿上传到 GitHub）：

| 文件 | 作用 |
|------|------|
| `dist\logs\secrets.env`（或 `logs\secrets.env`） | 全部 Token / Key |
| 可选 `positions.json`、`pending_orders.json` | 本地持仓、待确认订单 |
| 可选 `*_state.json` | 舆情去重、周报等（不拷会重新扫描） |

**不必拷贝：** `dist\TradePilot.exe`（新机器重新打包）、`build\`、`node_modules\`（`npm install` 重装）。

---

## Node.js 与 `TP_NODE_PATH` 的作用

**Node 不是用来跑 TradePilot 主程序的**（主程序是 Python / `TradePilot.exe`）。它只用于启动 **AI 桥接子进程** `clawbot_bridge.mjs`。

```
ClawBot 用户发消息
  → TradePilot（Python）长轮询收到
  → 本地能处理的（帮助 / 新闻 / 持仓 / 价格等）→ 直接 PushPlus 回复
  → 其余复杂问题 → 写入 dist/logs/clawbot_ai_queue.json
  → node.exe 运行 clawbot_bridge.mjs
  → 调用 Cursor Cloud Agent（@cursor/sdk）
  → AI 回复推回 ClawBot
```

TradePilot 启动桥接时等价于：

```text
node.exe  <项目根>\clawbot_bridge.mjs
```

常见情况：

- 双击 `TradePilot.exe` 时，系统 **PATH 里可能没有 `node`**
- 未装 Node → 日志：`未找到 node，无法启动 Cursor AI 桥接`
- 有 `CURSOR_API_KEY` 但无 Node → 仅本地指令可用，**无 AI 自然语言回复**

**`TP_NODE_PATH`**：在 `secrets.env` 中指定 `node.exe` 完整路径，不依赖 PATH。例如使用 Cursor 自带 Node：

```env
TP_NODE_PATH=C:\Users\你的用户名\AppData\Local\Programs\cursor\resources\app\resources\helpers\node.exe
```

| 使用场景 | 是否需要 Node |
|----------|----------------|
| 仅「新闻 / 持仓 / 帮助」等本地指令 | 否 |
| ClawBot 自然语言问 AI、复杂分析 | 是，且需 `CURSOR_API_KEY` |
| 未配置 `CURSOR_API_KEY` | 桥接不启动，Node 也用不上 |

安装 Node 后还需在项目根执行一次：`npm install`（安装 `@cursor/sdk`）。

---

## 推送渠道配置

| 变量 | 典型值 | 说明 |
|------|--------|------|
| `TP_PUSHPLUS_CHANNEL` | `clawbot` | 舆情、周报、系统通知默认渠道 |
| `TP_CLAWBOT_REPLY_CHANNEL` | `clawbot` | ClawBot 对话回复（应与用户发消息的入口一致） |
| `TP_CLAWBOT_REPLY_FALLBACK` | `wechat`（可选） | ClawBot 推送失败时再试公众号 |

双通道（会收到两份）示例：

```env
TP_PUSHPLUS_CHANNEL=clawbot,wechat
TP_CLAWBOT_REPLY_CHANNEL=clawbot,wechat
```

- **clawbot**：微信 ClawBot 对话，支持双向；需保持激活。
- **wechat**：微信公众号，单向通知较稳，但 ClawBot 里发的消息不会自动回到公众号会话。

---

## 运行后自动生成的文件

首次启动会在 `logs/` 或 `dist/logs/` 下创建，无需从 Git 拉取：

- `tradepilot.log` — 运行日志
- `clawbot_ai_queue.json` — AI 待处理队列
- `clawbot_bridge.lock` — 桥接单实例锁
- `pushplus_confirm_state.json`、`news_monitor_state.json` 等 — 去重与调度状态
- `positions.json`、`pending_orders.json` — 本地持仓与待确认单

---

## 常见问题

**ClawBot 发消息没回复**

1. 任务管理器里只保留 **一个** `TradePilot.exe`。
2. 确认 `secrets.env` 在正确目录（exe 用 `dist\logs\`）。
3. 看 `tradepilot.log` 是否有 `[ClawBot] 收到消息` / `已回复 channel=clawbot`。
4. PushPlus 后台 ClawBot 是否为「已激活」，并在 ClawBot 里先发一条激活。

**消息都进公众号、ClawBot 没有**

检查 `TP_PUSHPLUS_CHANNEL` 是否误设为 `wechat`；ClawBot 对话回复应使用 `TP_CLAWBOT_REPLY_CHANNEL=clawbot`。

**AI 报错 `SQLITE_CONSTRAINT`**

确保使用最新 `clawbot_bridge.mjs`（每条消息 `Agent.prompt`，非多轮 `resume`），且只有一个桥接进程（见 `clawbot_bridge.lock`）。

**多个 `clawbot_bridge` 进程**

结束多余 node 后删 lock 再重启 TradePilot：

```powershell
Get-CimInstance Win32_Process -Filter "name='node.exe'" |
  Where-Object { $_.CommandLine -match 'clawbot_bridge' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Remove-Item dist\logs\clawbot_bridge.lock -ErrorAction SilentlyContinue
```

---

## 配置说明

敏感项放在 `logs/secrets.env`（或 `dist/logs/secrets.env`），参考 `secrets.env.example`。

策略与交易参数见 `config.py` 及 `TP_*` 环境变量；标的列表见 `config/*.json`。

---

## 仓库

https://github.com/JackChen188/TradePilot
