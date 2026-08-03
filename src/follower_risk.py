"""Risk-limit enforcement for a follower account.

This is a mixin (no ``__init__``) — the composing executor provides ``_cfg``,
``_name``, ``_peak_equity``, ``_peak_equity_date``, and the ``connect()`` /
``disconnect()`` lifecycle. It enforces daily-loss, daily-trade, drawdown, and
max-position caps before a trade is opened.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.mt5_compat import mt5

logger = logging.getLogger(__name__)


class RiskLimitsMixin:
    """Enforce risk limits (daily loss / trades, drawdown, max positions)."""

    def _check_risk_limits(self) -> bool:
        """Check risk limits against MT5 account history for today.

        Enforces:
        - max_daily_loss: total loss since midnight exceeds this threshold
        - max_drawdown_pct: current equity drawdown from peak exceeds this %
        - max_daily_trades: number of distinct positions traded today

        Returns True if all limits satisfied (or unchecked), False if any exceeded.
        """
        if self.is_file_based():
            # No MT5 API in file mode; limits are NOT enforced (a one-time
            # warning is logged at construction when limits are configured).
            return True

        if not self.connect():
            return True  # can't check, allow trade

        try:
            today_start = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            # Get all deals today (closed trades + results)
            history = mt5.history_deals_get(
                today_start,
                datetime.now() + timedelta(seconds=1),
            )
            if history is not None and len(history) > 0:
                today_pnl = sum(deal.profit for deal in history)
                # Count distinct positions — a round trip (open+close) produces
                # 2 deals, so counting deals would double-count trades. Deals
                # without a position (deposits/withdrawals, position_id==0) are
                # not trades.
                today_trades = len(
                    {d.position_id for d in history if d.position_id > 0}
                )

                # Max daily loss (check only when P&L is negative)
                if self._cfg.max_daily_loss > 0.0 and today_pnl < 0:
                    loss_abs = abs(today_pnl)
                    if loss_abs >= self._cfg.max_daily_loss:
                        logger.warning(
                            "%s: daily loss %.2f exceeds limit %.2f -- blocking trade",
                            self._name, loss_abs, self._cfg.max_daily_loss,
                        )
                        return False

                # Max daily trades
                if self._cfg.max_daily_trades > 0:
                    if today_trades >= self._cfg.max_daily_trades:
                        logger.warning(
                            "%s: daily trades %d >= limit %d -- blocking trade",
                            self._name, today_trades, self._cfg.max_daily_trades,
                        )
                        return False

            # Max drawdown from peak equity (true drawdown, not balance-based).
            # Peak is tracked in-memory per process and resets each day; it is
            # NOT persisted across restarts (documented tradeoff).
            if self._cfg.max_drawdown_pct > 0.0:
                acc = mt5.account_info()
                if acc and acc.equity > 0:
                    equity = acc.equity
                    today = today_start.strftime("%Y-%m-%d")
                    if self._peak_equity_date != today:
                        self._peak_equity = equity
                        self._peak_equity_date = today
                    elif equity > self._peak_equity:
                        self._peak_equity = equity
                    if self._peak_equity > 0:
                        drawdown_pct = (
                            (self._peak_equity - equity) / self._peak_equity * 100.0
                        )
                        if drawdown_pct >= self._cfg.max_drawdown_pct:
                            logger.warning(
                                "%s: drawdown %.1f%% from peak %.2f >= limit %.1f%% -- blocking trade",
                                self._name, drawdown_pct, self._peak_equity,
                                self._cfg.max_drawdown_pct,
                            )
                            return False

        except Exception as e:
            logger.warning(
                "%s: risk check error (allowing trade): %s", self._name, e,
            )
        finally:
            self.disconnect()

        return True

    def _positions_below_max(self) -> bool:
        """True if open positions on the follower account are under max_positions.

        Only positions this follower itself opened (its configured magic) are
        counted — on shared master+follower accounts the master's positions
        must not consume the follower's copy cap. max_positions <= 0 disables
        the cap. File-relay mode cannot count positions via the API, so the cap
        is not enforced there (mirrors _check_risk_limits behaviour).
        """
        if self._cfg.max_positions <= 0 or self.is_file_based():
            return True
        if not self.connect():
            return True  # can't check, allow trade
        try:
            positions = mt5.positions_get() or []
            count = sum(1 for p in positions if p.magic == self._cfg.magic)
            if count >= self._cfg.max_positions:
                logger.warning(
                    "%s: open positions %d >= max_positions %d -- blocking OPEN",
                    self._name, count, self._cfg.max_positions,
                )
                return False
            return True
        except Exception as e:
            logger.warning(
                "%s: position count error (allowing trade): %s", self._name, e,
            )
            return True
        finally:
            self.disconnect()