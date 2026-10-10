"""A tray icon, so Prompter can be brought back after its window is closed.

GTK 4 has no tray API, so this speaks the protocols directly: StatusNotifierItem for the icon
(KDE Plasma, waybar, most Wayland bars; GNOME with the AppIndicator extension) and dbusmenu
for its menu. Clicking the icon calls on_activate, middle-clicking on_secondary, and the menu
is a flat list of items set with set_menu().
"""

import os

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gio, GLib  # noqa: E402

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

INTERFACES = Gio.DBusNodeInfo.new_for_xml("""
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionMovieName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
    <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus"><arg name="status" type="s"/></signal>
  </interface>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg name="id" type="i"/>
      <arg name="timestamp" type="u"/>
    </signal>
  </interface>
</node>
""")


class MenuItem:
    """label, or None for a separator. checked: None for a plain item, else True or False."""

    def __init__(self, label=None, on_click=None, checked=None, enabled=True):
        self.label, self.on_click, self.checked, self.enabled = label, on_click, checked, enabled

    def props(self):
        if self.label is None:
            return {"type": GLib.Variant("s", "separator")}
        props = {"label": GLib.Variant("s", self.label.replace("_", "__")),  # "_" marks a mnemonic
                 "enabled": GLib.Variant("b", self.enabled)}
        if self.checked is not None:
            props["toggle-type"] = GLib.Variant("s", "checkmark")
            props["toggle-state"] = GLib.Variant("i", int(self.checked))
        return props

    def same(self, other):
        return (self.label, self.checked, self.enabled) == (other.label, other.checked, other.enabled)


SEPARATOR = MenuItem()


class Tray:
    def __init__(self, app_id, title, icon_file, on_activate, on_secondary=None):
        self.app_id, self.title = app_id, title
        self.on_activate, self.on_secondary = on_activate, on_secondary
        self.pixmaps = _pixmap_variant(_pixmaps(icon_file))  # built once: it never changes
        self.tooltip = ""
        self.items = []  # ids are positions + 1; 0 is the root
        self.revision = 1
        self.registered = False  # a tray is showing the icon
        self.bus = None
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return
        _export(self.bus, ITEM_PATH, "org.kde.StatusNotifierItem", self._item_call, self._item_property)
        _export(self.bus, MENU_PATH, "com.canonical.dbusmenu", self._menu_call, self._menu_property)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE, None, None)
        # The tray may start after us, or restart: register whenever a watcher appears
        Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_appeared,
                                         self._watcher_vanished)

    # --- what the app changes -----------------------------------------------

    def set_menu(self, items):
        old, self.items = self.items, items
        if len(old) == len(items) and all((a.label is None) == (b.label is None) for a, b in zip(old, items)):
            # Same items, new labels or checkmarks: menus update those in place
            changed = [(i + 1, a.props(), b.props()) for i, (a, b) in enumerate(zip(old, items)) if not a.same(b)]
            if changed:
                self._emit(MENU_PATH, "com.canonical.dbusmenu", "ItemsPropertiesUpdated", GLib.Variant("(a(ia{sv})a(ias))", (
                    [(i, new) for i, _old, new in changed],
                    [(i, [k for k in old if k not in new]) for i, old, new in changed if old.keys() - new.keys()])))
            return
        self.revision += 1
        self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated", GLib.Variant("(ui)", (self.revision, 0)))

    def set_tooltip(self, text):
        if text != self.tooltip:
            self.tooltip = text
            self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None)

    # --- registration -------------------------------------------------------

    def _watcher_appeared(self, bus, _name, _owner):
        bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                 GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None, self._registered)

    def _registered(self, bus, result):
        try:
            bus.call_finish(result)
            self.registered = True
        except GLib.Error:
            self.registered = False

    def _watcher_vanished(self, _bus, _name):
        self.registered = False

    def _emit(self, path, interface, signal, params):
        if self.bus:
            self.bus.emit_signal(None, path, interface, signal, params)

    # --- StatusNotifierItem --------------------------------------------------

    def _item_property(self, _bus, _sender, _path, _interface, name):
        """Called once per property, on the main thread: trays read them all whenever anything
        changes, so each has to be quick."""
        if name == "IconPixmap":
            return self.pixmaps
        if name == "ToolTip":
            return GLib.Variant("(sa(iiay)ss)", ("", [], self.title, GLib.markup_escape_text(self.tooltip)))
        if name.endswith("Pixmap"):
            return GLib.Variant("a(iiay)", [])
        value = {
            "Category": ("s", "ApplicationStatus"),
            "Id": ("s", self.app_id),
            "Title": ("s", self.title),
            "Status": ("s", "Active"),
            "WindowId": ("i", 0),
            "IconName": ("s", self.app_id),  # from the icon theme once installed; else the pixmaps
            "ItemIsMenu": ("b", False),
            "Menu": ("o", MENU_PATH),
        }.get(name, ("s", ""))
        return GLib.Variant(*value)

    def _item_call(self, _bus, _sender, _path, _interface, method, _params, invocation):
        invocation.return_value(None)
        if method == "Activate":
            GLib.idle_add(_once(self.on_activate))
        elif method == "SecondaryActivate" and self.on_secondary:
            GLib.idle_add(_once(self.on_secondary))

    # --- dbusmenu ------------------------------------------------------------

    def _props(self, item_id, names=()):
        if item_id == 0:
            props = {"children-display": GLib.Variant("s", "submenu")}
        elif 0 < item_id <= len(self.items):
            props = self.items[item_id - 1].props()
        else:
            return None
        return {k: v for k, v in props.items() if not names or k in names}

    def _layout(self, names):
        children = [GLib.Variant("(ia{sv}av)", (i + 1, self._props(i + 1, names), [])) for i in range(len(self.items))]
        return 0, self._props(0, names), children

    def _menu_property(self, _bus, _sender, _path, _interface, name):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(name)

    def _menu_call(self, _bus, _sender, _path, _interface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent, _depth, names = args
            layout = self._layout(names) if parent == 0 else (parent, self._props(parent, names) or {}, [])
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, layout)))
        elif method == "GetGroupProperties":
            ids, names = args
            ids = ids or range(len(self.items) + 1)
            found = [(i, self._props(i, names)) for i in ids]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, p) for i, p in found if p is not None],)))
        elif method == "GetProperty":
            props = self._props(args[0]) or {}
            if args[1] in props:
                invocation.return_value(GLib.Variant("(v)", (props[args[1]],)))
            else:
                invocation.return_dbus_error("com.canonical.dbusmenu.Error", f"No property {args[1]}")
        elif method == "Event":
            item_id, event = args[0], args[1]
            invocation.return_value(None)
            self._clicked(item_id, event)
        elif method == "EventGroup":
            for item_id, event, _data, _time in args[0]:
                self._clicked(item_id, event)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)

    def _clicked(self, item_id, event):
        if event == "clicked" and 0 < item_id <= len(self.items):
            item = self.items[item_id - 1]
            if item.on_click and item.enabled:
                GLib.idle_add(_once(item.on_click))  # after the reply: the click may quit the app


def _export(bus, path, interface, on_call, on_property):
    info = INTERFACES.lookup_interface(interface)
    if hasattr(bus, "register_object_with_closures2"):  # GLib 2.84+
        bus.register_object_with_closures2(path, info, on_call, on_property, None)
    else:
        bus.register_object(path, info, on_call, on_property, None)


def _once(callback):
    return lambda: callback() and False


def _pixmap_variant(pixmaps):
    """a(iiay) built straight from the bytes. GLib.Variant("a(iiay)", pixmaps) makes a Python
    object per byte: about 0.1 s for these icons, which blocked the app for seconds at startup
    while every tray read every property."""
    return GLib.Variant.new_array(GLib.VariantType("(iiay)"), [
        GLib.Variant.new_tuple(GLib.Variant("i", w), GLib.Variant("i", h),
                               GLib.Variant.new_from_bytes(GLib.VariantType("ay"), GLib.Bytes.new(data), True))
        for w, h, data in pixmaps])


def _pixmaps(icon_file):
    """The icon at a few sizes, as ARGB32 in network byte order, for trays that can't find it in
    the icon theme (before `prompter --install-desktop`)."""
    pixmaps = []
    for size in (22, 32, 48, 64):
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(str(icon_file), size, size)
        except GLib.Error:
            return []
        if not pixbuf.get_has_alpha():
            pixbuf = pixbuf.add_alpha(False, 0, 0, 0)
        width, height, stride = pixbuf.get_width(), pixbuf.get_height(), pixbuf.get_rowstride()
        rgba = pixbuf.read_pixel_bytes().get_data()
        rows = b"".join(rgba[y * stride : y * stride + width * 4] for y in range(height))
        argb = bytearray(len(rows))
        argb[0::4], argb[1::4], argb[2::4], argb[3::4] = rows[3::4], rows[0::4], rows[1::4], rows[2::4]
        pixmaps.append((width, height, bytes(argb)))
    return pixmaps
