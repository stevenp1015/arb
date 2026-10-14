#!/usr/bin/env python3
"""
fp_engine/runner.py — the June sweep. For every Aster capture day and every coin
on it: split the day's shards once (single pass, low disk), then run the gated
labeler + featurizer per coin. One day's split lives at a time (~9GB peak),
deleted before the next day starts. Resume-safe: finished coin-days are skipped.

Output: out/sweep/<day>/<COIN>_matrix.csv  (features + first-passage labels)
Log:    sweep.log  — one DAY DONE line per day, SWEEP COMPLETE at the end.

Run niced so the live fleet keeps priority.
"""
import os, shutil, subprocess, sys, time
from multiprocessing import Pool

PY = "/root/hl_data/venv/bin/python"
BASE = "/root/fp_engine"
ASTER = "/root/hl_data/aster"
TMP = os.path.join(BASE, "tmp_split")
OUT = os.path.join(BASE, "out", "sweep")
ENTRIES = 2000
CHECKS = 8
MIN_BBO_LINES = 20000   # availability floor: below this the tape is too dead to label
WORKERS = 3


def split_day(day_dir):
    """One pass over each shard file, routing lines to per-coin files."""
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    os.makedirs(TMP)
    handles, counts = {}, {}
    try:
        for fn in sorted(os.listdir(day_dir)):
            kind = None
            for k in ("bbo", "trades", "book"):
                if fn.startswith(k + "_shard"):
                    kind = k
            if kind is None:
                continue
            with open(os.path.join(day_dir, fn)) as f:
                for line in f:
                    p = line.find('"c":"')
                    if p < 0:
                        continue
                    q = line.find('"', p + 5)
                    coin = line[p + 5:q]
                    key = (coin, kind)
                    h = handles.get(key)
                    if h is None:
                        d = os.path.join(TMP, coin)
                        os.makedirs(d, exist_ok=True)
                        h = open(os.path.join(d, kind + "_shard0.jsonl"), "w")
                        handles[key] = h
                    h.write(line)
                    if kind == "bbo":
                        counts[coin] = counts.get(coin, 0) + 1
    finally:
        for h in handles.values():
            h.close()
    return counts


def run_coin(args):
    day_name, coin, seed = args
    day_out = os.path.join(OUT, day_name)
    os.makedirs(day_out, exist_ok=True)
    matrix = os.path.join(day_out, "%s_matrix.csv" % coin)
    if os.path.exists(matrix):
        return (coin, "skip")
    coin_dir = os.path.join(TMP, coin)
    labels = os.path.join(day_out, "%s_labels.csv" % coin)
    try:
        r = subprocess.run([PY, os.path.join(BASE, "labeler.py"), "--day", coin_dir,
                            "--coin", coin, "--entries", str(ENTRIES), "--checks",
                            str(CHECKS), "--out", day_out, "--seed", str(seed)],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            return (coin, "LABEL-FAIL: " + (r.stdout + r.stderr)[-200:].replace("\n", " "))
        r = subprocess.run([PY, os.path.join(BASE, "featurizer.py"), "--day", coin_dir,
                            "--coin", coin, "--labels", labels, "--out", day_out],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            return (coin, "FEAT-FAIL: " + (r.stdout + r.stderr)[-200:].replace("\n", " "))
        return (coin, "ok")
    except subprocess.TimeoutExpired:
        return (coin, "TIMEOUT")
    except Exception as e:
        return (coin, "ERROR: %r" % e)


def main():
    days = sorted(d for d in os.listdir(ASTER)
                  if d.startswith("live_2") and os.path.isdir(os.path.join(ASTER, d)))
    print("SWEEP over %d days" % len(days), flush=True)
    for di, day in enumerate(days):
        day_out = os.path.join(OUT, day)
        t0 = time.time()
        counts = split_day(os.path.join(ASTER, day))
        coins = sorted(c for c, n in counts.items() if n >= MIN_BBO_LINES)
        skipped = sorted(c for c, n in counts.items() if n < MIN_BBO_LINES)
        todo = [(day, c, di * 1000 + ci) for ci, c in enumerate(coins)]
        pending = [t for t in todo
                   if not os.path.exists(os.path.join(day_out, "%s_matrix.csv" % t[1]))]
        if not pending:
            print("DAY DONE %s (all %d coins already present)" % (day, len(coins)),
                  flush=True)
            continue
        with Pool(WORKERS) as pool:
            results = pool.map(run_coin, todo)
        bad = [(c, s) for c, s in results if s not in ("ok", "skip")]
        print("DAY DONE %s: %d ok, %d skipped-dead(%s), %d FAILED %s  (%.0f min)"
              % (day, sum(1 for _, s in results if s in ("ok", "skip")),
                 len(skipped), ",".join(skipped[:5]), len(bad),
                 ["%s:%s" % b for b in bad[:3]], (time.time() - t0) / 60), flush=True)
    if os.path.isdir(TMP):
        shutil.rmtree(TMP)
    print("SWEEP COMPLETE", flush=True)


if __name__ == "__main__":
    main()
