import unittest

import gi

gi.require_version("Pango", "1.0")
from gi.repository import Pango  # noqa: E402

from prompter import APP_ID, displays  # noqa: E402
from prompter.actions import ACTIONS, GLOBAL_ONLY, KEYS  # noqa: E402
from prompter.fontpicker import closest, for_language  # noqa: E402


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
