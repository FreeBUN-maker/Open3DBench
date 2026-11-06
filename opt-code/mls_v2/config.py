"""Strict configuration validation, shared by host and container entrypoints."""
import json
import math
from pathlib import Path


def load_config(path=None, overrides=None):
    default = json.loads(Path(__file__).with_name("config.json").read_text())
    supplied = {} if path is None else json.loads(Path(path).read_text())
    supplied.update(overrides or {})
    if supplied.keys() - default.keys():
        raise ValueError(f"Unknown MLSv2 options: {sorted(supplied.keys()-default.keys())}")
    cfg = dict(default, **supplied)
    zero_ok = {"max_nets", "top_k", "max_trials", "max_relocations", "max_exchange_trials",
               "site_radius", "search_margin", "min_span_um", "slack_margin_ns",
               "unsampled_path_guard_ns", "max_tns_loss_ns", "max_wns_loss_ns",
               "max_hold_loss_ns", "max_extra_slew_violations", "max_extra_capacitance_violations"}
    for name, value in cfg.items():
        if isinstance(default[name], bool):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean")
            continue
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError(f"{name} must be finite numeric")
        if isinstance(default[name], int) and not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        if name == "min_utility":
            continue
        if value < 0 or (value == 0 and name not in zero_ok and not name.endswith("_weight")):
            raise ValueError(f"Invalid {name}={value}")
    if not 0 < cfg["max_hbt_fraction"] <= 1:
        raise ValueError("max_hbt_fraction must be in (0,1]")
    if cfg["max_site_radius"] < cfg["site_radius"]:
        raise ValueError("max_site_radius must be >= site_radius")
    return cfg


def utility(delta, cfg):
    """Positive means improvement; scales are fixed across nets AND rounds."""
    return sum(cfg[k+"_weight"] * delta.get(k, 0) / cfg[scale] for k, scale in (
        ("tns", "tns_scale_ns"), ("wns", "wns_scale_ns"),
        ("congestion", "congestion_scale"), ("wirelength", "wirelength_scale_um"),
        ("hbt", "hbt_scale"), ("access", "access_scale")))
