"""Setup → Voice follow → Speech model: choose, download and delete speech models.

Downloading is the only time Prompter goes online, so it's always a button you press here,
with the size shown first.
"""

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import models, tilde  # noqa: E402


class ModelsDialog(Adw.Dialog):
    def __init__(self, app, language_name):
        super().__init__(title="Speech Models", content_width=540, content_height=700)
        self.app = app
        self.language_name = language_name
        self.fraction = 0.0
        self.bar = None  # the progress bar of the download in progress...
        self.bar_for = None  # ...and which model it's for
        self.page = Adw.PreferencesPage()
        self.toasts = Adw.ToastOverlay(child=self.page)
        view = Adw.ToolbarView(content=self.toasts)
        view.add_top_bar(Adw.HeaderBar())
        self.set_child(view)
        self.groups = []
        self.refresh()

    # --- updates from the app -------------------------------------------------

    def refresh(self):
        for group in self.groups:
            self.page.remove(group)
        have = models.installed()
        self.groups = [self._here(have), self._available(have)]
        for group in self.groups:
            self.page.add(group)

    def set_download(self, name, fraction):
        """name None when no download is running (it just finished, or stopped)."""
        self.fraction = fraction
        if name is None or name != self.bar_for:
            self.refresh()  # a download started or finished: swap its row's button, list it
        elif self.bar:
            self.bar.set_fraction(fraction)
            self.bar.set_text(f"{fraction:.0%}")

    def message(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=8))

    # --- the two lists ----------------------------------------------------------

    def _here(self, have):
        """What's on this computer: one of these is the model in use."""
        language, choice = self.app.config["language"], self.app.config["model"]
        group = Adw.PreferencesGroup(title="On this computer")
        radios = [Gtk.CheckButton()]  # never shown: grouping even a lone choice makes it a radio button

        auto_name, auto_path = models.resolve("auto", language, self.app.speech_device(), have)
        if auto_path:
            what = f"Uses {models.title(auto_name)}, the best you have for {self.language_name}"
        else:
            what = f"Nothing for {self.language_name} yet: download one below"
        group.add(self._choice_row(radios, "Automatic", what, "auto", choice == "auto"))

        for name in have:
            model = models.BY_NAME[name]
            fits = models.suits(model, language)
            about = _describe(model) if fits else f"{models.size(model.mb)} · English only: not for {self.language_name}"
            row = self._choice_row(radios, models.title(name), about, name, choice == name, fits, link=model)
            delete = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Delete", valign=Gtk.Align.CENTER,
                                css_classes=["flat"])
            delete.connect("clicked", lambda _b, m=model: self._confirm_delete(m))
            row.add_suffix(delete)
            group.add(row)

        if choice not in models.BY_NAME and choice != "auto":  # a model folder
            row = self._choice_row(radios, models.title(choice), tilde(choice), choice, True)
            change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER)
            change.connect("clicked", self._choose_folder)
            row.add_suffix(change)
            group.add(row)
        else:
            folder = Adw.ButtonRow(title="Use a Model Folder…")
            folder.connect("activated", self._choose_folder)
            group.add(folder)
        return group

    def _available(self, have):
        language, device = self.app.config["language"], self.app.speech_device()
        group = Adw.PreferencesGroup(title="Available to download", description=(
            "Downloaded from Hugging Face only when you click one, and kept in "
            f"{tilde(models.models_dir())}"))
        recommended = models.recommended(language, device)
        downloading = self.app.download.name if self.app.download else None
        self.bar = self.bar_for = None
        shown = 0
        for model in sorted(models.CATALOGUE, key=lambda m: m.name != recommended):  # the recommended one first
            if model.name in have or not model.offered or not models.suits(model, language):
                continue
            shown += 1
            subtitle = _subtitle(_describe(model), model)
            if model.name == recommended:
                subtitle = "<b>Recommended</b> · " + subtitle
            row = Adw.ActionRow(title=_escape(models.title(model.name)), subtitle=subtitle, subtitle_lines=0)
            if model.name == downloading:
                self.bar = Gtk.ProgressBar(fraction=self.fraction, text=f"{self.fraction:.0%}", show_text=True,
                                           valign=Gtk.Align.CENTER, width_request=120)
                self.bar_for = model.name
                row.add_suffix(self.bar)
                cancel = Gtk.Button(icon_name="process-stop-symbolic", tooltip_text="Cancel the download",
                                    valign=Gtk.Align.CENTER, css_classes=["flat"])
                cancel.connect("clicked", lambda *_: self.app.cancel_download())
                row.add_suffix(cancel)
            else:
                get = Gtk.Button(label="Download", valign=Gtk.Align.CENTER, sensitive=downloading is None,
                                 tooltip_text=f"{models.size(model.mb)} from huggingface.co/{model.repo}")
                get.connect("clicked", lambda _b, name=model.name: self.app.download_model(name))
                row.add_suffix(get)
            group.add(row)
        if not shown:
            group.set_description("You have every model there is for " + self.language_name)
        return group

    def _choice_row(self, radios, title, subtitle, value, active, sensitive=True, link=None):
        check = Gtk.CheckButton(active=active, valign=Gtk.Align.CENTER, sensitive=sensitive, group=radios[0])
        radios.append(check)
        check.connect("toggled", lambda b: b.get_active() and self.app.set_speech(model=value))
        row = Adw.ActionRow(title=_escape(title), subtitle=_subtitle(subtitle, link), subtitle_lines=0,
                            activatable=sensitive)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        return row

    # --- actions ----------------------------------------------------------------

    def _confirm_delete(self, model):
        dialog = Adw.AlertDialog(heading=f"Delete {models.title(model.name)}?",
                                 body=f"That frees {models.size(model.mb)}. You can download it again later.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", lambda _d, response: response == "delete" and self.app.delete_model(model.name))
        dialog.present(self)

    def _choose_folder(self, *_):
        dialog = Gtk.FileDialog(title="Choose a Speech Model Folder", modal=True)
        dialog.select_folder(self.get_root(), None, self._folder_chosen)

    def _folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # cancelled
        path = folder.get_path() if folder else None
        if not path:
            return
        if not models.is_model_dir(path):
            self.message("That folder has no faster-whisper model in it: it needs "
                         + ", ".join(models.MODEL_FILES[:-1]) + " and " + models.MODEL_FILES[-1])
            return
        self.app.set_speech(model=path)
        self.refresh()


def _escape(text):
    return GLib.markup_escape_text(text)


def _subtitle(text, model=None):
    """Markup: the text, then a link to the model's page on Hugging Face, which is where its
    files come from."""
    if model is None:
        return _escape(text)
    url = f"https://huggingface.co/{model.repo}"
    return f'{_escape(text)}\n<a href="{_escape(url)}">huggingface.co/{_escape(model.repo)}</a>'


def _describe(model):
    """Size, languages (English-only ones say so in their title) and speed."""
    parts = [models.size(model.mb)] + ([] if model.english_only else ["99 languages"])
    parts.append("needs a graphics card" if model.needs_gpu else "fine on a processor")
    return " · ".join(parts)
