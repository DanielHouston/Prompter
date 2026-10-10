"""System-wide shortcuts through the XDG GlobalShortcuts portal.

The desktop decides the actual keys: KDE and GNOME ask once when the app first registers,
Hyprland binds them in its config with the `global` dispatcher. Without the portal,
prompterctl does the same job from any compositor's keybindings.
"""

import random

from gi.repository import Gio, GLib

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
IFACE = "org.freedesktop.portal.GlobalShortcuts"
INSTALL_HINT = "Run “prompter --install-desktop” once so your desktop recognises the app."

_registration = None  # (registered, problem) for this process
_bus = None  # the registration belongs to this connection: keep it alive (GLib drops unreferenced ones)


def register_app(app_id):
    """Tell the portal which app this is. Unsandboxed apps must do this before any other portal
    call (xdg-desktop-portal 1.19+), and the portal only accepts IDs with an installed desktop
    entry. Older portals have no registry and identify apps themselves."""
    global _registration, _bus
    if _registration is None:
        try:
            _bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            _bus.call_sync(BUS, PATH, "org.freedesktop.host.portal.Registry", "Register",
                          GLib.Variant("(sa{sv})", (app_id, {})), None, Gio.DBusCallFlags.NONE, 2000, None)
            _registration = (True, None)
        except GLib.Error as e:
            if "App info not found" in e.message:
                _registration = (False, INSTALL_HINT)
            else:  # no registry on this portal: carry on
                _registration = (True, None)
    return _registration


class GlobalShortcuts:
    def __init__(self, app_id, shortcuts, on_activated, on_bound):
        """shortcuts: [(id, description, preferred trigger such as "LOGO+ALT+space")].
        on_activated(id) when one fires; on_bound({id: trigger description}) when they're (re)bound."""
        self.app_id, self.shortcuts = app_id, shortcuts
        self.on_activated, self.on_bound = on_activated, on_bound
        self.session = None
        self.available = False
        self.problem = None  # why there are no global shortcuts, for the shortcuts dialog
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return
        registered, self.problem = register_app(app_id)
        if not registered:
            return
        if not self._has_portal():
            self.problem = "Your desktop has no global shortcuts portal."
            return
        self.available = True
        self.bus.signal_subscribe(None, IFACE, "Activated", PATH, None, Gio.DBusSignalFlags.NONE, self._activated)
        self.bus.signal_subscribe(None, IFACE, "ShortcutsChanged", PATH, None, Gio.DBusSignalFlags.NONE, self._changed)
        self._request("CreateSession", lambda token: GLib.Variant("(a{sv})", ({
            "handle_token": GLib.Variant("s", token),
            "session_handle_token": GLib.Variant("s", "prompter"),
        },)), self._session_created)

    def _has_portal(self):
        try:
            self.bus.call_sync(BUS, PATH, "org.freedesktop.DBus.Properties", "Get", GLib.Variant("(ss)", (IFACE, "version")),
                               None, Gio.DBusCallFlags.NONE, 2000, None)
            return True
        except GLib.Error:
            return False

    def _request(self, method, make_args, on_response):
        """Portal methods answer through a Request object's Response signal; subscribe first."""
        token = f"prompter{random.randrange(1 << 30)}"
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        subscription = None

        def response(_bus, _sender, _path, _iface, _signal, params):
            self.bus.signal_unsubscribe(subscription)
            code, results = params.unpack()
            if code == 0:
                on_response(results)

        subscription = self.bus.signal_subscribe(None, "org.freedesktop.portal.Request", "Response", handle, None,
                                                 Gio.DBusSignalFlags.NONE, response)

        def sent(bus, result):
            try:
                bus.call_finish(result)
            except GLib.Error as e:
                self.bus.signal_unsubscribe(subscription)
                reason = e.message.split(": ", 1)[-1] if e.message.startswith("GDBus.Error:") else e.message
                self.available = False
                self.problem = INSTALL_HINT if "app id is required" in reason else f"The shortcuts portal refused: {reason}."

        self.bus.call(BUS, PATH, IFACE, method, make_args(token), None, Gio.DBusCallFlags.NONE, -1, None, sent)

    def _session_created(self, results):
        self.session = results.get("session_handle")
        shortcuts = [(sid, {"description": GLib.Variant("s", what), "preferred_trigger": GLib.Variant("s", trigger)})
                     for sid, what, trigger in self.shortcuts]
        self._request("BindShortcuts", lambda token: GLib.Variant("(oa(sa{sv})sa{sv})", (
            self.session, shortcuts, "", {"handle_token": GLib.Variant("s", token)})), self._bound)

    def _bound(self, results):
        self._report(results.get("shortcuts", []))

    def _changed(self, _bus, _sender, _path, _iface, _signal, params):
        session, shortcuts = params.unpack()
        if session == self.session:
            self._report(shortcuts)

    def _report(self, shortcuts):
        self.on_bound({sid: info.get("trigger_description", "") for sid, info in shortcuts})

    def _activated(self, _bus, _sender, _path, _iface, _signal, params):
        session, sid = params.unpack()[:2]
        if session == self.session:
            self.on_activated(sid)
