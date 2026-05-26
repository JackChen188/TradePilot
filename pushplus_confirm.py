from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any

_log = logging.getLogger(__name__)

import pandas as pd
import requests

from config import TRADE
from data_provider import get_last_price
from holdings import apply_buy, apply_sell, load_holdings, save_holdings
from notifier import PushPlusNotifier
from pending_orders import PendingOrder, expire_pending_orders, find_pending_match, load_pending_orders, save_pending_orders
from report import append_trade_record
from risk_manager import append_trade_log
from secrets_loader import get_cursor_api_key, load_secrets_env, resolve_project_root, resolve_runtime_dir

# 支持查询的股票代码正则（US.CRWV 或 CRWV 形式）
_TICKER_RE = re.compile(r"\b(?:US\.)?([A-Z]{1,6})\b")
_STRATEGY_SIGNAL_RE = re.compile(r"signal\s*=\s*(BUY|SELL|HOLD)", re.I)

# ClawBot → Cursor Agent 消息队列（与 exe 工作目录 logs/ 对齐）
def _ai_queue_path() -> str:
    return os.path.join(resolve_runtime_dir(), "logs", "clawbot_ai_queue.json")


def _clawbot_reply_channel() -> str:
    """ClawBot 用户发消息的回复渠道（与系统舆情推送的 wechat 分开）。"""
    return (os.getenv("TP_CLAWBOT_REPLY_CHANNEL") or "clawbot").strip().lower()


def _clawbot_send(notifier: PushPlusNotifier, *, title: str, content: str) -> tuple[bool, str]:
    ch = _clawbot_reply_channel()
    ok, resp = notifier.send(title=title, content=content, channel=ch)
    if ok:
        _log.info("[ClawBot] 已回复 channel=%s title=%s", ch, title[:40])
    else:
        _log.warning("[ClawBot] 回复推送失败 channel=%s: %s", ch, str(resp)[:200])
        fallback = (os.getenv("TP_CLAWBOT_REPLY_FALLBACK") or "").strip().lower()
        if fallback and fallback != ch:
            ok2, resp2 = notifier.send(title=title, content=content, channel=fallback)
            _log.info("[ClawBot] 备用渠道 %s ok=%s", fallback, ok2)
            if ok2:
                return ok2, resp2
    return ok, resp


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if h:
            k.CloseHandle(h)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _find_node_executable() -> str | None:
    """
    Resolve node.exe for clawbot_bridge.
    PyInstaller/GUI 启动时 PATH 往往不含 node；可设 TP_NODE_PATH 覆盖。
    """
    override = (os.getenv("TP_NODE_PATH") or "").strip().strip('"')
    if override:
        if os.path.isfile(override):
            return os.path.abspath(override)
        _log.warning("[ClawBot] TP_NODE_PATH 不存在: %s", override)

    found = shutil.which("node")
    if found and os.path.isfile(found):
        return os.path.abspath(found)

    for part in (os.environ.get("PATH") or "").split(os.pathsep):
        part = part.strip()
        if not part:
            continue
        p = os.path.join(part, "node.exe")
        if os.path.isfile(p):
            return os.path.abspath(p)

    localappdata = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")

    static_candidates: list[str] = []
    nvm_symlink = (os.getenv("NVM_SYMLINK") or "").strip()
    if nvm_symlink:
        static_candidates.append(os.path.join(nvm_symlink, "node.exe"))

    static_candidates.extend(
        [
            os.path.join(program_files, "nodejs", "node.exe"),
            os.path.join(program_files_x86, "nodejs", "node.exe"),
            os.path.join(
                localappdata,
                "Programs",
                "cursor",
                "resources",
                "app",
                "resources",
                "helpers",
                "node.exe",
            ),
        ]
    )

    if sys.platform == "win32" and appdata:
        nvm_home = (os.getenv("NVM_HOME") or os.path.join(appdata, "nvm")).strip()
        if os.path.isdir(nvm_home):
            try:
                vers = sorted(
                    (d for d in os.listdir(nvm_home) if d.lower().startswith("v")),
                    reverse=True,
                )
                for v in vers:
                    p = os.path.join(nvm_home, v, "node.exe")
                    if os.path.isfile(p):
                        static_candidates.append(p)
                        break
            except OSError:
                pass

    for p in static_candidates:
        if p and os.path.isfile(p):
            return os.path.abspath(p)

    return None


def _futu_display_pl_pct(row: Any) -> float:
    """
    富途持仓接口的 pl_ratio（文档称已为百分数）在部分情况下会返回量级错误（如 -1267 表示 -12.67%）。
    用 pl_val 与摊薄成本基数交叉校验，明显不一致时以金额口径为准。
    """
    try:
        raw = float(row.get("pl_ratio") or 0)
    except Exception:
        raw = 0.0
    try:
        pl_val = float(row.get("pl_val") or row.get("unrealized_pl") or 0)
    except Exception:
        pl_val = 0.0
    try:
        qty = float(row.get("qty") or 0)
        cost_price = float(row.get("cost_price") or row.get("pl_cost_price") or 0)
    except Exception:
        qty = 0.0
        cost_price = 0.0

    cost_basis = abs(cost_price * qty) if qty and cost_price else 0.0
    if cost_basis <= 1e-9:
        return raw

    computed = (pl_val / cost_basis) * 100.0

    valid = row.get("pl_ratio_valid")
    if valid is False:
        return computed

    if abs(raw) > 500:
        return computed
    if abs(computed) > 1e-6 and abs(raw / computed) > 8.0 and abs(raw) > 40.0:
        return computed
    return raw


def _build_broker_context(broker) -> dict:
    ctx: dict = {}
    if broker is None:
        return ctx
    try:
        ctx["cash"] = float(broker.get_available_cash())
    except Exception:
        pass
    try:
        ctx["total_assets"] = float(broker.get_total_assets())
    except Exception:
        pass
    try:
        df = broker.get_positions()
        positions: list[dict] = []
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                positions.append(
                    {
                        "symbol": str(row.get("code", "")),
                        "qty": int(float(row.get("qty", 0))),
                        "cost": float(row.get("cost_price", 0) or 0),
                        "pl_ratio": float(_futu_display_pl_pct(row)),
                    }
                )
        ctx["positions"] = positions
    except Exception:
        pass
    return ctx


def _enqueue_ai_message(
    text: str,
    msg_id: str,
    *,
    broker=None,
) -> None:
    """
    将 ClawBot 用户原话写入队列，由 clawbot_bridge.mjs 转发给 Cursor Agent 理解并回复。
    不再依赖关键词硬匹配。
    """
    queue_path = _ai_queue_path()
    os.makedirs(os.path.dirname(queue_path), exist_ok=True)
    try:
        try:
            with open(queue_path, "r", encoding="utf-8") as f:
                queue: list = json.load(f) or []
        except Exception:
            queue = []
        # 同一 msg_id 可重复入队（例如用户再次发送「pong」），用时间戳区分
        entry_id = f"{msg_id}_{int(time.time() * 1000)}"
        queue.append(
            {
                "msg_id": entry_id,
                "text": text,
                "ts": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
                "context": _build_broker_context(broker),
            }
        )
        with open(queue_path, "w", encoding="utf-8") as f:
            json.dump(queue[-500:], f, ensure_ascii=False, indent=2)
        _log.info("[ClawBot] 已入队 Cursor AI: %s (id=%s)", text[:80], entry_id)
    except Exception as e:
        _log.error("[ClawBot] enqueue_ai_message error: %s", e)


def _default_news_tickers(broker=None) -> list[str]:
    """无股票代码时，用持仓/监控列表作为「新闻」默认标的。"""
    out: list[str] = []
    seen: set[str] = set()
    if broker is not None:
        try:
            df = broker.get_positions()
            if df is not None and not df.empty:
                for _, row in df.iterrows():
                    code = str(row.get("code", "")).replace("US.", "").upper()
                    if code and code not in seen:
                        seen.add(code)
                        out.append(code)
        except Exception:
            pass
    if not out:
        try:
            from config import TRADE

            for sym in TRADE.symbols:
                code = str(sym).replace("US.", "").upper()
                if code and code not in seen:
                    seen.add(code)
                    out.append(code)
        except Exception:
            pass
    return out[:5]


def _is_tradepilot_signal_line(text: str) -> bool:
    """TradePilot 控制台/推送里的策略扫描一行（勿当作「查新闻」）。"""
    t = (text or "").strip()
    if not t:
        return False
    tl = t.lower()
    if _STRATEGY_SIGNAL_RE.search(t) and ("score=" in tl or "rank=" in tl):
        return True
    if "buy signal:" in tl or "sell signal:" in tl:
        return True
    return False


def _parse_signal_line_ticker(text: str) -> str:
    m = re.search(r"\bUS\.([A-Z]{1,6})\b", text, re.I)
    if m:
        return m.group(1).upper()
    m2 = re.match(r"^\s*([A-Z]{1,6})\s+signal\s*=", text.strip(), re.I)
    if m2:
        return m2.group(1).upper()
    return "未知"


def _plain_score_clause(clause: str) -> str:
    c = clause.strip()
    if not c:
        return ""
    known = {
        "SPY>MA200(+20)": "大盘：SPY 在 200 日均线上方（整体偏多环境，+20 分）",
        "price>MA200(+20)": "个股：价格在 200 日均线上方（长期趋势偏多，+20 分；本条若未出现表示未满足）",
        "MA20>MA60(+20)": "个股：20 日均线在 60 日均线上方（短期趋势向上，+20 分）",
        "ret63 rank top(+20)": "动量：近 63 日涨幅在当次扫描池中排名靠前（+20 分）",
        "ret5d in[-6%,+4%](+10)": "节奏：近 5 日涨跌幅在 -6%～+4%（避免短期过热/暴跌，+10 分）",
        "RSI bypass(+10)": "RSI 规则放宽（白名单标的，+10 分）",
    }
    if c in known:
        return known[c]
    m = re.match(r"RSI14 in\[45,(\d+)\]\(\+10\)", c, re.I)
    if m:
        hi = m.group(1)
        return f"RSI：14 日 RSI 在 45～{hi} 之间（有动能但未过热，+10 分）"
    m2 = re.match(r"RSI14 strong-exception<=(\d+)\(\+10\)", c, re.I)
    if m2:
        return f"RSI：强势例外，RSI≤{m2.group(1)} 仍可加分（+10 分）"
    return c.replace("(+", "（+").replace(")", " 分）")


def _explain_tradepilot_signal_line(text: str) -> str:
    """把 main.py 打印的 signal= 一行翻成中文说明。"""
    ticker = _parse_signal_line_ticker(text)
    sig_m = _STRATEGY_SIGNAL_RE.search(text)
    score_m = re.search(r"score\s*=\s*(\d+)", text, re.I)
    rank_m = re.search(r"rank\s*=\s*([\d.]+)\s*%", text, re.I)

    lines: list[str] = [
        f"【US.{ticker} 策略信号解读】",
        "",
        "这是 TradePilot 扫描结果摘要，不是已经下单；BUY 仅表示「符合买入评分」，是否买入还需 PushPlus 确认。",
        "",
    ]
    if sig_m:
        sig = sig_m.group(1).upper()
        if sig == "BUY":
            lines.append("signal=BUY：达到买入评分线（默认 score≥60），可考虑纳入候选。")
        elif sig == "SELL":
            lines.append("signal=SELL：技术面转弱或触发卖出规则。")
        else:
            lines.append(f"signal={sig}：暂不买入也不卖出。")
    if score_m:
        sc = int(score_m.group(1))
        lines.append(f"score={sc}：技术面总分（约 100 分制），≥60 才给 BUY；你这条共 {sc} 分。")
    if rank_m:
        rp = float(rank_m.group(1))
        lines.append(
            f"rank={rp:.1f}%：近 63 日涨幅在本次扫描股票池中的相对位置，"
            f"约强于池中 {rp:.0f}% 的标的（越高动量越强）。"
        )

    reason_raw = text.split("|", 1)[-1].strip() if "|" in text else ""
    reason_raw = re.sub(r"^(BUY|SELL)\s+signal:\s*", "", reason_raw, flags=re.I).strip()
    if reason_raw:
        lines.extend(["", "【得分明细】"])
        for part in reason_raw.split("&"):
            plain = _plain_score_clause(part.strip())
            if plain:
                lines.append(f"  · {plain}")

    if score_m and int(score_m.group(1)) < 90:
        lines.extend(
            [
                "",
                "【提示】",
                "  · 若明细里没有 price>MA200(+20)，说明股价可能仍低于自身 200 日线；",
                "  · 满足该项时总分往往会更高（常见 90 分左右）。",
            ]
        )
    lines.extend(
        [
            "",
            "其他自然语言问题（非粘贴 signal= 日志）会交给 Cursor AI 回答。",
        ]
    )
    return "\n".join(lines)


def _handle_clawbot_query(text: str, *, notifier: PushPlusNotifier, broker=None) -> bool:
    """
    尝试将用户消息识别为查询请求并回复。
    返回 True 表示已处理（无需再当作确认码处理）。
    支持：
      - 余额/账户查询：含「余额」「现金」「账户」「资产」「总资产」
      - 新闻/消息查询：含「新闻」「消息」「怎么样」「如何」「最新」
      - 价格查询：含「价格」「多少」「现在」「涨」「跌」
      - 持仓查询：含「持仓」「我的」「仓位」
      - 帮助：「帮助」「help」
    """
    t = text.strip()
    tl = t.lower()

    # 含 YES BUY/SELL 确认命令时交给下单逻辑，勿当作新闻/行情查询
    if _extract_confirm_cmd_line(t):
        return False

    # TradePilot 策略扫描一行 / 用户粘贴 signal= 日志求解释（勿误判为查新闻）
    if _is_tradepilot_signal_line(t) or (
        any(k in t for k in ("什么意思", "解释一下", "帮我解释", "啥意思", "怎么理解"))
        and _STRATEGY_SIGNAL_RE.search(t)
    ):
        _clawbot_send(
            notifier,
            title=f"📊 {_parse_signal_line_ticker(t)} 信号解读",
            content=_explain_tradepilot_signal_line(t),
        )
        return True

    # 帮助
    if any(kw in tl for kw in ("帮助", "help", "怎么用", "命令", "指令")):
        _clawbot_send(
            notifier,
            title="📖 TradePilot 指令帮助",
            content=(
                "支持以下查询（直接发送给 ClawBot）：\n\n"
                "【账户查询】\n"
                "  我的余额\n"
                "  账户资产\n\n"
                "【持仓查询】\n"
                "  我的持仓\n\n"
                "【新闻查询】\n"
                "  CRWV最新消息\n"
                "  有关VOO的新闻吗\n\n"
                "【策略信号解读】\n"
                "  粘贴控制台一行：US.ZS signal=BUY score=70 ...\n\n"
                "【AI 对话】\n"
                "  其他自然语言（非上述格式）会交给 Cursor AI\n\n"
                "【价格查询】\n"
                "  CRWV现在多少\n"
                "  TQQQ价格\n\n"
                "【确认下单】\n"
                "  YES BUY US.VOO 1 A3F9\n"
                "  或：确认下单 YES BUY US.VOO 1 A3F9\n"
                "  或只发确认码：A3F9"
            ),
        )
        return True

    # 余额/账户查询
    if any(kw in tl for kw in ("余额", "现金", "账户", "资产", "总资产", "balance", "cash")):
        lines = []
        if broker is not None:
            try:
                cash = float(broker.get_available_cash())
                lines.append(f"可用现金：${cash:.2f}")
            except Exception:
                lines.append("可用现金：获取失败")
            try:
                assets = float(broker.get_total_assets())
                lines.append(f"总资产：${assets:.2f}")
            except Exception:
                lines.append("总资产：获取失败")
        else:
            lines.append("账户数据暂不可用（broker未连接）")
        # 同时附上持仓简况（优先读 broker 实时数据）
        lines.append("")
        if broker is not None:
            try:
                df = broker.get_positions()
                if df is not None and not df.empty:
                    lines.append("当前持仓：")
                    for _, row in df.iterrows():
                        code = str(row.get("code", "")).replace("US.", "")
                        qty = int(float(row.get("qty", 0)))
                        cost = float(row.get("cost_price", row.get("pl_cost_price", 0)) or 0)
                        pl_pct = _futu_display_pl_pct(row)
                        lines.append(f"  {code}: {qty}股 成本${cost:.2f} 盈亏{pl_pct:+.1f}%")
                else:
                    lines.append("当前无持仓")
            except Exception:
                lines.append("（持仓数据获取失败）")
        else:
            try:
                holdings = load_holdings()
                active = [h for h in holdings if int(h.qty) > 0]
                if active:
                    lines.append("当前持仓（本地记录）：")
                    for h in active:
                        sym = str(h.symbol).replace("US.", "")
                        lines.append(f"  {sym}: {h.qty}股 @ ${float(h.buy_price):.2f}")
                else:
                    lines.append("当前无持仓")
            except Exception:
                pass
        _clawbot_send(notifier, title="💰 账户余额", content="\n".join(lines))
        return True

    # 持仓查询
    if any(kw in tl for kw in ("持仓", "我的仓", "仓位", "portfolio", "holding")):
        lines = []
        # 优先读 broker 实时持仓（Futu 真实数据）
        if broker is not None:
            try:
                df = broker.get_positions()
                if df is not None and not df.empty:
                    lines.append("【Futu 实时持仓】")
                    for _, row in df.iterrows():
                        code = str(row.get("code", "")).replace("US.", "")
                        qty = int(float(row.get("qty", 0)))
                        cost = float(row.get("cost_price", row.get("pl_cost_price", 0)) or 0)
                        market_val = float(row.get("market_val", 0) or 0)
                        pl = float(row.get("pl_val", row.get("unrealized_pl", 0)) or 0)
                        pl_pct = _futu_display_pl_pct(row)
                        lines.append(
                            f"  {code}: {qty}股 成本${cost:.2f} "
                            f"市值${market_val:.2f} 盈亏${pl:+.2f}({pl_pct:+.1f}%)"
                        )
                    if not lines[1:]:  # 只有标题没有数据
                        lines = ["Futu 账户当前无持仓"]
                else:
                    lines.append("Futu 账户当前无持仓")
            except Exception as e:
                lines.append(f"Futu 实时持仓获取失败: {e}")
                # 降级读本地文件
                try:
                    holdings = load_holdings()
                    active = [h for h in holdings if int(h.qty) > 0]
                    if active:
                        lines.append("【本地记录（可能不准）】")
                        for h in active:
                            sym = str(h.symbol).replace("US.", "")
                            lines.append(f"  {sym}: {h.qty}股 @ 成本${float(h.buy_price):.2f}")
                except Exception:
                    pass
        else:
            # 无 broker 时读本地文件
            try:
                holdings = load_holdings()
                active = [h for h in holdings if int(h.qty) > 0]
                if active:
                    lines.append("【本地记录（可能不准确，请以 Futu 为准）】")
                    for h in active:
                        sym = str(h.symbol).replace("US.", "")
                        lines.append(f"  {sym}: {h.qty}股 @ 成本${float(h.buy_price):.2f}")
                else:
                    lines.append("本地记录无持仓（broker 未连接，无法读 Futu 实时数据）")
            except Exception as e:
                lines.append(f"查询持仓失败: {e}")
        _clawbot_send(notifier, title="📊 持仓查询", content="\n".join(lines))
        return True

    # 提取股票代码
    tickers = [m.group(1).upper() for m in _TICKER_RE.finditer(t.upper())]
    # 过滤明显不是股票的词
    skip = {"YES", "BUY", "SELL", "THE", "AND", "FOR", "ARE", "YOU", "CAN", "有", "的", "吗"}
    tickers = [tk for tk in tickers if tk not in skip and len(tk) >= 2]

    is_news_query = any(kw in tl for kw in ("新闻", "消息", "最新", "怎么样", "如何", "有没有", "动态", "公告", "news"))
    is_price_query = any(kw in tl for kw in ("价格", "多少", "现在", "涨", "跌", "price", "quote", "行情"))

    # 只要提到股票代码（比如“CRWV”），即使没有显式关键词，也默认给出「价格 + 最新新闻摘要」。
    # 但 TradePilot 策略日志行（含 signal=/score=）不要误判为查新闻。
    if tickers and (not is_news_query and not is_price_query) and not _is_tradepilot_signal_line(t):
        is_news_query = True
        is_price_query = True

    if not tickers and is_news_query:
        tickers = _default_news_tickers(broker)
        if not tickers:
            _clawbot_send(
                notifier,
                title="📰 新闻查询",
                content="未识别股票代码，且暂无持仓。\n请发：CRWV 新闻  或  CRWV最新消息",
            )
            return True

    if not tickers:
        return False  # 不是查询，交给确认码处理器

    ticker = tickers[0]  # 取第一个识别到的股票
    code_full = ticker if ticker.startswith("US.") else f"US.{ticker}"

    # 价格/新闻查询（可组合输出）
    out_lines: list[str] = []
    if is_price_query:
        try:
            from news_verdict_tracker import _fetch_yahoo_price
            price = _fetch_yahoo_price(ticker)
            if price:
                out_lines.append(f"当前价格：${price:.2f}（Yahoo，可能有延迟）")
            else:
                out_lines.append("当前价格：获取失败")
        except Exception as e:
            out_lines.append(f"当前价格：查询异常 {type(e).__name__}: {e}")

    if is_news_query:
        try:
            from market_context import classify_news_impact, fetch_news_summary, format_news_title_lines
            news = fetch_news_summary(code_full, max_items=5)
            if news is None or not news.titles:
                out_lines.append("最新新闻：暂无（RSS 未返回内容）")
            else:
                verdict, bull, bear = classify_news_impact(news)
                bull_s = "、".join(bull[:4]) if bull else "无"
                bear_s = "、".join(bear[:4]) if bear else "无"
                out_lines.append(f"舆情判断：{verdict}")
                out_lines.append(f"利好线索：{bull_s}")
                out_lines.append(f"利空线索：{bear_s}")
                out_lines.append("")
                out_lines.append("最新头条：")
                out_lines.extend(format_news_title_lines(news.titles, max_items=5))
        except Exception as e:
            out_lines.append(f"最新新闻：查询异常 {type(e).__name__}: {e}")

    if not out_lines:
        return False
    _clawbot_send(notifier, title=f"📰 {ticker} 新闻/行情", content="\n".join(out_lines))
    return True

    return False


PUSHPLUS_ACCESS_KEY_ENV = "PUSHPLUS_ACCESS_KEY"  # optional manual override
PUSHPLUS_SECRET_KEY_ENV = "PUSHPLUS_SECRET_KEY"  # used to fetch accessKey
PUSHPLUS_TOKEN_ENV = "PUSHPLUS_TOKEN"  # user token (NOT message token)
PUSHPLUS_GET_ACCESS_KEY = "https://www.pushplus.plus/api/common/openApi/getAccessKey"
PUSHPLUS_OPENAPI_LIST = "https://www.pushplus.plus/api/open/message/list"
PUSHPLUS_SHORT_MESSAGE = "https://www.pushplus.plus/shortMessage/{shortCode}"
PUSHPLUS_CLAWBOT_GETMSG = "https://www.pushplus.plus/api/open/clawBot/getMsg"

STATE_PATH = os.path.join(resolve_runtime_dir(), "logs", "pushplus_confirm_state.json")

# Require title prefix to reduce accidental triggers.
TITLE_PREFIX = "TP_CONFIRM"

CMD_RE = re.compile(r"^YES\s+(BUY|SELL)\s+([A-Z]{2}\.[A-Z0-9]+)\s+(\d+)\s+([A-Z0-9]{4,16})$")
_CONFIRM_EMBEDDED_RE = re.compile(
    r"YES\s+(BUY|SELL)\s+([A-Z]{2}\.[A-Z0-9]+)\s+(\d+)\s+([A-Z0-9]{4,16})",
    re.I,
)
CODE_ONLY_RE = re.compile(r"^[A-Z0-9]{4,16}$")


def _extract_confirm_cmd_line(text: str) -> str:
    """
    从 ClawBot/PushPlus 消息中提取下单确认命令。
    支持整行 YES BUY ...，以及「确认下单 YES BUY ...」等带前缀的写法。
    """
    for line in [x.strip() for x in (text or "").splitlines() if x.strip()]:
        u = line.strip().upper()
        u = re.sub(r"^确认下单\s*", "", u)
        u = re.sub(r"^CONFIRM\s+", "", u)
        if CMD_RE.match(u):
            return u
        m = _CONFIRM_EMBEDDED_RE.search(u)
        if m:
            return m.group(0).upper()
        if CODE_ONLY_RE.match(u):
            return u
    return ""

_PUSHPLUS_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_recent_message(update_time: str, *, max_age_seconds: int) -> bool:
    """
    PushPlus OpenAPI returns updateTime like "YYYY-MM-DD HH:MM:SS" (no timezone).
    We treat it as local time and ignore old confirmations to avoid processing historical test messages.
    """
    try:
        if not update_time:
            return False
        dt_local = datetime.strptime(str(update_time).strip()[:19], _PUSHPLUS_TIME_FMT)
        age = time.time() - dt_local.timestamp()
        return age >= 0 and age <= float(max_age_seconds)
    except Exception:
        return False


def _extract_order_id(data: Any) -> str | None:
    if isinstance(data, pd.DataFrame) and not data.empty and "order_id" in data.columns:
        try:
            return str(data.iloc[0]["order_id"])
        except Exception:
            return None
    return None


def _access_key() -> str:
    # 1) Manual override (fixed accessKey, 2h expiry unknown to us)
    ak = (os.getenv(PUSHPLUS_ACCESS_KEY_ENV) or "").strip()
    if ak:
        return ak

    # 2) Cached accessKey (recommended)
    st = _load_state()
    cached = str(st.get("accessKey") or "").strip()
    exp_ts = float(st.get("accessKeyExpiresAtEpoch") or 0.0)
    now = time.time()
    # Keep a small buffer to avoid edge expiry.
    if cached and exp_ts and now < (exp_ts - 120):
        return cached

    # 3) Fetch new accessKey using token + secretKey
    token = (os.getenv(PUSHPLUS_TOKEN_ENV) or "").strip()
    secret = (os.getenv(PUSHPLUS_SECRET_KEY_ENV) or "").strip()
    if not token or not secret:
        return ""
    try:
        resp = requests.post(PUSHPLUS_GET_ACCESS_KEY, json={"token": token, "secretKey": secret}, timeout=20)
        if not resp.ok:
            # Surface error to help user configure 开发设置/安全IP.
            print(
                f"[pushplus_confirm] getAccessKey failed: http={resp.status_code} body={resp.text[:300]}",
                flush=True,
            )
            return ""
        payload = resp.json() or {}
        if isinstance(payload, dict):
            biz_code = payload.get("code")
            if biz_code not in (200, "200"):
                print(f"[pushplus_confirm] getAccessKey rejected: {str(payload)[:300]}", flush=True)
                return ""
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            print(f"[pushplus_confirm] getAccessKey bad response: {str(payload)[:300]}", flush=True)
            return ""
        new_ak = str(data.get("accessKey") or "").strip()
        expires_in = float(data.get("expiresIn") or 0.0)
        if not new_ak:
            print(f"[pushplus_confirm] getAccessKey missing accessKey: {str(payload)[:300]}", flush=True)
            return ""
        st["accessKey"] = new_ak
        st["accessKeyExpiresAtEpoch"] = time.time() + max(expires_in, 0.0)
        _save_state(st)
        return new_ak
    except Exception as e:
        print(f"[pushplus_confirm] getAccessKey exception: {type(e).__name__}: {e}", flush=True)
        return ""


def _load_state() -> dict:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    if not os.path.exists(STATE_PATH):
        return {"processed_shortcodes": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {"processed_shortcodes": []}
    except Exception:
        return {"processed_shortcodes": []}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        s = str(data or "").strip()
        if s:
            self._chunks.append(s)

    def text(self) -> str:
        # Join with newlines to preserve user formatting.
        return "\n".join(self._chunks).strip()


def _fetch_clawbot_messages(*, long_poll: bool = False) -> list[dict]:
    """
    从 ClawBot 拉取用户发给微信机器人的消息。
    long_poll=True 时使用10秒长轮询（仅在独立线程中使用）。
    返回格式统一为 [{"text": str, "msg_id": str, "create_time": str}, ...]
    仅返回 type=1（文字）消息。
    """
    if os.getenv("TP_CLAWBOT_ENABLED", "1").strip() in ("0", "false", "False", "no"):
        return []
    ak = _access_key()
    if not ak:
        return []
    headers = {"access-key": ak, "Content-Type": "application/json"}
    params = {"longPoll": "true"} if long_poll else {}
    try:
        resp = requests.get(
            PUSHPLUS_CLAWBOT_GETMSG,
            headers=headers,
            params=params,
            timeout=15.0 if long_poll else 8.0,
        )
        if not resp.ok:
            return []
        data = resp.json() or {}
        if isinstance(data, dict) and data.get("code") not in (200, "200"):
            return []
        items = []
        raw_list = []
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, list):
                raw_list = inner           # data 直接是列表
            elif isinstance(inner, dict):
                raw_list = inner.get("list") or []
            else:
                raw_list = []
        # API 返回格式：data 直接是列表，每条消息有 type/text 字段
        # 也可能是 data.list 嵌套格式，兼容两种
        if isinstance(raw_list, dict):
            raw_list = raw_list.get("list") or []
        for item in (raw_list if isinstance(raw_list, list) else []):
            if not isinstance(item, dict):
                continue
            if int(item.get("type", 0)) != 1:  # 只要文字消息
                continue
            text_val = str(item.get("text") or item.get("content") or "").strip()
            if not text_val:
                continue
            # msgId 可能不存在：必须带上时间戳，避免「pong」等短句永远被判为重复
            import hashlib as _hl
            msg_id = str(item.get("msgId") or item.get("id") or "").strip()
            create_time = str(item.get("createTime") or item.get("sendTime") or "").strip()
            if not msg_id:
                seed = f"{item.get('type')}:{text_val}:{create_time or time.time_ns()}"
                msg_id = _hl.md5(seed.encode("utf-8")).hexdigest()
            items.append({
                "text": text_val,
                "msg_id": msg_id,
                "create_time": str(item.get("createTime") or item.get("sendTime") or ""),
            })
        return items
    except Exception as e:
        print(f"[clawbot] getMsg exception: {type(e).__name__}: {e}", flush=True)
        return []


def _fetch_message_list(*, current: int = 1, page_size: int = 20) -> list[dict]:
    ak = _access_key()
    if not ak:
        return []
    headers = {"access-key": ak, "Content-Type": "application/json"}
    payload = {"current": int(current), "pageSize": min(int(page_size), 50)}
    resp = requests.post(PUSHPLUS_OPENAPI_LIST, headers=headers, json=payload, timeout=20)
    if not resp.ok:
        return []
    data = resp.json() or {}
    items: list[Any] = []
    if isinstance(data, dict):
        data_obj = data.get("data") or {}
        if isinstance(data_obj, dict):
            raw_list = data_obj.get("list") or []
            if isinstance(raw_list, list):
                items = raw_list
    return [x for x in items if isinstance(x, dict)]


def _fetch_message_text(short_code: str) -> str:
    url = PUSHPLUS_SHORT_MESSAGE.format(shortCode=str(short_code).strip())
    resp = requests.get(url, timeout=20)
    if not resp.ok:
        return ""
    html = resp.text or ""
    p = _TextExtractor()
    try:
        p.feed(html)
    except Exception:
        return ""
    return p.text()


def _execute_pending(*, po: PendingOrder, broker, notifier: PushPlusNotifier, source: str) -> None:
    side = po.side.upper()
    current_price = get_last_price(broker.quote_ctx, po.symbol)
    est_amount = float(po.qty) * float(po.limit_price)
    if est_amount > float(TRADE.max_order_usd):
        raise RuntimeError(f"Blocked: single order amount ${est_amount:.2f} > max ${TRADE.max_order_usd:.2f}")
    if side == "BUY":
        available_cash = broker.get_available_cash()
        if available_cash < est_amount:
            raise RuntimeError(f"Blocked: insufficient cash for BUY. cash={available_cash:.2f} need={est_amount:.2f}")
    else:
        held_qty = broker.get_position_qty(po.symbol)
        if held_qty < int(po.qty):
            raise RuntimeError(f"Blocked: insufficient position for SELL. held={held_qty} need={po.qty}")

    broker.ensure_us_stock_only(po.symbol)
    broker.ensure_regular_session(po.symbol)

    ok, data = broker.place_limit_order(code=po.symbol, side=side, qty=int(po.qty), price=float(po.limit_price))
    order_id = _extract_order_id(data) or ""
    status_msg = ""
    if order_id:
        try:
            qdf = broker.query_order(order_id)
            if qdf is not None and not qdf.empty:
                status_msg = str(qdf.iloc[0].get("order_status", ""))
        except Exception:
            status_msg = ""

    po.broker_order_id = order_id
    po.broker_status = status_msg
    po.updated_at = _now_iso()
    po.confirm_code = ""  # invalidate immediately
    po.status = "EXECUTED" if ok else "FAILED"
    po.message = str(data)

    append_trade_log(
        {
            "code": po.symbol,
            "selected": 1,
            "action": side,
            "current_price": current_price,
            "limit_price": po.limit_price,
            "qty": po.qty,
            "est_amount": est_amount,
            "order_ok": 1 if ok else 0,
            "order_id": order_id,
            "order_status": status_msg,
            "message": f"pushplus source={source}; {data}",
        }
    )

    if ok:
        holdings = load_holdings()
        if side == "BUY":
            apply_buy(holdings, symbol=po.symbol, qty=int(po.qty), price=float(po.limit_price))
        else:
            apply_sell(holdings, symbol=po.symbol, qty=int(po.qty))
        save_holdings(holdings)
        append_trade_record({"ts_utc": _now_iso(), "symbol": po.symbol, "side": side, "qty": int(po.qty), "price": float(po.limit_price), "order_id": order_id})

    result_content = (
        f"source={source}\nsymbol={po.symbol}\nqty={po.qty}\nprice={po.limit_price}\n"
        f"order_ok={ok}\norder_id={order_id}\nstatus={status_msg}\nmessage={data}"
    )
    if "clawbot" in str(source).lower():
        _clawbot_send(notifier, title=f"TradePilot 下单结果 {side} {po.symbol}", content=result_content)
    else:
        notifier.send(title=f"TradePilot 下单结果 {side} {po.symbol}", content=result_content)


def process_pushplus_confirmations(*, broker, notifier: PushPlusNotifier) -> None:
    """
    Pull confirmations from PushPlus OpenAPI (no WeChat desktop listener needed).

    How user confirms:
    - Send a PushPlus message to yourself with title starting with 'TP_CONFIRM'
    - Content must be: YES BUY symbol qty confirm_code  (or SELL)
    """
    ak = _access_key()
    if not ak:
        return

    state = _load_state()
    processed = set(state.get("processed_shortcodes", []))

    pending_orders = load_pending_orders()
    changed = False
    if expire_pending_orders(pending_orders):
        changed = True

    # Fetch recent messages (first page is usually enough).
    items = _fetch_message_list(current=1, page_size=20)
    if not items:
        return

    # Process newest first by updateTime if present.
    def _key(x: dict) -> str:
        return str(x.get("updateTime") or "")

    # Only process very recent TP_CONFIRM to avoid old test messages triggering failures on startup.
    max_age = int(getattr(TRADE, "confirm_code_expire_seconds", 300)) + 300

    for it in sorted(items, key=_key, reverse=True):
        title = str(it.get("title") or "").strip()
        short_code = str(it.get("shortCode") or "").strip()
        update_time = str(it.get("updateTime") or "").strip()
        if not short_code or short_code in processed:
            continue
        if not title.startswith(TITLE_PREFIX):
            continue
        if not _is_recent_message(update_time, max_age_seconds=max_age):
            # Mark as processed so it won't keep reappearing on every boot.
            processed.add(short_code)
            continue

        text = _fetch_message_text(short_code)
        cmd_line = _extract_confirm_cmd_line(text or "")

        if not cmd_line:
            processed.add(short_code)
            continue

        m = CMD_RE.match(cmd_line)
        po: PendingOrder | None = None
        if m:
            side, symbol, qty_s, code = m.group(1), m.group(2), m.group(3), m.group(4)
            po = find_pending_match(pending_orders, side=side, symbol=symbol, qty=int(qty_s), confirm_code=code)
        else:
            # Only code: match a single pending order by confirm_code
            code = cmd_line.strip().upper()
            matches = [
                x
                for x in pending_orders
                if str(x.status).upper() == "PENDING" and str(x.confirm_code).upper() == code
            ]
            if len(matches) == 1:
                po = matches[0]
            else:
                po = None

        if po is None:
            # Only notify for recent messages; old ones are ignored above.
            notifier.send(title="TradePilot 确认失败", content=f"source=pushplus\nreason=pending_not_found\ncommand={cmd_line}")
            processed.add(short_code)
            continue

        # expiry check
        try:
            exp = datetime.strptime(po.expire_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if exp < _utc_now():
                po.status = "EXPIRED"
                changed = True
                notifier.send(title="TradePilot 确认失败", content=f"source=pushplus\nreason=order_expired\nsymbol={po.symbol}")
                processed.add(short_code)
                continue
        except Exception:
            pass

        try:
            _execute_pending(po=po, broker=broker, notifier=notifier, source=f"shortCode={short_code}")
            changed = True
        except Exception as e:
            po.status = "FAILED"
            po.updated_at = _now_iso()
            po.message = f"pushplus_execute_failed: {type(e).__name__}: {e}"
            changed = True
            notifier.send(
                title="TradePilot 确认执行失败",
                content=f"source=pushplus\nsymbol={po.symbol}\nside={po.side}\nreason={type(e).__name__}: {e}",
            )

        processed.add(short_code)

    if changed:
        save_pending_orders(pending_orders)
    state["processed_shortcodes"] = list(processed)[-2000:]

    # ── ClawBot：主循环做一次兜底轮询（监听线程是主路径，这里防漏）───────────
    # 监听线程已通过长轮询实时处理大多数消息；这里只处理线程遗漏的消息。
    clawbot_msgs = _fetch_clawbot_messages(long_poll=False)
    for cm in (clawbot_msgs or []):
        mid = cm.get("msg_id", "")
        text = cm.get("text", "").strip()
        if not mid or not text:
            continue
        if not _mark_clawbot_seen(mid):
            continue  # 监听线程已处理

        try:
            _process_one_clawbot_msg(text, mid, broker=broker, notifier=notifier)
        except Exception as e:
            _log.error("[ClawBot fallback] 处理消息异常: %s", e)

    # 把内存去重集合同步回状态文件
    with _clawbot_seen_lock:
        ids_snapshot = list(_clawbot_seen_ids)
    state["clawbot_processed_ids"] = ids_snapshot[-2000:]
    _save_state(state)


# ── 内存中已处理 ClawBot 消息 ID 集合（供监听线程与主循环共享去重）──────────
_clawbot_seen_lock = threading.Lock()
_clawbot_seen_ids: set[str] = set()


def _mark_clawbot_seen(mid: str) -> bool:
    """返回 True 表示这条消息是首次见到（可处理）；False 表示已处理过（跳过）。"""
    with _clawbot_seen_lock:
        if mid in _clawbot_seen_ids:
            return False
        _clawbot_seen_ids.add(mid)
        # 防止无限增长
        if len(_clawbot_seen_ids) > 5000:
            oldest = list(_clawbot_seen_ids)[:500]
            for x in oldest:
                _clawbot_seen_ids.discard(x)
        return True


def _process_one_clawbot_msg(text: str, mid: str, *, broker, notifier: PushPlusNotifier) -> None:
    """处理单条 ClawBot 消息（查询或下单确认）。线程安全。"""
    text = text.strip()
    cmd_line = _extract_confirm_cmd_line(text)

    if not cmd_line:
        # 先走本地规则（新闻/持仓/帮助等），秒回 ClawBot；其余再交给 Cursor AI
        if _handle_clawbot_query(text, notifier=notifier, broker=broker):
            return
        api_key = get_cursor_api_key()
        if api_key:
            _enqueue_ai_message(text, mid, broker=broker)
        else:
            _clawbot_send(
                notifier,
                title="⚠️ Cursor AI 未启用",
                    content=(
                        "请设置环境变量 CURSOR_API_KEY 后重启 TradePilot，\n"
                        "即可用自然语言远程对话（无需记指令关键词）。\n\n"
                        "获取地址：https://cursor.com/dashboard/cloud-agents"
                    ),
            )
        return

    # 下单确认
    pending_orders = load_pending_orders()
    m = CMD_RE.match(cmd_line)
    po: PendingOrder | None = None
    if m:
        side, symbol, qty_s, code = m.group(1), m.group(2), m.group(3), m.group(4)
        po = find_pending_match(pending_orders, side=side, symbol=symbol, qty=int(qty_s), confirm_code=code)
    else:
        matches = [
            x for x in pending_orders
            if str(x.status).upper() == "PENDING" and str(x.confirm_code).upper() == cmd_line
        ]
        po = matches[0] if len(matches) == 1 else None

    if po is None:
        _clawbot_send(
            notifier,
            title="TradePilot 确认失败",
            content=f"source=clawbot\nreason=pending_not_found\ncommand={cmd_line}",
        )
        return

    try:
        exp = datetime.strptime(po.expire_time, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        if exp < _utc_now():
            po.status = "EXPIRED"
            save_pending_orders(pending_orders)
            _clawbot_send(
                notifier,
                title="TradePilot 确认失败",
                content=f"source=clawbot\nreason=order_expired\nsymbol={po.symbol}",
            )
            return
    except Exception:
        pass

    try:
        _execute_pending(po=po, broker=broker, notifier=notifier, source=f"clawbot msg_id={mid}")
        save_pending_orders(pending_orders)
    except Exception as e:
        po.status = "FAILED"
        po.updated_at = _now_iso()
        po.message = f"clawbot_execute_failed: {type(e).__name__}: {e}"
        save_pending_orders(pending_orders)
        _clawbot_send(
            notifier,
            title="TradePilot 确认执行失败",
            content=f"source=clawbot\nsymbol={po.symbol}\nside={po.side}\nreason={type(e).__name__}: {e}",
        )


# ── ClawBot 长轮询监听线程 ────────────────────────────────────────────────────

_clawbot_listener_started = False
_clawbot_listener_lock = threading.Lock()


def start_clawbot_listener(*, broker, notifier: PushPlusNotifier) -> None:
    """
    启动 ClawBot 长轮询监听线程（全局只启动一次）。
    使用 PushPlus longPoll API：服务器挂起连接直到有新消息，收到立刻处理，
    响应时间 <5秒，无需等待主循环的 5 分钟轮询窗口。
    """
    global _clawbot_listener_started
    with _clawbot_listener_lock:
        if _clawbot_listener_started:
            return
        _clawbot_listener_started = True

    # 把状态文件里已处理的 ID 预加载到内存，防止重启后重复处理旧消息
    try:
        st = _load_state()
        with _clawbot_seen_lock:
            _clawbot_seen_ids.update(st.get("clawbot_processed_ids", []))
    except Exception:
        pass

    def _listener_loop():
        _log.info("[ClawBot] 长轮询监听线程已启动")
        consecutive_errors = 0
        while True:
            try:
                # long_poll=True：服务器最多挂起 ~30s 等待新消息
                msgs = _fetch_clawbot_messages(long_poll=True)
                consecutive_errors = 0

                for cm in (msgs or []):
                    mid = cm.get("msg_id", "")
                    text = cm.get("text", "").strip()
                    if not mid or not text:
                        continue
                    if not _mark_clawbot_seen(mid):
                        _log.info("[ClawBot] 跳过已处理消息 id=%s text=%s", mid, text[:60])
                        continue

                    _log.info("[ClawBot] 收到消息: %s (id=%s)", text[:80], mid)
                    try:
                        _process_one_clawbot_msg(text, mid, broker=broker, notifier=notifier)
                    except Exception as e:
                        _log.error("[ClawBot] 处理消息异常: %s: %s", type(e).__name__, e)

                    # 持久化已处理 ID（定期写入状态文件）
                    try:
                        st = _load_state()
                        with _clawbot_seen_lock:
                            ids_snapshot = list(_clawbot_seen_ids)
                        st["clawbot_processed_ids"] = ids_snapshot[-2000:]
                        _save_state(st)
                    except Exception:
                        pass

            except Exception as e:
                consecutive_errors += 1
                wait = min(30 * consecutive_errors, 300)
                _log.warning("[ClawBot] 长轮询异常（第%d次），%ds 后重试: %s", consecutive_errors, wait, e)
                time.sleep(wait)

    t = threading.Thread(target=_listener_loop, name="ClawBotListener", daemon=True)
    t.start()


# ── Cursor AI 桥接进程（Node + @cursor/sdk）──────────────────────────────────

_bridge_proc: subprocess.Popen | None = None
_bridge_lock = threading.Lock()


def start_clawbot_bridge_process() -> None:
    """
    启动 clawbot_bridge.mjs：从队列读取用户原话 → Cursor Agent → 微信回复。
    需要 CURSOR_API_KEY 和 node 可用。
    """
    global _bridge_proc
    api_key = get_cursor_api_key()
    if not api_key:
        _log.warning("[ClawBot] 未设置 CURSOR_API_KEY，跳过 Cursor AI 桥接")
        return

    project_root = resolve_project_root()
    bridge_script = os.path.join(project_root, "clawbot_bridge.mjs")
    if not os.path.isfile(bridge_script):
        _log.warning("[ClawBot] 找不到 clawbot_bridge.mjs: %s", bridge_script)
        return

    node = _find_node_executable()
    if not node:
        _log.warning(
            "[ClawBot] 未找到 node，无法启动 Cursor AI 桥接。"
            "请安装 Node.js 或设置环境变量 TP_NODE_PATH=node.exe完整路径"
        )
        return

    with _bridge_lock:
        if _bridge_proc is not None and _bridge_proc.poll() is None:
            return

        lock_path = os.path.join(os.path.dirname(_ai_queue_path()), "clawbot_bridge.lock")
        if os.path.isfile(lock_path):
            try:
                with open(lock_path, encoding="utf-8") as f:
                    pid = int(f.read().strip())
                if _pid_alive(pid):
                    _log.info(
                        "[ClawBot] 桥接已在运行 (pid=%s)，跳过重复启动",
                        pid,
                    )
                    return
            except (ValueError, OSError):
                pass

        env = os.environ.copy()
        load_secrets_env()
        env.update({k: v for k, v in os.environ.items() if k.startswith(("CURSOR_", "PUSHPLUS_", "TP_"))})
        if not env.get("TP_CLAWBOT_REPLY_CHANNEL"):
            env["TP_CLAWBOT_REPLY_CHANNEL"] = "clawbot"
        env["TP_AI_QUEUE_PATH"] = _ai_queue_path()
        env["TP_PROJECT_ROOT"] = project_root

        kwargs: dict = {
            "cwd": project_root,
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        try:
            _bridge_proc = subprocess.Popen([node, bridge_script], **kwargs)
            _log.info(
                "[ClawBot] Cursor AI 桥接已启动 pid=%s node=%s queue=%s",
                _bridge_proc.pid,
                node,
                env["TP_AI_QUEUE_PATH"],
            )
        except Exception as e:
            _log.error("[ClawBot] 启动 Cursor AI 桥接失败: %s", e)

