"""
Tests for p2pp_headless.py — regex stripping and end-to-end splice output.
"""

import json
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

# ---------------------------------------------------------------------------
# Bootstrap — import p2pp_headless so we can access its module-level symbols.
# The module stubs out p2pp.gui and p2pp.p3_upload in sys.modules before
# importing any p2pp code, so PyQt5 is never touched.
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import p2pp_headless as h  # noqa: E402  (after sys.path update)

EXAMPLE_GCODE = os.path.join(REPO_ROOT, "examples", "two_color_palette3.gcode")


# ---------------------------------------------------------------------------
# Toolchange-line regex
# ---------------------------------------------------------------------------


class TestToolchangeLineRegex:
    def test_bare_t0(self):
        assert h._TOOLCHANGE_LINE_RE.match("T0")

    def test_bare_t1(self):
        assert h._TOOLCHANGE_LINE_RE.match("T1")

    def test_bare_t10(self):
        assert h._TOOLCHANGE_LINE_RE.match("T10")

    def test_t_with_trailing_comment(self):
        assert h._TOOLCHANGE_LINE_RE.match("T0 ; select tool 0")

    def test_t_with_leading_whitespace(self):
        assert h._TOOLCHANGE_LINE_RE.match("  T1  ")

    def test_does_not_match_m104_t0(self):
        # T as a parameter word inside another command must be preserved
        assert not h._TOOLCHANGE_LINE_RE.match("M104 T0 S200")

    def test_does_not_match_m109_t1(self):
        assert not h._TOOLCHANGE_LINE_RE.match("M109 T1 S210")

    def test_does_not_match_comment_mentioning_t0(self):
        assert not h._TOOLCHANGE_LINE_RE.match("; T0 is a tool change comment")

    def test_does_not_match_g1(self):
        assert not h._TOOLCHANGE_LINE_RE.match("G1 X10 Y10")


# ---------------------------------------------------------------------------
# OctoPrint O-command regex
# ---------------------------------------------------------------------------


class TestOctoprintCmdRegex:
    def test_o31_standard_ping(self):
        assert h._OCTOPRINT_CMD_RE.match("O31 L350.00 mm")

    def test_o40_p3ping_plugin(self):
        assert h._OCTOPRINT_CMD_RE.match("O40 L350.00 mm")

    def test_leading_whitespace(self):
        assert h._OCTOPRINT_CMD_RE.match("  O31 L350.00 mm")

    def test_does_not_match_g_command(self):
        assert not h._OCTOPRINT_CMD_RE.match("G1 X10")

    def test_does_not_match_comment_mentioning_o31(self):
        assert not h._OCTOPRINT_CMD_RE.match("; O31 L350.00 mm")

    def test_does_not_match_m_command(self):
        assert not h._OCTOPRINT_CMD_RE.match("M104 S200")


# ---------------------------------------------------------------------------
# _O31_CMD_RE
# ---------------------------------------------------------------------------


class TestO31CmdRegex:
    def test_matches_o31(self):
        assert h._O31_CMD_RE.match("O31 L350.00 mm")

    def test_matches_o31_with_leading_whitespace(self):
        assert h._O31_CMD_RE.match("  O31 L350.00 mm")

    def test_does_not_match_o40(self):
        assert not h._O31_CMD_RE.match("O40 L350.00 mm")

    def test_does_not_match_o30(self):
        assert not h._O31_CMD_RE.match("O30 L350.00 mm")


# ---------------------------------------------------------------------------
# _strip_toolchange_lines()
# ---------------------------------------------------------------------------


class TestStripToolchangeLines:
    def test_strips_tx_lines(self):
        gcode = "G1 X10\nT0\nG1 X20\nT1\nG1 X30\n"
        result = h._strip_toolchange_lines(gcode)
        assert "T0" not in result
        assert "T1" not in result
        assert "G1 X10" in result
        assert "G1 X20" in result
        assert "G1 X30" in result

    def test_strips_octoprint_pings(self):
        gcode = "G1 X10\nO31 L350.00 mm\nG1 X20\nO40 L400.00 mm\n"
        result = h._strip_toolchange_lines(gcode)
        assert "O31" not in result
        assert "O40" not in result
        assert "G1 X10" in result
        assert "G1 X20" in result

    def test_preserve_o31_keeps_o31_lines(self):
        """With preserve_o31=True, O31 lines must survive stripping."""
        gcode = "G1 X10\nO31 L350.00 mm\nG1 X20\nO40 L400.00 mm\n"
        result = h._strip_toolchange_lines(gcode, preserve_o31=True)
        assert "O31 L350.00 mm" in result, "O31 must be preserved"
        assert "O40" not in result, "O40 must still be stripped"
        assert "G1 X10" in result
        assert "G1 X20" in result

    def test_preserve_o31_still_strips_tx(self):
        """Tx lines must still be removed even when preserve_o31 is True."""
        gcode = "T0\nO31 L150.00 mm\nG1 X10\n"
        result = h._strip_toolchange_lines(gcode, preserve_o31=True)
        assert "T0" not in result
        assert "O31 L150.00 mm" in result

    def test_preserve_o31_false_strips_o31(self):
        """Default behaviour (preserve_o31=False) must strip O31."""
        gcode = "G1 X10\nO31 L350.00 mm\n"
        result = h._strip_toolchange_lines(gcode, preserve_o31=False)
        assert "O31" not in result

    def test_preserves_m104_t0(self):
        """Temperature commands with T-parameter must survive stripping."""
        gcode = "M104 T0 S200\nT0\nM109 T1 S210\n"
        result = h._strip_toolchange_lines(gcode)
        assert "M104 T0 S200" in result
        assert "M109 T1 S210" in result
        # Bare T0 is stripped; the remaining T0 is only inside M104
        lines = result.splitlines()
        bare_t = [l for l in lines if l.strip() in ("T0", "T1")]
        assert bare_t == [], "bare Tx lines should have been removed"

    def test_output_ends_with_newline(self):
        result = h._strip_toolchange_lines("G1 X10\nT0\n")
        assert result.endswith("\n")

    def test_empty_input(self):
        result = h._strip_toolchange_lines("")
        assert result == "\n"

    def test_no_toolchange_lines(self):
        gcode = "G28\nG1 X10 Y10\nM104 S200\n"
        result = h._strip_toolchange_lines(gcode)
        assert result == gcode


# ---------------------------------------------------------------------------
# Integration — run headless on example gcode, verify palette.json splices
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIntegration:
    def test_example_gcode_splice_positions(self, tmp_path):
        """Two-color example must produce exactly the documented splice points.

        Expected (SPLICEOFFSET=100, EXTRAENDFILAMENT=0):
            splice 1 → {"id": 1, "length": 250.0}
            splice 2 → {"id": 2, "length": 410.0}
        """
        if not os.path.isfile(EXAMPLE_GCODE):
            pytest.skip("example gcode not found: {}".format(EXAMPLE_GCODE))

        # Work on a copy — the headless script overwrites the input file with
        # the cleaned (Tx-stripped) gcode.
        gcode_copy = str(tmp_path / "test.gcode")
        shutil.copy2(EXAMPLE_GCODE, gcode_copy)
        mcfx_out = str(tmp_path / "test.mcfx")

        script = os.path.join(REPO_ROOT, "p2pp_headless.py")
        result = subprocess.run(
            [sys.executable, script, gcode_copy, mcfx_out],
            capture_output=True,
            text=True,
        )

        # Exit 0 = success, 2 = success with non-fatal warnings (expected here
        # because the example gcode lacks a splice algorithm definition and a
        # thumbnail, which trigger warnings but not errors).
        assert result.returncode in (0, 2), (
            "p2pp_headless.py failed (exit {}):\n"
            "stdout: {}\nstderr: {}".format(
                result.returncode, result.stdout, result.stderr
            )
        )
        assert os.path.isfile(mcfx_out), ".mcfx file was not created"

        with zipfile.ZipFile(mcfx_out) as zf:
            assert "palette.json" in zf.namelist(), "palette.json missing from .mcfx"
            palette = json.loads(zf.read("palette.json"))

        splices = palette["splices"]
        assert len(splices) == 2, "expected 2 splices, got {}".format(len(splices))
        assert splices[0] == {"id": 1, "length": 250.0}, splices[0]
        assert splices[1] == {"id": 2, "length": 410.0}, splices[1]

    def test_cleaned_gcode_has_no_toolchange_lines(self, tmp_path):
        """After headless processing, the .gcode file must contain no bare Tx lines."""
        if not os.path.isfile(EXAMPLE_GCODE):
            pytest.skip("example gcode not found")

        gcode_copy = str(tmp_path / "test.gcode")
        shutil.copy2(EXAMPLE_GCODE, gcode_copy)
        mcfx_out = str(tmp_path / "test.mcfx")

        script = os.path.join(REPO_ROOT, "p2pp_headless.py")
        subprocess.run(
            [sys.executable, script, gcode_copy, mcfx_out],
            capture_output=True,
        )

        with open(gcode_copy, encoding="utf-8") as fh:
            lines = fh.readlines()

        bare_tx = [
            l.rstrip() for l in lines if h._TOOLCHANGE_LINE_RE.match(l)
        ]
        assert bare_tx == [], "cleaned gcode still contains Tx lines: {}".format(bare_tx)
