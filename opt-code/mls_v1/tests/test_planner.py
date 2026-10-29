import copy
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from planner import (Design, Grid, Net, Pin, Planner, SiteMap, cut_partition,
                     elmore, l1, load_config, rmst, tcl_quote, write_plan)
from compare_metrics import compare


def config(**overrides):
    cfg = load_config(Path(__file__).resolve().parents[1]/"config.json")
    cfg.update(overrides)
    return cfg


def design():
    return Design(dbu=10, bounds=(0, 0, 10000, 10000), pitch=64,
                  manufacturing_grid=1, width=10, height=10,
                  existing=[(64, 64)], obstacles=[],
                  tracks=[(d, axis, 10) for d in (0, 1)
                          for axis in ("HORIZONTAL", "VERTICAL")],
                  nets=[Net("bus[3]", 0, True,
                            [Pin("drv", "Y", (200, 200), "OUTPUT", 1.0),
                             Pin("sink", "A", (8000, 200), "INPUT", 1.0)])])


class TopologyTests(unittest.TestCase):
    def test_every_tree_cut_partitions_each_pin_once(self):
        points = [(0, 0), (20, 0), (30, 10), (0, 40), (25, 40)]
        tree = rmst(points)
        self.assertEqual(len(tree), len(points)-1)
        for root in range(len(points)):
            for edge in tree:
                source, sink = cut_partition(len(points), tree, edge, root)
                self.assertIn(root, source)
                self.assertTrue(sink)
                self.assertFalse(set(source) & set(sink))
                self.assertEqual(sorted(source+sink), list(range(len(points))))

    def test_elmore_matches_distributed_wire_and_load(self):
        delay = elmore([(0, 1, 10, 4)], {0: 0, 1: 6}, 0, 100)
        self.assertAlmostEqual(delay[1], (100*10 + 10*(6+2))*1e-6)

    def test_elmore_hbt_pi_is_counted_once(self):
        delay = elmore([(0, 1, 3, 0)], {0: 0.3, 1: 0.3}, 0, 0)
        self.assertAlmostEqual(delay[1], 3*0.3*1e-6)

    def test_elmore_rejects_cycle(self):
        with self.assertRaises(ValueError):
            elmore([(0, 1, 1, 1), (1, 2, 1, 1), (2, 0, 1, 1)], {}, 0, 1)

    def test_shared_rc_branch_sees_all_sink_loads(self):
        delay = elmore([(0, 1, 10, 0), (1, 2, 20, 0), (1, 3, 30, 0)],
                       {0: 0, 2: 2, 3: 3}, 0, 100)
        self.assertAlmostEqual(delay[2], (100*5 + 10*5 + 20*2)*1e-6)
        self.assertAlmostEqual(delay[3], (100*5 + 10*5 + 30*3)*1e-6)


class PhysicalTests(unittest.TestCase):
    def test_pitch_manufacturing_grid_and_obstacle(self):
        d = design()
        d.manufacturing_grid = 2
        d.obstacles = [(100, 100, 200, 200)]
        sites = SiteMap(d)
        self.assertFalse(sites.legal((64, 64)))
        self.assertFalse(sites.legal((65, 500)))
        self.assertFalse(sites.legal((128, 128)))
        self.assertTrue(sites.legal((128, 64)))  # exactly one pitch
        self.assertFalse(sites.legal((10000, 10000)))

    def test_nonzero_die_origin_and_hbt_lattice(self):
        d = design()
        d.bounds = (1000, 1000, 10000, 10000)
        d.existing = [(1026, 1030)]
        sites = SiteMap(d)
        for xy in sites.nearby((5000, 5000), 2, 5):
            self.assertEqual((xy[0]-1026) % d.pitch, 0)
            self.assertEqual((xy[1]-1030) % d.pitch, 0)
            self.assertTrue(sites.legal(xy))

    def test_maze_avoids_expensive_corridor(self):
        d = design()
        grid = Grid(d, config(grid_pitch_um=10, max_grid_axis=101))
        a, b = (1000, 1000), (2000, 1000)
        direct = grid.l_path(1, grid.node(a), grid.node(b))
        for edge in direct:
            grid.demand[edge] = 1000
        path, _ = grid.maze(1, a, b)
        self.assertTrue(path.isdisjoint(direct))
        self.assertTrue(all(edge[0] == 1 for edge in path))

    def test_maze_search_budget_stops(self):
        grid = Grid(design(), config(max_expansions_per_path=1))
        self.assertIsNone(grid.maze(1, (200, 200), (8000, 8000)))
        self.assertEqual(grid.expansions, 1)


class PlanningTests(unittest.TestCase):
    def test_two_hbts_and_complete_multisink_partition(self):
        d = design()
        d.nets[0].pins.append(Pin("sink2", "A", (8100, 200), "INPUT", 0.5))
        result = Planner(d, config(max_nets=1)).run()
        self.assertEqual(len(result["selected"]), 1)
        plan = result["selected"][0]
        self.assertEqual(len(plan["sites"]), 2)
        self.assertEqual(sorted(plan["source_indices"]+plan["sink_indices"]), [0, 1, 2])
        self.assertNotIn(0, plan["sink_indices"])
        self.assertEqual(len(plan["sink_pins"]), 2)
        self.assertGreater(plan["utility"], 0)

    def test_hbt_budget_and_unprofitable_fallback(self):
        self.assertEqual(Planner(design(), config(max_hbt_fraction=1e-8)).run()["selected"], [])
        result = Planner(design(), config(min_utility=1e6)).run()
        self.assertEqual(result["selected"], [])
        self.assertGreater(result["rejections"]["non_positive_utility"], 0)

    def test_no_mls_on_existing_or_top_nets(self):
        d = design()
        d.nets[0].eligible = False
        self.assertEqual(Planner(d, config()).run()["selected"], [])

    def test_site_conflicts_across_selected_nets_and_determinism(self):
        d = design()
        other = copy.deepcopy(d.nets[0])
        other.name = "other"
        d.nets.append(other)
        first = Planner(copy.deepcopy(d), config()).run()
        second = Planner(copy.deepcopy(d), config()).run()
        self.assertEqual(first, second)
        chosen = [p for proposal in first["selected"] for p in proposal["sites"]]
        self.assertEqual(len(first["selected"]), 2)
        for i, a in enumerate(chosen):
            for b in chosen[i+1:]:
                self.assertGreaterEqual(max(abs(a[0]-b[0]), abs(a[1]-b[1])), d.pitch)

    def test_negative_or_nan_configuration_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"bad.json"
            for value in ('{"max_nets": -1}', '{"grid_pitch_um": 0}',
                          '{"min_utility": NaN}', '{"unknown": 1}', '{"max_nets": 1.0}'):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    load_config(path)

    @unittest.skipUnless(shutil.which("tclsh"), "Tcl interpreter not installed")
    def test_generated_tcl_preserves_names_without_substitutions(self):
        name = 'net[3]${secret};x\\y"{字}'
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"plan.tcl"
            result = {"selected": [{"net": name, "sink_pins": [[name, "A[0]"]],
                                     "sites": [(10, 20), (30, 40)]}]}
            write_plan(result, path)
            script = ('namespace eval mlsv1 {}\n'
                      'proc mlsv1::commit {net pins args} {puts $net; puts [lindex [lindex $pins 0] 0]}\n'
                      'source '+tcl_quote(str(path))+'\n')
            run = subprocess.run([shutil.which("tclsh")], input=script,
                                 text=True, capture_output=True, check=True)
            self.assertEqual(run.stderr, "")
            self.assertEqual(run.stdout.splitlines(), [name, name])

    def test_metrics_handles_negative_slack_and_zero_baseline(self):
        a = dict(case="test", legal=True, tns_ns=-100, wns_ns=-10, drc=0)
        b = dict(case="test", legal=True, tns_ns=-90, wns_ns=-12, drc=1)
        result = compare(a, b)["metrics"]
        self.assertEqual(result["tns_ns"]["improvement_percent"], 10)
        self.assertEqual(result["wns_ns"]["improvement_percent"], -20)
        self.assertIsNone(result["drc"]["improvement_percent"])
        with self.assertRaises(ValueError):
            compare(a, dict(b, legal=False))


if __name__ == "__main__":
    unittest.main()
