import tkinter as tk
from tkinter import ttk, messagebox
import time
import threading
import platform
import json
import os
import sys
import ctypes

# ── optional deps ──────────────────────────────────────────────────────────
try:
    import keyboard
    HAS_KEYBOARD = True
except ImportError:
    HAS_KEYBOARD = False

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# ─── Config path ─────────────────────────────────────────────────────────────
# PyInstaller --onefile จะ extract ไปที่ temp folder ชั่วคราว
# ดังนั้นต้องใช้ sys.executable เพื่อหา path ของ .exe จริงๆ เสมอ
def _get_config_path():
    if getattr(sys, "frozen", False):
        # กำลังรันเป็น .exe (PyInstaller)
        base = os.path.dirname(sys.executable)
    else:
        # กำลังรันเป็น .py ปกติ
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "clock_config.json")

CONFIG_FILE = _get_config_path()
DEFAULT_CONFIG = {
    "opacity": 0.85, "font_size": 42,
    "pos_x": 200, "pos_y": 200, "win_w": 280, "win_h": 105,
    "click_through": False, "alarms": [],
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            c = DEFAULT_CONFIG.copy(); c.update(d); return c
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[config] {e}")

# ─── Alarm beep ──────────────────────────────────────────────────────────────
def beep_alarm():
    for _ in range(6):
        if platform.system() == "Windows":
            import winsound; winsound.Beep(1000, 400)
        else:
            print("\a", end="", flush=True)
        time.sleep(0.3)

# ─── Windows click-through ───────────────────────────────────────────────────
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020

def _set_click_through(hwnd, enable):
    if platform.system() != "Windows" or not hwnd:
        return
    try:
        s = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
        if enable:
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, s | WS_EX_LAYERED | WS_EX_TRANSPARENT)
        else:
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, s & ~WS_EX_TRANSPARENT)
    except Exception as e:
        print(f"[click-through] {e}")

# ─── Tray icon helpers ───────────────────────────────────────────────────────
def _make_tray_icon(active: bool):
    """Draw a simple clock-face icon; green = normal, purple = click-through."""
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d    = ImageDraw.Draw(img)
    color = "#9922ee" if active else "#00cc66"
    d.ellipse([4, 4, size-4, size-4], fill=color)
    cx = cy = size // 2
    # hour hand
    d.line([cx, cy, cx, cy - 14], fill="white", width=3)
    # minute hand
    d.line([cx, cy, cx + 12, cy], fill="white", width=2)
    return img

# ─── Main App ────────────────────────────────────────────────────────────────
class ClockOverlay:
    def __init__(self, root):
        self.root          = root
        self.root.title("Clock Overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#0d0d0d")
        self.root.minsize(160, 70)

        self.cfg           = load_config()
        self.opacity       = self.cfg["opacity"]
        self.font_size     = self.cfg["font_size"]
        self.click_through = self.cfg["click_through"]
        self.alarms        = list(self.cfg.get("alarms", []))
        self.fired_alarms  = set()
        self._drag_x = self._drag_y = 0
        self._rx = self._ry = self._rw = self._rh = 0
        self.panel_win     = None
        self._hwnd         = None
        self._save_job     = None
        self._tray         = None
        self._tray_thread  = None

        self.root.geometry(
            f"{self.cfg['win_w']}x{self.cfg['win_h']}"
            f"+{self.cfg['pos_x']}+{self.cfg['pos_y']}"
        )
        self.root.attributes("-alpha", self.opacity)

        self._build_ui()
        self._tick()
        self._check_alarms_loop()
        self.root.after(200, self._post_init)
        self.root.bind("<Configure>", self._on_configure)

    # ── post-init (needs window drawn) ──────────────────────────────────────
    def _post_init(self):
        self._hwnd = None
        hw = self._get_hwnd()
        _set_click_through(hw, self.click_through)
        self._update_visual()
        self._start_global_hotkey()
        self._start_tray()

    def _get_hwnd(self):
        if self._hwnd is None and platform.system() == "Windows":
            h = ctypes.windll.user32.GetParent(self.root.winfo_id())
            self._hwnd = h if h else self.root.winfo_id()
        return self._hwnd

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        self.topbar = tk.Frame(self.root, bg="#111111", height=22)
        self.topbar.pack(fill=tk.X, side=tk.TOP)
        self.topbar.pack_propagate(False)

        self.title_lbl = tk.Label(self.topbar, text="  🕐 Clock",
                                   bg="#111111", fg="#555555", font=("Arial", 8))
        self.title_lbl.pack(side=tk.LEFT)

        self.badge = tk.Label(self.topbar, text="", bg="#111111",
                               fg="#cc88ff", font=("Arial", 7, "bold"))
        self.badge.pack(side=tk.LEFT, padx=2)

        for txt, cmd, fg in [("✕", self._on_close, "#cc4444"),
                              ("⚙", self._toggle_panel, "#888888")]:
            tk.Button(self.topbar, text=txt, command=cmd,
                      bg="#111111", fg=fg, relief=tk.FLAT,
                      font=("Arial", 10, "bold" if txt == "✕" else "normal"),
                      activebackground="#333333", activeforeground="#fff",
                      bd=0, padx=6, pady=0, cursor="hand2").pack(side=tk.RIGHT)

        self.body = tk.Frame(self.root, bg="#0d0d0d")
        self.body.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        self.time_label = tk.Label(self.body, text="00:00:00",
                                    font=("Courier New", self.font_size, "bold"),
                                    fg="#00ff88", bg="#0d0d0d")
        self.time_label.pack(expand=True)

        self.date_label = tk.Label(self.body, text="",
                                    font=("Courier New", 10),
                                    fg="#777777", bg="#0d0d0d")
        self.date_label.pack()

        self.grip = tk.Label(self.root, text="◢", bg="#0d0d0d",
                              fg="#333333", font=("Arial", 12), cursor="sizing")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")

        for w in (self.topbar, self.title_lbl, self.badge,
                  self.body, self.time_label, self.date_label):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>",     self._do_drag)

        self.grip.bind("<ButtonPress-1>", self._start_resize)
        self.grip.bind("<B1-Motion>",     self._do_resize)

    # ── Move / Resize ─────────────────────────────────────────────────────────
    def _start_drag(self, e):
        self._drag_x = e.x_root - self.root.winfo_x()
        self._drag_y = e.y_root - self.root.winfo_y()
    def _do_drag(self, e):
        self.root.geometry(f"+{e.x_root-self._drag_x}+{e.y_root-self._drag_y}")

    def _start_resize(self, e):
        self._rx, self._ry = e.x_root, e.y_root
        self._rw, self._rh = self.root.winfo_width(), self.root.winfo_height()
    def _do_resize(self, e):
        nw = max(160, self._rw + e.x_root - self._rx)
        nh = max(70,  self._rh + e.y_root - self._ry)
        self.root.geometry(f"{nw}x{nh}")

    # ── Save ──────────────────────────────────────────────────────────────────
    def _on_configure(self, e=None):
        if self._save_job: self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(1000, self._persist)

    def _persist(self):
        self.cfg.update({
            "opacity": self.opacity, "font_size": self.font_size,
            "click_through": self.click_through,
            "pos_x": self.root.winfo_x(), "pos_y": self.root.winfo_y(),
            "win_w": self.root.winfo_width(), "win_h": self.root.winfo_height(),
            "alarms": self.alarms,
        })
        save_config(self.cfg)

    def _on_close(self):
        self._persist()
        if self._tray:
            try: self._tray.stop()
            except Exception: pass
        if HAS_KEYBOARD:
            try: keyboard.unhook_all()
            except Exception: pass
        self.root.destroy()

    # ── Clock ─────────────────────────────────────────────────────────────────
    def _tick(self):
        now = time.localtime()
        self.time_label.config(text=time.strftime("%H:%M:%S", now))
        self.date_label.config(text=time.strftime("%a  %d %b %Y", now))
        self.root.after(500, self._tick)

    # ── Alarm ─────────────────────────────────────────────────────────────────
    def _check_alarms_loop(self):
        now_hm = time.strftime("%H:%M")
        if now_hm == "00:00": self.fired_alarms.clear()
        for alarm in self.alarms:
            if alarm == now_hm and alarm not in self.fired_alarms:
                self.fired_alarms.add(alarm)
                self._fire_alarm(alarm)
        self.root.after(10_000, self._check_alarms_loop)

    def _fire_alarm(self, alarm_time):
        def flash(n=0):
            if n < 10:
                self.time_label.config(fg="#ff4444" if n%2==0 else "#00ff88")
                self.root.after(300, lambda: flash(n+1))
            else:
                self.time_label.config(fg="#00ff88")
        flash()
        threading.Thread(target=beep_alarm, daemon=True).start()
        was = self.click_through
        if was: self._apply_ct(False)
        messagebox.showinfo("⏰ แจ้งเตือน!", f"ถึงเวลา: {alarm_time}", parent=self.root)
        if was: self._apply_ct(True)

    # ── Click-through core ────────────────────────────────────────────────────
    def _apply_ct(self, enable):
        self.click_through = enable
        _set_click_through(self._get_hwnd(), enable)
        self._update_visual()
        self._update_tray_icon()
        self._persist()

    def toggle_ct(self):
        """Public toggle — called from hotkey thread and tray (thread-safe via after)."""
        self.root.after(0, lambda: self._apply_ct(not self.click_through))

    def _update_visual(self):
        if self.click_through:
            self.badge.config(text="👻 ทะลุผ่าน")
            bg = "#0a0a1a"
        else:
            self.badge.config(text="")
            bg = "#0d0d0d"
        self.root.configure(bg=bg)
        for w in (self.body, self.time_label, self.date_label, self.grip):
            w.configure(bg=bg)

    # ── Global hotkey (Ctrl+Shift+T) ─────────────────────────────────────────
    def _start_global_hotkey(self):
        if not HAS_KEYBOARD:
            # fallback: local Ctrl+T only works when window focused
            self.root.bind("<Control-t>", lambda e: self.toggle_ct())
            return
        try:
            keyboard.add_hotkey("ctrl+shift+t", self.toggle_ct, suppress=False)
        except Exception as e:
            print(f"[hotkey] {e}")

    # ── System Tray ───────────────────────────────────────────────────────────
    def _start_tray(self):
        if not HAS_TRAY:
            return

        # pystray requires plain callables — no lambdas with closures for checked items
        def on_toggle(icon, item):
            self.toggle_ct()

        def on_settings(icon, item):
            self.root.after(0, self._toggle_panel)

        def on_quit(icon, item):
            self.root.after(0, self._on_close)

        def is_ct_checked(item):
            return self.click_through

        menu = pystray.Menu(
            pystray.MenuItem(
                "👻 โหมดทะลุผ่าน (Click-through)",
                on_toggle,
                checked=is_ct_checked,   # shows checkmark when active
                default=True,            # double-click on tray icon triggers this
            ),
            pystray.MenuItem("⚙  ตั้งค่า", on_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("❌  ปิดโปรแกรม", on_quit),
        )

        icon_img = _make_tray_icon(self.click_through)
        self._tray = pystray.Icon("ClockOverlay", icon_img, "Clock Overlay", menu=menu)

        self._tray_thread = threading.Thread(target=self._tray.run, daemon=True)
        self._tray_thread.start()

    def _update_tray_icon(self):
        if self._tray is None:
            return
        try:
            self._tray.icon = _make_tray_icon(self.click_through)
            tip = "Clock Overlay  [👻 ทะลุผ่าน]" if self.click_through else "Clock Overlay"
            self._tray.title = tip
        except Exception:
            pass

    # ── Settings panel ────────────────────────────────────────────────────────
    def _toggle_panel(self):
        if self.panel_win and tk.Toplevel.winfo_exists(self.panel_win):
            self.panel_win.destroy(); self.panel_win = None; return
        self._open_panel()

    def _open_panel(self):
        was_ct = self.click_through
        if was_ct: _set_click_through(self._get_hwnd(), False)

        p = tk.Toplevel(self.root)
        self.panel_win = p
        p.title("ตั้งค่า Clock Overlay")
        p.configure(bg="#1a1a1a")
        p.resizable(False, False)
        p.attributes("-topmost", True)
        p.geometry(f"+{self.root.winfo_x()+10}+{self.root.winfo_y()+130}")

        def on_close():
            if was_ct: _set_click_through(self._get_hwnd(), True)
            p.destroy(); self.panel_win = None
        p.protocol("WM_DELETE_WINDOW", on_close)

        pad = dict(padx=14, pady=5)
        R = 0

        # ── Opacity ──
        tk.Label(p, text="🔆  ความทึบ", bg="#1a1a1a", fg="#cccccc",
                 font=("Arial", 10, "bold")).grid(row=R, column=0, columnspan=2, sticky="w", **pad)
        R+=1
        self.opacity_var = tk.DoubleVar(value=self.opacity)
        ttk.Scale(p, from_=0.1, to=1.0, orient=tk.HORIZONTAL,
                  variable=self.opacity_var, length=210,
                  command=self._on_opacity).grid(row=R, column=0, columnspan=2, **pad)
        self.opacity_pct = tk.Label(p, text=f"{int(self.opacity*100)}%",
                                    bg="#1a1a1a", fg="#00ff88", font=("Arial", 10), width=4)
        self.opacity_pct.grid(row=R, column=2, padx=4); R+=1

        # ── Font size ──
        tk.Label(p, text="🔡  ขนาดตัวอักษร", bg="#1a1a1a", fg="#cccccc",
                 font=("Arial", 10, "bold")).grid(row=R, column=0, columnspan=2, sticky="w", **pad)
        R+=1
        self.size_var = tk.IntVar(value=self.font_size)
        ttk.Scale(p, from_=16, to=120, orient=tk.HORIZONTAL,
                  variable=self.size_var, length=210,
                  command=self._on_size).grid(row=R, column=0, columnspan=2, **pad)
        self.size_lbl = tk.Label(p, text=str(self.font_size),
                                  bg="#1a1a1a", fg="#00ff88", font=("Arial", 10), width=4)
        self.size_lbl.grid(row=R, column=2, padx=4); R+=1

        tk.Frame(p, bg="#333333", height=1).grid(
            row=R, column=0, columnspan=3, sticky="ew", pady=8, padx=14); R+=1

        # ── Click-through section ──
        tk.Label(p, text="👻  โหมดทะลุผ่าน", bg="#1a1a1a", fg="#cccccc",
                 font=("Arial", 10, "bold")).grid(row=R, column=0, columnspan=3, sticky="w", **pad)
        R+=1

        # hotkey status line
        hk_txt = "🎯 Global Hotkey: Ctrl+Shift+T  (ใช้ได้แม้อยู่ในเกม)" if HAS_KEYBOARD \
                 else "⚠️ ติดตั้ง 'keyboard' เพื่อใช้ Global Hotkey:  pip install keyboard"
        hk_color = "#44bb88" if HAS_KEYBOARD else "#cc8800"
        tk.Label(p, text=hk_txt, bg="#1a1a1a", fg=hk_color,
                 font=("Arial", 8)).grid(row=R, column=0, columnspan=3, sticky="w", padx=14); R+=1

        tray_txt = "🖥️ System Tray: คลิกขวาที่ไอคอนในทาสก์บาร์" if HAS_TRAY \
                   else "⚠️ ติดตั้ง 'pystray pillow' เพื่อใช้ System Tray"
        tray_color = "#44bb88" if HAS_TRAY else "#cc8800"
        tk.Label(p, text=tray_txt, bg="#1a1a1a", fg=tray_color,
                 font=("Arial", 8)).grid(row=R, column=0, columnspan=3, sticky="w", padx=14, pady=(0,6)); R+=1

        self.ct_btn = tk.Button(p, text=self._ct_label(),
                                 command=lambda: [self._apply_ct(not self.click_through),
                                                  self._refresh_ct_btn()],
                                 font=("Arial", 10, "bold"), relief=tk.FLAT,
                                 padx=14, pady=6, cursor="hand2")
        self.ct_btn.grid(row=R, column=0, columnspan=3, pady=4); R+=1
        self._refresh_ct_btn()

        tk.Frame(p, bg="#333333", height=1).grid(
            row=R, column=0, columnspan=3, sticky="ew", pady=8, padx=14); R+=1

        # ── Alarm ──
        tk.Label(p, text="⏰  ตั้งเวลาเตือน", bg="#1a1a1a", fg="#cccccc",
                 font=("Arial", 10, "bold")).grid(row=R, column=0, columnspan=3, sticky="w", **pad)
        R+=1

        ef = tk.Frame(p, bg="#1a1a1a")
        ef.grid(row=R, column=0, columnspan=3, sticky="w", padx=14, pady=4); R+=1
        tk.Label(ef, text="HH:", bg="#1a1a1a", fg="#aaaaaa").pack(side=tk.LEFT)
        self.hour_var = tk.StringVar(value="08")
        tk.Spinbox(ef, from_=0, to=23, textvariable=self.hour_var,
                   format="%02.0f", width=3, bg="#2a2a2a", fg="#fff",
                   buttonbackground="#333").pack(side=tk.LEFT)
        tk.Label(ef, text="  MM:", bg="#1a1a1a", fg="#aaaaaa").pack(side=tk.LEFT)
        self.min_var = tk.StringVar(value="00")
        tk.Spinbox(ef, from_=0, to=59, textvariable=self.min_var,
                   format="%02.0f", width=3, bg="#2a2a2a", fg="#fff",
                   buttonbackground="#333").pack(side=tk.LEFT)
        tk.Button(ef, text="+ เพิ่ม", command=self._add_alarm,
                  bg="#006633", fg="white", relief=tk.FLAT,
                  padx=8, cursor="hand2").pack(side=tk.LEFT, padx=8)

        self.alarm_list_frame = tk.Frame(p, bg="#1a1a1a")
        self.alarm_list_frame.grid(row=R, column=0, columnspan=3,
                                   sticky="ew", padx=14, pady=4); R+=1
        self._refresh_alarm_list()

        tk.Button(p, text="  ปิดหน้าต่างนี้  ", command=on_close,
                  bg="#333333", fg="#cccccc", relief=tk.FLAT,
                  padx=16, pady=5, cursor="hand2").grid(row=R, column=0,
                  columnspan=3, pady=12)

    def _ct_label(self):
        return "🖱️  ปิดโหมดทะลุผ่าน (คืนการคลิก)" if self.click_through \
               else "👻  เปิดโหมดทะลุผ่าน (คลิกผ่านนาฬิกา)"

    def _refresh_ct_btn(self):
        if not hasattr(self, "ct_btn"): return
        if self.click_through:
            self.ct_btn.config(text=self._ct_label(), bg="#6600bb",
                               fg="white", activebackground="#8822dd")
        else:
            self.ct_btn.config(text=self._ct_label(), bg="#1a3a6a",
                               fg="white", activebackground="#2255aa")

    def _on_opacity(self, *_):
        v = round(self.opacity_var.get(), 2)
        self.opacity = v
        self.root.attributes("-alpha", v)
        if hasattr(self, "opacity_pct"):
            self.opacity_pct.config(text=f"{int(v*100)}%")
        self._persist()

    def _on_size(self, *_):
        v = int(self.size_var.get())
        self.font_size = v
        self.time_label.config(font=("Courier New", v, "bold"))
        if hasattr(self, "size_lbl"):
            self.size_lbl.config(text=str(v))
        self._persist()

    def _add_alarm(self):
        h = self.hour_var.get().zfill(2)
        m = self.min_var.get().zfill(2)
        s = f"{h}:{m}"
        if s not in self.alarms:
            self.alarms.append(s); self.alarms.sort()
            self.fired_alarms.discard(s)
        self._refresh_alarm_list(); self._persist()

    def _remove_alarm(self, s):
        if s in self.alarms: self.alarms.remove(s)
        self._refresh_alarm_list(); self._persist()

    def _refresh_alarm_list(self):
        for w in self.alarm_list_frame.winfo_children(): w.destroy()
        if not self.alarms:
            tk.Label(self.alarm_list_frame, text="ยังไม่มีการตั้งเตือน",
                     bg="#1a1a1a", fg="#555555", font=("Arial", 9)).pack(anchor="w")
            return
        for alarm in self.alarms:
            row = tk.Frame(self.alarm_list_frame, bg="#1a1a1a")
            row.pack(fill=tk.X, pady=2)
            tk.Label(row, text=f"⏰  {alarm}", bg="#1a1a1a", fg="#ffcc44",
                     font=("Courier New", 11)).pack(side=tk.LEFT)
            tk.Button(row, text="ลบ", command=lambda a=alarm: self._remove_alarm(a),
                      bg="#6b1010", fg="white", relief=tk.FLAT,
                      padx=6, cursor="hand2").pack(side=tk.RIGHT)


# ─── Entry point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    root = tk.Tk()
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("Horizontal.TScale", background="#1a1a1a",
                    troughcolor="#333333", sliderthickness=14)
    ClockOverlay(root)
    root.mainloop()