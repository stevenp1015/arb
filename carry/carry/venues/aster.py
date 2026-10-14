#!/usr/bin/env python3
"""
Aster (asterdex) perp client — EIP-712 wallet-signed fapi/v3 surface.

Signing proven against /fapi/v3/balance (returns 200). Order/chase/cancel
parameter shapes ported verbatim from the production TS client
(arb/aster/src/services/asterApi.ts). No X-MBX-APIKEY header — the signer
address is inside the signed payload.

Creds come from env only:  ASTER_SIGNER (EVM address), ASTER_PRIVATE_KEY.

Run directly for a read-only self-test:
    ASTER_SIGNER=0x.. ASTER_PRIVATE_KEY=0x.. python3 aster.py selftest
"""
import os, sys, time, math, json, urllib.parse, urllib.request

from eth_account import Account
from eth_account.messages import encode_typed_data

BASE = os.environ.get("ASTER_BASE", "https://fapi.asterdex.com")

_DOMAIN = {
    "name": "AsterSignTransaction",
    "version": "1",
    "chainId": 1666,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}
_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Message": [{"name": "msg", "type": "string"}],
}

_last_sec = [0]
_ctr = [0]


def _nonce() -> str:
    sec = int(time.time())
    if sec == _last_sec[0]:
        _ctr[0] += 1
    else:
        _last_sec[0] = sec
        _ctr[0] = 0
    return str(sec * 1_000_000 + _ctr[0])


class AsterError(Exception):
    pass


class AsterClient:
    def __init__(self, signer=None, private_key=None, base=BASE, timeout=12):
        self.signer = signer or os.environ["ASTER_SIGNER"]
        self.pk = private_key or os.environ["ASTER_PRIVATE_KEY"]
        self.base = base
        self.timeout = timeout

    # ---------- signing ----------
    def _sign(self, params: dict) -> str:
        full = dict(params)
        full["nonce"] = _nonce()
        full["signer"] = self.signer
        # sort alphabetically to match the production TS client (determinism;
        # the server re-derives from the exact string we send either way)
        qs = urllib.parse.urlencode(sorted(full.items()))
        td = {"types": _TYPES, "primaryType": "Message", "domain": _DOMAIN,
              "message": {"msg": qs}}
        signed = Account.sign_message(encode_typed_data(full_message=td), private_key=self.pk)
        return f"{qs}&signature={signed.signature.hex()}"

    def _req(self, endpoint, method, params=None, private=False):
        params = params or {}
        if private:
            url = f"{self.base}{endpoint}?{self._sign(params)}"
        else:
            qs = urllib.parse.urlencode(params)
            url = f"{self.base}{endpoint}" + (f"?{qs}" if qs else "")
        req = urllib.request.Request(
            url, method=method,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": "carry/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            raise AsterError(f"{method} {endpoint} -> {e.code} {body}")

    # ---------- public reads ----------
    def exchange_info(self):
        return self._req("/fapi/v1/exchangeInfo", "GET")

    def premium_index(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        return self._req("/fapi/v1/premiumIndex", "GET", p)

    def funding_info(self):
        return self._req("/fapi/v1/fundingInfo", "GET")

    def ticker_24h(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        return self._req("/fapi/v1/ticker/24hr", "GET", p)

    def depth(self, symbol, limit=20):
        return self._req("/fapi/v1/depth", "GET", {"symbol": symbol, "limit": str(limit)})

    def book_ticker(self, symbol):
        return self._req("/fapi/v1/ticker/bookTicker", "GET", {"symbol": symbol})

    # ---------- private reads ----------
    def balance(self):
        # v3 uses the EIP-712 wallet-sig scheme; v2 expects a legacy HMAC key.
        return self._req("/fapi/v3/balance", "GET", private=True)

    def usdt_available(self):
        for b in self.balance():
            if b.get("asset") == "USDT":
                return float(b.get("availableBalance", 0))
        return 0.0

    def position_risk(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        return self._req("/fapi/v3/positionRisk", "GET", p, private=True)

    def open_orders(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        return self._req("/fapi/v3/openOrders", "GET", p, private=True)

    # ---------- private writes ----------
    def set_leverage(self, symbol, leverage):
        return self._req("/fapi/v3/leverage", "POST",
                         {"symbol": symbol, "leverage": str(int(leverage))}, private=True)

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        # tolerate "no need to change" (-4046)
        try:
            return self._req("/fapi/v1/marginType", "POST",
                             {"symbol": symbol, "marginType": margin_type}, private=True)
        except AsterError as e:
            if "-4046" in str(e):
                return {"ok": "already", "detail": str(e)}
            raise

    def place_order(self, symbol, side, otype, quantity, price=None,
                    position_side=None, tif="GTC", reduce_only=None):
        p = {"symbol": symbol, "side": side, "type": otype, "quantity": str(quantity)}
        if otype == "LIMIT":
            if price is None:
                raise AsterError("LIMIT needs price")
            p["price"] = str(price)
            p["timeInForce"] = tif
        if position_side:
            p["positionSide"] = position_side
        if reduce_only is not None:
            p["reduceOnly"] = "true" if reduce_only else "false"
        return self._req("/fapi/v3/order", "POST", p, private=True)

    def place_post_only(self, symbol, side, quantity, price, position_side=None):
        """A plain GTX limit — post-only maker at a fixed price."""
        p = {"symbol": symbol, "side": side, "type": "LIMIT",
             "quantity": str(quantity), "price": str(price), "timeInForce": "GTX"}
        if position_side:
            p["positionSide"] = position_side
        return self._req("/fapi/v3/order", "POST", p, private=True)

    def place_chase(self, symbol, side, quantity, quantity_unit="BASE",
                    max_chase_offset=None, price_limit=None, position_side=None):
        """Server-side BBO-pegged post-only (GTX) maker that re-pegs itself."""
        p = {"symbol": symbol, "side": side, "quantityUnit": quantity_unit,
             "quantity": str(quantity), "timeInForce": "GTX"}
        if max_chase_offset is not None:
            p["maxChaseOffset"] = str(max_chase_offset)
            p["maxChaseOffsetType"] = "ABSOLUTE"
        if price_limit is not None:
            p["priceLimit"] = str(price_limit)
        if position_side:
            p["positionSide"] = position_side
        return self._req("/fapi/v3/chase", "POST", p, private=True)

    def cancel_order(self, symbol, order_id):
        return self._req("/fapi/v3/order", "DELETE",
                         {"symbol": symbol, "orderId": str(order_id)}, private=True)

    def cancel_all(self, symbol):
        return self._req("/fapi/v3/allOpenOrders", "DELETE", {"symbol": symbol}, private=True)

    # ---------- symbol filters / quantization ----------
    _filters = None

    def _load_filters(self):
        if self._filters is not None:
            return self._filters
        info = self.exchange_info()
        f = {}
        for s in info.get("symbols", []):
            tick = step = min_notional = None
            for flt in s.get("filters", []):
                t = flt.get("filterType")
                if t == "PRICE_FILTER":
                    tick = float(flt["tickSize"])
                elif t == "LOT_SIZE":
                    step = float(flt["stepSize"])
                elif t in ("MIN_NOTIONAL", "NOTIONAL"):
                    min_notional = float(flt.get("notional", flt.get("minNotional", 0)))
            f[s["symbol"]] = {"tick": tick, "step": step, "min_notional": min_notional,
                              "status": s.get("status")}
        self._filters = f
        return f

    @staticmethod
    def _round_step(x, step):
        if not step:
            return x
        return math.floor(x / step) * step

    def quantize(self, symbol, price=None, qty=None):
        f = self._load_filters().get(symbol, {})
        out = {}
        if price is not None and f.get("tick"):
            out["price"] = round(self._round_step(price, f["tick"]),
                                 max(0, -int(round(math.log10(f["tick"])))) if f["tick"] < 1 else 0)
        elif price is not None:
            out["price"] = price
        if qty is not None and f.get("step"):
            out["qty"] = round(self._round_step(qty, f["step"]),
                               max(0, -int(round(math.log10(f["step"])))) if f["step"] < 1 else 0)
        elif qty is not None:
            out["qty"] = qty
        return out


def _selftest():
    c = AsterClient()
    print("signer:", c.signer[:6] + "..." + c.signer[-4:])
    print("USDT available:", c.usdt_available())
    pr = c.position_risk()
    open_pos = [p for p in pr if abs(float(p.get("positionAmt", 0))) > 0]
    print("open positions:", len(open_pos))
    for p in open_pos[:10]:
        print("  ", p["symbol"], p["positionAmt"], "liq", p.get("liquidationPrice"), "lev", p.get("leverage"))
    bt = c.book_ticker("AAVEUSDT")
    print("AAVEUSDT book:", bt.get("bidPrice"), "/", bt.get("askPrice"))
    q = c.quantize("AAVEUSDT", price=float(bt["bidPrice"]), qty=0.123456)
    print("quantize AAVE bid+0.123456 ->", q)
    print("open orders:", len(c.open_orders()))
    print("SELFTEST OK")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    else:
        print("usage: aster.py selftest")
