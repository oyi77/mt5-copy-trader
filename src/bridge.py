"""Core copy-trade bridge — polls master, detects changes, broadcasts to hub."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    # EA-only master mode: the bridge polls TradeSender.mq5's signal file and
    # never touches MT5 IPC, so the package is optional at import time.
    mt5 = None
    _MT5_AVAILABLE = False

from src.config import Config, FollowerConfig
from src.master import MasterMonitor
from src.master_ea import MasterSignalFile
from src.follower import FollowerExecutor
from src.ea_watchdog import EaWatchdog
from src.state import SharedState

logger = logging.getLogger(__name__)


def _event_to_dict(event) -> dict:
    """Serialize TradeEvent to dict for JSON transmission."""
    d = {
        "action": event.action,
        "symbol": event.symbol,
        "volume": event.volume,
        "price": event.price,
        "sl": event.sl,
        "tp": event.tp,
        "master_ticket": event.master_ticket,
        "position_type": event.position_type,
        "comment": event.comment,
        "magic": event.magic,
        "prev_volume": event.prev_volume,
    }
    if event.order_type is not None:
        d["order_type"] = event.order_type
    if event.expiration is not None:
        d["expiration"] = event.expiration
    return d


class CopyTradeBridge:
    """Main loop: poll master → detect changes → update state + queue.

    Runs in a dedicated thread. Puts trade events on asyncio.Queue for the
    hub to broadcast to connected agents. Also executes trades on any
    activated local followers.
    """

    def __init__(
        self,
        config: Config,
        state: SharedState,
        event_queue: asyncio.Queue,
        loop: asyncio.AbstractEventLoop,
        event_store=None,
    ):
        self._cfg = config
        self._state = state
        self._queue = event_queue
        self._loop = loop
        self._event_store = event_store
        if config.master.ea_signals_file:
            self._file_master = True
            self._master: MasterMonitor | MasterSignalFile = MasterSignalFile(
                config.master.ea_signals_file
            )
            # Optional auto-recovery for the EA-mode master terminal.
            self._watchdog: Optional[EaWatchdog] = EaWatchdog(
                config.master.path,
                attach_script=config.master.ea_watchdog_attach_script,
                login=config.master.login,
                password=config.master.password,
                server=config.master.server,
            )
        else:
            self._file_master = False
            self._master = MasterMonitor(config.master)
            self._watchdog = None
        self._running = False
        self._active_followers: dict[str, FollowerExecutor] = {}
        self._activation_results: dict[str, tuple[bool, str]] = {}
        self._activation_pending: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Blocking run — call from a thread."""
        logger.info("%s", "=" * 60)
        logger.info("BRIDGE - starting")
        if self._file_master:
            logger.info("  Master source: EA signal file (%s)", self._cfg.master.ea_signals_file)
        else:
            logger.info("  Master port: %d", self._cfg.master.port)
        logger.info("  Poll interval: %d ms", self._cfg.poll_interval_ms)
        logger.info("%s", "=" * 60)

        self._state.stats.start_time = time.time()

        # Initial snapshot
        self._take_snapshot()
        self._state.stats.master_connected = True

        self._running = True
        while self._running:
            try:
                # Process any pending follower activations from the API
                self._process_pending_activations()
                self._tick()
            except Exception:
                logger.exception("Bridge cycle error")
                self._state.update_stats(errors=self._state.stats.errors + 1)
                time.sleep(2.0)

        self._log_shutdown()
        self._deactivate_all()

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Local Follower Management (queue-based — MT5 calls run in bridge thread)
    # ------------------------------------------------------------------

    def activate_follower(self, name: str) -> tuple[bool, str]:
        """Queue activation for the bridge thread. Returns previous result if any."""
        # Check if already active
        if name in self._active_followers:
            return False, f"Follower '{name}' is already active"

        # Find config
        cfg = None
        for fc in self._cfg.followers:
            if fc.name == name:
                cfg = fc
                break
        if cfg is None:
            return False, f"Follower '{name}' not found in config"

        # Check if activation already pending
        if name in self._activation_pending:
            return False, f"Follower '{name}' activation already in progress"

        # Queue for bridge thread
        self._activation_pending.append(name)
        # Remove any stale result
        self._activation_results.pop(name, None)
        return True, f"Follower '{name}' activation queued"

    def check_activation(self, name: str) -> Optional[tuple[bool, str]]:
        """Check if activation completed. Returns None if still pending."""
        return self._activation_results.get(name)

    def _process_pending_activations(self) -> None:
        """Bridge thread: process queued activations one per cycle."""
        if not self._activation_pending:
            return
        name = self._activation_pending.pop(0)
        logger.info("Processing queued activation for '%s'...", name)

        # Find config
        cfg = next((fc for fc in self._cfg.followers if fc.name == name), None)
        if cfg is None:
            self._activation_results[name] = (False, f"Follower '{name}' not found")
            return

        if not _MT5_AVAILABLE:
            self._activation_results[name] = (
                False,
                f"MetaTrader5 package not installed — cannot activate '{name}' "
                "(EA-only master mode has no IPC; use an agent on the follower machine)",
            )
            return

        executor = FollowerExecutor(cfg)

        # Launch MT5 terminal
        launched = executor.launch_terminal()
        if not launched:
            self._activation_results[name] = (False, f"Failed to launch MT5 terminal for '{name}'")
            return

        # Connect with login credentials
        if not executor.connect():
            self._activation_results[name] = (False, f"MT5 connect failed for '{name}'")
            return

        # Verify we're logged into the right account (safety check)
        try:
            acc = mt5.account_info()
            if not acc:
                executor.disconnect()
                self._activation_results[name] = (False, f"MT5 connected but no account info for '{name}'")
                return
            if acc.login != cfg.login:
                logger.warning(
                    "%s: connected to login %d instead of configured %d — forcing relogin",
                    name, acc.login, cfg.login,
                )
                mt5.shutdown()
                if not executor.connect():
                    self._activation_results[name] = (False, f"Failed to relogin for '{name}'")
                    return
                acc = mt5.account_info()

            self._state.register_follower_connection(
                name, acc.login, acc.server, acc.balance, acc.equity
            )
            logger.info(
                "%s: logged in as %d@%s (balance=%.2f %s)",
                name, acc.login, acc.server, acc.balance, acc.currency,
            )
        except Exception as e:
            executor.disconnect()
            self._activation_results[name] = (False, f"Account verification failed for '{name}': {e}")
            return
        finally:
            executor.disconnect()

        self._active_followers[name] = executor
        self._state.set_follower_active(name, True)
        self._activation_results[name] = (True, f"Follower '{name}' activated")
        logger.info("Follower '%s' activated (MT5 port %d)", name, cfg.port)

    def deactivate_follower(self, name: str) -> tuple[bool, str]:
        """Stop following for a local follower."""
        executor = self._active_followers.pop(name, None)
        if executor is None:
            return False, f"Follower '{name}' is not active"

        try:
            executor.disconnect()
        except Exception:
            pass

        self._state.set_follower_active(name, False)
        logger.info("Follower '%s' deactivated", name)
        return True, f"Follower '{name}' deactivated"

    def get_active_followers(self) -> dict[str, dict]:
        """Return status dict for all active followers."""
        result = {}
        for name, ex in self._active_followers.items():
            try:
                result[name] = ex.get_status()
            except Exception as e:
                result[name] = {"name": name, "active": True, "connected": False, "error": str(e)}
        return result

    def _deactivate_all(self) -> None:
        for name in list(self._active_followers.keys()):
            self.deactivate_follower(name)

    # ------------------------------------------------------------------
    # Cycle
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        self._state.update_stats(cycles=self._state.stats.cycles + 1)

        if self._file_master:
            self._tick_file_master()
            return

        # 1. Poll master
        if not self._master.connect():
            self._state.stats.master_connected = False
            time.sleep(1.0)
            return

        try:
            positions = self._master.poll()
            orders = self._master.poll_orders()
            account = self._get_account_info()
        finally:
            self._master.disconnect()

        self._state.stats.master_connected = True

        # 2. Update master state for dashboard
        self._update_master_state(positions, account)

        # 3. Detect changes (positions + orders)
        events = self._master.detect_changes(positions)
        events += self._master.detect_order_changes(orders)

        if not events:
            time.sleep(self._cfg.poll_interval_ms / 1000.0)
            return

        # 4. Record + broadcast events (existing — for remote agents)
        self._broadcast_events(events)
        time.sleep(0.1)

    def _tick_file_master(self) -> None:
        """EA-mode cycle: read signal file, relay events, update dashboard."""
        events = self._master.poll_events()
        if events is None:
            # Signal file missing or heartbeat stale — master unreachable.
            self._state.stats.master_connected = False
            if self._cfg.master.ea_watchdog and self._watchdog is not None:
                self._try_recover_ea()
            time.sleep(1.0)
            return

        self._state.stats.master_connected = True
        self._update_master_state([], self._master.last_account())

        if not events:
            time.sleep(self._cfg.poll_interval_ms / 1000.0)
            return

        self._broadcast_events(events)
        time.sleep(0.1)

    def _try_recover_ea(self) -> None:
        """Run one EA-watchdog recovery cycle; re-broadcast any events that
        arrived while the terminal was being brought back."""
        recovered_events: list = []

        def wait_alive(timeout: float) -> bool:
            alive, collected = self._wait_ea_alive(timeout)
            recovered_events.extend(collected)
            return alive

        try:
            result = self._watchdog.attempt_recovery(wait_alive)
            logger.warning("EA watchdog: %s", result)
        except Exception:
            logger.exception("EA watchdog recovery failed")
        if recovered_events:
            logger.info(
                "EA watchdog: re-broadcasting %d events received during recovery",
                len(recovered_events),
            )
            self._broadcast_events(recovered_events)

    def _wait_ea_alive(self, timeout: float) -> tuple[bool, list]:
        """Poll the EA signal file until the heartbeat resumes or timeout.

        Returns (alive, events) — trade events emitted during the wait are
        collected, never dropped (at-least-once delivery).
        """
        deadline = time.monotonic() + timeout
        collected: list = []
        while time.monotonic() < deadline:
            events = self._master.poll_events()
            if events is not None:
                collected.extend(events)
                return True, collected
            time.sleep(1.0)
        return False, collected

    def _broadcast_events(self, events: list) -> None:
        """Record events in the store and hand them to the hub + local followers."""
        self._state.update_stats(
            events_detected=self._state.stats.events_detected + len(events),
            last_event_time=time.time(),
        )
        self._state.known_tickets = set(self._master.known_tickets)

        for event in events:
            logger.info(
                "EVENT: %s %s %.2f %s (ticket=%d)",
                event.action.upper(), event.symbol, event.volume,
                "BUY" if event.position_type == 0 else "SELL",
                event.master_ticket,
            )

        # Put events on the queue for broadcast to remote agents
        dict_events = [_event_to_dict(e) for e in events]

        # Persist to event store (seq_id embedded in each dict)
        if self._event_store:
            for de in dict_events:
                seq_id = self._event_store.append_event(de)
                de["_seq_id"] = seq_id

        self._loop.call_soon_threadsafe(self._queue.put_nowait, dict_events)

        # Execute on active local followers
        if self._active_followers:
            self._execute_on_followers(events)

    def _execute_on_followers(self, events: list) -> None:
        """Execute trade events on all active local followers."""
        for name, executor in list(self._active_followers.items()):
            try:
                for event in events:
                    success = executor.execute(event)
                    self._state.record_follower_event(name, success)
                    if not success:
                        logger.warning("%s: event %s %s failed", name, event.action, event.symbol)
            except Exception as e:
                logger.error("%s: execution error: %s", name, e)
                self._state.record_follower_error(name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _take_snapshot(self) -> None:
        if self._file_master:
            logger.info("Taking master EA baseline (skips existing history)...")
            self._master.snapshot()
            self._update_master_state([], self._master.last_account())
            return
        logger.info("Taking master position snapshot...")
        if not self._master.connect():
            logger.error("Cannot take snapshot - master unreachable")
            return
        try:
            positions = self._master.poll()
            orders = self._master.poll_orders()
            self._master.snapshot(positions, orders)
            account = self._get_account_info()
            self._update_master_state(positions, account)
            logger.info("Master snapshot: %d positions, %d pending orders", len(positions), len(orders))
        finally:
            self._master.disconnect()

    def _update_master_state(self, positions, account) -> None:
        pos_list = []
        for p in positions:
            pos_list.append({
                "ticket": p.ticket,
                "symbol": p.symbol,
                "volume": p.volume,
                "type": p.type,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "swap": 0.0,
                "comment": p.comment,
                "magic": p.magic,
            })
        acc_info = {}
        if account:
            acc_info = {
                "balance": account.balance,
                "equity": account.equity,
                "margin": account.margin,
                "margin_free": account.margin_free,
                "leverage": account.leverage,
                "currency": account.currency,
                "login": account.login,
                "server": account.server,
                "name": account.name,
            }
        self._state.update_master(pos_list, acc_info)

    def _get_account_info(self):
        try:
            return mt5.account_info()
        except Exception:
            return None

    def _log_shutdown(self) -> None:
        stats = self._state.get_stats()
        logger.info("%s", "=" * 60)
        logger.info("BRIDGE STOPPED")
        logger.info("  Cycles : %d", stats["cycles"])
        logger.info("  Events : %d", stats["events_detected"])
        logger.info("  Errors : %d", stats["errors"])
        logger.info("  Uptime : %.0f s", stats["uptime"])
        logger.info("%s", "=" * 60)
