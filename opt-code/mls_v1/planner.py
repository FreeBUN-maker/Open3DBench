#!/usr/bin/env python3
"""Deterministic two-HBT MLS planning. Python standard library only.

The two-die grid, RMST and Elmore values are ranking proxies, not FastRoute
segments, a physical capacity model, or signoff STA. See README.md.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import heapq
import json
import math
from pathlib import Path
import time

Point = tuple[int, int]
Edge = tuple[int, Point, Point]


def l1(a: Point, b: Point) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def rmst(points: list[Point]) -> list[tuple[int, int]]:
    """Prim's Manhattan MST: an initial topology, deliberately not an RSMT."""
    if not points:
        return []
    distances = [math.inf] * len(points)
    parent = [-1] * len(points)
    distances[0] = 0
    remaining = set(range(len(points)))
    edges = []
    while remaining:
        u = min(remaining, key=lambda i: (distances[i], i))
        remaining.remove(u)
        if parent[u] >= 0:
            edges.append((parent[u], u))
        for v in sorted(remaining):
            distance = l1(points[u], points[v])
            if distance < distances[v]:
                distances[v], parent[v] = distance, u
    return edges


def cut_partition(count: int, tree: list[tuple[int, int]], cut: tuple[int, int],
                  root: int) -> tuple[list[int], list[int]]:
    adj = defaultdict(list)
    for u, v in tree:
        if (u, v) != cut:
            adj[u].append(v)
            adj[v].append(u)
    seen, stack = {root}, [root]
    while stack:
        for v in adj[stack.pop()]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return sorted(seen), sorted(set(range(count)) - seen)


@dataclass(frozen=True)
class Pin:
    inst: str
    port: str
    xy: Point
    direction: str
    criticality: float = 0.0


@dataclass
class Net:
    name: str
    die: int
    eligible: bool
    pins: list[Pin] = field(default_factory=list)
    tree: list[tuple[int, int]] = field(default_factory=list)
    resources: set[Edge] = field(default_factory=set)

    @property
    def driver(self) -> int:
        return next(i for i, p in enumerate(self.pins) if p.direction == "OUTPUT")


@dataclass
class Design:
    dbu: int
    bounds: tuple[int, int, int, int]
    pitch: int
    manufacturing_grid: int
    width: int
    height: int
    existing: list[Point]
    obstacles: list[tuple[int, int, int, int]]
    tracks: list[tuple[int, str, int]]
    nets: list[Net]


def read_design(path: Path) -> Design:
    """Read the typed TSV emitted by prepare.tcl, retaining DBU integers."""
    meta, nets = {}, {}
    existing, obstacles, tracks = [], [], []
    with path.open() as src:
        for line in src:
            row = line.rstrip("\n").split("\t")
            tag, values = row[0], row[1:]
            if tag in {"DBU", "BOUNDS", "HBT"}:
                meta[tag] = tuple(map(int, values))
            elif tag == "EXISTING":
                existing.append(tuple(map(int, values)))
            elif tag == "OBSTACLE":
                obstacles.append(tuple(map(int, values)))
            elif tag == "TRACK":
                tracks.append((int(values[0]), values[1], int(values[2])))
            elif tag == "NET":
                name, die, eligible = values
                nets[name] = Net(name, int(die), eligible == "1")
            elif tag == "PIN":
                name, inst, port, x, y, direction, crit = values
                nets[name].pins.append(Pin(inst, port, (int(x), int(y)), direction,
                                           float(crit)))
            else:
                raise ValueError(f"Unknown design record: {tag}")
    pitch, grid, width, height = meta["HBT"]
    if not existing or min(pitch, grid, width, height) <= 0:
        raise ValueError("A valid HBT lattice and existing HBTs are required")
    return Design(meta["DBU"][0], meta["BOUNDS"], pitch, grid, width, height,
                  existing, obstacles, tracks, list(nets.values()))


class SiteMap:
    """Conservative square spacing plus macro bounds, using spatial hashing."""
    def __init__(self, design: Design):
        self.d = design
        self.origin = tuple(v % design.pitch for v in design.existing[0])
        self.buckets = defaultdict(list)
        for point in design.existing:
            self.reserve(point)
        self.capacity = 0
        # Capacity counts lattice positions inside the die, like the count proxy.
        lx, ly, ux, uy = design.bounds
        self.ix0 = math.ceil((lx - self.origin[0]) / design.pitch)
        self.iy0 = math.ceil((ly - self.origin[1]) / design.pitch)
        self.ix1 = (ux - design.width - self.origin[0]) // design.pitch
        self.iy1 = (uy - design.height - self.origin[1]) // design.pitch
        self.capacity = max(0, self.ix1-self.ix0+1) * max(0, self.iy1-self.iy0+1)

    def reserve(self, xy: Point):
        self.buckets[xy[0] // self.d.pitch, xy[1] // self.d.pitch].append(xy)

    def conflict(self, a: Point, b: Point) -> bool:
        # More conservative than the PDF's Euclidean center-distance filter.
        return max(abs(a[0]-b[0]), abs(a[1]-b[1])) < self.d.pitch

    def legal(self, xy: Point) -> bool:
        x, y = xy
        d = self.d
        if x % d.manufacturing_grid or y % d.manufacturing_grid:
            return False
        lx, ly, ux, uy = d.bounds
        if x < lx or y < ly or x+d.width > ux or y+d.height > uy:
            return False
        if any(x < b[2] and x+d.width > b[0] and
               y < b[3] and y+d.height > b[1] for b in d.obstacles):
            return False
        bx, by = x // d.pitch, y // d.pitch
        return not any(self.conflict(xy, p) for dx in (-1, 0, 1)
                       for dy in (-1, 0, 1)
                       for p in self.buckets[bx+dx, by+dy])

    def nearby(self, xy: Point, radius: int, limit: int) -> list[Point]:
        ix, iy = (round((xy[i]-self.origin[i])/self.d.pitch) for i in (0, 1))
        points = [(self.origin[0]+a*self.d.pitch, self.origin[1]+b*self.d.pitch)
                  for a in range(ix-radius, ix+radius+1)
                  for b in range(iy-radius, iy+radius+1)]
        return sorted((p for p in points if self.legal(p)),
                      key=lambda p: (l1(p, xy), p))[:limit]


class Grid:
    """Two aggregated die planes. Per-layer routing is delegated to FastRoute."""
    def __init__(self, d: Design, cfg: dict):
        self.d, self.cfg = d, cfg
        lx, ly, ux, uy = d.bounds
        self.step = max(round(cfg["grid_pitch_um"]*d.dbu),
                        math.ceil(max(ux-lx, uy-ly)/(cfg["max_grid_axis"]-1)))
        self.nx = math.ceil((ux-lx)/self.step)+1
        self.ny = math.ceil((uy-ly)/self.step)+1
        self.demand = Counter()
        self.caps = Counter()
        for die, direction, pitch in d.tracks:
            axis = 0 if direction == "HORIZONTAL" else 1
            self.caps[die, axis] += self.step/max(1, pitch)*cfg["capacity_adjustment"]
        if any(self.caps[die, axis] <= 0 for die in (0, 1) for axis in (0, 1)):
            raise ValueError("Missing preferred-direction track capacity for a die")
        self.expansions = 0

    def node(self, xy: Point) -> Point:
        return (max(0, min(self.nx-1, round((xy[0]-self.d.bounds[0])/self.step))),
                max(0, min(self.ny-1, round((xy[1]-self.d.bounds[1])/self.step))))

    @staticmethod
    def edge(die: int, a: Point, b: Point) -> Edge:
        return (die, min(a, b), max(a, b))

    def capacity(self, edge: Edge) -> float:
        die, a, b = edge
        return self.caps[die, 0 if a[0] != b[0] else 1]

    def potential(self, edge: Edge, demand: int) -> float:
        rho = max(0, demand)/self.capacity(edge)
        return rho*rho + 8*max(0, rho-1)**2

    def l_path(self, die: int, a: Point, b: Point) -> set[Edge]:
        def make(horizontal_first):
            point, edges = a, set()
            for axis in ((0, 1) if horizontal_first else (1, 0)):
                while point[axis] != b[axis]:
                    nxt = list(point)
                    nxt[axis] += 1 if point[axis] < b[axis] else -1
                    nxt = tuple(nxt)
                    edges.add(self.edge(die, point, nxt))
                    point = nxt
            return edges
        choices = [make(True), make(False)]
        return min(choices, key=lambda edges: sum(
            self.potential(e, self.demand[e]+1) for e in sorted(edges)))

    def tree_resources(self, die: int, points: list[Point], tree) -> set[Edge]:
        resources = set()
        for u, v in tree:
            resources.update(self.l_path(die, self.node(points[u]), self.node(points[v])))
        return resources

    def maze(self, die: int, a: Point, b: Point) -> tuple[set[Edge], int] | None:
        start, goal = self.node(a), self.node(b)
        margin = self.cfg["search_margin"]
        xmin, xmax = max(0, min(start[0], goal[0])-margin), min(self.nx-1, max(start[0], goal[0])+margin)
        ymin, ymax = max(0, min(start[1], goal[1])-margin), min(self.ny-1, max(start[1], goal[1])+margin)
        queue, costs, parent = [(l1(start, goal), 0.0, start)], {start: 0.0}, {}
        expanded = 0
        while queue:
            _, cost, u = heapq.heappop(queue)
            if cost != costs[u]:
                continue
            if u == goal:
                edges = set()
                while u != start:
                    v = parent[u]
                    edges.add(self.edge(die, u, v))
                    u = v
                return edges, expanded
            if (expanded >= self.cfg["max_expansions_per_path"] or
                    self.expansions >= self.cfg["max_total_expansions"]):
                return None
            expanded += 1
            self.expansions += 1
            for v in ((u[0]-1, u[1]), (u[0]+1, u[1]), (u[0], u[1]-1), (u[0], u[1]+1)):
                if not (xmin <= v[0] <= xmax and ymin <= v[1] <= ymax):
                    continue
                e = self.edge(die, u, v)
                congestion = self.potential(e, self.demand[e]+1)-self.potential(e, self.demand[e])
                new = cost+1+self.cfg["maze_congestion_weight"]*congestion
                if new < costs.get(v, math.inf):
                    costs[v], parent[v] = new, u
                    heapq.heappush(queue, (new+l1(v, goal), new, v))
        return None


def elmore(delays_tree, loads: dict[int, float], root: int, driver_r: float) -> dict[int, float]:
    """Tree RC first moment in ns. Edge tuple: u,v,R(ohm),C(ff), distributed.

    Wire pi capacitances are split between endpoints. HBT pi capacitances are
    passed through loads so the 3-ohm/0.6-ff model is counted exactly once.
    """
    adj = defaultdict(list)
    caps = defaultdict(float, loads)
    for u, v, r, c in delays_tree:
        adj[u].append((v, r))
        adj[v].append((u, r))
        caps[u] += c/2
        caps[v] += c/2
    parent, order = {root: None}, [root]
    for u in order:
        for v, _ in adj[u]:
            if v == parent[u]:
                continue
            if v in parent:
                raise ValueError("RC topology must be a tree")
            parent[v] = u
            order.append(v)
    if len(order) != len(caps):
        raise ValueError("Disconnected RC tree")
    downstream = dict(caps)
    for u in reversed(order[1:]):
        downstream[parent[u]] += downstream[u]
    delays = {root: driver_r*downstream[root]*1e-6}
    for u in order:
        for v, r in adj[u]:
            if parent.get(v) == u:
                delays[v] = delays[u]+r*downstream[v]*1e-6
    return delays


class Planner:
    def __init__(self, design: Design, cfg: dict):
        self.d, self.cfg = design, cfg
        self.sites, self.grid = SiteMap(design), Grid(design, cfg)
        self.rejections = Counter()
        for net in sorted(design.nets, key=lambda n: n.name):
            points = [p.xy for p in net.pins]
            # Bound background high-fanout topology construction; never use it
            # as a candidate tree. High-fanout demand remains an approximation.
            net.tree = (rmst(points) if len(points) <= cfg["max_fanout"]+1 else
                        list(zip(range(len(points)-1), range(1, len(points)))))
            net.resources = self.grid.tree_resources(net.die, points, net.tree)
            self.grid.demand.update(net.resources)

    def rc_edges(self, indices, points, tree, die):
        prefix = "bottom" if die == 0 else "top"
        r, c = self.cfg[prefix+"_r_ohm_per_um"], self.cfg[prefix+"_c_ff_per_um"]
        return [(indices[u], indices[v], l1(points[u], points[v])/self.d.dbu*r,
                 l1(points[u], points[v])/self.d.dbu*c) for u, v in tree]

    def timing(self, net: Net, rc_tree, extra_loads=None) -> float:
        loads = {i: (self.cfg["sink_cap_ff"] if p.direction != "OUTPUT" else 0.0)
                 for i, p in enumerate(net.pins)}
        for i, cap in (extra_loads or {}).items():
            loads[i] = loads.get(i, 0.0)+cap
        delays = elmore(rc_tree, loads, net.driver, self.cfg["driver_r_ohm"])
        return max((delays[i]*(0.1+min(1, p.criticality)) for i, p in enumerate(net.pins)
                    if p.direction == "INPUT"), default=0.0)

    def plan_net(self, net: Net) -> dict | None:
        cfg, grid = self.cfg, self.grid
        points = [p.xy for p in net.pins]
        old_length = sum(l1(points[u], points[v]) for u, v in net.tree)/self.d.dbu
        old_rc = self.rc_edges(list(range(len(points))), points, net.tree, 0)
        old_delay = self.timing(net, old_rc)
        # Removal/addition uses sets: a net consumes a shared coarse edge once.
        old_cost = sum(grid.potential(e, grid.demand[e])-grid.potential(e, grid.demand[e]-1)
                       for e in sorted(net.resources))
        cuts = []
        for edge in net.tree:
            source, sink = cut_partition(len(points), net.tree, edge, net.driver)
            if not source or not sink:
                continue
            u, v = edge if edge[0] in source else edge[::-1]
            span = l1(points[u], points[v])/self.d.dbu
            crit = max(net.pins[i].criticality for i in sink)
            if span >= cfg["min_span_um"]:
                cuts.append((-(1+crit)*span, u, v, source, sink))
        best = None
        for _, u, v, source, sink in sorted(cuts)[:cfg["branches_per_net"]]:
            a_sites = self.sites.nearby(points[u], cfg["site_radius"], cfg["sites_per_endpoint"])
            b_sites = self.sites.nearby(points[v], cfg["site_radius"], cfg["sites_per_endpoint"])
            for a in a_sites:
                for b in b_sites:
                    if self.sites.conflict(a, b):
                        continue
                    routed = grid.maze(1, a, b)
                    if routed is None:
                        self.rejections["search_budget_or_unreachable"] += 1
                        continue
                    top_edges, _ = routed
                    # Grid nodes are ranking locations. Preserve the geometric
                    # lower bound and include off-grid access to the grid path.
                    def grid_xy(p):
                        n = grid.node(p)
                        return (self.d.bounds[0]+n[0]*grid.step, self.d.bounds[1]+n[1]*grid.step)
                    top_length = max(l1(a, b), len(top_edges)*grid.step +
                                     l1(a, grid_xy(a))+l1(b, grid_xy(b)))/self.d.dbu
                    resources = set(top_edges)
                    rc_tree, length = [], top_length
                    h1, h2, t1, t2 = range(len(points), len(points)+4)
                    for group, xy, h in ((source, a, h1), (sink, b, h2)):
                        local_points = [points[i] for i in group]+[xy]
                        local_tree = rmst(local_points)
                        length += sum(l1(local_points[i], local_points[j]) for i, j in local_tree)/self.d.dbu
                        resources.update(grid.tree_resources(0, local_points, local_tree))
                        rc_tree.extend(self.rc_edges(group+[h], local_points, local_tree, 0))
                    # Two HBTs, each with R=3 ohm and total C=0.6 ff.
                    # Lump via-stack access on these edges as a separate proxy.
                    access_r, access_c = cfg["access_r_ohm"], cfg["access_c_ff"]
                    rc_tree += [(h1, t1, 3.0+access_r, 0.0),
                                (t1, t2, top_length*cfg["top_r_ohm_per_um"],
                                 top_length*cfg["top_c_ff_per_um"]),
                                (t2, h2, 3.0+access_r, 0.0)]
                    new_delay = self.timing(net, rc_tree, {i: 0.3+access_c/2 for i in (h1, h2, t1, t2)})
                    delta = Counter(resources)
                    delta.subtract(net.resources)
                    potential_delta = sum(grid.potential(e, grid.demand[e]+change)-
                                          grid.potential(e, grid.demand[e])
                                          for e, change in sorted(delta.items()) if change)
                    timing_gain = (old_delay-new_delay)/max(old_delay, 1e-9)
                    cong_gain = -potential_delta/max(old_cost, 1e-9)
                    delta_wl = (length-old_length)/max(old_length, 1e-9)
                    budget_cost = 2/max(1, math.floor(self.sites.capacity*cfg["max_hbt_fraction"]))
                    utility = (cfg["timing_weight"]*timing_gain + cfg["congestion_weight"]*cong_gain -
                               cfg["wirelength_weight"]*delta_wl - cfg["hbt_weight"]*budget_cost)
                    proposal = dict(net=net.name, utility=utility, source_indices=source,
                                    sink_indices=sink, sink_pins=[[net.pins[i].inst, net.pins[i].port] for i in sink],
                                    sites=[a, b], old_length_um=old_length, new_length_um=length,
                                    old_delay_proxy_ns=old_delay, new_delay_proxy_ns=new_delay,
                                    congestion_gain_relative=cong_gain, timing_gain_relative=timing_gain,
                                    resources=resources, delta=delta,
                                    top_corridor_edges=[[list(e[1]), list(e[2])] for e in sorted(top_edges)])
                    if best is None or utility > best["utility"]:
                        best = proposal
        return best

    def run(self) -> dict:
        candidates = []
        for net in self.d.nets:
            if not net.eligible or not (2 <= len(net.pins) <= self.cfg["max_fanout"]+1):
                continue
            if sum(p.direction == "OUTPUT" for p in net.pins) != 1:
                continue
            span = max(p.xy[0] for p in net.pins)-min(p.xy[0] for p in net.pins)
            span += max(p.xy[1] for p in net.pins)-min(p.xy[1] for p in net.pins)
            if span/self.d.dbu < self.cfg["min_span_um"]:
                continue
            pressure = sum(self.grid.potential(e, self.grid.demand[e]) for e in sorted(net.resources))/max(1, len(net.resources))
            priority = (1+max(p.criticality for p in net.pins))*(1+pressure)*span/self.d.dbu
            candidates.append((-priority, net.name, net))
        ranked = sorted(candidates)[:self.cfg["top_k"]]
        accepted, examined = [], []
        limit = math.floor(self.sites.capacity*self.cfg["max_hbt_fraction"])
        for _, _, net in ranked:
            if len(accepted) >= self.cfg["max_nets"]:
                break
            if len(self.d.existing)+2*(len(accepted)+1) > limit:
                self.rejections["hbt_budget"] += 1
                break
            if self.grid.expansions >= self.cfg["max_total_expansions"]:
                self.rejections["total_search_budget"] += 1
                break
            plan = self.plan_net(net)
            if plan is None:
                self.rejections["no_legal_pair"] += 1
                continue
            examined.append({k: v for k, v in plan.items() if k not in {"resources", "delta"}})
            if plan["utility"] <= self.cfg["min_utility"]:
                self.rejections["non_positive_utility"] += 1
                continue
            for p in plan["sites"]:
                self.sites.reserve(p)
            self.grid.demand.update(plan["delta"])
            accepted.append(examined[-1])
        return dict(schema_version=1, model="coarse_two_die_RMST_Elmore_proxy",
                    config=self.cfg, eligible_nets=len(candidates), considered=len(examined),
                    grid=dict(step_dbu=self.grid.step, nx=self.grid.nx, ny=self.grid.ny,
                              expansions=self.grid.expansions),
                    hbt_capacity_proxy=self.sites.capacity, hbt_limit_proxy=limit,
                    existing_hbts=len(self.d.existing), selected=accepted,
                    candidates=examined, rejections=dict(self.rejections))


def tcl_quote(value: str) -> str:
    # Double-quoted Tcl word: disable every substitution, including net bus [].
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    escaped = escaped.replace("[", "\\[").replace("]", "\\]")
    return '"'+escaped.replace("\n", "\\n").replace("\r", "\\r")+'"'


def write_plan(result: dict, path: Path):
    with path.open("w") as out:
        out.write("# Generated by planner.py; no ODB mutations occur during search.\n")
        for p in result["selected"]:
            pins = " ".join("[list "+tcl_quote(inst)+" "+tcl_quote(port)+"]" for inst, port in p["sink_pins"])
            xy = " ".join(str(c) for point in p["sites"] for c in point)
            out.write(f'mlsv1::commit {tcl_quote(p["net"])} [list {pins}] {xy}\n')


def load_config(path: Path, overrides=()) -> dict:
    defaults = json.loads(Path(__file__).with_name("config.json").read_text())
    supplied = json.loads(path.read_text())
    if unknown := supplied.keys()-defaults.keys():
        raise ValueError(f"Unknown options: {sorted(unknown)}")
    defaults.update(supplied)
    for key, raw in overrides:
        defaults[key] = raw
    integer_min = dict(max_nets=0, top_k=0, branches_per_net=1, max_fanout=1,
                       max_grid_axis=2, site_radius=0, sites_per_endpoint=1, search_margin=0,
                       max_expansions_per_path=1, max_total_expansions=1)
    for key, value in defaults.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number")
        if key in integer_min and (not isinstance(value, int) or value < integer_min[key]):
            raise ValueError(f"Invalid integer option {key}: {value}")
        if key != "min_utility" and value < 0:
            raise ValueError(f"{key} must be non-negative")
    if not 0 < defaults["capacity_adjustment"] <= 1 or not 0 < defaults["max_hbt_fraction"] <= 1:
        raise ValueError("Capacity adjustment and HBT fraction must be in (0,1]")
    if defaults["grid_pitch_um"] <= 0:
        raise ValueError("grid_pitch_um must be positive")
    return defaults


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--design", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-nets", type=int)
    args = parser.parse_args()
    overrides = [] if args.max_nets is None else [("max_nets", args.max_nets)]
    started = time.monotonic()
    cfg = load_config(args.config, overrides)
    result = Planner(read_design(args.design), cfg).run()
    result["planner_runtime_seconds"] = time.monotonic()-started
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output/"mls_plan.json").write_text(json.dumps(result, indent=2, ensure_ascii=False)+"\n")
    write_plan(result, args.output/"mls_plan.tcl")
    print(f'MLSv1: eligible={result["eligible_nets"]} selected={len(result["selected"])} '
          f'expansions={result["grid"]["expansions"]} seconds={result["planner_runtime_seconds"]:.3f}')


if __name__ == "__main__":
    main()
