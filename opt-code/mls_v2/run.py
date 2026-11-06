#!/usr/bin/env python3
"""Host-side transactional full-GRT validation loop. Does not run the evaluator."""
import argparse
from collections import Counter
import hashlib
import itertools
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import subprocess
import time
import uuid

from common import write_operations
from config import load_config, utility
from feedback import Resources, Timing, read_rc, read_segments
from planner import Planner
from spef import hbt_groups


def save(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False)+"\n")
    tmp.replace(path)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as src:
        for chunk in iter(lambda: src.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(directory, cfg):
    directory = Path(directory)
    timing = Timing(directory/"mls_timing.tsv")
    layers, _, _ = read_rc(directory/"mls_rc.tsv")
    res = Resources(directory, layers, cfg).summary()
    dbu = int((directory/"mls_design.tsv").read_text().splitlines()[0].split("\t")[1])
    length, vias = 0, 0
    for die in ("bottom", "upper"):
        for segs in read_segments(directory/f"mls_{die}.segments").values():
            for a, b in segs:
                if a[2] == b[2]:
                    length += (abs(a[0]-b[0])+abs(a[1]-b[1]))/dbu
                else:
                    vias += abs(layers[a[2]]["level"]-layers[b[2]]["level"])
    return dict(**timing.metrics, congestion=res["congestion"], overflow=res["overflow"],
                wirelength_um=length, access=vias,
                hbt_count=sum(len(h) for h in hbt_groups(directory/"mls_hbts.tsv").values()),
                coverage=timing.coverage(), timing_model=timing.model, conditions=timing.conditions)


def assess(current, trial, cfg):
    if current["conditions"] != trial["conditions"] or current["timing_model"] != trial["timing_model"]:
        return dict(accepted=False, reasons=["inconsistent_analysis_conditions"])
    delta = dict(tns=trial["tns_ns"]-current["tns_ns"],
                 wns=trial["wns_ns"]-current["wns_ns"],
                 congestion=current["congestion"]-trial["congestion"],
                 wirelength=current["wirelength_um"]-trial["wirelength_um"],
                 hbt=current["hbt_count"]-trial["hbt_count"],
                 access=current["access"]-trial["access"])
    reasons = []
    for name, metric, limit in (("tns", "tns_ns", "max_tns_loss_ns"),
                                ("wns", "wns_ns", "max_wns_loss_ns"),
                                ("hold", "hold_wns_ns", "max_hold_loss_ns")):
        if trial[metric] < current[metric]-cfg[limit]-1e-9:
            reasons.append(name+"_guard")
    for metric, limit in (("slew_violations", "max_extra_slew_violations"),
                          ("capacitance_violations", "max_extra_capacitance_violations")):
        if trial[metric] > current[metric]+cfg[limit]:
            reasons.append(metric+"_guard")
    score = utility(delta, cfg)
    if score <= cfg["min_utility"]:
        reasons.append("insufficient_measured_utility")
    return dict(accepted=not reasons, reasons=reasons, measured=delta, utility=score)


def choose_winner(current, trials, cfg):
    """All trials in a round are compared with the SAME accepted state."""
    scored = [(assess(current, metrics, cfg), index) for index, metrics in trials]
    allowed = [(score["utility"], -index, index) for score, index in scored if score["accepted"]]
    return max(allowed)[2] if allowed else None


def compare_feedback(current_dir, trial_dir, proposal, cfg):
    before = Timing(Path(current_dir)/"mls_timing.tsv")
    after = Timing(Path(trial_dir)/"mls_timing.tsv")
    if before.endpoints.keys() != after.endpoints.keys():
        raise ValueError("Endpoint identity changed after a supposedly equivalent trial")
    changed = [dict(endpoint=e, old_slack_ns=s, new_slack_ns=after.endpoints[e],
                    tns_gain_ns=min(0, after.endpoints[e])-min(0, s))
               for e, s in before.endpoints.items() if abs(s-after.endpoints[e]) > 1e-9]
    result = dict(changed_endpoints=len(changed),
                  largest_endpoint_changes=sorted(changed, key=lambda x: -abs(x["tns_gain_ns"]))[:1000],
                  endpoint_details_truncated=len(changed) > 1000,
                  full_endpoints_in="mls_timing.tsv of the accepted state and trial")
    if proposal.get("predicted_top_edges") is not None:
        directory = Path(trial_dir)
        layers, _, _ = read_rc(directory/"mls_rc.tsv")
        resources = Resources(directory, layers, cfg)
        top = read_segments(directory/"mls_upper.segments")
        actual = resources.routed_edges(top.get(proposal["net"]+"__MLS__S1__TOP", []))
        predicted = {tuple(e) for e in proposal["predicted_top_edges"]}
        result["top_path"] = dict(predicted_edges=len(predicted), actual_edges=len(actual),
                                  shared_edges=len(actual & predicted),
                                  jaccard=len(actual & predicted)/max(1, len(actual | predicted)),
                                  actual=[list(e) for e in sorted(actual)])
    return result


class Runner:
    def __init__(self, root, case, label, cfg):
        self.root, self.case, self.label, self.cfg = Path(root), case, label, cfg
        self.started = time.monotonic()
        self.uid = uuid.uuid4().hex[:10]
        self.work = self.root/".contest/mls_v2"/case/(label+"-"+self.uid)
        self.work.mkdir(parents=True)
        self.config_path = self.work/"config.json"
        save(self.config_path, cfg)
        self.report = dict(schema_version=2, algorithm="mls_v2", config=cfg,
                           case=case, label=label, work_dir=str(self.work),
                           rounds=[], trials=[], selected=[], stop_reason=None)
        self.trial_count = 0

    def expired(self):
        return time.monotonic()-self.started >= self.cfg["max_seconds"]

    def trial(self, operations, role):
        index = len(self.report["trials"])
        label = f"{self.label}-{self.uid}-{index:03d}"
        plan = self.work/f"{index:03d}.tcl"
        write_operations(operations, plan)
        log = self.work/f"{index:03d}.log"
        output = self.root/"output"/self.case/label
        item = dict(index=index, role=role, label=label, operations=operations,
                    output=str(output), log=str(log), status="running")
        self.report["trials"].append(item)
        save(self.work/"mls_plan.json", self.report)
        print(f"MLSv2: {role}, full two-pass GRT/STA -> {label}", flush=True)
        started = time.monotonic()
        command = ["bash", str(Path(__file__).with_name("run_pass.sh")), label, str(plan), str(self.config_path)]
        remaining = self.cfg["max_seconds"]-(started-self.started)
        timeout = min(self.cfg["trial_timeout_seconds"], max(0.1, remaining))
        container_name = "mlsv2-"+self.uid+"-"+str(index)
        with log.open("w") as fp:
            env = dict(os.environ, MLSV2_CONTAINER_NAME=container_name)
            proc = subprocess.Popen(command, stdout=fp, stderr=subprocess.STDOUT, start_new_session=True, env=env)
            try:
                code = proc.wait(timeout=timeout)
                if code:
                    raise RuntimeError(f"routing process exited {code}")
                item["metrics"] = snapshot(output, self.cfg)
                item["status"] = "validated"
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as interrupt:
                os.killpg(proc.pid, signal.SIGTERM)
                # A timed-out docker client is not evidence its container exited.
                subprocess.run(["docker", "rm", "-f", container_name], stdout=fp, stderr=subprocess.STDOUT, timeout=30)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                item.update(status="failed", error="timeout_or_interrupt", rollback="accepted artifacts untouched")
                if not self.expired() and role == "baseline":
                    item["error"] = "baseline_timeout_or_interrupt"
                if isinstance(interrupt, KeyboardInterrupt):
                    raise
            except Exception as e:
                item.update(status="failed", error=str(e), rollback="accepted artifacts untouched")
        item["runtime_seconds"] = time.monotonic()-started
        item["flow_log_dirs"] = [str(p) for p in (self.root/".contest/flow-work/logs").glob(f"*/{self.case}/{label}")]
        item["stage_seconds"] = {p.name: float(p.read_text()) for p in output.glob("mls_*seconds")}
        save(self.work/"mls_plan.json", self.report)
        if item["status"] == "failed":
            print(f'MLSv2: trial failed, preserved accepted solution; see {log}', flush=True)
        return item

    def run(self):
        final = self.root/"output"/self.case/self.label
        if final.exists():
            raise FileExistsError(f"Output already exists; use a new label: {final}")
        self.provenance()
        current = self.trial([], "baseline")
        if current["status"] != "validated":
            raise RuntimeError(f'Baseline feedback failed: {current["log"]}')
        self.report["baseline"] = current["metrics"]
        self.report["baseline_trial"] = current["index"]
        operations = []
        generation = 0
        tried = set()
        search_limit = max(0, self.cfg["max_trials"]-(self.cfg["max_exchange_trials"] if self.cfg["enable_exchanges"] else 0))
        for round_index in range(self.cfg["max_rounds"]):
            counts = Counter(p["operation"] for p in operations)
            can_insert = counts["insert"] < self.cfg["max_nets"]
            can_move = self.cfg["enable_relocation"] and counts["relocate"] < self.cfg["max_relocations"]
            if not can_insert and not can_move:
                self.report["stop_reason"] = "accepted_operation_budget"
                break
            if self.expired() or self.trial_count >= search_limit:
                self.report["stop_reason"] = "time_or_trial_budget"
                break
            planner = Planner(current["output"], self.cfg)
            ranking = planner.run(can_insert, can_move)
            self.report["hbt_limit_proxy"] = ranking["hbt_limit_proxy"]
            ranking_path = self.work/f"round_{round_index:03d}.json"
            save(ranking_path, ranking)
            remaining = search_limit-self.trial_count
            queue = [p for p in ranking["candidates"] if (generation, p["id"]) not in tried
                     and not any(p["operation"] == "relocate" and q.get("hbt") == p.get("hbt") for q in operations)]
            queue = queue[:min(remaining, self.cfg["candidates_per_round"])]
            if not queue:
                self.report["stop_reason"] = "no_untried_candidates"
                break
            round_record = dict(round=round_index, accepted_state=current["index"], ranking=str(ranking_path), trials=[])
            self.report["rounds"].append(round_record)
            valid = []
            for proposal in queue:
                if self.expired():
                    break
                tried.add((generation, proposal["id"]))
                item = self.trial(operations+[proposal], "candidate")
                self.trial_count += 1
                item["candidate_id"] = proposal["id"]
                round_record["trials"].append(item["index"])
                if item["status"] == "validated":
                    try:
                        item["assessment"] = assess(current["metrics"], item["metrics"], self.cfg)
                        item["prediction_error"] = {k: item["assessment"].get("measured", {}).get(k, 0)-v
                                                    for k, v in proposal["predicted"].items() if k != "access"}
                        item["access_models"] = dict(predicted_pressure_gain=proposal["predicted"].get("access"),
                                                     measured_via_count_gain=item["assessment"].get("measured", {}).get("access"))
                        item["feedback_comparison"] = compare_feedback(current["output"], item["output"], proposal, self.cfg)
                        valid.append((item["index"], item["metrics"]))
                    except (ValueError, OSError) as error:
                        item.update(status="failed", error=str(error), rollback="accepted artifacts untouched")
            winner = choose_winner(current["metrics"], valid, self.cfg)
            for index in round_record["trials"]:
                item = self.report["trials"][index]
                if item["status"] == "validated":
                    item["status"] = "accepted" if index == winner else "rejected"
                    if index != winner:
                        item["rollback"] = "accepted artifacts untouched"
            if winner is not None:
                current = self.report["trials"][winner]
                operations = current["operations"]
                generation += 1
                print(f'MLSv2: accepted {current["candidate_id"]}, measured utility={current["assessment"]["utility"]:.6g}', flush=True)
            self.report["selected"] = operations
            save(self.work/"mls_plan.json", self.report)
        else:
            self.report["stop_reason"] = "round_budget"
        if self.cfg["enable_exchanges"]:
            current, operations = self.exchanges(current, operations)
        self.report["selected"] = operations
        self.report["final_metrics"] = current["metrics"]
        self.report["final_trial"] = current["index"]
        self.report["algorithm_runtime_seconds"] = time.monotonic()-self.started
        self.report["time_breakdown"] = dict(
            baseline_seconds=self.report["trials"][0].get("runtime_seconds", 0),
            candidate_seconds=sum(t.get("runtime_seconds", 0) for t in self.report["trials"][1:]),
            planner_seconds=sum(json.loads(Path(r["ranking"]).read_text()).get("planner_runtime_seconds", 0)
                                for r in self.report["rounds"]),
            final_reroute_seconds=0, evaluator_seconds=None)
        self.report["peak_host_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        self.report["max_child_rss_kb"] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        self.report["memory_scope"] = "host Python and child clients; container OpenROAD RSS is in flow logs"
        # Copy exactly the already validated ODB/guides. No final reroute can
        # invalidate the accepted state. Publish by an atomic directory rename.
        staging = final.with_name(final.name+".publishing-"+self.uid)
        staging.mkdir(parents=True)
        for name in ("5_1_grt.odb", "route.guide", "mls_commits.tsv", "mls_timing.tsv", "mls_hbts.tsv"):
            src = Path(current["output"])/name
            if src.exists():
                shutil.copy2(src, staging/name)
        for name in ("5_1_grt.odb", "route.guide"):
            if sha256(staging/name) != sha256(Path(current["output"])/name):
                raise RuntimeError("Final artifact copy mismatch; accepted trial remains available")
        self.report["artifacts"] = {name: sha256(staging/name) for name in ("5_1_grt.odb", "route.guide")}
        save(self.work/"mls_plan.json", self.report)
        save(staging/"mls_plan.json", self.report)
        write_operations(operations, staging/"mls_plan.tcl")
        staging.rename(final)
        print(f'MLSv2: completed; accepted operations={len(operations)}; output={final}', flush=True)

    def exchanges(self, current, operations):
        """Bounded 1->1 / 1->2 replacement of an accepted MLS insertion.

        Recover the earlier candidate pool, release removed sites by replay,
        and judge the entire combination against current measured metrics.
        Predicted marginal utilities are never added as acceptance evidence.
        """
        if not operations:
            return current, operations
        proposals = {}
        for record in self.report["rounds"]:
            for p in json.loads(Path(record["ranking"]).read_text())["candidates"]:
                if p["operation"] == "insert":
                    proposals[p["id"]] = p
        pool = sorted(proposals.values(), key=lambda p: (-p["utility"], p["id"]))[:self.cfg["top_k"]]
        attempts = 0
        for old in list(operations):
            if old["operation"] != "insert":
                continue
            keep = [p for p in operations if p["id"] != old["id"]]
            occupied_nets = {p["net"] for p in keep if p["operation"] == "insert"}
            eligible = [p for p in pool if p["net"] not in occupied_nets and p["id"] != old["id"]]
            # Interleave both exchange widths so a small experiment budget
            # actually reaches 1->2 candidates as well as 1->1 replacements.
            singles = itertools.combinations(eligible, 1)
            pairs = (pair for pair in itertools.combinations(eligible, 2)
                     if pair[0]["net"] != pair[1]["net"])
            replacements = itertools.chain.from_iterable(itertools.zip_longest(singles, pairs))
            for replacement in replacements:
                if replacement is None:
                    continue
                if self.expired() or self.trial_count >= self.cfg["max_trials"] or attempts >= self.cfg["max_exchange_trials"]:
                    return current, operations
                if len({p["net"] for p in replacement}) != len(replacement):
                    continue
                candidate = keep+list(replacement)
                if sum(p["operation"] == "insert" for p in candidate) > self.cfg["max_nets"]:
                    continue
                baseline_hbts = self.report["baseline"]["hbt_count"]
                if baseline_hbts+2*sum(p["operation"] == "insert" for p in candidate) > self.report["hbt_limit_proxy"]:
                    continue
                item = self.trial(candidate, "exchange")
                attempts += 1
                self.trial_count += 1
                if item["status"] == "validated":
                    try:
                        item["feedback_comparison"] = compare_feedback(current["output"], item["output"], {}, self.cfg)
                    except (ValueError, OSError) as error:
                        item.update(status="failed", error=str(error), rollback="accepted artifacts untouched")
                        continue
                    item["assessment"] = assess(current["metrics"], item["metrics"], self.cfg)
                    item["status"] = "accepted" if item["assessment"]["accepted"] else "rejected"
                    if item["status"] == "accepted":
                        return item, candidate
                    item["rollback"] = "accepted artifacts untouched"
        return current, operations

    def provenance(self):
        repo = Path(__file__).resolve().parents[2]
        input_name = os.environ.get("OPEN3DBENCH_INPUT_NAME", "open3dbench_8cases_post_hbt_input_20260724")
        inputs = self.root/"input"/input_name
        files = list((inputs/"cases"/self.case).rglob("*")) + list((inputs/"platforms").rglob("*"))
        input_hashes = {str(p.relative_to(inputs)): sha256(p) for p in sorted(files) if p.is_file()}
        sources = [repo/"opt-code/mls_v2", repo/"opt-code/mls_v1", repo/"opt-code/baseline"]
        source_hashes = {str(p.relative_to(repo)): sha256(p) for d in sources for p in sorted(d.rglob("*"))
                         if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".tcl", ".cpp", ".h", ".i", ".json", ".sh"}}
        image = os.environ.get("OPEN3DBENCH_CONTEST_IMAGE", "gaocr/3dbench-contest:20260914")
        image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True).strip()
        self.report["provenance"] = dict(image=image, image_id=image_id, threads=int(os.environ.get("MLSV2_THREADS", 8)),
            input_sha256=input_hashes, source_sha256=source_hashes, cache_reused=False,
            grt_args="-congestion_iterations 2 -congestion_report_iter_step 5 -verbose",
            incremental=False, full_replay=True,
            build_version=(self.root/".contest/build-version.env").read_text() if (self.root/".contest/build-version.env").exists() else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    overrides = {k: int(os.environ[e]) for k, e in (("max_nets", "MLSV2_MAX_NETS"), ("max_trials", "MLSV2_MAX_TRIALS")) if e in os.environ}
    cfg = load_config(args.config, overrides)
    runner = Runner(os.environ["OPEN3DBENCH_ROOT"], os.environ["CASE_NAME"], args.label, cfg)
    try:
        runner.run()
    except BaseException as e:
        runner.report["fatal_error"] = str(e)
        runner.report["algorithm_runtime_seconds"] = time.monotonic()-runner.started
        save(runner.work/"mls_plan.json", runner.report)
        raise


if __name__ == "__main__":
    main()
