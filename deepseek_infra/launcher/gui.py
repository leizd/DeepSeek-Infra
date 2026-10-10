"""Tkinter GUI launcher for DeepSeek Infra.

Lets the user enter API keys, choose host/port, and start/stop the local HTTP
server without ever typing a command. Credentials are persisted to an
encrypted local file (see :mod:`deepseek_infra.launcher.credentials`).
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox
from typing import Any

import customtkinter as ctk

from deepseek_infra.core.config import settings
from deepseek_infra.core.utils import local_ip, url_with_token
from deepseek_infra.launcher import credentials as credentials_store
from deepseek_infra.launcher.credentials import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    LAN_HOST,
    LauncherCredentials,
)
from deepseek_infra.launcher.runtime import LauncherRuntime, launcher_url_from_log

logger = logging.getLogger("deepseek_infra.launcher.gui")
APP_TITLE = f"DeepSeek Infra {settings.app_version} 启动器"
LOG_BUFFER_LINES = 400
POLL_INTERVAL_MS = 80

THEME = {
    "bg_main": "#0B0F19",
    "bg_card": "#161B2E",
    "bg_input": "#0F172A",
    "fg_title": "#F8FAFC",
    "fg_text": "#E2E8F0",
    "fg_muted": "#94A3B8",
    "accent_primary": "#2563EB",
    "accent_hover": "#1D4ED8",
    "accent_text": "#FFFFFF",
    "success": "#10B981",
    "danger": "#EF4444",
    "danger_hover": "#B91C1C",
    "warning": "#F59E0B",
    "border_card": "#1E293B",
    "border_input": "#334155",
    "indigo": "#4F46E5",
    "indigo_hover": "#4338CA",
    "slate": "#475569",
    "slate_hover": "#334155",
    "red_dark": "#991B1B",
    "red_dark_hover": "#7F1D1D",
    "url_link": "#60A5FA",
    "log_bg": "#05070F",
    "badge_bg": "#1F2A44",
}


def _font(size: int = 12, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family="Segoe UI", size=size, weight=weight)


def _mono_font(size: int = 12) -> ctk.CTkFont:
    return ctk.CTkFont(family="Consolas", size=size)


class LauncherWindow:
    def __init__(self, root: ctk.CTk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("860x740")
        self.root.minsize(780, 680)
        self.root.configure(fg_color=THEME["bg_main"])

        self._event_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self.runtime = LauncherRuntime(
            on_log=lambda line: self._event_queue.put(("log", line)),
            on_status=lambda status: self._event_queue.put(("status", status)),
        )

        self.deepseek_var = tk.StringVar()
        self.tavily_var = tk.StringVar()
        self.host_var = tk.StringVar(value=DEFAULT_HOST)
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.allow_lan_var = tk.BooleanVar(value=False)
        self.ocr_var = tk.BooleanVar(value=False)
        self.auth_disabled_var = tk.BooleanVar(value=False)
        self.show_deepseek_var = tk.BooleanVar(value=False)
        self.show_tavily_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="● 未启动")
        self.computer_url_var = tk.StringVar(value="—")
        self.phone_url_var = tk.StringVar(value="—")

        self._build_ui()
        self._load_persisted()
        self._sync_host_with_lan()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(POLL_INTERVAL_MS, self._drain_events)

    # ------- layout builders -------

    def _create_card(
        self,
        parent: Any,
        title: str | None = None,
        padding: int = 18,
    ) -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
        card = ctk.CTkFrame(
            parent,
            fg_color=THEME["bg_card"],
            border_color=THEME["border_card"],
            border_width=1,
            corner_radius=14,
        )

        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill=tk.BOTH, expand=True, padx=padding, pady=padding)

        if title:
            title_lbl = ctk.CTkLabel(
                inner,
                text=title,
                text_color=THEME["fg_title"],
                font=_font(13, "bold"),
                anchor="w",
            )
            title_lbl.pack(anchor=tk.W, pady=(0, 12), fill=tk.X)
            content = ctk.CTkFrame(inner, fg_color="transparent")
            content.pack(fill=tk.BOTH, expand=True)
            return card, content

        return card, inner

    def _build_ui(self) -> None:
        container = ctk.CTkFrame(self.root, fg_color="transparent")
        container.pack(fill=tk.BOTH, expand=True, padx=24, pady=22)

        header_frame = ctk.CTkFrame(container, fg_color="transparent")
        header_frame.pack(fill=tk.X, anchor=tk.W, pady=(0, 18))

        header = ctk.CTkLabel(
            header_frame,
            text=f"🚀 {APP_TITLE}",
            text_color=THEME["fg_title"],
            font=_font(18, "bold"),
            anchor="w",
        )
        header.pack(anchor=tk.W, fill=tk.X)

        intro = ctk.CTkLabel(
            header_frame,
            text="填写 API Key 并启动服务。配置采用本机指纹与算法强加密保存，安全可靠。",
            text_color=THEME["fg_muted"],
            font=_font(11),
            anchor="w",
            justify="left",
        )
        intro.pack(anchor=tk.W, pady=(6, 0), fill=tk.X)

        # 1. API 凭证密钥
        creds_card, creds_inner = self._create_card(container, "🔑 API 凭证密钥")
        creds_card.pack(fill=tk.X, pady=(0, 14))
        creds_inner.columnconfigure(1, weight=1)

        self._build_credential_row(
            creds_inner, 0, "DeepSeek API Key", self.deepseek_var, self.show_deepseek_var
        )
        self._build_credential_row(
            creds_inner, 1, "Tavily API Key（可选）", self.tavily_var, self.show_tavily_var
        )

        # 2. 服务运行选项
        options_card, options_inner = self._create_card(container, "⚙️ 服务运行选项")
        options_card.pack(fill=tk.X, pady=(0, 14))
        options_inner.columnconfigure(1, weight=1)

        self.lan_check = ctk.CTkCheckBox(
            options_inner,
            text="允许手机 / 同局域网设备访问服务 (HOST=0.0.0.0)",
            variable=self.allow_lan_var,
            command=self._sync_host_with_lan,
            text_color=THEME["fg_text"],
            font=_font(11),
            fg_color=THEME["accent_primary"],
            hover_color=THEME["accent_hover"],
            checkbox_width=20,
            checkbox_height=20,
            corner_radius=5,
            border_width=2,
            border_color=THEME["border_input"],
        )
        self.lan_check.grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 10))

        port_lbl = ctk.CTkLabel(
            options_inner,
            text="监听端口",
            text_color=THEME["fg_text"],
            font=_font(11),
            anchor="w",
        )
        port_lbl.grid(row=1, column=0, sticky=tk.W, pady=4)

        self.port_entry = ctk.CTkEntry(
            options_inner,
            textvariable=self.port_var,
            width=120,
            fg_color=THEME["bg_input"],
            border_color=THEME["border_input"],
            text_color=THEME["fg_text"],
            font=_font(11),
            corner_radius=8,
            border_width=1,
            height=34,
        )
        self.port_entry.grid(row=1, column=1, sticky=tk.W, padx=(12, 0), pady=4)

        self.ocr_check = ctk.CTkCheckBox(
            options_inner,
            text="开启 OCR 图像光学字符识别支持 (OCR_ENABLED)",
            variable=self.ocr_var,
            text_color=THEME["fg_text"],
            font=_font(11),
            fg_color=THEME["accent_primary"],
            hover_color=THEME["accent_hover"],
            checkbox_width=20,
            checkbox_height=20,
            corner_radius=5,
            border_width=2,
            border_color=THEME["border_input"],
        )
        self.ocr_check.grid(row=2, column=0, columnspan=2, sticky=tk.W, pady=(10, 4))

        self.auth_check = ctk.CTkCheckBox(
            options_inner,
            text="临时关闭本地 Token 鉴权（安全警告：仅本机调试，不推荐局域网开启）",
            variable=self.auth_disabled_var,
            text_color=THEME["warning"],
            font=_font(11),
            fg_color=THEME["warning"],
            hover_color="#D97706",
            checkbox_width=20,
            checkbox_height=20,
            corner_radius=5,
            border_width=2,
            border_color=THEME["border_input"],
        )
        self.auth_check.grid(row=3, column=0, columnspan=4, sticky=tk.W, pady=(4, 0))

        # 3. 动作按钮
        actions_frame = ctk.CTkFrame(container, fg_color="transparent")
        actions_frame.pack(fill=tk.X, pady=(0, 14))

        self.start_button = ctk.CTkButton(
            actions_frame,
            text="⚡ 启动服务",
            command=self._on_start,
            fg_color=THEME["accent_primary"],
            hover_color=THEME["accent_hover"],
            text_color=THEME["accent_text"],
            font=_font(12, "bold"),
            height=38,
            corner_radius=10,
            width=130,
        )
        self.start_button.pack(side=tk.LEFT)

        self.stop_button = ctk.CTkButton(
            actions_frame,
            text="🛑 停止服务",
            command=self._on_stop,
            fg_color=THEME["danger"],
            hover_color=THEME["danger_hover"],
            text_color=THEME["accent_text"],
            font=_font(12, "bold"),
            height=38,
            corner_radius=10,
            width=130,
        )
        self.stop_button.pack(side=tk.LEFT, padx=(10, 0))
        self.stop_button.configure(state="disabled")

        self.open_button = ctk.CTkButton(
            actions_frame,
            text="🌐 打开浏览器",
            command=self._on_open_browser,
            fg_color=THEME["indigo"],
            hover_color=THEME["indigo_hover"],
            text_color=THEME["accent_text"],
            font=_font(12, "bold"),
            height=38,
            corner_radius=10,
            width=130,
        )
        self.open_button.pack(side=tk.LEFT, padx=(10, 0))
        self.open_button.configure(state="disabled")

        self.save_button = ctk.CTkButton(
            actions_frame,
            text="💾 保存配置",
            command=self._on_save,
            fg_color=THEME["slate"],
            hover_color=THEME["slate_hover"],
            text_color=THEME["accent_text"],
            font=_font(12, "bold"),
            height=38,
            corner_radius=10,
            width=130,
        )
        self.save_button.pack(side=tk.LEFT, padx=(10, 0))

        self.clear_button = ctk.CTkButton(
            actions_frame,
            text="🗑️ 清空密钥",
            command=self._on_clear,
            fg_color=THEME["red_dark"],
            hover_color=THEME["red_dark_hover"],
            text_color=THEME["accent_text"],
            font=_font(12, "bold"),
            height=38,
            corner_radius=10,
            width=130,
        )
        self.clear_button.pack(side=tk.RIGHT)

        # 4. 运行状态监控
        status_card, status_inner = self._create_card(container, "📊 运行状态监控")
        status_card.pack(fill=tk.X, pady=(0, 14))
        status_inner.columnconfigure(1, weight=1)

        status_lbl = ctk.CTkLabel(
            status_inner,
            text="服务状态",
            text_color=THEME["fg_text"],
            font=_font(11),
            anchor="w",
            width=80,
        )
        status_lbl.grid(row=0, column=0, sticky=tk.W)

        self.status_badge = ctk.CTkLabel(
            status_inner,
            textvariable=self.status_var,
            text_color=THEME["fg_muted"],
            fg_color=THEME["badge_bg"],
            font=_font(11, "bold"),
            corner_radius=14,
            width=140,
            height=28,
        )
        self.status_badge.grid(row=0, column=1, sticky=tk.W, padx=(12, 0))

        comp_lbl = ctk.CTkLabel(
            status_inner,
            text="电脑访问",
            text_color=THEME["fg_text"],
            font=_font(11),
            anchor="w",
            width=80,
        )
        comp_lbl.grid(row=1, column=0, sticky=tk.W, pady=(12, 0))

        self.computer_url = ctk.CTkLabel(
            status_inner,
            textvariable=self.computer_url_var,
            text_color=THEME["url_link"],
            font=_mono_font(12),
            anchor="w",
        )
        self.computer_url.grid(row=1, column=1, sticky=tk.W, pady=(12, 0), padx=(12, 0))
        self.computer_url.configure(cursor="hand2")
        self.computer_url.bind("<Button-1>", lambda _event: self._copy_url(self.computer_url_var))

        phone_lbl = ctk.CTkLabel(
            status_inner,
            text="手机访问",
            text_color=THEME["fg_text"],
            font=_font(11),
            anchor="w",
            width=80,
        )
        phone_lbl.grid(row=2, column=0, sticky=tk.W, pady=(8, 0))

        self.phone_url = ctk.CTkLabel(
            status_inner,
            textvariable=self.phone_url_var,
            text_color=THEME["url_link"],
            font=_mono_font(12),
            anchor="w",
        )
        self.phone_url.grid(row=2, column=1, sticky=tk.W, pady=(8, 0), padx=(12, 0))
        self.phone_url.configure(cursor="hand2")
        self.phone_url.bind("<Button-1>", lambda _event: self._copy_url(self.phone_url_var))

        tip_lbl = ctk.CTkLabel(
            status_inner,
            text="💡 点击访问地址即可复制到剪贴板。移动设备需与电脑处于同一局域网下。",
            text_color=THEME["fg_muted"],
            font=_font(10),
            anchor="w",
            justify="left",
        )
        tip_lbl.grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(12, 0))

        # 5. 日志区
        log_card, log_inner = self._create_card(container, "📝 服务实时日志", padding=14)
        log_card.pack(fill=tk.BOTH, expand=True)

        self.log_widget = ctk.CTkTextbox(
            log_inner,
            wrap="word",
            font=_mono_font(11),
            fg_color=THEME["log_bg"],
            text_color="#E5E7EB",
            border_color=THEME["border_card"],
            border_width=1,
            corner_radius=8,
            height=140,
            scrollbar_button_color=THEME["border_input"],
            scrollbar_button_hover_color=THEME["accent_hover"],
        )
        self.log_widget.pack(fill=tk.BOTH, expand=True)
        self.log_widget.configure(state="disabled")

    def _build_credential_row(
        self,
        parent: Any,
        row: int,
        label: str,
        text_var: tk.StringVar,
        show_var: tk.BooleanVar,
    ) -> None:
        lbl = ctk.CTkLabel(
            parent,
            text=label,
            text_color=THEME["fg_text"],
            font=_font(11),
            anchor="w",
            width=170,
        )
        lbl.grid(row=row, column=0, sticky=tk.W, pady=8)

        entry = ctk.CTkEntry(
            parent,
            textvariable=text_var,
            show="" if show_var.get() else "•",
            fg_color=THEME["bg_input"],
            border_color=THEME["border_input"],
            text_color=THEME["fg_text"],
            font=_font(11),
            corner_radius=8,
            border_width=1,
            height=34,
        )
        entry.grid(row=row, column=1, sticky=tk.EW, padx=(12, 0), pady=8)

        def toggle() -> None:
            entry.configure(show="" if show_var.get() else "•")

        chk = ctk.CTkCheckBox(
            parent,
            text="显示密码",
            variable=show_var,
            command=toggle,
            text_color=THEME["fg_muted"],
            font=_font(10),
            fg_color=THEME["accent_primary"],
            hover_color=THEME["accent_hover"],
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=5,
            border_width=2,
            border_color=THEME["border_input"],
        )
        chk.grid(row=row, column=2, padx=(14, 0), pady=8)

    # ------- persistence -------

    def _load_persisted(self) -> None:
        try:
            data = credentials_store.load()
        except Exception:
            logger.exception("launcher_load_failed")
            return
        self.deepseek_var.set(data.deepseek_api_key)
        self.tavily_var.set(data.tavily_api_key)
        self.allow_lan_var.set(data.allow_lan)
        self.host_var.set(data.host)
        self.port_var.set(str(data.port))
        self.ocr_var.set(data.ocr_enabled)
        self.auth_disabled_var.set(data.auth_disabled)

    def _current_credentials(self) -> LauncherCredentials | None:
        try:
            port = int(self.port_var.get().strip() or DEFAULT_PORT)
        except ValueError:
            messagebox.showerror("端口无效", "请填写 1-65535 之间的端口号。")
            return None
        if port < 1 or port > 65535:
            messagebox.showerror("端口无效", "请填写 1-65535 之间的端口号。")
            return None
        allow_lan = self.allow_lan_var.get()
        host = LAN_HOST if allow_lan else DEFAULT_HOST
        self.host_var.set(host)
        return LauncherCredentials(
            deepseek_api_key=self.deepseek_var.get().strip(),
            tavily_api_key=self.tavily_var.get().strip(),
            host=host,
            port=port,
            allow_lan=allow_lan,
            ocr_enabled=self.ocr_var.get(),
            auth_disabled=self.auth_disabled_var.get(),
        )

    def _sync_host_with_lan(self) -> None:
        self.host_var.set(LAN_HOST if self.allow_lan_var.get() else DEFAULT_HOST)

    # ------- button handlers -------

    def _on_start(self) -> None:
        creds = self._current_credentials()
        if creds is None:
            return
        if not creds.deepseek_api_key:
            if not messagebox.askyesno(
                "未填写 DeepSeek API Key",
                "尚未填写 DeepSeek API Key。可以稍后在网页右上角设置中临时填写，是否继续启动？",
            ):
                return
        if port_in_use("127.0.0.1", creds.port):
            if not messagebox.askyesno(
                "端口被占用",
                f"端口 {creds.port} 似乎已被占用，服务会自动尝试下一个端口。是否继续启动？",
            ):
                return
        try:
            credentials_store.save(creds)
        except Exception:
            logger.exception("launcher_save_failed")
        self._set_buttons_running(True)
        self._update_urls(creds)
        self.runtime.start(creds)

    def _on_stop(self) -> None:
        self.status_var.set("● 正在停止…")
        self.status_badge.configure(text_color=THEME["warning"])
        self.stop_button.configure(state="disabled")
        threading.Thread(target=self.runtime.stop, name="launcher-stop", daemon=True).start()

    def _on_open_browser(self) -> None:
        url = self.computer_url_var.get()
        if url and url != "—":
            webbrowser.open(url, new=2)

    def _on_save(self) -> None:
        creds = self._current_credentials()
        if creds is None:
            return
        try:
            credentials_store.save(creds)
        except Exception as exc:
            logger.exception("launcher_save_failed")
            messagebox.showerror("保存失败", str(exc))
            return
        messagebox.showinfo("已保存", "配置已成功加密保存到本机。")

    def _on_clear(self) -> None:
        if not messagebox.askyesno("清空保存的 Key", "确认删除本机保存的 API Key 与配置？"):
            return
        credentials_store.clear()
        self.deepseek_var.set("")
        self.tavily_var.set("")
        self.allow_lan_var.set(False)
        self.ocr_var.set(False)
        self.auth_disabled_var.set(False)
        self.port_var.set(str(DEFAULT_PORT))
        self._sync_host_with_lan()

    def _on_close(self) -> None:
        if self.runtime.is_running():
            if not messagebox.askyesno("退出", "服务仍在运行。退出会停止本地服务，确定吗？"):
                return
            self.runtime.stop()
        self.root.destroy()

    # ------- event loop bridging -------

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self._event_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self._apply_status(payload)
        except queue.Empty:
            pass
        self.root.after(POLL_INTERVAL_MS, self._drain_events)

    def _append_log(self, line: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", line + "\n")
        line_count = int(self.log_widget.index("end-1c").split(".")[0])
        if line_count > LOG_BUFFER_LINES:
            self.log_widget.delete("1.0", f"{line_count - LOG_BUFFER_LINES}.0")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

        if "server_started" in line:
            self._try_update_urls_from_log(line)

    def _apply_status(self, status: str) -> None:
        labels = {
            "starting": "● 正在启动…",
            "running": "● 运行中",
            "stopping": "● 正在停止…",
            "stopped": "● 已停止",
        }
        self.status_var.set(labels.get(status, status))

        if status == "running":
            self.status_badge.configure(text_color=THEME["success"])
            self._set_buttons_running(True)
        elif status == "starting":
            self.status_badge.configure(text_color=THEME["warning"])
            self._set_buttons_running(True)
        elif status == "stopping":
            self.status_badge.configure(text_color=THEME["warning"])
            self._set_buttons_running(True)
        elif status == "stopped":
            self.status_badge.configure(text_color=THEME["fg_muted"])
            self._set_buttons_running(False)
            self.computer_url_var.set("—")
            self.phone_url_var.set("—")
            self.open_button.configure(state="disabled")

    def _set_buttons_running(self, running: bool) -> None:
        if running:
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.open_button.configure(state="normal")
        else:
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")

    def _update_urls(self, creds: LauncherCredentials) -> None:
        port = creds.port
        token = settings.auth.token if not creds.auth_disabled else ""
        computer = f"http://127.0.0.1:{port}/"
        try:
            ip = local_ip()
        except Exception:
            ip = "127.0.0.1"
        phone = f"http://{ip}:{port}/"
        if token:
            computer = url_with_token(computer, token)
            phone = url_with_token(phone, token)
        self.computer_url_var.set(computer)
        self.phone_url_var.set(phone)

    def _try_update_urls_from_log(self, line: str) -> None:
        start = line.find("{")
        if start < 0:
            return
        try:
            payload = json.loads(line[start:])
        except json.JSONDecodeError:
            return
        computer = launcher_url_from_log(payload.get("computer_url"), settings.auth.token)
        phone = launcher_url_from_log(payload.get("phone_url"), settings.auth.token)
        if computer:
            self.computer_url_var.set(computer)
        if phone:
            self.phone_url_var.set(phone)
        self.open_button.configure(state="normal")

    def _copy_url(self, var: tk.StringVar) -> None:
        value = var.get()
        if not value or value == "—":
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)

        running = self.runtime.is_running()
        self.status_var.set("● 已复制访问地址")
        self.status_badge.configure(text_color=THEME["success"])
        self.root.after(1500, lambda: self._restore_status_text(running))

    def _restore_status_text(self, running: bool) -> None:
        if running:
            self.status_var.set("● 运行中")
            self.status_badge.configure(text_color=THEME["success"])
        else:
            self.status_var.set("● 已停止")
            self.status_badge.configure(text_color=THEME["fg_muted"])


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        try:
            probe.connect((host, port))
        except (OSError, socket.timeout):
            return False
        return True


def _enable_windows_dpi_awareness() -> float:
    """声明进程 DPI-aware 并返回 Tk scaling 因子（dpi/72）。非 Windows 返回 0。"""
    import sys
    if sys.platform != "win32":
        return 0.0
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                ctypes.windll.user32.SetProcessDPIAware()
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
        except (AttributeError, OSError):
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
            ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi and dpi > 0:
            return dpi / 72.0
    except Exception:
        logger.debug("dpi_awareness_setup_failed", exc_info=True)
    return 0.0


def main() -> None:
    _enable_windows_dpi_awareness()
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    root = ctk.CTk()
    LauncherWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
