import unittest

from prompter import APP_ID, displays
from prompter.actions import ACTIONS, GLOBAL_ONLY, KEYS


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


if __name__ == "__main__":
    unittest.main()
