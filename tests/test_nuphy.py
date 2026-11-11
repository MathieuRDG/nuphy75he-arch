#!/usr/bin/env python3
"""Tests against the simulated Air75 HE (nuphy_sim) and against reference
vectors produced by the web driver's own serialisers.

    python3 -m unittest discover -s tests -v
"""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import nuphy  # noqa: E402
import nuphy_sim  # noqa: E402
from nuphy import Cmd, Dks, KeyRT, MacroAction, NuphyError  # noqa: E402

VEC = json.loads((ROOT / "tests/vendor_vectors.json").read_text())
KC = nuphy.KEYCODES


def frames(sim, cmd):
    return [(addr, data) for c, addr, _, data in sim.log if c == cmd]


class Base(unittest.TestCase):
    def setUp(self):
        self._state = tempfile.TemporaryDirectory()
        self.addCleanup(self._state.cleanup)
        nuphy.STATE_DIR = Path(self._state.name)
        self.sim = nuphy_sim.Sim()
        self.link = nuphy_sim.Link(self.sim)
        self.kb = nuphy.Keyboard(nuphy.Node("sim", nuphy.VID, 0x6120, "sim"), fd=self.link.fileno())
        self.kb.TIMEOUT = 0.05
        self.kb.__enter__()
        self.addCleanup(self.link.close)
        self.addCleanup(self.kb.__exit__, None, None, None)
        self.kb.identify()
        self.sim.log.clear()

    def idx(self, name):
        return self.kb.key_index(name)


class Vectors(unittest.TestCase):
    """Byte-for-byte agreement with the driver's own code."""

    def test_keycodes(self):
        for name, code, raw, back in VEC["keys"]:
            if 0x7E00 <= code <= 0x7EFF or code in (219, 220):
                with self.assertRaises(NuphyError):
                    nuphy.code_bytes(code)
                continue
            self.assertEqual(list(nuphy.code_bytes(code)), raw, name)
            self.assertEqual(nuphy.bytes_code(raw), back, name)

    def test_rapid_trigger_records(self):
        for fields, raw in VEC["rt"]:
            rt = KeyRT(*fields)
            self.assertEqual(list(rt.encode(0)), raw)
            self.assertEqual(KeyRT.decode(raw), rt)
            self.assertEqual(rt.encode(5)[0], raw[0] | 0xA0)

    def test_dks(self):
        letters = {0: ".", 1: "t", 5: "h", 7: "h", 3: "r"}
        for points, keys, segs, raw in VEC["dks"]:
            phases = ["".join(letters[v] for v in row) for row in segs]
            used = [any(v for v in row) for row in segs]
            mine = Dks(points, [k if u else 0 for k, u in zip(keys, used)], phases).encode()
            for k in range(4):
                at = 4 + k * 5
                self.assertEqual(list(mine[at + 3:at + 5]), raw[at + 3:at + 5], (phases[k], k))
                if used[k]:
                    self.assertEqual(list(mine[at:at + 3]), raw[at:at + 3])
            back = Dks.decode(raw)
            self.assertEqual(back.points, points)
            self.assertEqual(back.phases, phases)
            self.assertEqual(back.keys, keys)

    def test_light_segments(self):
        names = {"mode": "effect", "isRGB": "rainbow", "colorIndex": "color_index"}
        for kind, part, segs, full, whole in VEC["light"]:
            mine = nuphy.Keyboard.light_segments(kind, **{names.get(k, k): v for k, v in part.items()})
            self.assertEqual([[o, list(d)] for o, d in mine], segs, (kind, part))
            everything = nuphy.Keyboard.light_segments(kind, **{names.get(k, k): v for k, v in full.items()})
            self.assertEqual([[o, list(d)] for o, d in everything], [whole])


class Protocol(Base):
    def test_frames_match_the_driver(self):
        # Frames the driver's createCommand produces for the same requests.
        self.assertEqual(self.kb.frame(Cmd.GET_BASE, 16)[:8].hex(" "), "55 04 00 10 10 00 00 00")
        self.assertEqual(self.kb.frame(Cmd.GET_FUNC, 54, 64)[:8].hex(" "), "55 05 00 76 36 40 00 00")
        self.assertEqual(self.kb.frame(Cmd.SET_FUNC, 1, 73, b"\x25")[:9].hex(" "), "55 06 00 6f 01 49 00 00 25")
        self.assertEqual(self.kb.frame(Cmd.SET_FUNC, 2, 72, b"\xff\x00")[:10].hex(" "), "55 06 00 49 02 48 00 00 ff 00")
        self.kb.set_light("main", 1, brightness=37)
        self.assertEqual(frames(self.sim, Cmd.SET_FUNC), [(73, b"\x25")])

    def test_identity(self):
        self.assertEqual((self.kb.version, self.kb.version_text, self.kb.mode), (106, "1.0.6", 1))
        self.assertEqual(self.kb.build, "Feb 27 2025,18:25:59")
        self.assertEqual(self.kb.model.name, "Air75 HE")

    def test_version_reads_like_the_driver(self):
        for raw, want in ((b"\x06\x01", 106), (b"\x0a\x01", 10), (b"\x23\x01", 123), (b"\x04\x02", 204)):
            self.sim.mem["info"] = self.sim._reply(3, 22, 0, raw + b"Feb 27 2025")
            self.assertEqual(self.kb.identify().version, want)

    def test_fast_mode_is_optional(self):
        real = self.sim.handle
        self.sim.handle = lambda frame: [] if frame[2] in (1, 2) else real(frame)   # firmware without it
        self.assertEqual(len(self.kb.export_mode(1)["k"]), 2732)

    def test_every_key_is_placed(self):
        m = self.kb.index_map()
        self.assertEqual(len(m), 84)
        self.assertEqual(sorted(m.values()), list(range(84)))
        self.assertEqual(self.idx("esc"), 0)
        self.assertEqual(self.kb.key_title(self.idx("fn")), "Fn")
        self.assertEqual(self.idx("space"), self.idx("spc"))

    def test_retry_then_give_up(self):
        self.sim.mute = 2
        self.assertEqual(len(self.kb.read(Cmd.GET_FUNC, 0, 54)), 54)
        self.sim.mute = 10
        with self.assertRaises(NuphyError):
            self.kb.read(Cmd.GET_FUNC, 0, 54)
        self.sim.mute = 0
        self.sim.corrupt = 10
        with self.assertRaisesRegex(NuphyError, "somme de contrôle"):
            self.kb.read(Cmd.GET_FUNC, 0, 54)

    def test_events_do_not_confuse_replies(self):
        self.link.inject(self.sim.mode_event(2))
        self.assertEqual(len(self.kb.read(Cmd.GET_KEYS, 0, 512)), 512)
        self.assertIn({"event": "state", "fn_layer": 0, "mode": 2}, self.kb.poll(0.01))
        self.assertEqual(self.kb.mode, 2)

    def test_bootloader_frame_is_never_sent(self):
        for addr in (0, 1, 3, 64, 130):
            with self.assertRaises(NuphyError):
                self.kb.write(Cmd.SET_FUNC, addr, b"\x01")
        with self.assertRaises(NuphyError):
            self.kb.write(Cmd.SET_FUNC, 60, bytes(8))
        self.assertFalse(self.sim.bootloader)
        self.assertEqual(frames(self.sim, Cmd.SET_FUNC), [])

    def test_failed_write_is_reported(self):
        self.sim.mem[0x08] = nuphy_sim._factory()[0x08]

        class Deaf(bytearray):                       # memory that ignores writes
            def __setitem__(self, k, v):
                pass
        self.sim.mem[0x08] = Deaf(self.sim.mem[0x08])
        with self.assertRaisesRegex(NuphyError, "pas pris"):
            self.kb.set_key(4, self.idx("a"), KC["KC_B"])


class Keys(Base):
    def test_remap_and_reset(self):
        a = self.idx("a")
        self.kb.set_key(4, a, KC["KC_B"])
        self.assertEqual(frames(self.sim, Cmd.SET_KEYS), [(4 * 512 + a * 3, bytes([0x10, 0, 5]))])
        self.assertEqual(self.kb.keymap(4)[a], bytes([0x10, 0, 5]))
        self.kb.set_key(4, a, KC["KC_LCTL"])
        self.assertEqual(self.kb.get_key(4, a), bytes([0x10, 1, 0]))
        self.kb.set_key(4, a, KC["KC_VOL_UP"])
        self.assertEqual(self.kb.get_key(4, a), bytes([0x30, 0xE9, 0]))
        self.kb.reset_key(4, a)
        self.assertEqual(self.kb.get_key(4, a), bytes([0x10, 0, 4]))
        self.assertEqual(self.kb.keymap(4), self.kb.default_keymap(4))

    def test_other_layers_untouched(self):
        before = [self.kb.keymap(layer) for layer in range(12)]
        self.kb.set_key(5, self.idx("q"), KC["KC_F13"])
        after = [self.kb.keymap(layer) for layer in range(12)]
        self.assertEqual([i for i in range(12) if before[i] != after[i]], [5])

    def test_reset_layer(self):
        for name in ("a", "s", "d"):
            self.kb.set_key(4, self.idx(name), KC["KC_X"])
        self.kb.reset_layer(4)
        self.assertEqual(self.kb.keymap(4), self.kb.default_keymap(4))

    def test_parse_code(self):
        self.assertEqual(nuphy.parse_code("esc"), 41)
        self.assertEqual(nuphy.parse_code("KC_A"), 4)
        self.assertEqual(nuphy.parse_code("fn1"), 0xF0FF01)
        self.assertEqual(nuphy.parse_code("0x30e900"), KC["KC_VOL_UP"])
        with self.assertRaises(NuphyError):
            nuphy.parse_code("nope")
        with self.assertRaises(NuphyError):
            nuphy.parse_code("fn_battery_show")       # mechanical firmware only


class Trigger(Base):
    def test_factory_values_in_mm(self):
        v = self.kb.rt_mm(self.kb.rt_all()[0])
        self.assertEqual((v["actuation"], v["mode"], v["switch"], v["total"]), (1.5, "normal", 2, 3.3))
        self.assertEqual((v["top_dz"], v["bottom_dz"]), (0.0, 0.0))

    def test_set_and_read_back(self):
        rts = self.kb.rt_all(1)
        keys = [self.idx(n) for n in ("w", "a", "s", "d")]
        new = {i: self.kb.rt_from_mm(rts[i], actuation=0.3, mode="rtc", press=0.15, release=0.2, top_dz=0.1, bottom_dz=0.2)
               for i in keys}
        self.kb.set_rt(new, 1)
        for i in keys:
            v = self.kb.rt_mm(self.kb.rt_all(1)[i])
            self.assertEqual((v["actuation"], v["mode"], v["press"], v["release"], v["top_dz"], v["bottom_dz"]),
                             (0.3, "rtc", 0.15, 0.2, 0.1, 0.2))
        raw = self.sim.mem[0xA0][1024 + keys[0] * 8:1024 + keys[0] * 8 + 8]
        # switch 2 with the firmware's check flag, continuous RT, 0.30 mm -> 29, 0.15 -> 14, 0.20 -> 19
        self.assertEqual(list(raw), [0xA2, 2, 29, 0, 14, 9 << 1, 19, 19 << 1])
        untouched = self.kb.rt_all(1)[self.idx("q")]
        self.assertEqual(untouched, rts[self.idx("q")])
        self.assertEqual(self.kb.rt_all(0), rts)       # the other modes are not this one

    def test_neighbours_share_frames(self):
        rts = self.kb.rt_all(1)
        self.kb.set_rt({i: self.kb.rt_from_mm(rts[i], actuation=1.0) for i in range(20)}, 1)
        sent = frames(self.sim, Cmd.SET_RT)
        self.assertEqual([len(d) for _, d in sent], [56, 56, 48])
        self.assertEqual([a for a, _ in sent], [1024, 1024 + 56, 1024 + 112])

    def test_limits(self):
        rt = self.kb.rt_all()[0]
        for bad in (dict(actuation=0.05), dict(actuation=3.4), dict(press=2.5), dict(top_dz=0.6), dict(switch=9),
                    dict(mode="nope"), dict(actuation=3.3, bottom_dz=0.2)):
            with self.assertRaises(NuphyError, msg=bad):
                self.kb.rt_from_mm(rt, **bad)
        self.assertEqual(self.kb.rt_from_mm(rt, actuation=3.3).travel, 329)
        self.assertEqual(self.kb.rt_from_mm(rt, actuation=0.1).travel, 9)
        # a dead zone that passes the actuation point pulls it along, as the driver's slider does
        self.assertEqual(self.kb.rt_mm(self.kb.rt_from_mm(self.kb.rt_from_mm(rt, actuation=0.2), top_dz=0.4))["actuation"], 0.4)


class Advanced(Base):
    L = 4                                             # base layer of mode 2 (Windows)

    def test_mod_tap(self):
        caps = self.idx("caps")
        self.kb.set_mod_tap(self.L, caps, KC["KC_ESC"], KC["KC_LCTL"], 200)
        self.assertEqual(self.kb.get_key(self.L, caps), bytes([0x92, 0, 20]))
        self.assertEqual(bytes(self.sim.mem[0xA4][256:262]), bytes([0x10, 0, 41, 0x10, 1, 0]))
        item, = self.kb.advanced(1)
        self.assertEqual((item["kind"], item["tap"], item["hold"], item["ms"]), ("MT", 41, 224, 200))
        self.kb.set_mod_tap(self.L, caps, KC["KC_ESC"], KC["KC_LSFT"], 170)      # edit in place
        self.assertEqual(self.kb.get_key(self.L, caps), bytes([0x92, 0, 17]))
        self.kb.set_toggle(self.L, self.idx("tab"), KC["KC_W"])
        self.kb.set_mod_tap(self.L, self.idx("a"), KC["KC_A"], KC["KC_LALT"])
        self.assertEqual(self.kb.get_key(self.L, self.idx("a"))[1], 1)             # next free slot
        self.kb.reset_key(self.L, caps)
        self.assertEqual(bytes(self.sim.mem[0xA4][256:262]), bytes(6))             # slot given back
        self.assertEqual(self.kb.get_key(self.L, caps), bytes([0x10, 0, 57]))
        self.kb.set_mod_tap(self.L, self.idx("s"), KC["KC_S"], KC["KC_LGUI"])
        self.assertEqual(self.kb.get_key(self.L, self.idx("s"))[1], 0)             # and reused

    def test_slots_are_per_mode(self):
        self.kb.set_toggle(0, self.idx("a"), KC["KC_W"])
        self.kb.set_toggle(4, self.idx("a"), KC["KC_S"])
        self.assertEqual(bytes(self.sim.mem[0xA6][0:3]), bytes([0x10, 0, 26]))
        self.assertEqual(bytes(self.sim.mem[0xA6][128:131]), bytes([0x10, 0, 22]))
        self.assertEqual(self.kb.get_key(0, self.idx("a")), bytes([0x91, 0, 0]))

    def test_dks(self):
        d = Dks([10, 30, 30, 10], [KC["KC_A"], KC["KC_B"], 0, 0], ["hr..", "..t.", "....", "...."])
        self.kb.set_dks(self.L, self.idx("f"), d)
        self.assertEqual(self.kb.get_key(self.L, self.idx("f")), bytes([0x90, 0, 0]))
        item, = self.kb.advanced(1)
        self.assertEqual(item["dks"], d)
        with self.assertRaises(NuphyError):
            self.kb.set_dks(self.L, self.idx("g"), Dks([30, 10, 30, 10], [4, 0, 0, 0], ["t..."] + ["...."] * 3))

    def test_socd_pair(self):
        a, d = self.idx("a"), self.idx("d")
        self.kb.set_socd(self.L, a, d, "a")
        ka, kd = self.kb.get_key(self.L, a), self.kb.get_key(self.L, d)
        self.assertEqual((ka[0], ka[2], kd[0], kd[2]), (0x94, d, 0x94, a))
        mt = self.sim.mem[0xA4]
        self.assertEqual(bytes(mt[256 + ka[1] * 6:256 + ka[1] * 6 + 6]), bytes([0x10, 0, 4, 0x10, 0, 7]))
        self.assertEqual(bytes(mt[256 + kd[1] * 6:256 + kd[1] * 6 + 6]), bytes([0x10, 0, 7, 0x10, 0, 4]))
        rt = self.kb.rt_all(1)
        self.assertEqual((rt[a].priority, rt[d].priority), (0x10, 0x20))
        self.assertEqual(rt[a].travel, 149)
        self.kb.reset_key(self.L, a)                  # removing one side removes the pair
        self.assertEqual(self.kb.advanced(1), [])
        self.assertEqual(self.kb.keymap(self.L), self.kb.default_keymap(self.L))
        self.assertEqual(bytes(mt[256:256 + 12]), bytes(12))
        rt = self.kb.rt_all(1)
        self.assertEqual((rt[a].priority, rt[d].priority), (0, 0))

    def test_rapid_shift_and_hyper_tap(self):
        a, d, q, e = (self.idx(n) for n in "adqe")
        self.kb.set_rapid_shift(self.L, a, d)
        self.kb.set_hyper_tap(self.L, q, e, both=False, travel_mm=0.5)
        kinds = {(i["kind"], i["index"]) for i in self.kb.advanced(1)}
        self.assertEqual(kinds, {("RS", a), ("RS", d), ("HT", q)})
        kq = self.kb.get_key(self.L, q)
        self.assertEqual(bytes(self.sim.mem[0xA4][256 + kq[1] * 6:256 + kq[1] * 6 + 6]), bytes([0x10, 0, 20, 49, 0, 8]))
        self.assertEqual(self.kb.get_key(self.L, e), bytes([0x10, 0, 8]))
        self.kb.set_rapid_shift(self.L, a, q)         # re-pairing frees what it replaces
        kinds = {(i["kind"], i["index"]) for i in self.kb.advanced(1)}
        self.assertEqual(kinds, {("RS", a), ("RS", q)})
        self.assertEqual(self.kb.get_key(self.L, d), bytes([0x10, 0, 7]))

    def test_full_table(self):
        idxs = sorted(self.kb.index_map())
        for i in idxs[:32]:
            self.kb.set_toggle(self.L, i, KC["KC_A"])
        with self.assertRaisesRegex(NuphyError, "32"):
            self.kb.set_toggle(self.L, idxs[32], KC["KC_A"])
        self.kb.set_toggle(0, idxs[32], KC["KC_A"])   # another mode has its own table


class Macros(Base):
    def test_store_list_delete(self):
        copy = nuphy.parse_macro("+lctl +c 20 -c -lctl")
        self.kb.set_macro(0, copy, 1)
        self.kb.set_macro(4, nuphy.parse_macro("h i"), 1)
        got = self.kb.macros(1)
        self.assertEqual(got[0], copy)
        self.assertEqual(nuphy.format_macro(got[4]), "+h -h +i -i")
        self.assertEqual([i for i, m in enumerate(got) if m], [0, 4])
        area = self.sim.mem[0x0C][2048:4096]
        self.assertEqual(bytes(area[64:80]), bytes([10, 0, 0x41, 1, 20, 0, 0x42, 6, 10, 0, 0x02, 6, 10, 0, 0x81, 1]))
        free = 64 + 16 + 16
        self.assertEqual(bytes(area[free:free + 4]), bytes([0, 0, 0x80, 0]))
        self.assertEqual(int.from_bytes(area[0:2], "little"), 64)
        self.assertEqual(int.from_bytes(area[8:10], "little"), 80)
        self.assertEqual(int.from_bytes(area[2:4], "little"), free)    # unused macros point at free space
        self.kb.set_macro(0, [], 1)
        got = self.kb.macros(1)
        self.assertEqual([i for i, m in enumerate(got) if m], [4])
        self.assertEqual(nuphy.format_macro(got[4]), "+h -h +i -i")
        self.assertEqual(self.kb.macros(0), [[]] * 32)

    def test_bind(self):
        self.kb.set_macro(2, nuphy.parse_macro("a b"), 1)
        self.kb.bind_macro(4, self.idx("f12"), 2)
        self.assertEqual(self.kb.get_key(4, self.idx("f12")), bytes([0x70, 2, 1]))
        with self.assertRaisesRegex(NuphyError, "répéter"):
            self.kb.bind_macro(4, self.idx("f12"), 2, 3)          # loops arrive with firmware 1.0.8
        self.kb.version = 108
        self.kb.bind_macro(4, self.idx("f12"), 2, 3)
        self.assertEqual(self.kb.get_key(4, self.idx("f12")), bytes([0x70, 2, 3]))
        self.kb.bind_macro(4, self.idx("f12"), 2, nuphy.MACRO_TOGGLE)
        self.assertEqual(self.kb.get_key(4, self.idx("f12")), bytes([0x70, 2, 255]))

    def test_limits(self):
        with self.assertRaises(NuphyError):
            self.kb.set_macro(0, [MacroAction(KC["KC_VOL_UP"], True), MacroAction(KC["KC_VOL_UP"], False)])
        with self.assertRaises(NuphyError):
            self.kb.set_macro(32, nuphy.parse_macro("a"))
        big = [MacroAction(4, i % 2 == 0, 1) for i in range(496)]
        self.kb.set_macro(0, big, 1)
        self.assertEqual(self.kb.macros(1)[0], big)
        with self.assertRaisesRegex(NuphyError, "pleine"):
            self.kb.set_macro(1, nuphy.parse_macro("a"), 1)


class Settings(Base):
    def test_function_bytes_like_the_driver(self):
        prev = None
        for cfg, data in VEC["func"]:
            self.sim.mem[0x05][64 + 4:64 + 8] = bytes(data)
            s = self.kb.settings(1)
            self.assertEqual(s["rate"], nuphy.RATES[cfg["reportRate"]])
            self.assertEqual(s["tick"], nuphy.TICKS[cfg["tickRate"]])
            self.assertEqual((s["lock_win"], s["lock_alt_tab"], s["lock_alt_f4"]),
                             (cfg["disableWindows"], cfg["disableAltTab"], cfg["disableAltF4"]))
            self.assertEqual((s["game_optimization"], s["debug"]), (cfg["gameOptimization"], cfg["debugMode"]))
            self.assertEqual(s["stabilization"], nuphy.STABILIZATION[cfg["stabilization"]])
            self.assertEqual(s["sleep"], cfg["sleepTime"] if cfg["sleepEnable"] else 0)
            if prev is not None:                      # walk from the previous state to this one with the setters
                self.sim.mem[0x05][64 + 4:64 + 8] = bytes(prev)
                self.kb.set_rate(nuphy.RATES[cfg["reportRate"]], 1)
                self.kb.set_tick(nuphy.TICKS[cfg["tickRate"]], 1)
                self.kb.set_flag("lock_win", cfg["disableWindows"], 1)
                self.kb.set_flag("lock_alt_tab", cfg["disableAltTab"], 1)
                self.kb.set_flag("lock_alt_f4", cfg["disableAltF4"], 1)
                self.kb.set_flag("game_optimization", cfg["gameOptimization"], 1)
                self.kb.set_flag("debug", cfg["debugMode"], 1)
                self.kb.set_stabilization(nuphy.STABILIZATION[cfg["stabilization"]], 1)
                want = bytearray(data)
                want[1] = prev[1]                     # sleep is set for the three modes at once, below
                want[3] = want[3] & ~0x05 | prev[3] & 0x05   # bits this tool leaves alone
                self.assertEqual(bytes(self.sim.mem[0x05][64 + 4:64 + 8]), bytes(want))
            prev = data

    def test_first_write_backs_up(self):
        kb = nuphy.Keyboard(nuphy.Node("/dev/hidraw-sim", nuphy.VID, 0x6120, "sim"), fd=self.link.fileno())
        kb._given_fd, kb.fd, kb.version = None, self.link.fileno(), 106      # behave like a real node
        self.sim.log.clear()
        kb.set_light("main", 1, brightness=10)
        saved = json.loads((nuphy.STATE_DIR / "config-origine-6120.json").read_text())
        self.assertEqual(len(saved["cfg"]), 3)
        first_write = next(i for i, e in enumerate(self.sim.log) if e[0] == Cmd.SET_FUNC)
        self.assertGreater(first_write, 300)          # the whole keyboard was read before anything changed
        f = base64.b64decode(saved["cfg"][1]["f"])
        self.assertEqual(f[8 + 9], 100)               # and the file holds the value from before
        self.sim.log.clear()
        kb.set_light("main", 1, brightness=20)
        self.assertEqual(len(self.sim.log), 2)        # once only: a write and its read-back

    def test_legacy_sleep(self):
        self.kb.version = 104
        self.assertEqual(self.kb.settings()["sleep"], 6)
        self.kb.set_sleep(0)
        self.assertEqual(self.sim.mem[0x05][5], 0x80)
        self.assertEqual(self.kb.settings()["sleep"], 0)

    def test_sleep_applies_to_every_mode(self):
        self.kb.set_sleep(30)
        self.assertEqual([self.sim.mem[0x05][m * 64 + 5] for m in range(3)], [0x80 | 30] * 3)
        self.kb.set_sleep(0)
        self.assertEqual([self.sim.mem[0x05][m * 64 + 5] for m in range(3)], [30] * 3)
        self.assertEqual(self.kb.settings()["sleep"], 0)
        with self.assertRaises(NuphyError):
            self.kb.set_sleep(3)

    def test_header_bytes_never_written(self):
        head = [bytes(self.sim.mem[0x05][m * 64:m * 64 + 4]) for m in range(3)]
        self.kb.set_rate(1000)
        self.kb.set_tachyon(True)
        self.kb.set_tachyon(False)
        self.kb.set_sleep(10)
        self.kb.live(True)
        self.kb.live(False)
        self.assertEqual([bytes(self.sim.mem[0x05][m * 64:m * 64 + 4]) for m in range(3)], head)
        self.assertTrue(all(addr % 64 >= 4 for addr, _ in frames(self.sim, Cmd.SET_FUNC)))

    def test_lighting(self):
        self.kb.set_light("main", 1, effect=6, brightness=80, speed=3, direction=1, color="#00ff7f", rainbow=False)
        self.kb.set_light("side", 1, effect=2, color="#102030", rainbow=False)
        l = self.kb.lights(1)
        self.assertEqual(l["main"], {"effect": 6, "brightness": 80, "speed": 3, "direction": 1, "rainbow": False, "color": "#00ff7f"})
        self.assertEqual((l["side"]["effect"], l["side"]["color"], l["side"]["rainbow"]), (2, "#102030", False))
        self.assertEqual(self.kb.lights(0)["main"]["effect"], 6)       # factory value of mode 1, untouched
        with self.assertRaises(NuphyError):
            self.kb.set_light("main", 1, effect=3)                      # not offered for this keyboard
        with self.assertRaises(NuphyError):
            self.kb.set_light("loop", 1, brightness=10)
        self.kb.set_light("main", 1, effect=nuphy.LIGHT_OFF, brightness=0)
        self.assertEqual(self.kb.lights(1)["main"]["effect"], 255)

    def test_tachyon_round_trip(self):
        self.kb.set_light("main", 1, brightness=70)
        self.kb.set_tachyon(True, 1)
        s = self.kb.settings(1)
        self.assertTrue(s["tachyon"])
        self.assertEqual([v["brightness"] for v in self.kb.lights(1).values()], [0, 0])
        self.kb.set_tachyon(False, 1)
        self.assertFalse(self.kb.settings(1)["tachyon"])
        self.assertEqual(self.kb.lights(1)["main"]["brightness"], 70)

    def test_key_colours(self):
        w, a = self.idx("w"), self.idx("a")
        self.kb.set_key_colors({w: (255, 0, 0), a: (0, 0, 255), w + 1: (1, 2, 3)}, 1)
        c = self.kb.key_colors(1)
        self.assertEqual((c[w], c[w + 1], c[a], c[0]), ((255, 0, 0), (1, 2, 3), (0, 0, 255), (0, 0, 0)))
        self.assertEqual(len(frames(self.sim, Cmd.SET_LED)), 2)           # neighbours in one frame
        self.assertEqual(self.kb.key_colors(0), [(0, 0, 0)] * 128)

    def test_names_and_gamepad(self):
        self.assertEqual(self.kb.mode_names(), ["Gaming", "Windows", "Mac"])
        self.kb.set_mode_name(1, "Bureau été")
        self.assertEqual(self.kb.mode_names(), ["Gaming", "Bureau été", "Mac"])
        with self.assertRaises(NuphyError):
            self.kb.set_mode_name(1, "x" * 57)
        self.assertFalse(self.kb.auto_select())
        self.kb.set_auto_select(True)
        self.assertTrue(self.kb.auto_select())
        self.assertEqual(self.sim.mem[0xF1][224], 1)
        with self.assertRaises(NuphyError):
            self.kb.gamepad()                         # needs firmware 1.1.1 on this model
        self.kb.version = 111
        self.kb.set_gamepad(type="classic", angle=45, square_gate=True)
        self.assertEqual(bytes(self.sim.mem[0x10][2:9]), bytes([1, 0, 0b1000, 0, 0, 0, 45]))
        self.assertEqual(self.kb.gamepad()["square_gate"], True)
        with self.assertRaisesRegex(NuphyError, "Xbox"):
            self.kb.set_gamepad(type="xbox")          # the driver does not offer it on this keyboard


class Files(Base):
    def test_export_is_the_drivers_format(self):
        cfg = self.kb.export_mode(1)
        self.assertEqual(sorted(cfg), sorted("cfkrdmtnhpva"))
        self.assertEqual((cfg["p"], base64.b64decode(cfg["v"])), ("6", b"106"))
        f = base64.b64decode(cfg["f"])
        self.assertEqual((f[0], f[1], f[4], f[5]), (0xAA, 5, 54, 64))      # the raw reply, as the driver stores it
        self.assertEqual(len(base64.b64decode(cfg["k"])), 2048)
        sums = json.loads(base64.b64decode(cfg["c"]))
        self.assertEqual(sorted(sums), sorted("fkrdmtnhpva"))
        self.assertEqual(self.sim.quick, 0)

    def test_round_trip_into_another_mode(self):
        self.kb.set_key(4, self.idx("a"), KC["KC_B"])
        self.kb.set_mod_tap(4, self.idx("caps"), KC["KC_ESC"], KC["KC_LCTL"])
        self.kb.set_macro(0, nuphy.parse_macro("a b"), 1)
        self.kb.set_mode_name(1, "Perso")
        rts = self.kb.rt_all(1)
        self.kb.set_rt({5: self.kb.rt_from_mm(rts[5], actuation=0.5, mode="rt", press=0.2, release=0.2)}, 1)
        self.kb.set_light("main", 1, effect=6, brightness=55)
        self.kb.set_rate(1000, 2)
        cfg = self.kb.export_mode(1)
        self.kb.import_mode(cfg, 2)
        self.assertEqual(self.kb.get_key(8, self.idx("a")), bytes([0x10, 0, 5]))
        self.assertEqual(self.kb.get_key(8, self.idx("caps")), bytes([0x92, 0, 17]))
        self.assertEqual(self.kb.get_key(8, self.idx("fn")), bytes([0xF0, 0xFF, 9]))     # Fn follows the mode
        self.assertEqual(self.kb.get_key(9, self.idx("m")), bytes([0xF0, 0xFF, 10]))
        self.assertEqual(self.kb.mode_names()[2], "Perso")
        self.assertEqual(nuphy.format_macro(self.kb.macros(2)[0]), "+a -a +b -b")
        self.assertEqual(self.kb.rt_mm(self.kb.rt_all(2)[5])["actuation"], 0.5)
        self.assertEqual(self.kb.lights(2)["main"]["brightness"], 55)
        self.assertEqual(self.kb.settings(2)["rate"], 1000)                 # the keyboard's own rate is kept
        self.assertEqual(self.kb.keymap(4)[self.idx("a")], bytes([0x10, 0, 5]))

    def test_parts_and_switches(self):
        self.kb.set_key(4, self.idx("a"), KC["KC_B"])
        rts = self.kb.rt_all(1)
        self.kb.set_rt({7: self.kb.rt_from_mm(rts[7], switch=1, actuation=2.0)}, 1)
        cfg = self.kb.export_mode(1)
        self.kb.import_mode(cfg, 0, parts=("trigger",))
        self.assertEqual(self.kb.keymap(0), self.kb.default_keymap(0))
        got = self.kb.rt_all(0)[7]
        self.assertEqual((got.switch, self.kb.rt_mm(got)["actuation"]), (2, 2.0))    # fitted switch kept
        self.kb.import_mode(cfg, 0, parts=("trigger",), keep_switches=False)
        self.assertEqual(self.kb.rt_all(0)[7].switch, 1)

    def test_bad_files(self):
        cfg = self.kb.export_mode(1)
        for broken in (dict(cfg, k=cfg["k"][:-8] + "AAAAAAA="), dict(cfg, c=""), dict(cfg, p="2"), {}, {"c": "x"}):
            with self.assertRaises(NuphyError):
                self.kb.import_mode(broken, 1)
        self.assertEqual(frames(self.sim, Cmd.SET_KEYS), [])

    def test_resets(self):
        self.kb.set_key(4, self.idx("a"), KC["KC_B"])
        self.kb.set_toggle(4, self.idx("s"), KC["KC_W"])
        self.kb.set_macro(0, nuphy.parse_macro("a b"), 1)
        self.kb.set_mode_name(1, "Perso")
        self.kb.set_key(0, self.idx("a"), KC["KC_C"])
        self.kb.reset_mode(1)
        self.assertEqual(self.kb.keymap(4), self.kb.default_keymap(4))
        self.assertEqual((self.kb.advanced(1), self.kb.macros(1)[0], self.kb.mode_names()[1]), ([], [], "Windows"))
        self.assertEqual(self.kb.get_key(0, self.idx("a")), bytes([0x10, 0, 6]))     # other mode kept
        self.kb.factory_reset()
        self.assertEqual(self.kb.keymap(0), self.kb.default_keymap(0))
        self.assertIn({"event": "reset"}, self.kb.poll(0.05))
        self.assertEqual(self.sim.quick, 0)


class Live(Base):
    def test_travel_events(self):
        self.kb.live(True)
        self.assertEqual([self.sim.mem[0x05][m * 64 + 7] & 8 for m in range(3)], [8, 8, 8])
        self.link.inject(self.sim.key_event([0x10, 0, 26], 150))
        self.link.inject(self.sim.key_event([0xF0, 0xFF, 5], 20, 255))
        ev = [e for e in self.kb.poll(0.05) if e["event"] == "key"]
        self.assertEqual([(e["index"], e["travel"], e["state"]) for e in ev],
                         [(self.idx("w"), 1.5, "none"), (self.idx("fn"), 0.2, "done")])
        self.kb.live(False)
        self.assertEqual([self.sim.mem[0x05][m * 64 + 7] & 8 for m in range(3)], [0, 0, 0])

    def test_calibration_commands(self):
        self.kb.calibration_start()
        self.assertTrue(self.sim.calibrating)
        self.kb.calibration_end()
        self.assertFalse(self.sim.calibrating)


class CommandLine(unittest.TestCase):
    def run_cli(self, *args, ok=True):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = nuphy.main(list(args))
        self.assertEqual(code == 0, ok, err.getvalue() + out.getvalue())
        return out.getvalue() + err.getvalue()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        nuphy_sim._shared = None
        os.environ["NUPHY_DEMO"] = os.path.join(self.tmp.name, "kb.state")
        self.addCleanup(os.environ.pop, "NUPHY_DEMO", None)
        nuphy.STATE_DIR = Path(self.tmp.name)

    def test_a_session(self):
        self.assertIn("Air75 HE", self.run_cli("info"))
        self.assertIn("8000 Hz", self.run_cli("show"))
        self.run_cli("key", "caps", "esc")
        self.assertIn("Esc", self.run_cli("key", "caps"))
        self.assertIn("Esc", self.run_cli("keys").splitlines()[3])
        self.run_cli("trigger", "w,a,s,d", "--ap", "0.4", "--rt", "continu", "--press", "0.1", "--release", "0.1")
        self.assertIn("0.40 mm  rtc", self.run_cli("trigger", "w"))
        self.run_cli("light", "main", "--effect", "wave", "--brightness", "60", "--color", "ff8800", "--rainbow", "off")
        self.assertIn("Wave, luminosité 60", self.run_cli("light", "main"))
        self.run_cli("color", "w,a,s,d", "ff0000")
        self.run_cli("rate", "1000")
        self.run_cli("tick", "60")
        self.run_cli("debounce", "high")
        self.run_cli("lock", "win", "on")
        self.run_cli("sleep", "15")
        shown = json.loads(self.run_cli("show", "--json"))
        self.assertEqual((shown["rate"], shown["tick"], shown["stabilization"], shown["lock_win"], shown["sleep"]),
                         (1000, 60, "high", True, 15))
        self.run_cli("adv", "mt", "tab", "tab", "lgui", "--ms", "200")
        self.run_cli("adv", "socd", "a", "d", "--rule", "neutral")
        self.run_cli("adv", "dks", "f", "a:hr..", "b:..t.")
        listing = self.run_cli("adv", "list")
        for word in ("MT", "SOCD", "DKS", "200 ms"):
            self.assertIn(word, listing)
        self.run_cli("adv", "remove", "a")
        self.assertNotIn("SOCD", self.run_cli("adv", "list"))
        self.run_cli("macro", "set", "1", "+lctl +c 20 -c -lctl")
        self.assertIn("répéter", self.run_cli("macro", "bind", "f12", "1", "--loop", "hold", ok=False))
        self.run_cli("macro", "bind", "f12", "1")
        self.assertIn("+lctl +c 20 -c -lctl", self.run_cli("macro", "list"))
        self.assertIn("M1", self.run_cli("key", "f12"))
        self.run_cli("rename", "Jeu")
        self.assertIn("« Jeu »", self.run_cli("info"))
        path = os.path.join(self.tmp.name, "mode.json")
        self.run_cli("export", path)
        self.run_cli("-m", "3", "import", path)
        self.assertIn("Esc", self.run_cli("-m", "3", "key", "caps"))
        self.run_cli("reset", ok=False)
        self.run_cli("reset", "--yes")
        self.assertIn("Verr.Maj", self.run_cli("key", "caps"))
        self.assertIn("Esc", self.run_cli("-m", "3", "key", "caps"))
        self.run_cli("-m", "1", "reset", "--factory", "--yes")
        self.assertIn("Verr.Maj", self.run_cli("-m", "3", "key", "caps"))

    def test_errors_are_plain(self):
        self.assertIn("touche physique inconnue", self.run_cli("key", "nope", ok=False))
        self.assertIn("touche inconnue", self.run_cli("key", "a", "nope", ok=False))
        self.assertIn("entre", self.run_cli("trigger", "a", "--ap", "9", ok=False))
        self.assertIn("lecture", self.run_cli("raw", "6", "0", "4", ok=False))
        self.assertIn("10 00 29", self.run_cli("raw", "8", "0", "16"))


if __name__ == "__main__":
    unittest.main()
