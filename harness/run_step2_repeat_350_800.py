#!/usr/bin/env python3
"""
Step 2 Repeatability Suite — Rotated-Order Repetitions (Run 2 & Run 3) for 350 and 800 Connections.
Preserves Run 1 (`step2_runs/`) untouched and stores Run 2 and Run 3 in `step2_repeat_350_800/`,
then combines Run 1, Run 2, and Run 3 into `step2_350_800_repeatability.json`.

Profile rotation across the 3 runs:
  - Run 1 (already completed in `step2_runs/`): reference -> tuned -> argus
  - Run 2: tuned -> argus -> reference
  - Run 3: argus -> reference -> tuned
"""

import asyncio
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from run_step2_pressure_suite import (
    ORIGIN_IP,
    get_expected_homepage_hash,
    run_visitor_probes,
    run_connection_pressure_generator,
    run_ssh_management_probes,
    evaluate_trial,
    ssh_cmd,
)
from reanalyze_step2_and_timings import compute_500ms_recovery_from_timeline

BASE_DIR = Path(__file__).parent
REPEAT_DIR = BASE_DIR / "step2_repeat_350_800"
STEP2_RUN1_JSON = BASE_DIR / "step2_pressure_comparison.json"
OUT_REPEAT_JSON = BASE_DIR / "step2_350_800_repeatability.json"


async def run_repeat_trial(
    run_num: int,
    profile: str,
    tier_name: str,
    target_conns: int,
    active_req_workers: int,
    expected_sha256: str,
) -> dict:
    rc, out, err, _ = ssh_cmd(
        f"python3 <BENCHMARK_HARNESS>/step2_server_control.py {profile} 25 200",
        port=22,
        timeout=15.0,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to set profile {profile}: {out} / {err}")

    await asyncio.sleep(1.5)
    trial_id = f"run{run_num}_{profile}_{tier_name}_{target_conns}"
    print(f"[*] [Run {run_num}] profile={profile} | tier={tier_name} (target_conns={target_conns}, workers={active_req_workers})...", flush=True)

    visitor_task = asyncio.create_task(run_visitor_probes(trial_id, expected_sha256))
    flood_task = asyncio.create_task(run_connection_pressure_generator(target_conns, active_req_workers))
    ssh_task = asyncio.create_task(run_ssh_management_probes())

    visitor_rows, flood_stats, ssh_probes = await asyncio.gather(visitor_task, flood_task, ssh_task)

    _, status_out, _, _ = ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py status", port=22, timeout=10.0)
    host_after = {}
    for line in status_out.splitlines():
        if line.strip().startswith("{"):
            host_after = json.loads(line.strip())

    ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py clear_bans", port=22, timeout=10.0)

    summary = evaluate_trial(profile, tier_name, target_conns, visitor_rows, flood_stats, ssh_probes, host_after)
    summary["run_number"] = run_num

    tl = compute_500ms_recovery_from_timeline(visitor_rows)
    af = summary["phases"]["active_flood"]
    active_ssh = [p for p in ssh_probes if p["phase"] == "active_flood"]
    srv_sockets_active = [p["server_sockets_443"] for p in active_ssh if p.get("server_sockets_443") is not None]
    srv_bans_active = [p["server_bpf_bans"] for p in active_ssh if p.get("server_bpf_bans") is not None]

    summary["explicit_failure_criteria"] = {
        "criterion_original_code_http200_outage_failed": bool(summary["reached_failure_threshold"]),
        "criterion_active_load_ssh_failed": not all(p["authenticated_ok"] for p in active_ssh),
        "criterion_any_phase_ssh_failed": not all(p["authenticated_ok"] for p in ssh_probes),
        "criterion_500ms_overall_active_flood_below_80pct": (af["within_500ms_pct"] < 80.0),
        "criterion_500ms_sliding_5s_window_below_80pct": (tl["min_5s_window_within_500ms_count_of_10"] < 8),
    }
    summary["recovery_timeline_analysis"] = tl
    summary["achieved_connection_metrics"] = {
        "offered_target_connections": target_conns,
        "client_peak_established_connections": flood_stats["peak_client_held_connections"],
        "client_connect_failures": flood_stats["connect_timeouts_or_errors"],
        "generator_limited": not flood_stats["generator_reached_target"],
        "server_sockets_443_during_active_flood_samples": srv_sockets_active,
        "server_peak_sockets_443_observed": max(srv_sockets_active) if srv_sockets_active else None,
        "server_bpf_bans_during_active_flood": max(srv_bans_active) if srv_bans_active else 0,
    }

    REPEAT_DIR.mkdir(parents=True, exist_ok=True)
    (REPEAT_DIR / f"{trial_id}.probes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in visitor_rows),
        encoding="utf-8",
    )
    (REPEAT_DIR / f"{trial_id}.summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print(
        f"    -> [Run {run_num} | {profile} @ {target_conns}] "
        f"Active-Flood <=500ms={af['within_500ms_pct']:.1f}% ({af['within_500ms']}/40, p50={af['p50_ms']}ms, p95={af['p95_ms']}ms) | "
        f"PeakSrvConns={summary['achieved_connection_metrics']['server_peak_sockets_443_observed']} | "
        f"ActiveSSH_OK={not summary['explicit_failure_criteria']['criterion_active_load_ssh_failed']}",
        flush=True,
    )
    return summary


async def main():
    body_bytes, expected_sha256 = await get_expected_homepage_hash()
    print(f"[+] Preflight verified: homepage={body_bytes} bytes, sha256={expected_sha256}", flush=True)

    # Load Run 1 from step2_pressure_comparison.json
    run1_data = json.loads(STEP2_RUN1_JSON.read_text(encoding="utf-8"))
    run1_trials = []
    for t in run1_data["trials"]:
        if t["tier"] in ("tier2_350conns", "tier3_800conns"):
            t_copy = dict(t)
            t_copy["run_number"] = 1
            run1_trials.append(t_copy)

    new_trials = []
    tiers = [
        ("tier2_350conns", 350, 12),
        ("tier3_800conns", 800, 12),
    ]
    rotations = [
        (2, ["tuned", "argus", "reference"]),
        (3, ["argus", "reference", "tuned"]),
    ]

    try:
        for run_num, prof_order in rotations:
            for tier_name, target_conns, active_workers in tiers:
                for profile in prof_order:
                    s = await run_repeat_trial(run_num, profile, tier_name, target_conns, active_workers, expected_sha256)
                    new_trials.append(s)
                    await asyncio.sleep(2.0)
    finally:
        print("[*] Restoring production Step 1 configuration on <ORIGIN_SERVER_IP>...", flush=True)
        ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py restore", port=22, timeout=15.0)

    all_350_800 = run1_trials + new_trials

    # Aggregate 3-run repeatability summary per (tier, profile)
    agg = {}
    for tier_name in ("tier2_350conns", "tier3_800conns"):
        agg[tier_name] = {}
        for profile in ("reference", "tuned", "argus"):
            matching = [t for t in all_350_800 if t["tier"] == tier_name and t["profile"] == profile]
            matching.sort(key=lambda x: x["run_number"])
            w500_pcts = [t["phases"]["active_flood"]["within_500ms_pct"] for t in matching]
            w500_counts = [t["phases"]["active_flood"]["within_500ms"] for t in matching]
            p50s = [t["phases"]["active_flood"]["p50_ms"] for t in matching]
            p95s = [t["phases"]["active_flood"]["p95_ms"] for t in matching]
            p99s = [t["phases"]["active_flood"]["p99_ms"] for t in matching]
            srv_peaks = [t["achieved_connection_metrics"]["server_peak_sockets_443_observed"] for t in matching]
            cli_peaks = [t["achieved_connection_metrics"]["client_peak_established_connections"] for t in matching]
            below_80_flags = [t["explicit_failure_criteria"]["criterion_500ms_overall_active_flood_below_80pct"] for t in matching]
            active_ssh_fail_flags = [t["explicit_failure_criteria"]["criterion_active_load_ssh_failed"] for t in matching]

            agg[tier_name][profile] = {
                "runs_completed": len(matching),
                "per_run_active_flood_within_500ms_pct": w500_pcts,
                "per_run_active_flood_within_500ms_count_of_40": w500_counts,
                "pooled_active_flood_within_500ms_count_of_120": sum(w500_counts),
                "pooled_active_flood_within_500ms_pct": round(100.0 * sum(w500_counts) / (40 * len(matching)), 2),
                "min_within_500ms_pct": min(w500_pcts),
                "median_within_500ms_pct": round(statistics.median(w500_pcts), 2),
                "max_within_500ms_pct": max(w500_pcts),
                "per_run_p50_ms": p50s,
                "per_run_p95_ms": p95s,
                "per_run_p99_ms": p99s,
                "per_run_client_peak_connections": cli_peaks,
                "per_run_server_peak_sockets_443": srv_peaks,
                "runs_below_80pct_within_500ms": sum(1 for f in below_80_flags if f),
                "runs_with_active_load_ssh_failure": sum(1 for f in active_ssh_fail_flags if f),
            }

    report = {
        "experiment": "Step 2 Repeatability — 3 Rotated-Order Runs at 350 and 800 Offered Connections",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "rotation_orders": {
            "run_1": ["reference", "tuned", "argus"],
            "run_2": ["tuned", "argus", "reference"],
            "run_3": ["argus", "reference", "tuned"],
        },
        "aggregate_3run_summary": agg,
        "all_trials": all_350_800,
    }
    OUT_REPEAT_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[+] Saved 3-run repeatability report to {OUT_REPEAT_JSON}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
