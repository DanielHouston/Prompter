# Prompter

A voice-following teleprompter for the **Elgato Prompter** on Linux.

Elgato's Camera Hub, which drives the Prompter's teleprompter features, only runs on
Windows and macOS. On Linux the Prompter is just a small second screen. Prompter fills
the gap: it shows your script on the Prompter and scrolls it **as you speak**, using local
speech recognition, or at a steady speed if you prefer.

- **Voice follow.** Listens to your mic, transcribes the last few seconds with Whisper
  several times a second, and keeps the line you're on at the reading line. Ad-libs don't
  move it; re-reading a line moves it back.
- **Scripts are Markdown files.** Headings become sections you can jump between,
  `[cues]` are shown but never expected to be spoken, `<!-- comments -->` are hidden. Edits
  show up as soon as you save.
- **Presenting from slides.** Open a PowerPoint (.pptx) or Impress (.odp) deck and its
  speaker notes are the script, a section for each slide: voice follow reads along with
  you, and your clicker moves the notes on with the slides.
- **Eye-line crosshair** over the camera lens, with a "Find the lens" mode that blacks out
  the screen so you can see the lens through the glass. To keep your eyes close to the lens as
  you read, the script can run in short lines centred on it.
- **Camera self-view** in a corner of the Prompter.
- **A timer and progress** on the Prompter: reading time, time left or the time of day, and
  how far through the script you are, in whichever corner or edge suits you.
- **The Prompter's brightness and contrast**, set from the app (DDC/CI, as Camera Hub does).
- **Global shortcuts** through your desktop's shortcuts portal, plus `prompterctl` for
  anything else.

<p align="center">
  <img src="docs/screenshots/prompter.png" width="58%" alt="The script on the Prompter, with read text dimmed and the reading line marked">
  <img src="docs/screenshots/prompt.png" width="34%" alt="The control window's Prompt tab">
</p>

Not affiliated with or endorsed by Elgato or Corsair.

## Requirements

- An **Elgato Prompter**, with the **DisplayLink driver** for Linux (`evdi` plus
  DisplayLink Manager) installed and running.
- A **Wayland** desktop: GNOME, KDE Plasma, Hyprland, Sway and others.
- An **NVIDIA GPU is recommended** for voice follow. Without one it runs on the CPU with
  a smaller model, which tracks less closely.
- **PipeWire** (`pw-record`, `pactl`) and **ffmpeg** (for the camera self-view).
- **Python 3.11+**, **GTK 4.12+**, **libadwaita 1.8+** and **PyGObject**, from your
  distribution:
  - Arch: `sudo pacman -S python-gobject gtk4 libadwaita ffmpeg python-pipx`
  - Fedora 43+: `sudo dnf install python3-gobject gtk4 libadwaita ffmpeg pipx`
  - Debian 14 / Ubuntu 25.10+: `sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 ffmpeg pipx`

## Install

```sh
pipx install --system-site-packages "prompter[cuda] @ git+https://github.com/DanielHouston/prompter"
prompter --install-desktop
```

Leave out `[cuda]` if you don't have an NVIDIA GPU. `--system-site-packages` lets it use
your distribution's GTK bindings. `--install-desktop` adds Prompter to your app launcher,
and your desktop needs that entry before it will grant global shortcuts.

Prompter doesn't download anything by itself. Voice follow needs a speech model, which you
pick under Setup → Voice follow → **Speech model**: each one's size is listed (from 78 MB to
3.1 GB), one is marked as recommended for your computer, and nothing downloads until you
click Download. Models are kept in `~/.local/share/prompter/models`; any already in the
Hugging Face cache are used where they are. Fixed speed needs no model at all.

### Speech models

- **Automatic** uses the best model you've downloaded for your language and computer.
- **Run on** chooses the graphics card or the processor. The processor keeps an NVIDIA card
  free (for a game, say), with a smaller model so it keeps up.
- **Your own model:** any faster-whisper (CTranslate2) Whisper model works, such as one
  fine-tuned for your language. Download it yourself, for example with
  `hf download <repo> --local-dir ~/Models/my-whisper`, then choose Speech model → **Use a
  Model Folder…**.

## Using it

Start **Prompter** from your launcher. The script appears fullscreen on the Prompter by
itself. The control window opens on your current screen:

- **Prompt** is for while you read. It has start/pause, section jumps, the mode (voice
  follow or fixed speed), how far through you are and roughly how long is left, and
  the script itself: click anywhere to jump there. A quick bar holds text size, self-view, crosshair,
  timer, progress and brightness.
- **Setup** holds everything you set once: microphone, language, speech model, the
  clicker to follow, text style, crosshair, camera, timer, progress, brightness and
  diagnostics.

Closing the control window doesn't stop Prompter: it stays in your tray (KDE Plasma, waybar
and most other bars; GNOME needs the AppIndicator extension). Click the tray icon to bring
the controls back, middle-click it to start or pause, and quit from its menu.

Scripts live in `~/Documents/Prompter` by default (Setup → Script → Scripts folder →
**Change…** to use another folder). A welcome script is there the first time. The folder
button in the header bar lists your scripts, and Setup can show the folder in your file
manager.

```markdown
# Intro

Hi everyone, welcome back. [smile]

<!-- remember to check the mic -->

## Today's topic

Each heading is a section. Text in square brackets is a cue for you, not something to say.
```

Voice follow works for languages that separate words with spaces. Pick yours under Setup →
Voice follow → Language.

### Keeping your eyes on the lens

Even with the line you're on held at the reading line, your eyes move along it, and the
camera sees them move. Setup → Text → **Centre on the lens** sets the script in short lines
centred on the lens instead, so your eyes stay close to it, and breaks them where you'd pause
anyway: at the end of a sentence or a clause, and never just after a little word like "a" or
"the". It scrolls as usual. The lines are centred in line with the crosshair, so use **Find
the lens** and **Put the Reading Line at the Crosshair** first. **Line width** sets how short
they are: narrower moves your eyes less, wider fits more on each line.

### Presenting from slides

Put the presentation in your scripts folder and open it: PowerPoint (.pptx) or LibreOffice
Impress (.odp). For Google Slides, use File → Download → Microsoft PowerPoint. Its speaker
notes are the script, each slide a section headed by its title, and everything else works
as it does for any script: voice follow reads along as you talk, fixed speed scrolls, the
section list jumps to a slide. Hidden slides are skipped, as in the slideshow.

Present from Impress, PowerPoint, Google Slides or anything else, and press **Start** in
Prompter as you begin. To have the notes move on with your slides, switch your clicker on
under Setup → Slides; while you're started, Prompter follows it:

- Forward (Page Down, →, ↓, Space, Enter) and back (Page Up, ←, ↑, Backspace) take the
  notes to the slide on screen; Home and End go to the first and last slide. If voice
  follow already has you reading that slide's notes, they stay where you are.
- A click that plays one of the slide's animations doesn't move the notes on: Prompter
  reads how many clicks each slide's animations take from the file, as the slideshow does.
- If you talk on into the next slide's notes before clicking, the Prompt tab says which
  slide is still on screen.
- The clicks still reach your slides: Prompter only reads them, and only while you're
  started.

A clicker works with Markdown scripts too: there it moves from section to section.

Wayland doesn't let one app see another's keys, so Prompter reads the clicker directly.
That needs a one-time permission for that clicker only: **Allow…** next to it shows the
commands to run. Prompter only ever offers clickers, never keyboards: being able to read a
keyboard would mean being able to read everything typed on it.

If the notes and the slides ever disagree (you moved the slides with the keyboard or mouse,
say), the section buttons and list in the Prompt tab put them right.

### Shortcuts

Prompter registers its actions with your desktop's **global shortcuts portal**, so they
work while you're in another app:

- **KDE Plasma and GNOME** ask you to confirm or assign keys the first time.
- **Hyprland** lists them in `hyprctl globalshortcuts`; bind the keys you want in your
  config:

  ```lua
  -- hyprland.lua
  hl.bind("SUPER + ALT + space", hl.dsp.global("io.github.DanielHouston.Prompter:toggle"))
  ```
  ```ini
  # hyprland.conf
  bind = SUPER ALT, space, global, io.github.DanielHouston.Prompter:toggle
  ```

- **Anything else**: bind keys to `prompterctl <action>`. It starts Prompter if needed.

The actions are `toggle`, `mode`, `next-section`, `prev-section`, `back`, `restart`,
`faster`, `slower`, `bigger`, `smaller`, `preview`, `timer`, `progress` and `control`. The
menu's **Keyboard Shortcuts** shows what's bound. The same keys without modifiers (Space, V,
Page Up/Down, Backspace, Home, [ ], − =, C, T, %) work in the control window's Prompt tab.

### Prompter brightness

The DisplayLink driver exposes the Prompter's DDC/CI channel as an I2C bus. To let the app
use it, without root, run this once:

```sh
echo 'SUBSYSTEM=="i2c-dev", ATTR{name}=="DisplayLink I2C Adapter", TAG+="uaccess"' \
  | sudo tee /etc/udev/rules.d/70-elgato-prompter-ddc.rules
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf
sudo udevadm control --reload && sudo modprobe i2c-dev
```

Only the Prompter's bus is opened up, not your GPU's.

## Troubleshooting

- **"Elgato Prompter not found":** check the DisplayLink service is running
  (`systemctl status displaylink`), then reconnect the Prompter.
- **The Prompter stays black on Hyprland with an NVIDIA GPU.** This is a Hyprland
  limitation: it can't yet hand NVIDIA-rendered frames to DisplayLink displays. The fix is
  in progress in [hyprwm/aquamarine#279](https://github.com/hyprwm/aquamarine/pull/279).
  KDE Plasma and GNOME work today.
- **Voice follow lags:** Setup → Voice follow shows which model is running, and on the
  graphics card or the processor. On the processor, pick a model marked "fine on a
  processor".
- The speech engine logs to `~/.cache/prompter/asr.log`.
- **Bug reports about tracking:** turn on Setup → Diagnostics → *Save sessions for
  tuning*, read the part that goes wrong, and attach the session folder from
  `~/.cache/prompter/sessions`.

## Privacy

Everything runs on your machine. Your voice is never sent anywhere. Prompter only goes
online when you click Download for a speech model (from Hugging Face), and the speech engine
runs with Hugging Face's offline mode on, so it can't fetch anything by itself. Session
recordings are off unless you turn them on, stay local, and can be deleted from Setup.

## Development

```sh
python -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[cuda]"
.venv/bin/prompter
python -m unittest discover -s tests
```

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).

Elgato is a trademark of Corsair Memory, Inc. This project is not affiliated with,
endorsed by or sponsored by Elgato or Corsair.
