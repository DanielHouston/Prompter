"""The app's actions and their keys. Plain data, so prompterctl can use it without loading GTK."""

# Everything the app can be told to do: from the window, prompterctl, or global shortcuts
ACTIONS = ("toggle", "mode", "faster", "slower", "bigger", "smaller", "next-section", "prev-section", "back",
           "restart", "preview", "timer", "progress", "calibrate", "control", "open-folder", "shortcuts", "reload",
           "quit")

# key in the control window (GTK name), action, what it does, suggested global shortcut
# (XDG shortcut format; the desktop has the final say)
KEYS = (
    ("space", "toggle", "Start or pause", "LOGO+ALT+space"),
    ("v", "mode", "Switch voice follow / fixed speed", "LOGO+ALT+v"),
    ("Page_Down", "next-section", "Next section", "LOGO+ALT+Page_Down"),
    ("Page_Up", "prev-section", "Previous section", "LOGO+ALT+Page_Up"),
    ("BackSpace", "back", "Back a paragraph", "LOGO+ALT+BackSpace"),
    ("Home", "restart", "Back to the top", "LOGO+ALT+Home"),
    ("bracketright", "faster", "Faster (fixed speed)", "LOGO+ALT+bracketright"),
    ("bracketleft", "slower", "Slower (fixed speed)", "LOGO+ALT+bracketleft"),
    ("equal", "bigger", "Bigger text", "LOGO+ALT+equal"),
    ("minus", "smaller", "Smaller text", "LOGO+ALT+minus"),
    ("c", "preview", "Camera self-view", "LOGO+ALT+c"),
    ("t", "timer", "Timer", "LOGO+ALT+t"),
    ("percent", "progress", "Progress (%)", "LOGO+ALT+percent"),
)
GLOBAL_ONLY = (("control", "Show the control window", "LOGO+ALT+p"),)
