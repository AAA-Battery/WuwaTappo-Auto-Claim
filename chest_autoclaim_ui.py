"""
Wuwa Tappo - Chest Auto-Claim UI (Tkinter, Windows)

Run:  python chest_autoclaim_ui.py

Layout (top -> bottom):
    Header       : title + subtitle
    Status pill  : current state of the auto-claimer
    Timer cards  : LEFT CHEST | RIGHT CHEST
    Stats card   : available server clicks | claimed this session
    Actions      : Pause Auto-Claim | Re-pair Session | Minimize to Tray | Click when idle
    Note         : informational text
    Footer       : [gear] Settings (bottom-left) ........ Exit (bottom-right)

"Open Activity Log" and "Re-sync Tappo UI" live inside the Settings dialog.

Connect your existing logic through the constructor callbacks and the
public set_* methods of ChestAutoClaimUI.
"""

import sys
import tkinter as tk

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BG = "#0F1623"
CARD = "#161F30"
CARD_HOVER = "#1C2740"
BORDER = "#26324A"
TEXT = "#E6EAF2"
MUTED = "#8B97AD"
SUBTLE = "#5F6B82"
ACCENT = "#4FD1A5"
ACCENT_HOVER = "#6EE0BA"
ACCENT_TEXT = "#062018"
WARN = "#F2C14E"
DANGER = "#F26D6D"

FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"
FONT_ICON = "Segoe UI Symbol"

VARIANTS = {
    "primary": {"bg": ACCENT, "fg": ACCENT_TEXT, "border": ACCENT,
                "hover_bg": ACCENT_HOVER, "hover_fg": ACCENT_TEXT, "hover_border": ACCENT_HOVER},
    "secondary": {"bg": CARD, "fg": TEXT, "border": BORDER,
                  "hover_bg": CARD_HOVER, "hover_fg": TEXT, "hover_border": "#33415E"},
    "danger": {"bg": CARD, "fg": TEXT, "border": BORDER,
               "hover_bg": "#3A1E24", "hover_fg": "#FFD9D9", "hover_border": DANGER},
    "ghost": {"bg": BG, "fg": MUTED, "border": BG,
              "hover_bg": CARD, "hover_fg": TEXT, "hover_border": BORDER},
}

STATUS_COLORS = {
    "waiting": WARN,
    "active": ACCENT,
    "paused": MUTED,
    "error": DANGER,
}

# rarity key -> (label, text color, badge background) -- matches in-game colors
RARITY = {
    "epic": ("Epic", "#F5B98C", "#3B2A1F"),          # peach / gold
    "rare": ("Rare", "#D29BEB", "#33223F"),          # purple
    "uncommon": ("Uncommon", "#7FB2FF", "#1A2A47"),  # blue
    "common": ("Common", "#5EE0A0", "#163628"),      # green
}
RARITY_ORDER = ("epic", "rare", "uncommon", "common")

# Item type of a claimed reward
CATEGORIES = ("Character", "Accessory", "Emote")


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------
class HoverButton(tk.Label):
    """Flat, themeable button (tk.Button ignores colors on Windows)."""

    def __init__(self, master, text, command=None, variant="secondary",
                 font=(FONT_SEMI, 10), padx=16, pady=9, **kw):
        self._command = command
        self._variant = variant
        self._enabled = True
        s = VARIANTS[variant]
        super().__init__(master, text=text, font=font, padx=padx, pady=pady,
                         bg=s["bg"], fg=s["fg"], cursor="hand2",
                         highlightthickness=1, highlightbackground=s["border"],
                         highlightcolor=s["border"], **kw)
        self.bind("<Enter>", lambda e: self._paint(True))
        self.bind("<Leave>", lambda e: self._paint(False))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _paint(self, hover):
        s = VARIANTS[self._variant]
        if not self._enabled:
            self.configure(bg=s["bg"], fg=SUBTLE,
                           highlightbackground=BORDER, highlightcolor=BORDER)
            return
        p = "hover_" if hover else ""
        self.configure(bg=s[p + "bg"], fg=s[p + "fg"],
                       highlightbackground=s[p + "border"], highlightcolor=s[p + "border"])

    def _on_release(self, event):
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        if inside and self._enabled and self._command:
            self._command()

    def set_variant(self, variant):
        self._variant = variant
        self._paint(False)

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._paint(False)


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress-1>", self._hide, add="+")

    def _show(self, _):
        if self.tip:
            return
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() - 30
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, bg="#0A0F18", fg=TEXT, font=(FONT, 9),
                 padx=8, pady=3, highlightthickness=1,
                 highlightbackground=BORDER).pack()

    def _hide(self, _):
        if self.tip:
            self.tip.destroy()
            self.tip = None


def card(master, **kw):
    return tk.Frame(master, bg=CARD, highlightthickness=1,
                    highlightbackground=BORDER, highlightcolor=BORDER, **kw)


def enable_dark_titlebar(window):
    """Request a dark native title bar on Windows 10/11."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user32.GetAncestor.restype = ctypes.c_void_p
        dwmapi.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long

        window.update_idletasks()
        client_hwnd = ctypes.c_void_p(window.winfo_id())
        # GA_ROOT gets the top-level HWND without truncating it to a 32-bit int.
        hwnd = user32.GetAncestor(client_hwnd, 2) or client_hwnd
        enabled = ctypes.c_int(1)
        for attribute in (20, 19):  # Current and legacy DWMWA_USE_IMMERSIVE_DARK_MODE.
            result = dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            )
            if result == 0:
                break
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Settings dialog
# ---------------------------------------------------------------------------
class SettingsDialog(tk.Toplevel):
    def __init__(self, master, on_open_log, on_resync, on_add_clicks):
        super().__init__(master)
        self.title("Settings")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)

        body = tk.Frame(self, bg=BG, padx=24, pady=20)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="Settings", bg=BG, fg=TEXT,
                 font=(FONT_SEMI, 15)).pack(anchor="w")
        tk.Label(body, text="Tools and maintenance", bg=BG, fg=MUTED,
                 font=(FONT, 9)).pack(anchor="w", pady=(2, 14))

        self._row(body, "Open Activity Log",
                  "View claim history and recent events.", "Open", on_open_log)
        self._resync_button = self._row(
            body, "Re-sync Tappo UI",
            "Reload timers and state from Wuwa Tappo.", "Re-sync", on_resync
        )
        self._add_clicks_button = self._row(
            body, "Add 1000 clicks",
            "Send 1,000 F15 presses with 0.15-second spacing.",
            "Add", on_add_clicks,
        )

        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(16, 12))
        footer = tk.Frame(body, bg=BG)
        footer.pack(fill="x")
        HoverButton(footer, "Close", self.close, "secondary", padx=20).pack(side="right")

        self.bind("<Escape>", lambda e: self.close())
        self.protocol("WM_DELETE_WINDOW", self.close)

        # Center over parent, then make modal.
        self.update_idletasks()
        w, h = max(self.winfo_reqwidth(), 420), self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")
        self.after(50, lambda: enable_dark_titlebar(self))
        self.grab_set()
        self.focus_set()

    def _row(self, master, title, desc, action, command):
        row = card(master, padx=14, pady=12)
        row.pack(fill="x", pady=(0, 8))
        text = tk.Frame(row, bg=CARD)
        text.pack(side="left", fill="x", expand=True)
        tk.Label(text, text=title, bg=CARD, fg=TEXT,
                 font=(FONT_SEMI, 10)).pack(anchor="w")
        tk.Label(text, text=desc, bg=CARD, fg=MUTED,
                 font=(FONT, 9)).pack(anchor="w")

        def run():
            if command:
                command()

        button = HoverButton(row, action, run, "secondary", padx=14, pady=6,
                             font=(FONT_SEMI, 9))
        button.pack(side="right", padx=(12, 0))
        return button

    def set_resyncing(self, resyncing):
        self._resync_button.set_enabled(not resyncing)

    def set_add_clicks_running(self, running):
        self._add_clicks_button.configure(text="Adding..." if running else "Add")
        self._add_clicks_button.set_enabled(not running)

    def close(self):
        self.grab_release()
        self.destroy()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class ChestAutoClaimUI:
    def __init__(self, root, on_toggle_pause=None, on_repair=None,
                 on_minimize=None, on_open_log=None, on_resync=None, on_exit=None,
                 on_toggle_idle_click=None, on_add_clicks=None):
        self.root = root
        self.on_toggle_pause = on_toggle_pause
        self.on_repair = on_repair
        self.on_minimize = on_minimize
        self.on_open_log = on_open_log
        self.on_resync = on_resync
        self.on_exit = on_exit
        self.on_toggle_idle_click = on_toggle_idle_click
        self.on_add_clicks = on_add_clicks
        self.enabled = False
        self.paused = False
        self.pairing = False
        self.resyncing = False
        self.add_clicks_running = False
        self._settings = None

        root.title("Wuwa Tappo \u2022 Chest Auto-Claim")
        root.configure(bg=BG)
        root.geometry("720x600")
        root.minsize(680, 540)
        # Closing the window minimizes to tray (same as the button).
        root.protocol("WM_DELETE_WINDOW", self._minimize)

        body = tk.Frame(root, bg=BG, padx=28, pady=24)
        body.pack(fill="both", expand=True)

        self._build_header(body)
        self._build_status(body)
        self._build_timers(body)
        self._build_stats(body)
        self._build_actions(body)
        tk.Frame(body, bg=BG).pack(fill="both", expand=True)  # flexible spacer
        self._build_footer(body)

        root.after(50, lambda: enable_dark_titlebar(root))

    # ---- sections ---------------------------------------------------------
    def _build_header(self, parent):
        tk.Label(parent, text="Chest Auto-Claim", bg=BG, fg=TEXT,
                 font=(FONT_SEMI, 20)).pack(anchor="w")
        tk.Label(parent, text="Wuwa Tappo  \u00b7  Synchronized with official server cooldown",
                 bg=BG, fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(2, 16))

    def _build_status(self, parent):
        pill = card(parent, padx=12, pady=7)
        pill.pack(anchor="w", pady=(0, 14))
        self._dot = tk.Canvas(pill, width=10, height=10, bg=CARD, highlightthickness=0)
        self._dot.pack(side="left", padx=(0, 8))
        self._dot_id = self._dot.create_oval(1, 1, 9, 9, fill=WARN, outline="")
        self._status = tk.Label(pill, text="Waiting for server cooldown...",
                                bg=CARD, fg=TEXT, font=(FONT, 10))
        self._status.pack(side="left")

    def _build_timers(self, parent):
        grid = tk.Frame(parent, bg=BG)
        grid.pack(fill="x", pady=(0, 10))
        grid.columnconfigure((0, 1), weight=1, uniform="timers")
        self._left = self._timer_card(grid, "LEFT CHEST", 0, (0, 5))
        self._right = self._timer_card(grid, "RIGHT CHEST", 1, (5, 0))

    def _timer_card(self, grid, title, col, padx):
        c = card(grid, padx=18, pady=14)
        c.grid(row=0, column=col, sticky="ew", padx=padx)
        tk.Label(c, text=title, bg=CARD, fg=MUTED, font=(FONT_SEMI, 9)).pack(anchor="w")
        value = tk.Label(c, text="--:--", bg=CARD, fg=ACCENT, font=(FONT_SEMI, 26))
        value.pack(anchor="w", pady=(2, 0))
        tk.Label(c, text="until next claim", bg=CARD, fg=SUBTLE,
                 font=(FONT, 9)).pack(anchor="w")
        return value

    def _build_stats(self, parent):
        c = card(parent, padx=18, pady=12)
        c.pack(fill="x", pady=(0, 18))
        c.columnconfigure((0, 2, 4), weight=1, uniform="stats")

        def column(col, label):
            f = tk.Frame(c, bg=CARD)
            f.grid(row=0, column=col, sticky="nw")
            tk.Label(f, text=label, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
            return f

        def divider(col):
            tk.Frame(c, bg=BORDER, width=1).grid(row=0, column=col, sticky="ns", padx=14)

        # Available server clicks
        f = column(0, "Available server clicks")
        self._clicks = tk.Label(f, text="-", bg=CARD, fg=TEXT, font=(FONT_SEMI, 12))
        self._clicks.pack(anchor="w")
        divider(1)

        # Total claimed this session + rarity breakdown
        f = column(2, "Total claimed (session)")
        self._claimed = tk.Label(f, text="-", bg=CARD, fg=TEXT, font=(FONT_SEMI, 12))
        self._claimed.pack(anchor="w")
        breakdown = tk.Frame(f, bg=CARD)
        breakdown.pack(anchor="w", pady=(2, 0))
        self._rarity_counts = {}
        for i, key in enumerate(RARITY_ORDER):  # 2 x 2 grid: Epic Rare / Uncommon Common
            lbl = tk.Label(breakdown, text="", bg=CARD, fg=RARITY[key][1], font=(FONT, 9))
            lbl.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 10))
            self._rarity_counts[key] = lbl
        divider(3)

        # Last claim: rarity badge + detail
        f = column(4, "Last claim")
        top = tk.Frame(f, bg=CARD)
        top.pack(anchor="w", pady=(2, 1))
        self._last_badge = tk.Label(top, text="None yet", bg=CARD, fg=SUBTLE,
                                    font=(FONT_SEMI, 9), padx=0, pady=2)
        self._last_badge.pack(side="left")
        self._last_category = tk.Label(top, text="", bg=CARD, fg=TEXT, font=(FONT_SEMI, 10))
        self._last_category.pack(side="left", padx=(8, 0))
        self._last_detail = tk.Label(f, text="", bg=CARD, fg=SUBTLE, font=(FONT, 9),
                                     wraplength=175, justify="left")
        self._last_detail.pack(anchor="w")

    def _build_actions(self, parent):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x")
        row.columnconfigure((0, 1, 2, 3), weight=1, uniform="actions")
        self._pause_btn = HoverButton(row, "Pause Auto-Claim", self._toggle_pause, "primary")
        self._pause_btn.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self._repair_btn = HoverButton(row, "Re-pair Session", self._call(lambda: self.on_repair))
        self._repair_btn.grid(row=0, column=1, sticky="ew", padx=5)
        HoverButton(row, "Minimize to Tray", self._minimize).grid(
            row=0, column=2, sticky="ew", padx=5)
        self._idle_click_btn = HoverButton(
            row, "Click when idle", self._toggle_idle_click, "secondary"
        )
        self._idle_click_btn.grid(row=0, column=3, sticky="ew", padx=(5, 0))
        Tooltip(
            self._idle_click_btn,
            "When inactive for 15 seconds, the application will automatically click 1000 times, with a 5-minute cooldown.",
        )

    def _build_footer(self, parent):
        tk.Label(parent, justify="left", anchor="w", bg=BG, fg=SUBTLE, font=(FONT, 9),
                 text="Keep Wuwa Tappo running. Never changes system time or fabricates clicks.\n"
                      "Closing this window minimizes to the system tray."
                 , wraplength=650).pack(fill="x", pady=(8, 4))
        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=(0, 12))

        footer = tk.Frame(parent, bg=BG)
        footer.pack(fill="x", padx=12)
        gear = HoverButton(footer, "\u2699", self.open_settings, "ghost",
                           font=(FONT_ICON, 15), padx=10, pady=2)
        gear.pack(side="left")
        Tooltip(gear, "Settings")
        HoverButton(footer, "Exit", self._exit, "danger", padx=28).pack(side="right")

    # ---- handlers ---------------------------------------------------------
    def _call(self, get_cb):
        def run():
            cb = get_cb()
            if cb:
                cb()
        return run

    def _toggle_pause(self):
        if self.on_toggle_pause:
            self.on_toggle_pause()

    def _minimize(self):
        if self.on_minimize:
            self.on_minimize()
        else:
            self.root.iconify()

    def _exit(self):
        if self.on_exit:
            self.on_exit()
        else:
            self.root.destroy()

    def open_settings(self):
        if self._settings and self._settings.winfo_exists():
            self._settings.lift()
            return
        self._settings = SettingsDialog(
            self.root,
            on_open_log=self._call(lambda: self.on_open_log),
            on_resync=self._call(lambda: self.on_resync),
            on_add_clicks=self._call(lambda: self.on_add_clicks),
        )
        self._settings.set_resyncing(self.resyncing)
        self._settings.set_add_clicks_running(self.add_clicks_running)

    # ---- public API -------------------------------------------------------
    def set_status(self, text, state="waiting"):
        """state: 'waiting' | 'active' | 'paused' | 'error'"""
        self._status.configure(text=text)
        self._dot.itemconfigure(self._dot_id, fill=STATUS_COLORS.get(state, MUTED))

    def set_timers(self, left, right):
        self._left.configure(text=left)
        self._right.configure(text=right)

    def set_available_clicks(self, count):
        self._clicks.configure(text="—" if count is None else f"{count:,}")

    def set_claimed(self, total, epic=0, rare=0, uncommon=0, common=0):
        """Total chests claimed this session, with a per-rarity breakdown."""
        self._claimed.configure(text=f"{total} chest{'s' if total != 1 else ''}")
        counts = {"epic": epic, "rare": rare, "uncommon": uncommon, "common": common}
        for key, lbl in self._rarity_counts.items():
            lbl.configure(text=f"{RARITY[key][0]} {counts[key]}")

    def set_last_claim(self, rarity, category=None, detail=""):
        """rarity:   'epic' | 'rare' | 'uncommon' | 'common' | None (nothing claimed yet)
        category: 'Character' | 'Accessory' | 'Emote'
        detail:   free text, e.g. 'Left chest \u00b7 14:32' or the item name."""
        if rarity is None:
            self._last_badge.configure(text="None yet", bg=CARD, fg=SUBTLE, padx=0)
            self._last_category.configure(text="")
        else:
            label, fg, bg = RARITY.get(rarity.lower(), (rarity.title(), TEXT, BORDER))
            self._last_badge.configure(text=label.upper(), bg=bg, fg=fg, padx=8)
            self._last_category.configure(text=(category or "").title())
        self._last_detail.configure(text=detail)

    def set_paused(self, paused):
        self.set_auto_claim_state(not paused, paused=paused)
        self.set_status("Auto-claim paused" if paused else "Waiting for server cooldown...",
                        "paused" if paused else "waiting")

    def set_auto_claim_state(self, enabled, paused=False):
        self.enabled = bool(enabled)
        self.paused = bool(paused) if not enabled else False
        if enabled:
            label = "Pause Auto-Claim"
        elif paused:
            label = "Resume Auto-Claim"
        else:
            label = "Enable Auto-Claim"
        self._pause_btn.configure(text=label)

    def set_click_when_idle(self, enabled):
        self._idle_click_btn.set_variant("primary" if enabled else "secondary")

    def _toggle_idle_click(self):
        if self.on_toggle_idle_click:
            self.on_toggle_idle_click()

    def set_pairing(self, pairing):
        self.pairing = bool(pairing)
        self._repair_btn.set_enabled(not self.pairing)
        self._pause_btn.set_enabled(not self.pairing)

    def set_resyncing(self, resyncing):
        self.resyncing = bool(resyncing)
        if self._settings and self._settings.winfo_exists():
            self._settings.set_resyncing(self.resyncing)

    def set_add_clicks_running(self, running):
        self.add_clicks_running = bool(running)
        if self._settings and self._settings.winfo_exists():
            self._settings.set_add_clicks_running(running)

