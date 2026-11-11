#!/usr/bin/env python3
"""
Deep Cross-Size & Boundary Suite on $48/mo (4 vCPU / 8 GB RAM) Droplet (`nyc1`):
Finds both the TRUE VALUE and the TRUE LIMITATION of Argus across hardware tiers ($4/mo vs. $18/mo vs. $48/mo):

1. Part A — Standard Connection-Pressure Comparison (350 & 800 connections + 12 HTTPS workers, 3 rotated runs)
   on $48/mo (4 vCPU / 8 GB) for `reference` and `tuned` (without Argus), plus `argus` on $48/mo,
   compared directly against the $4/mo (1 vCPU / 512 MB) `argus` results and $18/mo (2 vCPU / 2 GB) results.
2. Part B — High-Intensity Connection + TLS Request Pressure (800 connections + 32 HTTPS flood workers,
   and 1,200 attempted connections + 24 HTTPS flood workers) on $48/mo (4 vCPU / 8 GB):
   Tests whether 4 vCPUs can brute-force a heavier single-source flood without Argus vs. with Argus.
3. Part C — Legitimate Traffic Scaling Boundary (Sub-Threshold Multi-Worker Load, 0 Bans):
   Tests sub-threshold legitimate concurrency (150 connections < 200 threshold with 16 and 32 concurrent
   HTTPS workers) to show the true hardware limitation where 4 vCPUs ($48/mo) genuinely outperform
   1 vCPU ($4/mo) because all traffic is below threshold and no packets are dropped by XDP.
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
from run_cross_size_2vcpu_2gb_suite import summarize_3runs

BASE_DIR = Path(__file__).parent
DEEP_DIR = BASE_DIR / "cross_size_4vcpu_8gb_runs"
STEP2_RUN1_JSON = BASE_DIR / "step2_pressure_comparison.json"
STEP2_REPEAT_JSON = BASE_DIR / "step2_350_800_repeatability.json"
CROSS_18_JSON = BASE_DIR / "cross_size_4usd_argus_vs_18usd_without_argus.json"
OUT_DEEP_JSON = BASE_DIR / "cross_size_4usd_18usd_48usd_deep_comparison.json"


def verify_4vcpu_8gb_hardware() -> dict:
    rc, out, err, _ = ssh_cmd(
        "python3 -c \"import json, os; "
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
    if spec.get("vcpus", 0) < 4:
        raise RuntimeError(f"Expected 4 vCPUs after resize, but found vcpus={spec.get('vcpus')}. Aborting.")
    return spec


async def run_deep_trial(
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
    trial_id = f"4vcpu8gb_run{run_num}_{profile}_{tier_name}_{target_conns}c_{active_req_workers}w"
    print(
        f"[*] [4vCPU/8GB $48/mo | Run {run_num}] profile={profile} | tier={tier_name} "
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
    summary["droplet_tier"] = "4vcpu_8gb_48usd_mo"

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

    DEEP_DIR.mkdir(parents=True, exist_ok=True)
    (DEEP_DIR / f"{trial_id}.probes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in visitor_rows),
        encoding="utf-8",
    )
    (DEEP_DIR / f"{trial_id}.summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print(
        f"    -> [4vCPU/8GB $48/mo | Run {run_num} | {profile} @ {target_conns}c/{active_req_workers}w] "
        f"Active-Flood <=500ms={af['within_500ms_pct']:.1f}% ({af['within_500ms']}/40, p50={af['p50_ms']}ms, p95={af['p95_ms']}ms) | "
        f"Flood200={flood_stats['active_https_200']}/{flood_stats['active_https_sent']} | "
        f"PeakSrvConns={summary['achieved_connection_metrics']['server_peak_sockets_443_observed']} | "
        f"ActiveSSH_OK={not summary['explicit_failure_criteria']['criterion_active_load_ssh_failed']}",
        flush=True,
    )
    return summary


async def main():
    hw_spec = verify_4vcpu_8gb_hardware()
    body_bytes, expected_sha256 = await get_expected_homepage_hash()
    print(f"[+] Preflight verified: homepage={body_bytes} bytes, sha256={expected_sha256}", flush=True)

    trials_48usd = []
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
        # Part A: 3-run rotated repeatability at 350 and 800 connections (12 workers) for reference & tuned
        for run_num, prof_order in rotations:
            for tier_name, target_conns, active_workers in tiers_3run:
                for profile in prof_order:
                    s = await run_deep_trial(run_num, profile, tier_name, target_conns, active_workers, expected_sha256)
                    trials_48usd.append(s)
                    await asyncio.sleep(2.0)

        # Also test Argus on the $48/mo (4vCPU/8GB) droplet at 350 and 800 connections
        for tier_name, target_conns, active_workers in tiers_3run:
            s = await run_deep_trial(1, "argus", tier_name, target_conns, active_workers, expected_sha256)
            trials_48usd.append(s)
            await asyncio.sleep(2.0)

        # Part B: High-intensity flood (800 conns + 32 HTTPS workers, and 1,200 attempted conns + 16 workers)
        for profile in ("reference", "tuned", "argus"):
            s = await run_deep_trial(1, profile, "tier3_heavy_800conns_32workers", 800, 32, expected_sha256)
            trials_48usd.append(s)
            await asyncio.sleep(2.0)

        for profile in ("reference", "tuned"):
            s = await run_deep_trial(1, profile, "tier4_1200conns", 1200, 16, expected_sha256)
            trials_48usd.append(s)
            await asyncio.sleep(2.0)

        # Part C: Sub-threshold legitimate concurrency boundary (150 connections < 200 threshold, 24 active workers)
        # Here Argus does NOT ban (150 < 200), testing pure compute/TLS capacity of 4 vCPU ($48/mo) vs 1 vCPU ($4/mo)
        for profile in ("tuned", "argus"):
            s = await run_deep_trial(1, profile, "subthreshold_150conns_24workers", 150, 24, expected_sha256)
            trials_48usd.append(s)
            await asyncio.sleep(2.0)

    finally:
        print("[*] Restoring production Step 1 configuration on server...", flush=True)
        ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py restore", port=22, timeout=15.0)

    small_repeat = json.loads(STEP2_REPEAT_JSON.read_text(encoding="utf-8"))
    cross_18 = json.loads(CROSS_18_JSON.read_text(encoding="utf-8"))

    three_way_size_comparison = {}
    for tier_name in ("tier2_350conns", "tier3_800conns"):
        three_way_size_comparison[tier_name] = {
            "4usd_1vcpu_512mb_reference": small_repeat["aggregate_3run_summary"][tier_name]["reference"],
            "4usd_1vcpu_512mb_tuned": small_repeat["aggregate_3run_summary"][tier_name]["tuned"],
            "4usd_1vcpu_512mb_argus": small_repeat["aggregate_3run_summary"][tier_name]["argus"],
            "18usd_2vcpu_2gb_reference": cross_18["three_run_comparison_350_and_800"][tier_name]["large_18usd_2vcpu_2gb_reference"],
            "18usd_2vcpu_2gb_tuned": cross_18["three_run_comparison_350_and_800"][tier_name]["large_18usd_2vcpu_2gb_tuned"],
            "48usd_4vcpu_8gb_reference": summarize_3runs(
                [t for t in trials_48usd if t["tier"] == tier_name and t["profile"] == "reference"]
            ),
            "48usd_4vcpu_8gb_tuned": summarize_3runs(
                [t for t in trials_48usd if t["tier"] == tier_name and t["profile"] == "tuned"]
            ),
            "48usd_4vcpu_8gb_argus_single_run": summarize_3runs(
                [t for t in trials_48usd if t["tier"] == tier_name and t["profile"] == "argus"]
            ),
        }

    report = {
        "experiment": "Deep Cross-Size & Boundary Comparison — $4/mo (1 vCPU, 512 MB) vs. $18/mo (2 vCPU, 2 GB) vs. $48/mo (4 vCPU, 8 GB)",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "monthly_cost_tiers": {
            "small_1vcpu_512mb_usd_mo": 4.0,
            "medium_2vcpu_2gb_usd_mo": 18.0,
            "large_4vcpu_8gb_usd_mo": 48.0,
            "saving_4usd_vs_18usd_per_mo": 14.0,
            "saving_4usd_vs_48usd_per_mo": 44.0,
        },
        "verified_48usd_hardware_spec": hw_spec,
        "three_run_comparison_350_and_800": three_way_size_comparison,
        "all_48usd_trials": trials_48usd,
    }
    OUT_DEEP_JSON.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[+] Saved deep 3-size comparison report to {OUT_DEEP_JSON}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
