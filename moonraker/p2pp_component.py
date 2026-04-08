"""
moonraker/p2pp_component.py - Moonraker component for P2PP post-processing.

Watches Moonraker's gcodes root for newly uploaded .gcode files that contain
P2PP Palette 3 configuration markers, then automatically runs p2pp_headless.py
as a subprocess to generate the corresponding .mcfx file.

Installation
------------
1. Place (or symlink) this file into Moonraker's components directory:
       ~/moonraker/moonraker/components/p2pp.py
   OR configure a custom component path in moonraker.conf.

2. Add a [p2pp] section to moonraker.conf:

       [p2pp]
       # Required: absolute path to p2pp_headless.py
       script_path: /home/pi/p2pp/p2pp_headless.py

       # Optional: file roots to monitor (default: gcodes)
       # watch_roots: gcodes

       # Optional: max concurrent processing jobs (default: 2)
       # max_concurrent: 2

3. Restart Moonraker.

Workflow
--------
    Slicer  --upload--> Moonraker gcodes/myprint.gcode
                              |
                    [p2pp component detects upload]
                              |
                    p2pp_headless.py myprint.gcode myprint.mcfx
                              |
                    gcodes/myprint.mcfx  (appears in Fluidd/Mainsail)

The original .gcode is left untouched.  Configuration comes entirely from
;P2PP ... comments embedded in the gcode by PrusaSlicer.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..confighelper import ConfigHelper

# Regex matching the P2PP Palette 3 marker written by PrusaSlicer into
# the config block at the end of every exported gcode file.
_P2PP_MARKER = re.compile(
    r"^;\s*P2PP\s+(PALETTE3|PALETTE3_PRO)",
    re.MULTILINE,
)

# How many bytes to read from the tail of the file when scanning for the
# marker.  PrusaSlicer writes its entire config block at the very end of
# the file; 32 KB is far more than enough.
_SCAN_TAIL_BYTES = 32 * 1024


class P2PPComponent:
    """Moonraker component that post-processes P2PP gcode on file upload."""

    def __init__(self, config: "ConfigHelper") -> None:
        self.server = config.get_server()
        self.logger = logging.getLogger("moonraker.p2pp")

        self._script: str = config.get("script_path")
        self._watch_roots: list[str] = config.getlist("watch_roots", ["gcodes"])
        max_concurrent: int = config.getint("max_concurrent", 2)
        self._semaphore = asyncio.Semaphore(max_concurrent)

        if not os.path.isfile(self._script):
            self.logger.warning(
                "P2PP: script_path does not exist: %s — component will be inactive",
                self._script,
            )

        self.server.register_event_handler(
            "file_manager:filelist_changed",
            self._on_filelist_changed,
        )
        self.logger.info(
            "P2PP: component loaded (watching roots: %s)",
            ", ".join(self._watch_roots),
        )

    # ------------------------------------------------------------------
    # Event handler
    # ------------------------------------------------------------------

    async def _on_filelist_changed(
        self,
        action: str,
        item: dict,
        source_item: dict | None = None,
    ) -> None:
        """Called by Moonraker whenever the file list changes."""
        if action != "create_file":
            return

        filename: str = item.get("filename", "")
        root: str = item.get("root", "")

        # Only process .gcode files in the configured roots.
        if root not in self._watch_roots:
            return
        if not filename.lower().endswith(".gcode"):
            return

        # Resolve the full filesystem path.
        fm = self.server.lookup_component("file_manager")
        root_path: str = fm.get_directory(root)
        full_path = os.path.join(root_path, filename)

        # Quick pre-scan — avoid spawning a subprocess for every gcode upload.
        loop = asyncio.get_event_loop()
        is_p2pp = await loop.run_in_executor(
            None, self._has_p2pp_marker, full_path
        )
        if not is_p2pp:
            return

        # Derive the output .mcfx path (same directory, same base name).
        base, _ = os.path.splitext(full_path)
        output_path = base + ".mcfx"

        self.logger.info("P2PP: Queuing processing for: %s", filename)

        # Acquire semaphore before spawning to cap concurrency.
        async with self._semaphore:
            await self._run_subprocess(full_path, output_path)

    # ------------------------------------------------------------------
    # P2PP marker detection
    # ------------------------------------------------------------------

    def _has_p2pp_marker(self, path: str) -> bool:
        """Return True if the file contains a P2PP Palette 3 config marker.

        Reads only the tail of the file for efficiency — PrusaSlicer always
        writes its configuration block at the very end of the exported gcode.
        """
        try:
            file_size = os.path.getsize(path)
            with open(path, encoding="utf-8", errors="replace") as fh:
                fh.seek(max(0, file_size - _SCAN_TAIL_BYTES))
                tail = fh.read()
            return bool(_P2PP_MARKER.search(tail))
        except OSError as exc:
            self.logger.warning("P2PP: Could not read %s for marker scan: %s", path, exc)
            return False

    # ------------------------------------------------------------------
    # Subprocess execution
    # ------------------------------------------------------------------

    async def _run_subprocess(self, input_path: str, output_path: str) -> None:
        """Spawn p2pp_headless.py and stream its output to the Moonraker log."""
        self.logger.info(
            "P2PP: Processing %s -> %s",
            os.path.basename(input_path),
            os.path.basename(output_path),
        )

        cmd = [sys.executable, self._script, input_path, output_path]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
        except Exception as exc:
            self.logger.error(
                "P2PP: Failed to launch subprocess for %s: %s",
                os.path.basename(input_path),
                exc,
            )
            return

        # Forward subprocess output to Moonraker's log.
        for line in stdout_bytes.decode(errors="replace").splitlines():
            line = line.strip()
            if line:
                if "WARN" in line:
                    self.logger.warning(line)
                else:
                    self.logger.info(line)

        for line in stderr_bytes.decode(errors="replace").splitlines():
            line = line.strip()
            if line:
                self.logger.warning("P2PP stderr: %s", line)

        # Exit-code semantics (defined in p2pp_headless.py):
        #   0  — success, no warnings
        #   2  — success, with warnings
        #   other — failure
        rc = proc.returncode
        if rc in (0, 2):
            if rc == 2:
                self.logger.warning(
                    "P2PP: Completed with warnings: %s",
                    os.path.basename(output_path),
                )
            else:
                self.logger.info(
                    "P2PP: Completed successfully: %s",
                    os.path.basename(output_path),
                )
            # Moonraker's inotify watcher will detect the new .mcfx file
            # automatically; no explicit notification is needed.
        else:
            self.logger.error(
                "P2PP: Processing FAILED (exit %d) for %s",
                rc,
                os.path.basename(input_path),
            )


# ---------------------------------------------------------------------------
# Moonraker component factory
# ---------------------------------------------------------------------------

def load_component(config: "ConfigHelper") -> P2PPComponent:
    return P2PPComponent(config)
