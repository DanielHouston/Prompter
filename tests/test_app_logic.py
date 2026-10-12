import unittest

import gi

gi.require_version("Pango", "1.0")
from gi.repository import Pango  # noqa: E402

from prompter import APP_ID, displays  # noqa: E402
from prompter.actions import ACTIONS, GLOBAL_ONLY, KEYS  # noqa: E402
from prompter.fontpicker import closest, for_language  # noqa: E402
from prompter.readouts import Clock, Stopwatch, duration  # noqa: E402
from prompter.tray import SEPARATOR, MenuItem, _pixmap_variant, _pixmaps  # noqa: E402
from prompter.view import GAP, PAD, phrase_lines, place_readouts  # noqa: E402


class FakeMonitor:
    def __init__(self, connector, manufacturer, model):
        self.connector, self.manufacturer, self.model = connector, manufacturer, model

    def get_connector(self):
        return self.connector

    def get_manufacturer(self):
        return self.manufacturer

    def get_model(self):
        return self.model


class FakeList:
    def __init__(self, items):
        self.items = items

    def get_n_items(self):
        return len(self.items)

    def get_item(self, i):
        return self.items[i]


class FakeDisplay:
    def __init__(self, monitors):
        self.list = FakeList(monitors)

    def get_monitors(self):
        return self.list


ACER = FakeMonitor("DP-1", "Acer Technologies", "XB271HU")
PROMPTER = FakeMonitor("DVI-I-1", "IDI", "Elgato Prom.")


class DisplaysTest(unittest.TestCase):
    def test_finds_the_prompter(self):
        self.assertIs(displays.find(FakeDisplay([ACER, PROMPTER])), PROMPTER)

    def test_no_prompter(self):
        self.assertIsNone(displays.find(FakeDisplay([ACER])))

    def test_configured_connector_wins(self):
        self.assertIs(displays.find(FakeDisplay([ACER, PROMPTER]), "DP-1"), ACER)
        self.assertIsNone(displays.find(FakeDisplay([PROMPTER]), "DP-1"))

    def test_label(self):
        self.assertEqual(displays.label(PROMPTER), "IDI Elgato Prom. (DVI-I-1)")


class ActionsTest(unittest.TestCase):
    def test_keys_and_shortcuts_use_real_actions(self):
        for _key, action, _what, _trigger in KEYS:
            self.assertIn(action, ACTIONS)
        for action, _what, _trigger in GLOBAL_ONLY:
            self.assertIn(action, ACTIONS)

    def test_shortcut_ids_are_unique(self):
        ids = [a for _k, a, _w, _t in KEYS] + [a for a, _w, _t in GLOBAL_ONLY]
        self.assertEqual(len(ids), len(set(ids)))

    def test_app_id_is_reverse_dns(self):
        self.assertRegex(APP_ID, r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*){2,}$")


class FontPickerTest(unittest.TestCase):
    def faces(self, *names):
        return [Pango.FontDescription.from_string(n) for n in names]

    def test_keeps_weight_across_families(self):
        faces = self.faces("Sans Light", "Sans", "Sans Medium", "Sans Bold", "Sans Medium Italic")
        self.assertEqual(closest(faces, Pango.Weight.MEDIUM, Pango.Style.NORMAL), 2)

    def test_nearest_weight_when_missing(self):
        faces = self.faces("Serif", "Serif Bold", "Serif Italic")
        self.assertEqual(closest(faces, Pango.Weight.MEDIUM, Pango.Style.NORMAL), 0)
        self.assertEqual(closest(faces, Pango.Weight.HEAVY, Pango.Style.NORMAL), 1)

    def test_slant_matters_more_than_weight(self):
        faces = self.faces("Sans Bold", "Sans Light Italic")
        self.assertEqual(closest(faces, Pango.Weight.BOLD, Pango.Style.ITALIC), 1)

    def test_lists_fonts_that_write_the_language(self):
        latin = "abcdefghijklmnopqrstuvwxyz"
        context = FakeFontContext({"Latin": latin + latin.upper(), "Adlam": "𞤀𞤁", "Symbols": "★"})
        names = [f.get_name() for f in for_language(context, context.families, "en")]
        self.assertEqual(names, ["Latin"])
        names = [f.get_name() for f in for_language(context, context.families, "en", keep="Adlam")]
        self.assertEqual(names, ["Latin", "Adlam"])  # the current font stays, so the list can start on it
        names = [f.get_name() for f in for_language(context, context.families, "ru", keep="Latin")]
        self.assertEqual(names, ["Latin", "Adlam", "Symbols"])  # nothing writes Russian: list everything


class TrayTest(unittest.TestCase):
    def test_menu_items(self):
        item = MenuItem("Show my_script", lambda: None)
        self.assertEqual(item.props()["label"].unpack(), "Show my__script")  # not a mnemonic
        self.assertNotIn("toggle-type", item.props())
        checked = MenuItem("Voice Follow", lambda: None, checked=True).props()
        self.assertEqual((checked["toggle-type"].unpack(), checked["toggle-state"].unpack()), ("checkmark", 1))
        self.assertEqual(SEPARATOR.props()["type"].unpack(), "separator")
        self.assertFalse(MenuItem("Start", enabled=False).props()["enabled"].unpack())

    def test_icon_pixmaps(self):
        from importlib import resources
        pixmaps = _pixmaps(resources.files("prompter") / "data" / f"{APP_ID}.svg")
        self.assertEqual([(w, h, len(d)) for w, h, d in pixmaps], [(s, s, s * s * 4) for s in (22, 32, 48, 64)])
        alpha = pixmaps[1][2][0::4]  # ARGB: alpha first
        self.assertEqual((min(alpha), max(alpha)), (0, 255))
        from gi.repository import GLib
        self.assertTrue(_pixmap_variant(pixmaps).equal(GLib.Variant("a(iiay)", pixmaps)))
        self.assertEqual(_pixmap_variant([]).get_type_string(), "a(iiay)")


class ReadoutsTest(unittest.TestCase):
    def test_stopwatch_only_runs_while_reading(self):
        now = [100.0]
        watch = Stopwatch(clock=lambda: now[0])
        watch.run(True)
        now[0] += 30
        watch.run(False)  # paused between takes
        now[0] += 600
        self.assertEqual(watch.seconds, 30)
        watch.run(True)
        now[0] += 5
        self.assertEqual(watch.seconds, 35)
        watch.run(True)  # still running: no restart
        now[0] += 5
        self.assertEqual(watch.seconds, 40)

    def test_stopwatch_reset_keeps_running(self):
        now = [0.0]
        watch = Stopwatch(clock=lambda: now[0])
        watch.run(True)
        now[0] += 50
        watch.reset()
        now[0] += 3
        self.assertEqual(watch.seconds, 3)

    def test_duration(self):
        self.assertEqual(duration(0), "0:00")
        self.assertEqual(duration(65.9), "1:05")
        self.assertEqual(duration(3723), "1:02:03")
        self.assertEqual(duration(-4), "0:00")

    def test_clock_formats(self):
        from gi.repository import GLib
        evening = GLib.DateTime.new_local(2026, 10, 10, 21, 5, 0)
        clock = Clock()
        clock.settings = None  # no desktop setting: 24-hour
        self.assertEqual(clock.text(evening), "21:05")
        clock.settings = FakeSettings({"clock-format": "12h"})
        self.assertRegex(clock.text(evening), r"^9:05")


class PlaceReadoutsTest(unittest.TestCase):
    W, H = 1024, 600

    def test_corners_and_centre(self):
        spots = place_readouts(self.W, self.H, [("top-left", 80, 40), ("bottom-right", 60, 40), ("bottom-center", 100, 40)])
        self.assertEqual(spots, [(PAD, PAD), (self.W - PAD - 60, self.H - PAD - 40), ((self.W - 100) // 2, self.H - PAD - 40)])

    def test_same_position_side_by_side(self):
        spots = place_readouts(self.W, self.H, [("top-center", 80, 40), ("top-center", 60, 40)])
        left = round((self.W - (80 + GAP + 60)) / 2)
        self.assertEqual(spots, [(left, PAD), (left + 80 + GAP, PAD)])

    def test_moves_beside_the_self_view(self):
        view_right = (700, PAD, 300, 170)  # x, y, w, h in the top right
        spots = place_readouts(self.W, self.H, [("top-right", 60, 40), ("bottom-right", 60, 40)], avoid=view_right)
        self.assertEqual(spots[0], (700 - PAD - 60, PAD))  # to its left
        self.assertEqual(spots[1], (self.W - PAD - 60, self.H - PAD - 40))  # not in the way: stays
        view_left = (PAD, PAD, 300, 170)
        self.assertEqual(place_readouts(self.W, self.H, [("top-left", 60, 40)], avoid=view_left), [(PAD + 300 + PAD, PAD)])


class PhraseLinesTest(unittest.TestCase):
    """Centred on the lens: short lines that end where you'd pause. Monospace here, 10 px a byte."""

    def lines(self, text, chars):
        out = phrase_lines(text, lambda i: i * 10, chars * 10)
        self.assertEqual(len(out.encode()), len(text.encode()))  # every word's byte offset still holds
        self.assertEqual(out.replace("\n", " "), text)
        return out.split("\n")

    def test_lines_fit(self):
        text = "Camera Hub only runs on Windows and macOS, so on Linux the prompter is just a second screen."
        for line in self.lines(text, 20):
            self.assertLessEqual(len(line), 20)

    def test_ends_sentences_and_clauses(self):
        self.assertEqual(self.lines("That's the whole idea. Write your script in Markdown, and start talking.", 24),
                         ["That's the whole idea.", "Write your script", "in Markdown,", "and start talking."])

    def test_little_words_go_with_what_follows(self):
        for line in self.lines("We look at how to turn an Elgato Prompter into a proper voice-following teleprompter", 22)[:-1]:
            self.assertNotIn(line.split()[-1], {"at", "to", "an", "a", "the"})

    def test_a_word_too_long_for_a_line_keeps_its_own(self):
        self.assertEqual(self.lines("a voice-following teleprompter", 10), ["a", "voice-following", "teleprompter"])

    def test_short_text_is_left_alone(self):
        self.assertEqual(self.lines("Wrapping up", 20), ["Wrapping up"])
        self.assertEqual(self.lines("", 20), [""])


class FakeSettings:
    def __init__(self, values):
        self.values = values

    def get_string(self, key):
        return self.values[key]


class FakeFontContext:
    """A Pango context whose fonts have the given characters."""

    def __init__(self, chars):
        self.chars = chars
        self.families = [FakeFamily(name) for name in chars]

    def get_font_map(self):
        return self

    def load_font(self, _context, desc):
        return FakeFont(self.chars[desc.get_family()])


class FakeFamily:
    def __init__(self, name):
        self.name = name

    def get_name(self):
        return self.name

    def get_face(self, _name):
        return self

    def describe(self):
        return Pango.FontDescription.from_string(self.name)


class FakeFont:
    def __init__(self, chars):
        self.chars = chars

    def has_char(self, char):
        return char in self.chars


if __name__ == "__main__":
    unittest.main()
