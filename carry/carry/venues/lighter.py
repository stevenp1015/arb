#!/usr/bin/env python3
"""
Lighter venue adapter.

Writes (orders/leverage/close/cancel) go through the compiled Rust CLI at
/root/carry/bin/lighter-cli (Python can't sign on Alpine musl). Reads that don't
need signing (account snapshot, funding payments) hit the public REST directly,
so the watchdog never depends on the Rust binary to SEE state.

Env (passed to the CLI): LIGHTER_API_KEY_PRIVATE, LIGHTER_API_KEY_INDEX,
LIGHTER_ACCOUNT_INDEX, LIGHTER_MAINNET.
"""
import os, json, subprocess, time, urllib.request

CLI = os.environ.get("LIGHTER_CLI", "/root/carry/bin/lighter-cli")
BASE = os.environ.get("LIGHTER_BASE", "https://mainnet.zklighter.elliot.ai")


class LighterError(Exception):
    pass


def _get(path, params, timeout=12):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{BASE}{path}" + (f"?{q}" if q else "")
    req = urllib.request.Request(url, headers={"User-Agent": "carry/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


class LighterVenue:
    def __init__(self, cli=CLI, timeout=25):
        self.cli = cli
        self.timeout = timeout
        self.account_index = int(os.environ.get("LIGHTER_ACCOUNT_INDEX", "0"))

    # ---------- signed writes / reads via Rust CLI ----------
    def _run(self, *args):
        try:
            p = subprocess.run([self.cli, *[str(a) for a in args]],
                               capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise LighterError(f"lighter-cli timeout: {args}")
        out = (p.stdout or "").strip().splitlines()
        obj = None
        for line in reversed(out):  # last stdout line is the JSON result
            line = line.strip()
            if line.startswith("{"):
                obj = json.loads(line)
                break
        if obj is None:
            raise LighterError(f"no JSON from cli {args}: stderr={p.stderr[-300:]}")
        if not obj.get("ok", False):
            raise LighterError(f"cli {args[0]} failed: {obj.get('error')}")
        return obj

    def account(self):
        return self._run("account")

    def market_info(self, symbol):
        return self._run("market_info", symbol)

    def set_leverage(self, symbol, lev):
        return self._run("leverage", symbol, int(lev))

    def place_market(self, symbol, side, qty, reduce_only=False, slip_pct=None):
        args = ["market", side, symbol, qty]
        if reduce_only:
            args.append("--reduce")
        if slip_pct is not None:
            args += ["--slip", slip_pct]
        return self._run(*args)

    def place_limit(self, symbol, side, qty, price, reduce_only=False):
        args = ["limit", side, symbol, qty, price]
        if reduce_only:
            args.append("--reduce")
        return self._run(*args)

    def close(self, symbol):
        return self._run("close", symbol)

    def cancel(self, symbol, order_index):
        return self._run("cancel", symbol, order_index)

    # ---------- public reads (no signing) ----------
    def account_public(self, index=None):
        idx = index if index is not None else self.account_index
        d = _get("/api/v1/account", {"by": "index", "value": idx})
        accts = d.get("accounts") or []
        return accts[0] if accts else None

    def funding_payments(self, market_id, start_ts, end_ts=None, count_back=200):
        """Actual per-hour funding records for (account, market). Proof source."""
        end_ts = end_ts or int(time.time())
        d = _get("/api/v1/fundings", {
            "account_index": self.account_index, "market_id": market_id,
            "resolution": "1h", "start_timestamp": int(start_ts),
            "end_timestamp": int(end_ts), "count_back": count_back})
        return d.get("fundings", [])

    def funding_rates(self):
        """Current posted rates for exchange=='lighter'."""
        d = _get("/api/v1/funding-rates", {})
        return {r["symbol"]: float(r["rate"]) for r in d.get("funding_rates", [])
                if r.get("exchange") == "lighter"}


if __name__ == "__main__":
    v = LighterVenue()
    print("account:", json.dumps(v.account())[:200])
    print("AAVE market:", json.dumps(v.market_info("AAVE")))
    print("funding BTC last 6h:", v.funding_payments(1, int(time.time()) - 6 * 3600)[:3])
