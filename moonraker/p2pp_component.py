"""
moonraker/p2pp_component.py - Moonraker component for P2PP post-processing.

Watches Moonraker's gcodes root for newly uploaded .gcode files that contain
P2PP Palette 3 configuration markers, then automatically runs p2pp_headless.py
as a subprocess to generate the corresponding .mcfx file.

After the .mcfx is created the component can optionally:
  - Upload it to the Palette 3 device over its HTTP API
  - Instruct Klipper to start printing the original .gcode

Installation
------------
1. Place (or symlink) this file into Moonraker's components directory:
       ~/moonraker/moonraker/components/p2pp.py
   OR configure a custom component path in moonraker.conf.

2. Add a [p2pp] section to moonraker.conf:

       [p2pp]
       # Required: absolute path to p2pp_headless.py
       script_path: /home/pi/p2pp/p2pp_headless.py

       # Optional: IP or hostname of the Palette 3 for automatic .mcfx upload
       # palette3_host: 192.168.1.100

       # Optional: start printing the .gcode automatically after upload
       # auto_print: false

       # Optional: file roots to monitor (default: gcodes)
       # watch_roots: gcodes

       # Optional: max concurrent processing jobs (default: 2)
       # max_concurrent: 2

       # Optional: Palette 3 upload timeout in seconds (default: 120)
       # p3_upload_timeout: 120

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
                              |
              [if palette3_host configured]
                              |
                    POST http://<palette3_host>:5000/print-file
                              |
              [if auto_print: true]
                              |
                    Klipper starts printing myprint.gcode
                    Palette 3 runs in sync via embedded pings

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

try:
    import aiohttp as _aiohttp
except ImportError:
    _aiohttp = None  # type: ignore[assignment]

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

        # Palette 3 upload / auto-print options
        self._p3_host: str = config.get("palette3_host", "")
        self._p3_upload_timeout: int = config.getint("p3_upload_timeout", 120)
        self._auto_print: bool = config.getboolean("auto_print", False)

        if not os.path.isfile(self._script):
            self.logger.warning(
                "P2PP: script_path does not exist: %s — component will be inactive",
                self._script,
            )

        if self._p3_host and _aiohttp is None:
            self.logger.warning(
                "P2PP: palette3_host is set but aiohttp is not installed — "
                "upload to Palette 3 will be skipped"
            )

        self.server.register_event_handler(
            "file_manager:filelist_changed",
            self._on_filelist_changed,
        )
        self.logger.info(
            "P2PP: component loaded (roots: %s, p3_host: %s, auto_print: %s)",
            ", ".join(self._watch_roots),
            self._p3_host or "not configured",
            self._auto_print,
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

        async with self._semaphore:
            success = await self._run_subprocess(full_path, output_path)
            if not success:
                return

            # Upload .mcfx to Palette 3 if host is configured.
            if self._p3_host:
                uploaded = await self._upload_to_palette3(output_path)
                if not uploaded:
                    # Upload failed — do not attempt to start the print, since
                    # the Palette 3 won't be ready.
                    return

            # Optionally start the Klipper print so both devices run in sync.
            if self._auto_print:
                await self._start_print(filename)

    # ------------------------------------------------------------------
    # P2PP marker detection
    # ------------------------------------------------------------------

    def _has_p2pp_marker(self, path: str) -> bool:
        """Return True if the file contains a P2PP Palette 3 config marker."""
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

    async def _run_subprocess(self, input_path: str, output_path: str) -> bool:
        """Spawn p2pp_headless.py and stream its output to the Moonraker log.

        Returns True on success (exit codes 0 or 2), False on failure.
        """
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
            return False

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
            return True
        else:
            self.logger.error(
                "P2PP: Processing FAILED (exit %d) for %s",
                rc,
                os.path.basename(input_path),
            )
            return False

    # ------------------------------------------------------------------
    # Palette 3 upload
    # ------------------------------------------------------------------

    async def _upload_to_palette3(self, mcfx_path: str) -> bool:
        """Upload the .mcfx file to the Palette 3 via its HTTP API.

        The Palette 3 exposes a simple multipart upload endpoint:
            POST http://<host>:5000/print-file
        with a single field named ``printFile``.

        Returns True on success, False on any error.
        """
        if _aiohttp is None:
            self.logger.error(
                "P2PP: Cannot upload — aiohttp is not installed"
            )
            return False

        p3_filename = os.path.basename(mcfx_path)
        url = "http://{}:5000/print-file".format(self._p3_host)
        self.logger.info(
            "P2PP: Uploading %s to Palette 3 at %s", p3_filename, self._p3_host
        )

        try:
            timeout = _aiohttp.ClientTimeout(total=self._p3_upload_timeout)
            async with _aiohttp.ClientSession(timeout=timeout) as session:
                with open(mcfx_path, "rb") as fh:
                    form = _aiohttp.FormData()
                    form.add_field(
                        "printFile",
                        fh,
                        filename=p3_filename,
                        content_type="application/octet-stream",
                    )
                    resp = await session.post(url, data=form)

            if resp.status == 200:
                self.logger.info(
                    "P2PP: Upload complete — %s is ready on Palette 3", p3_filename
                )
                return True
            else:
                self.logger.error(
                    "P2PP: Upload failed [HTTP %d] for %s", resp.status, p3_filename
                )
                return False

        except asyncio.TimeoutError:
            self.logger.error(
                "P2PP: Upload timed out after %ds for %s",
                self._p3_upload_timeout,
                p3_filename,
            )
        except OSError as exc:
            self.logger.error(
                "P2PP: Could not read .mcfx for upload (%s): %s", p3_filename, exc
            )
        except Exception as exc:
            self.logger.error(
                "P2PP: Upload error for %s: %s", p3_filename, exc
            )

        return False

    # ------------------------------------------------------------------
    # Auto-print
    # ------------------------------------------------------------------

    async def _start_print(self, gcode_filename: str) -> None:
        """Instruct Klipper to start printing gcode_filename.

        Uses Moonraker's klippy_apis component so the call is internal and
        does not require an extra HTTP round-trip.  The Palette 3 must
        already have the matching .mcfx loaded (uploaded in the previous
        step) before calling this, so that both devices start in sync.
        """
        try:
            kapis = self.server.lookup_component("klippy_apis")
            await kapis.start_print(gcode_filename)
            self.logger.info("P2PP: Print started: %s", gcode_filename)
        except Exception as exc:
            self.logger.error(
                "P2PP: Failed to start print %s: %s", gcode_filename, exc
            )


# ---------------------------------------------------------------------------
# Moonraker component factory
# ---------------------------------------------------------------------------

def load_component(config: "ConfigHelper") -> P2PPComponent:
    return P2PPComponent(config)
