#!/usr/bin/env python3
"""Bounded MLS candidate generation using current GRT and endpoint feedback.

This module proposes candidates; only the runner can accept a routed candidate.
"""
from collections import Counter, defaultdict
import hashlib
import itertools
import json
import math
from pathlib import Path
import time

from common import v1
from config import utility
from feedback import Resources, Timing, read_rc, read_segments
from spef import hbt_groups, read_spef


def signature(proposal):
    keys = ("operation", "net", "hbt", "sink_pins", "sites")
    raw = json.dumps({k: proposal[k] for k in keys if k in proposal}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def routed_cuts(net, rc):
    """Use internal Steiner nodes of a verified native RC tree, if requested."""
    adj = defaultdict(set)
    for a, b, _ in rc.resistors:
        if b in adj[a]:
            raise ValueError("Duplicate routed RC edge")
        adj[a].add(b)
        adj[b].add(a)
    names = [p.inst+":"+p.port for p in net.pins]
    root = names[net.driver]
    parent, order = {root: None}, [root]
    for a in order:
        for b in sorted(adj[a]):
            if b == parent[a]:
                continue
            if b in parent:
                raise ValueError("Routed RC graph has a cycle")
            parent[b] = a
            order.append(b)
    if len(order) != len(adj) or not set(names) <= parent.keys():
        raise ValueError("Disconnected routed RC graph")
    below = {n: ({names.index(n)} if n in names else set()) for n in order}
    cuts = set()
    for n in reversed(order[1:]):
        if below[n] and len(below[n]) < len(names):
            cuts.add(tuple(sorted(below[n])))
        below[parent[n]].update(below[n])
    return [(sorted(set(range(len(names)))-set(sink)), list(sink)) for sink in sorted(cuts)]


class Planner:
    def __init__(self, directory, cfg):
        self.directory, self.cfg = Path(directory), cfg
        total = sum(p.stat().st_size for p in self.directory.glob("mls_*" ) if p.is_file())
        if total > cfg["max_feedback_mb"]*1024**2:
            raise ValueError("Feedback exceeds max_feedback_mb; raise the explicit memory/input budget")
        self.d = v1.read_design(self.directory/"mls_design.tsv")
        self.layers, self.pins, self.vias = read_rc(self.directory/"mls_rc.tsv")
        self.timing = Timing(self.directory/"mls_timing.tsv")
        self.resources = Resources(self.directory, self.layers, cfg)
        self.segments = {}
        for die in ("bottom", "upper"):
            self.segments.update(read_segments(self.directory/f"mls_{die}.segments"))
        self.pin_layers, self.ndr = defaultdict(set), set()
        with (self.directory/"mls_geometry.tsv").open() as src:
            for line in src:
                tag, *v = line.rstrip().split("\t")
                if tag == "OBS" and v[0] in {"metal10", "metal11"}:
                    self.d.obstacles.append(tuple(map(int, v[1:])))
                elif tag == "PIN_LAYER":
                    self.pin_layers[v[0]].add(int(v[1]))
                elif tag == "NDR":
                    self.ndr.add(v[0])
        self.sites = v1.SiteMap(self.d)
        self.rejections = Counter()
        self.started = time.monotonic()
        self.cache = {}

    def expired(self):
        return (time.monotonic()-self.started >= self.cfg["planner_seconds"] or
                self.resources.expansions >= self.cfg["max_total_expansions"])

    def region_sites(self, points, anchor, hotspots=()):
        cfg = self.cfg
        nearest = self.sites.nearby(anchor, cfg["site_radius"], min(3, cfg["sites_per_region"]))
        if not cfg["enable_regions"]:
            return nearest
        # The median rectangle minimizes sum of Manhattan access distances to
        # the entire partition. Hotspot boundaries supply additional anchors.
        xs, ys = sorted(p[0] for p in points), sorted(p[1] for p in points)
        mids_x = {xs[(len(xs)-1)//2], xs[len(xs)//2]}
        mids_y = {ys[(len(ys)-1)//2], ys[len(ys)//2]}
        anchors = [anchor, *itertools.product(mids_x, mids_y), *hotspots]
        pool = set(nearest)
        for radius in sorted({cfg["site_radius"], cfg["max_site_radius"]}):
            for xy in anchors:
                pool.update(self.sites.nearby(xy, radius, cfg["sites_per_region"]))
            if len(pool) >= cfg["sites_per_region"]:
                break
        ranked = sorted(pool, key=lambda p: (
            sum(v1.l1(p, x) for x in points)/self.d.dbu +
            self.d.pitch/self.d.dbu * (self.resources.local_pressure(p, 0)+
                                      self.resources.local_pressure(p, 1)), p))
        # Preserve v1 nearest-site coverage while expanding the useful region.
        return (nearest + [p for p in ranked if p not in nearest])[:cfg["sites_per_region"]]

    def route(self, a, b, die, old):
        key = a, b, die
        if key not in self.cache:
            self.cache[key] = self.resources.route(a, b, die, old)
        return self.cache[key]

    def wire_rc(self, a, b, die, old):
        edges = self.route(a, b, die, old)
        if edges is None:
            raise ValueError("path_search_budget_or_unreachable")
        step = self.resources.grids[die][2]/self.d.dbu
        length = len(edges)*step
        r = sum(self.layers[e[0]]["r"]*step for e in edges)
        c = sum(self.layers[e[0]]["c"]*step for e in edges)
        access = (v1.l1(a, self.resources.xy(self.resources.node(a, die), die)) +
                  v1.l1(b, self.resources.xy(self.resources.node(b, die), die)))/self.d.dbu
        # Off-grid access is counted explicitly; not charged to wire capacity.
        layer_names = {e[0] for e in edges}
        if not layer_names:
            layer_names = set(self.resources.layer_options[die, "H"]+
                              self.resources.layer_options[die, "V"])
        if not layer_names:
            raise ValueError("no_routing_layer")
        r += access*max(self.layers[l]["r"] for l in layer_names)
        c += access*max(self.layers[l]["c"] for l in layer_names)
        return r, c, length+access, edges

    def access_resistance(self, levels, target):
        # LEF cut resistance is in ohms. HBT is excluded and added once below.
        if not levels:
            raise ValueError("missing_pin_layer")
        source = min(levels, key=lambda level: abs(level-target))
        links = [(i, i+1) for i in range(min(source, target), max(source, target))]
        if any(link not in self.vias for link in links):
            raise ValueError("missing_access_via_rc")
        return sum(self.vias[link] for link in links)

    def predict_pair(self, net, source, sink, a, b, old, old_delays):
        points = [p.xy for p in net.pins]
        count = len(points)
        h1, h2, t1, t2 = range(count, count+4)
        rc, new, length, access_r = [], Counter(), 0.0, 0.0
        for group, site, h in ((source, a, h1), (sink, b, h2)):
            local = [points[i] for i in group]+[site]
            ids = group+[h]
            subnet_resources = set()
            for u, v in v1.rmst(local):
                r, c, wire_length, edges = self.wire_rc(local[u], local[v], 0, old)
                rc.append((ids[u], ids[v], r, c))
                subnet_resources.update(edges)
                length += wire_length
            # Shared wire is charged once within a subnet, separately across
            # the two bottom subnets if they happen to reuse the same edge.
            new.update(subnet_resources)
        r, c, wire_length, edges = self.wire_rc(a, b, 1, old)
        rc.append((t1, t2, r, c))
        new.update(edges)
        length += wire_length
        # Access stacks are separate from each HBT's 3-ohm series resistance.
        for group, h, t in ((source, h1, t1), (sink, h2, t2)):
            levels = {level for i in group for level in self.pin_layers[net.pins[i].inst+":"+net.pins[i].port]}
            ar = self.access_resistance(levels, 10)
            top_levels = {self.layers[e[0]]["level"] for e in edges} or {11}
            ar += self.access_resistance(top_levels, 11)
            access_r += ar
            rc.append((h, t, 3+ar, 0))
        names = [p.inst+":"+p.port for p in net.pins]
        loads = {i: self.pins[n]["cap"] if i != net.driver else 0 for i, n in enumerate(names)}
        loads.update({i: 0.3 for i in (h1, h2, t1, t2)})
        delays = v1.elmore(rc, loads, net.driver, self.pins[names[net.driver]]["resistance"])
        changes = {(net.name, n): old_delays[n]-delays[i]
                   for i, n in enumerate(names) if i != net.driver}
        delta, endpoints = self.timing.predict(changes, self.cfg["unsampled_path_guard_ns"])
        old_length = sum(v1.l1(a, b) for a, b in self.segments[net.name] if a[2] == b[2])/self.d.dbu
        pressure = sum(self.resources.local_pressure(p, die) for p in (a, b) for die in (0, 1))
        delta.update(congestion=self.resources.delta_cost(old, new),
                     wirelength=old_length-length, hbt=-2, access=-pressure)
        proposal = dict(operation="insert", net=net.name, source_indices=source, sink_indices=sink,
                        sink_pins=[[net.pins[i].inst, net.pins[i].port] for i in sink],
                        sites=[list(a), list(b)], hbt_delta=2,
                        predicted=delta, utility=utility(delta, self.cfg),
                        affected_endpoints=endpoints, sink_delay_gain_ns={n: v for (_, n), v in changes.items()},
                        access_resistance_ohm=access_r,
                        old_route_length_um=old_length, predicted_route_length_um=length,
                        predicted_top_edges=[list(e) for e in sorted(edges)],
                        model="native_GRT_SPEF_old_vs_layer_RC_RMST_new; Liberty_drive_R_and_pin_C",
                        deduplication="minimum sampled competing-path slack per distinct endpoint",
                        approximation="unseen paths, slew propagation, via access and proposed topology; verified by full STA")
        proposal["id"] = signature(proposal)
        return proposal

    def net_candidates(self, net, rc):
        names = [p.inst+":"+p.port for p in net.pins]
        if not set(names) <= self.pins.keys():
            self.rejections["missing_liberty_pin_rc"] += 1
            return []
        old = self.resources.routed_edges(self.segments.get(net.name, []))
        loads = {n: self.pins[n]["cap"] for i, n in enumerate(names) if i != net.driver}
        try:
            old_delays = rc.delays(names[net.driver], loads, self.pins[names[net.driver]]["resistance"])
        except (ValueError, KeyError):
            self.rejections["old_rc_not_a_connected_tree"] += 1
            return []
        points = [p.xy for p in net.pins]
        tree = v1.rmst(points)
        cuts = [v1.cut_partition(len(points), tree, e, net.driver) for e in tree]
        if self.cfg["enable_route_tree_cuts"]:
            try:
                cuts.extend(routed_cuts(net, rc))
            except ValueError:
                self.rejections["route_tree_cut_unavailable"] += 1
        cuts = {tuple(t): (s, t) for s, t in cuts}
        def rank(partition):
            s, t = partition
            span = min(v1.l1(points[i], points[j]) for i in s for j in t)
            endpoints = {self.timing.paths[pid]["endpoint"] for pid in self.timing.by_net[net.name]
                         if any(n == net.name and sink in {names[i] for i in t}
                                for n, _, sink, _ in self.timing.paths[pid]["branches"])}
            return -(1+len(endpoints))*span, tuple(t)
        hotspot_points = sorted({a[:2] for seg in self.segments.get(net.name, []) for a in seg},
                                key=lambda p: (-self.resources.local_pressure(p, 0), p))[:2]
        proposals = []
        self.cache = {}  # released resources differ for each original net
        for source, sink in sorted(cuts.values(), key=rank)[:self.cfg["branches_per_net"]]:
            i, j = min(itertools.product(source, sink), key=lambda ij: (v1.l1(points[ij[0]], points[ij[1]]), ij))
            if v1.l1(points[i], points[j])/self.d.dbu < self.cfg["min_span_um"]:
                continue
            aa = self.region_sites([points[k] for k in source], points[i], hotspot_points)
            bb = self.region_sites([points[k] for k in sink], points[j], hotspot_points)
            pairs = sorted(itertools.product(aa, bb), key=lambda ab: (
                sum(v1.l1(points[k], ab[0]) for k in source)+sum(v1.l1(points[k], ab[1]) for k in sink)+v1.l1(*ab), ab))
            nearest_pairs = list(itertools.product(aa[:3], bb[:3]))
            pairs = nearest_pairs+[pair for pair in pairs if pair not in nearest_pairs]
            for a, b in pairs[:self.cfg["candidate_pairs_per_branch"]]:
                if self.expired():
                    self.rejections["planner_budget"] += 1
                    return proposals
                if self.sites.conflict(a, b):
                    self.rejections["pair_conflict"] += 1
                    continue
                try:
                    proposals.append(self.predict_pair(net, source, sink, a, b, old, old_delays))
                except ValueError as e:
                    self.rejections[str(e)] += 1
        return proposals

    def relocations(self):
        candidates = []
        for base, hs in hbt_groups(self.directory/"mls_hbts.tsv").items():
            if len(hs) != 1 or hs[0][0].startswith("LS_HBT_"):
                continue
            hbt, bot, top, x, y = hs[0]
            priority = len({self.timing.paths[pid]["endpoint"] for pid in self.timing.by_net[base]})
            candidates.append((-priority, base, hbt, bot, top, (x, y)))
        proposals = []
        for _, base, hbt, bot, top, old in sorted(candidates)[:self.cfg["top_k"]]:
            bucket = self.sites.buckets[old[0]//self.d.pitch, old[1]//self.d.pitch]
            bucket.remove(old)
            try:
                pts = [p[:2] for n in (bot, top) for seg in self.segments.get(n, []) for p in seg]
                sites = self.region_sites(pts or [old], old)
                for xy in sites:
                    if xy == old:
                        continue
                    gain = sum(v1.l1(old, p)-v1.l1(xy, p) for p in pts)/self.d.dbu
                    delta = dict(tns=0, wns=0, congestion=0, wirelength=gain, hbt=0,
                                 access=sum(self.resources.local_pressure(old, d)-self.resources.local_pressure(xy, d) for d in (0, 1)))
                    p = dict(operation="relocate", net=base, hbt=hbt, sites=[list(xy)],
                             old_site=list(old), hbt_delta=0, predicted=delta,
                             utility=utility(delta, self.cfg), affected_endpoints=[],
                             model="relocation_geometry_screen; full_GRT_STA_required")
                    p["id"] = signature(p)
                    proposals.append(p)
            finally:
                bucket.append(old)
        return proposals

    def run(self, allow_insert=True, allow_relocate=True):
        ranked = []
        for net in self.d.nets:
            if not net.eligible or net.name in self.ndr or not 2 <= len(net.pins) <= self.cfg["max_fanout"]+1:
                continue
            if sum(p.direction == "OUTPUT" for p in net.pins) != 1 or net.name not in self.segments:
                continue
            span = sum(max(p.xy[i] for p in net.pins)-min(p.xy[i] for p in net.pins) for i in (0, 1))/self.d.dbu
            if span < self.cfg["min_span_um"]:
                continue
            endpoints = {self.timing.paths[p]["endpoint"] for p in self.timing.by_net[net.name]
                         if self.timing.endpoints[self.timing.paths[p]["endpoint"]] < 0}
            old = self.resources.routed_edges(self.segments[net.name])
            pressure = sum(self.resources.potential(*self.resources.edges[e][:2]) for e in old)/max(1, len(old))
            ranked.append((-(1+len(endpoints))*(1+pressure)*span, net.name, net))
        chosen = {n: net for _, n, net in sorted(ranked)[:self.cfg["top_k"]]}
        limit = math.floor(self.sites.capacity*self.cfg["max_hbt_fraction"])
        proposals = []
        if allow_insert and len(self.d.existing)+2 <= limit:
            rc_nets = {n.name: n for n in read_spef(self.directory/"mls_bottom.spef") if n.name in chosen}
            for _, name, net in sorted(ranked)[:self.cfg["top_k"]]:
                if self.expired():
                    self.rejections["planner_budget"] += 1
                    break
                if name not in rc_nets:
                    self.rejections["missing_native_spef"] += 1
                    continue
                proposals.extend(self.net_candidates(net, rc_nets[name]))
        elif allow_insert:
            self.rejections["hbt_budget"] += 1
        if allow_relocate and self.cfg["enable_relocation"] and not self.expired():
            proposals.extend(self.relocations())
        unique = {p["id"]: p for p in proposals}
        return dict(schema_version=2, config=self.cfg, candidates=sorted(unique.values(), key=lambda p: (-p["utility"], p["id"])),
                    eligible_nets=len(ranked), rejections=dict(self.rejections),
                    coverage=self.timing.coverage(), expansions=self.resources.expansions,
                    hbt_capacity_proxy=self.sites.capacity, hbt_limit_proxy=limit,
                    existing_hbts=len(self.d.existing),
                    planner_runtime_seconds=time.monotonic()-self.started)
