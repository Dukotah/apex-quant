"""
apex.execution.base_execution
=============================
BaseExecutionEngine: the contract for turning approved OrderEvents into fills.

This is where the Live/Paper abstraction switch lives. The engine factory
reads a single config flag (MODE = paper | live) and returns the matching
concrete engine. Strategy code, risk code, and data code are all completely
unaware of which engine is active — they only ever deal with events.

  MODE=paper  → SimulatedExecutionEngine (models fills, slippage, commission)
  MODE=live   → AlpacaExecutionEngine / IBKRExecutionEngine (real broker)

Switching is a one-line config change. No strategy is ever modified.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

from apex.core.events import FillEvent, OrderEvent


class BaseExecutionEngine(ABC):
    """
    Abstract base for all execution engines.

    The engine receives approved OrderEvents (they have already passed risk),
    submits them to its venue (simulated or real), and emits FillEvents back
    onto the bus via the on_fill callback.

    Implementations MUST:
      - be idempotent on retries (never double-submit the same order)
      - emit a FillEvent for every fill (including partials)
      - handle broker errors without crashing the engine loop
      - on live disconnect, enter safe mode (cancel working orders, stop)
    """

    def __init__(self, on_fill: Optional[Callable[[FillEvent], None]] = None) -> None:
        # The engine calls on_fill(fill_event) to publish fills back to the bus.
        self._on_fill = on_fill
        self._connected: bool = False

    def bind_fill_handler(self, handler: Callable[[FillEvent], None]) -> None:
        """Engine wiring: where to send FillEvents."""
        self._on_fill = handler

    @abstractmethod
    def connect(self) -> None:
        """Authenticate / open the venue connection. Set self._connected = True."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Tear down the connection cleanly. Idempotent."""
        ...

    @abstractmethod
    def submit_order(self, order: OrderEvent) -> str:
        """
        Submit an approved order to the venue.
        Returns the broker/venue order id.
        On failure: log, retry with idempotency key, raise only if unrecoverable.
        """
        ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a working order. Returns True if cancellation accepted."""
        ...

    def cancel_open_orders(self) -> None:
        """
        Cancel ALL working/resting orders at the venue.

        Default implementation is a safe no-op: engines whose venue has no
        concept of resting orders (e.g. the simulator, which fills every order
        synchronously) inherit this and carry zero live exposure automatically.

        Concrete live engines (AlpacaExecutionEngine, etc.) MUST override this
        to issue the appropriate bulk-cancel API call so that a halted system
        leaves NO working orders at the broker.

        This method MUST NOT raise — it is called in halt and disconnect paths
        where a crash would leave the system in an unknown state.
        """

    @abstractmethod
    def get_account_equity(self) -> "Decimal":  # noqa: F821
        """Current account equity from the venue (for reconciliation)."""
        ...

    @abstractmethod
    def reconcile_positions(self) -> dict:
        """
        On startup, fetch the venue's truth for positions and return it so the
        local portfolio can sync. Prevents drift between our state and reality.
        """
        ...

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    @abstractmethod
    def is_paper(self) -> bool:
        """True for simulated/paper engines, False for live. Used for UI/labeling."""
        ...

    def _emit_fill(self, fill: FillEvent) -> None:
        """Internal: publish a fill back to the bus if a handler is bound."""
        if self._on_fill is not None:
            self._on_fill(fill)

    def __enter__(self) -> "BaseExecutionEngine":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.disconnect()
