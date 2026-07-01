from __future__ import annotations

from ..evdev import find_key_name
from ..evdev.types import KeyEvent
from ..logging import get_logger
from .gate import RelayGate

logger = get_logger(__name__)


class ShortcutToggler:
    """
    Tracks a user-defined shortcut and toggles relaying on/off when the shortcut is pressed.
    """

    def __init__(self, shortcut_keys: set[str], relay_gate: RelayGate) -> None:
        """
        :param shortcut_keys: A set of evdev-style key names to detect
        :param relay_gate: RelayGate controlling whether relaying is active
        """
        self._shortcut_keys = shortcut_keys
        self._relay_gate = relay_gate

        self._currently_pressed: set[str] = set()
        self._pending_release: set[str] = set()
        self._trigger_key: str | None = None
        self._shortcut_armed = True

    def handle_key_event(self, event: KeyEvent) -> bool:
        """
        Process a key press or release to detect the toggle shortcut.

        Only the trigger key (the one whose key-down completes the shortcut)
        is suppressed. The other shortcut keys were already forwarded to the
        host when pressed, so their key-ups are forwarded too, instead of
        being swallowed and leaving a stuck modifier on the host. The toggle
        itself still waits until every shortcut key has been released (not
        the initial key-down), so the device grab state only changes once
        the local terminal has seen a complete press+release cycle for every
        modifier.

        :param event: The incoming KeyEvent from evdev
        :type event: KeyEvent
        """
        key_name = find_key_name(event)
        if key_name is None:
            return False

        if event.keystate == KeyEvent.key_down:
            self._currently_pressed.add(key_name)
            if self._shortcut_armed and self._shortcut_keys and self._shortcut_keys.issubset(self._currently_pressed):
                self._shortcut_armed = False
                self._trigger_key = key_name
                self._pending_release = set(self._shortcut_keys)
            return key_name == self._trigger_key

        if event.keystate == KeyEvent.key_up:
            self._currently_pressed.discard(key_name)
            is_trigger = key_name == self._trigger_key
            if key_name in self._pending_release:
                self._pending_release.discard(key_name)
                if not self._pending_release:
                    self._shortcut_armed = True
                    self._trigger_key = None
                    self.toggle_relaying()
            elif self._shortcut_keys and key_name in self._shortcut_keys:
                self._shortcut_armed = True
            return is_trigger

        return False

    def toggle_relaying(self) -> None:
        """
        Toggle the global relaying state: if it was on, turn it off, otherwise turn it on.
        """
        if self._relay_gate.toggle_user_enabled():
            logger.info("User pause is now disabled.")
        else:
            logger.info("User pause is now enabled.")
