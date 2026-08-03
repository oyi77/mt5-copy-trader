"""Disk-persisted trade-event replay queue for a follower.

This is a mixin (no ``__init__``) — the composing executor provides
``_queue_lock`` (a reentrant ``threading.RLock``), ``_cfg.queue_path``, and
``_name``. ``_dequeue_and_replay`` calls ``self.execute()``; the lock is
reentrant so that a reconnect-failure inside ``execute()`` -> ``_enqueue_event``
can re-acquire it safely.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time

from src.models import TradeEvent

logger = logging.getLogger(__name__)


class QueueMixin:
    """Local trade-event queue persisted to disk for replay after failures."""

    def _load_queue(self) -> list[dict]:
        """Load queued events from disk."""
        with self._queue_lock:
            if not os.path.exists(self._cfg.queue_path):
                return []
            try:
                with open(self._cfg.queue_path, "r") as f:
                    data = json.load(f)
                # Guard against a corrupted queue file (json.load returning a
                # non-list) — reset to an empty queue.
                if not isinstance(data, list):
                    logger.warning(
                        "%s: queue file %s is corrupted (not a list), resetting to empty",
                        self._name, self._cfg.queue_path,
                    )
                    return []
                return data
            except Exception as e:
                logger.warning(
                    "%s: failed to load queue from %s: %s",
                    self._name, self._cfg.queue_path, e,
                )
                return []

    def _save_queue(self, queue: list[dict]) -> None:
        """Save queued events to disk atomically (tmp file + rename)."""
        with self._queue_lock:
            tmp_path = self._cfg.queue_path + ".tmp"
            try:
                with open(tmp_path, "w") as f:
                    json.dump(queue, f, indent=2)
                os.replace(tmp_path, self._cfg.queue_path)
            except Exception as e:
                logger.warning(
                    "%s: failed to save queue to %s: %s",
                    self._name, self._cfg.queue_path, e,
                )

    def _enqueue_event(self, event: TradeEvent) -> None:
        """Persist a trade event to the local disk queue for later replay."""
        with self._queue_lock:
            queue = self._load_queue()
            entry = {
                "event": dataclasses.asdict(event),
                "timestamp": time.time(),
                "retry_count": 0,
            }
            queue.append(entry)
            self._save_queue(queue)
            logger.info(
                "%s: event queued (action=%s ticket=%d, queue size=%d)",
                self._name, event.action, event.master_ticket, len(queue),
            )

    def _dequeue_and_replay(self) -> None:
        """Replay all queued events, dropping entries that exceed retry limit."""
        with self._queue_lock:
            queue = self._load_queue()
            if not queue:
                return
            logger.info(
                "%s: replaying %d queued events...", self._name, len(queue),
            )

            def _entry_key(e: dict) -> tuple:
                # Stable key for dedupe: the event payload (flat primitive dict).
                return tuple(sorted(e.get("event", {}).items()))

            remaining: list[dict] = []
            for entry in queue:
                try:
                    event_dict = entry["event"]
                    # Reconstruct TradeEvent from dict
                    event = TradeEvent(**event_dict)
                    # execute() may persist a fresh copy via _enqueue_event
                    # (connect failure, risk limits, open/place failure). Track
                    # it so the final save below keeps that copy instead of a
                    # stale original overwriting it.
                    queue_len_before = len(self._load_queue())
                    if self.execute(event):
                        logger.info(
                            "%s: replayed queued event for ticket %d",
                            self._name, event.master_ticket,
                        )
                    elif len(self._load_queue()) > queue_len_before:
                        logger.warning(
                            "%s: queued event ticket=%d failed and re-enqueued "
                            "itself, keeping fresh copy",
                            self._name, event.master_ticket,
                        )
                    else:
                        entry["retry_count"] = entry.get("retry_count", 0) + 1
                        if entry["retry_count"] < 3:
                            remaining.append(entry)
                            logger.warning(
                                "%s: queued event ticket=%d failed, %d/3 retries",
                                self._name, event.master_ticket,
                                entry["retry_count"],
                            )
                        else:
                            logger.warning(
                                "%s: dropping queued event ticket=%d after %d failed retries",
                                self._name, event.master_ticket,
                                entry["retry_count"],
                            )
                except Exception as e:
                    logger.warning(
                        "%s: error replaying queued event: %s", self._name, e,
                    )
                    # Keep retrying on deserialization errors (up to 3)
                    entry["retry_count"] = entry.get("retry_count", 0) + 1
                    if entry["retry_count"] < 3:
                        remaining.append(entry)

            # Preserve fresh copies that execute() re-enqueued during replay
            # (they were appended to the on-disk queue after our initial load);
            # _save_queue(remaining) alone would overwrite and lose them.
            fresh = self._load_queue()[len(queue):]
            fresh_keys = {_entry_key(e) for e in fresh}
            for entry in remaining:
                if _entry_key(entry) not in fresh_keys:
                    fresh.append(entry)
                    fresh_keys.add(_entry_key(entry))
            self._save_queue(fresh)
            if fresh:
                logger.info(
                    "%s: %d events still queued after replay",
                    self._name, len(fresh),
                )