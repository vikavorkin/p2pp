"""
Tests based on the committed example files in examples/.

These tests treat the example files as a specification: every assertion
encodes a known-good property of a real P2PP output, so a regression in
either the file format or the post-processing tools will fail here.

Files under test
----------------
examples/201901MosaicKeychain2.mafx
    Pure Accessory Mode output.  5-colour Voron print.  No print.gcode
    inside — Klipper prints the source gcode directly while Palette 3
    reads splice/ping positions from palette.json autonomously.

examples/201901MosaicKeychain2.mcfx
    Connected Mode output of the same print.  Contains print.gcode with
    O31 ping commands and bare Tx tool-change lines.

examples/201901MosaicKeychain2.zip
    Original PrusaSlicer source bundle: raw .gcode (with G4 pause
    sequences and bare Tx lines) + the same .mafx as above.
"""

import io
import json
import os
import re
import sys
import zipfile

import pytest

# ---------------------------------------------------------------------------
# Bootstrap p2pp_headless so we can call _strip_toolchange_lines directly.
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
import p2pp_headless as h  # noqa: E402

EXAMPLES = os.path.join(REPO_ROOT, "examples")
MAFX = os.path.join(EXAMPLES, "201901MosaicKeychain2.mafx")
MCFX = os.path.join(EXAMPLES, "201901MosaicKeychain2.mcfx")
ZIP  = os.path.join(EXAMPLES, "201901MosaicKeychain2.zip")


# ===========================================================================
# Fixtures
# ===========================================================================

@pytest.fixture(scope="module")
def mafx_zip():
    return zipfile.ZipFile(MAFX)


@pytest.fixture(scope="module")
def mafx_palette(mafx_zip):
    return json.loads(mafx_zip.read("palette.json"))


@pytest.fixture(scope="module")
def mafx_meta(mafx_zip):
    return json.loads(mafx_zip.read("meta.json"))


@pytest.fixture(scope="module")
def mcfx_zip():
    return zipfile.ZipFile(MCFX)


@pytest.fixture(scope="module")
def mcfx_palette(mcfx_zip):
    return json.loads(mcfx_zip.read("palette.json"))


@pytest.fixture(scope="module")
def mcfx_meta(mcfx_zip):
    return json.loads(mcfx_zip.read("meta.json"))


@pytest.fixture(scope="module")
def mcfx_gcode(mcfx_zip):
    return mcfx_zip.read("print.gcode").decode("utf-8", errors="replace")


@pytest.fixture(scope="module")
def src_zip():
    return zipfile.ZipFile(ZIP)


@pytest.fixture(scope="module")
def src_gcode(src_zip):
    return src_zip.read("201901MosaicKeychain2.gcode").decode("utf-8", errors="replace")


# ===========================================================================
# MAFX — ZIP structure
# ===========================================================================

class TestMafxStructure:
    def test_is_valid_zip(self):
        assert zipfile.is_zipfile(MAFX)

    def test_contains_palette_json(self, mafx_zip):
        assert "palette.json" in mafx_zip.namelist()

    def test_contains_meta_json(self, mafx_zip):
        assert "meta.json" in mafx_zip.namelist()

    def test_contains_thumbnail(self, mafx_zip):
        names = mafx_zip.namelist()
        assert any(n.lower().endswith(".png") for n in names), (
            "thumbnail .png not found in mafx"
        )

    def test_does_not_contain_print_gcode(self, mafx_zip):
        """MAFX is Accessory Mode — Klipper prints its own gcode; no embedded gcode."""
        assert "print.gcode" not in mafx_zip.namelist()

    def test_thumbnail_is_non_empty(self, mafx_zip):
        names = mafx_zip.namelist()
        png = next(n for n in names if n.lower().endswith(".png"))
        assert len(mafx_zip.read(png)) > 0


# ===========================================================================
# MAFX — palette.json schema
# ===========================================================================

class TestMafxPaletteSchema:
    def test_version_is_string(self, mafx_palette):
        assert isinstance(mafx_palette["version"], str)

    def test_version_is_3_0(self, mafx_palette):
        assert mafx_palette["version"] == "3.0"

    def test_drives_is_list(self, mafx_palette):
        assert isinstance(mafx_palette["drives"], list)

    def test_splices_is_list(self, mafx_palette):
        assert isinstance(mafx_palette["splices"], list)

    def test_pings_is_list(self, mafx_palette):
        assert isinstance(mafx_palette["pings"], list)

    def test_algorithms_is_list(self, mafx_palette):
        assert isinstance(mafx_palette["algorithms"], list)

    def test_no_ping_count_key(self, mafx_palette):
        """MAFX uses a populated pings array, not a pingCount integer."""
        assert "pingCount" not in mafx_palette

    def test_each_splice_has_id_and_length(self, mafx_palette):
        for splice in mafx_palette["splices"]:
            assert "id" in splice, f"missing 'id' in {splice}"
            assert "length" in splice, f"missing 'length' in {splice}"

    def test_each_ping_has_length_and_extrusion(self, mafx_palette):
        for ping in mafx_palette["pings"]:
            assert "length" in ping, f"missing 'length' in {ping}"
            assert "extrusion" in ping, f"missing 'extrusion' in {ping}"

    def test_each_algorithm_has_required_fields(self, mafx_palette):
        required = {"ingoingId", "outgoingId", "heat", "compression", "cooling"}
        for alg in mafx_palette["algorithms"]:
            assert required.issubset(alg.keys()), f"algorithm missing fields: {alg}"


# ===========================================================================
# MAFX — palette.json values
# ===========================================================================

class TestMafxPaletteValues:
    def test_splice_count(self, mafx_palette):
        assert len(mafx_palette["splices"]) == 12

    def test_ping_count(self, mafx_palette):
        assert len(mafx_palette["pings"]) == 11

    def test_drives_length(self, mafx_palette):
        assert len(mafx_palette["drives"]) == 8

    def test_five_active_drives(self, mafx_palette):
        """Five non-zero drive slots for a 5-colour print."""
        active = [d for d in mafx_palette["drives"] if d != 0]
        assert len(active) == 5

    def test_active_drive_ids(self, mafx_palette):
        active = sorted(d for d in mafx_palette["drives"] if d != 0)
        assert active == [1, 2, 3, 4, 5]

    def test_first_splice(self, mafx_palette):
        assert mafx_palette["splices"][0] == {"id": 2, "length": 130.0}

    def test_last_splice(self, mafx_palette):
        assert mafx_palette["splices"][-1] == {"id": 3, "length": 4375.39}

    def test_first_ping(self, mafx_palette):
        assert mafx_palette["pings"][0] == {"length": 374.92, "extrusion": 20.0}

    def test_last_ping(self, mafx_palette):
        assert mafx_palette["pings"][-1] == {"length": 4184.82, "extrusion": 20.0}

    def test_all_ping_extrusion_values_are_20mm(self, mafx_palette):
        for ping in mafx_palette["pings"]:
            assert ping["extrusion"] == 20.0, (
                f"expected extrusion=20.0, got {ping['extrusion']}"
            )

    def test_splice_lengths_monotonically_increasing(self, mafx_palette):
        lengths = [s["length"] for s in mafx_palette["splices"]]
        for i in range(len(lengths) - 1):
            assert lengths[i] < lengths[i + 1], (
                f"splice lengths not increasing at index {i}: "
                f"{lengths[i]} >= {lengths[i+1]}"
            )

    def test_ping_lengths_monotonically_increasing(self, mafx_palette):
        lengths = [p["length"] for p in mafx_palette["pings"]]
        for i in range(len(lengths) - 1):
            assert lengths[i] < lengths[i + 1], (
                f"ping lengths not increasing at index {i}: "
                f"{lengths[i]} >= {lengths[i+1]}"
            )

    def test_all_splice_ids_reference_active_drives(self, mafx_palette):
        active = {d for d in mafx_palette["drives"] if d != 0}
        for splice in mafx_palette["splices"]:
            assert splice["id"] in active, (
                f"splice id {splice['id']} not in active drives {active}"
            )

    def test_all_pings_precede_last_splice(self, mafx_palette):
        last_splice_len = mafx_palette["splices"][-1]["length"]
        for ping in mafx_palette["pings"]:
            assert ping["length"] < last_splice_len, (
                f"ping at {ping['length']} mm is beyond last splice at {last_splice_len} mm"
            )


# ===========================================================================
# MAFX — meta.json
# ===========================================================================

class TestMafxMeta:
    def test_meta_version_is_string(self, mafx_meta):
        assert isinstance(mafx_meta["version"], str)

    def test_meta_splice_count_matches_palette(self, mafx_meta, mafx_palette):
        assert mafx_meta["splices"] == len(mafx_palette["splices"])

    def test_meta_ping_count_matches_palette(self, mafx_meta, mafx_palette):
        assert mafx_meta["pings"] == len(mafx_palette["pings"])

    def test_meta_total_length_approx(self, mafx_meta):
        """totalLength should be close to the last splice length."""
        assert abs(mafx_meta["totalLength"] - 4375.39) < 0.01

    def test_meta_inputs_used(self, mafx_meta):
        assert mafx_meta["inputsUsed"] == 5


# ===========================================================================
# MCFX — ZIP structure
# ===========================================================================

class TestMcfxStructure:
    def test_is_valid_zip(self):
        assert zipfile.is_zipfile(MCFX)

    def test_contains_print_gcode(self, mcfx_zip):
        """MCFX (Connected Mode) must embed print.gcode."""
        assert "print.gcode" in mcfx_zip.namelist()

    def test_contains_palette_json(self, mcfx_zip):
        assert "palette.json" in mcfx_zip.namelist()

    def test_contains_meta_json(self, mcfx_zip):
        assert "meta.json" in mcfx_zip.namelist()

    def test_contains_thumbnail(self, mcfx_zip):
        names = mcfx_zip.namelist()
        assert any(n.lower().endswith(".png") for n in names)

    def test_print_gcode_is_non_empty(self, mcfx_gcode):
        assert len(mcfx_gcode) > 0


# ===========================================================================
# MCFX — palette.json schema and values
# ===========================================================================

class TestMcfxPalette:
    def test_version_is_3_0(self, mcfx_palette):
        assert mcfx_palette["version"] == "3.0"

    def test_has_ping_count_not_pings_array(self, mcfx_palette):
        """Connected Mode uses pingCount integer, not a populated pings array."""
        assert "pingCount" in mcfx_palette
        assert "pings" not in mcfx_palette

    def test_ping_count_value(self, mcfx_palette):
        assert mcfx_palette["pingCount"] == 10

    def test_splice_count(self, mcfx_palette):
        assert len(mcfx_palette["splices"]) == 12

    def test_first_splice_same_as_mafx(self, mcfx_palette, mafx_palette):
        """The very first splice (pure extrusion before any tool-change) is
        the same in both modes because no ping offset applies at position 0."""
        assert mcfx_palette["splices"][0] == mafx_palette["splices"][0]

    def test_drives_same_as_mafx(self, mcfx_palette, mafx_palette):
        assert mcfx_palette["drives"] == mafx_palette["drives"]

    def test_mcfx_splice_lengths_shorter_than_mafx(self, mcfx_palette, mafx_palette):
        """MCFX splice lengths are shorter because ping-offset accounting
        differs between Accessory and Connected modes."""
        mafx_total = mafx_palette["splices"][-1]["length"]
        mcfx_total = mcfx_palette["splices"][-1]["length"]
        assert mcfx_total < mafx_total, (
            f"expected mcfx total ({mcfx_total}) < mafx total ({mafx_total})"
        )


# ===========================================================================
# MCFX — print.gcode content
# ===========================================================================

_O31_RE = re.compile(r"^O31\s+L([\d.]+)\s+mm", re.MULTILINE)
_TX_RE   = re.compile(r"^T\d+\s*(?:;.*)?\r?$", re.MULTILINE)


class TestMcfxGcode:
    def test_contains_o31_ping_commands(self, mcfx_gcode):
        matches = _O31_RE.findall(mcfx_gcode)
        assert len(matches) == 10, (
            f"expected 10 O31 commands, found {len(matches)}"
        )

    def test_o31_ping_count_matches_palette_ping_count(
        self, mcfx_gcode, mcfx_palette
    ):
        o31_count = len(_O31_RE.findall(mcfx_gcode))
        assert o31_count == mcfx_palette["pingCount"]

    def test_o31_lengths_are_positive(self, mcfx_gcode):
        for length_str in _O31_RE.findall(mcfx_gcode):
            assert float(length_str) > 0

    def test_o31_lengths_are_monotonically_increasing(self, mcfx_gcode):
        lengths = [float(m) for m in _O31_RE.findall(mcfx_gcode)]
        for i in range(len(lengths) - 1):
            assert lengths[i] < lengths[i + 1], (
                f"O31 lengths not increasing at index {i}: "
                f"{lengths[i]} >= {lengths[i+1]}"
            )

    def test_contains_bare_tx_toolchange_commands(self, mcfx_gcode):
        tx_lines = [
            l for l in mcfx_gcode.splitlines()
            if l.strip() in ("T0", "T1", "T2", "T3", "T4", "T5")
        ]
        assert len(tx_lines) == 3

    def test_strip_removes_o31_from_mcfx_gcode(self, mcfx_gcode):
        cleaned = h._strip_toolchange_lines(mcfx_gcode)
        assert _O31_RE.search(cleaned) is None, "O31 commands should be stripped"

    def test_strip_removes_tx_from_mcfx_gcode(self, mcfx_gcode):
        cleaned = h._strip_toolchange_lines(mcfx_gcode)
        bare_tx = [
            l for l in cleaned.splitlines()
            if l.strip() in ("T0", "T1", "T2", "T3", "T4", "T5")
        ]
        assert bare_tx == [], f"bare Tx lines remain after stripping: {bare_tx}"

    def test_strip_preserves_g1_moves(self, mcfx_gcode):
        """Regular G1 move lines must survive Tx/O-command stripping."""
        cleaned = h._strip_toolchange_lines(mcfx_gcode)
        g1_lines = [l for l in cleaned.splitlines() if l.startswith("G1 ")]
        assert len(g1_lines) > 1000, (
            f"expected many G1 lines after stripping, found {len(g1_lines)}"
        )

    def test_preserve_o31_keeps_o31_in_mcfx_gcode(self, mcfx_gcode):
        """--connected-accessory preserves O31 so palette3.py can intercept them."""
        cleaned = h._strip_toolchange_lines(mcfx_gcode, preserve_o31=True)
        assert _O31_RE.search(cleaned) is not None, (
            "O31 commands must be preserved with preserve_o31=True"
        )
        # All 10 O31 commands must survive
        assert len(_O31_RE.findall(cleaned)) == 10


# ===========================================================================
# Source ZIP — structure and gcode properties
# ===========================================================================

class TestSourceZip:
    def test_is_valid_zip(self):
        assert zipfile.is_zipfile(ZIP)

    def test_contains_source_gcode(self, src_zip):
        assert "201901MosaicKeychain2.gcode" in src_zip.namelist()

    def test_contains_mafx(self, src_zip):
        assert "201901MosaicKeychain2.mafx" in src_zip.namelist()

    def test_embedded_mafx_identical_to_standalone(self, src_zip):
        """The .mafx bundled in the .zip must be byte-for-byte identical to
        the standalone .mafx — they represent the same P2PP output."""
        embedded_bytes = src_zip.read("201901MosaicKeychain2.mafx")
        with open(MAFX, "rb") as fh:
            standalone_bytes = fh.read()
        assert embedded_bytes == standalone_bytes


class TestSourceGcode:
    def test_has_bare_tx_toolchange_commands(self, src_gcode):
        tx_lines = [
            l for l in src_gcode.splitlines()
            if l.strip() in ("T0", "T1", "T2", "T3", "T4", "T5")
        ]
        assert len(tx_lines) == 3

    def test_has_no_o_commands(self, src_gcode):
        """Raw PrusaSlicer output has no O-commands — these are injected by
        P2PP during post-processing for Connected/Connected-Accessory modes."""
        o_lines = [
            l for l in src_gcode.splitlines()
            if l.strip().upper().startswith("O") and l.strip()[1:2].isdigit()
        ]
        assert o_lines == [], f"unexpected O-commands in source gcode: {o_lines[:5]}"

    def test_has_g4_pause_sequences(self, src_gcode):
        """Pure Accessory Mode embeds timed G4 dwells so Palette 3 can detect
        ping positions via filament-flow sensing."""
        g4_lines = [
            l for l in src_gcode.splitlines()
            if l.strip().upper().startswith("G4")
        ]
        assert len(g4_lines) >= 10, (
            f"expected at least 10 G4 pause lines, found {len(g4_lines)}"
        )

    def test_strip_removes_tx_from_source_gcode(self, src_gcode):
        """_strip_toolchange_lines must clean the raw source gcode as well."""
        cleaned = h._strip_toolchange_lines(src_gcode)
        bare_tx = [
            l for l in cleaned.splitlines()
            if l.strip() in ("T0", "T1", "T2", "T3", "T4", "T5")
        ]
        assert bare_tx == []

    def test_strip_preserves_g4_in_source_gcode(self, src_gcode):
        """G4 pause sequences must survive Tx stripping (they are the ping
        mechanism for Pure Accessory Mode — not OctoPrint O-commands)."""
        cleaned = h._strip_toolchange_lines(src_gcode)
        g4_lines = [
            l for l in cleaned.splitlines()
            if l.strip().upper().startswith("G4")
        ]
        assert len(g4_lines) >= 10
