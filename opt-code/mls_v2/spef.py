"""Read the native estimate_parasitics SPEF and electrically collapse HBTs.

Input is deliberately limited to the OpenROAD exporter (unmapped names,
ground caps, one scene). Unknown syntax fails instead of dropping parasitics.
Internal RC nodes are renumbered across subnets. No submission ODB is changed.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import argparse
import re

from common import v1


def unescape(name):
    return name.replace(r"\$", "$")


def escape(name):
    if any(c.isspace() for c in name):
        raise ValueError("Whitespace in SPEF name is unsupported")
    return name.replace("$", r"\$")


@dataclass
class RCNet:
    name: str
    connections: dict = field(default_factory=dict)  # name -> (I/P, direction)
    caps: dict = field(default_factory=lambda: defaultdict(float))  # fF
    resistors: list = field(default_factory=list)  # ohm

    def delays(self, driver, loads, driver_r):
        nodes = sorted(set(self.caps) | set(self.connections) |
                       {n for a, b, _ in self.resistors for n in (a, b)})
        ids = {n: i for i, n in enumerate(nodes)}
        caps = {ids[n]: self.caps.get(n, 0) + loads.get(n, 0) for n in nodes}
        edges = [(ids[a], ids[b], r, 0) for a, b, r in self.resistors]
        delays = v1.elmore(edges, caps, ids[driver], driver_r)
        return {n: delays[i] for n, i in ids.items()}


def read_spef(path):
    """Yield one RC net at a time to bound memory on the large cases."""
    cap_scale = res_scale = None
    section, net = None, None
    with Path(path).open() as src:
        for line in src:
            words = line.split()
            if not words:
                continue
            tag = words[0]
            if tag == "*C_UNIT":
                cap_scale = float(words[1]) * {"PF": 1000, "FF": 1, "NF": 1e6}[words[2]]
            elif tag == "*R_UNIT":
                res_scale = float(words[1]) * {"OHM": 1, "KOHM": 1000}[words[2]]
            elif tag == "*NAME_MAP":
                raise ValueError("Expected native GRT SPEF without NAME_MAP")
            elif tag == "*D_NET":
                if net is not None or cap_scale is None or res_scale is None:
                    raise ValueError("Incomplete SPEF header or net")
                net = RCNet(unescape(words[1]))
                section = None
            elif tag == "*END":
                if net is None:
                    raise ValueError("Unexpected SPEF END")
                yield net
                net, section = None, None
            elif net is None:
                continue
            elif tag in {"*CONN", "*CAP", "*RES"}:
                section = tag
            elif section == "*CONN" and tag in {"*I", "*P"}:
                net.connections[unescape(words[1])] = (tag, words[2])
            elif section == "*CAP" and len(words) == 3:
                net.caps[unescape(words[1])] += float(words[2]) * cap_scale
            elif section == "*RES" and len(words) == 4:
                net.resistors.append((unescape(words[1]), unescape(words[2]),
                                      float(words[3]) * res_scale))
            else:
                raise ValueError(f"Unsupported GRT SPEF record: {line.rstrip()}")
    if net is not None:
        raise ValueError("Truncated SPEF")


def write_net(out, net):
    out.write(f'*D_NET {escape(net.name)} {sum(net.caps.values()):.12g}\n*CONN\n')
    for name, (kind, direction) in sorted(net.connections.items()):
        out.write(f'{kind} {escape(name)} {direction}\n')
    out.write("*CAP\n")
    for i, (name, cap) in enumerate(sorted(net.caps.items()), 1):
        out.write(f'{i} {escape(name)} {cap:.12g}\n')
    out.write("*RES\n")
    for i, (a, b, r) in enumerate(net.resistors, 1):
        out.write(f'{i} {escape(a)} {escape(b)} {r:.12g}\n')
    out.write("*END\n\n")


def hbt_groups(path):
    groups = defaultdict(list)
    for line in Path(path).read_text().splitlines():
        name, bot, top, x, y = line.split("\t")
        if name.startswith("LS_HBT_"):
            b = re.fullmatch(r"(.*)__MLS__S\d+__BOT", bot)
            t = re.fullmatch(r"(.*)__MLS__S\d+__TOP", top)
            if not b or not t or b[1] != t[1]:
                raise ValueError(f"Invalid MLS family on {name}")
            base = b[1]
        else:
            if not bot.endswith("_BOT") or top != bot[:-4] + "_TOP":
                raise ValueError(f"Invalid original HBT family on {name}")
            base = bot[:-4]
        groups[base].append((name, bot, top, int(x), int(y)))
    for base, hbts in groups.items():
        expected = 2 if hbts[0][0].startswith("LS_HBT_") else 1
        if len(hbts) != expected:
            raise ValueError(f"Wrong HBT count on {base}")
    return groups


def merge_group(base, nets, hbts):
    result = RCNet(base)
    internal, hbt_pins = {}, {h + ":" + p for h, *_ in hbts for p in ("BOT", "TOP")}
    seen_pins = set()

    def node(net, name):
        if name in net.connections and name not in hbt_pins:
            return name
        key = net.name, name
        if key not in internal:
            internal[key] = f"{base}:{len(internal)+1}"
        return internal[key]

    for net in nets.values():
        for name, conn in net.connections.items():
            if name in hbt_pins:
                seen_pins.add(name)
            else:
                result.connections[name] = conn
        for name, cap in net.caps.items():
            result.caps[node(net, name)] += cap
        for a, b, r in net.resistors:
            result.resistors.append((node(net, a), node(net, b), r))
    if seen_pins != hbt_pins:
        raise ValueError(f"Missing routed HBT terminals on {base}: {hbt_pins-seen_pins}")
    for h, bot, top, *_ in hbts:
        a, b = node(nets[bot], h+":BOT"), node(nets[top], h+":TOP")
        result.caps[a] += 0.3
        result.caps[b] += 0.3
        result.resistors.append((a, b, 3.0))
    return result


def merge(directory):
    directory = Path(directory)
    groups = hbt_groups(directory / "mls_hbts.tsv")
    family = {n: base for base, hs in groups.items() for h in hs for n in h[1:3]}
    retained, seen = defaultdict(dict), set()
    with (directory / "mls_merged.spef").open("w") as out:
        # Preserve design name and package ports, but normalize RC units.
        with (directory / "mls_bottom.spef").open() as src:
            for line in src:
                if line.startswith("*D_NET"):
                    break
                if line.startswith("*C_UNIT"):
                    line = "*C_UNIT 1 FF\n"
                if line.startswith("*R_UNIT"):
                    line = "*R_UNIT 1 OHM\n"
                out.write(line)
        for die, listing in (("bottom", "bottom_2d.txt"), ("upper", "upper_2d.txt")):
            allowed = set((directory / "die_net_lists" / listing).read_text().splitlines())
            for net in read_spef(directory / f"mls_{die}.spef"):
                if net.name not in allowed:
                    continue
                if net.name in seen:
                    raise ValueError(f"Duplicate routed SPEF net {net.name}")
                seen.add(net.name)
                if net.name in family:
                    retained[family[net.name]][net.name] = net
                else:
                    if net.name in groups and net.connections:
                        raise ValueError(f"Unsplit base net coexists with {net.name}")
                    write_net(out, net)
        for base, hs in sorted(groups.items()):
            expected = {n for h in hs for n in h[1:3]}
            if set(retained[base]) != expected:
                raise ValueError(f"Incomplete HBT SPEF family {base}")
            write_net(out, merge_group(base, retained[base], hs))
    with (directory / "mls_collapse.tcl").open("w") as out:
        for base, hs in sorted(groups.items()):
            names = sorted({n for h in hs for n in h[1:3]})
            quoted = lambda xs: "[list " + " ".join(v1.tcl_quote(x) for x in xs) + "]"
            out.write(f'mlsv2::collapse {v1.tcl_quote(base)} {quoted(names)} '
                      f'{quoted([h[0] for h in hs])}\n')


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    merge(parser.parse_args().directory)
