#!/usr/bin/env python3
"""
Re-analyzes the saved Step 2A (`step2_pressure_comparison.json` + `step2_runs/*.probes.jsonl`)
and Step 2B (`detection_timings_10reps.json`) records without altering any raw observations:
1. Preserves original `reached_failure_threshold` flag and adds explicitly named failure flags:
   - `criterion_original_http200_outage_failed` (< 80% valid HTTP 200 in 5s window or >= 3 consecutive non-200)
   - `criterion_active_load_ssh_failed` (any active-flood SSH probe at T=12s or T=18s failed/timed out)
   - `criterion_any_phase_ssh_failed` (any SSH probe across pre_load, active_flood, post_load_recovery failed)
   - `criterion_500ms_overall_active_flood_below_80pct` (< 80% of the 40 active-flood probes within 500 ms)
   - `criterion_500ms_sliding_5s_window_below_80pct` (any 10-probe / 5s window during active_flood with < 8/10 within 500 ms)
2. Separates post-load HTTP 200 service availability from recovery to the 500-ms latency target:
   - Computes exact probe-timeline recovery for `within_500ms` (30 consecutive probes = 15.0s within 500 ms),
     marking it `"not observed within the measurement window"` (`null`) when a 30-probe <=500ms streak does not occur.
3. Corrects `detection_timings_10reps.json` so missing XDP drop observations (`Rep 02`, `xdp_drops_observed == 0`)
   remain explicitly `null` (`"not observed in this repetition"`) rather than substituting `t4_sockets_severed_mono`,
   and reports separately:
   - Threshold observation -> BPF map update (`t1 -> t3`, n=10)
   - Threshold observation -> Socket-severance completion (`t1 -> t4`, n=10)
   - Threshold observation -> First observed XDP drop (`t1 -> t5`, n=9 observed, 1 not observed)
   - Threshold observation -> Later of socket severance and observed XDP drop (`max(t4, t5) - t1`, n=9 observed)
"""

import json
import statistics
from pathlib import Path

BASE_DIR = Path(__file__).parent
RUNS_DIR = BASE_DIR / "step2_runs"
STEP2_JSON = BASE_DIR / "step2_pressure_comparison.json"
TIMINGS_JSON = BASE_DIR / "detection_timings_10reps.json"


def compute_500ms_recovery_from_timeline(rows: list[dict]) -> dict:
    """
    Analyzes the 80-probe timeline (0.0s .. 39.5s, 0.5s spacing):
    - Pre-load: index 0..9 (0.0s..4.5s)
    - Active flood: index 10..49 (5.0s..24.5s)
    - Post-load: index 50..79 (25.0s..39.5s)
    """
    active_rows = [r for r in rows if r["phase"] == "active_flood"]
    post_rows = [r for r in rows if r["phase"] == "post_load_recovery"]

    # Sliding 10-probe (5s) window checks during active_flood
    min_10probe_valid200 = min(
        sum(1 for r in active_rows[i : i + 10] if r["valid_200"])
        for i in range(0, len(active_rows) - 9)
    )
    min_10probe_within500ms = min(
        sum(1 for r in active_rows[i : i + 10] if r["within_500ms"])
        for i in range(0, len(active_rows) - 9)
    )

    # Find first probe >= index 10 (flood start) where within_500ms is False
    degraded_indices = [r["index"] for r in rows if r["index"] >= 10 and not r["within_500ms"]]
    first_degraded_idx = degraded_indices[0] if degraded_indices else None
    last_degraded_active_idx = next(
        (r["index"] for r in reversed(active_rows) if not r["within_500ms"]),
        None,
    )

    # Check if there is any 30-consecutive-probe (15.0s) window of within_500ms == True
    # starting at or after flood start (index 10), and specifically after first_degraded_idx
    search_start = (first_degraded_idx + 1) if first_degraded_idx is not None else 10
    first_30probe_streak_start_idx = None
    for start_i in range(search_start, len(rows) - 29):
        window_30 = rows[start_i : start_i + 30]
        if all(r["within_500ms"] for r in window_30):
            first_30probe_streak_start_idx = start_i
            break

    # Also check whether all 30 post-load probes (index 50..79) were within 500ms
    post_load_all_30_http200 = all(r["valid_200"] for r in post_rows)
    post_load_all_30_within_500ms = all(r["within_500ms"] for r in post_rows)

    # Longest consecutive within_500ms streak from index 10..79
    cur_streak = 0
    max_streak = 0
    for r in rows[10:]:
        if r["within_500ms"]:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    if first_degraded_idx is None:
        # Never degraded below 500ms at any point during active_flood or post_load
        streak_status = "never_degraded_0ms"
        time_from_first_degradation_to_15s_streak_s = 0.0
    elif first_30probe_streak_start_idx is not None:
        streak_status = "observed"
        time_from_first_degradation_to_15s_streak_s = round(
            (rows[first_30probe_streak_start_idx]["scheduled_s"] - rows[first_degraded_idx]["scheduled_s"]),
            2,
        )
    else:
        streak_status = "not observed within the measurement window"
        time_from_first_degradation_to_15s_streak_s = None

    return {
        "min_5s_window_valid_http200_count_of_10": min_10probe_valid200,
        "min_5s_window_within_500ms_count_of_10": min_10probe_within500ms,
        "post_load_http200_count_of_30": sum(1 for r in post_rows if r["valid_200"]),
        "post_load_all_30_http200": post_load_all_30_http200,
        "post_load_within_500ms_count_of_30": sum(1 for r in post_rows if r["within_500ms"]),
        "post_load_all_30_within_500ms": post_load_all_30_within_500ms,
        "sustained_15s_500ms_streak_observed_after_degradation": (first_30probe_streak_start_idx is not None),
        "sustained_15s_500ms_streak_status": streak_status,
        "first_500ms_degradation_probe_s": rows[first_degraded_idx]["scheduled_s"] if first_degraded_idx is not None else None,
        "first_15s_500ms_streak_start_probe_s": (
            rows[first_30probe_streak_start_idx]["scheduled_s"]
            if first_30probe_streak_start_idx is not None
            else None
        ),
        "time_from_first_500ms_miss_to_15s_streak_start_s": time_from_first_degradation_to_15s_streak_s,
        "max_consecutive_within_500ms_probes_after_flood_onset": max_streak,
        "max_consecutive_within_500ms_duration_s": round(max_streak * 0.5, 1),
    }


def reanalyze_step2():
    data = json.loads(STEP2_JSON.read_text(encoding="utf-8"))
    for trial in data["trials"]:
        profile = trial["profile"]
        tier = trial["tier"]
        conns = trial["target_connections"]
        trial_id = f"{profile}_{tier}_{conns}"
        probes_file = RUNS_DIR / f"{trial_id}.probes.jsonl"
        rows = [json.loads(line) for line in probes_file.read_text(encoding="utf-8").splitlines() if line.strip()]

        tl = compute_500ms_recovery_from_timeline(rows)
        af = trial["phases"]["active_flood"]
        ssh_probes = trial["ssh_management_probes"]
        active_ssh = [p for p in ssh_probes if p["phase"] == "active_flood"]
        srv_sockets_active = [p["server_sockets_443"] for p in active_ssh if p.get("server_sockets_443") is not None]
        srv_bans_active = [p["server_bpf_bans"] for p in active_ssh if p.get("server_bpf_bans") is not None]

        # Explicit separated failure criteria
        explicit_criteria = {
            "criterion_original_code_http200_outage_failed": bool(trial["reached_failure_threshold"]),
            "criterion_active_load_ssh_failed": not all(p["authenticated_ok"] for p in active_ssh),
            "criterion_any_phase_ssh_failed": not all(p["authenticated_ok"] for p in ssh_probes),
            "criterion_500ms_overall_active_flood_below_80pct": (af["within_500ms_pct"] < 80.0),
            "criterion_500ms_sliding_5s_window_below_80pct": (tl["min_5s_window_within_500ms_count_of_10"] < 8),
        }
        trial["explicit_failure_criteria"] = explicit_criteria
        trial["recovery_timeline_analysis"] = tl
        trial["achieved_connection_metrics"] = {
            "offered_target_connections": conns,
            "client_peak_established_connections": trial["flood_generator"]["peak_client_held_connections"],
            "client_connect_failures": trial["flood_generator"]["connect_timeouts_or_errors"],
            "generator_limited": not trial["flood_generator"]["generator_reached_target"],
            "server_sockets_443_during_active_flood_samples": srv_sockets_active,
            "server_peak_sockets_443_observed": max(srv_sockets_active) if srv_sockets_active else None,
            "server_bpf_bans_during_active_flood": max(srv_bans_active) if srv_bans_active else 0,
        }

        # Also update individual summary file in step2_runs
        indiv_file = RUNS_DIR / f"{trial_id}.summary.json"
        if indiv_file.exists():
            indiv_file.write_text(json.dumps(trial, indent=2), encoding="utf-8")

    data["methodology_corrections_note"] = (
        "1. Every configuration returned 40/40 valid HTTP 200 responses during active load and 30/30 during post-load recovery; "
        "these trials demonstrate responsiveness under single-source connection pressure rather than a reproduced server crash. "
        "2. Original `reached_failure_threshold` (HTTP 200 outage) is preserved alongside explicitly separated failure flags (`explicit_failure_criteria`). "
        "3. Post-load HTTP 200 availability is separated from recovery to a 15-second (30-probe) consecutive `<= 500 ms` streak (`recovery_timeline_analysis`). "
        "4. Two source IP paths were used: direct-to-origin (`<CLIENT_LOAD_IP>`) for load generation and SSH probes, and Cloudflare dynamic pass-through (`cf-cache-status: DYNAMIC`) for legitimate visitor HTTPS probes."
    )
    STEP2_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("[+] Updated step2_pressure_comparison.json with explicit failure criteria and timeline recovery analysis.")


def summarize_non_null(vals: list[float | None]) -> dict:
    clean = [round(float(v), 3) for v in vals if v is not None]
    missing = sum(1 for v in vals if v is None)
    return {
        "observed_count": len(clean),
        "missing_not_observed_count": missing,
        "all_10_values_ms": [round(float(v), 3) if v is not None else None for v in vals],
        "min_ms": round(min(clean), 3) if clean else None,
        "median_ms": round(statistics.median(clean), 3) if clean else None,
        "mean_ms": round(statistics.mean(clean), 3) if clean else None,
        "max_ms": round(max(clean), 3) if clean else None,
        "range_ms": f"{min(clean):.3f} - {max(clean):.3f}" if clean else None,
    }


def reanalyze_timings():
    tdata = json.loads(TIMINGS_JSON.read_text(encoding="utf-8"))
    for obs in tdata["observations"]:
        mt = obs["monotonic_timestamps_s"]
        sd = obs["stage_durations_ms"]
        t1 = float(mt["t1_threshold_crossed"])
        t2 = float(mt["t2_telemetry_poll_detected"])
        t3 = float(mt["t3_bpf_map_updated"])
        t4 = float(mt["t4_sockets_severed"])

        if int(obs.get("xdp_drops_observed", 0)) == 0:
            # Rep 02: XDP drop counter increment was not observed during the sampling loop; do not substitute t4!
            mt["t5_first_xdp_drop"] = None
            sd["bpf_update_to_first_xdp_drop_ms"] = None
            sd["threshold_to_first_observed_xdp_drop_ms"] = None
            sd["threshold_to_both_severed_and_xdp_drop_observed_ms"] = None
        else:
            t5 = float(mt["t5_first_xdp_drop"])
            sd["bpf_update_to_first_xdp_drop_ms"] = round((t5 - t3) * 1000.0, 3)
            sd["threshold_to_first_observed_xdp_drop_ms"] = round((t5 - t1) * 1000.0, 3)
            sd["threshold_to_both_severed_and_xdp_drop_observed_ms"] = round((max(t4, t5) - t1) * 1000.0, 3)

        # Rename / clarify threshold_to_socket_severance_completion_ms explicitly
        sd["threshold_to_bpf_map_update_ms"] = round((t3 - t1) * 1000.0, 3)
        sd["threshold_to_socket_severance_completion_ms"] = round((t4 - t1) * 1000.0, 3)

    obs_list = tdata["observations"]
    tdata["corrected_summary_statistics"] = {
        "threshold_to_poll_detect_ms": summarize_non_null([o["stage_durations_ms"]["threshold_to_poll_detect_ms"] for o in obs_list]),
        "poll_detect_to_bpf_update_ms": summarize_non_null([o["stage_durations_ms"]["poll_detect_to_bpf_update_ms"] for o in obs_list]),
        "bpf_update_to_socket_sever_ms": summarize_non_null([o["stage_durations_ms"]["bpf_update_to_socket_sever_ms"] for o in obs_list]),
        "bpf_update_to_first_xdp_drop_ms": summarize_non_null([o["stage_durations_ms"]["bpf_update_to_first_xdp_drop_ms"] for o in obs_list]),
        "threshold_to_bpf_map_update_ms": summarize_non_null([o["stage_durations_ms"]["threshold_to_bpf_map_update_ms"] for o in obs_list]),
        "threshold_to_socket_severance_completion_ms": summarize_non_null([o["stage_durations_ms"]["threshold_to_socket_severance_completion_ms"] for o in obs_list]),
        "threshold_to_first_observed_xdp_drop_ms": summarize_non_null([o["stage_durations_ms"]["threshold_to_first_observed_xdp_drop_ms"] for o in obs_list]),
        "threshold_to_both_severed_and_xdp_drop_observed_ms": summarize_non_null([o["stage_durations_ms"]["threshold_to_both_severed_and_xdp_drop_observed_ms"] for o in obs_list]),
    }
    tdata["methodology_correction_note"] = (
        "In Repetition 2 (`xdp_drops_observed == 0`), the XDP drop counter increment was not observed within the sampling window. "
        "Instead of substituting `t4_sockets_severed`, `t5_first_xdp_drop` is kept explicitly `null` (`missing_not_observed_count: 1`). "
        "The `63.268 - 312.636 ms` range is explicitly labeled `threshold_to_socket_severance_completion_ms` (`t1 -> t4`, n=10), "
        "while `threshold_to_bpf_map_update_ms` (`t1 -> t3`, n=10, `37.129 - 256.584 ms`), "
        "`threshold_to_first_observed_xdp_drop_ms` (`t1 -> t5`, n=9 observed, `161.488 - 353.017 ms`), and "
        "`threshold_to_both_severed_and_xdp_drop_observed_ms` (`max(t4, t5) - t1`, n=9 observed, `161.488 - 353.017 ms`) are reported separately."
    )
    TIMINGS_JSON.write_text(json.dumps(tdata, indent=2), encoding="utf-8")
    print("[+] Updated detection_timings_10reps.json with separated labels and explicit null for missing Rep 02 XDP drop observation.")


if __name__ == "__main__":
    reanalyze_step2()
    reanalyze_timings()
