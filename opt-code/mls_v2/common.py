"""Shared pure helpers; keep the v1 implementation and configuration untouched."""
import importlib.util
from pathlib import Path
import sys

_path = Path(__file__).resolve().parents[1] / "mls_v1/planner.py"
_spec = importlib.util.spec_from_file_location("mlsv1_helpers", _path)
v1 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = v1
_spec.loader.exec_module(v1)


def write_operations(operations, path):
    with Path(path).open("w") as out:
        out.write("# Replay against pristine CTS input, never against a failed trial.\n")
        for p in operations:
            if p["operation"] == "relocate":
                x, y = p["sites"][0]
                out.write(f'mlsv2::move_hbt {v1.tcl_quote(p["hbt"])} {x} {y}\n')
            else:
                pins = " ".join("[list " + v1.tcl_quote(i) + " " + v1.tcl_quote(t) + "]"
                                for i, t in p["sink_pins"])
                xy = " ".join(str(c) for point in p["sites"] for c in point)
                out.write(f'mlsv1::commit {v1.tcl_quote(p["net"])} [list {pins}] {xy}\n')
