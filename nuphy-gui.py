#!/usr/bin/env python3
"""Settings window for NuPhy HE keyboards (GTK 4 + libadwaita).

One page per tab of the NuPhyIO web driver: key bindings, trigger settings,
advanced keys, lighting, macros, mode settings, tools. Simple settings are sent
as soon as they change; the keyboard is then read back and every page redrawn
from what it actually holds. Device work runs on one worker thread, through
nuphy.py. NUPHY_DEMO=1 runs against a simulated Air75 HE.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import nuphy  # noqa: E402
from nuphy import KEYCODES as KC  # noqa: E402

LAYER_NAMES = ["Base", "Fn", "Fn 2", "Couche 3"]
RATE_HINT = "Le clavier redémarre pour changer de polling : quelques secondes sans réponse sont normales."
# Linux input codes -> keycode names, to record a macro from this very keyboard.
EVDEV = {1: "ESC", 12: "MINS", 13: "EQL", 14: "BSPC", 15: "TAB", 26: "LBRC", 27: "RBRC", 28: "ENT", 29: "LCTL",
         39: "SCLN", 40: "QUOT", 41: "GRV", 42: "LSFT", 43: "BSLS", 51: "COMM", 52: "DOT", 53: "SLSH", 54: "RSFT",
         55: "PAST", 56: "LALT", 57: "SPC", 58: "CAPS", 69: "NUM", 70: "SCRL", 74: "PMNS", 78: "PPLS", 82: "P0",
         83: "PDOT", 86: "NUBS", 87: "F11", 88: "F12", 96: "PENT", 97: "RCTL", 98: "PSLS", 99: "PSCR", 100: "RALT",
         102: "HOME", 103: "UP", 104: "PGUP", 105: "LEFT", 106: "RGHT", 107: "END", 108: "DOWN", 109: "PGDN",
         110: "INS", 111: "DEL", 119: "PAUS", 125: "LGUI", 126: "RGUI", 127: "APP"}
EVDEV.update({2 + i: c for i, c in enumerate("1234567890")})
EVDEV.update({16 + i: c for i, c in enumerate("QWERTYUIOP")})
EVDEV.update({30 + i: c for i, c in enumerate("ASDFGHJKL")})
EVDEV.update({44 + i: c for i, c in enumerate("ZXCVBNM")})
EVDEV.update({59 + i: f"F{i + 1}" for i in range(10)})
EVDEV.update({71: "P7", 72: "P8", 73: "P9", 75: "P4", 76: "P5", 77: "P6", 79: "P1", 80: "P2", 81: "P3"})


class _Plain:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def read_all(kb: nuphy.Keyboard, mode: int, fast: bool = False) -> dict:
    """Everything the pages show for one mode. `fast` brackets the read with the
    firmware's fast-transfer mode, as the web driver does when it starts."""
    kb.current_mode()
    with kb.bulk() if fast else _Plain():
        base = mode * nuphy.LAYERS_PER_MODE
        snap = nuphy.describe(kb, mode)
        snap.update(
            index_map=dict(kb.index_map()), layout=kb.model.layout,
            keymaps=[kb.keymap(base + i) for i in range(nuphy.LAYERS_PER_MODE)],
            defaults=[kb.default_keymap(base + i) for i in range(nuphy.LAYERS_PER_MODE)],
            rt=kb.rt_all(mode), advanced=kb.advanced(mode), macros=kb.macros(mode),
            colors=kb.key_colors(mode), auto_select=kb.auto_select(), version=kb.version,
            switches=[(s, kb.model.switch(s)) for s in kb.model.switch_list(kb.version)],
            light_modes=kb.model.light_modes, precision=kb.model.precision,
            macro_loop=kb.model.supports("macro_loop", kb.version),
            gamepad=kb.gamepad(mode) if kb.model.supports("gamepad", kb.version) else None,
            xbox=kb.model.supports("xbox", kb.version),
            titles={i: kb.key_title(i) for i in kb.index_map()},
        )
        snap["rt_mm"] = [kb.rt_mm(r) for r in snap["rt"]]
    return snap


class Worker:
    """One thread owns the keyboard; jobs queue up and report back on the main loop."""

    def __init__(self):
        self.kb: nuphy.Keyboard | None = None
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, op, done):
        self.jobs.put((op, done))

    def _run(self):
        while True:
            op, done = self.jobs.get()
            try:
                if self.kb is None:
                    self.kb = nuphy.open_keyboard()
                with self.kb:
                    result, error = op(self.kb), None
            except (nuphy.NuphyError, OSError) as e:
                result, error = None, e
                if isinstance(e, OSError):
                    self.kb = None                    # unplugged: look for it again next time
            GLib.idle_add(done, result, error)


# ---------------------------------------------------------------- key drawing

class KeyboardView(Gtk.DrawingArea):
    """The keyboard, drawn from the model's layout. Keys are addressed by data index."""

    def __init__(self, multi: bool = False, on_select=None, unit: int = 46):
        super().__init__()
        self.multi, self.on_select, self.unit = multi, on_select, unit
        self.layout, self.index_map = (), {}
        self.labels: dict[int, str] = {}
        self.sub: dict[int, str] = {}                # small second line
        self.colors: dict[int, tuple] = {}           # fill colour per key
        self.accent: set[int] = set()                # keys drawn as "different from factory"
        self.fill: dict[int, float] = {}             # 0..1 bar (live travel)
        self.selected: set[int] = set()
        self.set_draw_func(self._draw)
        click = Gtk.GestureClick()
        click.connect("pressed", self._click)
        self.add_controller(click)
        self.set_hexpand(True)

    def set_keyboard(self, layout, index_map):
        if layout is self.layout and index_map == self.index_map:
            return
        self.layout, self.index_map = layout, dict(index_map)
        self.selected &= set(index_map)
        w = max((k[0] + k[2] for k in layout), default=1)
        h = max((k[1] + k[3] for k in layout), default=1)
        self.set_content_width(int(w * self.unit) + 8)
        self.set_content_height(int(h * self.unit) + 8)

    def _rect(self, idx):
        x, y, w, h = self.layout[self.index_map[idx]][:4]
        scale = min(1.0, (self.get_width() - 8) / max(1, self.get_content_width() - 8))
        u = self.unit * scale
        return 4 + x * u, 4 + y * u, w * u - 4, h * u - 4, scale

    def _draw(self, _area, cr, _w, _h):
        dark = Adw.StyleManager.get_default().get_dark()
        base = (0.24, 0.24, 0.26) if dark else (0.93, 0.93, 0.94)
        ink = (0.95, 0.95, 0.95) if dark else (0.12, 0.12, 0.12)
        for idx in self.index_map:
            x, y, w, h, scale = self._rect(idx)
            r = 6 * scale
            cr.new_sub_path()
            cr.arc(x + w - r, y + r, r, -1.5708, 0)
            cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
            cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
            cr.arc(x + r, y + r, r, 3.1416, 4.7124)
            cr.close_path()
            rgb = self.colors.get(idx)
            fill = tuple(c / 255 for c in rgb) if rgb else base
            cr.set_source_rgb(*fill)
            cr.fill_preserve()
            if idx in self.selected:
                cr.set_source_rgb(0.21, 0.52, 0.89)
                cr.set_line_width(3)
            elif idx in self.accent:
                cr.set_source_rgb(0.90, 0.38, 0.0)
                cr.set_line_width(2)
            else:
                cr.set_source_rgba(*ink, 0.18)
                cr.set_line_width(1)
            cr.stroke()
            level = self.fill.get(idx)
            if level:
                cr.set_source_rgba(0.21, 0.52, 0.89, 0.55)
                cr.rectangle(x + 2, y + h - 2 - (h - 4) * min(1.0, level), w - 4, (h - 4) * min(1.0, level))
                cr.fill()
            light = rgb is not None and sum(rgb) > 380
            text = (0.1, 0.1, 0.1) if light else ink if rgb is None else (1, 1, 1)
            self._text(cr, self.labels.get(idx, ""), x, y, w, h * (0.62 if idx in self.sub else 1.0), 10 * scale, text)
            if idx in self.sub:
                self._text(cr, self.sub[idx], x, y + h * 0.5, w, h * 0.5, 8 * scale, (*text, 0.75))

    @staticmethod
    def _text(cr, text, x, y, w, h, size, rgb):
        if not text:
            return
        layout = PangoCairo.create_layout(cr)
        layout.set_text(text, -1)
        for _ in range(2):                            # shrink a long legend before cutting it
            layout.set_font_description(Pango.FontDescription(f"Sans {max(5.0, size):.1f}"))
            _, ext = layout.get_pixel_extents()
            if ext.width <= w - 4 or size <= 6.5:
                break
            size = max(6.5, size * (w - 4) / ext.width)
        layout.set_width(int((w - 2) * Pango.SCALE))
        layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_ellipsize(Pango.EllipsizeMode.END)
        _, ext = layout.get_pixel_extents()
        cr.set_source_rgba(*rgb) if len(rgb) == 4 else cr.set_source_rgb(*rgb)
        cr.move_to(x + 1, y + (h - ext.height) / 2)
        PangoCairo.show_layout(cr, layout)

    def _click(self, _gesture, _n, px, py):
        for idx in self.index_map:
            x, y, w, h, _ = self._rect(idx)
            if x <= px <= x + w and y <= py <= y + h:
                if self.multi:
                    self.selected ^= {idx}
                else:
                    self.selected = {idx}
                self.queue_draw()
                if self.on_select:
                    self.on_select(idx)
                return

    def select(self, idxs):
        self.selected = set(idxs) & set(self.index_map)
        self.queue_draw()
        if self.on_select:
            self.on_select(None)


def selection_bar(view: KeyboardView) -> Gtk.Box:
    box = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
    for label, pick in (("Tout", lambda: set(view.index_map)), ("Aucune", set),
                        ("Inverser", lambda: set(view.index_map) - view.selected)):
        b = Gtk.Button(label=label)
        b.connect("clicked", lambda _b, p=pick: view.select(p()))
        box.append(b)
    return box


def scrolled(child) -> Gtk.ScrolledWindow:
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    sw.set_child(child)
    return sw


def page_box(*children) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                  margin_start=12, margin_end=12)
    for c in children:
        box.append(c)
    return box


def combo(title: str, items: list[str], subtitle: str = "") -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, subtitle=subtitle)
    row.set_model(Gtk.StringList.new(items))
    return row


class CodeChooser(Gtk.Button):
    """A button that opens a searchable list of keycodes."""

    def __init__(self, win, codes: list[int], code: int = 0):
        super().__init__(valign=Gtk.Align.CENTER)
        self.win, self.codes, self.code = win, codes, code
        self._show()
        self.connect("clicked", self._open)

    def _show(self):
        self.set_label(nuphy.code_label(self.code) or "(aucune)")

    def set_code(self, code: int):
        self.code = code
        self._show()

    def _open(self, *_):
        pop = Gtk.Popover()
        pop.set_parent(self)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        entry = Gtk.SearchEntry()
        lst = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        rows = []
        for code in self.codes:
            label = Gtk.Label(label=f"{nuphy.code_label(code)}   ({nuphy.code_word(code)})", xalign=0)
            row = Gtk.ListBoxRow()
            row.set_child(label)
            row.code = code
            row.words = (nuphy.code_label(code) + " " + nuphy.code_word(code)).lower()
            lst.append(row)
            rows.append(row)
        lst.set_filter_func(lambda row: entry.get_text().lower() in row.words)
        entry.connect("search-changed", lambda *_: lst.invalidate_filter())

        def picked(_l, row):
            self.set_code(row.code)
            pop.popdown()
        lst.connect("row-activated", picked)
        sw = Gtk.ScrolledWindow(min_content_height=280, min_content_width=260)
        sw.set_child(lst)
        box.append(entry)
        box.append(sw)
        pop.set_child(box)
        pop.popup()


# ------------------------------------------------------------------- window

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Clavier NuPhy", default_width=1040, default_height=820)
        self.worker = Worker()
        self.snap: dict | None = None
        self.mode: int | None = None                 # mode shown; None = follow the keyboard
        self._loading = False
        self._busy = 0
        self._pending: dict = {}
        self._live_stop: threading.Event | None = None

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.title = Adw.WindowTitle(title="Clavier NuPhy", subtitle="lecture…")
        header.set_title_widget(self.title)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Relire le clavier")
        refresh.connect("clicked", lambda *_: self.refresh())
        header.pack_start(refresh)
        self.mode_drop = Gtk.DropDown.new_from_strings(["M1", "M2", "M3"])
        self.mode_drop.set_tooltip_text("Mode réglé ici (le clavier garde trois modes)")
        self.mode_drop.connect("notify::selected", self._mode_picked)
        header.pack_start(self.mode_drop)
        self.spinner = Gtk.Spinner()
        header.pack_end(self.spinner)
        view.add_top_bar(header)

        self.stack = Gtk.Stack()
        self.missing = Adw.StatusPage(icon_name="input-keyboard-symbolic", title="Clavier introuvable",
                                      description="Branche-le en USB, puis réessaie.")
        retry = Gtk.Button(label="Réessayer", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        retry.connect("clicked", lambda *_: self.refresh())
        self.missing.set_child(retry)
        self.stack.add_named(self.missing, "missing")

        self.pages = Gtk.Stack()
        self.tabs = [KeysPage(self), TriggerPage(self), AdvancedPage(self), LightPage(self), MacroPage(self),
                     ModePage(self), ToolsPage(self)]
        for tab in self.tabs:
            self.pages.add_titled(tab.widget, tab.name, tab.title)
        side = Gtk.StackSidebar(stack=self.pages, width_request=170)
        body = Gtk.Box()
        body.append(side)
        body.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.pages.set_hexpand(True)
        body.append(self.pages)
        self.stack.add_named(body, "settings")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)
        self.connect("close-request", self._closing)
        self.refresh()

    # -- device I/O

    def toast(self, text: str, seconds: int = 3):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=seconds))

    def run(self, op, ok: str | None = None, reread: bool = True):
        """Queue `op(kb)`, then read the keyboard back and redraw."""
        if self._live_stop:
            self._live_stop.set()                     # a live view holds the keyboard: end it first
        self._busy += 1
        self.spinner.start()
        self.pages.set_sensitive(False)
        shown = self.mode

        def job(kb):
            if kb.version == 0:
                kb.identify()
            out = op(kb) if op else None
            mode = kb.current_mode() if shown is None else shown
            return out, (read_all(kb, mode, fast=op is None) if reread else None)

        def done(result, error):
            self._busy -= 1
            if not self._busy:
                self.spinner.stop()
                self.pages.set_sensitive(True)
            if error:
                if self.snap is None or isinstance(error, OSError):
                    self.missing.set_description(str(error))
                    self.stack.set_visible_child_name("missing")
                    self.snap = None
                else:
                    self.toast(str(error), 6)
                    if op:
                        self.refresh()                # show what the keyboard really holds
                return False
            _, snap = result
            if snap:
                self.show(snap)
            if ok:
                self.toast(ok, 2)
            return False

        self.worker.submit(job, done)

    def refresh(self):
        self.run(None)

    def show(self, snap: dict):
        self.snap = snap
        self._loading = True
        try:
            self.stack.set_visible_child_name("settings")
            names = snap["mode_names"]
            self.title.set_title(snap["model"])
            self.title.set_subtitle(f"firmware {snap['firmware']} · mode actif M{snap['current_mode'] + 1} « {names[snap['current_mode']]} »")
            self.mode_drop.set_model(Gtk.StringList.new([f"M{i + 1} · {n}" for i, n in enumerate(names)]))
            self.mode_drop.set_selected(snap["mode"])
            for tab in self.tabs:
                tab.update(snap)
        finally:
            self._loading = False

    def _mode_picked(self, drop, _):
        if self._loading or self.snap is None:
            return
        self.mode = drop.get_selected()
        self.refresh()

    def later(self, key, fn, ms: int = 600):
        """Sliders and spin rows fire on every step; wait until the value settles."""
        if key in self._pending:
            GLib.source_remove(self._pending[key])

        def fire():
            self._pending.pop(key, None)
            fn()
            return False
        self._pending[key] = GLib.timeout_add(ms, fire)

    def confirm(self, heading: str, body: str, action: str, then):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Annuler")
        dialog.add_response("go", action)
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, r: then() if r == "go" else None)
        dialog.present(self)

    def layer(self, sub: int) -> int:
        return self.snap["mode"] * nuphy.LAYERS_PER_MODE + sub

    def _closing(self, *_):
        if self._live_stop:
            self._live_stop.set()
        return False


class Page:
    name = title = ""

    def __init__(self, win: Window):
        self.win = win
        self.widget = self.build()

    def build(self):
        raise NotImplementedError

    def update(self, snap: dict):
        raise NotImplementedError

    @property
    def loading(self) -> bool:
        return self.win._loading


# ----------------------------------------------------------------- 1. keys

class KeysPage(Page):
    name, title = "keys", "Touches"

    def build(self):
        self.sub = 0
        self.view = KeyboardView(on_select=self._selected)
        self.layers = Gtk.Box(css_classes=["linked"], halign=Gtk.Align.CENTER)
        self.layer_buttons = []
        for i, name in enumerate(LAYER_NAMES):
            b = Gtk.ToggleButton(label=name, active=i == 0)
            if self.layer_buttons:
                b.set_group(self.layer_buttons[0])
            b.connect("toggled", self._layer_toggled, i)
            self.layers.append(b)
            self.layer_buttons.append(b)
        self.info = Gtk.Label(label="Clique une touche, puis choisis ce qu'elle doit envoyer.", wrap=True)
        tools = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.reset_key = Gtk.Button(label="Touche d'usine", sensitive=False)
        self.reset_key.connect("clicked", self._reset_key)
        reset_layer = Gtk.Button(label="Couche d'usine")
        reset_layer.connect("clicked", self._reset_layer)
        tools.append(self.reset_key)
        tools.append(reset_layer)
        self.picker = Gtk.Notebook(vexpand=True)
        self.macro_flow = None
        return page_box(self.layers, self.view, self.info, tools, self.picker)

    def _fill_picker(self, snap):
        while self.picker.get_n_pages():
            self.picker.remove_page(0)
        lists = nuphy.DATA["lists"]
        groups = [("Basique", [KC[n] for n in lists["basic"]]),
                  ("Média", [KC[n] for n in lists["media"]]),
                  ("Souris et spécial", [KC[n] for n in lists["special"]]),
                  ("Éclairage", [KC[n] for n in lists["light"] + (lists["sidelight"] if "side" in snap["light_modes"] else [])])]
        if snap["gamepad"] is not None:
            groups.append(("Manette", [KC[n] for n in lists["gamepad"] + (lists["xbox"] if snap["xbox"] else [])]))
        seen = set()
        for title, codes in groups:
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=14, homogeneous=False,
                               valign=Gtk.Align.START, row_spacing=4, column_spacing=4, margin_top=6, margin_bottom=6)
            for code in codes:
                if (title, code) in seen:
                    continue
                seen.add((title, code))
                b = Gtk.Button(label=nuphy.code_label(code) or "(rien)", tooltip_text=nuphy.code_word(code))
                if code in nuphy.DANGEROUS:
                    b.add_css_class("destructive-action")
                b.connect("clicked", self._assign, code)
                flow.append(b)
            self.picker.append_page(scrolled(flow), Gtk.Label(label=title))
        self.macro_flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=12,
                                      valign=Gtk.Align.START, row_spacing=4, column_spacing=4, margin_top=6)
        self.picker.append_page(scrolled(self.macro_flow), Gtk.Label(label="Macros"))
        self._codes = groups

    def update(self, snap):
        self.view.set_keyboard(snap["layout"], snap["index_map"])
        if self.picker.get_n_pages() == 0 or (snap["gamepad"] is None) == any(t == "Manette" for t, _ in self._codes):
            self._fill_picker(snap)
        child = self.macro_flow.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.macro_flow.remove(child)
            child = nxt
        for i, acts in enumerate(snap["macros"]):
            if acts:
                b = Gtk.Button(label=f"M{i + 1}", tooltip_text=nuphy.format_macro(acts))
                b.connect("clicked", self._assign_macro, i)
                self.macro_flow.append(b)
        if not any(snap["macros"]):
            self.macro_flow.append(Gtk.Label(label="Aucune macro : crée-en une dans l'onglet Macros."))
        km, df = snap["keymaps"][self.sub], snap["defaults"][self.sub]
        self.view.labels = {i: nuphy.key_label(km[i]) for i in snap["index_map"]}
        self.view.accent = {i for i in snap["index_map"] if km[i] != df[i]}
        self.view.queue_draw()
        self._selected(None)

    def _layer_toggled(self, button, i):
        if button.get_active() and self.win.snap:
            self.sub = i
            self.update(self.win.snap)

    def _key(self):
        return next(iter(self.view.selected), None)

    def _selected(self, _idx):
        idx, snap = self._key(), self.win.snap
        self.reset_key.set_sensitive(idx is not None)
        if idx is None or snap is None:
            self.info.set_label("Clique une touche, puis choisis ce qu'elle doit envoyer. Contour orange : différent de l'usine.")
            return
        k = snap["keymaps"][self.sub][idx]
        self.info.set_label(f"{snap['titles'][idx]} · couche {LAYER_NAMES[self.sub]} : {nuphy.key_label(k) or '(rien)'}"
                            f"   —   usine : {nuphy.key_label(snap['defaults'][self.sub][idx]) or '(rien)'}")

    def _assign(self, _b, code):
        idx = self._key()
        if idx is None:
            self.win.toast("Choisis d'abord une touche sur le clavier")
            return
        layer = self.win.layer(self.sub)

        def go():
            self.win.run(lambda kb: kb.set_key(layer, idx, code), f"{self.win.snap['titles'][idx]} → {nuphy.code_label(code)}")
        if code in nuphy.DANGEROUS:
            self.win.confirm("Assigner cette fonction ?", "Appuyer sur cette touche redémarrera le clavier ou le mettra "
                             "en mode mise à jour du firmware.", "Assigner", go)
        else:
            go()

    def _assign_macro(self, _b, macro_id):
        idx = self._key()
        if idx is None:
            self.win.toast("Choisis d'abord une touche sur le clavier")
            return
        layer = self.win.layer(self.sub)
        self.win.run(lambda kb: kb.bind_macro(layer, idx, macro_id), f"Macro M{macro_id + 1} assignée")

    def _reset_key(self, *_):
        idx = self._key()
        if idx is not None:
            layer = self.win.layer(self.sub)
            self.win.run(lambda kb: kb.reset_key(layer, idx), "Touche d'usine")

    def _reset_layer(self, *_):
        layer = self.win.layer(self.sub)
        self.win.confirm(f"Remettre la couche {LAYER_NAMES[self.sub]} d'usine ?", "Toutes les touches de cette couche "
                         "reprennent leur fonction d'origine.", "Remettre", lambda: self.win.run(lambda kb: kb.reset_layer(layer), "Couche d'usine"))


# -------------------------------------------------------------- 2. trigger

class TriggerPage(Page):
    name, title = "trigger", "Actionnement"

    def build(self):
        self.view = KeyboardView(multi=True, on_select=self._selected)
        g = Adw.PreferencesGroup(title="Réglage des touches sélectionnées",
                                 description="Point d'actionnement, rapid trigger et zones mortes, en millimètres.")
        self.switch_row = combo("Switch", [], "Type de switch monté sous ces touches")
        self.ap = Adw.SpinRow.new_with_range(0.1, 3.3, 0.01)
        self.ap.set_title("Point d'actionnement")
        self.ap.set_subtitle("Enfoncement à partir duquel la touche se déclenche")
        self.ap.set_digits(2)
        self.rt = Adw.SwitchRow(title="Rapid Trigger", subtitle="Relâche et redéclenche selon le mouvement, pas selon un point fixe")
        self.rt.connect("notify::active", lambda *_: self._dim())
        self.cont = Adw.SwitchRow(title="Rapid Trigger continu", subtitle="Reste actif jusqu'au relâchement complet")
        self.press = Adw.SpinRow.new_with_range(0.01, 2.2, 0.01)
        self.press.set_title("Sensibilité à l'appui")
        self.press.set_digits(2)
        self.release = Adw.SpinRow.new_with_range(0.01, 2.2, 0.01)
        self.release.set_title("Sensibilité au relâchement")
        self.release.set_digits(2)
        self.top = Adw.SpinRow.new_with_range(0, 0.5, 0.01)
        self.top.set_title("Zone morte haute")
        self.top.set_digits(2)
        self.bottom = Adw.SpinRow.new_with_range(0, 0.5, 0.01)
        self.bottom.set_title("Zone morte basse")
        self.bottom.set_digits(2)
        for row in (self.switch_row, self.ap, self.rt, self.cont, self.press, self.release, self.top, self.bottom):
            g.add(row)
        self.apply = Gtk.Button(label="Appliquer aux touches sélectionnées", halign=Gtk.Align.END, sensitive=False,
                                css_classes=["suggested-action"])
        self.apply.connect("clicked", self._apply)
        self.hint = Gtk.Label(wrap=True, label="")
        return scrolled(page_box(self.view, selection_bar(self.view), self.hint, g, self.apply))

    def update(self, snap):
        self.view.set_keyboard(snap["layout"], snap["index_map"])
        self.switches = snap["switches"]
        self.switch_row.set_model(Gtk.StringList.new([f"{name} ({total:g} mm)" for _, (name, total, _) in self.switches]))
        mm = snap["rt_mm"]
        self.view.labels = {i: f"{mm[i]['actuation']:.2f}" for i in snap["index_map"]}
        self.view.sub = {i: {"normal": "", "rt": "RT", "rtc": "RT cont."}[mm[i]["mode"]] for i in snap["index_map"]}
        self.view.sub = {i: t for i, t in self.view.sub.items() if t}
        if snap["auto_select"] and not self.view.selected:
            self.view.selected = set(snap["index_map"])
        self.view.queue_draw()
        self._selected(None)

    def _dim(self):
        for row in (self.cont, self.press, self.release):
            row.set_sensitive(self.rt.get_active())

    def _selected(self, _idx):
        snap, sel = self.win.snap, sorted(self.view.selected)
        self.apply.set_sensitive(bool(sel))
        if not snap:
            return
        self.hint.set_label(f"{len(sel)} touche(s) sélectionnée(s)." if sel else
                            "Sélectionne les touches à régler (clic pour ajouter ou retirer).")
        if not sel:
            return
        v = snap["rt_mm"][sel[0]]
        ids = [s for s, _ in self.switches]
        self.switch_row.set_selected(ids.index(v["switch"]) if v["switch"] in ids else 0)
        step = self.switches[self.switch_row.get_selected()][1][2] if self.switches else 0.01
        self.ap.get_adjustment().configure(v["actuation"], 0.1, v["total"], snap["precision"], 0.1, 0)
        for row, key in ((self.press, "press"), (self.release, "release")):
            row.get_adjustment().configure(v[key], step, 2.2, step, 0.1, 0)
        self.rt.set_active(v["mode"] != "normal")
        self.cont.set_active(v["mode"] == "rtc")
        self.top.set_value(v["top_dz"])
        self.bottom.set_value(v["bottom_dz"])
        self._dim()

    def _apply(self, *_):
        snap, sel = self.win.snap, sorted(self.view.selected)
        if not sel:
            return
        switch = self.switches[self.switch_row.get_selected()][0] if self.switches else None
        mode = "normal" if not self.rt.get_active() else "rtc" if self.cont.get_active() else "rt"
        args = dict(switch=switch, actuation=round(self.ap.get_value(), 3), mode=mode,
                    press=round(self.press.get_value(), 3), release=round(self.release.get_value(), 3),
                    top_dz=round(self.top.get_value(), 3), bottom_dz=round(self.bottom.get_value(), 3))
        rts, shown = snap["rt"], snap["mode"]

        def op(kb):
            new = {i: kb.rt_from_mm(rts[i], **args) for i in sel}
            kb.set_rt({i: v for i, v in new.items() if v != rts[i]}, shown)
        self.win.run(op, f"{len(sel)} touche(s) réglée(s)")


# ------------------------------------------------------------- 3. advanced

ADV_KINDS = [("mt", "Mod-Tap", "Un appui bref envoie une touche, un appui maintenu une autre", 1),
             ("toggle", "Bascule", "Un appui bref verrouille la touche enfoncée jusqu'au suivant", 1),
             ("dks", "DKS (4 actions)", "Jusqu'à 4 touches selon la profondeur d'appui et de relâchement", 1),
             ("rs", "Rapid Shift", "De deux touches, celle enfoncée le plus loin l'emporte", 2),
             ("socd", "SOCD", "Règle de priorité entre deux directions opposées", 2),
             ("ht", "Hyper Tap", "Relâcher une touche déclenche l'autre", 2)]
SOCD_RULES = [("last", "La dernière pressée gagne"), ("neutral", "Aucune (neutre)"),
              ("a", "La 1re touche gagne toujours"), ("b", "La 2e touche gagne toujours")]


class AdvancedPage(Page):
    name, title = "advanced", "Touches avancées"

    def build(self):
        self.sub = 0
        self.view = KeyboardView(multi=True, on_select=self._selected)
        self.kind = combo("Type", [k[1] for k in ADV_KINDS])
        self.kind.connect("notify::selected", lambda *_: self._kind_changed())
        self.layer_row = combo("Couche", LAYER_NAMES)
        self.layer_row.connect("notify::selected", lambda *_: self._relabel())
        plain = [KC[n] for n in nuphy.DATA["lists"]["basic"] + nuphy.DATA["lists"]["media"] + nuphy.DATA["lists"]["special"]]
        plain = [c for i, c in enumerate(plain) if c and c not in plain[:i] and c not in nuphy.DANGEROUS]
        keyboard_only = [c for c in plain if nuphy.code_bytes(c)[0] == nuphy.T_KEYBOARD]
        self.form = Adw.PreferencesGroup(title="Nouvelle touche avancée")
        self.form.add(self.kind)
        self.form.add(self.layer_row)
        self.rows = {}

        def code_row(key, title, codes, default):
            row = Adw.ActionRow(title=title)
            chooser = CodeChooser(self.win, codes, default)
            row.add_suffix(chooser)
            self.form.add(row)
            self.rows[key] = (row, chooser)
        code_row("tap", "Appui bref", plain, KC["KC_ESC"])
        code_row("hold", "Appui maintenu", plain, KC["KC_LCTL"])
        self.ms = Adw.SpinRow.new_with_range(10, 1000, 10)
        self.ms.set_title("Délai de maintien (ms)")
        self.ms.set_value(170)
        self.form.add(self.ms)
        code_row("toggle", "Touche à verrouiller", plain, KC["KC_LSFT"])
        self.points = []
        for title, default in (("Début d'appui (mm)", 1.0), ("Fond d'appui (mm)", 3.0),
                               ("Relâchement depuis le fond (mm)", 3.0), ("Relâchement complet (mm)", 1.0)):
            row = Adw.SpinRow.new_with_range(0.1, 4.0, 0.1)
            row.set_title(title)
            row.set_digits(1)
            row.set_value(default)
            self.form.add(row)
            self.points.append(row)
        self.dks = []
        for n in range(4):
            row = Adw.ActionRow(title=f"Action {n + 1}", subtitle="4 phases : t bref, h tenue, r relâchée, . rien")
            chooser = CodeChooser(self.win, [0] + keyboard_only, 0)
            phases = Gtk.Entry(text="t..." if n == 0 else "....", max_length=4, width_chars=5, valign=Gtk.Align.CENTER)
            row.add_suffix(chooser)
            row.add_suffix(phases)
            self.form.add(row)
            self.dks.append((row, chooser, phases))
        self.rule = combo("Priorité", [r[1] for r in SOCD_RULES])
        self.form.add(self.rule)
        self.both = Adw.SwitchRow(title="Dans les deux sens", active=True, subtitle="Sinon seule la 1re touche déclenche la 2e")
        self.form.add(self.both)
        self.travel = Adw.SpinRow.new_with_range(0.1, 2.0, 0.05)
        self.travel.set_title("Course de relâchement (mm)")
        self.travel.set_digits(2)
        self.travel.set_value(0.5)
        self.form.add(self.travel)
        self.create = Gtk.Button(label="Créer", halign=Gtk.Align.END, css_classes=["suggested-action"], sensitive=False)
        self.create.connect("clicked", self._create)
        self.hint = Gtk.Label(wrap=True)
        self.list = Adw.PreferencesGroup(title="Touches avancées de ce mode")
        self.list_rows = []
        self._kind_changed()
        return scrolled(page_box(self.view, self.hint, self.form, self.create, self.list))

    def _need(self) -> int:
        return ADV_KINDS[self.kind.get_selected()][3]

    def _kind_changed(self):
        kind = ADV_KINDS[self.kind.get_selected()]
        self.kind.set_subtitle(kind[2])
        show = {"mt": ["tap", "hold", "ms"], "toggle": ["toggle"], "dks": ["points", "dks"], "rs": [],
                "socd": ["rule"], "ht": ["both", "travel"]}[kind[0]]
        for key, (row, _) in self.rows.items():
            row.set_visible(key in show)
        self.ms.set_visible("ms" in show)
        for row in self.points:
            row.set_visible("points" in show)
        for row, _, _ in self.dks:
            row.set_visible("dks" in show)
        self.rule.set_visible("rule" in show)
        self.both.set_visible("both" in show)
        self.travel.set_visible("travel" in show)
        self._selected(None)

    def _relabel(self):
        if self.win.snap:
            self.sub = self.layer_row.get_selected()
            km = self.win.snap["keymaps"][self.sub]
            self.view.labels = {i: nuphy.key_label(km[i]) for i in self.win.snap["index_map"]}
            self.view.accent = {i for i in self.win.snap["index_map"] if km[i][0] in nuphy.ADVANCED}
            self.view.queue_draw()

    def update(self, snap):
        self.view.set_keyboard(snap["layout"], snap["index_map"])
        self._relabel()
        for row in self.list_rows:
            self.list.remove(row)
        self.list_rows = []
        t = snap["titles"]
        for it in snap["advanced"]:
            where = f"{t[it['index']]} · couche {LAYER_NAMES[it['layer']]}"
            if it["kind"] == "MT":
                what = f"bref {nuphy.code_label(it['tap'])}, maintenu {nuphy.code_label(it['hold'])}, {it['ms']} ms"
            elif it["kind"] == "TGL":
                what = f"verrouille {nuphy.code_label(it['key'])}"
            elif it["kind"] == "DKS":
                d = it["dks"]
                what = "vide" if d is None else "points " + "/".join(f"{p / 10:g}" for p in d.points) + " mm · " + ", ".join(
                    f"{nuphy.code_label(k)} {r}" for k, r in zip(d.keys, d.phases) if k)
            else:
                what = f"avec {t.get(it['other'], '?')}" + (f", {it['travel']:g} mm" if it["kind"] == "HT" else "")
            row = Adw.ActionRow(title=f"{it['kind']} · {where}", subtitle=what)
            b = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Supprimer")
            b.connect("clicked", self._remove, it)
            row.add_suffix(b)
            self.list.add(row)
            self.list_rows.append(row)
        if not snap["advanced"]:
            row = Adw.ActionRow(title="Aucune pour l'instant")
            self.list.add(row)
            self.list_rows.append(row)
        self._selected(None)

    def _selected(self, _idx):
        need, n = self._need(), len(self.view.selected)
        self.hint.set_label(f"Sélectionne {'une touche' if need == 1 else 'deux touches'} sur le clavier ({n} choisie(s)).")
        self.create.set_sensitive(n == need)

    def _create(self, *_):
        kind = ADV_KINDS[self.kind.get_selected()][0]
        sel = sorted(self.view.selected)
        layer = self.win.layer(self.layer_row.get_selected())
        if kind == "mt":
            tap, hold, ms = self.rows["tap"][1].code, self.rows["hold"][1].code, int(self.ms.get_value())
            op = lambda kb: kb.set_mod_tap(layer, sel[0], tap, hold, ms)  # noqa: E731
        elif kind == "toggle":
            code = self.rows["toggle"][1].code
            op = lambda kb: kb.set_toggle(layer, sel[0], code)  # noqa: E731
        elif kind == "dks":
            d = nuphy.Dks([round(r.get_value() * 10) for r in self.points], [c.code for _, c, _ in self.dks],
                          [(p.get_text() or "....").ljust(4, ".")[:4] for _, _, p in self.dks])
            op = lambda kb: kb.set_dks(layer, sel[0], d)  # noqa: E731
        elif kind == "rs":
            op = lambda kb: kb.set_rapid_shift(layer, sel[0], sel[1])  # noqa: E731
        elif kind == "socd":
            rule = SOCD_RULES[self.rule.get_selected()][0]
            op = lambda kb: kb.set_socd(layer, sel[0], sel[1], rule)  # noqa: E731
        else:
            both, travel = self.both.get_active(), self.travel.get_value()
            op = lambda kb: kb.set_hyper_tap(layer, sel[0], sel[1], both, travel)  # noqa: E731
        self.view.selected = set()
        self.win.run(op, "Touche avancée créée")

    def _remove(self, _b, it):
        layer = self.win.layer(it["layer"])
        self.win.run(lambda kb: kb.reset_key(layer, it["index"]), "Touche avancée supprimée")


# ------------------------------------------------------------- 4. lighting

ZONE_TITLES = {"main": "Rétroéclairage des touches", "side": "Lumière latérale", "loop": "Halo"}


class LightPage(Page):
    name, title = "light", "Éclairage"

    def build(self):
        self.box = page_box()
        self.zones: dict[str, dict] = {}
        self.view = KeyboardView(multi=True)
        self.key_color = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        self.key_color.set_rgba(self._rgba("#ff0000"))
        self.custom = Adw.PreferencesGroup(title="Couleur par touche",
                                           description="Visible avec l'effet « Custom » du rétroéclairage.")
        row = Adw.ActionRow(title="Couleur à appliquer")
        row.add_suffix(self.key_color)
        paint = Gtk.Button(label="Peindre la sélection", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        paint.connect("clicked", self._paint)
        row.add_suffix(paint)
        self.custom.add(row)
        self.built_for = None
        return scrolled(self.box)

    @staticmethod
    def _rgba(text):
        c = Gdk.RGBA()
        c.parse(text)
        return c

    def _build_zones(self, snap):
        child = self.box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.box.remove(child)
            child = nxt
        self.zones = {}
        for kind, modes in snap["light_modes"].items():
            g = Adw.PreferencesGroup(title=ZONE_TITLES[kind])
            z = {"modes": modes}
            z["on"] = Adw.SwitchRow(title="Allumé")
            z["on"].connect("notify::active", self._changed, kind, "on")
            z["effect"] = combo("Effet", [m[1] for m in modes])
            z["effect"].connect("notify::selected", self._changed, kind, "effect")
            z["brightness"] = Adw.SpinRow.new_with_range(0, 100, 5)
            z["brightness"].set_title("Luminosité (%)")
            z["brightness"].connect("notify::value", self._changed, kind, "brightness")
            z["speed"] = Adw.SpinRow.new_with_range(0, 4, 1)
            z["speed"].set_title("Vitesse")
            z["speed"].connect("notify::value", self._changed, kind, "speed")
            z["direction"] = Adw.SwitchRow(title="Sens inversé")
            z["direction"].connect("notify::active", self._changed, kind, "direction")
            z["rainbow"] = Adw.SwitchRow(title="Arc-en-ciel", subtitle="Sinon la couleur choisie ci-dessous")
            z["rainbow"].connect("notify::active", self._changed, kind, "rainbow")
            z["color"] = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
            z["color"].connect("notify::rgba", self._changed, kind, "color")
            crow = Adw.ActionRow(title="Couleur")
            presets = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
            for hexa in nuphy.DATA["presets"]:
                b = Gtk.Button(width_request=20, height_request=20, tooltip_text=hexa)
                css = Gtk.CssProvider()
                css.load_from_data(f"button {{ background: {hexa}; min-width: 18px; min-height: 18px; padding: 0; }}".encode())
                b.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
                b.connect("clicked", lambda _b, h=hexa, zz=z: zz["color"].set_rgba(self._rgba(h)))
                presets.append(b)
            crow.add_suffix(presets)
            crow.add_suffix(z["color"])
            z["color_row"] = crow
            for key in ("on", "effect", "brightness", "speed", "direction", "rainbow"):
                g.add(z[key])
            g.add(crow)
            self.box.append(g)
            self.zones[kind] = z
        self.box.append(self.custom)
        self.box.append(self.view)
        self.box.append(selection_bar(self.view))
        self.built_for = snap["model"]

    def update(self, snap):
        if self.built_for != snap["model"]:
            self._build_zones(snap)
        self.view.set_keyboard(snap["layout"], snap["index_map"])
        self.view.labels = {i: snap["titles"][i] for i in snap["index_map"]}
        self.view.colors = {i: snap["colors"][i] for i in snap["index_map"]}
        self.view.queue_draw()
        for kind, z in self.zones.items():
            v = snap["lights"][kind]
            ids = [m[0] for m in z["modes"]]
            z["on"].set_active(v["brightness"] > 0 and v["effect"] != nuphy.LIGHT_OFF)
            if v["effect"] in ids:
                z["effect"].set_selected(ids.index(v["effect"]))
            perms = z["modes"][ids.index(v["effect"])][2] if v["effect"] in ids else []
            z["brightness"].set_value(v["brightness"])
            z["speed"].set_value(v["speed"])
            z["speed"].set_sensitive("speed" in perms)
            z["direction"].set_visible(kind == "main")
            z["direction"].set_sensitive("lr" in perms or "ud" in perms)
            z["direction"].set_active(bool(v["direction"]))
            z["rainbow"].set_active(v["rainbow"])
            z["rainbow"].set_sensitive("color" in perms)
            z["color"].set_rgba(self._rgba(v["color"]))
            z["color_row"].set_sensitive("color" in perms and not v["rainbow"])

    def _changed(self, widget, _param, kind, field):
        if self.loading or not self.win.snap:
            return
        z, shown, cur = self.zones[kind], self.win.snap["mode"], self.win.snap["lights"][kind]
        if field == "on":
            level = (cur["brightness"] or 40) if widget.get_active() else 0
            change = {"brightness": level if widget.get_active() else 0}
            if widget.get_active() and cur["effect"] == nuphy.LIGHT_OFF:
                change["effect"] = z["modes"][0][0]
        elif field == "effect":
            mode = z["modes"][widget.get_selected()]
            change = {"effect": mode[0]}
            if "color" not in mode[2]:
                change["rainbow"] = True              # an effect without colour control runs in rainbow
        elif field == "brightness":
            change = {"brightness": int(widget.get_value())}
        elif field == "speed":
            change = {"speed": int(widget.get_value())}
        elif field == "direction":
            change = {"direction": 1 if widget.get_active() else 0}
        elif field == "rainbow":
            change = {"rainbow": widget.get_active()}
        else:
            c = widget.get_rgba()
            change = {"color": "#%02x%02x%02x" % tuple(round(v * 255) for v in (c.red, c.green, c.blue)), "rainbow": False}
        if all(cur.get(k) == v for k, v in change.items()):
            return
        self.win.later(("light", kind, field), lambda: self.win.run(lambda kb: kb.set_light(kind, shown, **change),
                                                                    "Éclairage réglé"), 350)

    def _paint(self, *_):
        sel, shown = sorted(self.view.selected), self.win.snap["mode"]
        if not sel:
            self.win.toast("Sélectionne des touches sur le clavier ci-dessous")
            return
        c = self.key_color.get_rgba()
        rgb = tuple(round(v * 255) for v in (c.red, c.green, c.blue))
        custom = self.win.snap["lights"]["main"]["effect"] == 0

        def op(kb):
            kb.set_key_colors({i: rgb for i in sel}, shown)
            if not custom:
                kb.set_light("main", shown, effect=0)
        self.win.run(op, f"{len(sel)} touche(s) peinte(s)" + ("" if custom else " · effet Custom activé"))


# --------------------------------------------------------------- 5. macros

class MacroPage(Page):
    name, title = "macros", "Macros"

    def build(self):
        self.recording = False
        g = Adw.PreferencesGroup(title="Macro", description="Écris la suite d'actions ou enregistre-la : +touche appuie, "
                                 "-touche relâche, touche seule = appui bref, un nombre = délai en ms après l'action.")
        self.which = combo("Emplacement", [f"M{i + 1}" for i in range(nuphy.MACROS)])
        self.which.connect("notify::selected", lambda *_: self._load())
        self.entry = Adw.EntryRow(title="Actions (ex. +lctl +c 20 -c -lctl)")
        g.add(self.which)
        g.add(self.entry)
        buttons = Gtk.Box(spacing=6, halign=Gtk.Align.END)
        self.rec = Gtk.ToggleButton(label="Enregistrer au clavier")
        self.rec.connect("toggled", self._record)
        save = Gtk.Button(label="Sauvegarder", css_classes=["suggested-action"])
        save.connect("clicked", self._save)
        delete = Gtk.Button(label="Supprimer")
        delete.connect("clicked", self._delete)
        for b in (self.rec, delete, save):
            buttons.append(b)
        self.usage = Gtk.Label(xalign=0)
        bind = Adw.PreferencesGroup(title="Assigner à une touche", description="Choisis la touche sur le clavier ci-dessous.")
        self.layer_row = combo("Couche", LAYER_NAMES)
        self.loop = combo("Répétition", ["Une fois", "Nombre de fois", "Tant que la touche est tenue", "Jusqu'au prochain appui"])
        self.count = Adw.SpinRow.new_with_range(2, 254, 1)
        self.count.set_title("Nombre de répétitions")
        self.loop.connect("notify::selected", lambda *_: self.count.set_visible(self.loop.get_selected() == 1))
        self.count.set_visible(False)
        for row in (self.layer_row, self.loop, self.count):
            bind.add(row)
        self.view = KeyboardView()
        assign = Gtk.Button(label="Assigner la macro à la touche", halign=Gtk.Align.END)
        assign.connect("clicked", self._assign)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key, True)
        keys.connect("key-released", self._key, False)
        box = page_box(g, buttons, self.usage, bind, self.view, assign)
        box.add_controller(keys)
        self.box = box
        return scrolled(box)

    def update(self, snap):
        self.view.set_keyboard(snap["layout"], snap["index_map"])
        km = snap["keymaps"][self.layer_row.get_selected()]
        self.view.labels = {i: nuphy.key_label(km[i]) for i in snap["index_map"]}
        self.view.accent = {i for i in snap["index_map"] if km[i][0] == nuphy.T_MACRO}
        self.view.queue_draw()
        used = sum(len(m) for m in snap["macros"])
        self.usage.set_label(f"{sum(1 for m in snap['macros'] if m)} macro(s) · {used} action(s) sur "
                             f"{(nuphy.SZ_MACRO - nuphy.MACRO_TABLE) // 4} dans ce mode")
        self.which.set_model(Gtk.StringList.new([f"M{i + 1}" + ("  ·  " + nuphy.format_macro(m)[:40] if m else "")
                                                 for i, m in enumerate(snap["macros"])]))
        self.loop.set_sensitive(snap["macro_loop"])
        if not snap["macro_loop"]:
            self.loop.set_subtitle("Demande un firmware plus récent")
        self._load()

    def _load(self):
        if self.win.snap and not self.recording:
            m = self.win.snap["macros"][min(self.which.get_selected(), nuphy.MACROS - 1)]
            self.entry.set_text(nuphy.format_macro(m))

    def _record(self, button):
        self.recording = button.get_active()
        if self.recording:
            self.entry.set_text("")
            self._last = None
            self.box.grab_focus()
            button.set_label("Arrêter l'enregistrement")
        else:
            button.set_label("Enregistrer au clavier")

    def _key(self, _ctl, _keyval, keycode, _state, press):
        if not self.recording:
            return False
        name = EVDEV.get(keycode - 8)
        if name is None:
            return True
        now = time.monotonic()
        words = self.entry.get_text().split()
        if self._last is not None and words:
            words.append(str(max(1, min(65535, round((now - self._last) * 1000)))))
        self._last = now
        words.append(("+" if press else "-") + name.lower())
        self.entry.set_text(" ".join(words))
        return True

    def _save(self, *_):
        self.rec.set_active(False)
        macro_id, shown = self.which.get_selected(), self.win.snap["mode"]
        try:
            acts = nuphy.parse_macro(self.entry.get_text())
        except nuphy.NuphyError as e:
            self.win.toast(str(e), 5)
            return
        self.win.run(lambda kb: kb.set_macro(macro_id, acts, shown), f"Macro M{macro_id + 1} sauvegardée")

    def _delete(self, *_):
        macro_id, shown = self.which.get_selected(), self.win.snap["mode"]
        self.win.run(lambda kb: kb.set_macro(macro_id, [], shown), f"Macro M{macro_id + 1} supprimée")

    def _assign(self, *_):
        idx = next(iter(self.view.selected), None)
        macro_id = self.which.get_selected()
        if idx is None:
            self.win.toast("Choisis d'abord une touche sur le clavier")
            return
        if not self.win.snap["macros"][macro_id]:
            self.win.toast("Cette macro est vide : sauvegarde-la d'abord")
            return
        loop = [1, int(self.count.get_value()), nuphy.MACRO_HOLD, nuphy.MACRO_TOGGLE][self.loop.get_selected()]
        layer = self.win.layer(self.layer_row.get_selected())
        self.win.run(lambda kb: kb.bind_macro(layer, idx, macro_id, loop), f"Macro M{macro_id + 1} assignée")


# ----------------------------------------------------------------- 6. mode

class ModePage(Page):
    name, title = "mode", "Mode et réglages"

    def build(self):
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Mode", description="Le clavier garde trois modes indépendants ; "
                                 "choisis en haut à gauche celui que tu règles.")
        self.name_row = Adw.EntryRow(title="Nom du mode", show_apply_button=True)
        self.name_row.connect("apply", self._rename)
        g.add(self.name_row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Performance")
        self.rate = combo("Polling USB", [f"{hz} Hz" for hz in sorted(nuphy.RATES.values())], RATE_HINT)
        self.rate.connect("notify::selected", self._rate)
        self.tick = combo("Tick rate", ["Aucun", "30", "60"], "À baisser si un PC lent rate des appuis à haut polling")
        self.tick.connect("notify::selected", lambda r, _: self._set(lambda kb, m, v=[0, 30, 60][r.get_selected()]: kb.set_tick(v, m)))
        self.stab = combo("Anti-rebond", ["Faible", "Moyen", "Fort"], "Plus haut si une touche se double, au prix d'un léger délai")
        self.stab.connect("notify::selected", lambda r, _: self._set(
            lambda kb, m, v=["low", "mid", "high"][r.get_selected()]: kb.set_stabilization(v, m)))
        self.tachyon = Adw.SwitchRow(title="Mode Tachyon", subtitle="Latence minimale : toutes les LED s'éteignent")
        self.tachyon.connect("notify::active", lambda r, _: self._set(lambda kb, m, v=r.get_active(): kb.set_tachyon(v, m)))
        for row in (self.rate, self.tick, self.stab, self.tachyon):
            g.add(row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Verrous", description="Raccourcis bloqués dans ce mode")
        self.locks = {}
        for key, title in (("lock_win", "Touche Windows / Super"), ("lock_alt_tab", "Alt+Tab"), ("lock_alt_f4", "Alt+F4")):
            row = Adw.SwitchRow(title=title)
            row.connect("notify::active", lambda r, _, k=key: self._set(lambda kb, m, v=r.get_active(): kb.set_flag(k, v, m)))
            g.add(row)
            self.locks[key] = row
        page.add(g)

        g = Adw.PreferencesGroup(title="Clavier", description="Réglages communs aux trois modes")
        self.sleep = Adw.SwitchRow(title="Veille automatique")
        self.sleep.connect("notify::active", self._sleep)
        self.sleep_min = Adw.SpinRow.new_with_range(5, 127, 1)
        self.sleep_min.set_title("Délai de veille (minutes)")
        self.sleep_min.connect("notify::value", lambda *_: None if self.loading else self.win.later("sleep", self._sleep_now, 800))
        self.auto = Adw.SwitchRow(title="Tout sélectionner dans Actionnement",
                                  subtitle="Pratique quand toutes les touches ont le même switch")
        self.auto.connect("notify::active", lambda r, _: None if self.loading else self.win.run(
            lambda kb, v=r.get_active(): kb.set_auto_select(v)))
        for row in (self.sleep, self.sleep_min, self.auto):
            g.add(row)
        page.add(g)

        self.pad = Adw.PreferencesGroup(title="Manette virtuelle",
                                        description="Le clavier se présente aussi comme une manette ; assigne ses boutons "
                                        "dans l'onglet Touches, catégorie Manette.")
        self.pad_type = combo("Type", ["Désactivée", "Classique"])
        self.pad_type.connect("notify::selected", lambda r, _: self._pad(type=["off", "classic", "xbox"][r.get_selected()]))
        self.pad.add(self.pad_type)
        self.pad_flags = {}
        for key, title, sub in (("keyboard_keys", "Garder les touches clavier", "Les touches assignées tapent aussi leur caractère"),
                                ("overlay", "Masquer les touches assignées", "Une touche liée à la manette n'envoie plus sa lettre"),
                                ("angle_adjust", "Réglage de l'angle diagonal", ""),
                                ("square_gate", "Stick à butée carrée", "Valeur maximale sur chaque axe en diagonale"),
                                ("extreme_value", "Priorité à la valeur extrême", "Deux directions opposées : la plus enfoncée gagne")):
            row = Adw.SwitchRow(title=title, subtitle=sub)
            row.connect("notify::active", lambda r, _, k=key: self._pad(**{k: r.get_active()}))
            self.pad.add(row)
            self.pad_flags[key] = row
        self.pad_angle = Adw.SpinRow.new_with_range(0, 90, 1)
        self.pad_angle.set_title("Angle diagonal (°)")
        self.pad_angle.connect("notify::value", lambda r, _: None if self.loading else self.win.later(
            "angle", lambda: self._pad(angle=int(r.get_value()))))
        self.pad.add(self.pad_angle)
        page.add(self.pad)

        g = Adw.PreferencesGroup(title="Fichiers et remise à zéro", description="Même format que le driver web : "
                                 "un export fait ici s'importe là-bas, et l'inverse.")
        for title, sub, label, handler, css in (
                ("Exporter ce mode", "Touches, actionnement, éclairage, macros…", "Exporter…", self._export, []),
                ("Importer dans ce mode", "Garde le polling et le type de switch du clavier", "Importer…", self._import, []),
                ("Remettre ce mode d'usine", "Touches, touches avancées, macros et nom", "Remettre", self._reset_mode,
                 ["destructive-action"]),
                ("Remettre tout le clavier d'usine", "Les trois modes, tous les réglages", "Réinitialiser", self._factory,
                 ["destructive-action"])):
            row = Adw.ActionRow(title=title, subtitle=sub)
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER, css_classes=css)
            b.connect("clicked", handler)
            row.add_suffix(b)
            g.add(row)
        page.add(g)
        return page

    def update(self, snap):
        self.name_row.set_text(snap["mode_names"][snap["mode"]])
        rates = sorted(nuphy.RATES.values())
        if snap["rate"] in rates:
            self.rate.set_selected(rates.index(snap["rate"]))
        self.tick.set_selected({0: 0, 30: 1, 60: 2}[snap["tick"]])
        self.stab.set_selected(["low", "mid", "high"].index(snap["stabilization"]))
        self.tachyon.set_active(snap["tachyon"])
        for key, row in self.locks.items():
            row.set_active(snap[key])
        self.sleep.set_active(snap["sleep"] > 0)
        self.sleep_min.set_value(max(5, snap["sleep_minutes"]))
        self.sleep_min.set_sensitive(snap["sleep"] > 0)
        self.auto.set_active(snap["auto_select"])
        pad = snap["gamepad"]
        self.pad.set_visible(pad is not None)
        if pad:
            self.pad_type.set_model(Gtk.StringList.new(["Désactivée", "Classique"] + (["Xbox"] if snap["xbox"] else [])))
            self.pad_type.set_selected(min(["off", "classic", "xbox"].index(pad["type"]), 2 if snap["xbox"] else 1))
            for key, row in self.pad_flags.items():
                row.set_active(pad[key])
            self.pad_angle.set_value(pad["angle"])

    def _set(self, call, ok="Réglage enregistré"):
        if self.loading or not self.win.snap:
            return
        shown = self.win.snap["mode"]
        self.win.run(lambda kb: call(kb, shown), ok)

    def _rate(self, row, _):
        if self.loading or not self.win.snap:
            return
        hz, shown = sorted(nuphy.RATES.values())[row.get_selected()], self.win.snap["mode"]

        def op(kb):
            kb.set_rate(hz, shown)
            if kb.node.path != "demo":
                time.sleep(2.5)                       # it leaves the bus and comes back
        self.win.run(op, f"Polling : {hz} Hz", reread=False)
        GLib.timeout_add(300 if os.environ.get("NUPHY_DEMO") else 3500, lambda: (self._after_rate(), False)[1])

    def _after_rate(self):
        self.win.worker.kb = None                     # the node may have been renumbered
        self.win.refresh()

    def _sleep(self, row, _):
        if self.loading:
            return
        self.sleep_min.set_sensitive(row.get_active())
        self._sleep_now()

    def _sleep_now(self):
        minutes = int(self.sleep_min.get_value()) if self.sleep.get_active() else 0
        self.win.run(lambda kb: kb.set_sleep(minutes), "Veille : " + (f"{minutes} min" if minutes else "jamais"))

    def _pad(self, **change):
        if self.loading or not self.win.snap:
            return
        shown = self.win.snap["mode"]
        self.win.run(lambda kb: kb.set_gamepad(shown, **change), "Manette réglée")

    def _rename(self, entry):
        name, shown = entry.get_text(), self.win.snap["mode"]
        self.win.run(lambda kb: kb.set_mode_name(shown, name), "Mode renommé")

    def _export(self, *_):
        snap = self.win.snap
        dialog = Gtk.FileDialog(title="Exporter le mode",
                                initial_name=f"{snap['model'].replace(' ', '_')}_M{snap['mode'] + 1}.json")

        def chosen(d, res):
            try:
                path = d.save_finish(res).get_path()
            except GLib.Error:
                return
            shown = snap["mode"]

            def op(kb):
                data = {"cfg": kb.export_mode(shown), "deviceName": kb.model.name.replace(" ", "_")}
                with open(path, "w") as f:
                    json.dump(data, f)
            self.win.run(op, f"Exporté : {os.path.basename(path)}", reread=False)
        dialog.save(self.win, None, chosen)

    def _import(self, *_):
        dialog = Gtk.FileDialog(title="Importer un mode")

        def chosen(d, res):
            try:
                path = d.open_finish(res).get_path()
            except GLib.Error:
                return
            shown = self.win.snap["mode"]

            def op(kb):
                try:
                    with open(path) as f:
                        cfg = json.load(f).get("cfg")
                except (ValueError, AttributeError, UnicodeDecodeError):
                    raise nuphy.NuphyError("ce fichier n'est pas un export du driver") from None
                if isinstance(cfg, list):
                    cfg = cfg[shown] if shown < len(cfg) else None
                if not isinstance(cfg, dict):
                    raise nuphy.NuphyError("ce fichier n'est pas un export du driver")
                kb.import_mode(cfg, shown)
            self.win.confirm("Importer dans ce mode ?", "Les réglages du mode affiché seront remplacés par ceux du fichier.",
                             "Importer", lambda: self.win.run(op, "Mode importé"))
        dialog.open(self.win, None, chosen)

    def _reset_mode(self, *_):
        shown = self.win.snap["mode"]
        self.win.confirm(f"Remettre le mode M{shown + 1} d'usine ?", "Ses touches, touches avancées, macros et son nom "
                         "reviennent à l'état d'origine.", "Remettre", lambda: self.win.run(lambda kb: kb.reset_mode(shown), "Mode remis d'usine"))

    def _factory(self, *_):
        self.win.confirm("Réinitialiser tout le clavier ?", "Les trois modes et tous les réglages reviennent à l'état "
                         "d'usine. Une sauvegarde de l'état d'origine se trouve dans " + str(nuphy.STATE_DIR) + ".",
                         "Réinitialiser", lambda: self.win.run(lambda kb: kb.factory_reset(), "Clavier remis d'usine"))


# ---------------------------------------------------------------- 7. tools

class ToolsPage(Page):
    name, title = "tools", "Outils"

    def build(self):
        self.view = KeyboardView()
        g = Adw.PreferencesGroup(title="Course en direct et calibration")
        row = Adw.ActionRow(title="Test des touches", subtitle="Affiche l'enfoncement de chaque touche en temps réel")
        self.live = Gtk.ToggleButton(label="Démarrer", valign=Gtk.Align.CENTER)
        self.live.connect("toggled", self._toggle, "live")
        row.add_suffix(self.live)
        g.add(row)
        row = Adw.ActionRow(title="Recalibrer les switches", subtitle="Clavier à plat, loin du métal : enfonce à fond chaque "
                            "touche plusieurs fois. Les touches calibrées passent en vert.")
        self.cal = Gtk.ToggleButton(label="Démarrer", valign=Gtk.Align.CENTER)
        self.cal.connect("toggled", self._toggle, "cal")
        row.add_suffix(self.cal)
        g.add(row)
        self.status = Gtk.Label(label="", wrap=True)
        about = Adw.PreferencesGroup(title="À propos")
        self.about = Adw.ActionRow(title="Clavier")
        about.add(self.about)
        row = Adw.ActionRow(title="Sauvegarde d'origine", subtitle=str(nuphy.STATE_DIR))
        about.add(row)
        row = Adw.ActionRow(title="Mise à jour du firmware", subtitle="Volontairement absente : elle reste dans le driver web "
                            "(drive.nuphy.io), seul endroit où NuPhy la valide.")
        about.add(row)
        return scrolled(page_box(g, self.status, self.view, about))

    def update(self, snap):
        self.view.set_keyboard(snap["layout"], snap["index_map"])
        self.view.labels = {i: snap["titles"][i] for i in snap["index_map"]}
        self.view.queue_draw()
        self.about.set_subtitle(f"{snap['model']} · firmware {snap['firmware']} · {snap['build']}")

    def _toggle(self, button, what):
        other = self.cal if what == "live" else self.live
        if button.get_active():
            other.set_sensitive(False)
            button.set_label("Arrêter")
            self._start(what)
        else:
            if self.win._live_stop:
                self.win._live_stop.set()
            other.set_sensitive(True)
            button.set_label("Démarrer")

    def _start(self, what):
        stop = self.win._live_stop = threading.Event()
        total = len(self.view.index_map)
        self.view.fill, self.view.colors = {}, {}
        self.status.set_label("Appuie sur les touches…")
        done_keys: set[int] = set()

        def show(events):
            for ev in events:
                if ev["event"] != "key" or ev["index"] is None:
                    continue
                i = ev["index"]
                if what == "cal":
                    if ev["state"] == "done":
                        done_keys.add(i)
                        self.view.colors[i] = (46, 160, 67)
                    elif ev["state"] == "calibrating":
                        self.view.colors.setdefault(i, (200, 120, 0))
                    self.status.set_label(f"{len(done_keys)}/{total} touches calibrées")
                else:
                    span = self.win.snap["rt_mm"][i]["total"] if self.win.snap else 3.3
                    self.view.fill[i] = ev["travel"] / span
                    self.view.sub[i] = f"{ev['travel']:.2f}"
            self.view.queue_draw()
            return False

        def op(kb):
            kb.calibration_start() if what == "cal" else kb.live(True)
            beat = time.monotonic()
            try:
                while not stop.is_set():
                    events = kb.poll(0.05)
                    if events:
                        GLib.idle_add(show, events)
                    if what == "cal" and time.monotonic() - beat > 2:
                        kb.calibration_start()         # the firmware leaves calibration without this
                        beat = time.monotonic()
            finally:
                kb.calibration_end() if what == "cal" else kb.live(False)

        def finished(_result, error):
            self.win._live_stop = None
            self.view.fill, self.view.sub = {}, {}
            self.view.colors = {}
            self.view.queue_draw()
            (self.cal if what == "cal" else self.live).set_active(False)
            if error:
                self.win.toast(str(error), 6)
            elif what == "cal":
                self.win.toast(f"Calibration terminée : {len(done_keys)}/{total} touches")
            self.status.set_label("")
            return False
        self.win.worker.submit(op, finished)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.mathieurdg.NuphyHE", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.get_active_window() or Window(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
