#!/usr/bin/env python3
"""Install / repair / inspect `colab-ext` for google-colab-cli.

Runs with ANY python3 (stdlib only) -- that is the point: after `colab update --install`
rebuild the tool environment, the extension source in ~/.colab-ext is still there and
this script re-wires it in one command.

    python3 install.py                  # install (default: copy source + drop colab_ext.pth)
    python3 install.py --status         # what is wired where
    python3 install.py --uninstall      # remove the .pth (the copy in ~/.colab-ext stays)
    python3 install.py --link           # dev mode: symlink the repo instead of copying
    python3 install.py --site-dir DIR   # target a specific site-packages
    COLAB_EXT_DEBUG=1 ...               # verbose loader diagnostics
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from colab_ext import inject  # noqa: E402  (stdlib-only module)


def _cleanup_legacy_dirs(src: str | None, *, quiet: bool = False) -> None:
    src_real = os.path.realpath(src) if src else None
    script_dir_real = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    for legacy_dir in inject.LEGACY_HOME_DIRS:
        if not (os.path.islink(legacy_dir) or os.path.exists(legacy_dir)):
            continue
        legacy_real = os.path.realpath(legacy_dir)
        if src_real and legacy_real == src_real and not os.path.islink(legacy_dir):
            continue
        if legacy_real == script_dir_real and not os.path.islink(legacy_dir):
            continue
        try:
            if os.path.islink(legacy_dir) or os.path.isfile(legacy_dir):
                os.unlink(legacy_dir)
            else:
                shutil.rmtree(legacy_dir)
            if not quiet:
                print("[colab-ext] cleaned up legacy dir %s" % legacy_dir)
        except OSError as exc:
            if not quiet:
                print("[colab-ext] WARN: could not clean up legacy dir %s: %s" % (legacy_dir, exc))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--status", action="store_true", help="show the wiring and exit")
    ap.add_argument("--uninstall", action="store_true", help="remove the .pth hook")
    ap.add_argument(
        "--link",
        action="store_true",
        help="dev: symlink the extension source instead of copying it",
    )
    ap.add_argument("--site-dir", help="site-packages of the colab-cli environment")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.status:
        for key, val in inject.status().items():
            print("%-16s: %s" % (key, val))
        return 0

    if args.uninstall:
        out = inject.remove(args.site_dir)
        if out["removed"]:
            removed = [out["pth"], *out.get("removed_legacy", [])]
            print("[colab-ext] removed %s" % ", ".join(p for p in removed if p))
        else:
            print("[colab-ext] nothing to remove (%s)" % out["pth"])
        return 0

    src = inject.source_dir()
    if args.link:
        if not src:
            sys.exit("cannot locate the extension source (run from the repo checkout)")
        os.makedirs(inject.install_root(), exist_ok=True)
        dst = os.path.join(inject.install_root(), inject.PACKAGE)
        if os.path.islink(dst):
            os.unlink(dst)
        elif os.path.isdir(dst):
            shutil.rmtree(dst)
        elif os.path.exists(dst):
            os.unlink(dst)
        os.symlink(os.path.join(src, inject.PACKAGE), dst)
        loader_dst = os.path.join(inject.install_root(), inject.LOADER)
        loader_src = os.path.join(src, inject.LOADER)
        if os.path.islink(loader_dst) or os.path.exists(loader_dst):
            os.unlink(loader_dst)
        os.symlink(loader_src, loader_dst)
        print("[colab-ext] linked %s -> %s" % (inject.install_root(), src))

    rep = inject.ensure(site_dir=args.site_dir, copy_source=not args.link, quiet=args.quiet)
    if rep.get("error"):
        print("ERROR: %s" % rep["error"], file=sys.stderr)
        return 1

    _cleanup_legacy_dirs(src, quiet=args.quiet)

    if not args.quiet:
        st = inject.status(site_dir=args.site_dir)
        print(
            "[colab-ext] .pth : %s (%s)"
            % (rep.get("pth"), "written" if rep.get("changed") else "already present")
        )
        print("[colab-ext] source: %s" % rep.get("source"))
        if not st.get("colab_cli_here"):
            print("[colab-ext] WARN: %s does not contain colab_cli" % st.get("site_packages"))
        print("[colab-ext] done. `colab ext doctor` and `colab drive --help` work in the NEXT invocation.")
        print("[colab-ext] after a colab-cli upgrade, re-run this script if extension commands disappear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
