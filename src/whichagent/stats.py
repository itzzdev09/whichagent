"""Solve rates with intervals, solved-every-time, cost per solved task and the paired sign test."""
import collections
import math
import random
import statistics


def wilson(k, n, z=1.96):
    """95% Wilson interval for k successes in n: sane for small n and rates near 0% or 100%."""
    if n == 0:
        return 0.0, 1.0
    p, d = k / n, 1 + z * z / n
    center = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, center - half), min(1.0, center + half)


def bootstrap(rates, reps=2000, seed=0):
    """95% interval for the mean per-task rate, resampling tasks: runs on one task aren't independent."""
    rnd, n = random.Random(seed), len(rates)
    means = sorted(sum(rnd.choices(rates, k=n)) / n for _ in range(reps))
    return means[int(0.025 * reps)], means[int(0.975 * reps) - 1]


def sign_test(wins, losses):
    """Exact two-sided sign test p-value (ties already dropped)."""
    n = wins + losses
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(min(wins, losses) + 1)) / 2 ** n)


def by_task(results):
    """{task: [solved?, ...]}. Setup errors aren't the agent's fault, so they don't count."""
    out = {}
    for r in results:
        if r["status"] != "setup_error":
            out.setdefault(r["task"], []).append(r["status"] == "solved")
    return out


def summary(results):
    """One agent's numbers, or None if it has no gradable runs."""
    tasks = by_task(results)
    if not tasks:
        return None
    runs = [r for r in results if r["status"] != "setup_error"]
    rates = [sum(v) / len(v) for v in tasks.values()]
    one_each = all(len(v) == 1 for v in tasks.values())
    known = [r["cost_usd"] for r in runs if r["cost_usd"] is not None]
    total = sum(known) if known else None
    solved = sum(r["status"] == "solved" for r in runs)
    secs = [r["secs"] for r in runs if r["secs"] is not None]
    return {"tasks": len(tasks), "runs": len(runs), "solved": solved, "rate": sum(rates) / len(rates),
            "ci": wilson(sum(rates), len(rates)) if one_each else bootstrap(rates),
            "every": None if one_each else sum(all(v) for v in tasks.values()) / len(tasks),
            "per_run": total / len(known) if known else None,
            "per_solved": total / solved if known and solved else None,
            "cost_complete": len(known) == len(runs),
            "secs": statistics.median(secs) if secs else None,
            "status": collections.Counter(r["status"] for r in results),
            "tampered": sum(bool(r.get("tampered")) for r in runs),
            "regressions": sum(r.get("regression") is True for r in runs)}


def compare(ra, rb):
    """(wins, losses, ties, p) for agent A vs B on the tasks both ran: A wins a task with a higher solve rate."""
    a, b = by_task(ra), by_task(rb)
    shared = a.keys() & b.keys()
    rate = {t: (sum(a[t]) / len(a[t]), sum(b[t]) / len(b[t])) for t in shared}
    wins = sum(x > y for x, y in rate.values())
    losses = sum(x < y for x, y in rate.values())
    return wins, losses, len(shared) - wins - losses, sign_test(wins, losses)
