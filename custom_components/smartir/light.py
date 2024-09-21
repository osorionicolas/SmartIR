import asyncio
import logging

import voluptuous as vol

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
    PLATFORM_SCHEMA,
)
from homeassistant.const import CONF_NAME, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, Event, EventStateChangedData, callback
from homeassistant.helpers.event import async_track_state_change_event, async_call_later
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.typing import ConfigType
from . import DeviceData
from .controller import get_controller, get_controller_schema

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = "SmartIR Light"
DEFAULT_DELAY = 0.5
DEFAULT_POWER_SENSOR_DELAY = 10

CONF_UNIQUE_ID = "unique_id"
CONF_DEVICE_CODE = "device_code"
CONF_CONTROLLER_DATA = "controller_data"
CONF_DELAY = "delay"
CONF_POWER_SENSOR = "power_sensor"
CONF_POWER_SENSOR_DELAY = "power_sensor_delay"
CONF_POWER_SENSOR_RESTORE_STATE = "power_sensor_restore_state"

CMD_BRIGHTNESS_INCREASE = "brighten"
CMD_BRIGHTNESS_DECREASE = "dim"
CMD_COLORMODE_COLDER = "colder"
CMD_COLORMODE_WARMER = "warmer"
CMD_POWER_ON = "on"
CMD_POWER_OFF = "off"
CMD_NIGHTLIGHT = "night"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Required(CONF_DEVICE_CODE): cv.positive_int,
        vol.Required(CONF_CONTROLLER_DATA): get_controller_schema(vol, cv),
        vol.Optional(CONF_DELAY, default=DEFAULT_DELAY): cv.string,
        vol.Optional(CONF_POWER_SENSOR): cv.entity_id,
        vol.Optional(
            CONF_POWER_SENSOR_DELAY, default=DEFAULT_POWER_SENSOR_DELAY
        ): cv.positive_int,
        vol.Optional(CONF_POWER_SENSOR_RESTORE_STATE, default=True): cv.boolean,
    }
)


async def async_setup_platform(
    hass: HomeAssistant, config: ConfigType, async_add_entities, discovery_info=None
):
    """Set up the IR Light platform."""
    _LOGGER.debug("Setting up the SmartIR light platform")
    if not (
        device_data := await DeviceData.load_file(
            config.get(CONF_DEVICE_CODE),
            "light",
            {},
            hass,
        )
    ):
        _LOGGER.error("SmartIR light device data init failed!")
        return

    async_add_entities([SmartIRLight(hass, config, device_data)])


class SmartIRLight(LightEntity, RestoreEntity):
    _attr_should_poll = False

    def __init__(self, hass, config, device_data):
        self.hass = hass
        self._unique_id = config.get(CONF_UNIQUE_ID)
        self._name = config.get(CONF_NAME)
        self._device_code = config.get(CONF_DEVICE_CODE)
        self._controller_data = config.get(CONF_CONTROLLER_DATA)
        self._delay = config.get(CONF_DELAY)
        self._power_sensor = config.get(CONF_POWER_SENSOR)
        self._power_sensor_delay = config.get(CONF_POWER_SENSOR_DELAY)
        self._power_sensor_restore_state = config.get(CONF_POWER_SENSOR_RESTORE_STATE)

        self._power = STATE_ON
        self._brightness = None
        self._colortemp = None
        self._on_by_remote = False
        self._support_color_mode = ColorMode.UNKNOWN
        self._power_sensor_check_expect = None
        self._power_sensor_check_cancel = None

        self._manufacturer = device_data["manufacturer"]
        self._supported_models = device_data["supportedModels"]
        self._supported_controller = device_data["supportedController"]
        self._commands_encoding = device_data["commandsEncoding"]
        self._brightnesses = device_data["brightness"]
        self._colortemps = device_data["colorTemperature"]
        self._commands = device_data["commands"]

        if (
            CMD_COLORMODE_COLDER in self._commands
            and CMD_COLORMODE_WARMER in self._commands
        ):
            self._colortemp = self.max_color_temp_kelvin
            self._support_color_mode = ColorMode.COLOR_TEMP

        if CMD_NIGHTLIGHT in self._commands or (
            CMD_BRIGHTNESS_INCREASE in self._commands
            and CMD_BRIGHTNESS_DECREASE in self._commands
        ):
            self._brightness = 100
            self._support_brightness = True
            if self._support_color_mode == ColorMode.UNKNOWN:
                self._support_color_mode = ColorMode.BRIGHTNESS
        else:
            self._support_brightness = False

        if (
            CMD_POWER_OFF in self._commands
            and CMD_POWER_ON in self._commands
            and self._support_color_mode == ColorMode.UNKNOWN
        ):
            self._support_color_mode = ColorMode.ONOFF

        # Init exclusive lock for sending IR commands
        self._temp_lock = asyncio.Lock()

        # Init the IR/RF controller
        self._controller = get_controller(
            self.hass,
            self._supported_controller,
            self._commands_encoding,
            self._controller_data,
            self._delay,
        )

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._power = last_state.state
            if ATTR_BRIGHTNESS in last_state.attributes:
                self._brightness = last_state.attributes[ATTR_BRIGHTNESS]
            if ATTR_COLOR_TEMP_KELVIN in last_state.attributes:
                self._colortemp = last_state.attributes[ATTR_COLOR_TEMP_KELVIN]

        if self._power_sensor:
            async_track_state_change_event(
                self.hass, self._power_sensor, self._async_power_sensor_changed
            )

    @property
    def unique_id(self):
        """Return a unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the display name of the light."""
        return self._name

    @property
    def supported_color_modes(self):
        """Return the list of supported color modes."""
        return [self._support_color_mode]

    @property
    def color_mode(self):
        return self._support_color_mode

    @property
    def color_temp_kelvin(self):
        return self._colortemp

    @property
    def min_color_temp_kelvin(self):
        if self._colortemps:
            return self._colortemps[0]

    @property
    def max_color_temp_kelvin(self):
        if self._colortemps:
            return self._colortemps[-1]

    @property
    def is_on(self):
        return self._power == STATE_ON or self._on_by_remote

    @property
    def brightness(self):
        return self._brightness

    @property
    def extra_state_attributes(self):
        """Platform specific attributes."""
        return {
            "device_code": self._device_code,
            "manufacturer": self._manufacturer,
            "supported_models": self._supported_models,
            "supported_controller": self._supported_controller,
            "commands_encoding": self._commands_encoding,
            "on_by_remote": self._on_by_remote,
        }

    async def async_turn_on(self, **params):
        did_something = False
        # Turn the light on if off
        if self._power != STATE_ON and not self._on_by_remote:
            self._power = STATE_ON
            did_something = True
            await self.send_command(CMD_POWER_ON)

        if (
            ATTR_COLOR_TEMP_KELVIN in params
            and ColorMode.COLOR_TEMP == self._support_color_mode
        ):
            target = params.get(ATTR_COLOR_TEMP_KELVIN)
            old_color_temp = DeviceData.closest_match(self._colortemp, self._colortemps)
            new_color_temp = DeviceData.closest_match(target, self._colortemps)
            _LOGGER.debug(
                f"Changing color temp from {self._colortemp}K step {old_color_temp} to {target}K step {new_color_temp}"
            )

            steps = new_color_temp - old_color_temp
            did_something = True
            if steps < 0:
                cmd = CMD_COLORMODE_WARMER
                steps = abs(steps)
            else:
                cmd = CMD_COLORMODE_COLDER

            if steps > 0 and cmd:
                # If we are heading for the highest or lowest value,
                # take the opportunity to resync by issuing enough
                # commands to go the full range.
                if new_color_temp == len(self._colortemps) - 1 or new_color_temp == 0:
                    steps = len(self._colortemps)
                self._colortemp = self._colortemps[new_color_temp]
                await self.send_command(cmd, steps)

        if ATTR_BRIGHTNESS in params and self._support_brightness:
            # before checking the supported brightnesses, make a special case
            # when a nightlight is fitted for brightness of 1
            if params.get(ATTR_BRIGHTNESS) == 1 and CMD_NIGHTLIGHT in self._commands:
                self._brightness = 1
                self._power = STATE_ON
                did_something = True
                await self.send_command(CMD_NIGHTLIGHT)

            elif self._brightnesses:
                target = params.get(ATTR_BRIGHTNESS)
                old_brightness = DeviceData.closest_match(
                    self._brightness, self._brightnesses
                )
                new_brightness = DeviceData.closest_match(target, self._brightnesses)
                did_something = True
                _LOGGER.debug(
                    f"Changing brightness from {self._brightness} step {old_brightness} to {target} step {new_brightness}"
                )
                steps = new_brightness - old_brightness
                if steps < 0:
                    cmd = CMD_BRIGHTNESS_DECREASE
                    steps = abs(steps)
                else:
                    cmd = CMD_BRIGHTNESS_INCREASE

                if steps > 0 and cmd:
                    # If we are heading for the highest or lowest value,
                    # take the opportunity to resync by issuing enough
                    # commands to go the full range.
                    if (
                        new_brightness == len(self._brightnesses) - 1
                        or new_brightness == 0
                    ):
                        steps = len(self._colortemps)
                    did_something = True
                    self._brightness = self._brightnesses[new_brightness]
                    await self.send_command(cmd, steps)

        # If we did nothing above, and the light is not detected as on
        # already issue the on command, even though we think the light
        # is on.  This is because we may be out of sync due to use of the
        # remote when we don't have anything to detect it.
        # If we do have such monitoring, avoid issuing the command in case
        # on and off are the same remote code.
        if not did_something and not self._on_by_remote:
            self._power = STATE_ON
            await self.send_command(CMD_POWER_ON)

        await self.async_write_ha_state()

    async def async_turn_off(self):
        self._power = STATE_OFF
        await self.send_command(CMD_POWER_OFF)

    async def async_toggle(self):
        await (self.async_turn_on() if not self.is_on else self.async_turn_off())

    async def send_command(self, cmd, count=1):
        if cmd not in self._commands:
            _LOGGER.error(f"Unknown command '{cmd}'")
            return
        _LOGGER.debug(f"Sending {cmd} remote command {count} times.")
        remote_cmd = self._commands.get(cmd)
        async with self._temp_lock:
            self._on_by_remote = False
            try:
                for _ in range(count):
                    await self._controller.send(remote_cmd)
            except Exception as e:
                _LOGGER.exception(e)

    async def _async_power_sensor_changed(
        self, event: Event[EventStateChangedData]
    ) -> None:
        """Handle power sensor changes."""
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]
        if new_state is None:
            return

        if old_state is not None and new_state.state == old_state.state:
            return

        if new_state.state == STATE_ON and self._state == STATE_OFF:
            self._state = STATE_ON
            self._on_by_remote = True
        elif new_state.state == STATE_OFF:
            self._on_by_remote = False
            if self._state == STATE_ON:
                self._state = STATE_OFF
        self.async_write_ha_state()

    @callback
    def _async_power_sensor_check_schedule(self, state):
        if self._power_sensor_check_cancel:
            self._power_sensor_check_cancel()
            self._power_sensor_check_cancel = None
            self._power_sensor_check_expect = None

        @callback
        def _async_power_sensor_check(*_):
            self._power_sensor_check_cancel = None
            expected_state = self._power_sensor_check_expect
            self._power_sensor_check_expect = None
            current_state = getattr(
                self.hass.states.get(self._power_sensor), "state", None
            )
            _LOGGER.debug(
                "Executing power sensor check for expected state '%s', current state '%s'.",
                expected_state,
                current_state,
            )

            if (
                expected_state in [STATE_ON, STATE_OFF]
                and current_state in [STATE_ON, STATE_OFF]
                and expected_state != current_state
            ):
                self._state = current_state
                _LOGGER.debug(
                    "Power sensor check failed, reverted device state to '%s'.",
                    self._state,
                )
                self.async_write_ha_state()

        self._power_sensor_check_expect = state
        self._power_sensor_check_cancel = async_call_later(
            self.hass, self._power_sensor_delay, _async_power_sensor_check
        )
        _LOGGER.debug("Scheduled power sensor check for '%s' state.", state)
