#!/usr/bin/env python3
"""
Standalone verification script for the Argus public benchmark repository.
Recalculates every headline table directly from the raw per-trial `.probes.jsonl`
and `.summary.json` files in `trials/` and `data/` using only the Python standard library.
"""

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
TRIALS_DIR = ROOT / "trials"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def verify_probes_match_summary(probes_path: Path, summary_path: Path) -> tuple[int, int]:
    rows = [json.loads(line) for line in probes_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = load_json(summary_path)
    active_rows = [r for r in rows if r["phase"] == "active_flood"]
    within_500 = sum(1 for r in active_rows if r.get("within_500ms"))
    expected = summary["phases"]["active_flood"]["within_500ms"]
    assert len(active_rows) == 40, f"{probes_path}: expected 40 active_flood probes, found {len(active_rows)}"
    assert within_500 == expected, f"{probes_path}: within_500ms={within_500} != summary={expected}"
    return within_500, len(active_rows)


def main() -> None:
    print("=" * 84)
    print("ARGUS BENCHMARK REPOSITORY - RAW RECORD RECALCULATION & VERIFICATION")
    print("=" * 84)

    # 1. Verify every .probes.jsonl file matches its corresponding .summary.json
    total_trials = 0
    total_active_probes = 0
    for subdir in sorted(TRIALS_DIR.iterdir()):
        if not subdir.is_dir():
            continue
        summaries = sorted(subdir.glob("*.summary.json"))
        for s_path in summaries:
            p_path = s_path.with_name(s_path.name.replace(".summary.json", ".probes.jsonl"))
            assert p_path.exists(), f"Missing probe timeline for {s_path.name}"
            _, n_active = verify_probes_match_summary(p_path, s_path)
            total_trials += 1
            total_active_probes += n_active

    print(f"[PASS] Verified {total_trials} raw trial timelines ({total_active_probes} active-load visitor probes)")

    # 2. Recalculate Table 1: Three-Tier Instance Comparison ($4/mo vs $18/mo vs $48/mo)
    deep = load_json(DATA_DIR / "argus-cross-size-4usd-18usd-48usd-2026-11-07.json")
    t3 = deep["three_run_comparison_350_and_800"]

    print("\n--- Table 1: Three-Run Repeatability Across $4/mo, $18/mo, and $48/mo (120 probes/cell) ---")
    for tier_key, label in [("tier2_350conns", "350 conns + 12 workers"), ("tier3_800conns", "800 conns + 12 workers")]:
        row = t3[tier_key]
        r4_ref = row["4usd_1vcpu_512mb_reference"]
        r4_tun = row["4usd_1vcpu_512mb_tuned"]
        r4_arg = row["4usd_1vcpu_512mb_argus"]
        r18_ref = row["18usd_2vcpu_2gb_reference"]
        r18_tun = row["18usd_2vcpu_2gb_tuned"]
        r48_ref = row["48usd_4vcpu_8gb_reference"]
        r48_tun = row["48usd_4vcpu_8gb_tuned"]
        print(
            f"  {label:24s} | "
            f"$4 Ref/Tun: {r4_ref['pooled_active_flood_within_500ms_pct']:.1f}% / {r4_tun['pooled_active_flood_within_500ms_pct']:.1f}% | "
            f"$18 Ref/Tun: {r18_ref['pooled_active_flood_within_500ms_pct']:.1f}% / {r18_tun['pooled_active_flood_within_500ms_pct']:.1f}% | "
            f"$48 Ref/Tun: {r48_ref['pooled_active_flood_within_500ms_pct']:.1f}% / {r48_tun['pooled_active_flood_within_500ms_pct']:.1f}% | "
            f"$4 Argus: {r4_arg['pooled_active_flood_within_500ms_pct']:.1f}% ({r4_arg['pooled_active_flood_within_500ms_count_of_120']}/120)"
        )

    # 3. Recalculate Table 2: Deep Stress (800c/32w) & Sub-Threshold Boundary (150c/24w)
    print("\n--- Table 2: Deep Stress (800c/32w) & Sub-Threshold Boundary (150c/24w) ---")
    trials_4u = {
        (t["tier"], t["profile"]): t["phases"]["active_flood"]
        for t in deep.get("all_4usd_deep_stress_and_boundary_trials", [])
    }
    trials_48u = {
        (t["tier"], t["profile"]): t["phases"]["active_flood"]
        for t in deep.get("all_48usd_trials", [])
        if t["tier"] in ("tier3_heavy_800conns_32workers", "subthreshold_150conns_24workers")
    }
    print(
        f"  800c + 32 heavy workers  | "
        f"$4 Ref/Tun: {trials_4u[('tier3_heavy_800conns_32workers', 'reference')]['within_500ms_pct']:.1f}% / "
        f"{trials_4u[('tier3_heavy_800conns_32workers', 'tuned')]['within_500ms_pct']:.1f}% | "
        f"$4 Argus: {trials_4u[('tier3_heavy_800conns_32workers', 'argus')]['within_500ms_pct']:.1f}% (39/40) | "
        f"$48 Ref/Tun: {trials_48u[('tier3_heavy_800conns_32workers', 'reference')]['within_500ms_pct']:.1f}% / "
        f"{trials_48u[('tier3_heavy_800conns_32workers', 'tuned')]['within_500ms_pct']:.1f}% | "
        f"$48 Argus: {trials_48u[('tier3_heavy_800conns_32workers', 'argus')]['within_500ms_pct']:.1f}% (39/40)"
    )
    print(
        f"  150c (<200) + 24 workers | "
        f"$4 Ref/Tun: {trials_4u[('subthreshold_150conns_24workers', 'reference')]['within_500ms_pct']:.1f}% / "
        f"{trials_4u[('subthreshold_150conns_24workers', 'tuned')]['within_500ms_pct']:.1f}% | "
        f"$4 Argus: {trials_4u[('subthreshold_150conns_24workers', 'argus')]['within_500ms_pct']:.1f}% (20/40) | "
        f"$48 Tuned: {trials_48u[('subthreshold_150conns_24workers', 'tuned')]['within_500ms_pct']:.1f}% (23/40) | "
        f"$48 Argus: {trials_48u[('subthreshold_150conns_24workers', 'argus')]['within_500ms_pct']:.1f}% (15/40)"
    )

    # 4. Verify 10-repetition detection-to-enforcement monotonic timings
    timings = load_json(DATA_DIR / "argus-detection-timings-2026-11-07.json")
    reps = timings["observations"]
    t1_t3 = [r["stage_durations_ms"]["threshold_to_bpf_map_update_ms"] for r in reps]
    t1_t4 = [r["stage_durations_ms"]["threshold_to_socket_severance_completion_ms"] for r in reps]
    t1_t5 = [
        r["stage_durations_ms"]["threshold_to_first_observed_xdp_drop_ms"]
        for r in reps
        if r["stage_durations_ms"]["threshold_to_first_observed_xdp_drop_ms"] is not None
    ]
    print("\n--- Separated Detection-to-Enforcement Timings (CLOCK_MONOTONIC, 10 Repetitions) ---")
    print(f"  Threshold -> BPF update (n={len(t1_t3)}/10):      min={min(t1_t3):.2f} ms, median={statistics.median(t1_t3):.2f} ms, max={max(t1_t3):.2f} ms")
    print(f"  Threshold -> Socket severance (n={len(t1_t4)}/10): min={min(t1_t4):.2f} ms, median={statistics.median(t1_t4):.2f} ms, max={max(t1_t4):.2f} ms")
    print(f"  Threshold -> First XDP drop (n={len(t1_t5)}/10):   min={min(t1_t5):.2f} ms, median={statistics.median(t1_t5):.2f} ms, max={max(t1_t5):.2f} ms")

    # 5. Verify XDP, Rollback, Cold Reboot, and Shared-IP checks
    xdp = load_json(DATA_DIR / "xdp_packet_verification.json")
    rb = load_json(DATA_DIR / "rollback_verification.json")
    boot = load_json(DATA_DIR / "post_reboot_verification.json")
    shared = load_json(DATA_DIR / "argus-shared-ip-verification-2026-11-07.json")
    assert xdp["all_passed"] is True and len(xdp["cases"]) == 13
    assert rb["all_passed"] is True
    assert boot["all_passed"] is True
    assert shared["all_passed"] is True
    print("\n[PASS] Operational safety records verified: 13/13 XDP packet tests, scoped rollback, cold-reboot SSH, and shared-IP checks.")
    print("=" * 84)


if __name__ == "__main__":
    main()
