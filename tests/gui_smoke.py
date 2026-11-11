#!/usr/bin/env python3
"""Drive the settings window against the simulated keyboard: open every page,
use each one the way a person would, and check what reached the keyboard.

    NUPHY_DEMO=1 xvfb-run -a python3 tests/gui_smoke.py [screenshot-dir]
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("NUPHY_DEMO", "1")
os.environ.setdefault("XDG_STATE_HOME", tempfile.mkdtemp())
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("nuphy_gui", ROOT / "nuphy-gui.py")
gui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gui)
import nuphy  # noqa: E402
import nuphy_sim  # noqa: E402
from gi.repository import GLib  # noqa: E402

SHOTS = sys.argv[1] if len(sys.argv) > 1 else None
KC = nuphy.KEYCODES
failures = []


def check(what, cond):
    print(("ok   " if cond else "FAIL ") + what, flush=True)
    if not cond:
        failures.append(what)


def shot(name):
    if SHOTS:
        subprocess.run(["import", "-window", "root", os.path.join(SHOTS, name + ".png")], check=False)


def main():
    app = gui.App()
    state = {}

    def idle(win):
        return win._busy == 0 and win.snap is not None

    def steps(win):
        mem = lambda: nuphy_sim._shared.mem  # noqa: E731
        tabs = {t.name: t for t in win.tabs}
        idx = lambda name: next(i for i, t in win.snap["titles"].items() if t == name)  # noqa: E731

        yield "first read"
        check("window shows the keyboard", win.snap["model"] == "Air75 HE" and win.snap["firmware"] == "1.0.6")
        check("84 keys drawn", len(tabs["keys"].view.index_map) == 84)
        check("mode shown is the active one (M2)", win.snap["mode"] == 1)
        shot("1-touches")

        keys = tabs["keys"]
        caps = idx("Verr.Maj")
        keys.view.selected = {caps}
        keys._assign(None, KC["KC_ESC"])
        yield "remap"
        check("caps lock now sends Esc", bytes(mem()[0x08][4 * 512 + caps * 3:4 * 512 + caps * 3 + 3]) == bytes([0x10, 0, 41]))
        check("changed key is marked", caps in keys.view.accent)
        keys.layer_buttons[1].set_active(True)
        check("Fn layer shown", keys.view.labels[idx("F1")] == "Écran-")
        shot("1b-touches-fn")
        keys.layer_buttons[0].set_active(True)

        win.pages.set_visible_child_name("trigger")
        trig = tabs["trigger"]
        trig.view.select({idx(n) for n in "WASD"})
        trig.ap.set_value(0.4)
        trig.rt.set_active(True)
        trig.cont.set_active(True)
        trig.press.set_value(0.1)
        trig.release.set_value(0.15)
        trig.top.set_value(0.1)
        trig._apply()
        yield "trigger"
        w = win.snap["rt_mm"][idx("W")]
        check("WASD: 0.40 mm, continuous RT, 0.10/0.15", (w["actuation"], w["mode"], w["press"], w["release"], w["top_dz"]) == (0.4, "rtc", 0.1, 0.15, 0.1))
        check("other keys untouched", win.snap["rt_mm"][idx("Q")]["actuation"] == 1.5)
        shot("2-actionnement")

        win.pages.set_visible_child_name("advanced")
        adv = tabs["advanced"]
        adv.kind.set_selected(0)
        adv.view.select({idx("Tab")})
        adv._create()
        yield "mod-tap"
        adv.kind.set_selected(4)
        adv.rule.set_selected(1)
        adv.view.select({idx("A"), idx("D")})
        adv._create()
        yield "socd"
        adv.kind.set_selected(2)
        adv.view.select({idx("F")})
        adv.dks[0][1].set_code(KC["KC_A"])
        adv.dks[0][2].set_text("hr..")
        adv._create()
        yield "dks"
        kinds = sorted(i["kind"] for i in win.snap["advanced"])
        check("MT, SOCD pair and DKS listed", kinds == ["DKS", "MT", "SOCD", "SOCD"])
        shot("3-avancees")
        adv._remove(None, next(i for i in win.snap["advanced"] if i["kind"] == "SOCD"))
        yield "remove socd"
        check("removing one SOCD key removes the pair", sorted(i["kind"] for i in win.snap["advanced"]) == ["DKS", "MT"])

        win.pages.set_visible_child_name("light")
        light = tabs["light"]
        z = light.zones["main"]
        z["effect"].set_selected(5)                   # Wave
        yield from wait_for(win, lambda: win.snap["lights"]["main"]["effect"] == 6)
        z["brightness"].set_value(60)
        yield from wait_for(win, lambda: win.snap["lights"]["main"]["brightness"] == 60)
        z["color"].set_rgba(light._rgba("#00ff7f"))
        yield from wait_for(win, lambda: win.snap["lights"]["main"]["color"] == "#00ff7f")
        check("backlight: wave, 60 %, custom colour", not win.snap["lights"]["main"]["rainbow"])
        light.view.select({idx(n) for n in "WASD"})
        light.key_color.set_rgba(light._rgba("#ff0000"))
        light._paint()
        yield "paint"
        check("WASD painted red and effect switched to Custom",
              win.snap["colors"][idx("W")] == (255, 0, 0) and win.snap["lights"]["main"]["effect"] == 0)
        shot("4-eclairage")

        win.pages.set_visible_child_name("macros")
        mac = tabs["macros"]
        mac.which.set_selected(0)
        mac.entry.set_text("+lctl +c 20 -c -lctl")
        mac._save()
        yield "macro"
        check("macro stored", nuphy.format_macro(win.snap["macros"][0]) == "+lctl +c 20 -c -lctl")
        mac.view.selected = {idx("F12")}
        mac._assign()
        yield "bind"
        f12 = idx("F12")
        check("F12 runs macro 1", bytes(mem()[0x08][4 * 512 + f12 * 3:4 * 512 + f12 * 3 + 3]) == bytes([0x70, 0, 1]))
        mac.rec.set_active(True)
        mac._key(None, 0, 30 + 8, 0, True)            # the A key, as GDK reports it
        mac._key(None, 0, 30 + 8, 0, False)
        check("recording captures key presses", mac.entry.get_text().startswith("+a ") and mac.entry.get_text().endswith("-a"))
        mac.rec.set_active(False)
        shot("5-macros")

        win.pages.set_visible_child_name("mode")
        mode = tabs["mode"]
        mode.tick.set_selected(2)
        yield "tick"
        mode.stab.set_selected(2)
        yield "stab"
        mode.locks["lock_win"].set_active(True)
        yield "lock"
        mode.sleep_min.set_value(20)
        yield from wait_for(win, lambda: win.snap["sleep"] == 20)
        mode.name_row.set_text("Jeu")
        mode._rename(mode.name_row)
        yield "rename"
        s = win.snap
        check("tick 60, strong debounce, Win locked, sleep 20 min, renamed",
              (s["tick"], s["stabilization"], s["lock_win"], s["sleep"], s["mode_names"][1]) == (60, "high", True, 20, "Jeu"))
        mode.rate.set_selected(0)
        yield from wait_for(win, lambda: win.snap["rate"] == 1000)
        check("polling 1000 Hz", win.snap["rate"] == 1000)
        mode.tachyon.set_active(True)
        yield "tachyon"
        check("Tachyon mode turns every LED off", win.snap["tachyon"] and win.snap["lights"]["main"]["brightness"] == 0)
        mode.tachyon.set_active(False)
        yield "tachyon off"
        check("and gives the brightness back", win.snap["lights"]["main"]["brightness"] == 60)
        shot("6-mode")

        win.mode_drop.set_selected(0)
        yield "other mode"
        check("mode M1 shown, with its own keys", win.snap["mode"] == 0 and win.snap["keymaps"][0][caps] == bytes([0x10, 0, 57]))
        win.mode_drop.set_selected(1)
        yield "back"

        win.pages.set_visible_child_name("tools")
        tools = tabs["tools"]
        tools.live.set_active(True)
        yield from wait_for(win, lambda: mem()[0x05][128 + 7] & 8 and win.worker.kb._sim, idle_first=False)
        check("live view turns travel reports on", bool(mem()[0x05][64 + 7] & 8))
        link = win.worker.kb._sim
        link.inject(nuphy_sim._shared.key_event([0x10, 0, 26], 200))
        yield from wait_for(win, lambda: tools.view.fill.get(idx("W")), idle_first=False)
        check("a pressed key shows its travel", abs(tools.view.fill.get(idx("W"), 0) - 2.0 / 3.3) < 0.01)
        shot("7-outils")
        tools.live.set_active(False)
        yield from wait_for(win, lambda: not mem()[0x05][128 + 7] & 8 and win._live_stop is None, idle_first=False)
        check("and off again when stopped", not mem()[0x05][64 + 7] & 8)
        tools.cal.set_active(True)
        yield from wait_for(win, lambda: nuphy_sim._shared.calibrating, idle_first=False)
        check("calibration started", nuphy_sim._shared.calibrating)
        win.refresh()                                 # any other action ends the live session first
        yield "refresh during calibration"
        check("calibration ended by the next action", not nuphy_sim._shared.calibrating)

        check("bootloader frame never sent", not nuphy_sim._shared.bootloader)
        check("fast-transfer mode always closed", nuphy_sim._shared.quick == 0)

    def wait_for(win, cond, idle_first=True, tries=200):
        for _ in range(tries):
            if (not idle_first or idle(win)) and cond():
                return
            yield None
        check("timed out waiting", False)

    def pump():
        win = app.get_active_window()
        if win is None:
            return True
        if "gen" not in state:
            state["gen"] = steps(win)
            state["wait"] = True
        if state["wait"] and not idle(win):
            return True
        try:
            label = next(state["gen"])
            state["wait"] = label is not None         # a named step waits for the window to settle
        except StopIteration:
            app.quit()
            return False
        except Exception as e:                        # noqa: BLE001
            import traceback
            traceback.print_exc()
            failures.append(repr(e))
            app.quit()
            return False
        return True

    GLib.timeout_add(120, pump)
    GLib.timeout_add_seconds(120, lambda: (failures.append("timeout"), app.quit())[1])
    app.run([])
    print("\n%d failure(s)" % len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
