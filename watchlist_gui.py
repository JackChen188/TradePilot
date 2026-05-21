from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime

import tkinter as tk
from tkinter import messagebox


WATCHLIST_PATH = os.path.join("config", "watchlist.json")

DEFAULT_CURRENT = [
    "US.VOO",
    "US.AAPL",
    "US.MSFT",
    "US.NVDA",
    "US.AMZN",
    "US.META",
    "US.HUT",
    "US.STRF",
]

RECOMMENDED = {
    "稳健ETF": ["US.VOO", "US.SPY", "US.QQQ"],
    "科技大盘": ["US.AAPL", "US.MSFT", "US.NVDA", "US.AMZN", "US.META", "US.GOOGL"],
    "高波动成长": ["US.TSLA", "US.HUT", "US.COIN", "US.PLTR"],
    "防御型": ["US.JNJ", "US.PG", "US.KO", "US.WMT"],
}


def _ensure_watchlist_file(path: str = WATCHLIST_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"symbols": DEFAULT_CURRENT, "updated_at": ""}, f, ensure_ascii=False, indent=2)


def load_watchlist(path: str = WATCHLIST_PATH) -> list[str]:
    _ensure_watchlist_file(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        syms = [str(x).strip().upper() for x in (data.get("symbols") or []) if str(x).strip()]
        return syms if syms else list(DEFAULT_CURRENT)
    except Exception:
        return list(DEFAULT_CURRENT)


def save_watchlist(symbols: list[str], path: str = WATCHLIST_PATH) -> None:
    _ensure_watchlist_file(path)
    payload = {
        "symbols": sorted(set(s.strip().upper() for s in symbols if s.strip())),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


@dataclass
class AppState:
    current_symbols: set[str]


class WatchlistApp:
    def __init__(self):
        self.state = AppState(current_symbols=set(load_watchlist()))
        self.root = tk.Tk()
        self.root.title("TradePilot 股票池管理")
        self.root.geometry("980x680")

        self.current_vars: dict[str, tk.BooleanVar] = {}
        self.reco_vars: dict[str, tk.BooleanVar] = {}

        self._build_ui()
        self._render_current()
        self._render_recommended()

    def _build_ui(self) -> None:
        top = tk.Frame(self.root, padx=12, pady=10)
        top.pack(fill="both", expand=True)

        row1 = tk.Frame(top)
        row1.pack(fill="x")

        left = tk.LabelFrame(row1, text="A. 当前股票池", padx=8, pady=8)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        right = tk.LabelFrame(row1, text="B. 推荐股票池", padx=8, pady=8)
        right.pack(side="left", fill="both", expand=True)

        self.current_frame = tk.Frame(left)
        self.current_frame.pack(fill="both", expand=True)

        list_frame = tk.Frame(left)
        list_frame.pack(fill="x", pady=(8, 0))
        tk.Label(list_frame, text="当前股票池列表（可删除）").pack(anchor="w")
        self.current_listbox = tk.Listbox(list_frame, height=8, exportselection=False)
        self.current_listbox.pack(fill="x", pady=(4, 4))
        tk.Button(list_frame, text="删除选中股票", command=self._delete_selected).pack(anchor="w")

        add_frame = tk.Frame(left)
        add_frame.pack(fill="x", pady=(8, 0))
        tk.Label(add_frame, text="手动添加股票代码（例如 US.AMD）").pack(anchor="w")
        self.add_entry = tk.Entry(add_frame)
        self.add_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        tk.Button(add_frame, text="添加", command=self._add_symbol).pack(side="left")

        self.reco_container = tk.Frame(right)
        self.reco_container.pack(fill="both", expand=True)

        bottom = tk.Frame(top)
        bottom.pack(fill="x", pady=(10, 0))
        tk.Button(bottom, text="保存股票池", bg="#2e7d32", fg="white", command=self._save).pack(side="left")
        tk.Button(bottom, text="关闭", command=self.root.destroy).pack(side="left", padx=(8, 0))

    def _refresh_current_listbox(self) -> None:
        self.current_listbox.delete(0, tk.END)
        for s in sorted(self.state.current_symbols):
            self.current_listbox.insert(tk.END, s)

    def _render_current(self) -> None:
        for w in self.current_frame.winfo_children():
            w.destroy()
        self.current_vars.clear()

        for s in sorted(self.state.current_symbols):
            var = tk.BooleanVar(value=True)
            self.current_vars[s] = var
            tk.Checkbutton(self.current_frame, text=s, variable=var).pack(anchor="w")
        self._refresh_current_listbox()

    def _render_recommended(self) -> None:
        for w in self.reco_container.winfo_children():
            w.destroy()
        self.reco_vars.clear()

        for cat, syms in RECOMMENDED.items():
            box = tk.LabelFrame(self.reco_container, text=cat, padx=6, pady=6)
            box.pack(fill="x", pady=(0, 8))
            for s in syms:
                checked = s in self.state.current_symbols
                var = tk.BooleanVar(value=checked)
                self.reco_vars[s] = var
                tk.Checkbutton(box, text=s, variable=var).pack(anchor="w")

    def _add_symbol(self) -> None:
        s = self.add_entry.get().strip().upper()
        if not s or "." not in s or not s.startswith("US."):
            messagebox.showwarning("输入有误", "请输入合法美股代码，例如 US.AMD")
            return
        self.state.current_symbols.add(s)
        self.add_entry.delete(0, tk.END)
        self._render_current()
        self._render_recommended()

    def _delete_selected(self) -> None:
        idx = self.current_listbox.curselection()
        if not idx:
            return
        symbol = self.current_listbox.get(idx[0]).strip().upper()
        if symbol in self.state.current_symbols:
            self.state.current_symbols.remove(symbol)
        self._render_current()
        self._render_recommended()

    def _save(self) -> None:
        final_symbols: set[str] = set()

        # Current stock pool checkboxes
        for s, v in self.current_vars.items():
            if v.get():
                final_symbols.add(s)

        # Recommended pool checkboxes
        for s, v in self.reco_vars.items():
            if v.get():
                final_symbols.add(s)

        if not final_symbols:
            messagebox.showwarning("保存失败", "股票池不能为空")
            return

        save_watchlist(sorted(final_symbols))
        self.state.current_symbols = set(final_symbols)
        self._render_current()
        self._render_recommended()
        messagebox.showinfo("保存成功", "股票池已更新")

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    WatchlistApp().run()

