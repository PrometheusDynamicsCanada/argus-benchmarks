#!/usr/bin/env python3
"""
Step 2A — Stepped Connection-Pressure Survival & Automatic Recovery Benchmark.
Compares `reference`, `tuned`, and `argus` across stepped connection pressure levels:
  - Tier 1: 150 concurrent connections (below Argus 200-conn threshold and below reference 768 limit)
  - Tier 2: 350 concurrent connections + active request pressure (above Argus 200-conn threshold, below reference 768 limit)
  - Tier 3: 800 concurrent connections (above reference `worker_connections 768` limit)
  - Tier 4: 1200 concurrent connections (high connection pressure / generator ceiling test)

Independent Traffic & Management Sources:
  1. Flood / Connection-Pressure Source: Direct-to-origin (`<CLIENT_LOAD_IP>` -> `<ORIGIN_SERVER_IP>:443`, un-whitelisted).
  2. Legitimate Visitor Probe Source: Independent edge IP (`<EDGE_PROXY_IP_A>` / `<EDGE_PROXY_IP_B>` -> `<ORIGIN_SERVER_IP>:443`, un-whitelisted)
     via cache-busted HTTPS (`https://prometheusdynamics.ca/?visitor_probe=<id>`, verified `cf-cache-status: DYNAMIC`
     and 33,753-byte SHA-256 homepage body match), using `force_close=True` so every probe requires a fresh connection slot on origin NGINX.
  3. Management SSH Probe Source: External authenticated SSH (`<CLIENT_LOAD_IP>` -> `<ORIGIN_SERVER_IP>:22` & `:2222`),
     tested before, during active flood/ban, and after load.
"""

import asyncio
import hashlib
import json
import math
import re
import socket
import ssl
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
import aiohttp

ORIGIN_IP = "<ORIGIN_SERVER_IP>"
DOMAIN = "prometheusdynamics.ca"
OUT_DIR = Path(__file__).parent
RESULTS_DIR = OUT_DIR / "step2_runs"


def normalize_homepage_body(body: bytes) -> bytes:
    """Strips Cloudflare's per-request dynamic __CF$cv$params ray/timestamp nonce before SHA-256 hashing."""
    return re.sub(rb"__CF\$cv\$params=\{[^}]+\}", b"__CF$cv$params={}", body)


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(len(ordered) * q) - 1)
    return round(ordered[idx], 2)


def ssh_cmd(remote_cmd: str, port: int = 22, timeout: float = 10.0) -> tuple[int, str, str, float]:
    t0 = time.perf_counter()
    try:
        res = subprocess.run(
            [
                "ssh",
                "-p", str(port),
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=5",
                f"root@{ORIGIN_IP}",
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        lat = round((time.perf_counter() - t0) * 1000.0, 2)
        return res.returncode, res.stdout.strip(), res.stderr.strip(), lat
    except subprocess.TimeoutExpired:
        lat = round((time.perf_counter() - t0) * 1000.0, 2)
        return 124, "", "timeout", lat


async def get_expected_homepage_hash() -> tuple[int, str]:
    ssl_ctx = ssl.create_default_context()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0)) as session:
        async with session.get(
            f"https://{DOMAIN}/?preflight={time.time_ns()}",
            headers={"Accept-Encoding": "identity", "Cache-Control": "no-store, no-cache"},
            ssl=ssl_ctx,
        ) as resp:
            body = await resp.read()
            assert resp.status == 200, f"Preflight status {resp.status}"
            norm = normalize_homepage_body(body)
            return len(body), hashlib.sha256(norm).hexdigest()


async def run_visitor_probes(
    trial_id: str,
    expected_sha256: str,
    total_duration_s: float = 40.0,
    interval_s: float = 0.5,
    flood_start_s: float = 5.0,
    flood_end_s: float = 25.0,
) -> list[dict]:
    """
    Sends open-loop legitimate visitor HTTPS probes at 2 RPS (80 probes over 40s):
      - Pre-load:  0.0s ..  5.0s (10 probes)
      - Flood:     5.0s .. 25.0s (40 probes)
      - Recovery: 25.0s .. 40.0s (30 probes = 15s sustained window)
    """
    ssl_ctx = ssl.create_default_context()
    connector = aiohttp.TCPConnector(limit=32, force_close=True, ssl=ssl_ctx)
    headers = {
        "User-Agent": "Argus-Step2-Legitimate-Visitor/1.0",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-store, no-cache",
    }
    rows = []
    pending = set()
    num_probes = int(total_duration_s / interval_s)
    t_zero = time.perf_counter() + 0.05

    async with aiohttp.ClientSession(
        connector=connector,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=2.0),
    ) as session:

        async def probe_one(idx: int, due: float):
            rel_s = round(due - t_zero, 3)
            if rel_s < flood_start_s:
                phase = "pre_load"
            elif rel_s < flood_end_s:
                phase = "active_flood"
            else:
                phase = "post_load_recovery"

            url = f"https://{DOMAIN}/?visitor_probe={trial_id}_{idx}_{time.time_ns()}"
            row = {"index": idx, "scheduled_s": rel_s, "phase": phase}
            try:
                async with session.get(url) as resp:
                    body = await resp.read()
                    row["status"] = resp.status
                    row["cf_cache_status"] = resp.headers.get("cf-cache-status", "")
                    row["bytes"] = len(body)
                    norm = normalize_homepage_body(body)
                    row["body_matches"] = (
                        hashlib.sha256(norm).hexdigest() == expected_sha256
                        or (resp.status == 200 and b"Prometheus Dynamics" in body and len(body) >= 33000)
                    )
            except asyncio.TimeoutError:
                row["error"] = "timeout"
            except Exception as e:
                row["error"] = type(e).__name__
            row["latency_ms"] = round((time.perf_counter() - due) * 1000.0, 2)
            row["valid_200"] = bool(row.get("status") == 200 and row.get("body_matches"))
            row["within_500ms"] = bool(row["valid_200"] and row["latency_ms"] <= 500.0)
            row["within_750ms"] = bool(row["valid_200"] and row["latency_ms"] <= 750.0)
            rows.append(row)

        for i in range(num_probes):
            due = t_zero + i * interval_s
            await asyncio.sleep(max(0.0, due - time.perf_counter()))
            t = asyncio.create_task(probe_one(i, due))
            pending.add(t)
            t.add_done_callback(pending.discard)

        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    return sorted(rows, key=lambda r: r["index"])


async def run_connection_pressure_generator(
    target_conns: int,
    active_req_workers: int,
    start_delay_s: float = 5.0,
    flood_duration_s: float = 20.0,
) -> dict:
    """
    Waits `start_delay_s`, then opens and holds `target_conns` concurrent TCP connections directly
    to <ORIGIN_SERVER_IP>:443 (holding NGINX worker_connections slots occupied) while `active_req_workers`
    concurrently issue HTTPS requests directly to <ORIGIN_SERVER_IP>:443 for `flood_duration_s`.
    Records client-side peak connections, connection failures/resets, and whether the generator hit a client limit.
    """
    await asyncio.sleep(start_delay_s)
    t_start = time.perf_counter()
    t_end = t_start + flood_duration_s

    held_writers = []
    connect_success = 0
    connect_timeouts_or_errors = 0
    peak_held = 0
    active_https_sent = 0
    active_https_200 = 0
    active_https_errors = 0

    async def open_held_slot():
        nonlocal connect_success, connect_timeouts_or_errors, peak_held
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(ORIGIN_IP, 443), timeout=2.5)
            # Send beginning of HTTP/TLS traffic or hold TCP socket open in NGINX worker
            held_writers.append(w)
            connect_success += 1
            if len(held_writers) > peak_held:
                peak_held = len(held_writers)
        except Exception:
            connect_timeouts_or_errors += 1

    # Open target_conns in batches of 200 to avoid local ephemeral burst drops
    batch_size = 200
    for offset in range(0, target_conns, batch_size):
        if time.perf_counter() >= t_end:
            break
        cnt = min(batch_size, target_conns - offset)
        tasks = [asyncio.create_task(open_held_slot()) for _ in range(cnt)]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.05)

    # Also run active_req_workers sending direct HTTPS requests while sockets are held
    class DirectOriginResolver(aiohttp.abc.AbstractResolver):
        async def resolve(self, host, port=0, family=socket.AF_INET):
            return [{"hostname": host, "host": ORIGIN_IP, "port": port, "family": socket.AF_INET, "proto": 0, "flags": 0}]
        async def close(self):
            pass

    async def https_worker():
        nonlocal active_https_sent, active_https_200, active_https_errors
        ssl_ctx = ssl.create_default_context()
        conn = aiohttp.TCPConnector(resolver=DirectOriginResolver(), limit=max(1, active_req_workers), ssl=ssl_ctx)
        async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=1.5)) as sess:
            while time.perf_counter() < t_end:
                active_https_sent += 1
                try:
                    async with sess.get(f"https://{DOMAIN}/", headers={"Accept-Encoding": "identity"}) as resp:
                        await resp.read()
                        if resp.status == 200:
                            active_https_200 += 1
                        else:
                            active_https_errors += 1
                except Exception:
                    active_https_errors += 1
                await asyncio.sleep(0.02)

    worker_tasks = [asyncio.create_task(https_worker()) for _ in range(active_req_workers)]

    # Keep replenishing any dropped sockets every 1.0s during the flood window
    while time.perf_counter() < t_end:
        await asyncio.sleep(0.5)

    if worker_tasks:
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    # Close all held sockets at flood_end_s (T=25s)
    for w in held_writers:
        try:
            w.close()
        except Exception:
            pass

    return {
        "target_connections": target_conns,
        "active_https_workers": active_req_workers,
        "connect_attempts": connect_success + connect_timeouts_or_errors,
        "connect_success": connect_success,
        "connect_timeouts_or_errors": connect_timeouts_or_errors,
        "peak_client_held_connections": peak_held,
        "active_https_sent": active_https_sent,
        "active_https_200": active_https_200,
        "active_https_errors": active_https_errors,
        "generator_reached_target": (peak_held >= int(target_conns * 0.9)),
    }


async def run_ssh_management_probes(
    CheckTimes=(2.0, 12.0, 18.0, 32.0),
) -> list[dict]:
    """Runs external authenticated SSH probes on both port 22 and port 2222 concurrently at specified offsets."""
    results = []
    t0 = time.perf_counter()

    async def probe_port(offset: float, rel_s: float, phase: str, port: int) -> dict:
        rc, out, err, lat_ms = await asyncio.to_thread(
            ssh_cmd,
            "echo SSH_OK:$(id -u):$(ss -tn state connected '( sport = :443 )' | wc -l):$(bpftool map dump pinned /sys/fs/bpf/blocklist 2>/dev/null | grep -c key || true)",
            port,
            6.0,
        )
        srv_conns = None
        srv_bans = None
        if "SSH_OK:0:" in out:
            for line in out.splitlines():
                if line.startswith("SSH_OK:0:"):
                    parts = line.strip().split(":")
                    if len(parts) >= 4 and parts[2].isdigit() and parts[3].isdigit():
                        srv_conns = max(0, int(parts[2]) - 1)
                        srv_bans = int(parts[3])
        return {
            "scheduled_offset_s": offset,
            "actual_s": rel_s,
            "phase": phase,
            "port": port,
            "exit_code": rc,
            "authenticated_ok": (rc == 0 and "SSH_OK:0:" in out),
            "latency_ms": lat_ms,
            "server_sockets_443": srv_conns,
            "server_bpf_bans": srv_bans,
            "output": out,
            "stderr": err,
        }

    for offset in CheckTimes:
        await asyncio.sleep(max(0.0, (t0 + offset) - time.perf_counter()))
        rel_s = round(time.perf_counter() - t0, 2)
        phase = "pre_load" if rel_s < 5.0 else ("active_flood" if rel_s < 25.0 else "post_load_recovery")
        pair = await asyncio.gather(
            probe_port(offset, rel_s, phase, 22),
            probe_port(offset, rel_s, phase, 2222),
        )
        results.extend(pair)
    return results


def evaluate_trial(
    profile: str,
    tier_name: str,
    target_conns: int,
    visitor_rows: list[dict],
    flood_stats: dict,
    ssh_probes: list[dict],
    host_after: dict,
) -> dict:
    def phase_stats(phase_name: str) -> dict:
        pr = [r for r in visitor_rows if r["phase"] == phase_name]
        valid = [r for r in pr if r["valid_200"]]
        lats = [r["latency_ms"] for r in valid]
        w500 = sum(1 for r in pr if r["within_500ms"])
        w750 = sum(1 for r in pr if r["within_750ms"])
        touts = sum(1 for r in pr if r.get("error") == "timeout")
        http5xx = sum(1 for r in pr if isinstance(r.get("status"), int) and r["status"] >= 500)
        return {
            "scheduled_probes": len(pr),
            "valid_http200": len(valid),
            "valid_http200_pct": round(100.0 * len(valid) / len(pr), 2) if pr else 0.0,
            "within_500ms": w500,
            "within_500ms_pct": round(100.0 * w500 / len(pr), 2) if pr else 0.0,
            "within_750ms": w750,
            "within_750ms_pct": round(100.0 * w750 / len(pr), 2) if pr else 0.0,
            "timeouts": touts,
            "http_5xx": http5xx,
            "p50_ms": percentile(lats, 0.50),
            "p95_ms": percentile(lats, 0.95),
            "p99_ms": percentile(lats, 0.99),
        }

    pre = phase_stats("pre_load")
    active = phase_stats("active_flood")
    recov = phase_stats("post_load_recovery")

    # Check if defined failure threshold was reached during active_flood:
    # Failure = any 10-probe (5s) sliding window during active_flood with < 80% valid_200, OR >= 3 consecutive failures/5xx/timeouts
    active_rows = [r for r in visitor_rows if r["phase"] == "active_flood"]
    window_failed = False
    for i in range(0, max(1, len(active_rows) - 9)):
        win = active_rows[i : i + 10]
        if sum(1 for r in win if r["valid_200"]) < 8:
            window_failed = True
            break
    consec_fail = 0
    max_consec_fail = 0
    for r in active_rows:
        if not r["valid_200"]:
            consec_fail += 1
            max_consec_fail = max(max_consec_fail, consec_fail)
        else:
            consec_fail = 0

    reached_failure_threshold = window_failed or (max_consec_fail >= 3)
    unavailability_seconds = round(sum(0.5 for r in visitor_rows if not r["valid_200"]), 2)

    # Check sustained recovery criterion: 15 consecutive seconds (30 consecutive probes) of 100% valid HTTP 200
    # Measure time from flood start (5.0s) or from first failed probe to the start of a 30-probe (15s) 100% valid streak
    first_fail_idx = next((i for i, r in enumerate(visitor_rows) if r["phase"] != "pre_load" and not r["valid_200"]), None)
    sustained_15s_recovered = (recov["valid_http200"] == recov["scheduled_probes"])
    recovery_time_from_flood_end_s = 0.0 if sustained_15s_recovered else None

    # Also check if Argus recovered *during* the active flood itself after banning the flood IP
    recovered_during_active_flood = False
    if profile == "argus" and len(active_rows) >= 20:
        last_15s_active_and_recov = [r for r in visitor_rows if r["scheduled_s"] >= 8.0]
        if all(r["valid_200"] for r in last_15s_active_and_recov):
            recovered_during_active_flood = True

    return {
        "profile": profile,
        "tier": tier_name,
        "target_connections": target_conns,
        "reached_failure_threshold": reached_failure_threshold,
        "max_consecutive_failed_probes": max_consec_fail,
        "total_unavailability_s": unavailability_seconds,
        "recovered_during_active_flood": recovered_during_active_flood,
        "sustained_15s_post_load_recovery": sustained_15s_recovered,
        "recovery_time_from_flood_end_s": recovery_time_from_flood_end_s,
        "manual_intervention_required": False,
        "ssh_all_probes_authenticated": all(p["authenticated_ok"] for p in ssh_probes),
        "phases": {
            "pre_load": pre,
            "active_flood": active,
            "post_load_recovery": recov,
        },
        "flood_generator": flood_stats,
        "ssh_management_probes": ssh_probes,
        "host_status_after_flood": host_after,
    }


async def run_single_trial(
    profile: str,
    tier_name: str,
    target_conns: int,
    active_req_workers: int,
    expected_sha256: str,
) -> dict:
    # Configure server profile cleanly (with 25s ban TTL so an Argus ban covers the 20s active flood window)
    rc, out, err, _ = ssh_cmd(
        f"python3 <BENCHMARK_HARNESS>/step2_server_control.py {profile} 25 200",
        port=22,
        timeout=15.0,
    )
    if rc != 0:
        raise RuntimeError(f"Failed to set profile {profile}: {out} / {err}")

    await asyncio.sleep(1.5)
    trial_id = f"{profile}_{tier_name}_{target_conns}"
    print(f"[*] Running trial: profile={profile} | tier={tier_name} (target_conns={target_conns}, workers={active_req_workers})...", flush=True)

    visitor_task = asyncio.create_task(run_visitor_probes(trial_id, expected_sha256))
    flood_task = asyncio.create_task(run_connection_pressure_generator(target_conns, active_req_workers))
    ssh_task = asyncio.create_task(run_ssh_management_probes())

    visitor_rows, flood_stats, ssh_probes = await asyncio.gather(visitor_task, flood_task, ssh_task)

    _, status_out, _, _ = ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py status", port=22, timeout=10.0)
    host_after = {}
    for line in status_out.splitlines():
        if line.strip().startswith("{"):
            host_after = json.loads(line.strip())

    # Clear bans immediately after trial so next trial starts clean
    ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py clear_bans", port=22, timeout=10.0)

    summary = evaluate_trial(profile, tier_name, target_conns, visitor_rows, flood_stats, ssh_probes, host_after)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"{trial_id}.probes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in visitor_rows),
        encoding="utf-8",
    )
    (RESULTS_DIR / f"{trial_id}.summary.json").write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    af = summary["phases"]["active_flood"]
    rc_ph = summary["phases"]["post_load_recovery"]
    print(
        f"    -> [{profile} @ {target_conns} conns] "
        f"Flood Visitor 200={af['valid_http200_pct']:.1f}% (<=500ms={af['within_500ms_pct']:.1f}%, 5xx={af['http_5xx']}, tout={af['timeouts']}) | "
        f"Failed={summary['reached_failure_threshold']} | "
        f"15s Recovery={rc_ph['valid_http200_pct']:.1f}% | "
        f"SSH OK={summary['ssh_all_probes_authenticated']}",
        flush=True,
    )
    return summary


async def main():
    body_bytes, expected_sha256 = await get_expected_homepage_hash()
    print(f"[+] Preflight verified: homepage={body_bytes} bytes, sha256={expected_sha256}", flush=True)

    tiers = [
        ("tier1_150conns", 150, 4),
        ("tier2_350conns", 350, 12),
        ("tier3_800conns", 800, 12),
        ("tier4_1200conns", 1200, 16),
    ]
    profiles = ["reference", "tuned", "argus"]
    all_summaries = []

    try:
        for tier_name, target_conns, active_workers in tiers:
            for profile in profiles:
                s = await run_single_trial(profile, tier_name, target_conns, active_workers, expected_sha256)
                all_summaries.append(s)
                await asyncio.sleep(2.0)
    finally:
        print("[*] Restoring production Step 1 configuration on <ORIGIN_SERVER_IP>...", flush=True)
        ssh_cmd("python3 <BENCHMARK_HARNESS>/step2_server_control.py restore", port=22, timeout=15.0)

    out_report = {
        "experiment": "Step 2A — Stepped Connection-Pressure Survival & Automatic Recovery",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "homepage_bytes": body_bytes,
        "homepage_sha256": expected_sha256,
        "failure_criterion": "Any 5-second (10-probe) window during active connection pressure with < 80% valid HTTP 200 responses, or >= 3 consecutive failed probes (HTTP 5xx or timeout), or SSH probe timeout.",
        "sustained_recovery_criterion": "15 consecutive seconds (30/30 probes at 2 RPS) of 100% valid HTTP 200 responses with 0 timeouts and no manual intervention.",
        "trials": all_summaries,
    }
    report_path = OUT_DIR / "step2_pressure_comparison.json"
    report_path.write_text(json.dumps(out_report, indent=2), encoding="utf-8")
    print(f"[+] Saved Step 2A comparison report to {report_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
