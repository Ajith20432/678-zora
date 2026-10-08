"""Safe execution adapters with native protection and reconciliation helpers."""
from __future__ import annotations

import time

try:
    import ccxt
except ImportError:  # keep paper/replay/contract tests usable without the optional exchange client
    class _CCXTNetworkError(Exception):
        pass

    class _CCXTBaseError(Exception):
        pass

    class _CCXTFallback:
        NetworkError = _CCXTNetworkError
        BaseError = _CCXTBaseError

    ccxt = _CCXTFallback()

from config import CFG


class PaperExecutor:
    live = False
    last_amount = None

    def open(self, exchange, symbol, action, amount, price):
        return price

    def close(self, exchange, symbol, action, amount, price):
        return price

    def place_protection(self, *args, **kwargs):
        return {"stop": "paper", "take": "paper"}

    def cancel_protection(self, *args, **kwargs):
        return True


class LiveExecutor:
    live = True

    def __init__(self, telegram_alert=lambda msg: None):
        self.alert = telegram_alert
        self.last_amount = None     # exchange-precision amount of the most recent order actually sent
        # True after a network error on an order: the exchange may or may not have filled it. Callers must
        # stop trading that symbol until the exchange state has been checked by a human (see bot.Trader).
        self.unknown_state = False

    def _sized_amount(self, exchange, symbol, amount):
        try:
            market = exchange.market(symbol)
            amount = float(exchange.amount_to_precision(symbol, amount))
            min_amount = ((market.get("limits", {}) or {}).get("amount", {}) or {}).get("min")
            if min_amount and amount < min_amount:
                return None
            return amount
        except Exception:
            return amount

    @staticmethod
    def _market_buy_needs_price(exchange, side: str) -> bool:
        """HTX (and a few others) price a MARKET BUY by cost = amount * price, so ccxt refuses it without a price."""
        opts = getattr(exchange, "options", None) or {}
        return side == "buy" and bool(opts.get("createMarketBuyOrderRequiresPrice", True))

    def _place(self, exchange, symbol, side, amount, ref_price=None):
        amount = self._sized_amount(exchange, symbol, amount)
        self.last_amount = amount
        if not amount:
            self.alert(f"[live] {symbol} {side} skipped — below the exchange minimum")
            return None
        params = {"clientOrderId": f"zora-{int(time.time()*1000)}"}
        order_price = ref_price if (ref_price and self._market_buy_needs_price(exchange, side)) else None
        try:
            # ONE attempt only. A market order whose reply was lost may already have filled; sending it again
            # would open the position twice. Retrying is only safe after the exchange has been checked.
            return exchange.create_order(symbol, "market", side, amount, order_price, params)
        except ccxt.NetworkError as e:
            self.unknown_state = True
            self.alert(f"[live] {symbol} {side} {amount} UNKNOWN STATE after network error ({e}). "
                       "Not retried. Check the exchange order history, then restart the bot to reconcile.")
            return None
        except ccxt.BaseError as e:
            self.alert(f"[live] {symbol} {side} rejected: {e}")
            return None

    def open(self, exchange, symbol, action, amount, price):
        side = "buy" if action == "BUY" else "sell"
        order = self._place(exchange, symbol, side, amount, price)
        if order is None:
            return None
        fill = order.get("average") or order.get("price") or price
        self.alert(f"[live] OPENED {action} {symbol} amount={amount} @ ~{fill}")
        return float(fill)

    def close(self, exchange, symbol, action, amount, price):
        side = "sell" if action == "BUY" else "buy"
        order = self._place(exchange, symbol, side, amount, price)
        if order is None:
            return None
        fill = order.get("average") or order.get("price") or price
        self.alert(f"[live] CLOSED {action} {symbol} amount={amount} @ ~{fill}")
        return float(fill)

    def _native_supported(self, exchange, symbol: str) -> bool:
        try:
            if hasattr(exchange, "featureValue"):
                # Both legs are placed below; one supported leg is not sufficient
                # for a protected live position.
                stop_ok = bool(exchange.featureValue(symbol, "createOrder", "stopLossPrice"))
                take_ok = bool(exchange.featureValue(symbol, "createOrder", "takeProfitPrice"))
                return stop_ok and take_ok
        except Exception:
            pass
        # Never assume support: protection is safety-critical.
        return False

    def place_protection(self, exchange, symbol, action, amount, stop, take):
        """Place exchange-native conditional exits. Refuse to trade unprotected if required."""
        if not self._native_supported(exchange, symbol):
            msg = f"[live] {symbol} native stop/take-profit capability not confirmed by exchange adapter"
            self.alert(msg)
            if CFG.native_protection_required:
                return None
            return {"software_only": True}
        exit_side = "sell" if action == "BUY" else "buy"
        base = {}
        try:
            # reduceOnly is a derivatives flag; spot markets reject it. Unknown market -> keep it (safer on swaps).
            if exchange.market(symbol).get("spot"):
                base = {}
            else:
                base = {"reduceOnly": True}
        except Exception:
            base = {"reduceOnly": True}
        orders = {}
        ex_id = getattr(exchange, "id", "ex")
        stamp = int(time.time() * 1000)
        try:
            orders["stop"] = exchange.create_order(
                symbol, "market", exit_side, amount, None,
                {"stopLossPrice": stop, **base, "clientOrderId": f"zora-sl-{ex_id}-{stamp}"})
            orders["take"] = exchange.create_order(
                symbol, "market", exit_side, amount, None,
                {"takeProfitPrice": take, **base, "clientOrderId": f"zora-tp-{ex_id}-{stamp}"})
            self.alert(f"[live] NATIVE PROTECTION armed {symbol}: SL={stop} TP={take}")
            return orders
        except ccxt.BaseError as e:
            self.alert(f"[live] native protection FAILED for {symbol}: {e}")
            # Don't leave half a bracket behind: an orphaned stop would later fire on its own
            # and open an unwanted position in the opposite direction.
            if orders:
                self.cancel_protection(exchange, symbol, orders)
            return None

    def protection_triggered(self, exchange, symbol, protection):
        """
        Did the exchange already close the position via its native stop/take order?
        Returns ("stop"|"take", average_fill_price_or_None) or None. Without this, the bot's own
        price check would fire a SECOND closing order on top of the one the exchange already filled.
        """
        if not protection or protection.get("software_only"):
            return None
        for key in ("stop", "take"):
            oid = (protection.get(key) or {}).get("id")
            if not oid:
                continue
            try:
                order = exchange.fetch_order(oid, symbol)
            except Exception:
                continue
            if str(order.get("status", "")).lower() in ("closed", "filled"):
                return key, (order.get("average") or order.get("price"))
        return None

    def cancel_protection(self, exchange, symbol, protection):
        if not protection or protection.get("software_only"):
            return True
        ok = True
        for key in ("stop", "take"):
            order = protection.get(key) or {}
            oid = order.get("id")
            if not oid:
                continue
            try:
                exchange.cancel_order(oid, symbol)
            except ccxt.BaseError as e:
                ok = False
                self.alert(f"[live] failed to cancel {key} protection {oid}: {e}")
        return ok

    def reconcile(self, exchange, symbol, expected_position=None):
        """Return exchange truth. If it cannot be proven, return safe=False (callers must then refuse entries)."""
        try:
            open_orders = exchange.fetch_open_orders(symbol)
        except Exception as e:
            return {"safe": False, "reason": f"cannot fetch open orders: {e}", "open_orders": []}
        result = {"safe": True, "open_orders": open_orders, "position": None}
        try:
            try:
                is_spot = bool(exchange.market(symbol).get("spot"))
            except Exception:
                is_spot = False
            if not is_spot and exchange.has.get("fetchPositions"):
                positions = exchange.fetch_positions([symbol])
                # Only a non-zero held size counts. contractSize is the contract multiplier, not a position: using
                # it as a fallback reported flat positions as open and could hide a real one behind them.
                result["position"] = next(
                    (p for p in positions if abs(float(p.get("contracts") or 0)) > 0), None)
            elif exchange.has.get("fetchBalance"):
                base = symbol.split("/")[0]
                balance = exchange.fetch_balance()
                result["position"] = {"contracts": float((balance.get("total") or {}).get(base) or 0), "spot_balance": True}
        except Exception as e:
            return {"safe": False, "reason": f"cannot reconcile position: {e}", "open_orders": open_orders}
        return result
