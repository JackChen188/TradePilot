from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class GuiConfirmResult:
    action: str  # CONFIRM / CANCEL
    entered_code: str


def confirm_order_dialog(
    *,
    order_id: str,
    symbol: str,
    side: str,
    qty: int,
    price: float,
    confirm_code: str,
) -> GuiConfirmResult:
    """
    Blocking tkinter dialog for manual order approval.
    Returns user action and entered confirm code.
    """
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        # If GUI is unavailable, fail-safe cancel.
        return GuiConfirmResult(action="CANCEL", entered_code="")

    result = {"action": "CANCEL", "entered_code": ""}
    est_amount = float(qty) * float(price)

    root = tk.Tk()
    root.title("TradePilot 订单确认")
    root.geometry("460x360")
    root.resizable(False, False)

    frame = tk.Frame(root, padx=14, pady=12)
    frame.pack(fill="both", expand=True)

    title = tk.Label(frame, text="待确认订单", font=("Arial", 14, "bold"))
    title.pack(anchor="w", pady=(0, 8))

    info_lines = [
        f"订单ID: {order_id}",
        f"股票代码: {symbol}",
        f"方向: {side}",
        f"数量: {qty}",
        f"价格: {price:.4f}",
        f"预计金额: ${est_amount:.2f}",
    ]
    for line in info_lines:
        tk.Label(frame, text=line, anchor="w").pack(fill="x")

    tk.Label(
        frame,
        text="请输入确认码（二次校验）:",
        anchor="w",
    ).pack(fill="x", pady=(12, 4))

    code_var = tk.StringVar()
    code_entry = tk.Entry(frame, textvariable=code_var)
    code_entry.pack(fill="x")
    code_entry.focus_set()

    def _cancel():
        result["action"] = "CANCEL"
        result["entered_code"] = code_var.get().strip().upper()
        root.destroy()

    def _confirm():
        entered = code_var.get().strip().upper()
        if not entered:
            messagebox.showerror("确认失败", "请先输入确认码。")
            return
        if entered != str(confirm_code).strip().upper():
            messagebox.showerror("确认失败", "确认码不匹配。")
            return
        result["action"] = "CONFIRM"
        result["entered_code"] = entered
        root.destroy()

    def _open_watchlist():
        try:
            watchlist_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist_gui.py")
            subprocess.Popen([sys.executable, watchlist_script])
        except Exception as e:
            messagebox.showerror("打开失败", f"无法打开股票池窗口: {e}")

    btn_frame = tk.Frame(frame)
    btn_frame.pack(fill="x", pady=(16, 0))
    tk.Button(btn_frame, text="确认下单", command=_confirm, bg="#2e7d32", fg="white").pack(
        side="left", padx=(0, 8)
    )
    tk.Button(btn_frame, text="取消", command=_cancel, bg="#b71c1c", fg="white").pack(side="left")
    tk.Button(btn_frame, text="股票池管理", command=_open_watchlist).pack(side="left", padx=(8, 0))

    root.protocol("WM_DELETE_WINDOW", _cancel)
    root.mainloop()

    return GuiConfirmResult(action=result["action"], entered_code=result["entered_code"])

