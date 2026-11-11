#!/usr/bin/env python3
"""
Runs the remaining deep stress & sub-threshold boundary cells on the $4/mo (1 vCPU / 512 MB)
droplet (`<ORIGIN_SERVER_IP>`) so that every comparison table on `/argus` has complete measured data
and zero blank (`—`) cells:
  1. `tier3_heavy_800conns_32workers` (800 conns + 32 heavy HTTPS workers): `reference`, `tuned`, `argus`
  2. `subthreshold_150conns_24workers` (150 conns < 200 threshold + 24 heavy HTTPS workers): `reference`, `tuned`, `argus`
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import run_step2_pressure_suite as s2
from reanalyze_step2_and_timings import compute_500ms_recovery_from_timeline

s2.ORIGIN_IP = "<ORIGIN_SERVER_IP>"

BASE_DIR = Path(__file__).parent
DEEP_4USD_DIR = BASE_DIR / "cross_size_1vcpu_512mb_deep_runs"
OUT_DEEP_JSON = BASE_DIR / "cross_size_4usd_18usd_48usd_deep_comparison.json"


def verify_1vcpu_512mb_hardware() -> dict:
    rc, out, err, _ = s2.ssh_cmd(
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
    print(f"[+] Verified $4/mo server hardware spec: {spec}", flush=True)
    return spec


async def run_4usd_deep_trial(
    run_num: int,
    profile: str,
    tier_name: str,
    target_conns: int,
    active_req_workers: int,
    expected_sha256: str,
) -> dict:
    rc, out, err, _ = s2.ssh_cmd(
        f"python3 <BENCHMARK_HARNESS>/step2_server_control.py {profile} 25 200",
        port=22,
        timeout=15.0,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to set profile {profile}: {out} / {err}")

    await asyncio.sleep(1.5)
    trial_id = f"1vcpu512mb_run{run_num}_{profile}_{tier_name}_{target_conns}c_{active_req_workers}w"
    print(
        f"[*] [1vCPU/512MB $4/mo | Run {run_num}] profile={profile} | tier={tier_name} "
        f"(target_conns={target_conns}, workers={active_req_workers})...",
        flush=True,
    )

    visitor_task = asyncio.create_task(s2.run_visitor_probes(trial_id, expected_sha256))
    flood_task = asyncio.create_task(s2.run_connection_pressure_generator(target_conns, active_req_workers))
    ssh_task = asyncio.create_task(s2.run_ssh_management_probes())

    visitor_rows, flood_stats, ssh_probes = await asyncio.gather(visitor_task, flood_task, ssh_task)

    _, status_out, _, _ = s2.ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py status", port=22, timeout=10.0)
    host_after = {}
    for line in status_out.splitlines():
        if line.strip().startswith("{"):
            host_after = json.loads(line.strip())

    s2.ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py clear_bans", port=22, timeout=10.0)

    summary = s2.evaluate_trial(profile, tier_name, target_conns, visitor_rows, flood_stats, ssh_probes, host_after)
    summary["run_number"] = run_num
    summary["droplet_tier"] = "1vcpu_512mb_4usd_mo"

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

    DEEP_4USD_DIR.mkdir(parents=True, exist_ok=True)
    (DEEP_4USD_DIR / f"{trial_id}.probes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in visitor_rows),
        encoding="utf-8",
    )
    (DEEP_4USD_DIR / f"{trial_id}.summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print(
        f"    -> [1vCPU/512MB $4/mo | Run {run_num} | {profile} @ {target_conns}c/{active_req_workers}w] "
        f"Active-Flood <=500ms={af['within_500ms_pct']:.1f}% ({af['within_500ms']}/40, p50={af['p50_ms']}ms, p95={af['p95_ms']}ms) | "
        f"Flood200={flood_stats['active_https_200']}/{flood_stats['active_https_sent']} | "
        f"PeakSrvConns={summary['achieved_connection_metrics']['server_peak_sockets_443_observed']} | "
        f"ActiveSSH_OK={not summary['explicit_failure_criteria']['criterion_active_load_ssh_failed']}",
        flush=True,
    )
    return summary


async def main():
    hw_spec = verify_1vcpu_512mb_hardware()
    body_bytes, expected_sha256 = await s2.get_expected_homepage_hash()
    print(f"[+] Preflight verified: homepage={body_bytes} bytes, sha256={expected_sha256}", flush=True)

    trials_4usd_deep = []
    try:
        for profile in ("reference", "tuned", "argus"):
            s = await run_4usd_deep_trial(1, profile, "tier3_heavy_800conns_32workers", 800, 32, expected_sha256)
            trials_4usd_deep.append(s)
            await asyncio.sleep(2.0)

        for profile in ("reference", "tuned", "argus"):
            s = await run_4usd_deep_trial(1, profile, "subthreshold_150conns_24workers", 150, 24, expected_sha256)
            trials_4usd_deep.append(s)
            await asyncio.sleep(2.0)
    finally:
        print("[*] Restoring production Step 1 configuration on $4/mo server...", flush=True)
        s2.ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py restore", port=22, timeout=15.0)

    if OUT_DEEP_JSON.exists():
        deep_doc = json.loads(OUT_DEEP_JSON.read_text(encoding="utf-8"))
    else:
        deep_doc = {}
    deep_doc["verified_4usd_deep_hardware_spec"] = hw_spec
    deep_doc["all_4usd_deep_stress_and_boundary_trials"] = trials_4usd_deep
    deep_doc["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
    OUT_DEEP_JSON.write_text(json.dumps(deep_doc, indent=2), encoding="utf-8")
    print(f"[+] Updated {OUT_DEEP_JSON} with 6 complete $4/mo deep & boundary trials.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
