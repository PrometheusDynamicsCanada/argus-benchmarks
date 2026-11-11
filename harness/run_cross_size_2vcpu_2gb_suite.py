#!/usr/bin/env python3
"""
Cross-Size Benchmark Suite:
Tests a $18/month (2 vCPU / 2 GB RAM) DigitalOcean droplet in nyc1 running:
  - `reference` (Configured reference without Argus)
  - `tuned` (Linux + NGINX concurrency tuning only, without Argus mitigation)
across 3 rotated-order runs at 350 and 800 offered connections (plus the 1,200-attempted
generator-limited tier), and compares the results directly against the saved
$4/month (1 vCPU / 512 MB RAM) droplet running `argus`.
"""

import asyncio
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from run_step2_pressure_suite import (
    get_expected_homepage_hash,
    run_visitor_probes,
    run_connection_pressure_generator,
    run_ssh_management_probes,
    evaluate_trial,
    ssh_cmd,
)
from reanalyze_step2_and_timings import compute_500ms_recovery_from_timeline

BASE_DIR = Path(__file__).parent
CROSS_DIR = BASE_DIR / "cross_size_2vcpu_2gb_runs"
STEP2_RUN1_JSON = BASE_DIR / "step2_pressure_comparison.json"
STEP2_REPEAT_JSON = BASE_DIR / "step2_350_800_repeatability.json"
OUT_CROSS_JSON = BASE_DIR / "cross_size_4usd_argus_vs_18usd_without_argus.json"


def verify_2vcpu_2gb_hardware() -> dict:
    rc, out, err, _ = ssh_cmd(
        "python3 -c \"import json, os, subprocess; "
        "mem = [l for l in open('/proc/meminfo') if 'MemTotal' in l][0].strip(); "
        "cpus = os.cpu_count(); "
        "boot_id = open('/proc/sys/kernel/random/boot_id').read().strip(); "
        "uname = os.uname().release; "
        "print(json.dumps({'vcpus': cpus, 'mem_total': mem, 'boot_id': boot_id, 'kernel': uname}))\"",
        port=22,
        timeout=15.0,
    )
    if rc != 0:
        raise RuntimeError(f"Hardware check failed: {out} / {err}")
    spec = {}
    for line in out.splitlines():
        if line.strip().startswith("{"):
            spec = json.loads(line.strip())
    print(f"[+] Verified server hardware spec: {spec}", flush=True)
    if spec.get("vcpus", 0) < 2:
        raise RuntimeError(f"Expected 2 vCPUs after resize, but found vcpus={spec.get('vcpus')}. Aborting.")
    return spec


async def run_cross_trial(
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
    trial_id = f"2vcpu2gb_run{run_num}_{profile}_{tier_name}_{target_conns}"
    print(
        f"[*] [2vCPU/2GB $18/mo | Run {run_num}] profile={profile} | tier={tier_name} "
        f"(target_conns={target_conns}, workers={active_req_workers})...",
        flush=True,
    )

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
    summary["droplet_tier"] = "2vcpu_2gb_18usd_mo"

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

    CROSS_DIR.mkdir(parents=True, exist_ok=True)
    (CROSS_DIR / f"{trial_id}.probes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in visitor_rows),
        encoding="utf-8",
    )
    (CROSS_DIR / f"{trial_id}.summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print(
        f"    -> [2vCPU/2GB $18/mo | Run {run_num} | {profile} @ {target_conns}] "
        f"Active-Flood <=500ms={af['within_500ms_pct']:.1f}% ({af['within_500ms']}/40, p50={af['p50_ms']}ms, p95={af['p95_ms']}ms) | "
        f"PeakSrvConns={summary['achieved_connection_metrics']['server_peak_sockets_443_observed']} | "
        f"ActiveSSH_OK={not summary['explicit_failure_criteria']['criterion_active_load_ssh_failed']}",
        flush=True,
    )
    return summary


def summarize_3runs(matching: list[dict]) -> dict:
    matching.sort(key=lambda x: x["run_number"])
    w500_pcts = [t["phases"]["active_flood"]["within_500ms_pct"] for t in matching]
    w500_counts = [t["phases"]["active_flood"]["within_500ms"] for t in matching]
    http200_counts = [t["phases"]["active_flood"]["valid_http200"] for t in matching]
    p50s = [t["phases"]["active_flood"]["p50_ms"] for t in matching]
    p95s = [t["phases"]["active_flood"]["p95_ms"] for t in matching]
    p99s = [t["phases"]["active_flood"]["p99_ms"] for t in matching]
    srv_peaks = [t["achieved_connection_metrics"]["server_peak_sockets_443_observed"] for t in matching]
    cli_peaks = [t["achieved_connection_metrics"]["client_peak_established_connections"] for t in matching]
    below_80_flags = [t["explicit_failure_criteria"]["criterion_500ms_overall_active_flood_below_80pct"] for t in matching]
    win5s_flags = [t["explicit_failure_criteria"]["criterion_500ms_sliding_5s_window_below_80pct"] for t in matching]
    active_ssh_fail_flags = [t["explicit_failure_criteria"]["criterion_active_load_ssh_failed"] for t in matching]

    return {
        "runs_completed": len(matching),
        "per_run_active_flood_http200_count_of_40": http200_counts,
        "pooled_active_flood_http200_count_of_120": sum(http200_counts),
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
        "runs_with_sliding_5s_window_below_80pct": sum(1 for f in win5s_flags if f),
        "runs_with_active_load_ssh_failure": sum(1 for f in active_ssh_fail_flags if f),
    }


async def main():
    hw_spec = verify_2vcpu_2gb_hardware()
    body_bytes, expected_sha256 = await get_expected_homepage_hash()
    print(f"[+] Preflight verified: homepage={body_bytes} bytes, sha256={expected_sha256}", flush=True)

    large_trials = []
    tiers_3run = [
        ("tier2_350conns", 350, 12),
        ("tier3_800conns", 800, 12),
    ]
    rotations = [
        (1, ["reference", "tuned"]),
        (2, ["tuned", "reference"]),
        (3, ["reference", "tuned"]),
    ]

    try:
        for run_num, prof_order in rotations:
            for tier_name, target_conns, active_workers in tiers_3run:
                for profile in prof_order:
                    s = await run_cross_trial(run_num, profile, tier_name, target_conns, active_workers, expected_sha256)
                    large_trials.append(s)
                    await asyncio.sleep(2.0)

        # Also run Tier 4 (1,200 offered connections, generator-limited) on the 2vCPU/2GB droplet
        for profile in ("reference", "tuned"):
            s = await run_cross_trial(1, profile, "tier4_1200conns", 1200, 16, expected_sha256)
            large_trials.append(s)
            await asyncio.sleep(2.0)
    finally:
        print("[*] Restoring production Step 1 configuration on server...", flush=True)
        ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py restore", port=22, timeout=15.0)

    # Load saved $4/mo (1 vCPU / 512 MB) results for comparison
    small_repeat = json.loads(STEP2_REPEAT_JSON.read_text(encoding="utf-8"))
    small_run1 = json.loads(STEP2_RUN1_JSON.read_text(encoding="utf-8"))

    comparison = {}
    for tier_name in ("tier2_350conns", "tier3_800conns"):
        comparison[tier_name] = {
            "small_4usd_1vcpu_512mb_argus": small_repeat["aggregate_3run_summary"][tier_name]["argus"],
            "small_4usd_1vcpu_512mb_reference": small_repeat["aggregate_3run_summary"][tier_name]["reference"],
            "small_4usd_1vcpu_512mb_tuned": small_repeat["aggregate_3run_summary"][tier_name]["tuned"],
            "large_18usd_2vcpu_2gb_reference": summarize_3runs(
                [t for t in large_trials if t["tier"] == tier_name and t["profile"] == "reference"]
            ),
            "large_18usd_2vcpu_2gb_tuned": summarize_3runs(
                [t for t in large_trials if t["tier"] == tier_name and t["profile"] == "tuned"]
            ),
        }

    # Tier 4 single-run comparison
    small_t4_argus = next(t for t in small_run1["trials"] if t["tier"] == "tier4_1200conns" and t["profile"] == "argus")
    large_t4_ref = next(t for t in large_trials if t["tier"] == "tier4_1200conns" and t["profile"] == "reference")
    large_t4_tuned = next(t for t in large_trials if t["tier"] == "tier4_1200conns" and t["profile"] == "tuned")

    report = {
        "experiment": "Cross-Size Comparison — $4/mo (1 vCPU, 512 MB) with Argus vs. $18/mo (2 vCPU, 2 GB) without Argus",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "monthly_cost_comparison": {
            "small_with_argus_usd_per_month": 4.0,
            "large_without_argus_usd_per_month": 18.0,
            "monthly_saving_usd_per_instance": 14.0,
            "annual_saving_usd_per_instance": 168.0,
            "cost_reduction_pct": 77.78,
        },
        "verified_large_droplet_hardware_spec": hw_spec,
        "three_run_comparison_350_and_800": comparison,
        "tier4_1200_attempted_single_run_comparison": {
            "small_4usd_1vcpu_512mb_argus": {
                "within_500ms_pct": small_t4_argus["phases"]["active_flood"]["within_500ms_pct"],
                "within_500ms_count_of_40": small_t4_argus["phases"]["active_flood"]["within_500ms"],
                "p50_ms": small_t4_argus["phases"]["active_flood"]["p50_ms"],
                "p95_ms": small_t4_argus["phases"]["active_flood"]["p95_ms"],
                "p99_ms": small_t4_argus["phases"]["active_flood"]["p99_ms"],
                "server_peak_sockets_443": small_t4_argus["achieved_connection_metrics"]["server_peak_sockets_443_observed"],
                "active_load_ssh_failed": small_t4_argus["explicit_failure_criteria"]["criterion_active_load_ssh_failed"],
            },
            "large_18usd_2vcpu_2gb_reference": {
                "within_500ms_pct": large_t4_ref["phases"]["active_flood"]["within_500ms_pct"],
                "within_500ms_count_of_40": large_t4_ref["phases"]["active_flood"]["within_500ms"],
                "p50_ms": large_t4_ref["phases"]["active_flood"]["p50_ms"],
                "p95_ms": large_t4_ref["phases"]["active_flood"]["p95_ms"],
                "p99_ms": large_t4_ref["phases"]["active_flood"]["p99_ms"],
                "server_peak_sockets_443": large_t4_ref["achieved_connection_metrics"]["server_peak_sockets_443_observed"],
                "active_load_ssh_failed": large_t4_ref["explicit_failure_criteria"]["criterion_active_load_ssh_failed"],
            },
            "large_18usd_2vcpu_2gb_tuned": {
                "within_500ms_pct": large_t4_tuned["phases"]["active_flood"]["within_500ms_pct"],
                "within_500ms_count_of_40": large_t4_tuned["phases"]["active_flood"]["within_500ms"],
                "p50_ms": large_t4_tuned["phases"]["active_flood"]["p50_ms"],
                "p95_ms": large_t4_tuned["phases"]["active_flood"]["p95_ms"],
                "p99_ms": large_t4_tuned["phases"]["active_flood"]["p99_ms"],
                "server_peak_sockets_443": large_t4_tuned["achieved_connection_metrics"]["server_peak_sockets_443_observed"],
                "active_load_ssh_failed": large_t4_tuned["explicit_failure_criteria"]["criterion_active_load_ssh_failed"],
            },
        },
        "large_droplet_trials": large_trials,
    }
    OUT_CROSS_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[+] Saved cross-size comparison report to {OUT_CROSS_JSON}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
