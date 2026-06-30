from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

from ..evdev import find_key_name
from ..evdev.types import KeyEvent
from ..logging import get_logger
from .gate import RelayGate

if TYPE_CHECKING:
    from ..gadgets.manager import HidGadgets

logger = get_logger(__name__)


class MouseJiggler:
    """
    Periodically moves the mouse by a small amount to prevent screen timeout.

    Fires only when the USB host is configured, regardless of the user pause
    state. Reset whenever any input event is successfully relayed to USB.

    Each jiggle moves one pixel right then immediately one pixel left so the
    visible cursor position never drifts over time.
    """

    def __init__(
        self,
        hid_gadgets: HidGadgets,
        relay_gate: RelayGate,
        base_interval: float = 120.0,
        jitter: float = 15.0,
    ) -> None:
        """
        :param hid_gadgets: Provides access to the mouse gadget
        :param relay_gate: Provides host connection state
        :param base_interval: Base seconds between jiggles (default: 120)
        :param jitter: Random ± seconds added to base_interval (default: 15)
        """
        self._hid_gadgets = hid_gadgets
        self._relay_gate = relay_gate
        self._base_interval = base_interval
        self._jitter = jitter

        self._enabled = True
        self._next_jiggle_time = time.monotonic() + self._sample_interval()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def toggle(self) -> bool:
        """Toggle enabled state. Returns the new enabled state."""
        self._enabled = not self._enabled
        return self._enabled

    def reset_timer(self) -> None:
        """
        Push the next jiggle forward on user activity. Only extends the timer,
        never shortens it.
        """
        earliest_next = time.monotonic() + self._base_interval
        if self._next_jiggle_time < earliest_next:
            self._next_jiggle_time = earliest_next

    def _sample_interval(self) -> float:
        return self._base_interval + random.uniform(-self._jitter, self._jitter)

    async def run(self) -> None:
        """
        Background coroutine: polls every second and jiggles the mouse when due.
        Designed to run as an asyncio Task; exits cleanly on CancelledError.
        """
        logger.debug(
            "MouseJiggler: Started (base_interval=%.0fs, jitter=±%.0fs)",
            self._base_interval,
            self._jitter,
        )
        while True:
            try:
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                logger.debug("MouseJiggler: Stopped.")
                break

            now = time.monotonic()
            if now < self._next_jiggle_time:
                continue

            if self._enabled and self._relay_gate.state.host_configured:
                await self._perform_jiggle()
            elif not self._enabled:
                logger.debug("MouseJiggler: Skipping jiggle (disabled by user)")
            else:
                logger.debug("MouseJiggler: Skipping jiggle (host not connected)")

            interval = self._sample_interval()
            self._next_jiggle_time = now + interval
            logger.debug("MouseJiggler: Next jiggle in %.1fs", interval)

    async def _perform_jiggle(self) -> None:
        """Move +1 pixel then -1 pixel so the cursor returns to its original position."""
        mouse = self._hid_gadgets.mouse
        if mouse is None:
            logger.warning("MouseJiggler: Mouse gadget not available; skipping jiggle")
            return
        try:
            await mouse.move(x=1, y=0)
            await mouse.move(x=-1, y=0)
            logger.info("MouseJiggler: Jiggled mouse")
        except Exception as exc:
            logger.warning("MouseJiggler: Failed to jiggle mouse: %s", exc)


class JigglerToggler:
    """
    Tracks a user-defined shortcut and toggles the mouse jiggler on/off.

    Uses the same key-suppression approach as ShortcutToggler so the shortcut
    keys are not forwarded to the USB host.
    """

    def __init__(self, shortcut_keys: set[str], mouse_jiggler: MouseJiggler) -> None:
        """
        :param shortcut_keys: Set of evdev-style key names (e.g. {"KEY_LEFTCTRL", "KEY_F11"})
        :param mouse_jiggler: The MouseJiggler instance to toggle
        """
        self._shortcut_keys = shortcut_keys
        self._mouse_jiggler = mouse_jiggler

        self._currently_pressed: set[str] = set()
        self._suppressed_keys: set[str] = set()
        self._shortcut_armed = True

    def handle_key_event(self, event: KeyEvent) -> bool:
        """
        Process a key event to detect the jiggler toggle shortcut.

        :return: True if the event was consumed and should not be forwarded
        """
        key_name = find_key_name(event)
        if key_name is None:
            return False

        if event.keystate == KeyEvent.key_down:
            self._currently_pressed.add(key_name)
        elif event.keystate == KeyEvent.key_up:
            self._currently_pressed.discard(key_name)
            if key_name in self._suppressed_keys:
                self._suppressed_keys.discard(key_name)
                if not self._suppressed_keys:
                    self._shortcut_armed = True
                return True
            if self._shortcut_keys and key_name in self._shortcut_keys:
                self._shortcut_armed = True

        if self._shortcut_armed and self._shortcut_keys and self._shortcut_keys.issubset(self._currently_pressed):
            self._shortcut_armed = False
            self._suppressed_keys.update(self._shortcut_keys)
            enabled = self._mouse_jiggler.toggle()
            logger.info("JigglerToggler: Mouse jiggler is now %s.", "ON" if enabled else "OFF")
            return True

        return key_name in self._suppressed_keys
