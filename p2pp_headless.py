#!/usr/bin/env python3
"""
p2pp_headless.py - Headless CLI entry point for P2PP.

Processes a PrusaSlicer multi-material gcode file and generates Palette 3
output (.mcfx) without any GUI dependency (no PyQt5 required).

Usage:
    python3 p2pp_headless.py <input.gcode> [output.mcfx]

Exit codes:
    0  - success, no warnings
    2  - success, but processing warnings were generated
    1  - fatal error

Designed to be called as a subprocess by the Moonraker p2pp component,
but can also be used standalone.
"""

import sys
import os
import types

# Ensure the repo root is on sys.path so bare `import version` works
# regardless of the working directory.
_repo_root = os.path.dirname(os.path.abspath(__file__))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)


# ---------------------------------------------------------------------------
# GUI stub — must be injected into sys.modules BEFORE any p2pp.* import,
# because every core module does `import p2pp.gui as gui` at module level.
# The real gui.py initialises a QApplication at import time; stubbing it
# out here means PyQt5 is never touched.
# ---------------------------------------------------------------------------

class _App:
    """Stub for gui.app — all Qt event-loop calls become no-ops."""
    def sync(self):   pass
    def exec_(self):  pass
    def exec(self):   pass
    def quit(self):   pass


def _create_logitem(text, color="#000000", force_update=True, position=0):
    print("[P2PP] {}".format(text), flush=True)


def _log_warning(text):
    # Append to v.process_warnings (module may not be imported yet, so
    # we do a lazy import each time — after the first call the import is
    # cached by Python and there is no overhead).
    import p2pp.variables as _v
    _v.process_warnings.append(";" + text)
    print("[P2PP WARN] {}".format(text), flush=True)


def _logexception(e):
    import traceback
    print("[P2PP ERROR] {}".format(e), flush=True)
    traceback.print_exc()


def _print_summary(summary):
    for line in summary:
        print("[P2PP] {}".format(line), flush=True)


_gui_module = types.ModuleType("p2pp.gui")
_gui_module.app                  = _App()
_gui_module.create_logitem       = _create_logitem
_gui_module.log_warning          = _log_warning
_gui_module.create_emptyline     = lambda: None
_gui_module.progress_string      = lambda pct: None
_gui_module.setfilename          = lambda text: None
_gui_module.close_button_enable  = lambda: None
_gui_module.logexception         = _logexception
_gui_module.print_summary        = _print_summary
_gui_module.create_colordefinition = lambda *args, **kwargs: None

sys.modules["p2pp.gui"] = _gui_module


# ---------------------------------------------------------------------------
# p3_upload stub — p3_upload.py also initialises Qt windows at module level
# (it imports PyQt5 and calls uic.loadUiType at the top level).  We replace
# the entire module so the real file is never executed.
# ---------------------------------------------------------------------------

def _uploadfile_headless(localfile, p3file):
    print(
        "[P2PP] Palette 3 HTTP upload skipped in headless mode: {}".format(p3file),
        flush=True,
    )


_upload_module = types.ModuleType("p2pp.p3_upload")
_upload_module.uploadfile = _uploadfile_headless
sys.modules["p2pp.p3_upload"] = _upload_module


# ---------------------------------------------------------------------------
# Safe to import p2pp modules now — gui and p3_upload are already resolved.
# ---------------------------------------------------------------------------

import p2pp.mcf as mcf        # noqa: E402  (import after sys.modules patch)
import p2pp.variables as v    # noqa: E402
import version as ver         # noqa: E402  (repo-root module)

v.version = ver.Version


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(
            "Usage: p2pp_headless.py <input.gcode> [output.mcfx]",
            file=sys.stderr,
        )
        sys.exit(1)

    input_file  = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.isfile(input_file):
        print("[P2PP ERROR] Input file not found: {}".format(input_file), file=sys.stderr)
        sys.exit(1)

    try:
        mcf.p2pp_process_file(input_file, output_file)
    except Exception as e:
        _logexception(e)
        sys.exit(1)

    sys.exit(2 if v.process_warnings else 0)


if __name__ == "__main__":
    main()
