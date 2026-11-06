"""Versioned, fail-closed readers for actual GRT resources, RC and endpoint STA."""
from collections import Counter, defaultdict
import heapq
import math
from pathlib import Path


class Timing:
    def __init__(self, path):
        self.metrics, self.endpoints, self.paths = {}, {}, {}
        self.sample, self.conditions, self.by_net = {}, {}, defaultdict(set)
        version = None
        for line in Path(path).read_text().splitlines():
            tag, *v = line.split("\t")
            if tag == "SCHEMA":
                version = int(v[0])
            elif tag == "MODEL":
                self.model = v[0]
            elif tag == "METRIC":
                self.metrics[v[0]] = float(v[1])
            elif tag == "CONDITION":
                self.conditions[v[0]] = v[1]
            elif tag == "ENDPOINT":
                self.endpoints[v[0]] = float(v[1])
            elif tag == "PATH":
                self.paths[v[0]] = dict(endpoint=v[1], slack=float(v[2]), branches=[])
            elif tag == "BRANCH":
                pid, net, driver, sink, delay = v
                self.paths[pid]["branches"].append((net, driver, sink, float(delay)))
                self.by_net[net].add(pid)
            elif tag == "SAMPLE":
                self.sample[v[0]] = int(v[1])
            else:
                raise ValueError(f"Unknown timing record {tag}")
        required = {"tns_ns", "wns_ns", "hold_wns_ns", "slew_violations", "capacitance_violations"}
        if version != 2 or not required <= self.metrics.keys() or not self.endpoints:
            raise ValueError("Incomplete GRT timing snapshot")
        if not all(math.isfinite(x) for x in [*self.metrics.values(), *self.endpoints.values()]):
            raise ValueError("Nonfinite timing snapshot")
        self.by_endpoint = defaultdict(list)
        for path in self.paths.values():
            self.by_endpoint[path["endpoint"]].append(path)
        total = sum(min(0, x) for x in self.endpoints.values())
        if abs(total-self.metrics["tns_ns"]) > max(1e-4, abs(total)*1e-5):
            raise ValueError("Endpoint sum differs from OpenSTA TNS (analysis scope mismatch)")

    def coverage(self):
        negative = {e: s for e, s in self.endpoints.items() if s < 0}
        sampled = negative.keys() & self.by_endpoint.keys()
        return dict(sampled_endpoints=len(self.by_endpoint), violated_endpoints=len(negative),
                    sampled_violated_endpoints=len(sampled),
                    violated_endpoint_fraction=len(sampled)/max(1, len(negative)),
                    covered_negative_slack_ns=-sum(negative[e] for e in sampled),
                    total_negative_slack_ns=-sum(negative.values()),
                    approximate=True, **self.sample)

    def predict(self, changes, guard):
        """changes[(net, sink)] is delay_old-delay_new in ns, for ALL sinks.

        Each endpoint contributes once; all sampled competing paths participate.
        Unknown paths cap positive extrapolation, but never hide predicted loss.
        No graph traversal beyond captured timing paths (STA honors exceptions).
        """
        affected = {p["endpoint"] for n, _ in changes for pid in self.by_net.get(n, ())
                    for p in (self.paths[pid],)}
        updated, details = dict(self.endpoints), []
        for endpoint in sorted(affected):
            paths = self.by_endpoint[endpoint]
            old = self.endpoints[endpoint]
            sampled_old = min(p["slack"] for p in paths)
            predicted = min(p["slack"] + sum(changes.get((n, s), 0.0)
                                             for n, _, s, _ in p["branches"])
                            for p in paths)
            # If no sampled path explains the endpoint's worst slack, preserve
            # the unsampled worst path as a competitor. Otherwise use a bounded
            # optimistic extrapolation beyond the last observed competitor.
            unknown_bound = (old if sampled_old > old+1e-6 else
                             max(p["slack"] for p in paths)+guard)
            new = min(predicted, unknown_bound)
            updated[endpoint] = new
            details.append(dict(endpoint=endpoint, old_slack_ns=old,
                                predicted_slack_ns=new, sampled_paths=len(paths),
                                tns_gain_ns=min(0, new)-min(0, old)))
        return dict(tns=sum(d["tns_gain_ns"] for d in details),
                    wns=min(0, min(updated.values()))-self.metrics["wns_ns"]), details


def read_rc(path):
    layers, pins, vias = {}, {}, {}
    for line in Path(path).read_text().splitlines():
        tag, *v = line.split("\t")
        if tag == "LAYER":
            layers[v[0]] = dict(level=int(v[1]), direction=v[2], r=float(v[3]), c=float(v[4]))
        elif tag == "VIA":
            vias[int(v[2]), int(v[3])] = float(v[1])
        elif tag == "PIN":
            pins[v[1]] = dict(net=v[0], cap=float(v[2]), resistance=float(v[3]), slew=float(v[4]))
        elif tag != "MISSING_LIBERTY_PINS":
            raise ValueError(f"Unknown RC record {tag}")
    if not layers or not pins:
        raise ValueError("Missing calibrated layer/pin RC")
    return layers, pins, vias


def read_segments(path):
    result, name, opened = {}, None, False
    with Path(path).open() as src:
        for line in src:
            line = line.strip()
            if not line:
                continue
            if name is None:
                name = line
                if name in result:
                    raise ValueError(f"Duplicate segment net {name}")
                result[name] = []
            elif line == "(":
                opened = True
            elif line == ")":
                name, opened = None, False
            elif opened:
                x, y, layer, xx, yy, ll = line.split()
                result[name].append(((int(x), int(y), layer), (int(xx), int(yy), ll)))
            else:
                raise ValueError("Malformed GRT segment file")
    if name is not None:
        raise ValueError("Truncated GRT segments")
    return result


class Resources:
    def __init__(self, directory, layers, cfg):
        self.cfg, self.layers = cfg, layers
        self.edges, self.grids = {}, {}
        self.expansions = 0
        for die, label in enumerate(("bottom", "upper")):
            with (Path(directory)/f"mls_{label}.resources.tsv").open() as src:
                for line in src:
                    if line.startswith("#"):
                        continue
                    v = line.rstrip().split("\t")
                    if v[0] == "GRID":
                        self.grids[die] = tuple(map(int, v[1:]))
                        continue
                    layer, d, direction, x, y, cap, demand, blockage, overflow = v
                    key = layer, int(x), int(y), direction
                    values = tuple(map(int, (cap, demand, blockage, overflow)))
                    if int(d) != die or key in self.edges or min(values) < 0:
                        raise ValueError("Invalid resource record")
                    if values[3] != max(0, values[1]-values[0]):
                        raise ValueError("Overflow/resource accounting mismatch")
                    self.edges[key] = values
        if len(self.grids) != 2 or not self.edges:
            raise ValueError("Incomplete per-die resources")
        self.layer_options = {(d, direction): sorted(
            [l for l, rc in layers.items() if (0 if rc["level"] <= 10 else 1) == d
             and rc["direction"] == ("HORIZONTAL" if direction == "H" else "VERTICAL")
             and (l, 0, 0, direction) in self.edges],
            key=lambda l: (layers[l]["r"], l)) for d in (0, 1) for direction in ("H", "V")}

    @staticmethod
    def potential(cap, demand):
        rho = max(0, demand)/max(1, cap)
        return rho*rho + 8*max(0, rho-1)**2 + (100*max(0, demand) if cap == 0 else 0)

    def summary(self):
        return dict(overflow=sum(v[3] for v in self.edges.values()),
                    congestion=sum(self.potential(v[0], v[1]) for v in self.edges.values()),
                    demand=sum(v[1] for v in self.edges.values()),
                    capacity_semantics="available; do not subtract blockage again")

    def node(self, xy, die):
        ox, oy, step, nx, ny = self.grids[die]
        return (max(0, min(nx-1, (xy[0]-ox)//step)), max(0, min(ny-1, (xy[1]-oy)//step)))

    def xy(self, node, die):
        ox, oy, step, _, _ = self.grids[die]
        return ox+node[0]*step+step//2, oy+node[1]*step+step//2

    def routed_edges(self, segments):
        edges = set()
        for a, b in segments:
            if a[2] != b[2]:
                continue  # vias are exported, but FastRoute has no via capacity
            die = 0 if self.layers[a[2]]["level"] <= 10 else 1
            x, y = self.node(a, die)
            xx, yy = self.node(b, die)
            if self.xy((x, y), die) != a[:2] or self.xy((xx, yy), die) != b[:2]:
                continue  # native pin access is not FastRoute grid-edge demand
            if x != xx and y != yy:
                raise ValueError("Non-Manhattan global route segment")
            direction = "H" if x != xx else "V"
            for i in range(min(x, xx), max(x, xx)) if direction == "H" else range(min(y, yy), max(y, yy)):
                e = a[2], i if direction == "H" else x, y if direction == "H" else i, direction
                # Off-grid pin access can project onto edges not consumed by
                # FastRoute. Such edges must never be released from demand.
                if e in self.edges and self.edges[e][1] > 0:
                    edges.add(e)
        return edges

    def delta_cost(self, old, new):
        old, new = Counter(old), Counter(new)
        gain = 0.0
        for e in old.keys() | new.keys():
            cap, demand, *_ = self.edges[e]
            changed = demand-old[e]+new[e]
            if changed < 0:
                raise ValueError("Resource release exceeds background demand")
            gain += self.potential(cap, demand)-self.potential(cap, changed)
        return gain

    def local_pressure(self, xy, die):
        x, y = self.node(xy, die)
        vals = [self.potential(v[0], v[1]) for direction in ("H", "V")
                for layer in self.layer_options[die, direction]
                if (v := self.edges.get((layer, x, y, direction))) is not None]
        return sum(vals)/max(1, len(vals))

    def route(self, a, b, die, released=frozenset()):
        start, goal = self.node(a, die), self.node(b, die)
        _, _, _, nx, ny = self.grids[die]
        margin = self.cfg["search_margin"]
        lx, ux = max(0, min(start[0], goal[0])-margin), min(nx-1, max(start[0], goal[0])+margin)
        ly, uy = max(0, min(start[1], goal[1])-margin), min(ny-1, max(start[1], goal[1])+margin)
        dist, parent, queue = {start: 0}, {}, [(0, 0, start)]
        expanded = 0
        while queue:
            _, cost, u = heapq.heappop(queue)
            if cost != dist[u]:
                continue
            if u == goal:
                edges = set()
                while u != start:
                    u, edge = parent[u]
                    edges.add(edge)
                return edges
            if expanded >= self.cfg["max_expansions_per_path"] or self.expansions >= self.cfg["max_total_expansions"]:
                return None
            self.expansions += 1
            expanded += 1
            for v in ((u[0]-1, u[1]), (u[0]+1, u[1]), (u[0], u[1]-1), (u[0], u[1]+1)):
                if not lx <= v[0] <= ux or not ly <= v[1] <= uy:
                    continue
                direction = "H" if u[0] != v[0] else "V"
                choices = []
                for layer in self.layer_options[die, direction]:
                    edge = layer, min(u[0], v[0]), min(u[1], v[1]), direction
                    cap, demand, *_ = self.edges[edge]
                    if cap <= 0:
                        continue
                    demand -= int(edge in released)
                    marginal = self.potential(cap, demand+1)-self.potential(cap, demand)
                    choices.append((1+max(0, marginal), edge))
                if not choices:
                    continue
                step_cost, edge = min(choices)
                new = cost+step_cost
                if new < dist.get(v, math.inf):
                    dist[v], parent[v] = new, (u, edge)
                    h = abs(v[0]-goal[0])+abs(v[1]-goal[1])
                    heapq.heappush(queue, (new+h, new, v))
        return None
