import asyncio
from asyncio import Task, TaskGroup
from pathlib import Path
import random
import re
import time
from typing import Optional, Union

from adafruit_hid.consumer_control import ConsumerControl
from adafruit_hid.keyboard import Keyboard
from adafruit_hid.mouse import Mouse
from evdev import InputDevice, InputEvent, KeyEvent, RelEvent, categorize, list_devices
import pyudev
import usb_hid
from usb_hid import Device

from .evdev import (
    evdev_to_usb_hid,
    find_key_name,
    get_mouse_movement,
    is_consumer_key,
    is_mouse_button,
)
from .logging import get_logger

_logger = get_logger()


class GadgetManager:
    """
    Manages enabling, disabling, and references to USB HID gadget devices.

    :ivar _gadgets: Internal dictionary mapping device types to HID device objects
    :ivar _enabled: Indicates whether the gadgets have been enabled
    """

    def __init__(self) -> None:
        """
        Initialize without enabling devices. Call enable_gadgets() to enable them.
        """
        self._gadgets = {
            "keyboard": None,
            "mouse": None,
            "consumer": None,
        }
        self._enabled = False

    def enable_gadgets(self) -> None:
        """
        Enable usb_hid devices and store references to Keyboard, Mouse, and ConsumerControl gadgets.
        Only enables once - does not disable first to avoid Windows compatibility issues.
        """
        if self._enabled:
            _logger.debug("USB HID gadgets already enabled, skipping re-initialization")
            return

        # Try BOOT_KEYBOARD to match BOOT_MOUSE protocol
        usb_hid.enable([Device.BOOT_KEYBOARD, Device.BOOT_MOUSE])  # type: ignore
        enabled_devices = list(usb_hid.devices)  # type: ignore

        _logger.info(f"Enabled devices: {enabled_devices}")

        # Create keyboard and mouse - order matters!
        self._gadgets["keyboard"] = Keyboard(enabled_devices)
        self._gadgets["mouse"] = Mouse(enabled_devices)

        _logger.info(f"Keyboard gadget device: {self._gadgets['keyboard']._keyboard_device if hasattr(self._gadgets['keyboard'], '_keyboard_device') else 'unknown'}")
        _logger.info(f"Mouse gadget device: {self._gadgets['mouse']._mouse_device if hasattr(self._gadgets['mouse'], '_mouse_device') else 'unknown'}")
        # TEMPORARY: Skip consumer control for Windows debugging
        # self._gadgets["consumer"] = ConsumerControl(enabled_devices)
        self._gadgets["consumer"] = None
        self._enabled = True

        # Send initial HID reports to help Windows recognize and enumerate the devices
        _logger.info("Sending initial HID reports to host...")

        # Release all first
        self._gadgets["keyboard"].release_all()
        self._gadgets["mouse"].release_all()

        # WORKAROUND: Send a dummy keypress to force Windows to recognize the keyboard
        # Some Windows versions won't enumerate the keyboard until it sends actual data
        import time
        from adafruit_hid.keycode import Keycode
        time.sleep(0.1)  # Small delay
        self._gadgets["keyboard"].press(Keycode.SCROLL_LOCK)
        time.sleep(0.05)
        self._gadgets["keyboard"].release(Keycode.SCROLL_LOCK)
        time.sleep(0.05)
        self._gadgets["keyboard"].release_all()

        _logger.info("Initial HID reports sent (including keyboard wake-up)")

        _logger.debug(f"USB HID gadgets initialized: {enabled_devices}")

    def get_keyboard(self) -> Optional[Keyboard]:
        """
        Get the Keyboard gadget.

        :return: A Keyboard object, or None if not initialized
        :rtype: Keyboard | None
        """
        return self._gadgets["keyboard"]

    def get_mouse(self) -> Optional[Mouse]:
        """
        Get the Mouse gadget.

        :return: A Mouse object, or None if not initialized
        :rtype: Mouse | None
        """
        return self._gadgets["mouse"]

    def get_consumer(self) -> Optional[ConsumerControl]:
        """
        Get the ConsumerControl gadget.

        :return: A ConsumerControl object, or None if not initialized
        :rtype: ConsumerControl | None
        """
        return self._gadgets["consumer"]


class ShortcutToggler:
    """
    Tracks a user-defined shortcut and toggles relaying on/off when the shortcut is pressed.
    """

    def __init__(
        self,
        shortcut_keys: set[str],
        relaying_active: asyncio.Event,
        gadget_manager: GadgetManager,
    ) -> None:
        """
        :param shortcut_keys: A set of evdev-style key names to detect
        :param relaying_active: An asyncio.Event controlling whether relaying is active
        :param gadget_manager: GadgetManager to release keyboard/mouse states on toggle
        """
        self.shortcut_keys = shortcut_keys
        self.relaying_active = relaying_active
        self.gadget_manager = gadget_manager

        self.currently_pressed: set[str] = set()
        self._shortcut_triggered = False  # Prevent multiple toggles while held

    def handle_key_event(self, event: KeyEvent) -> bool:
        """
        Process a key press or release to detect the toggle shortcut.

        :param event: The incoming KeyEvent from evdev
        :type event: KeyEvent
        :return: True if the event should be consumed (not forwarded), False otherwise
        :rtype: bool
        """
        key_name = find_key_name(event)
        if key_name is None:
            return False

        if event.keystate == KeyEvent.key_down:
            self.currently_pressed.add(key_name)
        elif event.keystate == KeyEvent.key_up:
            self.currently_pressed.discard(key_name)
            # Reset trigger flag when any key is released
            if not self.shortcut_keys.issubset(self.currently_pressed):
                self._shortcut_triggered = False

        # Only trigger once when shortcut is first detected
        if self.shortcut_keys and self.shortcut_keys.issubset(self.currently_pressed):
            if not self._shortcut_triggered:
                self._shortcut_triggered = True
                self.toggle_relaying()
            # Consume the event when all shortcut keys are pressed
            return True

        return False

    def toggle_relaying(self) -> None:
        """
        Toggle the global relaying state: if it was on, turn it off, otherwise turn it on.
        Explicitly releases shortcut keys on USB to prevent stuck keys.
        """
        keyboard = self.gadget_manager.get_keyboard()
        mouse = self.gadget_manager.get_mouse()

        if self.relaying_active.is_set():
            # Turning OFF: Release all keys and mouse buttons
            if keyboard:
                keyboard.release_all()
            if mouse:
                mouse.release_all()

            self.currently_pressed.clear()
            self.relaying_active.clear()
            _logger.info("ShortcutToggler: Relaying is now OFF.")
        else:
            # Turning ON: Release all to ensure clean state (in case shortcut keys still held)
            if keyboard:
                keyboard.release_all()
            if mouse:
                mouse.release_all()

            self.relaying_active.set()
            _logger.info("ShortcutToggler: Relaying is now ON.")


class JigglerToggler:
    """
    Tracks a user-defined shortcut and toggles mouse jiggler on/off when the shortcut is pressed.
    """

    def __init__(
        self,
        shortcut_keys: set[str],
        jiggler_enabled: asyncio.Event,
        gadget_manager: GadgetManager,
    ) -> None:
        """
        :param shortcut_keys: A set of evdev-style key names to detect
        :param jiggler_enabled: An asyncio.Event controlling whether jiggler is enabled
        :param gadget_manager: GadgetManager to release keyboard/mouse states on toggle
        """
        self.shortcut_keys = shortcut_keys
        self.jiggler_enabled = jiggler_enabled
        self.gadget_manager = gadget_manager

        self.currently_pressed: set[str] = set()
        self._shortcut_triggered = False  # Prevent multiple toggles while held

    def handle_key_event(self, event: KeyEvent) -> bool:
        """
        Process a key press or release to detect the toggle shortcut.

        :param event: The incoming KeyEvent from evdev
        :type event: KeyEvent
        :return: True if the event should be consumed (not forwarded), False otherwise
        :rtype: bool
        """
        key_name = find_key_name(event)
        if key_name is None:
            return False

        if event.keystate == KeyEvent.key_down:
            self.currently_pressed.add(key_name)
        elif event.keystate == KeyEvent.key_up:
            self.currently_pressed.discard(key_name)
            # Reset trigger flag when any key is released
            if not self.shortcut_keys.issubset(self.currently_pressed):
                self._shortcut_triggered = False

        # Only trigger once when shortcut is first detected
        if self.shortcut_keys and self.shortcut_keys.issubset(self.currently_pressed):
            if not self._shortcut_triggered:
                self._shortcut_triggered = True
                self.toggle_jiggler()
            # Consume the event when all shortcut keys are pressed
            return True

        return False

    def toggle_jiggler(self) -> None:
        """
        Toggle the jiggler enabled state: if it was on, turn it off, otherwise turn it on.
        Explicitly releases shortcut keys on USB to prevent stuck keys.
        """
        keyboard = self.gadget_manager.get_keyboard()
        mouse = self.gadget_manager.get_mouse()

        # Release all keys and mouse buttons to prevent stuck keys from shortcut
        if keyboard:
            keyboard.release_all()
        if mouse:
            mouse.release_all()

        if self.jiggler_enabled.is_set():
            self.jiggler_enabled.clear()
            _logger.info("JigglerToggler: Mouse jiggler is now OFF.")
        else:
            self.jiggler_enabled.set()
            _logger.info("JigglerToggler: Mouse jiggler is now ON.")


class MouseJiggler:
    """
    Periodically moves the mouse by 1 pixel to prevent screen timeout.
    Only jiggles if no input activity has occurred for approximately 2 minutes.
    """

    def __init__(
        self,
        gadget_manager: GadgetManager,
        jiggler_enabled: asyncio.Event,
        usb_connected: asyncio.Event,
        base_interval: float = 120.0,
        jitter: float = 15.0,
    ) -> None:
        """
        :param gadget_manager: Provides access to the USB HID mouse gadget
        :param jiggler_enabled: Event indicating if jiggler is enabled by user
        :param usb_connected: Event indicating if USB is connected and configured (from UDC state)
        :param base_interval: Base interval in seconds between jiggles (default: 120s / 2 minutes)
        :param jitter: Random jitter in seconds to add/subtract from base_interval (default: ±15s)
        """
        self.gadget_manager = gadget_manager
        self.jiggler_enabled = jiggler_enabled
        self.usb_connected = usb_connected
        self.base_interval = base_interval
        self.jitter = jitter

        self._stop = False
        self._task: Optional[asyncio.Task] = None
        # Set the absolute time when next jiggle should occur
        self._next_jiggle_time = time.monotonic() + self._get_next_interval()

    async def __aenter__(self):
        """
        Async context manager entry. Starts the jiggler task.
        """
        self._stop = False
        interval = self._next_jiggle_time - time.monotonic()
        self._task = asyncio.create_task(self._jiggle_loop())
        _logger.debug(f"MouseJiggler: Started jiggle loop (first jiggle in {interval:.1f}s).")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Async context manager exit. Stops the jiggler task.
        """
        if self._task:
            self._stop = True
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        _logger.debug("MouseJiggler: Stopped jiggle loop.")
        return False

    def reset_timer(self) -> None:
        """
        Reset the jiggle timer. Should be called whenever input is relayed to USB.
        Simply pushes the target time forward without recalculating randomness.
        """
        current_time = time.monotonic()
        # Only push forward if we're not already far enough in the future
        if self._next_jiggle_time < current_time + self.base_interval:
            self._next_jiggle_time = current_time + self.base_interval

    def _get_next_interval(self) -> float:
        """
        Calculate the next interval with random jitter.

        :return: Interval in seconds
        :rtype: float
        """
        return self.base_interval + random.uniform(-self.jitter, self.jitter)

    async def _jiggle_loop(self) -> None:
        """
        Main loop that periodically checks if it should jiggle the mouse.
        Works independently of relay state to keep OTG device awake.

        Logic:
        1. Wait until the target jiggle time is reached
        2. Check if jiggling is enabled and USB is connected
        3. If yes, perform jiggle; if no, skip jiggle
        4. Set a new random target time ~2 minutes in the future
        5. Repeat
        """
        while not self._stop:
            try:
                # Check every second if it's time to jiggle
                await asyncio.sleep(1.0)

                current_time = time.monotonic()

                # Check if it's time to jiggle
                if current_time >= self._next_jiggle_time:
                    # Time to jiggle! But only if enabled and USB is connected
                    jiggler_enabled = self.jiggler_enabled.is_set()
                    usb_connected = self.usb_connected.is_set()

                    if jiggler_enabled and usb_connected:
                        self._perform_jiggle()
                    else:
                        # Log why jiggle was skipped
                        if not jiggler_enabled and not usb_connected:
                            _logger.info("MouseJiggler: Jiggle skipped (disabled by user and USB not connected)")
                        elif not jiggler_enabled:
                            _logger.info("MouseJiggler: Jiggle skipped (disabled by user)")
                        elif not usb_connected:
                            _logger.info("MouseJiggler: Jiggle skipped (USB not connected)")

                    # Set next jiggle time (regardless of whether we jiggled or not)
                    interval = self._get_next_interval()
                    self._next_jiggle_time = current_time + interval
                    _logger.debug(f"MouseJiggler: Next jiggle scheduled in {interval:.1f}s")

            except asyncio.CancelledError:
                break
            except Exception:
                _logger.exception("MouseJiggler: Error in jiggle loop")

    def _perform_jiggle(self) -> None:
        """
        Move the mouse by 1 pixel in a random direction (x and/or y, +/-).
        """
        mouse = self.gadget_manager.get_mouse()
        if mouse is None:
            _logger.warning("MouseJiggler: Mouse gadget not available")
            return

        try:
            # Randomly choose direction for each axis
            # 50% chance to move on X axis, 50% on Y axis, can be both
            x_move = random.choice([-2, 0, 2])
            y_move = random.choice([-2, 0, 2])

            # Ensure we move at least in one direction
            if x_move == 0 and y_move == 0:
                # If both are 0, pick a random axis and direction
                if random.choice([True, False]):
                    x_move = random.choice([-2, 2])
                else:
                    y_move = random.choice([-2, 2])

            mouse.move(x=x_move, y=y_move)
            _logger.info(f"MouseJiggler: Jiggled mouse (x={x_move:+d}, y={y_move:+d})")
        except Exception as ex:
            _logger.warning(f"MouseJiggler: Failed to jiggle mouse: {ex}")


class RelayController:
    """
    Controls the creation and lifecycle of per-device relays.
    Monitors add/remove events from udev and includes optional auto-discovery.
    """

    def __init__(
        self,
        gadget_manager: GadgetManager,
        device_identifiers: Optional[list[str]] = None,
        auto_discover: bool = False,
        skip_name_prefixes: Optional[list[str]] = None,
        grab_devices: bool = False,
        relaying_active: Optional[asyncio.Event] = None,
        shortcut_toggler: Optional["ShortcutToggler"] = None,
        jiggler_toggler: Optional["JigglerToggler"] = None,
        mouse_jiggler: Optional["MouseJiggler"] = None,
    ) -> None:
        """
        :param gadget_manager: Provides the USB HID gadget devices
        :param device_identifiers: A list of path, MAC, or name fragments to identify devices to relay
        :param auto_discover: If True, relays all valid input devices except those skipped
        :param skip_name_prefixes: A list of device.name prefixes to skip if auto_discover is True
        :param grab_devices: If True, the relay tries to grab exclusive access to each device
        :param relaying_active: asyncio.Event to indicate if relaying is active
        :param shortcut_toggler: ShortcutToggler to allow toggling relaying globally
        :param jiggler_toggler: JigglerToggler to allow toggling jiggler via shortcut
        :param mouse_jiggler: MouseJiggler to prevent screen timeout
        """
        self._gadget_manager = gadget_manager
        self._device_ids = [DeviceIdentifier(id) for id in (device_identifiers or [])]
        self._auto_discover = auto_discover
        self._skip_name_prefixes = skip_name_prefixes or ["vc4-hdmi", "pwr_button"]
        self._grab_devices = grab_devices
        self._relaying_active = relaying_active
        self._shortcut_toggler = shortcut_toggler
        self._jiggler_toggler = jiggler_toggler
        self._mouse_jiggler = mouse_jiggler

        self._active_tasks: dict[str, Task] = {}
        self._task_group: Optional[TaskGroup] = None
        self._cancelled = False

    async def async_relay_devices(self) -> None:
        """
        Launch a TaskGroup that relays events from all matching devices.
        Dynamically adds or removes tasks when devices appear or disappear.

        :return: Never returns unless an unrecoverable exception or cancellation occurs
        :rtype: None
        """
        try:
            async with TaskGroup() as task_group:
                self._task_group = task_group
                _logger.debug("RelayController: TaskGroup started.")

                for device in await async_list_input_devices():
                    if self._should_relay(device):
                        self.add_device(device.path)

                # Keep running unless canceled
                while not self._cancelled:
                    await asyncio.sleep(0.1)
        except* Exception as exc_grp:
            _logger.exception(
                "RelayController: Exception in TaskGroup", exc_info=exc_grp
            )
        finally:
            self._task_group = None
            _logger.debug("RelayController: TaskGroup exited.")

    def add_device(self, device_path: str) -> None:
        """
        Add a device by path. If a TaskGroup is active, create a new relay task.

        :param device_path: The absolute path to the input device (e.g., /dev/input/event5)
        """
        if not Path(device_path).exists():
            _logger.debug(f"{device_path} does not exist.")
            return

        try:
            device = InputDevice(device_path)
        except (OSError, FileNotFoundError):
            _logger.debug(f"{device_path} vanished before opening.")
            return

        if self._task_group is None:
            _logger.critical(f"No TaskGroup available; ignoring {device}.")
            return

        if device.path in self._active_tasks:
            _logger.debug(f"Device {device} is already active.")
            return

        task = self._task_group.create_task(
            self._async_relay_events(device), name=device.path
        )
        self._active_tasks[device.path] = task
        _logger.debug(f"Created task for {device}.")

    def remove_device(self, device_path: str) -> None:
        """
        Cancel and remove the relay task for a given device path.

        :param device_path: The path of the device to remove
        """
        task = self._active_tasks.pop(device_path, None)
        if task and not task.done():
            task.cancel()
            _logger.debug(f"Cancelled relay for {device_path}.")
        else:
            _logger.debug(f"No active task found for {device_path} to remove.")

    async def _async_relay_events(self, device: InputDevice) -> None:
        """
        Create a DeviceRelay context, then read events in a loop until cancellation or error.

        :param device: The evdev InputDevice to relay
        """
        try:
            async with DeviceRelay(
                device,
                self._gadget_manager,
                grab_device=self._grab_devices,
                relaying_active=self._relaying_active,
                shortcut_toggler=self._shortcut_toggler,
                jiggler_toggler=self._jiggler_toggler,
                mouse_jiggler=self._mouse_jiggler,
            ) as relay:
                _logger.info(f"Activated {relay}")
                await relay.async_relay_events_loop()
        except (OSError, FileNotFoundError):
            _logger.info(f"Lost connection to {device}.")
        except Exception:
            _logger.exception(f"Unhandled exception in relay for {device}.")
        finally:
            self.remove_device(device.path)

    def _should_relay(self, device: InputDevice) -> bool:
        """
        Decide if a device should be relayed based on auto_discover,
        skip_name_prefixes, or user-specified device_identifiers.

        :param device: The input device to check
        :return: True if we should relay it, False otherwise
        :rtype: bool
        """
        name_lower = device.name.lower()
        phys_lower = (device.phys or "").lower()

        if self._auto_discover:
            # Skip devices by name prefix
            for prefix in self._skip_name_prefixes:
                if name_lower.startswith(prefix.lower()):
                    _logger.debug(f"Skipping device '{device.name}' (matches skip prefix '{prefix}')")
                    return False

            # Skip GPIO-based devices (like Raspberry Pi power button)
            if "gpio" in phys_lower:
                _logger.debug(f"Skipping device '{device.name}' (GPIO-based device: {device.phys})")
                return False

            # TEMPORARY: Skip keyboard devices that present as "Mouse" to debug Windows issue
            # Example: "Keychron K8 Pro Mouse" should be skipped
            if "mouse" in name_lower and ("keychron" in name_lower or "k8" in name_lower):
                _logger.info(f"Skipping '{device.name}' (keyboard's mouse interface)")
                return False

            return True

        return any(identifier.matches(device) for identifier in self._device_ids)


class DeviceRelay:
    """
    Relay a single InputDevice's events to USB HID gadgets.

    - Optionally grabs the device exclusively.
    - Retries HID writes if they raise BlockingIOError.
    """

    def __init__(
        self,
        input_device: InputDevice,
        gadget_manager: GadgetManager,
        grab_device: bool = False,
        relaying_active: Optional[asyncio.Event] = None,
        shortcut_toggler: Optional["ShortcutToggler"] = None,
        jiggler_toggler: Optional["JigglerToggler"] = None,
        mouse_jiggler: Optional["MouseJiggler"] = None,
    ) -> None:
        """
        :param input_device: The evdev input device
        :param gadget_manager: Provides references to Keyboard, Mouse, ConsumerControl
        :param grab_device: Whether to grab the device for exclusive access
        :param relaying_active: asyncio.Event that indicates relaying is on/off
        :param shortcut_toggler: Optional handler for toggling relay via a shortcut
        :param jiggler_toggler: Optional handler for toggling jiggler via a shortcut
        :param mouse_jiggler: Optional MouseJiggler to reset activity timer on input
        """
        self._input_device = input_device
        self._gadget_manager = gadget_manager
        self._grab_device = grab_device
        self._relaying_active = relaying_active
        self._shortcut_toggler = shortcut_toggler
        self._jiggler_toggler = jiggler_toggler
        self._mouse_jiggler = mouse_jiggler

        self._currently_grabbed = False

    def __str__(self) -> str:
        return f"relay for {self._input_device}"

    @property
    def input_device(self) -> InputDevice:
        """
        The underlying evdev InputDevice being relayed.

        :return: The InputDevice
        :rtype: InputDevice
        """
        return self._input_device

    async def __aenter__(self) -> "DeviceRelay":
        """
        Async context manager entry. Grabs the device if requested.

        :return: self
        """
        if self._grab_device:
            try:
                self._input_device.grab()
                self._currently_grabbed = True
            except Exception as ex:
                _logger.warning(f"Could not grab {self._input_device.path}: {ex}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """
        Async context manager exit. Ungrabs the device if we grabbed it.

        :return: False to propagate exceptions
        """
        if self._grab_device:
            try:
                self._input_device.ungrab()
                self._currently_grabbed = False
            except Exception as ex:
                _logger.warning(f"Unable to ungrab {self._input_device.path}: {ex}")
        return False

    async def async_relay_events_loop(self) -> None:
        """
        Continuously read events from the device and relay them
        to the USB HID gadgets. Stops when canceled or on error.

        :return: None
        """
        async for input_event in self._input_device.async_read_loop():
            event = categorize(input_event)

            # Only log KeyEvents to avoid flooding logs with mouse movements
            if isinstance(event, KeyEvent):
                _logger.debug(
                    f"Received {event} from {self._input_device.name} ({self._input_device.path})"
                )
            # Uncomment below to debug mouse movement issues:
            # if isinstance(event, RelEvent):
            #     _logger.debug(
            #         f"Received {event} from {self._input_device.name} ({self._input_device.path})"
            #     )

            # Handle shortcuts before checking relay state (shortcuts work even when relaying is paused)
            event_consumed = False
            if isinstance(event, KeyEvent):
                if self._shortcut_toggler:
                    event_consumed |= self._shortcut_toggler.handle_key_event(event)
                if self._jiggler_toggler:
                    event_consumed |= self._jiggler_toggler.handle_key_event(event)

            # Skip forwarding if event was consumed by a shortcut
            if event_consumed:
                continue

            active = self._relaying_active and self._relaying_active.is_set()

            # Debug logging for Windows troubleshooting
            if isinstance(event, KeyEvent) and not active:
                _logger.warning(
                    f"Keyboard event blocked - relaying_active is not set. "
                    f"Event: {event}, Device: {self._input_device.name}"
                )

            # Log events from devices with "Mouse" in the name for debugging
            if "mouse" in self._input_device.name.lower() and isinstance(event, KeyEvent):
                _logger.info(
                    f"KeyEvent from '{self._input_device.name}': {event}"
                )

            # Dynamically grab/ungrab if relaying state changes
            if self._grab_device and active and not self._currently_grabbed:
                try:
                    self._input_device.grab()
                    self._currently_grabbed = True
                    _logger.debug(f"Grabbed {self._input_device}")
                except Exception as ex:
                    _logger.warning(f"Could not grab {self._input_device}: {ex}")

            elif self._grab_device and not active and self._currently_grabbed:
                try:
                    self._input_device.ungrab()
                    self._currently_grabbed = False
                    _logger.debug(f"Ungrabbed {self._input_device}")
                except Exception as ex:
                    _logger.warning(f"Could not ungrab {self._input_device}: {ex}")

            if not active:
                continue

            await self._process_event_with_retry(event)

    async def _process_event_with_retry(self, event: InputEvent) -> None:
        """
        Attempt to relay the given event to the appropriate HID gadget.
        Retry on BlockingIOError up to 2 times.

        :param event: The InputEvent to process
        """
        max_tries = 3
        retry_delay = 0.1
        for attempt in range(1, max_tries + 1):
            try:
                relay_event(event, self._gadget_manager)
                # Reset mouse jiggler timer on successful relay
                if self._mouse_jiggler:
                    self._mouse_jiggler.reset_timer()
                return
            except BlockingIOError:
                if attempt < max_tries:
                    _logger.debug(f"HID write blocked ({attempt}/{max_tries})")
                    await asyncio.sleep(retry_delay)
                else:
                    _logger.warning(f"HID write blocked ({attempt}/{max_tries})")
            except BrokenPipeError:
                _logger.warning(
                    "BrokenPipeError: USB cable likely disconnected or power-only. "
                    "Pausing relay.\nSee: "
                    "https://github.com/quaxalber/bluetooth_2_usb?tab=readme-ov-file#7-troubleshooting"
                )
                if self._relaying_active:
                    self._relaying_active.clear()
                return
            except Exception:
                _logger.exception(f"Error processing {event}")
                return


class DeviceIdentifier:
    """
    Identifies an input device by path (/dev/input/eventX), MAC address,
    or a substring of the device name.
    """

    def __init__(self, device_identifier: str) -> None:
        """
        :param device_identifier: Path, MAC, or name fragment
        """
        self._value = device_identifier
        self._type = self._determine_identifier_type()
        self._normalized_value = self._normalize_identifier()

    def __str__(self) -> str:
        return f'{self._type} "{self._value}"'

    def _determine_identifier_type(self) -> str:
        if re.match(r"^/dev/input/event.*$", self._value):
            return "path"
        if re.match(r"^([0-9a-fA-F]{2}[:-]){5}([0-9a-fA-F]{2})$", self._value):
            return "mac"
        return "name"

    def _normalize_identifier(self) -> str:
        if self._type == "path":
            return self._value
        if self._type == "mac":
            return self._value.lower().replace("-", ":")
        return self._value.lower()

    def matches(self, device: InputDevice) -> bool:
        """
        Check whether this identifier matches the given evdev InputDevice.

        :param device: An evdev InputDevice to compare
        :return: True if matched, False otherwise
        :rtype: bool
        """
        if self._type == "path":
            return self._value == device.path
        if self._type == "mac":
            return self._normalized_value == (device.uniq or "").lower()
        return self._normalized_value in device.name.lower()


async def async_list_input_devices() -> list[InputDevice]:
    """
    Return a list of available /dev/input/event* devices.

    :return: List of InputDevice objects
    :rtype: list[InputDevice]
    """
    try:
        return [InputDevice(path) for path in list_devices()]
    except (OSError, FileNotFoundError) as ex:
        _logger.critical(f"Failed listing devices: {ex}")
        return []
    except Exception:
        _logger.exception("Unexpected error listing devices")
        return []


def relay_event(event: InputEvent, gadget_manager: GadgetManager) -> None:
    """
    Relay the given event to the appropriate USB HID device.

    :param event: The evdev InputEvent
    :param gadget_manager: GadgetManager with references to HID devices
    :raises BlockingIOError: If HID device write is blocked
    """
    if isinstance(event, RelEvent):
        move_mouse(event, gadget_manager)
    elif isinstance(event, KeyEvent):
        send_key_event(event, gadget_manager)


def move_mouse(event: RelEvent, gadget_manager: GadgetManager) -> None:
    """
    Relay relative mouse movement events to the USB HID Mouse gadget.

    :param event: A RelEvent describing the movement
    :param gadget_manager: GadgetManager with Mouse reference
    :raises RuntimeError: If Mouse gadget is not available
    """
    mouse = gadget_manager.get_mouse()
    if mouse is None:
        raise RuntimeError("Mouse gadget not initialized or manager not enabled.")

    x, y, mwheel = get_mouse_movement(event)
    mouse.move(x, y, mwheel)


def send_key_event(event: KeyEvent, gadget_manager: GadgetManager) -> None:
    """
    Relay a key event (press/release) to the appropriate HID gadget.

    :param event: The KeyEvent to process
    :param gadget_manager: GadgetManager with references to the HID devices
    :raises RuntimeError: If no appropriate HID gadget is available
    """
    key_id, key_name = evdev_to_usb_hid(event)
    if key_id is None or key_name is None:
        return

    output_gadget = get_output_device(event, gadget_manager)
    if output_gadget is None:
        raise RuntimeError("No appropriate USB gadget found (manager not enabled?).")

    if event.keystate == KeyEvent.key_down:
        _logger.debug(f"Pressing {key_name} (0x{key_id:02X}) via {output_gadget}")
        output_gadget.press(key_id)
    elif event.keystate == KeyEvent.key_up:
        _logger.debug(f"Releasing {key_name} (0x{key_id:02X}) via {output_gadget}")
        output_gadget.release(key_id)


def get_output_device(
    event: KeyEvent, gadget_manager: GadgetManager
) -> Union[ConsumerControl, Keyboard, Mouse, None]:
    """
    Determine which HID gadget to target for the given key event.

    :param event: The KeyEvent to process
    :param gadget_manager: GadgetManager for HID references
    :return: A ConsumerControl, Mouse, or Keyboard object, or None if not found
    """
    if is_consumer_key(event):
        return gadget_manager.get_consumer()
    elif is_mouse_button(event):
        return gadget_manager.get_mouse()
    return gadget_manager.get_keyboard()


class UdcStateMonitor:
    """
    Monitors the UDC (USB Device Controller) state and
    sets/clears Events when the device is configured or not.
    Re-initializes USB HID gadgets on state transitions to handle sleep/wake cycles.
    """

    def __init__(
        self,
        udc_connected: asyncio.Event,
        relaying_active: asyncio.Event,
        gadget_manager: GadgetManager,
        udc_path: Path = Path("/sys/class/udc/20980000.usb/state"),
        poll_interval: float = 0.5,
    ) -> None:
        """
        :param udc_connected: Event tracking USB connection state (only managed by UDC monitor)
        :param relaying_active: Event controlling whether relaying is active (can be toggled by user)
        :param gadget_manager: GadgetManager to re-initialize gadgets on state transitions
        :param udc_path: Path to the UDC state file
        :param poll_interval: Interval (seconds) to re-check the UDC state
        """
        self._udc_connected = udc_connected
        self._relaying_active = relaying_active
        self._gadget_manager = gadget_manager
        self.udc_path = udc_path
        self.poll_interval = poll_interval

        self._stop = False
        self._task: Optional[asyncio.Task] = None
        self._last_state: Optional[str] = None

        if not self.udc_path.is_file():
            _logger.warning(
                f"UDC state file {self.udc_path} not found. Cable monitoring may be unavailable."
            )

    async def __aenter__(self):
        """
        Async context manager entry. Starts a background task to poll the UDC state.
        """
        self._stop = False
        self._task = asyncio.create_task(self._poll_state())
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Async context manager exit. Cancels the polling task.
        """
        if self._task:
            self._stop = True
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        return False

    async def _poll_state(self):
        # Initialize state on first check
        initial_state = self._read_udc_state()
        _logger.info(f"UdcStateMonitor: Initial UDC state is '{initial_state}'")

        # Set initial relaying state based on initial UDC state
        if initial_state == "configured":
            self._udc_connected.set()
            self._relaying_active.set()
            _logger.info("UdcStateMonitor: Initially set relaying_active=True (UDC already configured)")
        else:
            self._udc_connected.clear()
            self._relaying_active.clear()
            _logger.info("UdcStateMonitor: Initially set relaying_active=False (UDC not configured)")

        self._last_state = initial_state

        # Monitor for state changes
        while not self._stop:
            new_state = self._read_udc_state()
            if new_state != self._last_state:
                self._handle_state_change(new_state)
                self._last_state = new_state
            await asyncio.sleep(self.poll_interval)

    def _read_udc_state(self) -> str:
        """
        Read the UDC state file. If not found, treat as "not_attached".

        :return: The current UDC state (e.g. "configured")
        :rtype: str
        """
        try:
            with open(self.udc_path, "r") as f:
                return f.read().strip()
        except FileNotFoundError:
            return "not_attached"

    def _handle_state_change(self, new_state: str):
        """
        Handle a change in the UDC state. Manages state transitions and re-initializes
        gadgets when recovering from sleep/disconnect.

        :param new_state: The new UDC state
        """
        prev_state = self._last_state
        _logger.info(f"UDC state transition: '{prev_state}' → '{new_state}'")

        # Detect if we're transitioning TO a connected state (wake/reconnect)
        transitioning_to_configured = (
            new_state == "configured" and prev_state not in ("configured", None)
        )

        # Detect if we're transitioning FROM a connected state (sleep/disconnect)
        transitioning_from_configured = (
            prev_state == "configured" and new_state != "configured"
        )

        # Handle transition FROM configured (going to sleep/disconnect)
        if transitioning_from_configured:
            _logger.info("USB host disconnecting or entering sleep - releasing all keys")
            keyboard = self._gadget_manager.get_keyboard()
            mouse = self._gadget_manager.get_mouse()

            # Release all keys and buttons to prevent them getting stuck
            if keyboard:
                keyboard.release_all()
            if mouse:
                mouse.release_all()

        # Handle transition TO configured (waking up/reconnecting)
        if transitioning_to_configured:
            _logger.info("USB host reconnecting or waking from sleep")
            # Note: We no longer re-initialize gadgets here to avoid Windows compatibility issues
            # Instead, we rely on release_all() being called during the FROM transition

        # Update connection and relay state based on new state
        if new_state == "configured":
            self._udc_connected.set()
            self._relaying_active.set()
            _logger.info("UdcStateMonitor: Set relaying_active=True and udc_connected=True")
        else:
            self._udc_connected.clear()
            self._relaying_active.clear()
            _logger.info("UdcStateMonitor: Set relaying_active=False and udc_connected=False")


class UdevEventMonitor:
    """
    Monitors udev for /dev/input/event* add/remove events and
    notifies the RelayController.
    """

    def __init__(self, relay_controller: RelayController) -> None:
        """
        :param relay_controller: The RelayController to add/remove devices
        :param loop: The asyncio event loop
        """
        self.relay_controller = relay_controller

        self.context = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.context)
        self.monitor.filter_by("input")
        self.observer = pyudev.MonitorObserver(self.monitor, self._udev_event_callback)

    async def __aenter__(self):
        """
        Async context manager entry. Starts the pyudev monitor observer.
        """
        self.observer.start()
        _logger.debug("UdevEventMonitor started observer.")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """
        Async context manager exit. Stops the pyudev monitor observer.
        """
        self.observer.stop()
        _logger.debug("UdevEventMonitor stopped observer.")
        return False

    def _udev_event_callback(self, action: str, device: pyudev.Device) -> None:
        """
        pyudev callback for input devices.

        :param action: "add" or "remove"
        :param device: The pyudev device
        """
        device_node = device.device_node
        if not device_node or not device_node.startswith("/dev/input/event"):
            return

        if action == "add":
            _logger.debug(f"UdevEventMonitor: Added input => {device_node}")
            self.relay_controller.add_device(device_node)
        elif action == "remove":
            _logger.debug(f"UdevEventMonitor: Removed input => {device_node}")
            self.relay_controller.remove_device(device_node)
