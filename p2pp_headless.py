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

After processing, the script:
  1. Extracts print.gcode from the generated .mcfx.
  2. Strips all Tx tool-change commands (T0, T1, …) — in Palette 3 accessory
     mode the splicer tracks extrusion mechanically and drives splices from
     palette.json; Tx commands serve no purpose at the printer and would
     cause errors without matching Klipper macros.
  3. Writes the cleaned gcode back over the original .gcode input so that
     Klipper can print it directly without any printer.cfg changes.
  4. Updates print.gcode inside the .mcfx to match.
"""

import sys
import os
import re
import types
import zipfile

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
# Tx strip — remove tool-change commands from processed gcode
# ---------------------------------------------------------------------------

# Matches bare tool-change lines: T0, T1, T0 ; comment, etc.
# Does NOT match T-words inside other commands (e.g. M104 T0 stays intact).
_TOOLCHANGE_LINE_RE = re.compile(r"^\s*T\d+\s*(?:;.*)?\r?$")

# Matches OctoPrint-specific O-commands used for connected Palette pings:
#   O31 L350.00 mm  — standard connected-mode ping (Palette 2/3)
#   O40 L350.00 mm  — Palette 3 connected via OctoPrint P3PING plugin
# These are not valid Klipper commands and would cause "Unknown command" errors.
# Standard accessory-mode pings (G4 pause sequences + retract/unretract) are
# inserted instead when P2PP ACCESSORY_MODE is configured, and those are
# valid Klipper gcode that the physical Palette 3 can detect via filament-flow
# sensing — no stripping needed for those.
_OCTOPRINT_CMD_RE = re.compile(r"^\s*O\d+\b.*\r?$")

# Matches specifically O31 ping commands (Palette 3 Connected Mode).
# P2PP emits O31 L<mm> mm for every ping in Connected Mode gcode.  These must
# be preserved when Klipper is printing the gcode so that the palette3.py
# component can intercept them and forward each ping to the Palette 3 device
# via MQTT.
_O31_CMD_RE = re.compile(r"^\s*O31\b.*\r?$")


def _strip_toolchange_lines(gcode_text, preserve_o31=False):
    """Return gcode_text with Tx and OctoPrint O-commands removed.

    Args:
        gcode_text:  Raw gcode string to process.
        preserve_o31: When True, O31 lines are kept (Connected Mode with
                      Klipper — the palette3.py component intercepts them at
                      runtime and forwards each ping to Palette 3 via MQTT).
                      When False (default), O31 is stripped along with all
                      other O-commands.
    """
    out = []
    for line in gcode_text.splitlines():
        if _TOOLCHANGE_LINE_RE.match(line):
            continue
        if _OCTOPRINT_CMD_RE.match(line):
            if preserve_o31 and _O31_CMD_RE.match(line):
                # Keep O31 for palette3.py to handle at print time.
                pass
            else:
                continue
        out.append(line)
    return "\n".join(out) + "\n"


def _repack_mcfx_and_write_gcode(mcfx_path, gcode_out_path, preserve_o31=False):
    """Extract print.gcode from the .mcfx, strip Tx lines, then:
      - overwrite gcode_out_path with the cleaned gcode (for Klipper)
      - rewrite the .mcfx with the cleaned print.gcode (for consistency)

    Returns True on success, False if the .mcfx cannot be read/written.
    """
    # --- read existing mcfx ---
    try:
        with zipfile.ZipFile(mcfx_path, "r") as zin:
            names = zin.namelist()
            contents = {name: zin.read(name) for name in names}
    except (zipfile.BadZipFile, OSError) as exc:
        print("[P2PP WARN] Could not open {}: {}".format(mcfx_path, exc), flush=True)
        return False

    if "print.gcode" not in contents:
        print("[P2PP WARN] print.gcode not found inside {}".format(mcfx_path), flush=True)
        return False

    # --- strip Tx lines (and O-commands, unless preserve_o31 is set) ---
    raw_gcode = contents["print.gcode"].decode("utf-8", errors="replace")
    cleaned_gcode = _strip_toolchange_lines(raw_gcode, preserve_o31=preserve_o31)
    contents["print.gcode"] = cleaned_gcode.encode("utf-8")

    # --- write cleaned gcode for Klipper ---
    try:
        with open(gcode_out_path, "w", encoding="utf-8") as fh:
            fh.write(cleaned_gcode)
        print(
            "[P2PP] Cleaned gcode (Tx stripped) written to {}".format(
                os.path.basename(gcode_out_path)
            ),
            flush=True,
        )
    except OSError as exc:
        print(
            "[P2PP WARN] Could not write cleaned gcode to {}: {}".format(
                gcode_out_path, exc
            ),
            flush=True,
        )
        return False

    # --- rewrite .mcfx with the cleaned print.gcode ---
    try:
        with zipfile.ZipFile(mcfx_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for name, data in contents.items():
                zout.writestr(name, data)
    except OSError as exc:
        print(
            "[P2PP WARN] Could not rewrite {}: {}".format(mcfx_path, exc),
            flush=True,
        )
        # gcode_out_path was already written successfully; non-fatal
        return True

    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        prog="p2pp_headless",
        description="Headless P2PP post-processor — generates .mcfx from PrusaSlicer gcode.",
    )
    parser.add_argument("input", metavar="INPUT.gcode", help="PrusaSlicer gcode file to process")
    parser.add_argument(
        "output",
        metavar="OUTPUT.mcfx",
        nargs="?",
        default=None,
        help="Output .mcfx path (optional — P2PP may derive it from the input)",
    )
    parser.add_argument(
        "--connected-accessory",
        action="store_true",
        default=False,
        help=(
            "Preserve O31 ping commands in the cleaned gcode. "
            "Use this when Klipper is printing Connected Mode gcode: "
            "the palette3.py component intercepts O31 at print time "
            "and forwards each ping to the Palette 3 device via MQTT."
        ),
    )
    args = parser.parse_args()

    input_file  = args.input
    output_file = args.output
    preserve_o31 = args.connected_accessory

    if not os.path.isfile(input_file):
        print("[P2PP ERROR] Input file not found: {}".format(input_file), file=sys.stderr)
        sys.exit(1)

    try:
        mcf.p2pp_process_file(input_file, output_file)
    except Exception as e:
        _logexception(e)
        sys.exit(1)

    # Strip Tx commands (and optionally O40) from the processed gcode.
    # The cleaned gcode replaces the original .gcode so Klipper can print it
    # without T-macro definitions, and the .mcfx is updated to keep
    # print.gcode consistent.
    if output_file and output_file.endswith(".mcfx") and os.path.isfile(output_file):
        _repack_mcfx_and_write_gcode(output_file, input_file, preserve_o31=preserve_o31)

    sys.exit(2 if v.process_warnings else 0)


if __name__ == "__main__":
    main()
