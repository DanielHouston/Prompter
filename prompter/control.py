"""Control window for the main monitor.

Two tabs: Prompt, for while you're reading (transport, mode, where you are, the script with
click-to-jump, quick toggles), and Setup, for what you set once (microphone, text, eye-line
crosshair, camera self-view, timer and progress, Prompter screen, diagnostics).
"""

import bisect
import math
import os
import re
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .actions import GLOBAL_ONLY, KEYS  # noqa: E402
from . import models  # noqa: E402
from .fontpicker import FontPicker  # noqa: E402
from .modelsdialog import ModelsDialog  # noqa: E402
from .readouts import POSITIONS, TIMER_SHOWS  # noqa: E402
from .view import rgba  # noqa: E402

CORNERS = (("top-right", "Top right"), ("top-left", "Top left"), ("bottom-right", "Bottom right"), ("bottom-left", "Bottom left"))

LANGUAGES = (("en", "English"), ("de", "German"), ("fr", "French"), ("es", "Spanish"), ("it", "Italian"),
             ("pt", "Portuguese"), ("nl", "Dutch"), ("pl", "Polish"), ("sv", "Swedish"), ("da", "Danish"),
             ("no", "Norwegian"), ("fi", "Finnish"), ("cs", "Czech"), ("tr", "Turkish"), ("ru", "Russian"),
             ("uk", "Ukrainian"), ("el", "Greek"), ("hu", "Hungarian"), ("ro", "Romanian"))
DEVICES = (("auto", "Automatic"), ("cuda", "Graphics card"), ("cpu", "Processor"))

CSS = """
.prompter-play { min-width: 72px; min-height: 72px; -gtk-icon-size: 30px; }
.prompter-transport > button.circular:not(.prompter-play) { min-width: 44px; min-height: 44px; }
.prompter-overview, .prompter-overview text { background: none; }
.prompter-overview text { font-size: 12pt; }
.prompter-quick { padding: 4px 2px 0 2px; }
levelbar.prompter-level trough { min-height: 6px; }
levelbar.prompter-level block.filled { background-color: var(--accent-bg-color); }
"""


class ControlWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Prompter Control", default_width=560, default_height=820)
        self.app = app
        self._updating = False
        self._font = ""
        self._token_chars, self._token_blocks, self._block_chars = [], [], []
        self._mic_names = [None]
        self._camera_paths = []
        self._display_connectors = [None]
        self._voice = True
        self._missing = False  # voice follow has no speech model to run
        self.models_dialog = None
        self._global_triggers = {}
        self.connect("close-request", lambda *_: self.set_visible(False) or True)
        _install_css()

        # Shared adjustments: the Prompt and Setup tabs show the same values
        self.wpm_adj = self._adjustment(60, 260, 5, lambda v: self.app.set_wpm(v))
        self.size_adj = self._adjustment(24, 140, 2, lambda v: self.app.set_font_size(v))
        self.screen_adj = {name: self._adjustment(0, 100, 1, lambda v, n=name: self.app.screen.set(n, v))
                           for name in ("brightness", "contrast")}

        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(self._prompt_page(), "prompt", "Prompt", "media-playback-start-symbolic")
        self.stack.add_titled_with_icon(self._setup_page(), "setup", "Setup", "emblem-system-symbolic")

        header = Adw.HeaderBar(title_widget=Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        header.pack_start(self._scripts_button())
        header.pack_end(self._menu_button())
        view = Adw.ToolbarView(content=self.stack)
        view.add_top_bar(header)
        self.banner = Adw.Banner(title="Elgato Prompter not found. Connect it, and check the DisplayLink driver is running.")
        view.add_top_bar(self.banner)
        self.stack.connect("notify::visible-child-name", lambda *_: self.stack.get_visible_child_name() == "setup"
                           and self.app.refresh_devices())
        self.set_content(view)

        self.add_controller(self._prompt_keys())
        calibration = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        calibration.connect("key-pressed", self._calibration_key)
        self.add_controller(calibration)

    # --- Prompt tab -----------------------------------------------------------

    def _prompt_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                       margin_top=14, margin_bottom=12, margin_start=16, margin_end=16)

        head = Gtk.Box(spacing=8)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.title = Gtk.Label(label="No script", xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["title-2"])
        self.where = Gtk.Label(label="Add Markdown files to the scripts folder", xalign=0,
                               ellipsize=Pango.EllipsizeMode.END, css_classes=["dim-label"])
        titles.append(self.title)
        titles.append(self.where)
        head.append(titles)
        self.sections = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["navigation-sidebar"])
        self.sections.connect("row-activated", self._section_picked)
        self.sections_popover = Gtk.Popover(child=Gtk.ScrolledWindow(
            child=self.sections, propagate_natural_height=True, propagate_natural_width=True, max_content_height=380))
        head.append(Gtk.MenuButton(icon_name="view-list-bullet-symbolic", tooltip_text="Jump to a section",
                                   popover=self.sections_popover, valign=Gtk.Align.CENTER, css_classes=["flat"]))
        page.append(head)
        self.progress = Gtk.ProgressBar()
        page.append(self.progress)

        transport = Gtk.Box(spacing=14, halign=Gtk.Align.CENTER, css_classes=["prompter-transport"])
        transport.append(_round("go-top-symbolic", "app.restart", "Back to the top (Home)"))
        transport.append(_round("media-skip-backward-symbolic", "app.prev-section", "Previous section (Page Up)"))
        self.play = _round("media-playback-start-symbolic", "app.toggle", "Start (Space)")
        self.play.add_css_class("suggested-action")
        self.play.add_css_class("prompter-play")
        transport.append(self.play)
        transport.append(_round("media-skip-forward-symbolic", "app.next-section", "Next section (Page Down)"))
        transport.append(_round("edit-undo-symbolic", "app.back", "Back a paragraph (Backspace)"))
        page.append(transport)
        self.status = Gtk.Label(label="Starting…", css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END)
        page.append(self.status)

        self.mode = Adw.ToggleGroup(halign=Gtk.Align.CENTER)
        self.mode.add(Adw.Toggle(name="voice", label="Voice follow", icon_name="audio-input-microphone-symbolic"))
        self.mode.add(Adw.Toggle(name="fixed", label="Fixed speed", icon_name="media-seek-forward-symbolic"))
        self.mode.connect("notify::active-name", self._mode_picked)
        page.append(self.mode)

        self.mode_detail = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vhomogeneous=True)
        listening = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, tooltip_text="Microphone level")
        listening.append(Gtk.Image(icon_name="audio-input-microphone-symbolic", css_classes=["dim-label"]))
        self.level = Gtk.LevelBar(min_value=0, max_value=1, width_request=220, valign=Gtk.Align.CENTER,
                                  css_classes=["prompter-level"])
        for offset in ("low", "high", "full"):  # one colour, not traffic lights
            self.level.remove_offset_value(offset)
        listening.append(self.level)
        self.mode_detail.add_named(listening, "voice")
        speed = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        speed.append(Gtk.SpinButton(adjustment=self.wpm_adj, valign=Gtk.Align.CENTER))
        speed.append(Gtk.Label(label="words per minute", css_classes=["dim-label"]))
        self.mode_detail.add_named(speed, "fixed")
        missing = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
        self.missing_label = Gtk.Label(css_classes=["dim-label", "numeric"], visible=False)  # download progress
        missing.append(self.missing_label)
        get = Gtk.Button(label="Choose a Speech Model…", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
        get.connect("clicked", lambda *_: self.show_models())
        missing.append(get)
        self.mode_detail.add_named(missing, "missing")
        page.append(self.mode_detail)

        self.text = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 top_margin=12, bottom_margin=12, left_margin=14, right_margin=14,
                                 tooltip_text="Click anywhere to jump there")
        buf = self.text.get_buffer()
        buf.create_tag("heading", weight=Pango.Weight.BOLD, scale=1.1, pixels_above_lines=6)
        self.cue_tag = buf.create_tag("cue", style=Pango.Style.ITALIC)
        self.spoken_tag = buf.create_tag("spoken")
        self.paragraph_tag = buf.create_tag("paragraph")
        self.here_tag = buf.create_tag("here", underline=Pango.Underline.SINGLE)
        self.mark = buf.create_mark("pos", buf.get_start_iter(), True)
        click = Gtk.GestureClick()
        click.connect("released", self._text_clicked)
        self.text.add_controller(click)
        self.text.connect("realize", lambda *_: self._color_tags())
        page.append(Gtk.ScrolledWindow(child=self.text, vexpand=True, css_classes=["card", "prompter-overview"]))

        page.append(self._quick_bar())
        return page

    def _quick_bar(self):
        bar = Gtk.Box(spacing=6, css_classes=["prompter-quick"])
        size = Gtk.Box(css_classes=["linked"])
        size.append(_flat("zoom-out-symbolic", "app.smaller", "Smaller text (−)"))
        self.size_label = Gtk.Label(width_chars=7, css_classes=["numeric", "dim-label"])
        size.append(self.size_label)
        size.append(_flat("zoom-in-symbolic", "app.bigger", "Bigger text (=)"))
        bar.append(size)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4, margin_end=4))

        self.quick_preview = Gtk.ToggleButton(icon_name="camera-web-symbolic", tooltip_text="Camera self-view (C)",
                                              css_classes=["flat"], focusable=False)
        self.quick_preview.connect("toggled", lambda b: self._updating or self.app.set_preview("enabled", b.get_active()))
        bar.append(self.quick_preview)
        self.quick_crosshair = Gtk.ToggleButton(icon_name="find-location-symbolic", tooltip_text="Eye-line crosshair",
                                                css_classes=["flat"], focusable=False)
        self.quick_crosshair.connect("toggled", lambda b: self._updating or self.app.set_style("crosshair", "enabled", b.get_active()))
        bar.append(self.quick_crosshair)
        self.quick_timer = Gtk.ToggleButton(icon_name="preferences-system-time-symbolic", tooltip_text="Timer (T)",
                                            css_classes=["flat"], focusable=False)
        self.quick_timer.connect("toggled", lambda b: self._updating or self.app.set_readout("timer", b.get_active()))
        bar.append(self.quick_timer)
        self.quick_progress = Gtk.ToggleButton(label="%", tooltip_text="How far through, in percent (%)",
                                               css_classes=["flat"], focusable=False)
        self.quick_progress.connect("toggled", lambda b: self._updating or self.app.set_readout("progress", b.get_active()))
        bar.append(self.quick_progress)
        bar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4, margin_end=4))

        self.quick_brightness = Gtk.Box(spacing=4, hexpand=True, tooltip_text="Prompter brightness")
        self.quick_brightness.append(Gtk.Image(icon_name="display-brightness-symbolic", css_classes=["dim-label"]))
        self.quick_brightness.append(Gtk.Scale(adjustment=self.screen_adj["brightness"], hexpand=True, draw_value=False))
        self.quick_brightness.set_sensitive(False)  # until the Prompter has reported its range
        bar.append(self.quick_brightness)
        return bar

    # --- Setup tab ------------------------------------------------------------

    def _setup_page(self):
        page = Adw.PreferencesPage()
        self.colors, self.numbers = {}, {}

        group = Adw.PreferencesGroup(title="Script")
        self.folder_row = Adw.ActionRow(title="Scripts folder", use_markup=False, subtitle_lines=2)
        self.folder_reset = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Back to the default folder",
                                       valign=Gtk.Align.CENTER, css_classes=["flat"], visible=False)
        self.folder_reset.connect("clicked", lambda *_: self.app.set_scripts_dir(None))
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER, css_classes=["flat"])
        change.connect("clicked", self._choose_folder)
        show = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Show in the file manager",
                          action_name="app.open-folder", valign=Gtk.Align.CENTER, css_classes=["flat"])
        for button in (self.folder_reset, change, show):
            self.folder_row.add_suffix(button)
        group.add(self.folder_row)
        group.add(Adw.SpinRow(title="Fixed speed", subtitle="Words per minute when voice follow is off", adjustment=self.wpm_adj))
        self.display_row = Adw.ComboRow(title="Show the script on", subtitle="Automatic finds the Elgato Prompter")
        self.display_row.connect("notify::selected", self._display_picked)
        group.add(self.display_row)
        page.add(group)

        self.voice_group = Adw.PreferencesGroup(title="Voice follow")
        self.mic_row = Adw.ComboRow(title="Microphone")
        self.mic_row.connect("notify::selected", self._mic_picked)
        self.voice_group.add(self.mic_row)
        self.language_row = Adw.ComboRow(title="Language", subtitle="What you read in",
                                         model=Gtk.StringList.new([name for _, name in LANGUAGES]))
        self.language_row.connect("notify::selected", self._speech_picked)
        self.voice_group.add(self.language_row)
        self.model_row = Adw.ActionRow(title="Speech model", activatable=True, use_markup=False)
        self.model_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.model_row.connect("activated", lambda *_: self.show_models())
        self.voice_group.add(self.model_row)
        self.device_row = Adw.ComboRow(title="Run on", subtitle="Processor keeps the graphics card free",
                                       model=Gtk.StringList.new([label for _, label in DEVICES]))
        self.device_row.connect("notify::selected", self._device_picked)
        self.voice_group.add(self.device_row)
        page.add(self.voice_group)

        group = Adw.PreferencesGroup(title="Text", description="How the script looks on the Prompter")
        reset = Gtk.Button(label="Reset", valign=Gtk.Align.CENTER, css_classes=["flat"])
        reset.connect("clicked", lambda *_: self.app.reset_style())
        group.set_header_suffix(reset)
        self.font = Gtk.Button(valign=Gtk.Align.CENTER,
                               child=Gtk.Label(ellipsize=Pango.EllipsizeMode.END, max_width_chars=24))
        self.font.connect("clicked", self._pick_font)
        group.add(_suffix_row("Font", self.font))
        group.add(Adw.SpinRow(title="Size", subtitle="Pixels", adjustment=self.size_adj))
        group.add(self._spin_row("appearance", "line_spacing", "Line spacing", None, 0.9, 2.0, 0.05, 2))
        group.add(self._spin_row("appearance", "margin", "Side margins", "% of the width", 0, 25, 0.5, 1))
        group.add(self._spin_row("appearance", "reading_line", "Reading line", "% down from the top", 10, 90, 1, 0))
        group.add(_suffix_row("Text colour", self._color_button("appearance", "text_color")))
        group.add(_suffix_row("Background", self._color_button("appearance", "background")))
        group.add(_suffix_row("Accent", self._color_button("appearance", "accent"), "Headings and the reading marker"))
        page.add(group)

        group = Adw.PreferencesGroup(title="Eye-line crosshair",
                                     description="A target over the camera lens, so you know where to look")
        self.cross_row = Adw.SwitchRow(title="Show crosshair")
        self.cross_row.connect("notify::active", lambda r, _p: self._updating or self.app.set_style("crosshair", "enabled", r.get_active()))
        group.add(self.cross_row)
        self.find_lens = Gtk.ToggleButton(label="Start", valign=Gtk.Align.CENTER)
        self.find_lens.connect("toggled", lambda b: self._updating or self.app.set_calibrating(b.get_active()))
        self.find_lens_row = _suffix_row("Find the lens", self.find_lens)
        group.add(self.find_lens_row)
        self.set_calibrating(False)
        group.add(self._spin_row("crosshair", "x", "Across", "% of the width", 0, 100, 0.5, 1))
        group.add(self._spin_row("crosshair", "y", "Down", "% of the height", 0, 100, 0.5, 1))
        group.add(self._spin_row("crosshair", "size", "Size", "Pixels", 16, 240, 2, 0))
        group.add(self._spin_row("crosshair", "opacity", "Opacity", "Percent", 10, 100, 5, 0))
        group.add(_suffix_row("Colour", self._color_button("crosshair", "color")))
        to_line = Adw.ButtonRow(title="Put the Reading Line at the Crosshair")
        to_line.connect("activated", lambda *_: self.app.reading_line_to_crosshair())
        group.add(to_line)
        page.add(group)

        group = Adw.PreferencesGroup(title="Camera self-view", description="What the camera sees, in a corner of the Prompter")
        self.preview_row = Adw.SwitchRow(title="Show self-view", subtitle="Off", use_markup=False)
        self.preview_row.connect("notify::active", lambda r, _p: self._updating or self.app.set_preview("enabled", r.get_active()))
        group.add(self.preview_row)
        self.camera_row = Adw.ComboRow(title="Camera", subtitle="Only one app can use a camera at a time")
        self.camera_row.connect("notify::selected", self._camera_picked)
        group.add(self.camera_row)
        self.mirror_row = Adw.SwitchRow(title="Mirror")
        self.mirror_row.connect("notify::active", lambda r, _p: self._updating or self.app.set_preview("mirror", r.get_active()))
        group.add(self.mirror_row)
        self.corner_row = Adw.ComboRow(title="Corner", model=Gtk.StringList.new([label for _, label in CORNERS]))
        self.corner_row.connect("notify::selected", lambda r, _p: self._updating or self.app.set_preview("corner", CORNERS[r.get_selected()][0]))
        group.add(self.corner_row)
        self.preview_width = self._adjustment(15, 60, 1, lambda v: self.app.set_preview("size", int(v)))
        group.add(Adw.SpinRow(title="Width", subtitle="% of the Prompter", adjustment=self.preview_width))
        page.add(group)

        group = Adw.PreferencesGroup(title="Timer", description="On the Prompter, while you read")
        self.timer_row = Adw.SwitchRow(title="Show timer")
        self.timer_row.connect("notify::active", lambda r, _p: self._updating or self.app.set_readout("timer", r.get_active()))
        group.add(self.timer_row)
        self.timer_shows_row = Adw.ComboRow(title="Shows", model=Gtk.StringList.new([label for _, label, _ in TIMER_SHOWS]))
        self.timer_shows_row.connect("notify::selected", self._timer_shows_picked)
        group.add(self.timer_shows_row)
        self.timer_position_row = self._position_row("timer_position")
        group.add(self.timer_position_row)
        page.add(group)

        group = Adw.PreferencesGroup(title="Progress", description="How far through the script you are, in percent")
        self.progress_row = Adw.SwitchRow(title="Show progress")
        self.progress_row.connect("notify::active", lambda r, _p: self._updating or self.app.set_readout("progress", r.get_active()))
        group.add(self.progress_row)
        self.progress_position_row = self._position_row("progress_position")
        group.add(self.progress_position_row)
        page.add(group)

        self.screen_group = Adw.PreferencesGroup(title="Prompter screen")
        self.screen_rows = []
        for name, title in (("brightness", "Brightness"), ("contrast", "Contrast")):
            scale = Gtk.Scale(adjustment=self.screen_adj[name], draw_value=True, digits=0,
                              value_pos=Gtk.PositionType.LEFT, width_request=260, valign=Gtk.Align.CENTER)
            row = _suffix_row(title, scale)
            row.set_sensitive(False)  # until the Prompter has reported its range
            self.screen_rows.append(row)
            self.screen_group.add(row)
        page.add(self.screen_group)

        group = Adw.PreferencesGroup(title="Diagnostics")
        self.record_row = Adw.SwitchRow(title="Save sessions for tuning",
                                        subtitle="Records the mic while voice follow listens, with every transcript")
        self.record_row.connect("notify::active", lambda r, _p: self._updating or self.app.set_recording(r.get_active()))
        group.add(self.record_row)
        self.delete_sessions = Gtk.Button(label="Delete…", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
        self.delete_sessions.connect("clicked", self._confirm_delete)
        self.sessions_row = _suffix_row("Saved sessions", self.delete_sessions)
        group.add(self.sessions_row)
        page.add(group)
        return page

    def _position_row(self, key):
        row = Adw.ComboRow(title="Position", model=Gtk.StringList.new([label for _, label in POSITIONS]))
        row.connect("notify::selected", self._position_picked, key)
        return row

    def _spin_row(self, group, key, title, subtitle, lo, hi, step, digits):
        adj = self._adjustment(lo, hi, step, lambda v: self.app.set_style(group, key, _number(v, digits)))
        self.numbers[(group, key)] = adj
        return Adw.SpinRow(title=title, subtitle=subtitle or "", adjustment=adj, digits=digits)

    def _color_button(self, group, key):
        button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        button.connect("notify::rgba", lambda b, _p: self._updating or self.app.set_style(group, key, _hex(b.get_rgba())))
        self.colors[(group, key)] = button
        return button

    def _adjustment(self, lo, hi, step, on_change):
        adj = Gtk.Adjustment(lower=lo, upper=hi, step_increment=step, page_increment=step * 5)
        adj.connect("value-changed", lambda a: self._updating or on_change(a.get_value()))
        return adj

    # --- header: scripts and menu ---------------------------------------------

    def _scripts_button(self):
        self.files = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.files.set_placeholder(Gtk.Label(label="No scripts yet", margin_top=18, margin_bottom=18, css_classes=["dim-label"]))
        self.files.connect("row-activated", self._file_picked)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        box.append(Gtk.ScrolledWindow(child=self.files, propagate_natural_height=True, max_content_height=420,
                                      min_content_width=320, hscrollbar_policy=Gtk.PolicyType.NEVER))
        buttons = Gtk.Box(spacing=6, homogeneous=True)
        buttons.append(Gtk.Button(label="Open Folder", action_name="app.open-folder"))
        rescan = Gtk.Button(label="Rescan")
        rescan.connect("clicked", lambda *_: self.app.refresh_files())
        buttons.append(rescan)
        box.append(buttons)
        self.files_popover = Gtk.Popover(child=box)
        return Gtk.MenuButton(icon_name="document-open-symbolic", tooltip_text="Scripts", popover=self.files_popover)

    def _menu_button(self):
        menu = Gio.Menu()
        for items in ((("Find the Lens", "app.calibrate"), ("Reload Script", "app.reload")),
                      (("Keyboard Shortcuts", "app.shortcuts"),),
                      (("Quit", "app.quit"),)):
            section = Gio.Menu()
            for label, action in items:
                section.append(label, action)
            menu.append_section(None, section)
        return Gtk.MenuButton(icon_name="open-menu-symbolic", tooltip_text="Menu", menu_model=menu)

    def show_shortcuts(self):
        page = Adw.PreferencesPage()
        everywhere = Adw.PreferencesGroup(title="Anywhere")
        if not self.app.shortcuts.available:
            everywhere.set_description(GLib.markup_escape_text(
                f"{self.app.shortcuts.problem or 'Global shortcuts are unavailable.'} "
                "You can also bind keys to “prompterctl ACTION” in your desktop's settings."))
        else:
            everywhere.set_description("Your desktop decides the keys: assign them in its shortcut settings, or "
                                       "on Hyprland with the global dispatcher (see the README).")
        for action, what in [(a, w) for _k, a, w, _t in KEYS] + [(a, w) for a, w, _t in GLOBAL_ONLY]:
            trigger = self._global_triggers.get(action)
            row = Adw.ActionRow(title=what, subtitle=f"prompterctl {action}")
            row.add_suffix(Gtk.Label(label=trigger or "Not set", css_classes=[] if trigger else ["dim-label"]))
            everywhere.add(row)
        page.add(everywhere)
        here = Adw.PreferencesGroup(title="In this window, on the Prompt tab")
        for key, _action, what, _trigger in KEYS:
            row = Adw.ActionRow(title=what)
            row.add_suffix(Adw.ShortcutLabel(accelerator=key, valign=Gtk.Align.CENTER))
            here.add(row)
        page.add(here)
        view = Adw.ToolbarView(content=page)
        view.add_top_bar(Adw.HeaderBar())
        Adw.Dialog(title="Keyboard Shortcuts", child=view, content_width=520, content_height=640).present(self)

    # --- updates from the app -----------------------------------------------

    def set_files(self, entries, current):
        while row := self.files.get_row_at_index(0):
            self.files.remove(row)
        for name, title, subtitle in entries:
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable=True, use_markup=False)
            if name == current:
                row.add_suffix(Gtk.Image(icon_name="object-select-symbolic"))
            row.file = name
            self.files.append(row)

    def set_scripts_dir(self, folder, custom):
        self.folder_row.set_subtitle(_tilde(folder))
        self.folder_reset.set_visible(custom)

    def set_script(self, script, title):
        self.title.set_label(title)
        buf = self.text.get_buffer()
        buf.set_text("")
        self._token_chars, self._token_blocks, self._block_chars = [], [], []
        if script is None:
            self.where.set_label("Add Markdown files to the scripts folder")
            self.progress.set_fraction(0)
        for b, block in enumerate(script.blocks if script else []):
            if b:
                buf.insert(buf.get_end_iter(), "\n\n")
            start = buf.get_char_count()
            self._block_chars.append((start, start + len(block.text)))
            buf.insert_with_tags_by_name(buf.get_end_iter(), block.text, *(["heading"] if block.kind == "heading" else []))
            raw = block.text.encode()
            for s, e in block.cues:
                buf.apply_tag(self.cue_tag, buf.get_iter_at_offset(start + len(raw[:s].decode())),
                              buf.get_iter_at_offset(start + len(raw[:e].decode())))
            for t in script.tokens[block.first_token : block.end_token]:
                self._token_chars.append((start + len(raw[: t.start].decode()), start + len(raw[: t.end].decode())))
                self._token_blocks.append(b)

        while row := self.sections.get_row_at_index(0):
            self.sections.remove(row)
        for section in script.sections if script else []:
            row = Gtk.ListBoxRow(child=Gtk.Label(label=section.title, xalign=0, margin_start=6, margin_end=12,
                                                 margin_top=4, margin_bottom=4))
            row.token = section.token
            self.sections.append(row)

    def set_position(self, pos, section, where, fraction):
        buf = self.text.get_buffer()
        for tag in (self.spoken_tag, self.paragraph_tag, self.here_tag):
            buf.remove_tag(tag, buf.get_start_iter(), buf.get_end_iter())
        if pos < len(self._token_chars):
            start, end = self._token_chars[pos]
            buf.apply_tag(self.here_tag, buf.get_iter_at_offset(start), buf.get_iter_at_offset(end))
            p_start, p_end = self._block_chars[self._token_blocks[pos]]
            buf.apply_tag(self.paragraph_tag, buf.get_iter_at_offset(p_start), buf.get_iter_at_offset(p_end))
        else:
            start = buf.get_char_count()
        here = buf.get_iter_at_offset(start)
        buf.apply_tag(self.spoken_tag, buf.get_start_iter(), here)
        buf.move_mark(self.mark, here)
        self.text.scroll_to_mark(self.mark, 0.0, True, 0.0, 0.3)

        self.where.set_label(where)
        self.progress.set_fraction(fraction)
        row = self.sections.get_row_at_index(section)
        if row and self.sections.get_selected_row() is not row:
            self.sections.select_row(row)

    def set_state(self, playing, voice, wpm, font_size):
        self._updating = True
        self.play.set_icon_name("media-playback-pause-symbolic" if playing else "media-playback-start-symbolic")
        self.play.set_tooltip_text("Pause (Space)" if playing else "Start (Space)")
        self.mode.set_active_name("voice" if voice else "fixed")
        self._voice = voice
        self._show_mode_detail()
        self.wpm_adj.set_value(wpm)
        self.size_adj.set_value(font_size)
        self.size_label.set_label(f"{font_size} px")
        self._updating = False

    def set_level(self, rms):
        db = 20 * math.log10(max(rms, 1e-6))
        self.level.set_value(min(1.0, max(0.0, (db + 60) / 60)))

    def set_status(self, text):
        self.status.set_label(text)

    def set_style(self, appearance, crosshair):
        self._updating = True
        self._font = appearance["font"]
        self.font.get_child().set_label(self._font)
        self.font.set_tooltip_text(self._font)
        values = {"appearance": appearance, "crosshair": crosshair}
        for (group, key), button in self.colors.items():
            button.set_rgba(rgba(values[group][key]))
        for (group, key), adj in self.numbers.items():
            adj.set_value(values[group][key])
        self.cross_row.set_active(crosshair["enabled"])
        self.quick_crosshair.set_active(crosshair["enabled"])
        self._updating = False

    def set_calibrating(self, on):
        self._updating = True
        self.find_lens.set_active(on)
        self.find_lens.set_label("Done" if on else "Start")
        self._updating = False
        self.find_lens_row.set_subtitle(
            "The Prompter is black so the glass turns see-through. Arrow keys move the crosshair onto the "
            "lens (Shift for fine steps); Enter when it's there." if on else
            "Blacks out the Prompter so you can see the lens through the glass")

    def set_preview(self, preview):
        self._updating = True
        self.preview_row.set_active(preview["enabled"])
        self.quick_preview.set_active(preview["enabled"])
        self.mirror_row.set_active(preview["mirror"])
        corners = [c for c, _ in CORNERS]
        self.corner_row.set_selected(corners.index(preview["corner"]) if preview["corner"] in corners else 0)
        self.preview_width.set_value(preview["size"])
        self._updating = False

    def set_readouts(self, readouts):
        self._updating = True
        self.timer_row.set_active(readouts["timer"])
        self.quick_timer.set_active(readouts["timer"])
        self.progress_row.set_active(readouts["progress"])
        self.quick_progress.set_active(readouts["progress"])
        shows = [value for value, _, _ in TIMER_SHOWS]
        selected = shows.index(readouts["timer_shows"]) if readouts["timer_shows"] in shows else 0
        self.timer_shows_row.set_selected(selected)
        self.timer_shows_row.set_subtitle(TIMER_SHOWS[selected][2])
        positions = [value for value, _ in POSITIONS]
        shared = readouts["timer"] and readouts["progress"] and readouts["timer_position"] == readouts["progress_position"]
        for row, key, other in ((self.timer_position_row, "timer_position", "progress"),
                                (self.progress_position_row, "progress_position", "timer")):
            row.set_selected(positions.index(readouts[key]) if readouts[key] in positions else 0)
            row.set_subtitle(f"Beside the {other}" if shared else "")
        self._updating = False

    def set_camera_status(self, text):
        self.preview_row.set_subtitle(text)

    def set_screen(self, values):
        """values: {name: (current, max)} from the Prompter, or a message when it can't be reached."""
        if isinstance(values, str):
            self.screen_group.set_description(GLib.markup_escape_text(values))
            return
        self._updating = True
        for name, (current, maximum) in values.items():
            self.screen_adj[name].set_upper(maximum)
            self.screen_adj[name].set_value(current)
        self._updating = False
        for row in self.screen_rows:
            row.set_sensitive(True)
        self.quick_brightness.set_sensitive(True)
        self.screen_group.set_description("")

    def set_prompter_found(self, found):
        self.banner.set_revealed(not found)

    def set_global_shortcuts(self, triggers):
        self._global_triggers = triggers

    def set_displays(self, monitors, current):
        """monitors: [(connector, label)]; current None means find the Elgato Prompter."""
        self._display_connectors = [None] + [c for c, _ in monitors]
        labels = ["Automatic"] + [label for _, label in monitors]
        self._set_combo(self.display_row, labels, self._display_connectors, current)

    def set_cameras(self, devices, current):
        self._camera_paths = [path for path, _ in devices]
        labels = [f"{name} ({path})" for path, name in devices]
        if current and _realpath(current) not in [_realpath(p) for p in self._camera_paths]:
            self._camera_paths.append(current)  # chosen before, but not there right now
            labels.append(f"{current} (not connected)")
        if not self._camera_paths:
            self._camera_paths, labels = [None], ["No cameras found"]
        real = [_realpath(p) for p in self._camera_paths]
        self._set_combo(self.camera_row, labels, real, _realpath(current) if current else None)

    def set_speech(self, language, model, device):
        self._updating = True
        codes = [code for code, _ in LANGUAGES]
        self.language_row.set_selected(codes.index(language) if language in codes else 0)
        devices = [code for code, _ in DEVICES]
        self.device_row.set_selected(devices.index(device) if device in devices else 0)
        self.model_row.set_subtitle(models.title(model))
        self._updating = False
        if self.models_dialog:  # after this: the change may have come from one of its rows
            self.models_dialog.language_name = self._language_name()
            GLib.idle_add(lambda: self.models_changed() and False)

    def set_engine(self, engine, missing=None):
        """engine: (model, device) when it's running; missing: the model it needs, when there's none."""
        self._missing = missing is not None
        self._show_mode_detail()
        if missing:
            self.voice_group.set_description(
                f"Needs a speech model for {self._language_name()}: choose Speech model to download one")
        elif engine is None:
            self.voice_group.set_description("Loading the speech model…")
        else:
            model, device = engine
            where = "on the graphics card" if device == "cuda" else "on the processor (an NVIDIA graphics card follows more closely)"
            self.voice_group.set_description(GLib.markup_escape_text(f"Running {models.title(model)} {where}"))

    def show_models(self):
        if self.models_dialog is None:
            self.models_dialog = ModelsDialog(self.app, self._language_name())
            self.models_dialog.connect("closed", lambda *_: setattr(self, "models_dialog", None))
        self.models_dialog.present(self)

    def set_download(self, name, fraction):
        if self.models_dialog:
            self.models_dialog.set_download(name, fraction)
        self.missing_label.set_visible(name is not None)
        self.missing_label.set_label(f"Downloading… {fraction:.0%}")

    def models_changed(self):
        if self.models_dialog:
            self.models_dialog.refresh()

    def models_message(self, text):
        if self.models_dialog:
            self.models_dialog.message(text)
        else:
            self.set_status(text)

    def _show_mode_detail(self):
        self.mode_detail.set_visible_child_name("fixed" if not self._voice else "missing" if self._missing else "voice")

    def _language_name(self):
        selected = self.language_row.get_selected()
        return LANGUAGES[selected if selected < len(LANGUAGES) else 0][1]

    def _set_combo(self, row, labels, values, current):
        """Replace a dropdown's choices only if they changed: rebuilding the list re-emits
        "selected" after the fact, which would loop straight back into the app."""
        self._updating = True
        model = row.get_model()
        if model is None or [model.get_string(i) for i in range(model.get_n_items())] != labels:
            row.set_model(Gtk.StringList.new(labels))
        index = values.index(current) if current in values else 0
        if row.get_selected() != index:
            row.set_selected(index)
        self._updating = False

    def set_mics(self, mics, current):
        self._mic_names = [None] + [name for name, _ in mics]
        labels = ["System default"] + [description for _, description in mics]
        self._set_combo(self.mic_row, labels, self._mic_names, current)

    def set_recording(self, on, count, size):
        self._updating = True
        self.record_row.set_active(on)
        self._updating = False
        self.sessions_row.set_subtitle(f"{count} session{'s' if count != 1 else ''} · {size / 1e6:.0f} MB" if count else "None")
        self.delete_sessions.set_sensitive(count > 0)

    # --- input ----------------------------------------------------------------

    def _prompt_keys(self):
        """Single-key shortcuts: only on the Prompt tab, and only if nothing focused uses the key."""
        controller = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
        for key, action, _what, _trigger in KEYS:
            controller.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(key),
                                                 action=Gtk.CallbackAction.new(self._prompt_key, action)))
        return controller

    def _prompt_key(self, _widget, _args, action):
        if self.stack.get_visible_child_name() != "prompt":
            return False
        self.app.activate_action(action)
        return True

    def _calibration_key(self, _controller, keyval, _code, state):
        if not self.app.calibrating:
            return False
        step = 0.2 if state & Gdk.ModifierType.SHIFT_MASK else 1.0
        moves = {Gdk.KEY_Left: (-step, 0), Gdk.KEY_Right: (step, 0), Gdk.KEY_Up: (0, -step), Gdk.KEY_Down: (0, step)}
        if keyval in moves:
            self.app.nudge_crosshair(*moves[keyval])
        elif keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Escape):
            self.app.set_calibrating(False)
        else:
            return False
        return True

    def _mode_picked(self, group, _pspec):
        if not self._updating and group.get_active_name():
            self.app.set_mode(group.get_active_name())

    def _file_picked(self, _list, row):
        self.files_popover.popdown()
        self.app.open_named(row.file)

    def _section_picked(self, _list, row):
        self.sections_popover.popdown()
        self.app.jump(row.token)

    def _display_picked(self, row, _pspec):
        if not self._updating and 0 <= row.get_selected() < len(self._display_connectors):
            self.app.set_display(self._display_connectors[row.get_selected()])

    def _camera_picked(self, row, _pspec):
        if not self._updating and 0 <= row.get_selected() < len(self._camera_paths):
            self.app.set_camera(self._camera_paths[row.get_selected()])

    def _speech_picked(self, row, _pspec):
        if not self._updating and 0 <= row.get_selected() < len(LANGUAGES):
            self.app.set_speech(language=LANGUAGES[row.get_selected()][0])

    def _device_picked(self, row, _pspec):
        if not self._updating and 0 <= row.get_selected() < len(DEVICES):
            self.app.set_speech(device=DEVICES[row.get_selected()][0])

    def _choose_folder(self, *_):
        dialog = Gtk.FileDialog(title="Choose the Scripts Folder", modal=True,
                                initial_folder=Gio.File.new_for_path(str(self.app.folder)))
        dialog.select_folder(self, None, self._folder_chosen)

    def _folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # cancelled
        if folder and folder.get_path():
            self.app.set_scripts_dir(folder.get_path())

    def _position_picked(self, row, _pspec, key):
        if not self._updating and 0 <= row.get_selected() < len(POSITIONS):
            self.app.set_readout(key, POSITIONS[row.get_selected()][0])

    def _timer_shows_picked(self, row, _pspec):
        if not self._updating and 0 <= row.get_selected() < len(TIMER_SHOWS):
            self.app.set_readout("timer_shows", TIMER_SHOWS[row.get_selected()][0])

    def _mic_picked(self, row, _pspec):
        if not self._updating and 0 <= row.get_selected() < len(self._mic_names):
            self.app.set_mic(self._mic_names[row.get_selected()])

    def _pick_font(self, _button):
        selected = self.language_row.get_selected()
        language, name = LANGUAGES[selected if selected < len(LANGUAGES) else 0]
        FontPicker(self._font, lambda font: self.app.set_style("appearance", "font", font), language, name).present(self)

    def _text_clicked(self, _gesture, _n, x, y):
        if not self._token_chars:
            return
        bx, by = self.text.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        found, it = self.text.get_iter_at_location(bx, by)
        if found:
            starts = [s for s, _ in self._token_chars]
            self.app.jump(max(0, bisect.bisect_right(starts, it.get_offset()) - 1))

    def _color_tags(self):
        """Read-text dimming and the current-word highlight, in the theme's own colours."""
        fg = self.text.get_color()
        dim = Gdk.RGBA()
        dim.red, dim.green, dim.blue, dim.alpha = fg.red, fg.green, fg.blue, 0.42
        self.spoken_tag.set_property("foreground-rgba", dim)
        self.cue_tag.set_property("foreground-rgba", dim)
        accent = theme_accent()
        tint = accent.copy()
        tint.alpha = 0.16
        self.paragraph_tag.set_property("paragraph-background-rgba", tint)
        if not Adw.StyleManager.get_default().get_dark():  # pastel accents need deepening on light cards
            accent.red, accent.green, accent.blue = (c * 0.55 for c in (accent.red, accent.green, accent.blue))
        self.here_tag.set_property("underline-rgba", accent)

    def _confirm_delete(self, *_):
        dialog = Adw.AlertDialog(heading="Delete saved sessions?",
                                 body="Their mic recordings and transcripts are removed for good.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, response: response == "delete" and self.app.delete_sessions())
        dialog.present(self)


def _round(icon, action, tooltip):
    return Gtk.Button(icon_name=icon, action_name=action, tooltip_text=tooltip, valign=Gtk.Align.CENTER,
                      css_classes=["circular"], focusable=False)


def _flat(icon, action, tooltip):
    return Gtk.Button(icon_name=icon, action_name=action, tooltip_text=tooltip, css_classes=["flat"], focusable=False)


def _suffix_row(title, widget, subtitle=None):
    row = Adw.ActionRow(title=title, subtitle=subtitle or "", use_markup=False)  # paths may contain & or <
    row.add_suffix(widget)
    row.set_activatable_widget(widget)
    return row


def _tilde(path):
    path = str(path)
    home = str(Path.home())
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _realpath(path):
    return os.path.realpath(path) if path else path


def _hex(c):
    return "#{:02x}{:02x}{:02x}".format(*(round(x * 255) for x in (c.red, c.green, c.blue)))


def _number(value, digits):
    return int(round(value)) if digits == 0 else round(value, digits)


USER_COLORS = Path.home() / ".config" / "gtk-4.0" / "colors.css"


def _user_colors():
    """@define-color values from the user's GTK colours (theme generators such as Matugen write these)."""
    try:
        text = USER_COLORS.read_text()
    except OSError:
        return {}
    return dict(re.findall(r"@define-color\s+(\w+)\s+(#[0-9a-fA-F]{6})\s*;", text))


def theme_accent():
    color = _user_colors().get("accent_bg_color")
    return rgba(color) if color else Adw.StyleManager.get_default().get_accent_color_rgba()


def _install_css():
    """Our tweaks, plus the theme's accent as libadwaita's CSS variables: themes that only set the
    old @define-color names would otherwise get stock blue from current libadwaita."""
    colors = _user_colors()
    accent = ""
    if "accent_bg_color" in colors:
        accent = f":root {{ --accent-bg-color: {colors['accent_bg_color']};"
        if "accent_fg_color" in colors:
            accent += f" --accent-fg-color: {colors['accent_fg_color']};"
        accent += " }\n"
    provider = Gtk.CssProvider()
    provider.load_from_string(accent + CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
