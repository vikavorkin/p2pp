"""
Tests for the P2PP marker detection logic used by the Moonraker component.

We test the regex and scanning logic directly rather than instantiating
P2PPComponent (which needs a live Moonraker ConfigHelper).
"""

import os
import re

import pytest

# ---------------------------------------------------------------------------
# Replicate the module-level symbols from moonraker/p2pp_component.py so
# tests run without Moonraker installed.
# ---------------------------------------------------------------------------

_P2PP_MARKER = re.compile(
    r"^;\s*P2PP\s+(PALETTE3|PALETTE3_PRO)",
    re.MULTILINE,
)
_SCAN_TAIL_BYTES = 32 * 1024


def _has_p2pp_marker(path: str) -> bool:
    """Standalone copy of P2PPComponent._has_p2pp_marker for unit testing."""
    try:
        file_size = os.path.getsize(path)
        with open(path, encoding="utf-8", errors="replace") as fh:
            fh.seek(max(0, file_size - _SCAN_TAIL_BYTES))
            tail = fh.read()
        return bool(_P2PP_MARKER.search(tail))
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Regex unit tests
# ---------------------------------------------------------------------------


class TestP2PPMarkerRegex:
    def test_matches_palette3(self):
        assert _P2PP_MARKER.search("; P2PP PALETTE3")

    def test_matches_palette3_pro(self):
        assert _P2PP_MARKER.search("; P2PP PALETTE3_PRO")

    def test_matches_without_space_before_p2pp(self):
        assert _P2PP_MARKER.search(";P2PP PALETTE3")

    def test_matches_extra_whitespace(self):
        assert _P2PP_MARKER.search(";  P2PP  PALETTE3")

    def test_does_not_match_spliceoffset(self):
        assert not _P2PP_MARKER.search("; P2PP SPLICEOFFSET=100")

    def test_does_not_match_printerprofile(self):
        assert not _P2PP_MARKER.search("; P2PP PRINTERPROFILE=abc123")

    def test_does_not_match_plain_p2pp(self):
        assert not _P2PP_MARKER.search("; P2PP")

    def test_matches_within_multiline_text(self):
        text = "G1 X10\n; some comment\n; P2PP PALETTE3\n; end\n"
        assert _P2PP_MARKER.search(text)


# ---------------------------------------------------------------------------
# File scanning tests
# ---------------------------------------------------------------------------


class TestHasP2PPMarker:
    def _write(self, tmp_path, content: str) -> str:
        p = tmp_path / "test.gcode"
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_detects_palette3(self, tmp_path):
        path = self._write(tmp_path, "; P2PP PALETTE3\n")
        assert _has_p2pp_marker(path)

    def test_detects_palette3_pro(self, tmp_path):
        path = self._write(tmp_path, "; P2PP PALETTE3_PRO\n")
        assert _has_p2pp_marker(path)

    def test_detects_compact_marker(self, tmp_path):
        path = self._write(tmp_path, ";P2PP PALETTE3\n")
        assert _has_p2pp_marker(path)

    def test_no_marker_returns_false(self, tmp_path):
        path = self._write(tmp_path, "G1 X10 Y10\n; regular gcode\n")
        assert not _has_p2pp_marker(path)

    def test_only_other_p2pp_params_returns_false(self, tmp_path):
        content = "; P2PP SPLICEOFFSET=100\n; P2PP PRINTERPROFILE=abc\n"
        path = self._write(tmp_path, content)
        assert not _has_p2pp_marker(path)

    def test_nonexistent_file_returns_false(self, tmp_path):
        assert not _has_p2pp_marker(str(tmp_path / "does_not_exist.gcode"))

    def test_marker_in_tail_of_large_file(self, tmp_path):
        """Marker must be found even when the file exceeds _SCAN_TAIL_BYTES."""
        # ~50 KB of filler puts the marker beyond the first 32 KB window
        filler = "G1 X10 Y10 E0.5\n" * 3200  # ≈51 KB
        content = filler + "; P2PP PALETTE3\n"
        path = self._write(tmp_path, content)
        assert _has_p2pp_marker(path)

    def test_marker_only_before_tail_not_detected(self, tmp_path):
        """If the marker is only in the part before the tail window, it is missed.

        This matches the intended design: P2PP markers appear in the config
        block at the END of PrusaSlicer gcode, so scanning the tail is
        sufficient.  We document (not fix) the behaviour here.
        """
        # Place marker at the very start, then pad with > 32 KB of filler
        marker = "; P2PP PALETTE3\n"
        filler = "G1 X10\n" * 5000  # ~70 KB
        content = marker + filler
        path = self._write(tmp_path, content)
        # The marker is outside the scan window — not detected
        assert not _has_p2pp_marker(path)
