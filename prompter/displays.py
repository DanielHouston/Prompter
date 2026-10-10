"""Finding the Elgato Prompter among the connected displays."""

# How the Prompter's EDID names itself: manufacturer "IDI", model "Elgato Prom."
PROMPTER_MODEL = "Elgato Prom"


def monitors(display):
    model = display.get_monitors()
    return [model.get_item(i) for i in range(model.get_n_items())]


def is_prompter(monitor):
    return (monitor.get_model() or "").startswith(PROMPTER_MODEL)


def find(display, connector=None):
    """Where the script goes: a specific connector if one is configured, otherwise the Prompter."""
    for monitor in monitors(display):
        if (monitor.get_connector() == connector) if connector else is_prompter(monitor):
            return monitor
    return None


def label(monitor):
    name = " ".join(p for p in (monitor.get_manufacturer(), monitor.get_model()) if p) or "Display"
    return f"{name} ({monitor.get_connector()})"
