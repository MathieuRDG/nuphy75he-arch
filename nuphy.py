#!/usr/bin/env python3
"""NuPhy Hall-effect keyboards (Air75 HE and its siblings) on Linux, without the web driver.

Standard library only. The protocol comes from NuPhy's own NuPhyIO web driver
(drive.nuphy.io, bundle main.ba072d08.js), read statically; its built-in demo
keyboard ("virtual Air75 HE", real firmware 1.0.6 captures) is what the tests
replay. Nothing here has met a physical keyboard yet: see the README.

Transport: the keyboard's second HID interface carries one vendor collection
(usage page 1, usage 0, no report id) with 64-byte input and output reports.

    request  55 cmd 00 sum len addr_lo addr_hi 00 data...   (sum = bytes 4..63)
    reply    aa cmd 00 sum len addr_lo addr_hi 00 data...

Every setting lives in a memory region addressed by (command, byte address);
a read or write moves at most 56 bytes, so bigger regions go chunk by chunk.
Input reports that do not start with aa are events (mode change, calibration...).
There is no commit command: a write is live and persistent at once.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import select
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(os.path.realpath(__file__)).parent
DATA = json.loads((HERE / "nuphy_data.json").read_text(encoding="utf-8"))
VID = DATA["vendor_id"]
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "nuphy-he"

PACKET, HEADER, CHUNK = 64, 8, 56
MODES, LAYERS_PER_MODE, KEYS, GKEYS = 3, 4, 128, 16
# Bytes per mode (per layer for the key matrix) of each region.
SZ_LAYER, SZ_FUNC, SZ_LED, SZ_MT, SZ_TGL, SZ_DKS, SZ_MACRO, SZ_RT, SZ_NAME = 512, 64, 512, 256, 128, 768, 2048, 1024, 56
FUNC_LEN = 54                      # what the web driver reads of a 64-byte function block
ADV_SLOTS, MACROS, MACRO_TABLE = 32, 32, 64
APP_AUTOSELECT = 224               # app-define byte: "select every key on the trigger page"


class Cmd:
    QUICK_START, QUICK_END, GET_INFO, GET_BASE = 0x01, 0x02, 0x03, 0x04
    GET_FUNC, SET_FUNC = 0x05, 0x06
    GET_DEFAULT_KEYS, GET_KEYS, SET_KEYS = 0x07, 0x08, 0x09
    GET_LED, SET_LED = 0x0A, 0x0B
    GET_MACRO, SET_MACRO = 0x0C, 0x0D
    GET_GAMEPAD, SET_GAMEPAD = 0x10, 0x11
    GET_RT, SET_RT, GET_DKS, SET_DKS = 0xA0, 0xA1, 0xA2, 0xA3
    GET_MT, SET_MT, GET_TGL, SET_TGL = 0xA4, 0xA5, 0xA6, 0xA7
    CAL_START, CAL_END, GET_CAL = 0xA8, 0xA9, 0xAA
    LED_SYNC = 0xDE
    FACTORY_RESET = 0xEE
    GET_APP, SET_APP = 0xF1, 0xF2


READBACK = {Cmd.SET_FUNC: Cmd.GET_FUNC, Cmd.SET_KEYS: Cmd.GET_KEYS, Cmd.SET_LED: Cmd.GET_LED,
            Cmd.SET_MACRO: Cmd.GET_MACRO, Cmd.SET_GAMEPAD: Cmd.GET_GAMEPAD, Cmd.SET_RT: Cmd.GET_RT,
            Cmd.SET_DKS: Cmd.GET_DKS, Cmd.SET_MT: Cmd.GET_MT, Cmd.SET_TGL: Cmd.GET_TGL,
            Cmd.SET_APP: Cmd.GET_APP}
READ_ONLY = {Cmd.GET_INFO, Cmd.GET_BASE, Cmd.GET_FUNC, Cmd.GET_DEFAULT_KEYS, Cmd.GET_KEYS, Cmd.GET_LED,
             Cmd.GET_MACRO, Cmd.GET_GAMEPAD, Cmd.GET_RT, Cmd.GET_DKS, Cmd.GET_MT, Cmd.GET_TGL,
             Cmd.GET_CAL, Cmd.LED_SYNC, Cmd.GET_APP}

# Events: first byte of an input report that is not a reply.
EV_KEY_STATUS, EV_STATE, EV_RESET, EV_FUNC = 0xA0, 0xA1, 0xA2, 0xA3

# Key types: first of the three bytes that describe one key.
T_NONE, T_KEYBOARD, T_MOUSE_KEY, T_MOUSE_WHEEL, T_CONSUMER, T_SYSTEM = 0x00, 0x10, 0x20, 0x21, 0x30, 0x40
T_MACRO, T_DKS, T_TGL, T_MT, T_RS, T_SOCD, T_HT = 0x70, 0x90, 0x91, 0x92, 0x93, 0x94, 0x95
T_GKEY, T_FUNCTION, T_GAMEPAD = 0xE0, 0xF0, 0xF1
MT_FAMILY = (T_MT, T_RS, T_SOCD, T_HT)        # all four keep their two keys in the MT table
ADVANCED = {T_DKS: "DKS", T_TGL: "TGL", T_MT: "MT", T_RS: "RS", T_SOCD: "SOCD", T_HT: "HT"}
MACRO_HOLD, MACRO_TOGGLE = 0, 255            # loop byte: repeat while held / until pressed again

TRIGGER = {0: "normal", 1: "rt", 2: "rtc"}                      # rtc = continuous rapid trigger
PRIORITY = {0x00: "last", 0x10: "own", 0x20: "other", 0x30: "neutral"}
RATES = {1: 8000, 2: 4000, 3: 2000, 4: 1000}                    # function byte 4, low nibble
TICKS = {0: 0, 1: 30, 2: 60}
STABILIZATION = {0: "low", 3: "mid", 7: "high"}
LIGHT_BASE = {"main": 8, "side": 24, "loop": 35}                # offsets in the function block
LIGHT_OFF = 255
GAMEPAD_TYPES = {0: "off", 1: "classic", 2: "xbox"}
GAMEPAD_FLAGS = ("keyboard_keys", "overlay", "angle_adjust", "square_gate", "extreme_value")

_debug = bool(os.environ.get("NUPHY_DEBUG"))


class NuphyError(Exception):
    pass


def _log(*parts):
    if _debug:
        print(*parts, file=sys.stderr)


def _hex(b) -> str:
    return " ".join(f"{x:02x}" for x in b)


# ------------------------------------------------------------------ keycodes

KEYCODES: dict[str, int] = DATA["keycodes"]
_MOD_BITS = {224: 1, 225: 2, 226: 4, 227: 8, 228: 16, 229: 32, 230: 64, 231: 128}
_BIT_MODS = {v: k for k, v in _MOD_BITS.items()}
# The 0x7exx codes belong to NuPhy's mechanical firmware; 219/220 are its wheel codes.
_HE_INVALID = lambda v: 0x7E00 <= v <= 0x7EFF or v in (219, 220)  # noqa: E731
NAME_OF: dict[int, str] = {}
for _n, _v in KEYCODES.items():
    if not _HE_INVALID(_v):
        NAME_OF.setdefault(_v, _n)
NAME_OF[0], NAME_OF[1] = "KC_NO", "KC_TRANSPARENT"

_LABELS = {
    "KC_NO": "", "KC_ESCAPE": "Esc", "KC_ENTER": "Entrée", "KC_BACKSPACE": "⌫", "KC_TAB": "Tab",
    "KC_SPACE": "Espace", "KC_MINUS": "-", "KC_EQUAL": "=", "KC_LEFT_BRACKET": "[", "KC_RIGHT_BRACKET": "]",
    "KC_BACKSLASH": "\\", "KC_SEMICOLON": ";", "KC_QUOTE": "'", "KC_GRAVE": "`", "KC_COMMA": ",",
    "KC_DOT": ".", "KC_SLASH": "/", "KC_CAPS_LOCK": "Verr.Maj", "KC_PRINT_SCREEN": "Impr.écr",
    "KC_SCROLL_LOCK": "Arrêt défil", "KC_PAUSE": "Pause", "KC_INSERT": "Inser", "KC_HOME": "Début",
    "KC_PAGE_UP": "Pg↑", "KC_DELETE": "Suppr", "KC_END": "Fin", "KC_PAGE_DOWN": "Pg↓", "KC_RIGHT": "→",
    "KC_LEFT": "←", "KC_DOWN": "↓", "KC_UP": "↑", "KC_NUM_LOCK": "Verr.Num", "KC_APPLICATION": "Menu",
    "KC_LEFT_CTRL": "Ctrl", "KC_LEFT_SHIFT": "Maj", "KC_LEFT_ALT": "Alt", "KC_LEFT_GUI": "Super",
    "KC_RIGHT_CTRL": "Ctrl D", "KC_RIGHT_SHIFT": "Maj D", "KC_RIGHT_ALT": "AltGr", "KC_RIGHT_GUI": "Super D",
    "KC_NONUS_BACKSLASH": "<>", "KC_NONUS_HASH": "#~", "KC_VOL_UP": "Vol+", "KC_VOL_DOWN": "Vol-",
    "KC_MUTE": "Muet", "KC_PLAY": "Lecture", "KC_MEDIA_STOP": "Stop", "KC_MEDIA_PREV": "Préc.",
    "KC_MEDIA_NEXT": "Suiv.", "KC_SCRN_BRIGHTNESS_UP": "Écran+", "KC_SCRN_BRIGHTNESS_DOWN": "Écran-",
    "KC_CALCULATOR": "Calc", "KC_MAIL": "Mail", "KC_MY_COMPUTER": "Fichiers", "KC_MEDIA_PLAYER": "Lecteur",
    "KC_MS_LEFT_BTN": "Clic G", "KC_MS_RIGHT_BTN": "Clic D", "KC_MS_MIDDLE_BTN": "Clic M",
    "KC_MS_GO_AHEAD": "Souris ⇥", "KC_MS_GO_BACK": "Souris ⇤", "KC_MS_WH_UP": "Molette↑",
    "KC_MS_WH_DOWN": "Molette↓", "KC_MS_LEFT_BTN_DB_CLICK": "Double clic",
    "KC_FN_WIN_AREA_SCREENSHOT": "Capture", "KC_FN_WIN_GLOBAL_SCREENSHOT": "Capture écran",
    "KC_FN_MAC_AREA_SCREENSHOT": "Capture zone (Mac)", "KC_FN_MAC_GLOBAL_SCREENSHOT": "Capture écran (Mac)",
    "KC_FN_SWITCH_LAYER": "Couche ⇄", "KC_FN_KEYBOARD_LOCK": "Verrou clavier", "KC_FN_WIN_KEY_LOCK": "Verrou Win",
    "KC_FN_WINDOWS_KEYBOARD": "Mode Windows", "KC_FN_MAC_KEYBOARD": "Mode Mac",
    "KC_FN_WINDOWS_MAC_MODE_SWITCH": "Win/Mac", "KC_FN_BOOT": "⚠ Bootloader", "KC_FN_RESET": "⚠ Reset clavier",
    "KC_FN_NEXT_LIGHT": "Effet suivant", "KC_FN_PRE_LIGHT": "Effet préc.", "KC_FN_LIGHT_TOGGLE": "Effets ⟳",
    "KC_FN_LIGHT_TEST": "Test LED", "KC_FN_LIGHT_UP": "Lum+", "KC_FN_LIGHT_DOWN": "Lum-",
    "KC_FN_LIGHT_LOOP": "Lum ⟳", "KC_FN_LIGHT_OFF": "LED off", "KC_FN_SPEED_FAST": "Vitesse+",
    "KC_FN_SPEED_SLOWLY": "Vitesse-", "KC_FN_SPEED_LOOP": "Vitesse ⟳", "KC_FN_LIGHT_LEFT": "Sens ←",
    "KC_FN_LIGHT_RIGHT": "Sens →", "KC_FN_TREND": "Sens ⇄", "KC_FN_COLOR_UP": "Couleur+",
    "KC_FN_COLOR_DOWN": "Couleur-", "KC_FN_COLOR_LOOP": "Couleur ⟳", "KC_FN_SIDE_LIGHT_UP": "Côté lum+",
    "KC_FN_SIDE_LIGHT_DOWN": "Côté lum-", "KC_FN_SIDE_SLOWLY": "Côté vit-", "KC_FN_SIDE_FAST": "Côté vit+",
    "KC_FN_SIDE_COLOR_LOOP": "Côté couleur", "KC_FN_SIDE_MODE_LOOP": "Côté effet",
    "KC_FN_SLEEP_SWITCH": "Veille on/off", "KC_FN_MAC_SEARCH": "Recherche (Mac)",
    "KC_MAC_MISSION_CONTROL": "Mission Ctrl", "KC_MAC_LAUNCHPAD": "Launchpad", "KC_MAC_SIRI": "Siri",
    "KC_SYS_MAC_DND": "Ne pas déranger", "KC_FN_SWITCH_M1": "Mode M1", "KC_FN_SWITCH_M2": "Mode M2",
    "KC_FN_SWITCH_M3": "Mode M3", "KC_WWW_HOME": "Web accueil", "KC_WWW_REFRESH": "Web ⟳",
    "KC_WWW_STOP": "Web stop", "KC_WWW_BACK": "Web ←", "KC_WWW_FORWARD": "Web →",
    "KC_WWW_FAVORITES": "Web favoris", "KC_WWW_SEARCH": "Web recherche",
}
DANGEROUS = {KEYCODES["KC_FN_BOOT"], KEYCODES["KC_FN_RESET"]}


def code_bytes(code: int) -> bytes:
    """A keycode as the three bytes the firmware stores: type, code0, code1."""
    if code in _MOD_BITS:
        return bytes([T_KEYBOARD, _MOD_BITS[code], 0])
    if code == 0:
        return bytes(3)
    if _HE_INVALID(code):
        raise NuphyError(f"code réservé aux claviers mécaniques : 0x{code:04x}")
    return bytes([(code >> 16 & 0xFF) or T_KEYBOARD, code >> 8 & 0xFF, code & 0xFF])


def bytes_code(key) -> int:
    """The reverse: 0 for an empty slot, the raw 24-bit value for anything typed."""
    t, c0, c1 = key[0], key[1], key[2]
    if t == T_KEYBOARD:
        if c0 == 0:
            return c1
        return _BIT_MODS.get(c0, t << 16 | c0 << 8 | c1) if c1 == 0 else t << 16 | c0 << 8 | c1
    return t << 16 | c0 << 8 | c1


def parse_code(text: str) -> int:
    """KC_A, a, esc, lctl, fn1, 0xf0ff01... -> keycode. Names are the driver's own."""
    s = text.strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    u = s.upper().replace("-", "_").replace(" ", "_")
    for cand in (u, "KC_" + u, "KC_FN_" + u, "KC_" + u.removeprefix("KC_")):
        if cand in KEYCODES and not _HE_INVALID(KEYCODES[cand]):
            return KEYCODES[cand]
    raise NuphyError(f"touche inconnue : {text!r} (voir « nuphy codes »)")


def code_name(code: int) -> str:
    return NAME_OF.get(code) or f"0x{code:06x}"


_SHORTEST: dict[int, str] = {}
for _n, _v in KEYCODES.items():
    if _n.startswith("KC_") and not _HE_INVALID(_v) and len(_n) < len(_SHORTEST.get(_v, _n + "x")):
        _SHORTEST[_v] = _n


def code_word(code: int) -> str:
    """The shortest name the command line accepts for a keycode: lctl, esc, a..."""
    return _SHORTEST[code][3:].lower() if code in _SHORTEST else f"0x{code:06x}"


def code_label(code: int) -> str:
    """Short legend for a key cap."""
    name = NAME_OF.get(code)
    if name is None:
        return f"{code:06x}"
    if name in _LABELS:
        return _LABELS[name]
    if name.startswith("KC_FN") and name[5:].isdigit():
        return f"Fn→{name[5:]}"
    for prefix, short in (("KC_KP_", "Num "), ("KC_GAMEPAD_", "Pad "), ("KC_XBOX_", "Xbox "), ("KC_FN_", ""), ("KC_", "")):
        if name.startswith(prefix):
            body = name[len(prefix):].replace("_", " ")
            return short + (body if len(body) <= 2 else body.capitalize())
    return name


def key_label(key) -> str:
    """Legend for any stored key, advanced ones included."""
    t, c0 = key[0], key[1]
    if t == T_MACRO:
        return f"M{c0 + 1}"
    if t in ADVANCED:
        return f"{ADVANCED[t]}{c0 + 1}"
    if t == 0xFF and c0 == 0xFF:
        return ""
    return code_label(bytes_code(key))


# -------------------------------------------------------------------- models

@dataclass(frozen=True)
class Model:
    type_id: int
    name: str
    pids: tuple
    layout: tuple            # (x, y, w, h, keycode[, hidden]) in drawing order
    short: bool              # low-profile switches
    switches: tuple
    precision: float         # mm per unit of actuation travel
    rt_precisions: tuple
    lights: tuple            # LEDs under keys, LEDs elsewhere
    light_modes: dict
    mode_names: tuple
    default_layer: int
    raw: dict = field(compare=False, hash=False, default_factory=dict)

    def supports(self, feature: str, version: int) -> bool:
        """Features tied to a firmware version: 0 = always, None = never."""
        need = self.raw.get(feature, None if feature in ("gamepad", "xbox") else 0)
        return need is not None and version >= need

    def _in(self, key: str, version: int) -> bool:
        r = self.raw.get(key)
        return bool(r) and r[0] <= version <= r[1]

    def base_offset(self, version: int) -> int:
        return 8 if self._in("base_offset_until", version) else 0

    def rt_flag(self, version: int) -> int:
        return 0 if self._in("rt_flag0_until", version) else 5

    def legacy_sleep(self, version: int) -> bool:
        return version <= self.raw.get("legacy_sleep", -1)

    def switch_list(self, version: int) -> list[int]:
        out = list(self.switches)
        for vmin, extra in self.raw.get("switches_from", {}).items():
            if version >= int(vmin):
                out += [s for s in extra if s not in out]
        return out

    def switch(self, sid: int) -> tuple:
        """(name, total travel in mm, rapid-trigger step in mm) of a switch id."""
        table = DATA["switches"]["low" if self.short else "high"]
        name, total, steps = table.get(str(sid)) or (f"switch {sid}", 3.3 if self.short else 4.0, [self.precision])
        step = next((s for s in steps if s in self.rt_precisions), self.rt_precisions[0])
        return name, total, step


def _model(tid: str, m: dict) -> Model:
    return Model(int(tid), m["name"], tuple(m["pids"]), tuple(tuple(k) for k in m["layout"]), m["short"],
                 tuple(m["switches"]), m["precision"], tuple(m["rt_precisions"]), tuple(m["lights"]),
                 m["light_modes"], tuple(m["mode_names"]), m["default_layer"], m)


MODELS = {int(t): _model(t, m) for t, m in DATA["models"].items()}
BY_PID = {pid: m for m in MODELS.values() for pid in m.pids}


# ----------------------------------------------------------------- discovery

@dataclass(frozen=True)
class Node:
    path: str
    vid: int
    pid: int
    name: str


_CONTROL_DESC = bytes([0x05, 0x01, 0x09, 0x00, 0xA1, 0x01])   # usage page 1, usage 0, application


def find_nodes() -> list[Node]:
    """hidraw nodes that are the configuration interface of a known HE keyboard."""
    if os.environ.get("NUPHY_DEMO"):
        return [Node("demo", VID, MODELS[6].pids[0], "NuPhy Air75 HE (démo)")]
    base, out = Path("/sys/class/hidraw"), []
    if not base.is_dir():
        return out
    for entry in sorted(base.iterdir(), key=lambda p: int(p.name[6:] or 0)):
        try:
            uevent = (entry / "device/uevent").read_text()
            desc = (entry / "device/report_descriptor").read_bytes()
        except OSError:
            continue
        info = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        try:
            _, vid, pid = (int(x, 16) for x in info.get("HID_ID", "").split(":"))
        except ValueError:
            continue
        if vid == VID and pid in BY_PID and desc.startswith(_CONTROL_DESC):
            out.append(Node(f"/dev/{entry.name}", vid, pid, info.get("HID_NAME", "").strip()))
    return out


# ------------------------------------------------------------------- records

@dataclass
class KeyRT:
    """Trigger settings of one key, in firmware units (see Keyboard.rt_mm)."""
    switch: int = 0
    trigger: int = 0          # 0 normal, 1 rapid trigger, 2 continuous rapid trigger
    priority: int = 0         # SOCD: 0x00 last, 0x10 own, 0x20 other, 0x30 neutral
    travel: int = 0
    press_rt: int = 0
    release_rt: int = 0
    press_dz: int = 0         # dead zone at the top of the stroke
    release_dz: int = 0       # dead zone at the bottom
    press_prec: int = 0       # 0 = 0.01 mm, 1 = 0.005 mm, 2 = 0.001 mm
    release_prec: int = 0

    @classmethod
    def decode(cls, b) -> "KeyRT":
        travel = (b[3] << 8 | b[2]) & 0x1FF
        press, release = b[5] << 8 | b[4], b[7] << 8 | b[6]
        return cls(b[0] & 0x1F, b[1] & 0x0F, b[1] & 0xF0, travel, press & 0x1FF, release & 0x1FF,
                   press >> 9 & 0x7F, release >> 9 & 0x7F, b[3] >> 1 & 3, b[3] >> 3 & 3)

    def encode(self, flag: int) -> bytes:
        press = (self.press_dz & 0x7F) << 9 | self.press_rt & 0x1FF
        release = (self.release_dz & 0x7F) << 9 | self.release_rt & 0x1FF
        return bytes([self.switch & 0x1F | flag << 5, self.priority & 0xF0 | self.trigger & 0x0F,
                      self.travel & 0xFF, self.travel >> 8 & 1 | self.press_prec << 1 | self.release_prec << 3,
                      press & 0xFF, press >> 8, release & 0xFF, release >> 8])


@dataclass
class Dks:
    """Dynamic keystroke: four points of the stroke, up to four keys, and for
    each key what it does at each of the four phases of a stroke (press start,
    press bottom, release from bottom, release to top)."""
    points: list              # tenths of a millimetre: start, bottom, bottom release, top release
    keys: list                # four keycodes (0 = unused)
    phases: list              # four strings of four letters: . nothing, t tap, h press and hold, r release

    @classmethod
    def decode(cls, b) -> "Dks | None":
        if not any(b[:4]):
            return None
        keys, phases = [], []
        for k in range(4):
            at = 4 + k * 5
            keys.append(bytes_code(b[at:at + 3]))
            bits, row = b[at + 3] | b[at + 4] << 8, ""
            for seg in range(4):
                select, cont, nxt = (bits >> seg * 3 + i & 1 for i in range(3))
                if seg == 3:                           # only its "select" bit is stored
                    cont, nxt = row[-1:] == "h", 0
                row += "." if not select else "h" if nxt else "r" if cont else "t"
            phases.append(row)
        return cls(list(b[:4]), keys, phases)

    def encode(self) -> bytes:
        out = bytearray(self.points[:4])
        for code, row in zip(self.keys, self.phases):
            row = row.ljust(4, ".")[:4]
            bits = 0
            for seg, what in enumerate(row):
                held = seg > 0 and row[seg - 1] == "h"
                ok = what in "hr" if held else what in ".th"      # a held key is held on or released
                if not ok or (what == "h" and seg == 3):
                    raise NuphyError("phases DKS : t = appui bref, h = enfoncée et tenue, r = relâchée (après h), . = rien")
                if what != ".":
                    bits |= 1 << seg * 3
                if held:
                    bits |= 2 << seg * 3
                if what == "h":
                    bits |= 4 << seg * 3
            out += code_bytes(code if any(c != "." for c in row) else 0) + bytes([bits & 0xFF, bits >> 8 & 3])
        return bytes(out.ljust(24, b"\0"))


@dataclass
class MacroAction:
    code: int
    press: bool
    delay: int = 1            # milliseconds to wait after this action

    def encode(self, last: bool) -> bytes:
        k = code_bytes(self.code)
        if k[0] != T_KEYBOARD:
            raise NuphyError(f"une macro ne peut contenir que des touches clavier ({code_name(self.code)})")
        kind = 1 if k[1] else 2
        return bytes([self.delay & 0xFF, self.delay >> 8 & 0xFF, kind | (0x40 if self.press else 0) | (0x80 if last else 0),
                      k[1] if kind == 1 else k[2]])

    @classmethod
    def decode(cls, b) -> "MacroAction":
        kind = b[2] & 0x3F
        code = bytes_code([T_KEYBOARD, b[3] if kind == 1 else 0, b[3] if kind == 2 else 0])
        return cls(code, bool(b[2] & 0x40), b[1] << 8 | b[0])


# ------------------------------------------------------------------ keyboard

class Keyboard:
    """One open configuration interface. Use as a context manager: it holds a
    lock shared by every nuphy process so two of them never interleave frames."""

    TIMEOUT, ATTEMPTS = 0.3, 4

    def __init__(self, node: Node, fd: int | None = None):
        self.node = node
        self.model: Model = BY_PID.get(node.pid, MODELS[6])
        self.fd = -1
        self._given_fd = fd
        self._lock = None
        self._sim = None
        self._echo_addr: bool | None = None
        self.events: list[bytes] = []
        self.version = 0
        self.build = ""
        self.mode = 0
        self._index_pos: dict[int, int] | None = None
        self._check = not os.environ.get("NUPHY_NO_CHECKSUM")

    # -- lifetime

    def __enter__(self):
        run = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        self._lock = open(run / "nuphy-he.lock", "w")
        fcntl.flock(self._lock, fcntl.LOCK_EX)
        try:
            if self._given_fd is not None:
                self.fd = self._given_fd
            elif self.node.path == "demo":
                import nuphy_sim
                self._sim = nuphy_sim.spawn()
                self.fd = self._sim.fileno()
            else:
                self.fd = os.open(self.node.path, os.O_RDWR | os.O_CLOEXEC)
        except BaseException:
            self._unlock()
            raise
        return self

    def __exit__(self, *exc):
        if self.fd >= 0 and self._given_fd is None and self._sim is None:
            os.close(self.fd)
        if self._sim is not None:
            self._sim.close()
            self._sim = None
        self.fd = -1
        self._unlock()

    def _unlock(self):
        if self._lock:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
            self._lock.close()
            self._lock = None

    # -- frames

    @staticmethod
    def frame(cmd: int, length: int = 0, addr: int = 0, data=b"") -> bytes:
        if len(data) > CHUNK or not 0 <= addr <= 0xFFFF:
            raise ValueError("trame hors limites")
        p = bytearray(PACKET)
        p[0], p[1], p[4], p[5], p[6] = 0x55, cmd, length, addr & 0xFF, addr >> 8
        p[HEADER:HEADER + len(data)] = data
        p[3] = sum(p[4:]) & 0xFF
        return bytes(p)

    def _read_report(self, deadline: float) -> bytes | None:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                return None
            buf = os.read(self.fd, PACKET + 1)
            if len(buf) >= 2:
                return buf.ljust(PACKET, b"\0")[:PACKET]

    def _drain(self):
        while True:
            r, _, _ = select.select([self.fd], [], [], 0)
            if not r:
                return
            buf = os.read(self.fd, PACKET + 1)
            if not buf:
                return
            if buf[0] != 0xAA:
                self.events.append(buf)

    def transfer(self, cmd: int, length: int = 0, addr: int = 0, data=b"") -> bytes:
        """Send one frame and return the reply that answers it (all 64 bytes)."""
        req = self.frame(cmd, length, addr, data)
        why = "pas de réponse"
        for attempt in range(self.ATTEMPTS):
            self._drain()
            _log("->", _hex(req[:HEADER + max(len(data), 0)]))
            os.write(self.fd, b"\x00" + req)          # hidraw wants the (absent) report id first
            deadline = time.monotonic() + self.TIMEOUT
            while (rep := self._read_report(deadline)) is not None:
                if rep[0] != 0xAA:
                    self.events.append(rep)
                    continue
                if rep[1] != cmd:
                    continue
                got_addr = rep[5] | rep[6] << 8
                if cmd in READ_ONLY and addr and self._echo_addr is None:
                    self._echo_addr = got_addr == addr      # does this firmware echo addresses?
                if self._echo_addr and cmd in READ_ONLY and got_addr != addr:
                    continue                                # a late reply to an earlier chunk
                if self._check and sum(rep[4:]) & 0xFF != rep[3]:
                    why = "somme de contrôle invalide (NUPHY_NO_CHECKSUM=1 pour l'ignorer)"
                    break
                _log("<-", _hex(rep[:HEADER + max(length, rep[4] if cmd == Cmd.GET_INFO else 0)]))
                return rep
            _log(f"   essai {attempt + 1} : {why}")
        raise NuphyError(f"le clavier ne répond pas à la commande 0x{cmd:02x} : {why}")

    def read(self, cmd: int, addr: int, size: int) -> bytes:
        out = bytearray()
        while len(out) < size:
            n = min(CHUNK, size - len(out))
            out += self.transfer(cmd, n, addr + len(out))[HEADER:HEADER + n]
        return bytes(out)

    def write(self, cmd: int, addr: int, data, verify: bool = True):
        """Write a region chunk by chunk, then read it back: a write that returns
        has reached the keyboard."""
        data = bytes(data)
        if cmd == Cmd.SET_FUNC and (addr % SZ_FUNC < 4 or addr % SZ_FUNC + len(data) > SZ_FUNC):
            # Byte 0 of a function block is how the web driver enters the bootloader.
            raise NuphyError("écriture refusée : en-tête du bloc de fonctions")
        self._backup_once()
        for at in range(0, len(data), CHUNK):
            part = data[at:at + CHUNK]
            self.transfer(cmd, len(part), addr + at, part)
        if verify and cmd in READBACK:
            back = self.read(READBACK[cmd], addr, len(data))
            if back != data:
                raise NuphyError(f"le clavier n'a pas pris l'écriture (commande 0x{cmd:02x}, adresse {addr})")

    def quick(self, on: bool):
        """Fast-transfer mode, as the web driver brackets its bulk reads and writes."""
        self.transfer(Cmd.QUICK_START if on else Cmd.QUICK_END)

    class _Quick:
        def __init__(self, kb):
            self.kb = kb

        def __enter__(self):
            try:
                self.kb.quick(True)
                self.on = True
            except NuphyError:
                self.on = False                       # like the driver: carry on at normal speed

        def __exit__(self, *exc):
            if not self.on:
                return
            try:
                self.kb.quick(False)
            except (NuphyError, OSError):
                if exc[0] is None:
                    raise

    def bulk(self):
        return self._Quick(self)

    # -- identity

    def identify(self):
        """Firmware version, build stamp and current mode."""
        rep = self.transfer(Cmd.GET_INFO)
        d = rep[HEADER:]
        if bytes(d[:3]).isalpha():
            self.version = 100                       # oldest firmware answers with text only
        else:                                        # 0x0106 reads "106", as the driver's parseInt does
            digits = f"{d[1] << 8 | d[0]:x}"
            lead = len(digits) - len(digits.lstrip("0123456789"))
            self.version = int(digits[:lead] or 0)
        self.build = bytes(d[2:]).split(b"\0")[0].decode("ascii", "replace")
        base = self.transfer(Cmd.GET_BASE, 16)[HEADER:]
        off = self.model.base_offset(self.version)
        self.mode = base[off] if base[off] < MODES else 0
        return self

    @property
    def version_text(self) -> str:
        v = f"{self.version:03d}"
        return f"{v[:-2]}.{v[-2]}.{v[-1]}"

    def current_mode(self) -> int:
        base = self.transfer(Cmd.GET_BASE, 16)[HEADER:]
        m = base[self.model.base_offset(self.version)]
        self.mode = m if m < MODES else 0
        return self.mode

    def _mode(self, mode) -> int:
        mode = self.mode if mode is None else mode
        if not 0 <= mode < MODES:
            raise NuphyError("mode entre 1 et 3")
        return mode

    # -- layout: which stored key sits under which key cap

    def index_map(self) -> dict[int, int]:
        """data index -> position in the model's layout. The firmware stores keys
        in its own matrix order; the driver matches them to the drawing by the
        factory keycode of each one, and so does this."""
        if self._index_pos is not None:
            return self._index_pos
        defaults = self.default_keymap(self.model.default_layer)
        by_code: dict[int, list[int]] = {}
        for pos, k in enumerate(self.model.layout):
            by_code.setdefault(k[4], []).append(pos)
        fn1 = by_code.get(KEYCODES["KC_FN1"])
        if fn1:
            for n in range(12):
                by_code[KEYCODES["KC_FN0"] + n] = fn1
        shot = by_code.get(KEYCODES["KC_FN_WIN_AREA_SCREENSHOT"])
        if shot:
            by_code[KEYCODES["KC_PRINT_SCREEN"]] = by_code[KEYCODES["KC_FN_MAC_AREA_SCREENSHOT"]] = shot
        for missing, stand_in in (("KC_HOME", "KC_INSERT"), ("KC_END", "KC_DELETE")):
            if KEYCODES[missing] not in by_code and KEYCODES[stand_in] in by_code:
                by_code[KEYCODES[missing]] = by_code[KEYCODES[stand_in]]
        out, used = {}, set()
        for idx, key in enumerate(defaults[:KEYS]):
            if bytes(key) == b"\xff\xff\xff":
                continue
            spots = by_code.get(bytes_code(key))
            if not spots:
                continue
            pos = next((p for p in spots if p not in used), spots[0])
            used.add(pos)
            out[idx] = pos
        self._index_pos = out
        return out

    def key_names(self) -> dict[str, int]:
        """Names a key can be called by on the command line -> data index."""
        names = {}
        for idx, pos in self.index_map().items():
            code = self.model.layout[pos][4]
            for name, v in KEYCODES.items():
                if v == code:
                    names.setdefault(name[3:].lower(), idx)
        fn = next((i for i, p in self.index_map().items() if self.model.layout[p][4] == KEYCODES["KC_FN1"]), None)
        if fn is not None:
            names["fn"] = fn
        return names

    def key_index(self, ref) -> int:
        if isinstance(ref, int):
            idx = ref
        else:
            s = str(ref).strip().lower()
            idx = int(s.lstrip("#")) if s.lstrip("#").isdigit() else self.key_names().get(s.removeprefix("kc_"), -1)
        if idx not in self.index_map():
            raise NuphyError(f"touche physique inconnue : {ref!r} (voir « nuphy keys »)")
        return idx

    def key_title(self, idx: int) -> str:
        """What is printed on the key cap."""
        pos = self.index_map().get(idx)
        if pos is None:
            return f"#{idx}"
        code = self.model.layout[pos][4]
        return "Fn" if code == KEYCODES["KC_FN1"] else code_label(code)

    # -- key matrix

    @staticmethod
    def _split_keys(block) -> list[bytes]:
        return [bytes(block[i * 3:i * 3 + 3]) for i in range(KEYS)]

    def keymap(self, layer: int) -> list[bytes]:
        return self._split_keys(self.read(Cmd.GET_KEYS, self._layer(layer) * SZ_LAYER, KEYS * 3))

    def default_keymap(self, layer: int) -> list[bytes]:
        return self._split_keys(self.read(Cmd.GET_DEFAULT_KEYS, self._layer(layer) * SZ_LAYER, KEYS * 3))

    @staticmethod
    def _layer(layer: int) -> int:
        if not 0 <= layer < MODES * LAYERS_PER_MODE:
            raise NuphyError("couche invalide")
        return layer

    def layer_of(self, mode, sub: int) -> int:
        if not 0 <= sub < LAYERS_PER_MODE:
            raise NuphyError("couche entre 0 et 3")
        return self._mode(mode) * LAYERS_PER_MODE + sub

    def get_key(self, layer: int, idx: int) -> bytes:
        return self.read(Cmd.GET_KEYS, self._layer(layer) * SZ_LAYER + idx * 3, 3)

    def _release_slot(self, layer: int, key):
        """A key that leaves an advanced function gives its table slot back."""
        mode, t, slot = layer // LAYERS_PER_MODE, key[0], key[1]
        if slot >= ADV_SLOTS:
            return
        if t in MT_FAMILY:
            self.write(Cmd.SET_MT, mode * SZ_MT + slot * 6, bytes(6))
        elif t == T_TGL:
            self.write(Cmd.SET_TGL, mode * SZ_TGL + slot * 3, bytes(3))
        elif t == T_DKS:
            self.write(Cmd.SET_DKS, mode * SZ_DKS + slot * 24, bytes(24))

    def set_key_raw(self, layer: int, idx: int, key, release: bool = True):
        key = bytes(key)
        if len(key) != 3 or not 0 <= idx < KEYS:
            raise NuphyError("touche invalide")
        if release:
            old = self.get_key(layer, idx)
            if old == key:
                return
            self._release_slot(layer, old)
        self.write(Cmd.SET_KEYS, self._layer(layer) * SZ_LAYER + idx * 3, key)

    def set_key(self, layer: int, idx: int, code: int):
        """Plain remap: the key sends this keycode on this layer."""
        k = code_bytes(code)
        if k[0] in ADVANCED or k[0] == T_MACRO:
            raise NuphyError("les touches avancées et les macros ont leurs propres commandes")
        self._drop_partner(layer, idx)
        self.set_key_raw(layer, idx, k)

    def reset_key(self, layer: int, idx: int):
        """Back to the factory key of that layer."""
        self._drop_partner(layer, idx)
        default = self.read(Cmd.GET_DEFAULT_KEYS, self._layer(layer) * SZ_LAYER + idx * 3, 3)
        self.set_key_raw(layer, idx, default)

    def reset_layer(self, layer: int):
        addr = self._layer(layer) * SZ_LAYER
        self.write(Cmd.SET_KEYS, addr, self.read(Cmd.GET_DEFAULT_KEYS, addr, SZ_LAYER))

    def _drop_partner(self, layer: int, idx: int):
        """RS, SOCD and HT bind two keys: undoing one undoes the other."""
        old = self.get_key(layer, idx)
        if old[0] not in (T_RS, T_SOCD, T_HT):
            return
        other = old[2]
        back = self.get_key(layer, other) if other < KEYS else b""
        if len(back) == 3 and back[0] == old[0] and back[2] == idx:
            default = self.read(Cmd.GET_DEFAULT_KEYS, layer * SZ_LAYER + other * 3, 3)
            self.set_key_raw(layer, other, default)
        if old[0] == T_SOCD:
            mode = layer // LAYERS_PER_MODE
            for i in (idx, other):
                if i < KEYS:
                    at = mode * SZ_RT + i * 8 + 1
                    self.write(Cmd.SET_RT, at, bytes([self.read(Cmd.GET_RT, at, 1)[0] & 0x0F]))

    # -- function block: polling rate, locks, sleep, lighting

    def func(self, mode=None) -> bytes:
        return self.read(Cmd.GET_FUNC, self._mode(mode) * SZ_FUNC, FUNC_LEN)

    def settings(self, mode=None) -> dict:
        f = self.func(mode)
        sleep_on, sleep_min = bool(f[5] & 0x80), f[5] & 0x7F
        if self.model.legacy_sleep(self.version):
            sleep_on = sleep_min > 0
        lights = self.lights(mode, f)
        return {
            "rate": RATES.get(f[4] & 0x0F), "tick": TICKS.get(f[4] >> 4, 0),
            "sleep": sleep_min if sleep_on else 0, "sleep_minutes": sleep_min,
            "lock_win": bool(f[6] & 1), "lock_alt_tab": bool(f[6] & 2), "lock_alt_f4": bool(f[6] & 4),
            "game_optimization": bool(f[7] & 2), "debug": bool(f[7] & 8),
            "stabilization": STABILIZATION[0 if f[7] >> 4 & 7 < 3 else 7 if f[7] >> 4 & 7 > 3 else 3],
            "tachyon": not f[7] & 2 and all(v["brightness"] == 0 for v in lights.values()),
        }

    def _patch_func(self, mode: int, change, verify: bool = True):
        """Read bytes 4..7, let `change` edit them, write them back in one go."""
        at = mode * SZ_FUNC + 4
        b = bytearray(self.read(Cmd.GET_FUNC, at, 4))
        before = bytes(b)
        change(b)
        if bytes(b) != before:
            self.write(Cmd.SET_FUNC, at, b, verify)

    def set_rate(self, hz: int, mode=None):
        """USB polling rate. The keyboard re-enumerates after this one."""
        code = next((c for c, v in RATES.items() if v == hz), None)
        if code is None:
            raise NuphyError("polling : 1000, 2000, 4000 ou 8000 Hz")

        def change(b):
            b[0] = b[0] & 0xF0 | code
        try:
            self._patch_func(self._mode(mode), change, verify=False)
        except OSError:
            pass                                      # it left the bus to come back at the new rate

    def set_tick(self, tick: int, mode=None):
        code = next((c for c, v in TICKS.items() if v == tick), None)
        if code is None:
            raise NuphyError("tick rate : 0 (aucun), 30 ou 60")

        def change(b):
            b[0] = b[0] & 0x0F | code << 4
        self._patch_func(self._mode(mode), change)

    def set_flag(self, name: str, on: bool, mode=None):
        byte, bit = {"lock_win": (2, 1), "lock_alt_tab": (2, 2), "lock_alt_f4": (2, 4),
                     "game_optimization": (3, 2), "debug": (3, 8)}[name]

        def change(b):
            b[byte] = b[byte] | bit if on else b[byte] & ~bit
        self._patch_func(self._mode(mode), change)

    def set_stabilization(self, level: str, mode=None):
        code = next((c for c, v in STABILIZATION.items() if v == level), None)
        if code is None:
            raise NuphyError("anti-rebond : low, mid ou high")

        def change(b):
            b[3] = b[3] & 0x8F | code << 4
        self._patch_func(self._mode(mode), change)

    def set_sleep(self, minutes: int):
        """Auto sleep after that many idle minutes, 0 = never. Applies to the three
        modes, as in the web driver."""
        v = self.version
        if minutes and not 5 <= minutes <= 127:
            raise NuphyError("veille entre 5 et 127 minutes, ou 0 pour jamais")
        if minutes and not self.model.supports("sleep_time", v) and not self.model.legacy_sleep(v):
            raise NuphyError("ce firmware ne règle pas la durée de veille")
        for mode in range(MODES):
            def change(b):
                if self.model.legacy_sleep(v):        # old firmware: on or off, nothing in between
                    b[1] = (1 if minutes else 0) | (0x80 if b[1] & 0x7F else 0)
                elif minutes:
                    b[1] = 0x80 | minutes
                else:
                    b[1] &= 0x7F
            self._patch_func(mode, change)

    def set_tachyon(self, on: bool, mode=None):
        """The driver's "Tachyon mode": every LED off and the firmware's game
        optimisation pass skipped, for the shortest latency."""
        mode = self._mode(mode)
        cache = STATE_DIR / f"brightness-{mode}.json"
        lights = self.lights(mode)
        if on:
            lit = {k: v["brightness"] for k, v in lights.items() if v["brightness"]}
            if lit:
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(lit))
            self.set_flag("game_optimization", False, mode)
            for kind in lights:
                self.set_light(kind, mode, brightness=0)
        else:
            try:
                saved = json.loads(cache.read_text())
            except (OSError, ValueError):
                saved = {}
            self.set_flag("game_optimization", True, mode)
            for kind in lights:
                self.set_light(kind, mode, brightness=saved.get(kind) or 40)

    # -- lighting

    def lights(self, mode=None, func=None) -> dict:
        f = func if func is not None else self.func(mode)
        out = {}
        for kind in self.model.light_modes:
            at, main = LIGHT_BASE[kind], kind == "main"
            b = f[at:at + (9 if main else 8)]
            rest = b[4:] if main else b[3:]
            out[kind] = {"effect": b[0], "brightness": b[1], "speed": max(0, min(4, abs(b[2] - 4))),
                         "direction": b[3] if main else None, "rainbow": bool(rest[0]),
                         "color": "#%02x%02x%02x" % tuple(rest[2:5])}
        return out

    @staticmethod
    def light_segments(kind: str, **change) -> list[tuple[int, bytes]]:
        """Bytes to write, as (offset in the function block, data), for a change of
        some fields of one light zone. Neighbouring fields travel together."""
        main, seg = kind == "main", {}
        for name, v in change.items():
            if v is None:
                continue
            if name == "effect":
                seg[0] = int(v)
            elif name == "brightness":
                seg[1] = max(0, min(100, int(v)))
            elif name == "speed":
                seg[2] = max(0, min(4, abs(int(v) - 4)))      # stored reversed: 4 is the slowest
            elif name == "direction":
                if main:
                    seg[3] = 1 if v else 0
            elif name == "rainbow":
                seg[4 if main else 3] = 1 if v else 0
            elif name == "color_index":
                seg[5 if main else 4] = int(v)
            elif name == "color":
                for i, c in enumerate(parse_color(v)):
                    seg[(6 if main else 5) + i] = c
            else:
                raise NuphyError(f"réglage d'éclairage inconnu : {name}")
        out, run = [], []
        for o in sorted(seg) + [None]:
            if run and (o is None or o != run[-1] + 1):
                out.append((LIGHT_BASE[kind] + run[0], bytes(seg[i] for i in run)))
                run = []
            run.append(o)
        return out

    def set_light(self, kind: str, mode=None, **change):
        """Change some fields of one light zone: effect, brightness (0-100),
        speed (0-4), direction (0/1, key backlight only), rainbow (bool), color (#rrggbb)."""
        if kind not in self.model.light_modes:
            raise NuphyError(f"zone d'éclairage absente de ce clavier : {kind}")
        effect = change.get("effect")
        if effect is not None and effect != LIGHT_OFF and effect not in [m[0] for m in self.model.light_modes[kind]]:
            raise NuphyError("effet inconnu pour cette zone")
        for off, data in self.light_segments(kind, **change):
            self.write(Cmd.SET_FUNC, self._mode(mode) * SZ_FUNC + off, data)

    def key_colors(self, mode=None) -> list[tuple]:
        """Per-key colours of the "Custom" effect, by data index."""
        b = self.read(Cmd.GET_LED, self._mode(mode) * SZ_LED, KEYS * 3)
        return [tuple(b[i * 3:i * 3 + 3]) for i in range(KEYS)]

    def set_key_colors(self, colors: dict, mode=None):
        """{data index: (r, g, b)}; neighbours are written in one frame."""
        if not colors:
            return
        mode = self._mode(mode)
        cur = bytearray(self.read(Cmd.GET_LED, mode * SZ_LED, KEYS * 3))
        new = bytearray(cur)
        for idx, rgb in colors.items():
            if not 0 <= idx < KEYS:
                raise NuphyError("touche invalide")
            new[idx * 3:idx * 3 + 3] = bytes(rgb)
        self._write_diff(Cmd.SET_LED, mode * SZ_LED, cur, new)

    def _write_diff(self, cmd: int, base: int, old, new, gap: int = 8):
        """Write only what changed, merging runs closer than `gap` bytes."""
        i, n = 0, len(new)
        while i < n:
            if old[i] == new[i]:
                i += 1
                continue
            j = last = i
            while j < n and j - last <= gap and j - i < CHUNK:
                if old[j] != new[j]:
                    last = j
                j += 1
            self.write(cmd, base + i, new[i:last + 1])
            i = last + 1

    # -- trigger settings (actuation point, rapid trigger, dead zones)

    def rt_all(self, mode=None) -> list[KeyRT]:
        b = self.read(Cmd.GET_RT, self._mode(mode) * SZ_RT, SZ_RT)
        return [KeyRT.decode(b[i * 8:i * 8 + 8]) for i in range(KEYS)]

    def set_rt(self, changes: dict, mode=None):
        """{data index: KeyRT}. Neighbouring keys go in one frame, at most 7."""
        if not changes:
            return
        mode, flag = self._mode(mode), self.model.rt_flag(self.version)
        idxs = sorted(changes)
        run = [idxs[0]]
        for i in idxs[1:] + [None]:
            if i is not None and i == run[-1] + 1 and len(run) < CHUNK // 8:
                run.append(i)
                continue
            self.write(Cmd.SET_RT, mode * SZ_RT + run[0] * 8, b"".join(changes[k].encode(flag) for k in run))
            run = [i]

    def rt_mm(self, rt: KeyRT) -> dict:
        """Firmware units -> millimetres, the way the driver shows them."""
        name, total, step = self.model.switch(rt.switch)
        p = self.model.precision
        dz = lambda v: 0.0 if v == 0 else round((v + 1) * 0.01, 3)  # noqa: E731
        return {"switch": rt.switch, "switch_name": name, "total": total, "mode": TRIGGER.get(rt.trigger, "normal"),
                "actuation": round((rt.travel + 1) * p, 3), "press": round((rt.press_rt + 1) * step, 3),
                "release": round((rt.release_rt + 1) * step, 3), "top_dz": dz(rt.press_dz),
                "bottom_dz": dz(rt.release_dz), "priority": PRIORITY.get(rt.priority, "last")}

    def rt_from_mm(self, base: KeyRT, *, switch=None, actuation=None, mode=None, press=None, release=None,
                   top_dz=None, bottom_dz=None) -> KeyRT:
        """A copy of `base` with some values replaced, checked against the switch."""
        rt = KeyRT(**vars(base))
        if switch is not None:
            if switch not in self.model.switch_list(self.version):
                raise NuphyError("switch non proposé pour ce clavier")
            rt.switch = switch
        _, total, step = self.model.switch(rt.switch)
        unit = lambda mm, s: max(0, round(mm / s) - 1)  # noqa: E731
        if top_dz is not None:
            if not 0 <= top_dz <= 0.5:
                raise NuphyError("zone morte entre 0 et 0,5 mm")
            rt.press_dz = unit(top_dz, 0.01)
        if bottom_dz is not None:
            if not 0 <= bottom_dz <= 0.5:
                raise NuphyError("zone morte entre 0 et 0,5 mm")
            rt.release_dz = unit(bottom_dz, 0.01)
        if actuation is not None:
            rt.travel = unit(actuation, self.model.precision)
        cur = self.rt_mm(rt)
        lo, hi = max(0.1, cur["top_dz"]), round(total - cur["bottom_dz"], 3)
        if not lo - 1e-9 <= cur["actuation"] <= hi + 1e-9:
            if actuation is None:                      # a dead zone or switch moved the limits
                rt.travel = unit(min(max(cur["actuation"], lo), hi), self.model.precision)
            else:
                raise NuphyError(f"point d'actionnement entre {lo:g} et {hi:g} mm pour ce switch")
        for value, attr in ((press, "press_rt"), (release, "release_rt")):
            if value is not None:
                if not step - 1e-9 <= value <= 2.2 + 1e-9:
                    raise NuphyError(f"sensibilité rapid trigger entre {step:g} et 2,2 mm")
                setattr(rt, attr, unit(value, step))
        rt.press_prec = rt.release_prec = 1 if step == 0.005 else 0
        if mode is not None:
            code = next((c for c, n in TRIGGER.items() if n == mode), None)
            if code is None:
                raise NuphyError("mode : normal, rt ou rtc")
            rt.trigger = code
        return rt

    # -- advanced keys

    def _used_slots(self, mode: int) -> dict[int, set]:
        """Table slots some key of the mode points at, per table."""
        used = {T_MT: set(), T_TGL: set(), T_DKS: set()}
        for sub in range(LAYERS_PER_MODE):
            for key in self.keymap(mode * LAYERS_PER_MODE + sub):
                table = T_MT if key[0] in MT_FAMILY else key[0]
                if table in used:
                    used[table].add(key[1])
        return used

    def _slot(self, layer: int, idx: int, table: int) -> tuple[int, bool]:
        """Slot to store this key's data in: its own if it has one, else a free one."""
        old = self.get_key(layer, idx)
        if (T_MT if old[0] in MT_FAMILY else old[0]) == table and old[1] < ADV_SLOTS:
            return old[1], True
        used = self._used_slots(layer // LAYERS_PER_MODE)[table]
        free = next((s for s in range(ADV_SLOTS) if s not in used), None)
        if free is None:
            raise NuphyError("les 32 emplacements de ce type sont pris dans ce mode")
        return free, False

    @staticmethod
    def _plain(code: int, what: str) -> bytes:
        k = code_bytes(code)
        if k[0] in ADVANCED or k[0] in (T_MACRO, T_GKEY, T_NONE):
            raise NuphyError(f"{what} : il faut une touche simple")
        return k

    def set_mod_tap(self, layer: int, idx: int, tap: int, hold: int, ms: int = 170):
        """Tap sends one key, holding longer than `ms` sends the other."""
        data = self._plain(tap, "tap") + self._plain(hold, "maintien")
        self._drop_partner(layer, idx)
        slot, own = self._slot(layer, idx, T_MT)
        self.write(Cmd.SET_MT, layer // LAYERS_PER_MODE * SZ_MT + slot * 6, data)
        self.set_key_raw(layer, idx, [T_MT, slot, max(1, min(100, round(ms / 10)))], release=not own)

    def set_toggle(self, layer: int, idx: int, code: int):
        """A tap latches the key down until the next tap; holding acts normally."""
        data = self._plain(code, "bascule")
        self._drop_partner(layer, idx)
        slot, own = self._slot(layer, idx, T_TGL)
        self.write(Cmd.SET_TGL, layer // LAYERS_PER_MODE * SZ_TGL + slot * 3, data)
        self.set_key_raw(layer, idx, [T_TGL, slot, 0], release=not own)

    def set_dks(self, layer: int, idx: int, dks: Dks):
        p = dks.points
        if len(p) != 4 or not all(1 <= v <= 40 for v in p) or p[0] > p[1] or p[3] > p[2]:
            raise NuphyError("DKS : quatre points en dixièmes de mm, début ≤ fond et relâchement haut ≤ bas")
        for code in dks.keys:
            if code:
                self._plain(code, "DKS")
        self._drop_partner(layer, idx)
        slot, own = self._slot(layer, idx, T_DKS)
        self.write(Cmd.SET_DKS, layer // LAYERS_PER_MODE * SZ_DKS + slot * 24, dks.encode())
        self.set_key_raw(layer, idx, [T_DKS, slot, 0], release=not own)

    def _pair(self, layer: int, a: int, b: int, kind: int, entry_a: bytes, entry_b: bytes):
        if a == b:
            raise NuphyError("il faut deux touches différentes")
        mode = layer // LAYERS_PER_MODE
        for idx, other, entry in ((a, b, entry_a), (b, a, entry_b)):
            old = self.get_key(layer, idx)
            if old[0] in (T_RS, T_SOCD, T_HT) and old[2] != other:
                self._drop_partner(layer, idx)        # was bound to a third key
            slot, own = self._slot(layer, idx, T_MT)
            self.write(Cmd.SET_MT, mode * SZ_MT + slot * 6, entry)
            self.set_key_raw(layer, idx, [kind, slot, other], release=not own)

    def _own_code(self, layer: int, idx: int) -> int:
        """What the key itself sends: its current key if plain, else its factory key."""
        k = self.get_key(layer, idx)
        if k[0] in ADVANCED or k[0] in (T_MACRO, T_NONE):
            k = self.read(Cmd.GET_DEFAULT_KEYS, layer * SZ_LAYER + idx * 3, 3)
        return bytes_code(k)

    def set_rapid_shift(self, layer: int, a: int, b: int):
        """Of the two keys, the one pressed deeper wins."""
        ka, kb = (self._plain(self._own_code(layer, i), "RS") for i in (a, b))
        self._pair(layer, a, b, T_RS, ka + kb, kb + ka)

    def set_socd(self, layer: int, a: int, b: int, rule: str = "last"):
        """Opposite directions: 'last' pressed wins, 'neutral' cancels both, 'a' or 'b' always wins."""
        pa, pb = {"last": (0x00, 0x00), "neutral": (0x30, 0x30), "a": (0x10, 0x20), "b": (0x20, 0x10)}.get(rule, (None, None))
        if pa is None:
            raise NuphyError("règle SOCD : last, neutral, a ou b")
        ka, kb = (self._plain(self._own_code(layer, i), "SOCD") for i in (a, b))
        self._pair(layer, a, b, T_SOCD, ka + kb, kb + ka)
        mode = layer // LAYERS_PER_MODE
        for idx, prio in ((a, pa), (b, pb)):
            at = mode * SZ_RT + idx * 8 + 1
            self.write(Cmd.SET_RT, at, bytes([prio | self.read(Cmd.GET_RT, at, 1)[0] & 0x0F]))

    def set_hyper_tap(self, layer: int, a: int, b: int, both: bool = True, travel_mm: float = 0.5):
        """Releasing one key taps the other. One-way (a -> b) or both ways."""
        ka, kb = (self._plain(self._own_code(layer, i), "HT") for i in (a, b))
        if kb[0] != T_KEYBOARD or ka[0] != T_KEYBOARD:
            raise NuphyError("Hyper Tap : touches clavier uniquement")
        raw = max(0, round(travel_mm / self.model.precision) - 1) & 0xFF
        if both:
            self._pair(layer, a, b, T_HT, ka + bytes([raw]) + kb[1:], kb + bytes([raw]) + ka[1:])
        else:
            self._drop_partner(layer, a)
            slot, own = self._slot(layer, a, T_MT)
            self.write(Cmd.SET_MT, layer // LAYERS_PER_MODE * SZ_MT + slot * 6, ka + bytes([raw]) + kb[1:])
            self.set_key_raw(layer, a, [T_HT, slot, b], release=not own)

    def advanced(self, mode=None) -> list[dict]:
        """Every advanced key of a mode, decoded."""
        mode = self._mode(mode)
        mt = self.read(Cmd.GET_MT, mode * SZ_MT, ADV_SLOTS * 6)
        tgl = self.read(Cmd.GET_TGL, mode * SZ_TGL, ADV_SLOTS * 3)
        dks = self.read(Cmd.GET_DKS, mode * SZ_DKS, SZ_DKS)
        out = []
        for sub in range(LAYERS_PER_MODE):
            layer = mode * LAYERS_PER_MODE + sub
            for idx, key in enumerate(self.keymap(layer)):
                t, slot = key[0], key[1]
                if t not in ADVANCED or slot >= ADV_SLOTS or idx not in self.index_map():
                    continue
                item = {"kind": ADVANCED[t], "layer": sub, "index": idx, "slot": slot}
                if t in MT_FAMILY:
                    e = mt[slot * 6:slot * 6 + 6]
                    if t == T_MT:
                        item.update(tap=bytes_code(e[:3]), hold=bytes_code(e[3:]), ms=key[2] * 10)
                    elif t == T_HT:
                        item.update(key=bytes_code(e[:3]), other=key[2],
                                    travel=round((e[3] + 1) * self.model.precision, 2))
                    else:
                        item.update(key=bytes_code(e[:3]), other=key[2])
                elif t == T_TGL:
                    item.update(key=bytes_code(tgl[slot * 3:slot * 3 + 3]))
                else:
                    d = Dks.decode(dks[slot * 24:slot * 24 + 24])
                    item.update(dks=d)
                out.append(item)
        return out

    # -- macros

    def _macro_area(self, mode: int) -> bytes:
        return self.read(Cmd.GET_MACRO, mode * SZ_MACRO, SZ_MACRO)

    @staticmethod
    def _parse_macros(area) -> list[list[MacroAction]]:
        out = []
        for m in range(MACROS):
            at = area[m * 2] | area[m * 2 + 1] << 8
            acts: list[MacroAction] = []
            while MACRO_TABLE <= at <= SZ_MACRO - 4:
                b = area[at:at + 4]
                if not any(b) or (not acts and b[2] & 0x80):
                    break                              # free space starts with a lone end marker
                acts.append(MacroAction.decode(b))
                if b[2] & 0x80:
                    break
                at += 4
            out.append(acts)
        return out

    def macros(self, mode=None) -> list[list[MacroAction]]:
        return self._parse_macros(self._macro_area(self._mode(mode)))

    def set_macro(self, macro_id: int, actions: list[MacroAction], mode=None):
        """Store (or with an empty list, delete) one of the 32 macros of a mode."""
        mode = self._mode(mode)
        if not 0 <= macro_id < MACROS:
            raise NuphyError("macro entre 1 et 32")
        if len(actions) == 1:
            raise NuphyError("une macro fait au moins deux actions (appui puis relâchement)")
        old = self._macro_area(mode)
        macros = self._parse_macros(old)
        macros[macro_id] = list(actions)
        new, at = bytearray(SZ_MACRO), MACRO_TABLE
        starts = {}
        for m, acts in enumerate(macros):
            if not acts:
                continue
            blob = b"".join(a.encode(i == len(acts) - 1) for i, a in enumerate(acts))
            if at + len(blob) > SZ_MACRO:
                raise NuphyError("mémoire macro pleine (496 actions par mode)")
            starts[m] = at
            new[at:at + len(blob)] = blob
            at += len(blob)
        free = at if at + 4 <= SZ_MACRO else 0xFFFF
        if free != 0xFFFF:
            new[at:at + 4] = bytes([0, 0, 0x80, 0])     # free space starts with an end marker...
        for m in range(MACROS):
            struct.pack_into("<H", new, m * 2, starts.get(m, free))   # ...and unused macros point at it
        self._write_diff(Cmd.SET_MACRO, mode * SZ_MACRO, old, new)

    def bind_macro(self, layer: int, idx: int, macro_id: int, loop: int = 1):
        """Put a macro on a key. loop: 1-254 repeats, MACRO_HOLD, MACRO_TOGGLE."""
        if not 0 <= macro_id < MACROS or not 0 <= loop <= 255:
            raise NuphyError("macro entre 1 et 32, répétitions entre 1 et 254")
        if loop != 1 and not self.model.supports("macro_loop", self.version):
            raise NuphyError("ce firmware ne sait pas répéter une macro")
        self._drop_partner(layer, idx)
        self.set_key_raw(layer, idx, [T_MACRO, macro_id, loop])

    # -- names, small preferences

    def mode_names(self) -> list[str]:
        out = []
        for m in range(MODES):
            raw = self.read(Cmd.GET_APP, m * SZ_NAME, SZ_NAME).split(b"\0")[0]
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                name = ""
            out.append(name if name.strip() and name.isprintable() else self.model.mode_names[m])
        return out

    def set_mode_name(self, mode: int, name: str):
        raw = name.strip().encode("utf-8")
        if len(raw) > SZ_NAME:
            raise NuphyError("nom trop long (56 octets)")
        self.write(Cmd.SET_APP, self._mode(mode) * SZ_NAME, raw.ljust(SZ_NAME, b"\0"))

    def auto_select(self) -> bool:
        return self.read(Cmd.GET_APP, APP_AUTOSELECT, 1)[0] == 1

    def set_auto_select(self, on: bool):
        self.write(Cmd.SET_APP, APP_AUTOSELECT, bytes([1 if on else 0]))

    # -- virtual gamepad

    def _pad_base(self, mode) -> int:
        per_mode = self.model.type_id == 1 and self.version >= 999
        return (self._mode(mode) if per_mode else 0) * 256

    def gamepad(self, mode=None) -> dict:
        if not self.model.supports("gamepad", self.version):
            raise NuphyError("pas de mode manette sur ce clavier ou ce firmware")
        b = self.read(Cmd.GET_GAMEPAD, self._pad_base(mode) + 2, 7)
        mask = int.from_bytes(b[2:6], "little")
        out = {"type": GAMEPAD_TYPES.get(b[0], "off"), "angle": min(90, b[6])}
        out.update({name: bool(mask >> bit & 1) for bit, name in enumerate(GAMEPAD_FLAGS)})
        return out

    def set_gamepad(self, mode=None, **change):
        cur = self.gamepad(mode)
        for k, v in change.items():
            if v is None:
                continue
            if k not in cur:
                raise NuphyError(f"réglage manette inconnu : {k}")
            cur[k] = v
        code = next((c for c, n in GAMEPAD_TYPES.items() if n == cur["type"]), None)
        if code is None:
            raise NuphyError("type de manette : off, classic ou xbox")
        if code == 2 and not self.model.supports("xbox", self.version):
            raise NuphyError("le type Xbox n'existe pas sur ce clavier : off ou classic")
        mask = sum(1 << bit for bit, name in enumerate(GAMEPAD_FLAGS) if cur[name])
        data = bytes([code, 0]) + mask.to_bytes(4, "little") + bytes([max(0, min(90, int(cur["angle"])))])
        self.write(Cmd.SET_GAMEPAD, self._pad_base(mode) + 2, data)

    def gamepad_curve(self, mode=None) -> bytes:
        return self.read(Cmd.GET_GAMEPAD, self._pad_base(mode) + 64, 64)

    def set_gamepad_curve(self, curve, mode=None):
        if len(curve) != 64:
            raise NuphyError("une courbe fait 64 points")
        self.write(Cmd.SET_GAMEPAD, self._pad_base(mode) + 64, bytes(curve))

    # -- calibration and live travel

    def calibration_start(self):
        self.transfer(Cmd.CAL_START)

    def calibration_end(self):
        self.transfer(Cmd.CAL_END)

    def live(self, on: bool):
        """Ask the keyboard to report key travel as it happens (events 0xa0)."""
        for mode in range(MODES):
            def change(b):
                b[3] = b[3] | 8 if on else b[3] & ~8
            at = mode * SZ_FUNC + 4
            b = bytearray(self.read(Cmd.GET_FUNC, at, 4))
            before = bytes(b)
            change(b)
            if bytes(b) != before:
                self.write(Cmd.SET_FUNC, at, b)

    def poll(self, timeout: float = 0.1) -> list[dict]:
        """Events received since the last call (and for up to `timeout` more)."""
        deadline = time.monotonic() + timeout
        while (rep := self._read_report(deadline)) is not None:
            if rep[0] != 0xAA:
                self.events.append(rep)
            if len(self.events) > 256:
                break
        raw, self.events = self.events, []
        return [e for e in (self.parse_event(r) for r in raw) if e]

    def parse_event(self, e) -> dict | None:
        if e[0] == EV_KEY_STATUS:
            code = bytes_code(e[1:4])
            idx = next((i for i, p in self.index_map().items() if self.model.layout[p][4] == code
                        or (KEYCODES["KC_FN0"] <= code <= KEYCODES["KC_FN11"] and self.model.layout[p][4] == KEYCODES["KC_FN1"])), None)
            return {"event": "key", "index": idx, "code": code, "travel": round((e[6] << 8 | e[7]) * self.model.precision, 3),
                    "state": "none" if e[10] == 0 else "done" if e[10] == 255 else "calibrating"}
        if e[0] == EV_STATE:
            if e[2] < MODES:
                self.mode = e[2]
            return {"event": "state", "fn_layer": e[1], "mode": e[2]}
        if e[0] == EV_RESET:
            return {"event": "reset"}
        if e[0] == EV_FUNC:
            return {"event": "settings", "lock_win": bool(e[6] & 1)}
        return None

    # -- reset, export, import

    def reset_mode(self, mode=None):
        """What the driver's "Reset" of one mode does: advanced keys, macros, name
        and key matrix back to factory. Trigger settings and lighting stay."""
        mode = self._mode(mode)
        self._backup_once()
        with self.bulk():
            self.write(Cmd.SET_MT, mode * SZ_MT, bytes(SZ_MT), verify=False)
            self.write(Cmd.SET_TGL, mode * SZ_TGL, bytes(SZ_TGL), verify=False)
            self.write(Cmd.SET_DKS, mode * SZ_DKS, bytes(SZ_DKS), verify=False)
            self.write(Cmd.SET_MACRO, mode * SZ_MACRO, bytes(SZ_MACRO), verify=False)
            self.write(Cmd.SET_APP, mode * SZ_NAME, bytes(SZ_NAME), verify=False)
            addr, size = mode * LAYERS_PER_MODE * SZ_LAYER, LAYERS_PER_MODE * SZ_LAYER
            self.write(Cmd.SET_KEYS, addr, self.read(Cmd.GET_DEFAULT_KEYS, addr, size), verify=False)

    def factory_reset(self):
        """The driver's "Reset keyboard": wipe the three modes, then the firmware's own reset."""
        self._backup_once()
        with self.bulk():
            for cmd, size in ((Cmd.SET_MT, SZ_MT), (Cmd.SET_TGL, SZ_TGL), (Cmd.SET_DKS, SZ_DKS),
                              (Cmd.SET_MACRO, SZ_MACRO), (Cmd.SET_APP, SZ_NAME)):
                self.write(cmd, 0, bytes(size * MODES), verify=False)
        self.transfer(Cmd.FACTORY_RESET)
        self._index_pos = None

    def export_mode(self, mode=None) -> dict:
        """One mode in the web driver's own file format (its Export button)."""
        mode = self._mode(mode)
        enc = lambda b: base64.b64encode(bytes(b)).decode()  # noqa: E731
        with self.bulk():
            cfg = {
                "c": "",
                "f": enc(self.transfer(Cmd.GET_FUNC, FUNC_LEN, mode * SZ_FUNC)),
                "k": enc(self.read(Cmd.GET_KEYS, mode * LAYERS_PER_MODE * SZ_LAYER, LAYERS_PER_MODE * SZ_LAYER)),
                "r": enc(self.read(Cmd.GET_RT, mode * SZ_RT, SZ_RT)),
                "d": enc(self.read(Cmd.GET_DKS, mode * SZ_DKS, SZ_DKS)),
                "m": enc(self.read(Cmd.GET_MT, mode * SZ_MT, SZ_MT)),
                "t": enc(self.read(Cmd.GET_TGL, mode * SZ_TGL, SZ_TGL)),
                "n": enc(self.read(Cmd.GET_APP, mode * SZ_NAME, SZ_NAME)),
                "h": enc(self.read(Cmd.GET_MACRO, mode * SZ_MACRO, SZ_MACRO)),
                "p": str(self.model.type_id),
                "v": base64.b64encode(str(self.version).encode()).decode(),
                "a": enc(self.read(Cmd.GET_LED, mode * SZ_LED, SZ_LED)),
            }
        sums = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in cfg.items() if k != "c"}
        cfg["c"] = base64.b64encode(json.dumps(sums, separators=(",", ":")).encode()).decode()
        return cfg

    def export_all(self) -> dict:
        return {"cfg": [self.export_mode(m) for m in range(MODES)],
                "deviceName": (self.node.name or self.model.name).replace(" ", "_")}

    PARTS = ("light", "advanced", "keys", "trigger", "settings", "macros")

    def import_mode(self, cfg: dict, mode=None, parts=PARTS, keep_switches: bool = True):
        """Load an exported mode. Like the driver, the polling rate and sleep
        toggle of the keyboard are kept, and (unless told otherwise) so is the
        switch type of each key: a file cannot know which switches are fitted."""
        mode = self._mode(mode)
        try:
            sums = json.loads(base64.b64decode(cfg["c"]))
            ok = all(hashlib.sha256(cfg[k].encode()).hexdigest() == s for k, s in sums.items())
            dec = {k: base64.b64decode(cfg[k]) for k in "fkrdmtnha" if cfg.get(k)}
        except (KeyError, ValueError, TypeError, AttributeError):
            ok, dec = False, {}
        if not ok or "f" not in dec or "k" not in dec:
            raise NuphyError("fichier de configuration invalide ou abîmé")
        if str(cfg.get("p")) != str(self.model.type_id):
            raise NuphyError("ce fichier vient d'un autre modèle de clavier")
        sizes = {"k": LAYERS_PER_MODE * SZ_LAYER, "r": SZ_RT, "d": SZ_DKS, "m": SZ_MT, "t": SZ_TGL,
                 "n": SZ_NAME, "h": SZ_MACRO, "a": SZ_LED, "f": PACKET}
        if any(len(dec[k]) != n for k, n in sizes.items() if k in dec):
            raise NuphyError("fichier de configuration invalide (tailles)")
        func = dec["f"][HEADER:HEADER + FUNC_LEN]
        self._backup_once()
        with self.bulk():
            if "light" in parts:
                if "a" in dec:
                    self.write(Cmd.SET_LED, mode * SZ_LED, dec["a"], verify=False)
                self.write(Cmd.SET_FUNC, mode * SZ_FUNC + 8, func[8:], verify=False)
            if "advanced" in parts and all(k in dec for k in "mtd"):
                self.write(Cmd.SET_MT, mode * SZ_MT, dec["m"], verify=False)
                self.write(Cmd.SET_TGL, mode * SZ_TGL, dec["t"], verify=False)
                self.write(Cmd.SET_DKS, mode * SZ_DKS, dec["d"], verify=False)
            if "keys" in parts:
                keys = bytearray(dec["k"])
                for sub in range(LAYERS_PER_MODE):     # Fn keys name absolute layers: move them to this mode
                    for at in range(sub * SZ_LAYER, sub * SZ_LAYER + KEYS * 3, 3):
                        if keys[at] == T_FUNCTION and keys[at + 1] == 0xFF:
                            keys[at + 2] = keys[at + 2] % LAYERS_PER_MODE + mode * LAYERS_PER_MODE
                self.write(Cmd.SET_KEYS, mode * LAYERS_PER_MODE * SZ_LAYER, keys, verify=False)
            if "trigger" in parts and "r" in dec:
                rt = bytearray(dec["r"])
                if keep_switches:
                    cur, flag = self.read(Cmd.GET_RT, mode * SZ_RT, SZ_RT), self.model.rt_flag(self.version)
                    for i in range(KEYS):
                        mine, theirs = KeyRT.decode(cur[i * 8:]), KeyRT.decode(rt[i * 8:])
                        if mine.switch != theirs.switch:
                            mm = self.rt_mm(theirs)
                            theirs.switch = mine.switch
                            _, total, step = self.model.switch(mine.switch)
                            fixed = self.rt_from_mm(theirs, press=min(2.2, max(step, mm["press"])),
                                                    release=min(2.2, max(step, mm["release"])),
                                                    actuation=min(mm["actuation"], round(total - mm["bottom_dz"] - 0.05, 2)))
                            rt[i * 8:i * 8 + 8] = fixed.encode(flag)
                self.write(Cmd.SET_RT, mode * SZ_RT, rt, verify=False)
            if "settings" in parts:
                if "n" in dec:
                    self.write(Cmd.SET_APP, mode * SZ_NAME, dec["n"], verify=False)
                at = mode * SZ_FUNC + 4
                cur = self.read(Cmd.GET_FUNC, at, 4)
                new = bytearray(func[4:8])
                new[0] = new[0] & 0xF0 | cur[0] & 0x0F                    # polling rate stays
                new[1] = cur[1] & 0x80 | (new[1] & 0x7F if new[1] & 0x7F >= 5 else cur[1] & 0x7F)
                new[3] = new[3] & ~0x0C | cur[3] & 0x04                   # calibration flag stays, live view off
                self.write(Cmd.SET_FUNC, at, new, verify=False)
            if "macros" in parts and "h" in dec:
                self.write(Cmd.SET_MACRO, mode * SZ_MACRO, dec["h"], verify=False)
        self._index_pos = None

    def _backup_once(self):
        """Before the very first change this tool ever makes, keep a full export."""
        if getattr(self, "_backed_up", False):
            return
        self._backed_up = True
        if self.node.path == "demo" or self._given_fd is not None:
            return
        path = STATE_DIR / f"config-origine-{self.node.pid:04x}.json"
        if path.exists():
            return
        if not self.version:
            self.identify()
        data = self.export_all()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))


def parse_color(v) -> tuple:
    if isinstance(v, (tuple, list)) and len(v) == 3:
        return tuple(int(x) & 0xFF for x in v)
    s = str(v).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    try:
        if len(s) != 6:
            raise ValueError
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise NuphyError(f"couleur invalide : {v!r} (attendu rrggbb)") from None


def open_keyboard() -> Keyboard:
    """The first known keyboard that answers. Returned unopened but identified."""
    nodes = find_nodes()
    if not nodes:
        raise NuphyError("aucun clavier NuPhy HE trouvé (branché en USB ? règle udev installée ?)")
    errors = []
    for node in nodes:
        kb = Keyboard(node)
        try:
            with kb:
                kb.identify()
            return kb
        except PermissionError:
            errors.append(f"{node.path} : accès refusé (règle udev manquante pour {node.vid:04x}:{node.pid:04x})")
        except (OSError, NuphyError) as e:
            errors.append(f"{node.path} : {e}")
    raise NuphyError("le clavier ne répond pas\n  " + "\n  ".join(errors))


# ----------------------------------------------------------------------- CLI

def _on_off(v: str) -> bool:
    v = v.lower()
    if v in ("on", "1", "oui", "yes", "true"):
        return True
    if v in ("off", "0", "non", "no", "false"):
        return False
    raise argparse.ArgumentTypeError("on ou off")


def _mm(v: str) -> float:
    try:
        return float(v.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError("valeur en millimètres, ex. 1.2") from None


def _keys(kb: Keyboard, spec: str) -> list[int]:
    if spec.lower() in ("all", "tout", "toutes"):
        return sorted(kb.index_map())
    return [kb.key_index(s) for s in spec.split(",") if s.strip()]


def _draw(kb: Keyboard, text_of) -> str:
    """The keyboard as rows of text, one cell per key."""
    rows: dict[float, list] = {}
    for idx, pos in kb.index_map().items():
        x, y = kb.model.layout[pos][:2]
        rows.setdefault(y, []).append((x, text_of(idx)))
    return "\n".join("  ".join(t for _, t in sorted(cells)) for _, cells in sorted(rows.items()))


def parse_macro(spec: str) -> list[MacroAction]:
    """'+lctl +c 20 -c -lctl' : +key presses, -key releases, key taps, a number
    is the delay in ms after the previous action (10 ms when not given)."""
    acts: list[MacroAction] = []
    for tok in spec.replace(",", " ").split():
        if tok.isdigit():
            if not acts:
                raise NuphyError("une macro commence par une touche")
            acts[-1].delay = max(1, min(65535, int(tok)))
        elif tok[0] in "+-" and len(tok) > 1:
            acts.append(MacroAction(parse_code(tok[1:]), tok[0] == "+", 10))
        else:
            code = parse_code(tok)
            acts += [MacroAction(code, True, 10), MacroAction(code, False, 10)]
    return acts


def format_macro(acts: list[MacroAction]) -> str:
    out = []
    for a in acts:
        out.append(("+" if a.press else "-") + code_word(a.code))
        if a.delay != 10:
            out.append(str(a.delay))
    return " ".join(out)


def describe(kb: Keyboard, mode=None) -> dict:
    """Everything the settings pages show for one mode, as plain data."""
    mode = kb._mode(mode)
    d = {"model": kb.model.name, "firmware": kb.version_text, "build": kb.build, "mode": mode,
         "current_mode": kb.mode, "mode_names": kb.mode_names(), "lights": kb.lights(mode)}
    d.update(kb.settings(mode))
    return d


def _show(d: dict):
    on = lambda v: "oui" if v else "non"  # noqa: E731
    names = d["mode_names"]
    print(f"{d['model']}  ·  firmware {d['firmware']}  ·  {d['build']}")
    print(f"Mode affiché     M{d['mode'] + 1} « {names[d['mode']]} »" + ("" if d["mode"] == d["current_mode"]
          else f"   (mode actif sur le clavier : M{d['current_mode'] + 1})"))
    print(f"Polling          {d['rate']} Hz")
    print(f"Tick rate        {d['tick'] or 'aucun'}")
    print(f"Anti-rebond      {d['stabilization']}")
    print(f"Mode Tachyon     {on(d['tachyon'])}")
    print(f"Verrou Win       {on(d['lock_win'])}   Alt+Tab {on(d['lock_alt_tab'])}   Alt+F4 {on(d['lock_alt_f4'])}")
    print(f"Veille           {str(d['sleep']) + ' min' if d['sleep'] else 'jamais'}")
    for kind, v in d["lights"].items():
        color = "arc-en-ciel" if v["rainbow"] else v["color"]
        print(f"Éclairage {kind:<5}  effet {v['effect']}, luminosité {v['brightness']} %, vitesse {v['speed']}, {color}")


def main(argv=None):
    global _debug
    ap = argparse.ArgumentParser(prog="nuphy", description="Claviers NuPhy HE (Air75 HE…) : tous les réglages du driver web")
    ap.add_argument("--debug", action="store_true", help="affiche chaque trame envoyée et reçue")
    ap.add_argument("-m", "--mode", type=int, choices=(1, 2, 3), help="mode à régler (défaut : le mode actif)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="modèle, firmware, mode actif")
    p = sub.add_parser("show", help="réglages du mode")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("keys", help="carte des touches d'une couche")
    p.add_argument("-l", "--layer", type=int, default=0, help="0 = base, 1 = Fn, 2, 3")
    p.add_argument("--default", action="store_true", help="carte d'usine")
    p.add_argument("--names", action="store_true", help="noms des touches physiques")
    p = sub.add_parser("codes", help="codes de touches assignables")
    p.add_argument("filter", nargs="?", default="")
    p = sub.add_parser("key", help="voir ou réassigner une touche (CODE = default pour l'usine)")
    p.add_argument("key")
    p.add_argument("code", nargs="?")
    p.add_argument("-l", "--layer", type=int, default=0)
    p = sub.add_parser("layer-reset", help="remettre une couche d'usine")
    p.add_argument("-l", "--layer", type=int, required=True)
    p = sub.add_parser("trigger", help="point d'actionnement, rapid trigger, zones mortes")
    p.add_argument("keys", nargs="?", help="all ou w,a,s,d")
    p.add_argument("--ap", type=_mm, help="point d'actionnement (mm)")
    p.add_argument("--rt", choices=("off", "on", "continu"), help="rapid trigger")
    p.add_argument("--press", type=_mm, help="sensibilité à l'appui (mm)")
    p.add_argument("--release", type=_mm, help="sensibilité au relâchement (mm)")
    p.add_argument("--top", type=_mm, help="zone morte haute (mm)")
    p.add_argument("--bottom", type=_mm, help="zone morte basse (mm)")
    p.add_argument("--switch", type=int, help="type de switch (voir « nuphy switches »)")
    sub.add_parser("switches", help="switches proposés pour ce clavier")
    p = sub.add_parser("light", help="éclairage d'une zone")
    p.add_argument("zone", nargs="?", choices=("main", "side", "loop"))
    p.add_argument("--effect", help="nom, numéro ou off")
    p.add_argument("--brightness", type=int)
    p.add_argument("--speed", type=int, choices=range(5))
    p.add_argument("--direction", type=int, choices=(0, 1))
    p.add_argument("--color", help="rrggbb")
    p.add_argument("--rainbow", type=_on_off)
    p = sub.add_parser("color", help="couleur par touche (effet Custom)")
    p.add_argument("keys")
    p.add_argument("color")
    p = sub.add_parser("rate", help="polling USB : 1000, 2000, 4000, 8000 Hz")
    p.add_argument("hz", type=int)
    p = sub.add_parser("tick", help="tick rate : 0, 30 ou 60")
    p.add_argument("tick", type=int)
    p = sub.add_parser("debounce", help="anti-rebond : low, mid, high")
    p.add_argument("level", choices=("low", "mid", "high"))
    p = sub.add_parser("lock", help="bloquer win, alt-tab ou alt-f4")
    p.add_argument("what", choices=("win", "alt-tab", "alt-f4"))
    p.add_argument("state", type=_on_off)
    p = sub.add_parser("tachyon", help="mode Tachyon (latence minimale, LED éteintes)")
    p.add_argument("state", type=_on_off)
    p = sub.add_parser("sleep", help="veille auto en minutes (5-127, 0 = jamais)")
    p.add_argument("minutes", type=int)
    p = sub.add_parser("rename", help="renommer un mode")
    p.add_argument("name")
    p = sub.add_parser("adv", help="touches avancées : list, mt, toggle, dks, rs, socd, ht, remove")
    p.add_argument("what", choices=("list", "mt", "toggle", "dks", "rs", "socd", "ht", "remove"))
    p.add_argument("args", nargs="*")
    p.add_argument("-l", "--layer", type=int, default=0)
    p.add_argument("--ms", type=int, default=170, help="mt : délai de maintien")
    p.add_argument("--rule", default="last", help="socd : last, neutral, a, b")
    p.add_argument("--one-way", action="store_true", help="ht : la 1re touche déclenche la 2e seulement")
    p.add_argument("--travel", type=_mm, default=0.5, help="ht : course de relâchement (mm)")
    p.add_argument("--points", default="1,3,3,1", help="dks : 4 points en mm")
    p = sub.add_parser("macro", help="macros : list, set, delete, bind")
    p.add_argument("what", choices=("list", "set", "delete", "bind"))
    p.add_argument("args", nargs="*")
    p.add_argument("-l", "--layer", type=int, default=0)
    p.add_argument("--loop", default="1", help="bind : 1-254, hold (tant qu'appuyé) ou toggle")
    p = sub.add_parser("gamepad", help="manette virtuelle")
    p.add_argument("--type", choices=("off", "classic", "xbox"))
    p.add_argument("--angle", type=int)
    for flag in GAMEPAD_FLAGS:
        p.add_argument("--" + flag.replace("_", "-"), type=_on_off)
    sub.add_parser("calibrate", help="recalibrer les switches")
    sub.add_parser("monitor", help="course des touches en direct")
    p = sub.add_parser("export", help="exporter au format du driver web")
    p.add_argument("file")
    p.add_argument("--all", action="store_true", help="les trois modes")
    p = sub.add_parser("import", help="importer un fichier du driver web")
    p.add_argument("file")
    p.add_argument("--only", help="parmi : " + ",".join(Keyboard.PARTS))
    p.add_argument("--switches", action="store_true", help="prendre aussi le type de switch du fichier")
    p = sub.add_parser("reset", help="remettre un mode d'usine, ou tout le clavier")
    p.add_argument("--factory", action="store_true")
    p.add_argument("--yes", action="store_true")
    p = sub.add_parser("raw", help="lecture brute (diagnostic)")
    p.add_argument("command", type=lambda v: int(v, 0))
    p.add_argument("addr", type=lambda v: int(v, 0))
    p.add_argument("size", type=lambda v: int(v, 0))
    a = ap.parse_args(argv)
    _debug = _debug or a.debug
    try:
        kb = open_keyboard()
        with kb:
            return _run(kb, a) or 0
    except NuphyError as e:
        print(f"erreur : {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"erreur : {e}", file=sys.stderr)
        return 1


def _run(kb: Keyboard, a) -> int:
    mode = a.mode - 1 if a.mode else None
    m = kb._mode(mode)
    layer = kb.layer_of(mode, a.layer) if getattr(a, "layer", None) is not None else None
    c = a.cmd
    if c == "info":
        names = kb.mode_names()
        print(f"{kb.model.name}  ·  firmware {kb.version_text}  ·  {kb.build}")
        print(f"{kb.node.path}  ({kb.node.vid:04x}:{kb.node.pid:04x})")
        print(f"Mode actif : M{kb.mode + 1} « {names[kb.mode]} »   (" + ", ".join(f"M{i + 1} {n}" for i, n in enumerate(names)) + ")")
        if "tested" not in kb.model.raw:
            print("Note : ce modèle partage le protocole de l'Air75 HE mais n'a pas été testé ici.")
    elif c == "show":
        d = describe(kb, mode)
        print(json.dumps(d, ensure_ascii=False)) if a.json else _show(d)
    elif c == "keys":
        if a.names:
            lay, pos = kb.model.layout, kb.index_map()
            print(_draw(kb, lambda i: "fn" if lay[pos[i]][4] == KEYCODES["KC_FN1"] else code_word(lay[pos[i]][4])))
            return 0
        km = kb.default_keymap(layer) if a.default else kb.keymap(layer)
        print(_draw(kb, lambda i: key_label(km[i]) or "·"))
    elif c == "codes":
        f = a.filter.upper()
        for name in sorted({NAME_OF[v] for v in NAME_OF}):
            if f in name:
                print(f"{name[3:].lower():34} {code_label(KEYCODES.get(name, 0))}")
    elif c == "key":
        idx = kb.key_index(a.key)
        if a.code is None:
            k = kb.get_key(layer, idx)
            print(f"{kb.key_title(idx)} (#{idx}), couche {a.layer} : {key_label(k)}  [{_hex(k)}]")
        elif a.code.lower() in ("default", "defaut", "défaut", "reset"):
            kb.reset_key(layer, idx)
            print(f"{kb.key_title(idx)} : touche d'usine ({key_label(kb.get_key(layer, idx))})")
        else:
            code = parse_code(a.code)
            kb.set_key(layer, idx, code)
            print(f"{kb.key_title(idx)} → {code_label(code)}")
            if code in DANGEROUS:
                print("Attention : cette touche redémarre le clavier ou le met en mode mise à jour.")
    elif c == "layer-reset":
        kb.reset_layer(layer)
        print(f"Couche {a.layer} du mode M{m + 1} remise d'usine.")
    elif c == "switches":
        for sid in kb.model.switch_list(kb.version):
            name, total, step = kb.model.switch(sid)
            print(f"{sid:3}  {name}  (course {total:g} mm, pas RT {step:g} mm)")
    elif c == "trigger":
        rts = kb.rt_all(mode)
        change = any(v is not None for v in (a.ap, a.rt, a.press, a.release, a.top, a.bottom, a.switch))
        if not change:
            idxs = _keys(kb, a.keys) if a.keys else None
            if idxs is None:
                print(_draw(kb, lambda i: f"{kb.rt_mm(rts[i])['actuation']:.2f}"
                            + {"normal": " ", "rt": "r", "rtc": "c"}[kb.rt_mm(rts[i])["mode"]]))
                print("(point d'actionnement en mm ; r = rapid trigger, c = rapid trigger continu)")
            else:
                for i in idxs:
                    v = kb.rt_mm(rts[i])
                    print(f"{kb.key_title(i):10} {v['actuation']:.2f} mm  {v['mode']:6} appui {v['press']:.2f}  "
                          f"relâchement {v['release']:.2f}  zones mortes {v['top_dz']:.2f}/{v['bottom_dz']:.2f}  {v['switch_name']}")
            return 0
        if not a.keys:
            raise NuphyError("quelles touches ? (all ou w,a,s,d)")
        rt_mode = {None: None, "off": "normal", "on": "rt", "continu": "rtc"}[a.rt]
        new = {i: kb.rt_from_mm(rts[i], switch=a.switch, actuation=a.ap, mode=rt_mode, press=a.press,
                                release=a.release, top_dz=a.top, bottom_dz=a.bottom) for i in _keys(kb, a.keys)}
        kb.set_rt({i: v for i, v in new.items() if v != rts[i]}, mode)
        print(f"{len(new)} touche(s) réglée(s).")
    elif c == "light":
        lights = kb.lights(mode)
        if a.zone is None or all(getattr(a, k) is None for k in ("effect", "brightness", "speed", "direction", "color", "rainbow")):
            for kind, v in lights.items():
                if a.zone in (None, kind):
                    names = {mm[0]: mm[1] for mm in kb.model.light_modes[kind]}
                    print(f"{kind}: effet {names.get(v['effect'], v['effect'])}, luminosité {v['brightness']} %, vitesse {v['speed']}, "
                          f"{'arc-en-ciel' if v['rainbow'] else v['color']}"
                          + (f", sens {v['direction']}" if v["direction"] is not None else ""))
                    print("   effets : " + ", ".join(mm[1].lower().replace(" ", "-") for mm in kb.model.light_modes[kind]))
            return 0
        effect = None
        if a.effect is not None:
            table = {mm[1].lower().replace(" ", "-"): mm[0] for mm in kb.model.light_modes.get(a.zone, [])}
            effect = LIGHT_OFF if a.effect.lower() == "off" else int(a.effect) if a.effect.isdigit() else table.get(a.effect.lower())
            if effect is None:
                raise NuphyError("effet inconnu : " + ", ".join(table))
        kb.set_light(a.zone, mode, effect=effect, brightness=a.brightness, speed=a.speed, direction=a.direction,
                     color=a.color, rainbow=a.rainbow)
        print("Éclairage réglé.")
    elif c == "color":
        rgb = parse_color(a.color)
        idxs = _keys(kb, a.keys)
        kb.set_key_colors({i: rgb for i in idxs}, mode)
        print(f"{len(idxs)} touche(s) colorée(s) (visible avec l'effet « custom »).")
    elif c == "rate":
        kb.set_rate(a.hz, mode)
        print(f"Polling : {a.hz} Hz (le clavier redémarre pour l'appliquer).")
    elif c == "tick":
        kb.set_tick(a.tick, mode)
        print(f"Tick rate : {a.tick or 'aucun'}")
    elif c == "debounce":
        kb.set_stabilization(a.level, mode)
        print(f"Anti-rebond : {a.level}")
    elif c == "lock":
        kb.set_flag("lock_" + a.what.replace("-", "_"), a.state, mode)
        print(f"{a.what} : {'bloqué' if a.state else 'libre'}")
    elif c == "tachyon":
        kb.set_tachyon(a.state, mode)
        print(f"Mode Tachyon : {'activé' if a.state else 'désactivé'}")
    elif c == "sleep":
        kb.set_sleep(a.minutes)
        print("Veille : " + (f"{a.minutes} min" if a.minutes else "jamais"))
    elif c == "rename":
        kb.set_mode_name(m, a.name)
        print(f"M{m + 1} s'appelle « {a.name} »")
    elif c == "adv":
        return _adv(kb, a, mode, layer)
    elif c == "macro":
        return _macro(kb, a, mode, layer)
    elif c == "gamepad":
        change = {"type": a.type, "angle": a.angle, **{f: getattr(a, f) for f in GAMEPAD_FLAGS}}
        if any(v is not None for v in change.values()):
            kb.set_gamepad(mode, **change)
        for k, v in kb.gamepad(mode).items():
            print(f"{k:14} {v}")
    elif c == "calibrate":
        return _calibrate(kb)
    elif c == "monitor":
        return _monitor(kb)
    elif c == "export":
        data = kb.export_all() if a.all else {"cfg": kb.export_mode(mode), "deviceName": kb.model.name.replace(" ", "_")}
        Path(a.file).write_text(json.dumps(data))
        print(f"Exporté dans {a.file} (" + ("3 modes" if a.all else f"mode M{m + 1}") + ")")
    elif c == "import":
        data = json.loads(Path(a.file).read_text())
        cfg = data.get("cfg") if isinstance(data, dict) else None
        parts = tuple(a.only.split(",")) if a.only else Keyboard.PARTS
        if bad := [x for x in parts if x not in Keyboard.PARTS]:
            raise NuphyError(f"partie inconnue : {bad[0]}")
        if isinstance(cfg, list):
            if a.mode:
                cfg = [(m, cfg[m])] if m < len(cfg) else []
            else:
                cfg = list(enumerate(cfg))[:MODES]
        elif isinstance(cfg, dict):
            cfg = [(m, cfg)]
        if not cfg:
            raise NuphyError("ce fichier n'est pas un export du driver")
        for target, one in cfg:
            kb.import_mode(one, target, parts, keep_switches=not a.switches)
            print(f"Mode M{target + 1} importé.")
    elif c == "reset":
        if not a.yes:
            raise NuphyError("ajoute --yes pour confirmer : " + ("tout le clavier revient d'usine" if a.factory
                             else f"touches, touches avancées, macros et nom du mode M{m + 1} reviennent d'usine"))
        if a.factory:
            kb.factory_reset()
            print("Clavier remis d'usine.")
        else:
            kb.reset_mode(mode)
            print(f"Mode M{m + 1} remis d'usine.")
    elif c == "raw":
        if a.command not in READ_ONLY:
            raise NuphyError("seules les commandes de lecture sont permises ici")
        data = kb.read(a.command, a.addr, a.size)
        for at in range(0, len(data), 16):
            print(f"{a.addr + at:5}  {_hex(data[at:at + 16])}")
    return 0


def _adv(kb: Keyboard, a, mode, layer) -> int:
    w, args = a.what, a.args
    if w == "list":
        items = kb.advanced(mode)
        for it in items:
            head = f"couche {it['layer']}  {kb.key_title(it['index']):10} {it['kind']:5}"
            if it["kind"] == "MT":
                print(f"{head} tap {code_label(it['tap'])}, maintien {code_label(it['hold'])}, {it['ms']} ms")
            elif it["kind"] == "TGL":
                print(f"{head} {code_label(it['key'])}")
            elif it["kind"] == "DKS":
                d = it["dks"]
                print(head + (" vide" if d is None else " points " + "/".join(f"{p / 10:g}" for p in d.points) + " mm : "
                      + ", ".join(f"{code_label(k)}={''.join(r)}" for k, r in zip(d.keys, d.phases) if k)))
            else:
                extra = f", course {it['travel']:g} mm" if it["kind"] == "HT" else ""
                print(f"{head} avec {kb.key_title(it['other'])}{extra}")
        if not items:
            print("Aucune touche avancée dans ce mode.")
        return 0
    need = {"mt": 3, "toggle": 2, "dks": 2, "rs": 2, "socd": 2, "ht": 2, "remove": 1}[w]
    if len(args) < need:
        raise NuphyError({"mt": "adv mt TOUCHE TAP MAINTIEN", "toggle": "adv toggle TOUCHE CODE",
                          "dks": "adv dks TOUCHE CODE:phases [CODE:phases…]  (4 phases : t bref, h tenue, r relâchée, . rien)",
                          "rs": "adv rs TOUCHE1 TOUCHE2", "socd": "adv socd TOUCHE1 TOUCHE2",
                          "ht": "adv ht TOUCHE1 TOUCHE2", "remove": "adv remove TOUCHE"}[w])
    idx = kb.key_index(args[0])
    if w == "mt":
        kb.set_mod_tap(layer, idx, parse_code(args[1]), parse_code(args[2]), a.ms)
    elif w == "toggle":
        kb.set_toggle(layer, idx, parse_code(args[1]))
    elif w == "dks":
        points = [round(_mm(x) * 10) for x in a.points.split(",")]
        keys, phases = [], []
        for spec in args[1:5]:
            name, _, ph = spec.partition(":")
            keys.append(parse_code(name))
            phases.append((ph or "t...").ljust(4, ".")[:4])
        while len(keys) < 4:
            keys.append(0)
            phases.append("....")
        kb.set_dks(layer, idx, Dks(points, keys, phases))
    elif w == "rs":
        kb.set_rapid_shift(layer, idx, kb.key_index(args[1]))
    elif w == "socd":
        kb.set_socd(layer, idx, kb.key_index(args[1]), a.rule)
    elif w == "ht":
        kb.set_hyper_tap(layer, idx, kb.key_index(args[1]), both=not a.one_way, travel_mm=a.travel)
    elif w == "remove":
        kb.reset_key(layer, idx)
    print("Fait.")
    return 0


def _macro(kb: Keyboard, a, mode, layer) -> int:
    w, args = a.what, a.args
    if w == "list":
        macros = kb.macros(mode)
        for i, acts in enumerate(macros):
            if acts:
                print(f"M{i + 1:<3} {format_macro(acts)}")
        used = sum(len(x) for x in macros)
        print(f"{sum(1 for x in macros if x)} macro(s), {used} action(s) sur {(SZ_MACRO - MACRO_TABLE) // 4}.")
        return 0
    if not args or not args[-1 if w == "bind" else 0].lstrip("mM").isdigit():
        raise NuphyError("macro set N \"+lctl c -lctl\" | macro delete N | macro bind TOUCHE N")
    if w == "set":
        if len(args) < 2:
            raise NuphyError("macro set N \"+lctl +c 20 -c -lctl\"")
        kb.set_macro(int(args[0].lstrip("mM")) - 1, parse_macro(" ".join(args[1:])), mode)
    elif w == "delete":
        kb.set_macro(int(args[0].lstrip("mM")) - 1, [], mode)
    elif w == "bind":
        if len(args) < 2:
            raise NuphyError("macro bind TOUCHE N [--loop 3|hold|toggle]")
        loop = {"hold": MACRO_HOLD, "toggle": MACRO_TOGGLE}.get(a.loop.lower())
        if loop is None:
            if not a.loop.isdigit() or not 1 <= int(a.loop) <= 254:
                raise NuphyError("--loop : 1 à 254, hold ou toggle")
            loop = int(a.loop)
        kb.bind_macro(layer, kb.key_index(args[0]), int(args[1].lstrip("mM")) - 1, loop)
    print("Fait.")
    return 0


def _calibrate(kb: Keyboard) -> int:
    print("Calibration : pose le clavier à plat, loin du métal, puis enfonce à fond chaque touche,\n"
          "plusieurs fois. Ctrl+C pour terminer.", flush=True)
    done, total = set(), len(kb.index_map())
    kb.calibration_start()
    try:
        beat = time.monotonic()
        while len(done) < total:
            for ev in kb.poll(0.2):
                if ev["event"] == "key" and ev["state"] == "done" and ev["index"] is not None and ev["index"] not in done:
                    done.add(ev["index"])
                    print(f"\r{len(done)}/{total}  {kb.key_title(ev['index']):12}", end="", flush=True)
            if time.monotonic() - beat > 2:            # the firmware leaves calibration without this
                kb.calibration_start()
                beat = time.monotonic()
    except KeyboardInterrupt:
        pass
    finally:
        kb.calibration_end()
    print(f"\nCalibration terminée : {len(done)}/{total} touches.")
    return 0


def _monitor(kb: Keyboard) -> int:
    print("Course des touches en direct (Ctrl+C pour arrêter).", flush=True)
    kb.live(True)
    try:
        while True:
            for ev in kb.poll(0.2):
                if ev["event"] == "key":
                    name = kb.key_title(ev["index"]) if ev["index"] is not None else code_label(ev["code"])
                    print(f"{name:12} {ev['travel']:.2f} mm", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        kb.live(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
