import sys

from . import __version__


def main():
    args = sys.argv[1:]
    if "--version" in args:
        print(f"prompter {__version__}")
        return 0
    if "--install-desktop" in args:
        from .desktop import install

        install()
        return 0

    from . import APP_ID
    from .shortcuts import register_app

    register_app(APP_ID)  # must precede GTK's own portal calls (settings, file chooser...)

    from .app import PrompterApp

    return PrompterApp().run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
