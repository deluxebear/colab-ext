"""Loaded by colab_ext.pth at interpreter startup inside the colab-cli environment.

Imports colab_cli.cli eagerly (in this venv that is nearly free: the venv exists to run
colab, so the module would be imported moments later anyway), then registers the
multi-account hooks, `colab account`, `colab drive` (`dfs` alias), `colab ext`, and
enhanced `colab drivemount` + `colab skill` on the typer app that colab_cli.cli built.

Best-effort by design: any failure is silent unless COLAB_EXT_DEBUG=1 (or legacy
COLAB_DFS_DEBUG=1), so a missing or half-updated extension can never stop `colab` itself
from working.
"""

import os
import sys

DEBUG = os.environ.get("COLAB_EXT_DEBUG") or os.environ.get("COLAB_DFS_DEBUG")


def _register():
    import colab_cli.cli as cli

    if getattr(cli.app, "_colab_ext_registered", False) or getattr(cli, "_colab_ext_registered", False):
        return
    from colab_ext import register

    if register(cli.app) and DEBUG:
        import colab_ext

        sys.stderr.write("[colab-ext] registered colab-ext v%s\n" % colab_ext.__version__)


try:
    _register()
except ImportError as exc:  # colab_cli not installed here / extension not fully installed
    if DEBUG:
        sys.stderr.write("[colab-ext] not loaded: %r\n" % (exc,))
except Exception as exc:
    sys.stderr.write(
        "[colab-ext] extension failed to load: %r\n"
        "  fix: python3 ~/.colab-ext/install.py\n" % (exc,)
    )
