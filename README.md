# TradePilot

美股量化辅助交易系统：富途 OpenD 行情/持仓、策略信号、PushPlus/ClawBot 微信推送与人工确认下单。

## 功能概览

- 多标的 Alpha 扫描与买卖信号（不自动下单，需 PushPlus 确认）
- 舆情 RSS 监控推送（英文标题附中文翻译）
- ClawBot 微信查询持仓/新闻/价格
- Cursor Agent 桥接（`clawbot_bridge.mjs`）理解自然语言指令

## 环境要求

- Python 3.9+
- 富途 OpenD
- Node.js（或 Cursor 自带 node；也可设置 `TP_NODE_PATH`）
- PushPlus Token、可选 `CURSOR_API_KEY`

## 安装与运行（源码）

```powershell
pip install -r requirements.txt
npm install
# 配置 secrets.env（勿提交到 Git）
copy secrets.env.example secrets.env   # 若存在示例文件则参考填写
python main.py
```

## 打包 exe

```powershell
py -m PyInstaller TradePilot.spec --noconfirm
# 从 dist 目录运行
.\dist\TradePilot.exe
```

## 配置说明

敏感项放在 `logs/secrets.env` 或环境变量，例如 `PUSHPLUS_TOKEN`、`CURSOR_API_KEY`、`FUTU_*`。

详见各模块 `config.py` 与 `TP_*` 环境变量。
