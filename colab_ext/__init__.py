"""`colab-ext` -- multi-account, zero-browser Google Drive, and agent-skill extension for google-colab-cli.

The whole integration is one function, `register(app)`, called by `colab_ext_loader`
after `colab_cli.cli` has built its typer app. No file belonging to google-colab-cli is
edited, so `colab update --install` / `uv tool install -U google-colab-cli` cannot produce
merge conflicts; the worst an upgrade can do is drop the single `colab_ext.pth` we dropped
into its site-packages, and `python3 ~/.colab-ext/install.py` puts it back.
"""

from __future__ import annotations

import os
import sys

__version__ = "0.3.0"
REGISTERED_ATTR = "_colab_ext_registered"

ACCOUNT_HELP = "Manage multiple Google accounts, isolated sessions, and shared Drive bindings."

DRIVE_HELP = "Browser-free Google Drive mounting & DriveFS credential management (alias: `dfs`)."

EXT_HELP = "Diagnose, repair, or inspect the colab-ext CLI extension."


def register(app, wrap_update: bool = True, force: bool = False) -> bool:
    """Attach multi-account hooks, `account`, `drive` (`dfs`), and `ext` to a Typer app. Never raises."""
    if getattr(app, REGISTERED_ATTR, False) and not force:
        return False
    try:
        from . import cli_hooks
        from .account_commands import account_app
        from .commands import drive_app, ext_app

        cli_hooks.install_hooks(app, force=force)
        if force:
            app.registered_groups = [
                g
                for g in getattr(app, "registered_groups", [])
                if getattr(g, "name", None) not in ("account", "acct", "drive", "dfs", "ext")
            ]
        app.add_typer(account_app, name="account", help=ACCOUNT_HELP)
        app.add_typer(account_app, name="acct", help=ACCOUNT_HELP, hidden=True)
        app.add_typer(drive_app, name="drive", help=DRIVE_HELP)
        app.add_typer(drive_app, name="dfs", help=DRIVE_HELP, hidden=True)
        app.add_typer(ext_app, name="ext", help=EXT_HELP)
        if wrap_update:
            _wrap_update(app, force=force)
        setattr(app, REGISTERED_ATTR, True)
        setattr(app, "_colab_dfs_registered", True)
        cli_mod = sys.modules.get("colab_cli.cli")
        if cli_mod is not None:
            setattr(cli_mod, REGISTERED_ATTR, True)
            setattr(cli_mod, "_colab_dfs_registered", True)
        return True
    except Exception as exc:  # a broken extension must never take the CLI down
        if os.environ.get("COLAB_EXT_DEBUG") or os.environ.get("COLAB_DFS_DEBUG"):
            import traceback

            traceback.print_exc()
        else:
            print(
                "[colab-ext] extension disabled this run: %r (COLAB_EXT_DEBUG=1 for the traceback)"
                % exc,
                file=sys.stderr,
            )
        return False


def _wrap_update(app, force: bool = False) -> None:
    """After `colab update --install` rebuilds the tool env, re-drop our .pth."""
    import functools

    for reg in list(getattr(app, "registered_commands", []) or []):
        if getattr(reg, "name", None) != "update":
            continue
        raw_cb = getattr(reg, "callback", None)
        if raw_cb is None:
            return
        if (
            getattr(raw_cb, "_colab_ext_wrapped", False)
            or getattr(raw_cb, "_colab_dfs_wrapped", False)
        ) and not force:
            return
        original = getattr(raw_cb, "_colab_ext_orig", getattr(raw_cb, "_colab_dfs_orig", raw_cb))

        def wrapper(*args, **kwargs):
            rc = original(*args, **kwargs)
            if kwargs.get("install") or (args and args[0]):
                try:
                    from . import inject

                    rep = inject.ensure(copy_source=False, quiet=True)
                    if rep.get("pth") and os.path.exists(rep["pth"]):
                        print("[colab-ext] extension still wired: %s" % rep["pth"])
                    else:
                        print(
                            "[colab-ext] could not re-wire the extension; run:"
                            " python3 ~/.colab-ext/install.py"
                        )
                except Exception as exc:
                    print(
                        "[colab-ext] re-wire failed: %r (run: python3 ~/.colab-ext/install.py)"
                        % exc
                    )
            return rc

        wrapped = functools.wraps(original)(wrapper) if hasattr(original, "__name__") else wrapper
        wrapped._colab_ext_orig = original
        wrapped._colab_ext_wrapped = True
        reg.callback = wrapped
        return
