#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════╗
║         TrainLiveEG — Load & Performance Test Suite             ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║  Tests two critical live-tracking endpoints:                     ║
║    1. POST /live/{train_id}/location  (contributor GPS update)   ║
║    2. GET  /live/position/{train_id}  (viewer position poll)     ║
║                                                                  ║
║  Profiles:                                                       ║
║    light   —   50 trains ×  5 contrib ×   300 viewers            ║
║    medium  —  200 trains ×  6 contrib ×   500 viewers            ║
║    heavy   —  500 trains ×  7 contrib × 1,000 viewers            ║
║    extreme —  700 trains ×  8 contrib × 1,500 viewers            ║
║                                                                  ║
║  Install:  pip install aiohttp                                   ║
║                                                                  ║
║  Usage:                                                          ║
║    python load_test.py --url URL --token TOKEN --profile light   ║
║    python load_test.py --url URL --token TOKEN --profile heavy   ║
║    python load_test.py --url URL --token TOKEN --profile extreme ║
║              --duration 120                                      ║
║                                                                  ║
║  Custom:                                                         ║
║    python load_test.py --url URL --token TOKEN --profile light   ║
║              --trains 100 --contributors 5 --viewers 800         ║
║              --concurrency 3000 --duration 90                    ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""

import asyncio
import math
import random
import sys
import time
import argparse
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

try:
    import aiohttp
except ImportError:
    print("ERROR: aiohttp is required. Install it with:")
    print("  pip install aiohttp")
    sys.exit(1)


# ─── Egypt Railway Coordinates (realistic GPS data) ────────────────────────────

RAILWAY_COORDS = [
    (30.0444, 31.2357),  # القاهرة
    (30.5852, 31.5002),  # بنها
    (30.7865, 31.0004),  # طنطا
    (31.0409, 31.3785),  # المنصورة
    (31.2001, 29.9187),  # الإسكندرية
    (30.0131, 31.2089),  # الجيزة
    (29.3085, 30.8428),  # بني سويف
    (28.0871, 30.7618),  # المنيا
    (27.1783, 31.1859),  # أسيوط
    (26.5570, 31.6948),  # سوهاج
    (26.1551, 32.7160),  # قنا
    (25.6872, 32.6396),  # الأقصر
    (24.0889, 32.8998),  # أسوان
]


# ─── Load Profiles ──────────────────────────────────────────────────────────────

@dataclass
class LoadProfile:
    name: str
    num_trains: int
    contributors_per_train: int
    viewers_per_train: int
    update_interval: float
    max_concurrency: int
    ramp_up_seconds: int


PROFILES = {
    "light": LoadProfile(
        name="Light Load",
        num_trains=50,
        contributors_per_train=5,
        viewers_per_train=300,
        update_interval=30.0,
        max_concurrency=500,
        ramp_up_seconds=10,
    ),
    "medium": LoadProfile(
        name="Medium Load",
        num_trains=200,
        contributors_per_train=6,
        viewers_per_train=500,
        update_interval=30.0,
        max_concurrency=2000,
        ramp_up_seconds=20,
    ),
    "heavy": LoadProfile(
        name="Heavy Load",
        num_trains=500,
        contributors_per_train=7,
        viewers_per_train=1000,
        update_interval=30.0,
        max_concurrency=5000,
        ramp_up_seconds=30,
    ),
    "extreme": LoadProfile(
        name="Extreme Load",
        num_trains=700,
        contributors_per_train=8,
        viewers_per_train=1500,
        update_interval=30.0,
        max_concurrency=10000,
        ramp_up_seconds=45,
    ),
}


# ─── Stats Collector ────────────────────────────────────────────────────────────

MAX_LATENCY_SAMPLES = 200_000


@dataclass
class Stats:
    contrib_success: int = 0
    contrib_errors: int = 0
    contrib_latencies: list = field(default_factory=list)
    contrib_status_codes: dict = field(default_factory=lambda: defaultdict(int))

    viewer_success: int = 0
    viewer_errors: int = 0
    viewer_latencies: list = field(default_factory=list)
    viewer_status_codes: dict = field(default_factory=lambda: defaultdict(int))

    timeout_errors: int = 0
    connection_errors: int = 0

    start_time: float = 0.0

    _pending_count: int = 0

    def record_contrib(self, latency_ms: float, status: int):
        if 200 <= status < 300:
            self.contrib_success += 1
        else:
            self.contrib_errors += 1
        if len(self.contrib_latencies) < MAX_LATENCY_SAMPLES:
            self.contrib_latencies.append(latency_ms)
        self.contrib_status_codes[status] += 1

    def record_viewer(self, latency_ms: float, status: int):
        if 200 <= status < 300:
            self.viewer_success += 1
        else:
            self.viewer_errors += 1
        if len(self.viewer_latencies) < MAX_LATENCY_SAMPLES:
            self.viewer_latencies.append(latency_ms)
        self.viewer_status_codes[status] += 1

    def record_error(self, kind: str):
        if kind == "timeout":
            self.timeout_errors += 1
        else:
            self.connection_errors += 1

    @property
    def total(self) -> int:
        return (
            self.contrib_success + self.contrib_errors
            + self.viewer_success + self.viewer_errors
            + self.timeout_errors + self.connection_errors
        )

    @property
    def total_errors(self) -> int:
        return (
            self.contrib_errors + self.viewer_errors
            + self.timeout_errors + self.connection_errors
        )

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.start_time if self.start_time else 0.0

    @property
    def rps(self) -> float:
        e = self.elapsed
        return self.total / e if e > 0 else 0.0


# ─── Helpers ────────────────────────────────────────────────────────────────────

def pct(data: list, p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100.0)
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[int(k)]
    return s[f] * (c - k) + s[c] * (k - f)


def random_coords() -> tuple[float, float]:
    base = random.choice(RAILWAY_COORDS)
    return (
        base[0] + random.uniform(-0.05, 0.05),
        base[1] + random.uniform(-0.05, 0.05),
    )


def fmt_num(n: int | float) -> str:
    if isinstance(n, float):
        return f"{n:,.1f}"
    return f"{n:,}"


# ─── HTTP Requests ──────────────────────────────────────────────────────────────

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)


async def do_contributor(
    session: aiohttp.ClientSession,
    base_url: str,
    train_id: str,
    token: str,
    stats: Stats,
    sem: asyncio.Semaphore,
):
    async with sem:
        stats._pending_count += 1
        lat, lng = random_coords()
        payload = {
            "lat": lat,
            "lng": lng,
            "speed": round(random.uniform(0, 140), 1),
            "bearing": round(random.uniform(0, 360), 1),
        }
        url = f"{base_url}/live/{train_id}/location"
        headers = {"Authorization": token, "Content-Type": "application/json"}
        t0 = time.monotonic()
        try:
            async with session.post(
                url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                await resp.read()
                stats.record_contrib((time.monotonic() - t0) * 1000, resp.status)
        except asyncio.TimeoutError:
            stats.record_error("timeout")
        except (aiohttp.ClientError, OSError, ConnectionError):
            stats.record_error("connection")
        finally:
            stats._pending_count -= 1


async def do_viewer(
    session: aiohttp.ClientSession,
    base_url: str,
    train_id: str,
    token: str,
    stats: Stats,
    sem: asyncio.Semaphore,
):
    async with sem:
        stats._pending_count += 1
        url = f"{base_url}/live/position/{train_id}"
        headers = {"Authorization": token}
        t0 = time.monotonic()
        try:
            async with session.get(
                url, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                await resp.read()
                stats.record_viewer((time.monotonic() - t0) * 1000, resp.status)
        except asyncio.TimeoutError:
            stats.record_error("timeout")
        except (aiohttp.ClientError, OSError, ConnectionError):
            stats.record_error("connection")
        finally:
            stats._pending_count -= 1


# ─── Live Reporter ──────────────────────────────────────────────────────────────

def print_live(stats: Stats, target_rps: float, profile: LoadProfile):
    e = stats.elapsed
    ramp_pct = min(100.0, (e / profile.ramp_up_seconds) * 100) if profile.ramp_up_seconds > 0 else 100.0
    phase = "RAMP-UP" if ramp_pct < 100 else "STEADY "

    c_p50 = pct(stats.contrib_latencies[-2000:], 50)
    v_p50 = pct(stats.viewer_latencies[-2000:], 50)

    line = (
        f"\r  [{phase} {ramp_pct:5.1f}%] "
        f"{e:6.1f}s | "
        f"RPS: {stats.rps:8.1f}/{target_rps:,.0f} | "
        f"Contrib: {stats.contrib_success:>7,} p50={c_p50:6.0f}ms | "
        f"Viewer: {stats.viewer_success:>8,} p50={v_p50:6.0f}ms | "
        f"Err: {stats.total_errors:>5,} | "
        f"Pending: {stats._pending_count:>5,}"
    )
    sys.stdout.write(line)
    sys.stdout.flush()


# ─── Final Report ───────────────────────────────────────────────────────────────

def print_report(stats: Stats, profile: LoadProfile, target_rps: float, duration: int):
    e = stats.elapsed
    actual_rps = stats.rps
    total = stats.total
    error_rate = (stats.total_errors / total * 100) if total > 0 else 0.0

    all_lat = stats.contrib_latencies + stats.viewer_latencies
    avg_lat = sum(all_lat) / len(all_lat) if all_lat else 0.0

    total_contrib = profile.num_trains * profile.contributors_per_train
    total_viewers = profile.num_trains * profile.viewers_per_train

    print("\n\n")
    print("=" * 72)
    print(f"   LOAD TEST REPORT — {profile.name}")
    print("=" * 72)

    print(f"""
   Configuration
   {'─' * 66}
   Trains:             {profile.num_trains:,}
   Contributors:       {total_contrib:,} ({profile.contributors_per_train}/train)
   Viewers:            {total_viewers:,} ({profile.viewers_per_train}/train)
   Simulated Users:    {total_contrib + total_viewers:,}
   Update Interval:    {profile.update_interval}s
   Max Concurrency:    {profile.max_concurrency:,}
   Ramp-up:            {profile.ramp_up_seconds}s
   Test Duration:      {duration}s

   Results
   {'─' * 66}
   Total Duration:     {e:.1f}s
   Total Requests:     {total:,}
   Target RPS:         {target_rps:,.1f}
   Actual RPS:         {actual_rps:,.1f}
   Achievement:        {(actual_rps / target_rps * 100) if target_rps > 0 else 0:.1f}%
   Error Rate:         {error_rate:.2f}%""")

    print(f"""
   Contributor Endpoint — POST /live/{{train_id}}/location
   {'─' * 66}
   Requests:     {stats.contrib_success + stats.contrib_errors:,}
   Success:      {stats.contrib_success:,}
   Errors:       {stats.contrib_errors:,}""")
    if stats.contrib_latencies:
        print(f"   Latency p50:   {pct(stats.contrib_latencies, 50):.1f} ms")
        print(f"   Latency p90:   {pct(stats.contrib_latencies, 90):.1f} ms")
        print(f"   Latency p95:   {pct(stats.contrib_latencies, 95):.1f} ms")
        print(f"   Latency p99:   {pct(stats.contrib_latencies, 99):.1f} ms")
        print(f"   Latency max:   {max(stats.contrib_latencies):.1f} ms")
        print(f"   Latency avg:   {sum(stats.contrib_latencies)/len(stats.contrib_latencies):.1f} ms")

    print(f"""
   Viewer Endpoint — GET /live/position/{{train_id}}
   {'─' * 66}
   Requests:     {stats.viewer_success + stats.viewer_errors:,}
   Success:      {stats.viewer_success:,}
   Errors:       {stats.viewer_errors:,}""")
    if stats.viewer_latencies:
        print(f"   Latency p50:   {pct(stats.viewer_latencies, 50):.1f} ms")
        print(f"   Latency p90:   {pct(stats.viewer_latencies, 90):.1f} ms")
        print(f"   Latency p95:   {pct(stats.viewer_latencies, 95):.1f} ms")
        print(f"   Latency p99:   {pct(stats.viewer_latencies, 99):.1f} ms")
        print(f"   Latency max:   {max(stats.viewer_latencies):.1f} ms")
        print(f"   Latency avg:   {sum(stats.viewer_latencies)/len(stats.viewer_latencies):.1f} ms")

    print(f"""
   Error Breakdown
   {'─' * 66}
   HTTP errors:       {stats.contrib_errors + stats.viewer_errors:,}
   Timeouts:          {stats.timeout_errors:,}
   Connection fails:  {stats.connection_errors:,}""")

    if stats.contrib_status_codes:
        print(f"\n   Contributor HTTP Status Codes:")
        for code, count in sorted(stats.contrib_status_codes.items()):
            marker = " <<<" if code >= 400 else ""
            print(f"     {code}: {count:,}{marker}")

    if stats.viewer_status_codes:
        print(f"\n   Viewer HTTP Status Codes:")
        for code, count in sorted(stats.viewer_status_codes.items()):
            marker = " <<<" if code >= 400 else ""
            print(f"     {code}: {count:,}{marker}")

    # ── Verdict ──
    print(f"\n   {'=' * 66}")
    print(f"   VERDICT")
    print(f"   {'=' * 66}")

    if error_rate < 0.5 and avg_lat < 300:
        verdict = "SERVER PASSED — Low errors, fast response"
        icon = "+"
    elif error_rate < 2 and avg_lat < 800:
        verdict = "SERVER OK — Minor degradation"
        icon = "~"
    elif error_rate < 5 and avg_lat < 2000:
        verdict = "SERVER STRESSED — Noticeable degradation"
        icon = "!"
    elif error_rate < 15 and avg_lat < 5000:
        verdict = "SERVER STRUGGLING — Significant degradation"
        icon = "!!"
    else:
        verdict = "SERVER OVERLOADED — Cannot handle this load"
        icon = "X"

    print(f"\n   [{icon}] {verdict}")
    print(f"   Error Rate:      {error_rate:.2f}%")
    print(f"   Avg Latency:     {avg_lat:.1f} ms")
    print(f"   Achieved RPS:    {actual_rps:,.1f} / {target_rps:,.1f} target")

    # Capacity estimate
    if actual_rps > 0 and error_rate < 5:
        capacity_trains = int(actual_rps * profile.update_interval / (
            profile.contributors_per_train + profile.viewers_per_train
        ))
        print(f"\n   Estimated Capacity:  ~{capacity_trains:,} trains at this error rate")
        print(f"                        ~{capacity_trains * profile.viewers_per_train:,} concurrent viewers")

    print(f"\n{'=' * 72}\n")


# ─── Main Load Test Runner ──────────────────────────────────────────────────────

async def run_test(base_url: str, token: str, profile: LoadProfile, duration: int):
    contrib_rps = (profile.num_trains * profile.contributors_per_train) / profile.update_interval
    viewer_rps = (profile.num_trains * profile.viewers_per_train) / profile.update_interval
    target_rps = contrib_rps + viewer_rps

    total_contrib = profile.num_trains * profile.contributors_per_train
    total_viewers = profile.num_trains * profile.viewers_per_train

    train_ids = [str(1000 + i) for i in range(profile.num_trains)]

    print(f"\n{'=' * 72}")
    print(f"   TrainLiveEG Load Test — {profile.name}")
    print(f"{'=' * 72}")
    print(f"   Server:           {base_url}")
    print(f"   Trains:           {profile.num_trains:,}")
    print(f"   Contributors:     {total_contrib:,} ({profile.contributors_per_train}/train)")
    print(f"   Viewers:          {total_viewers:,} ({profile.viewers_per_train}/train)")
    print(f"   Simulated Users:  {total_contrib + total_viewers:,}")
    print(f"   Target RPS:       {target_rps:,.1f}")
    print(f"     Contributor:    {contrib_rps:,.1f} req/s")
    print(f"     Viewer:         {viewer_rps:,.1f} req/s")
    print(f"   Max Concurrency:  {profile.max_concurrency:,}")
    print(f"   Duration:         {duration}s (+ {profile.ramp_up_seconds}s ramp-up)")
    print(f"{'=' * 72}")

    print(f"\n   NOTE: Using a single auth token. All contributor requests appear as")
    print(f"   the same user — this stress-tests HTTP, auth, Redis, and in-memory")
    print(f"   processing. For multi-user testing, provide multiple tokens.")

    print(f"\n   Starting in 3 seconds...\n")
    await asyncio.sleep(3)

    stats = Stats()
    stats.start_time = time.monotonic()

    sem = asyncio.Semaphore(profile.max_concurrency)

    connector = aiohttp.TCPConnector(
        limit=profile.max_concurrency,
        limit_per_host=profile.max_concurrency,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
        force_close=False,
    )

    running = True
    total_duration = duration + profile.ramp_up_seconds

    async with aiohttp.ClientSession(connector=connector) as session:

        # ── Request Generator ──
        async def generator():
            nonlocal running
            BATCH_INTERVAL = 0.1  # fire a batch every 100ms
            contrib_sent = 0
            viewer_sent = 0
            pending_tasks: set = set()

            while running:
                elapsed = time.monotonic() - stats.start_time
                if elapsed >= total_duration:
                    break

                # Ramp-up factor 0.0 → 1.0
                ramp = min(1.0, elapsed / profile.ramp_up_seconds) if profile.ramp_up_seconds > 0 else 1.0

                # How many requests should have been sent by now?
                expected_contrib = int(contrib_rps * ramp * elapsed)
                expected_viewer = int(viewer_rps * ramp * elapsed)

                to_contrib = max(0, expected_contrib - contrib_sent)
                to_viewer = max(0, expected_viewer - viewer_sent)

                # Backpressure: don't overwhelm the event loop
                max_burst = profile.max_concurrency * 2
                to_contrib = min(to_contrib, max_burst)
                to_viewer = min(to_viewer, max_burst)

                # Cleanup finished tasks
                done = {t for t in pending_tasks if t.done()}
                pending_tasks -= done

                # Cap in-flight tasks
                headroom = max(0, profile.max_concurrency * 3 - len(pending_tasks))
                total_to_send = min(to_contrib + to_viewer, headroom)

                if total_to_send > 0:
                    # Maintain correct ratio
                    ratio = to_contrib / (to_contrib + to_viewer) if (to_contrib + to_viewer) > 0 else 0
                    batch_contrib = int(total_to_send * ratio)
                    batch_viewer = total_to_send - batch_contrib

                    for _ in range(batch_contrib):
                        tid = random.choice(train_ids)
                        t = asyncio.create_task(
                            do_contributor(session, base_url, tid, token, stats, sem)
                        )
                        pending_tasks.add(t)
                        contrib_sent += 1

                    for _ in range(batch_viewer):
                        tid = random.choice(train_ids)
                        t = asyncio.create_task(
                            do_viewer(session, base_url, tid, token, stats, sem)
                        )
                        pending_tasks.add(t)
                        viewer_sent += 1

                await asyncio.sleep(BATCH_INTERVAL)

            running = False

        # ── Stats Reporter ──
        async def reporter():
            while running:
                await asyncio.sleep(3)
                if running:
                    print_live(stats, target_rps, profile)

        gen_task = asyncio.create_task(generator())
        rep_task = asyncio.create_task(reporter())

        try:
            await gen_task
        except KeyboardInterrupt:
            running = False
        except Exception as exc:
            print(f"\n   ERROR in generator: {exc}")
            running = False

        running = False
        rep_task.cancel()

        # Wait for in-flight requests to complete (up to 10s)
        print(f"\n\n   Waiting for in-flight requests to complete...")
        await asyncio.sleep(min(10, max(2, stats._pending_count * 0.01)))

    print_report(stats, profile, target_rps, duration)


# ─── Dry Run ────────────────────────────────────────────────────────────────────

def dry_run(profile: LoadProfile, duration: int):
    total_contrib = profile.num_trains * profile.contributors_per_train
    total_viewers = profile.num_trains * profile.viewers_per_train
    contrib_rps = total_contrib / profile.update_interval
    viewer_rps = total_viewers / profile.update_interval
    target_rps = contrib_rps + viewer_rps
    total_requests = int(target_rps * duration)

    print(f"\n{'=' * 72}")
    print(f"   DRY RUN — {profile.name}")
    print(f"{'=' * 72}")
    print(f"""
   Trains:                {profile.num_trains:,}
   Contributors/train:    {profile.contributors_per_train}
   Viewers/train:         {profile.viewers_per_train:,}
   Total Contributors:    {total_contrib:,}
   Total Viewers:         {total_viewers:,}
   Total Simulated Users: {total_contrib + total_viewers:,}
   Update Interval:       {profile.update_interval}s

   Contributor RPS:       {contrib_rps:,.1f}  (each sends every {profile.update_interval}s)
   Viewer RPS:            {viewer_rps:,.1f}  (each polls every {profile.update_interval}s)
   Total Target RPS:      {target_rps:,.1f}

   Test Duration:         {duration}s (+ {profile.ramp_up_seconds}s ramp-up)
   Expected Requests:     ~{total_requests:,}
   Max Concurrency:       {profile.max_concurrency:,}

   NOTE: Actual RPS depends on server response time and client machine.
   A single machine typically achieves 2,000–8,000 req/s with aiohttp.
   For extreme profiles, consider running multiple instances or using
   a dedicated load testing machine (Linux recommended).
""")
    print(f"{'=' * 72}\n")


# ─── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="TrainLiveEG Load & Performance Test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python load_test.py --url https://api.example.com/api/v1 --token "Bearer eyJ..." --profile light
  python load_test.py --url https://api.example.com/api/v1 --token "Bearer eyJ..." --profile heavy --duration 120
  python load_test.py --url https://api.example.com/api/v1 --token "Bearer eyJ..." --profile light --trains 100 --viewers 800
  python load_test.py --dry-run --profile extreme
        """,
    )
    parser.add_argument("--url", help="Base API URL (e.g. https://server.com/api/v1)")
    parser.add_argument("--token", help='Auth token (include "Bearer " prefix)')
    parser.add_argument(
        "--profile",
        choices=list(PROFILES.keys()),
        default="light",
        help="Load profile (default: light)",
    )
    parser.add_argument("--duration", type=int, default=60, help="Test duration in seconds (default: 60)")
    parser.add_argument("--trains", type=int, help="Override number of trains")
    parser.add_argument("--contributors", type=int, help="Override contributors per train")
    parser.add_argument("--viewers", type=int, help="Override viewers per train")
    parser.add_argument("--concurrency", type=int, help="Override max concurrency")
    parser.add_argument("--dry-run", action="store_true", help="Show expected load without sending requests")

    args = parser.parse_args()

    profile = LoadProfile(**vars(PROFILES[args.profile]))

    if args.trains:
        profile.num_trains = args.trains
    if args.contributors:
        profile.contributors_per_train = args.contributors
    if args.viewers:
        profile.viewers_per_train = args.viewers
    if args.concurrency:
        profile.max_concurrency = args.concurrency

    if args.dry_run:
        dry_run(profile, args.duration)
        return

    if not args.url or not args.token:
        parser.error("--url and --token are required (use --dry-run to skip)")

    base_url = args.url.rstrip("/")
    token = args.token
    if not token.startswith("Bearer "):
        token = "Bearer " + token

    try:
        asyncio.run(run_test(base_url, token, profile, args.duration))
    except KeyboardInterrupt:
        print("\n\n   Test interrupted by user (Ctrl+C)")
        sys.exit(0)


if __name__ == "__main__":
    main()
