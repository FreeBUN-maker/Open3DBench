#!/usr/bin/env python3
"""Compare evaluator metrics. Does not invent an official aggregate score."""
import argparse
import json
from pathlib import Path


def compare(baseline, candidate):
    if baseline.get("legal") is not True or candidate.get("legal") is not True:
        raise ValueError("Both evaluations must be legal")
    if baseline.get("case") != candidate.get("case"):
        raise ValueError("Metrics are from different cases")
    keys = ("tns_ns", "wns_ns", "drt_wirelength_um", "drc", "hbt_count",
            "hbt_excess_count", "evaluator_runtime_seconds")
    result = {}
    for key in keys:
        a, b = baseline.get(key), candidate.get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        # Larger slack is better. Smaller length/count/runtime is better.
        gain = b-a if key in {"tns_ns", "wns_ns"} else a-b
        result[key] = dict(baseline=a, candidate=b, delta=b-a,
                           improvement_percent=100*gain/abs(a) if a else None)
    return dict(case=baseline.get("case"), metrics=result,
                note="Relative changes only; evaluator runtime excludes algorithm/GRT runtime. No official score computed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(json.loads(args.baseline.read_text()),
                             json.loads(args.candidate.read_text())), indent=2))


if __name__ == "__main__":
    main()
