#!/usr/bin/env python3
"""A stand-in for an Air75 HE, for the tests and for the demo mode (NUPHY_DEMO=1).

It answers the configuration protocol the way the keyboard does: same frames,
same memory regions, and for its starting state the captures that NuPhy ships
inside the web driver for its own demo keyboard (firmware 1.0.6: identity,
function blocks, factory key matrix, LED colours). Unlike that demo it really
stores what is written, so a write can be read back.

NUPHY_DEMO=/some/file keeps the state in that file between runs.
"""
from __future__ import annotations

import base64
import os
import pickle
import socket
import threading
import zlib

_BLOB = """
eNrtlj1TE1EUhp+NSdyAHy8gqPgVURQUYbMYvoxCUBhFwICDY604dmqrlY22OmNJ4YyFBZ2FNqi9hWPnD9BKq8sfcL0hAgEiZLAR
2DNndu/dfc/Ze/fMvc+d3sajOiDuDN65lfQ7k77np1tSXT1+uifdzZrmRKdnXq1iOaZnnKdOZbwp4jgEi4F+xMnftIblovl4KiOT
y+Ino+XFu4X49X5fNIsecU5kxHlxQfSKPpEV/eKiuCQGzHXEFTEs0uKwOCKS4qhoEMfEcdEoToiT4oxoFafEVXFa7BH1whW1Yq84
KPaJHaJa1Ig24YmUGBXdIirqxHaREBWiUuwUu8Ru0S7OiiYxpIgdzCFxQMTFfhETVbJTpUN0ii4lrWBcjMhBLorabov6MIFjRSIn
xsS1ILRA4D3Au2//yqJ77/A+473F+4b3E++HfThYLCjTTXotgYsZZx2Z/4T/KmoHEdOJ6Zjv+qX0tv722oZpx2QIqz9X/43rJrW0
W42pmW/r7/Wvw1RhasP6b/T6/6OH1c/Xf0vxPxbyf9PyP14G/2Mh/7c0/2Mh/0P+h/xfwv/C/u+9jJgxvC/0TZXY/83EhuV/dO4I
YPn/2K7/RMj/Euu/jPPfDf57/leUwf9EyP8tzf9EyP+Q/yH/i8x1v2fqv95t+PSi+cPHtsk33ZdfZ7qmelPPs/6TbOrhgHdvqPX2
aMvNXPPwSGOm3+pn66sW9M/ep0uGWH1BXJx8pbiQfCHz6mKbuZy0Vlac047Wiq1m2YzybycmgtkgyAazbpB1rTZY+YRNbr8BjIqT
hA==
"""

MODES, PACKET = 3, 64
SIZES = {"info": 64, "base": 32, "func": 192, "keys": 6144, "leds": 512}
# set command -> get command that reads the same memory
PAIRS = {0x06: 0x05, 0x09: 0x08, 0x0B: 0x0A, 0x0D: 0x0C, 0x11: 0x10, 0xA1: 0xA0, 0xA3: 0xA2,
         0xA5: 0xA4, 0xA7: 0xA6, 0xF2: 0xF1}
ECHO = (0x01, 0x02, 0xA8, 0xA9)


def _factory() -> dict:
    raw = zlib.decompress(base64.b64decode("".join(_BLOB.split())))
    part, at = {}, 0
    for name, size in SIZES.items():
        part[name] = raw[at:at + size]
        at += size
    func = bytearray(part["func"])
    for mode in range(MODES):
        func[mode * 64 + 7] &= ~0x08        # the capture was taken with live travel reports on
    return {
        "info": bytes(part["info"]),
        0x04: bytearray(part["base"].ljust(256, b"\0")),
        0x05: func,
        0x07: bytearray(part["keys"]),
        0x08: bytearray(part["keys"]),
        0x0A: bytearray(1536),
        0x0C: bytearray(6144),
        0x10: bytearray(768),
        0xA0: bytearray(bytes([0xA2, 0, 149, 0, 0, 0, 0, 0]) * 384),   # low-profile Jade Pro, 1.50 mm
        0xA2: bytearray(2304),
        0xA4: bytearray(768),
        0xA6: bytearray(384),
        0xAA: bytearray(512),
        0xDE: bytearray(part["leds"]),
        0xF1: bytearray(1024),
    }


class Sim:
    """The keyboard's side of the conversation."""

    def __init__(self, state_file: str | None = None):
        self.state_file = state_file
        self.mem = _factory()
        self.log: list[tuple] = []          # (cmd, addr, length, data) of every valid request
        self.bootloader = False             # set if a frame that enters firmware update ever arrives
        self.quick = 0
        self.calibrating = False
        self.mute = 0                       # swallow that many requests (to test retries)
        self.corrupt = 0                    # answer that many requests with a bad checksum
        if state_file and os.path.exists(state_file):
            try:
                with open(state_file, "rb") as f:
                    self.mem.update(pickle.load(f))
            except (OSError, pickle.PickleError, EOFError):
                pass

    def save(self):
        if self.state_file:
            with open(self.state_file, "wb") as f:
                pickle.dump({k: v for k, v in self.mem.items() if k != 0x07}, f)

    @staticmethod
    def _reply(cmd, length, addr, data=b"") -> bytes:
        p = bytearray(PACKET)
        p[0], p[1], p[4], p[5], p[6] = 0xAA, cmd, length, addr & 0xFF, addr >> 8
        p[8:8 + len(data)] = data
        p[3] = sum(p[4:]) & 0xFF
        return bytes(p)

    def handle(self, frame: bytes) -> list[bytes]:
        """One output report (with its leading report id) -> input reports."""
        if len(frame) != PACKET + 1 or frame[0] != 0 or self.bootloader:
            return []
        p = frame[1:]
        if p[0] != 0x55 or p[2] != 0 or sum(p[4:]) & 0xFF != p[3]:
            return []
        cmd, length, addr = p[1], p[4], p[5] | p[6] << 8
        data = bytes(p[8:8 + length])
        if self.mute:
            self.mute -= 1
            return []
        self.log.append((cmd, addr, length, data))
        if self.corrupt:
            self.corrupt -= 1
            bad = bytearray(self._reply(cmd, length, addr, data))
            bad[3] ^= 0x55
            return [bytes(bad)]
        if cmd == 0x03:
            return [self.mem["info"]]
        if cmd in ECHO:
            self.quick += {0x01: 1, 0x02: -1}.get(cmd, 0)
            if cmd in (0xA8, 0xA9):
                self.calibrating = cmd == 0xA8
            return [self._reply(cmd, length, addr)]
        if cmd == 0xEE:
            keep = self.mem[0x07]
            self.mem = _factory()
            self.mem[0x07] = keep
            return [self._reply(cmd, 0, 0), bytes([0xA2]).ljust(PACKET, b"\0")]
        if cmd in PAIRS:
            region = self.mem[PAIRS[cmd]]
            if length > 56 or addr + length > len(region):
                return []
            if cmd == 0x06 and addr % 64 == 0 and length and data[0] == 1:
                self.bootloader = True                   # what the driver sends to flash firmware
                return []
            region[addr:addr + length] = data
            if cmd == 0x06:                               # the firmware reports setting changes
                return [self._reply(cmd, length, addr, data)]
            return [self._reply(cmd, length, addr, data)]
        region = self.mem.get(cmd)
        if region is None or length > 56 or addr + length > len(region):
            return []
        return [self._reply(cmd, length, addr, bytes(region[addr:addr + length]))]

    # -- what a person at the keyboard would cause

    def key_event(self, key3, travel: int, state: int = 0) -> bytes:
        e = bytearray(PACKET)
        e[0], e[1:4], e[6], e[7], e[10] = 0xA0, bytes(key3), travel >> 8, travel & 0xFF, state
        return bytes(e)

    def mode_event(self, mode: int, fn_layer: int = 0) -> bytes:
        self.mem[0x04][0] = mode
        return bytes([0xA1, fn_layer, mode]).ljust(PACKET, b"\0")


class Link:
    """A socket pair that behaves like a hidraw node, with the Sim behind it."""

    def __init__(self, sim: Sim):
        self.sim = sim
        self.host, self._dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                frame = self._dev.recv(256)
            except OSError:
                return
            if not frame:
                return
            for reply in self.sim.handle(frame):
                try:
                    self._dev.send(reply)
                except OSError:
                    return

    def inject(self, report: bytes):
        """An event the keyboard sends on its own."""
        self._dev.send(report)

    def fileno(self) -> int:
        return self.host.fileno()

    def close(self):
        self.sim.save()
        for s in (self.host, self._dev):
            try:
                s.close()
            except OSError:
                pass


_shared: Sim | None = None


def spawn() -> Link:
    """Demo keyboard: one per process, so its state survives between openings."""
    global _shared
    if _shared is None:
        target = os.environ.get("NUPHY_DEMO", "")
        _shared = Sim(target if os.sep in target else None)
    return Link(_shared)
