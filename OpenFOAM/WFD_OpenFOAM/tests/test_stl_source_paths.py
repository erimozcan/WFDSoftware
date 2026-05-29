#!/usr/bin/env python3
"""Tests for STL source path handling."""

from __future__ import annotations

import sys
import tempfile
import unittest
import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_case import CaseGenerationError, infer_imported_stl_scale, resolve_stl_source, write_single_region_ascii_stl  # noqa: E402


class StlSourcePathTests(unittest.TestCase):
    def write_binary_stl(self, path: Path) -> None:
        with path.open("wb") as handle:
            handle.write(b"binary test".ljust(80, b"\0"))
            handle.write(struct.pack("<I", 1))
            handle.write(
                struct.pack(
                    "<12fH",
                    0.0, 0.0, 1.0,
                    0.0, 0.0, 0.0,
                    100.0, 0.0, 0.0,
                    0.0, 100.0, 0.0,
                    0,
                )
            )

    def test_imported_batch_path_is_accepted_inside_imported_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stl = root / "imported_exports" / "test_batch" / "prop_A.stl"
            stl.parent.mkdir(parents=True)
            stl.write_text("solid prop\nendsolid prop\n")
            source, case_name, mode = resolve_stl_source(
                root,
                {
                    "variant_id": "prop_A",
                    "stl_file": "prop_A.stl",
                    "stl_source_mode": "imported_batch",
                    "imported_stl_path": str(stl),
                },
                2,
            )
        self.assertEqual(source, stl.resolve())
        self.assertEqual(case_name, "prop_A.stl")
        self.assertEqual(mode, "imported_batch")

    def test_imported_batch_rejects_external_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            stl = Path(outside) / "prop_A.stl"
            stl.write_text("solid prop\nendsolid prop\n")
            with self.assertRaises(CaseGenerationError):
                resolve_stl_source(
                    root,
                    {
                        "variant_id": "prop_A",
                        "stl_file": "prop_A.stl",
                        "stl_source_mode": "imported_batch",
                        "imported_stl_path": str(stl),
                    },
                    2,
                )

    def test_library_mode_still_accepts_prop_stls_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stl = root / "prop_stls" / "prop_A.stl"
            stl.parent.mkdir(parents=True)
            stl.write_text("solid prop\nendsolid prop\n")
            source, case_name, mode = resolve_stl_source(
                root,
                {"variant_id": "prop_A", "stl_file": "prop_A.stl", "stl_source_mode": "library"},
                2,
            )
        self.assertEqual(source, stl.resolve())
        self.assertEqual(case_name, "prop_A.stl")
        self.assertEqual(mode, "library")

    def test_imported_batch_uses_case_local_stl_name_for_nested_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stl = root / "imported_exports" / "test_batch" / "prop_A.stl"
            stl.parent.mkdir(parents=True)
            stl.write_text("solid prop\nendsolid prop\n")
            source, case_name, mode = resolve_stl_source(
                root,
                {
                    "variant_id": "batch_test_batch/prop_A",
                    "stl_file": "prop_A.stl",
                    "case_stl_file": "prop_A.stl",
                    "stl_source_mode": "imported_batch",
                    "imported_stl_path": str(stl),
                },
                2,
            )
        self.assertEqual(source, stl.resolve())
        self.assertEqual(case_name, "prop_A.stl")
        self.assertEqual(mode, "imported_batch")

    def test_imported_binary_stl_is_normalized_to_single_meter_region(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "prop_A.stl"
            target = Path(tmp) / "case_prop_A.stl"
            self.write_binary_stl(source)
            scale, metadata = infer_imported_stl_scale(source, 0.1)
            write_single_region_ascii_stl(source, target, scale=scale)

            text = target.read_text()
        self.assertEqual(scale, 0.001)
        self.assertEqual(metadata["source_stl_facet_count"], 1)
        self.assertIn("solid propeller", text)
        self.assertIn("vertex 0.1 0 0", text)
        self.assertIn("endsolid propeller", text)


if __name__ == "__main__":
    unittest.main()
