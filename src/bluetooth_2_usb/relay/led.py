from __future__ import annotations

import asyncio
from collections.abc import Iterable

from ..evdev import ecodes
from ..evdev.types import InputDevice
from ..logging import get_logger

logger = get_logger(__name__)

LED_NAME_ALIASES: dict[str, int] = {
    "caps_lock": ecodes.LED_CAPSL,
    "num_lock": ecodes.LED_NUML,
    "scroll_lock": ecodes.LED_SCROLLL,
}
DEFAULT_LED_NAME = "scroll_lock"


def resolve_led_name(name: str) -> int:
    """
    Resolve a user-facing LED name (e.g. "caps_lock") to its evdev LED code.

    :raises ValueError: if the name isn't one of the supported choices
    """
    try:
        return LED_NAME_ALIASES[name]
    except KeyError as exc:
        choices = ", ".join(sorted(LED_NAME_ALIASES))
        raise ValueError(f"Unknown LED name: {name!r}. Choices: {choices}") from exc


class KeyboardLedBlinker:
    """
    Blinks a keyboard LED briefly to give visual feedback for state changes
    that have no other on-screen indication (e.g. toggling the mouse jiggler).
    """

    def __init__(self, led: int = ecodes.LED_SCROLLL, pulse_seconds: float = 0.15, pulse_count: int = 2) -> None:
        """
        :param led: The LED code to blink. Scroll Lock by default, since it is
            rarely used for anything else and unlikely to be confused with a
            real Caps/Num Lock state change.
        :param pulse_seconds: How long each on/off phase of a pulse lasts
        :param pulse_count: How many on/off pulses to emit
        """
        self._led = led
        self._pulse_seconds = pulse_seconds
        self._pulse_count = pulse_count

    async def blink(self, devices: Iterable[InputDevice], *, pulse_count: int | None = None) -> None:
        """
        Blink the configured LED on every device that supports it, restoring
        each device's original LED state afterwards.

        A device that lacks the LED capability, or that fails mid-blink (e.g.
        because it just disconnected), is skipped without affecting the others.

        :param pulse_count: Overrides the configured pulse count for this call
        """
        count = self._pulse_count if pulse_count is None else pulse_count
        for device in devices:
            await self._blink_device(device, count)

    async def _blink_device(self, device: InputDevice, pulse_count: int) -> None:
        try:
            supported_leds = device.capabilities(verbose=False).get(ecodes.EV_LED, [])
            if self._led not in supported_leds:
                return
            original = self._led in device.leds()
            for pulse in range(pulse_count):
                device.set_led(self._led, 0 if original else 1)
                await asyncio.sleep(self._pulse_seconds)
                device.set_led(self._led, 1 if original else 0)
                if pulse < pulse_count - 1:
                    await asyncio.sleep(self._pulse_seconds)
        except OSError as exc:
            logger.debug("Skipping LED blink for %s: %s", device.path, exc)
        except Exception:
            logger.debug("Unexpected error blinking LED for %s.", device.path, exc_info=True)
