"""`colab dfs` -- browser-free Google Drive mounting for google-colab-cli.

The whole integration is one function, `register(app)`, called by `colab_dfs_loader`
after `colab_cli.cli` has built its typer app. No file belonging to google-colab-cli is
edited, so `colab update --install` / `uv tool install -U google-colab-cli` cannot produce
merge conflicts; the worst an upgrade can do is drop the single `colab_dfs.pth` we dropped
into its site-packages, and `python3 ~/.colab-dfs-ext/install.py` puts it back.
"""

from __future__ import annotations

import os

__version__ = "0.2.0"
REGISTERED_ATTR = "_colab_dfs_registered"

ACCOUNT_HELP = "Manage multiple Google accounts, isolated sessions, and shared Drive bindings."

DFS_HELP = (
    "Browser-free Drive mounting (DriveFS + your own refresh token).\n\n"
    "Unlike `colab drivemount`, this never touches Colab's dfs_ephemeral consent dance:"
    " it starts /opt/google/drive/drive in the runtime and feeds it a local GCE-metadata"
    " shim. One `colab dfs consent` in a browser is enough for every later mount."
)


def register(app, wrap_update: bool = True, force: bool = False) -> bool:
    """Attach multi-account hooks, `account`, and `dfs` to a Typer app. Never raises."""
    if getattr(app, REGISTERED_ATTR, False) and not force:
        return False
    try:
        from . import cli_hooks
        from .account_commands import account_app
        from .commands import dfs

        cli_hooks.install_hooks(app, force=force)
        if force:
            app.registered_groups = [
                g for g in getattr(app, "registered_groups", [])
                if getattr(g, "name", None) not in ("account", "acct", "dfs")
            ]
        app.add_typer(account_app, name="account", help=ACCOUNT_HELP)
        app.add_typer(account_app, name="acct", help=ACCOUNT_HELP, hidden=True)
        app.add_typer(dfs, name="dfs", help=DFS_HELP)
        if wrap_update:
            _wrap_update(app)
        setattr(app, REGISTERED_ATTR, True)
        return True
    except Exception as exc:  # a broken extension must never take the CLI down
        if os.environ.get("COLAB_DFS_DEBUG"):
            import traceback

            traceback.print_exc()
        else:
            print("[colab-dfs] extension disabled this run: %r (COLAB_DFS_DEBUG=1 for the traceback)"
                  % exc, file=__import__("sys").stderr)
        return False


def _wrap_update(app) -> None:
    """After `colab update --install` rebuilds the tool env, re-drop our .pth."""
    for reg in list(getattr(app, "registered_commands", []) or []):
        if getattr(reg, "name", None) != "update":
            continue
        original = getattr(reg, "callback", None)
        if original is None or getattr(original, "_colab_dfs_wrapped", False):
            return

        def wrapper(*args, **kwargs):
            rc = original(*args, **kwargs)
            if kwargs.get("install") or (args and args[0]):
                try:
                    from . import inject

                    rep = inject.ensure(copy_source=False, quiet=True)
                    if rep.get("pth") and os.path.exists(rep["pth"]):
                        print("[colab-dfs] extension still wired: %s" % rep["pth"])
                    else:
                        print("[colab-dfs] could not re-wire the extension; run:"
                              " python3 ~/.colab-dfs-ext/install.py")
                except Exception as exc:
                    print("[colab-dfs] re-wire failed: %r (run: python3 ~/.colab-dfs-ext/install.py)"
                          % exc)
            return rc

        wrapper._colab_dfs_wrapped = True
        import functools

        reg.callback = functools.wraps(original)(wrapper) if hasattr(original, "__name__") else wrapper
        return
