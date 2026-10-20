import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import policy_did as pd_  # noqa: E402


def _run(args):
    try:
        pd_.main(args)
    except SystemExit as e:
        if e.code is None:
            return 0
        return e.code if isinstance(e.code, int) else 1
    return 0


class TestMiniYaml(unittest.TestCase):
    def test_subset(self):
        text = """
# 주석
name: 예시
n: 3
f: 1.5
flag: true
none: null
quoted: "a: b # not comment"
list: [1, 2, 서울]
map:
  a: 1
  b:
    - x
    - y
items:
  - id: H1
    direction: positive
    horizon: [0, 3]
  - id: H2
    direction: negative
timing:
  경기: 2019
  경남: 2019
"""
        d = pd_._MiniYaml(text).parse()
        self.assertEqual(d["name"], "예시")
        self.assertEqual(d["n"], 3)
        self.assertEqual(d["f"], 1.5)
        self.assertIs(d["flag"], True)
        self.assertIsNone(d["none"])
        self.assertEqual(d["quoted"], "a: b # not comment")
        self.assertEqual(d["list"], [1, 2, "서울"])
        self.assertEqual(d["map"]["b"], ["x", "y"])
        self.assertEqual(d["items"][0]["horizon"], [0, 3])
        self.assertEqual(d["items"][1]["direction"], "negative")
        self.assertEqual(d["timing"]["경남"], 2019)

    def test_template_parses_same_with_or_without_pyyaml(self):
        text = pd_.DESIGN_TEMPLATE.format(today="2026-01-01")
        mini = pd_._MiniYaml(text).parse()
        self.assertEqual(mini["treatment"]["timing"], {"A": 2019, "B": 2019, "C": 2021})
        self.assertEqual(mini["hypotheses"][0]["horizon"], [0, 3])
        try:
            import yaml  # type: ignore
        except ImportError:
            return
        ref = yaml.safe_load(text)
        ref["registered_at"] = str(ref["registered_at"])  # PyYAML은 날짜형으로 읽는다; 도구는 문자열로 쓴다
        self.assertEqual(ref, mini)


class TestDesign(unittest.TestCase):
    def test_missing_direction_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "d.yaml"
            text = pd_.DESIGN_TEMPLATE.format(today="2026-01-01").replace("direction: positive", "direction: ")
            p.write_text(text, encoding="utf-8")
            d, errs = pd_.load_design(p)
            self.assertTrue(any("direction" in e for e in errs))

    def test_unknown_method_is_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "d.yaml"
            p.write_text(pd_.DESIGN_TEMPLATE.format(today="2026-01-01").replace("method: cs", "method: magic"), encoding="utf-8")
            _, errs = pd_.load_design(p)
            self.assertTrue(any("magic" in e for e in errs))


class TestGate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        pd_.simulate(self.root / "s", n_units=30, n_never=12)
        self.design = self.root / "s" / "design.yaml"
        self.data = self.root / "s" / "panel.csv"

    def tearDown(self):
        self.td.cleanup()

    def _gate(self, design=None, data=None, out=None):
        d, errs = pd_.load_design(design or self.design)
        self.assertEqual(errs, [])
        panel = pd_.build_panel(d, data or self.data)
        items = pd_.run_gate(d, panel)
        return d, panel, items

    def test_clean_synthetic_passes(self):
        _, _, items = self._gate()
        self.assertNotEqual(pd_.gate_status(items), "FAIL")
        g5 = [i for i in items if i.id == "G5"][0]
        self.assertEqual(g5.severity, "PASS", g5.detail)

    def test_trend_break_warns(self):
        pd_.simulate(self.root / "tb", n_units=30, n_never=12, trend_break=True)
        _, _, items = self._gate(self.root / "tb" / "design.yaml", self.root / "tb" / "panel.csv")
        g5 = [i for i in items if i.id == "G5"][0]
        self.assertEqual(g5.severity, "WARN", g5.detail)
        self.assertIn("Roth", g5.say)

    def test_staggered_twfe_fails(self):
        p = self.root / "twfe.yaml"
        p.write_text(self.design.read_text(encoding="utf-8").replace("method: cs ", "method: twfe "), encoding="utf-8")
        _, _, items = self._gate(p)
        self.assertEqual(pd_.gate_status(items), "FAIL")
        self.assertTrue(any(i.id == "G3c" for i in items))

    def test_no_comparison_fails(self):
        pd_.simulate(self.root / "nc", n_units=8, n_never=0, cohorts=(2018,))
        _, _, items = self._gate(self.root / "nc" / "design.yaml", self.root / "nc" / "panel.csv")
        self.assertEqual(pd_.gate_status(items), "FAIL")
        self.assertTrue(any(i.id == "G3" and i.severity == "FAIL" for i in items))

    def test_missing_sutva_justification_fails(self):
        p = self.root / "sutva.yaml"
        p.write_text(self.design.read_text(encoding="utf-8").replace('justification: "합성 자료 — 단위 간 상호작용 없이 생성"', 'justification: ""'), encoding="utf-8")
        _, _, items = self._gate(p)
        self.assertTrue(any(i.id == "G6" and i.severity == "FAIL" for i in items))

    def test_ghost_unit_in_timing_fails(self):
        p = self.root / "ghost.yaml"
        p.write_text(self.design.read_text(encoding="utf-8").replace("  timing:\n", "  timing:\n    GHOST: 2015\n"), encoding="utf-8")
        _, _, items = self._gate(p)
        self.assertTrue(any(i.id == "G2d" and i.severity == "FAIL" for i in items))

    def test_break_note_in_data_header_warns(self):
        data = self.root / "withnote.csv"
        data.write_text("# 시계열 단절 2024: 기준 변경\n" + self.data.read_text(encoding="utf-8"), encoding="utf-8")
        _, _, items = self._gate(data=data)
        self.assertTrue(any(i.id == "G2g" and "단절" in i.say for i in items))

    def test_few_clusters_warn_and_fail(self):
        pd_.simulate(self.root / "few", n_units=12, n_never=6, cohorts=(2016, 2019))
        _, _, items = self._gate(self.root / "few" / "design.yaml", self.root / "few" / "panel.csv")
        g4 = [i for i in items if i.id == "G4"][0]
        self.assertEqual(g4.severity, "WARN")
        pd_.simulate(self.root / "tiny", n_units=4, n_never=2, cohorts=(2018,))
        _, _, items = self._gate(self.root / "tiny" / "design.yaml", self.root / "tiny" / "panel.csv")
        self.assertTrue(any(i.id == "G4" and i.severity == "FAIL" for i in items))

    def test_estimate_refuses_without_gate(self):
        out = self.root / "out0"
        rc = _run(["estimate", str(self.design), "--data", str(self.data), "--out", str(out)])
        self.assertNotEqual(rc, 0)

    def test_estimate_refuses_after_design_change(self):
        out = self.root / "out1"
        self.assertEqual(_run(["check", str(self.design), "--data", str(self.data), "--out", str(out)]), 0)
        self.design.write_text(self.design.read_text(encoding="utf-8") + "\n# 변경\n", encoding="utf-8")
        rc = _run(["estimate", str(self.design), "--data", str(self.data), "--out", str(out)])
        self.assertNotEqual(rc, 0)

    def test_estimate_refuses_on_fail(self):
        p = self.root / "twfe2.yaml"
        p.write_text(self.design.read_text(encoding="utf-8").replace("method: cs ", "method: twfe "), encoding="utf-8")
        out = self.root / "out2"
        _run(["check", str(p), "--data", str(self.data), "--out", str(out)])
        rc = _run(["estimate", str(p), "--data", str(self.data), "--out", str(out)])
        self.assertEqual(rc, 3)
        self.assertFalse((out / "estimate.json").exists())


class TestRddGate(unittest.TestCase):
    def test_rdd_gate_and_cutoff_range(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pd_.simulate_rdd(root, effect=5)
            d, errs = pd_.load_design(root / "design.yaml")
            self.assertEqual(errs, [])
            panel = pd_.build_panel(d, root / "rdd.csv")
            items = pd_.run_gate(d, panel)
            self.assertTrue(any(i.id == "G9a" for i in items))
            bad = (root / "design.yaml").read_text(encoding="utf-8").replace("cutoff: 60", "cutoff: 160")
            (root / "bad.yaml").write_text(bad, encoding="utf-8")
            d2, _ = pd_.load_design(root / "bad.yaml")
            items2 = pd_.run_gate(d2, pd_.build_panel(d2, root / "rdd.csv"))
            self.assertTrue(any(i.id == "G9" and i.severity == "FAIL" for i in items2))


class TestPanelFromKosis(unittest.TestCase):
    def test_convert_long_export(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "long.csv"
            src.write_text(
                "# DT_X 표 | 조회 2026-10-20\n# 시계열 단절 2024: 기준 변경\n"
                "org,tbl,prd_de,itm_id,itm_nm,c1,c1_nm,unit,dt\n"
                "127,DT_X,2013,D,금액,A01,교육부,억원,100\n127,DT_X,2014,D,금액,A01,교육부,억원,110\n"
                "127,DT_X,2013,B,사업수,A01,교육부,건,3\n127,DT_X,2013,D,금액,A00,합계,억원,999\n",
                encoding="utf-8")
            out = Path(td) / "panel.csv"
            rc = _run(["panel", str(src), "--item", "금액", "--name", "exec", "--drop", "합계", "--out", str(out)])
            self.assertEqual(rc, 0)
            rows, notes, cols = pd_.read_csv_with_notes(out)
            self.assertEqual(cols, ["unit", "year", "exec"])
            self.assertEqual(len(rows), 2)
            self.assertTrue(any("단절" in n for n in notes))


@unittest.skipUnless(pd_.engine_availability().get("py:differences") or pd_.engine_availability().get("r:did"), "CS 추정기 없음")
class TestEndToEnd(unittest.TestCase):
    def test_gate_estimate_report_recovers_truth(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pd_.simulate(root, n_units=60, n_never=24)
            out = root / "out"
            self.assertEqual(_run(["run", str(root / "design.yaml"), "--data", str(root / "panel.csv"), "--out", str(out), "--quiet"]), 0)
            est = json.loads((out / "estimate.json").read_text(encoding="utf-8"))
            e0 = [p for p in est["dynamic"] if p["rel"] == 0][0]
            self.assertLess(abs(e0["att"] - 2.0), 3 * e0["se"] + 0.05, est["engine"])
            pre = [p for p in est["dynamic"] if p["rel"] <= -2]
            for p in pre:
                self.assertLess(abs(p["att"]), 3 * p["se"] + 0.05)
            rep = (out / "report.md").read_text(encoding="utf-8")
            self.assertIn("말할 수 없는 것", rep)
            self.assertIn("방향 일치", rep)
            self.assertTrue((out / "event_study.svg").exists())


if __name__ == "__main__":
    unittest.main()
