import copy
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import v1, write_operations
from config import load_config
from feedback import Resources, Timing
from planner import Planner, routed_cuts
from run import Runner, assess, choose_winner
from spef import RCNet, hbt_groups, merge, merge_group, read_spef, write_net


def metrics(**changes):
    base = dict(tns_ns=-10, wns_ns=-1, hold_wns_ns=-0.1, slew_violations=0,
                capacitance_violations=0, congestion=100, wirelength_um=1000,
                hbt_count=10, access=20, conditions={"setup": "max"},
                timing_model="GRT_SPEF_passive_HBT_3ohm_0.6ff")
    return dict(base, **changes)


def timing_file(path, endpoints, paths):
    text = ["SCHEMA\t2", "MODEL\tGRT_SPEF_passive_HBT_3ohm_0.6ff"]
    for k, v in metrics(tns_ns=sum(min(0, x) for x in endpoints.values()),
                        wns_ns=min(0, *endpoints.values())).items():
        if k in {"tns_ns", "wns_ns", "hold_wns_ns", "slew_violations", "capacitance_violations"}:
            text.append(f"METRIC\t{k}\t{v}")
    for e, s in endpoints.items():
        text.append(f"ENDPOINT\t{e}\t{s}")
    for i, (e, s, branches) in enumerate(paths):
        text.append(f"PATH\t{i}\t{e}\t{s}")
        for n, sink in branches:
            text.append(f"BRANCH\t{i}\t{n}\tdrv:Y\t{sink}\t0.1")
    text += [f"SAMPLE\tpaths\t{len(paths)}", "SAMPLE\ttruncated\t1", "SAMPLE\tendpoint_path_limit\t3"]
    path.write_text("\n".join(text)+"\n")


class TimingTests(unittest.TestCase):
    def predict(self, endpoints, paths, changes):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"timing.tsv"
            timing_file(path, endpoints, paths)
            return Timing(path).predict(changes, 1.0)

    def test_common_branch_counts_distinct_endpoints(self):
        delta, rows = self.predict({"q1": -.1, "q2": -.1},
            [("q1", -.1, [("n", "s:A")]), ("q2", -.1, [("n", "s:A")])],
            {("n", "s:A"): .01})
        self.assertAlmostEqual(delta["tns"], .02)
        self.assertEqual(len(rows), 2)

    def test_competitor_takes_over_without_double_counting(self):
        delta, _ = self.predict({"q": -.1},
            [("q", -.1, [("n", "s:A")]), ("q", -.095, [("m", "other:A")])],
            {("n", "s:A"): .02})
        self.assertAlmostEqual(delta["tns"], .005)

    def test_crossing_zero_and_positive_endpoint_loss(self):
        delta, _ = self.predict({"q": -.01, "positive": .005},
            [("q", -.01, [("n", "s:A")]), ("positive", .005, [("n", "untouched:A")])],
            {("n", "s:A"): .03, ("n", "untouched:A"): -.02})
        self.assertAlmostEqual(delta["tns"], -.005)

    def test_combined_candidates_share_endpoint_and_clamp_once(self):
        delta, _ = self.predict({"q": -.015},
            [("q", -.015, [("a", "s1:A"), ("b", "s2:A")])],
            {("a", "s1:A"): .01, ("b", "s2:A"): .01})
        self.assertAlmostEqual(delta["tns"], .015)

    def test_noncritical_and_unmapped_paths_do_not_get_credit(self):
        delta, _ = self.predict({"q": -.2}, [("q", -.1, [("n", "s:A")])],
                                {("n", "s:A"): .05, ("false_path", "s:A"): 100})
        self.assertEqual(delta["tns"], 0)

    def test_stale_snapshot_or_analysis_scope_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"timing.tsv"
            timing_file(path, {"q": -.1}, [])
            path.write_text(path.read_text().replace("tns_ns\t-0.1", "tns_ns\t-0.2"))
            with self.assertRaises(ValueError):
                Timing(path)


class ParasiticsTests(unittest.TestCase):
    def nets(self):
        a = RCNet("n_BOT", {"d:Y": ("*I", "O"), "HBT_0:BOT": ("*I", "I")})
        a.caps["n_BOT:1"] = 2
        a.resistors = [("d:Y", "n_BOT:1", 10), ("n_BOT:1", "HBT_0:BOT", 1)]
        b = RCNet("n_TOP", {"HBT_0:TOP": ("*I", "O"), "s:A": ("*I", "I")})
        b.caps["n_TOP:1"] = 3
        b.resistors = [("HBT_0:TOP", "n_TOP:1", 1), ("n_TOP:1", "s:A", 20)]
        return {a.name: a, b.name: b}

    def test_hbt_rc_once_and_internal_nodes_do_not_alias(self):
        net = merge_group("n", self.nets(), [("HBT_0", "n_BOT", "n_TOP", 0, 0)])
        self.assertAlmostEqual(sum(net.caps.values()), 5.6)
        self.assertEqual(sum(r == 3 for _, _, r in net.resistors), 1)
        self.assertEqual(set(net.connections), {"d:Y", "s:A"})
        self.assertGreater(net.delays("d:Y", {"s:A": 2}, 10)["s:A"], 0)

    def test_two_hbts_in_series(self):
        nets = self.nets()
        nets["n_TOP"].connections.pop("s:A")
        nets["n_TOP"].connections["LS_HBT_1:TOP"] = ("*I", "I")
        nets["n_TOP"].resistors[-1] = ("n_TOP:1", "LS_HBT_1:TOP", 20)
        c = RCNet("n_END", {"LS_HBT_1:BOT": ("*I", "O"), "s:A": ("*I", "I")})
        c.resistors = [("LS_HBT_1:BOT", "s:A", 2)]
        nets[c.name] = c
        merged = merge_group("n", nets, [("HBT_0", "n_BOT", "n_TOP", 0, 0),
                                         ("LS_HBT_1", "n_END", "n_TOP", 100, 0)])
        self.assertAlmostEqual(sum(merged.caps.values()), 6.2)
        self.assertEqual(sum(r == 3 for _, _, r in merged.resistors), 2)

    def test_missing_hbt_connection_and_cycles_fail(self):
        nets = self.nets()
        nets["n_TOP"].connections.pop("HBT_0:TOP")
        with self.assertRaises(ValueError):
            merge_group("n", nets, [("HBT_0", "n_BOT", "n_TOP", 0, 0)])
        n = RCNet("n", {"a": ("*I", "O")}, resistors=[("a", "b", 1), ("b", "c", 1), ("c", "a", 1)])
        with self.assertRaises(ValueError):
            n.delays("a", {}, 0)

    def test_streaming_merge_and_unit_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d/"die_net_lists").mkdir()
            (d/"mls_hbts.tsv").write_text("HBT_0\tn_BOT\tn_TOP\t0\t0\n")
            for die, net in zip(("bottom", "upper"), self.nets().values()):
                (d/"die_net_lists"/(die+"_2d.txt")).write_text(net.name+"\n")
                with (d/f"mls_{die}.spef").open("w") as f:
                    f.write('*SPEF "ieee 1481-1999"\n*C_UNIT 1 FF\n*R_UNIT 1 OHM\n')
                    write_net(f, net)
            merge(d)
            merged = list(read_spef(d/"mls_merged.spef"))
            self.assertEqual(len(merged), 1)
            self.assertAlmostEqual(sum(merged[0].caps.values()), 5.6)
            self.assertIn('mlsv2::collapse "n"', (d/"mls_collapse.tcl").read_text())
            unit = d/"units.spef"
            unit.write_text("*C_UNIT 1 PF\n*R_UNIT 1 KOHM\n*D_NET n 1\n*CAP\n1 n:1 .002\n*RES\n1 d:Y n:1 .003\n*END\n")
            net = next(read_spef(unit))
            self.assertEqual(net.caps["n:1"], 2)
            self.assertEqual(net.resistors[0][2], 3)


class ResourcesTests(unittest.TestCase):
    def test_blockage_not_subtracted_twice_release_add_conserves(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            layers = {}
            for die, label, layer in ((0, "bottom", "metal2"), (1, "upper", "metal11")):
                layers[layer] = dict(level=2 if die == 0 else 11, direction="HORIZONTAL", r=1, c=1)
                (d/f"mls_{label}.resources.tsv").write_text(
                    f"GRID\t0\t0\t10\t2\t2\n{layer}\t{die}\tH\t0\t0\t2\t3\t9\t1\n")
            r = Resources(d, layers, load_config())
            e = ("metal2", 0, 0, "H")
            f = ("metal11", 0, 0, "H")
            self.assertEqual(r.summary()["overflow"], 2)
            self.assertEqual(r.delta_cost({e}, {e}), 0)
            self.assertEqual(r.delta_cost({e}, {f}), r.delta_cost({f}, {e}))
            before = copy.deepcopy(r.edges)
            r.delta_cost({e}, set())
            self.assertEqual(before, r.edges)
            self.assertLess(r.delta_cost({e}, Counter({e: 2})), 0)

    def test_internal_steiner_cut_collects_whole_subtree(self):
        net = v1.Net("n", 0, True, [v1.Pin("d", "Y", (0, 0), "OUTPUT"),
                                   v1.Pin("s1", "A", (100, 0), "INPUT"),
                                   v1.Pin("s2", "A", (100, 10), "INPUT")])
        rc = RCNet("n", resistors=[("d:Y", "n:1", 1), ("n:1", "s1:A", 1), ("n:1", "s2:A", 1)])
        self.assertIn(([0], [1, 2]), routed_cuts(net, rc))


class AcceptanceTests(unittest.TestCase):
    def test_measured_loss_rejects_prediction_and_guard_checks(self):
        cfg = load_config()
        result = assess(metrics(), metrics(tns_ns=-11, congestion=0), cfg)
        self.assertFalse(result["accepted"])
        self.assertIn("tns_guard", result["reasons"])
        for change, reason in ((dict(hold_wns_ns=-1), "hold_guard"),
                               (dict(slew_violations=1), "slew_violations_guard"),
                               (dict(capacitance_violations=1), "capacitance_violations_guard")):
            result = assess(metrics(), metrics(tns_ns=-5, **change), cfg)
            self.assertIn(reason, result["reasons"])

    def test_same_state_best_trial_and_zero_utility_rejection(self):
        cfg = load_config()
        self.assertEqual(choose_winner(metrics(), [(4, metrics(tns_ns=-9)), (7, metrics(tns_ns=-8))], cfg), 7)
        self.assertFalse(assess(metrics(), metrics(), cfg)["accepted"])

    def test_failed_and_rejected_trials_publish_baseline_unchanged(self):
        class FakeRunner(Runner):
            def provenance(self):
                pass

            def trial(self, operations, role):
                i = len(self.report["trials"])
                out = self.work/f"state{i}"
                out.mkdir()
                for name in ("5_1_grt.odb", "route.guide"):
                    (out/name).write_text("baseline" if i == 0 else "bad")
                item = dict(index=i, role=role, operations=operations, output=str(out),
                            status="failed" if i == 1 else "validated",
                            metrics=metrics(tns_ns=-10 if i == 0 else -11))
                self.report["trials"].append(item)
                return item

        p = dict(id="p", operation="insert", net="n", sink_pins=[["s", "A"]], sites=[[0, 0], [64, 0]], predicted={"tns": 5})
        q = dict(p, id="q", net="m")
        with tempfile.TemporaryDirectory() as tmp, patch("run.Planner") as planner, patch("run.compare_feedback", return_value={}):
            planner.return_value.run.return_value = dict(candidates=[p, q], hbt_limit_proxy=100)
            runner = FakeRunner(tmp, "case", "result", load_config(overrides={"max_rounds": 1, "max_trials": 2}))
            runner.run()
            final = Path(tmp)/"output/case/result"
            self.assertEqual((final/"5_1_grt.odb").read_text(), "baseline")
            self.assertEqual(runner.report["selected"], [])
            self.assertEqual(runner.report["trials"][2]["status"], "rejected")

    def test_config_rejects_typos_and_invalid_budgets(self):
        for overrides in ({"path_count": 0}, {"unknown": 1}, {"enable_regions": 1},
                          {"max_hbt_fraction": 1.1}, {"max_nets": -1}, {"tns_scale_ns": 0},
                          {"max_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                load_config(overrides=overrides)

    def test_acceptance_refreshes_feedback_and_uses_marginal_gain(self):
        class FakeRunner(Runner):
            def provenance(self):
                pass

            def trial(self, operations, role):
                i = len(self.report["trials"])
                out = self.work/f"state{i}"
                out.mkdir()
                for name in ("5_1_grt.odb", "route.guide"):
                    (out/name).write_text(str(i))
                item = dict(index=i, operations=operations, output=str(out), status="validated",
                            metrics=metrics(tns_ns=[-10, -9, -8.5][i]))
                self.report["trials"].append(item)
                return item
        def proposal(n):
            return dict(id=n, operation="insert", net=n, sink_pins=[["s", "A"]],
                        sites=[[0, 0], [64, 0]], predicted={"tns": 1})
        with tempfile.TemporaryDirectory() as tmp, patch("run.Planner") as planner, patch("run.compare_feedback", return_value={}):
            planner.return_value.run.side_effect = [dict(candidates=[proposal("a")], hbt_limit_proxy=100),
                                                    dict(candidates=[proposal("b")], hbt_limit_proxy=100)]
            runner = FakeRunner(tmp, "case", "result", load_config(overrides={"max_nets": 2, "max_trials": 2}))
            runner.run()
            self.assertEqual(planner.call_args_list[1].args[0], runner.report["trials"][1]["output"])
            self.assertEqual(runner.report["trials"][2]["assessment"]["measured"]["tns"], .5)
            self.assertEqual(len(runner.report["selected"]), 2)
            self.assertEqual((Path(tmp)/"output/case/result/5_1_grt.odb").read_text(), "2")

    def test_tcl_replay_quotes_special_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = 'bus[1]$x;{a}\\b"'
            path = Path(tmp)/"ops.tcl"
            write_operations([dict(operation="relocate", hbt=name, sites=[[1, 2]])], path)
            script = ('namespace eval mlsv2 {}\nproc mlsv2::move_hbt {name args} {puts $name}\nsource '+v1.tcl_quote(str(path))+"\n")
            result = subprocess.run(["tclsh"], input=script, text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.rstrip(), name)
            self.assertEqual(result.stderr, "")


class PlannerIntegrationTests(unittest.TestCase):
    def fixture(self, d):
        (d/"mls_design.tsv").write_text(
            "DBU\t10\nBOUNDS\t0\t0\t11000\t3000\nHBT\t64\t1\t10\t10\nEXISTING\t64\t64\n"
            "TRACK\t0\tHORIZONTAL\t10\nTRACK\t0\tVERTICAL\t10\n"
            "TRACK\t1\tHORIZONTAL\t10\nTRACK\t1\tVERTICAL\t10\nNET\tn\t0\t1\n"
            "PIN\tn\td\tY\t500\t500\tOUTPUT\t0\nPIN\tn\ts\tA\t8500\t500\tINPUT\t0\n")
        layers = [("metal2", 2, "HORIZONTAL"), ("metal3", 3, "VERTICAL"),
                  ("metal11", 11, "HORIZONTAL"), ("metal12", 12, "VERTICAL")]
        rows = [f"LAYER\t{n}\t{l}\t{direction}\t0.1\t0.1" for n, l, direction in layers]
        rows += [f"VIA\tv{i}\t1\t{i}\t{i+1}" for i in range(1, 20)]
        rows += ["PIN\tn\td:Y\t0\t10\t0.1", "PIN\tn\ts:A\t2\t0\t0.1"]
        (d/"mls_rc.tsv").write_text("\n".join(rows)+"\n")
        (d/"mls_geometry.tsv").write_text("PIN_LAYER\td:Y\t1\nPIN_LAYER\ts:A\t1\n")
        for die, label in enumerate(("bottom", "upper")):
            rows = ["GRID\t0\t0\t1000\t11\t3"]
            for name, level, direction in layers:
                if int(level > 10) != die:
                    continue
                for x in range(10 if direction == "HORIZONTAL" else 11):
                    for y in range(3 if direction == "HORIZONTAL" else 2):
                        demand = int(die == 0 and direction == "HORIZONTAL" and y == 0 and x < 8)
                        rows.append(f"{name}\t{die}\t{'H' if direction == 'HORIZONTAL' else 'V'}\t{x}\t{y}\t4\t{demand}\t0\t0")
            (d/f"mls_{label}.resources.tsv").write_text("\n".join(rows)+"\n")
            (d/f"mls_{label}.segments").write_text("n\n(\n500 500 metal2 8500 500 metal2\n)\n" if die == 0 else "")
        rc = RCNet("n", {"d:Y": ("*I", "O"), "s:A": ("*I", "I")})
        rc.caps["n:1"] = 40
        rc.resistors = [("d:Y", "n:1", 10000), ("n:1", "s:A", 10000)]
        with (d/"mls_bottom.spef").open("w") as f:
            f.write("*C_UNIT 1 FF\n*R_UNIT 1 OHM\n")
            write_net(f, rc)
        timing_file(d/"mls_timing.tsv", {"q": -1}, [("q", -1, [("n", "s:A")])])

    def test_complete_feedback_to_candidates_is_deterministic_and_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.fixture(d)
            cfg = load_config(overrides={"candidate_pairs_per_branch": 4})
            first = Planner(d, cfg).run()
            second = Planner(d, cfg).run()
            self.assertTrue(first["candidates"])
            self.assertEqual(first["candidates"], second["candidates"])
            self.assertLessEqual(len(first["candidates"]), 4)
            p = first["candidates"][0]
            self.assertEqual(p["hbt_delta"], 2)
            self.assertEqual(p["sink_pins"], [["s", "A"]])
            self.assertEqual(len(p["affected_endpoints"]), 1)
            self.assertTrue(p["predicted_top_edges"])
            self.assertGreaterEqual(v1.l1(*p["sites"]), 64)
            self.assertEqual(Planner(d, cfg).run(allow_insert=False)["candidates"], [])


if __name__ == "__main__":
    unittest.main()
