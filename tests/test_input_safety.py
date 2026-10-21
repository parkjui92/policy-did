import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import policy_did as pd


class TestInputSafety(unittest.TestCase):
    def test_nonfinite_is_missing(self):
        for value in ("inf", "-Infinity", "NAN", "1e999"):
            self.assertIsNone(pd._num(value))

    def test_multiline_hash_is_data(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.csv"
            p.write_text('# source note\nunit,year,y,note\nu,2020,1,"first\n# not a note\nlast"\n')
            rows, notes, _ = pd.read_csv_with_notes(p)
            self.assertEqual(notes, ["source note"])
            self.assertIn("# not a note", rows[0]["note"])

    def test_bad_time_and_empty_unit_fail(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td); pd.simulate(out)
            d, _ = pd.load_design(out / "design.yaml")
            for row in ("u,2020.5,1", "u,inf,1", "u,,1", ",2020,1"):
                p = out / "bad.csv"; p.write_text("unit,year,y\n" + row + "\n")
                with self.assertRaises(SystemExit): pd.build_panel(d, p)

    def test_panel_empty_filter_and_duplicates_do_not_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); source = root / "source.csv"; target = root / "panel.csv"
            source.write_text("prd_de,itm_nm,c1_nm,dt,unit\n2024,금액,A,1,억원\n2024,금액,A,2,억원\n")
            for extra in ([], ["--drop", "A"]):
                with self.assertRaisesRegex(SystemExit, "중복|남는 행"): pd.main(["panel", str(source), "--item", "금액", "--out", str(target), *extra])
                self.assertFalse(target.exists())
