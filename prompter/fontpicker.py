"""A font picker that shows each font on the Prompter as you move through the list.

GTK's font dialog only hands back a font once you press Select, so you can't see how a font
reads on the Prompter until you've committed to it. This one applies every font you land on,
by clicking or with the arrow keys, and puts the original back if you cancel. It only lists
fonts that can write the language you read in, so the dozens of fonts for other scripts that
would just fall back to the same default font aren't in the way.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402


class FontPicker(Adw.Dialog):
    def __init__(self, font, on_change, language="en", language_name="English"):
        """font: the current Pango font description, as a string. on_change(font) for each font
        you land on, and with the original again if you cancel. language: an ISO 639-1 code."""
        super().__init__(title="Font", content_width=420, content_height=640)
        self.original = self.font = font
        self.on_change = on_change
        self.faces = []
        self._done = False
        self._updating = False
        self._pending = 0
        current = Pango.FontDescription.from_string(font)
        self._want = (current.get_weight(), current.get_style())  # kept while you move between families

        context = self.get_pango_context()
        every = context.get_font_map().list_families()
        families = for_language(context, every, language, keep=current.get_family())
        families.sort(key=lambda f: f.get_name().casefold())
        self.families = {f.get_name(): f for f in families}
        names = [f.get_name() for f in families]
        self.filter = Gtk.StringFilter(expression=Gtk.PropertyExpression.new(Gtk.StringObject, None, "string"),
                                       ignore_case=True, match_mode=Gtk.StringFilterMatchMode.SUBSTRING)
        # No autoselect, set before the model so nothing starts selected: narrowing the search
        # shouldn't change the font on the Prompter
        self.selection = Gtk.SingleSelection(autoselect=False, can_unselect=True)
        self.selection.set_model(Gtk.FilterListModel(model=Gtk.StringList.new(names), filter=self.filter))
        self.selection.connect("notify::selected-item", self._family_picked)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, item: item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                                                          margin_top=4, margin_bottom=4)))
        factory.connect("bind", _bind_family)
        self.list = Gtk.ListView(model=self.selection, factory=factory, css_classes=["navigation-sidebar"])
        self.list.connect("activate", lambda *_: self._finish())  # double-click or Enter

        self.search = Gtk.SearchEntry(placeholder_text="Search fonts", hexpand=True)
        self.search.connect("search-changed", lambda entry: self.filter.set_search(entry.get_text()))
        self.search.connect("activate", lambda *_: self._focus_list())
        self.search.connect("stop-search", self._stop_search)
        self.search.set_key_capture_widget(self)  # type anywhere to search
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._search_key)
        self.search.add_controller(keys)

        self.style_row = Adw.ComboRow(title="Style", sensitive=False)
        self.style_row.connect("notify::selected", self._face_picked)
        styles = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                             margin_start=12, margin_end=12, margin_top=6, margin_bottom=12)
        styles.append(self.style_row)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        done = Gtk.Button(label="Done", css_classes=["suggested-action"])
        done.connect("clicked", lambda *_: self._finish())
        subtitle = f"Fonts that can write {language_name}" if len(families) < len(every) else ""
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False,
                               title_widget=Adw.WindowTitle(title="Font", subtitle=subtitle))
        header.pack_start(cancel)
        header.pack_end(done)
        search_bar = Gtk.Box(margin_start=12, margin_end=12, margin_bottom=6)
        search_bar.append(self.search)

        view = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=self.list, vexpand=True,
                                                          hscrollbar_policy=Gtk.PolicyType.NEVER))
        view.add_top_bar(header)
        view.add_top_bar(search_bar)
        view.add_bottom_bar(styles)
        self.set_child(view)
        self.connect("closed", self._closed)

        family = current.get_family()
        if family in self.families:
            self._position = names.index(family)
            self._updating = True
            self.selection.set_selected(self._position)
            self._updating = False
            self._show_faces(self.families[family], current)
            self.list.add_tick_callback(self._scroll_to_current)
        else:  # not installed here: the list starts unselected
            self._position = None

    def _scroll_to_current(self, _widget, _clock):
        """Once the list has its size: before then, scrolling to a row does nothing."""
        if not self.list.get_height():
            return GLib.SOURCE_CONTINUE
        self.list.scroll_to(self._position, Gtk.ListScrollFlags.FOCUS, None)
        self.list.grab_focus()  # the dialog focuses nothing by itself, so the arrow keys would do nothing
        return GLib.SOURCE_REMOVE

    def _family_picked(self, selection, _pspec):
        item = selection.get_selected_item()
        if self._updating or item is None:
            return
        face = self._show_faces(self.families[item.get_string()])
        self._apply(face)

    def _show_faces(self, family, current=None):
        """Fill the Style row with the family's faces and select the one nearest the wanted
        weight and slant (or the current font). Returns that face."""
        self.faces = sorted(family.list_faces(), key=lambda f: _face_order(f.describe()))
        descs = [f.describe() for f in self.faces]
        target = (current.get_weight(), current.get_style()) if current else self._want
        best = closest(descs, *target)
        self._updating = True
        self.style_row.set_model(Gtk.StringList.new([f.get_face_name() for f in self.faces]))
        self.style_row.set_selected(best)
        self.style_row.set_sensitive(len(self.faces) > 1)
        self._updating = False
        return self.faces[best]

    def _face_picked(self, row, _pspec):
        if self._updating or not 0 <= row.get_selected() < len(self.faces):
            return
        face = self.faces[row.get_selected()]
        desc = face.describe()
        self._want = (desc.get_weight(), desc.get_style())
        self._apply(face)

    def _apply(self, face):
        desc = face.describe()
        desc.unset_fields(Pango.FontMask.SIZE)  # size has its own control
        self.font = desc.to_string()
        # Holding an arrow key moves faster than the Prompter can relayout: apply only the latest
        if not self._pending:
            self._pending = GLib.idle_add(self._flush)

    def _flush(self):
        self._pending = 0
        self.on_change(self.font)
        return GLib.SOURCE_REMOVE

    def _focus_list(self):
        if not self.selection.get_n_items():
            return
        selected = self.selection.get_selected()
        position = selected if selected != Gtk.INVALID_LIST_POSITION else 0
        self.list.scroll_to(position, Gtk.ListScrollFlags.FOCUS | Gtk.ListScrollFlags.SELECT, None)

    def _search_key(self, _controller, keyval, _code, _state):
        if keyval in (Gdk.KEY_Down, Gdk.KEY_KP_Down):
            self._focus_list()
            return True
        return False

    def _stop_search(self, entry):
        if entry.get_text():
            entry.set_text("")
        else:
            self.close()

    def _finish(self):
        self._done = True
        self.close()

    def _closed(self, _dialog):
        if self._pending:
            GLib.source_remove(self._pending)
            self._pending = 0
        self.on_change(self.font if self._done else self.original)


def for_language(context, families, language, keep=None):
    """The families with every letter of Pango's sample sentence for language (a pangram, so
    it has the letters that language needs), plus keep, the current family, so the list can
    start on it. All of them if none have the letters."""
    sample = Pango.Language.from_string(language).get_sample_string()
    letters = {c for c in sample.upper() + sample.lower() if c.isalpha()}
    fontmap = context.get_font_map()

    def writes(family):
        face = family.get_face(None)
        if face is None:
            return False
        desc = face.describe()
        desc.set_size(12 * Pango.SCALE)
        font = fontmap.load_font(context, desc)
        return font is not None and all(font.has_char(c) for c in letters)

    able = [f for f in families if f.get_name() == keep or writes(f)]
    return able if any(f.get_name() != keep for f in able) else list(families)


def _bind_family(_factory, item):
    """Each family's name, written in that family."""
    name = item.get_item().get_string()
    attrs = Pango.AttrList()
    attrs.insert(Pango.attr_family_new(name))
    attrs.insert(Pango.attr_scale_new(1.3))
    label = item.get_child()
    label.set_label(name)
    label.set_attributes(attrs)


def _face_order(desc):
    """Upright before italic, then lightest to heaviest."""
    return desc.get_style() != Pango.Style.NORMAL, int(desc.get_weight()), int(desc.get_stretch())


def closest(descs, weight, style):
    """Index of the face nearest a weight and slant: the slant matters more."""
    def distance(desc):
        return (desc.get_style() != style) * 10000 + abs(int(desc.get_weight()) - int(weight))
    return min(range(len(descs)), key=lambda i: distance(descs[i]))
