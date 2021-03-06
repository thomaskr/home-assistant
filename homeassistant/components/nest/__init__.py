"""
Support for Nest devices.

For more details about this component, please refer to the documentation at
https://home-assistant.io/components/nest/
"""
from concurrent.futures import ThreadPoolExecutor
import logging
import os.path
import socket
from datetime import datetime, timedelta

import voluptuous as vol

from homeassistant.const import (
    CONF_STRUCTURE, CONF_FILENAME, CONF_BINARY_SENSORS, CONF_SENSORS,
    CONF_MONITORED_CONDITIONS,
    EVENT_HOMEASSISTANT_START, EVENT_HOMEASSISTANT_STOP)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send, \
    async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from . import local_auth

REQUIREMENTS = ['python-nest==4.0.2']

_CONFIGURING = {}
_LOGGER = logging.getLogger(__name__)


DATA_NEST = 'nest'
DATA_NEST_CONFIG = 'nest_config'

SIGNAL_NEST_UPDATE = 'nest_update'

NEST_CONFIG_FILE = 'nest.conf'
CONF_CLIENT_ID = 'client_id'
CONF_CLIENT_SECRET = 'client_secret'

ATTR_HOME_MODE = 'home_mode'
ATTR_STRUCTURE = 'structure'
ATTR_TRIP_ID = 'trip_id'
ATTR_ETA = 'eta'
ATTR_ETA_WINDOW = 'eta_window'

HOME_MODE_AWAY = 'away'
HOME_MODE_HOME = 'home'

SENSOR_SCHEMA = vol.Schema({
    vol.Optional(CONF_MONITORED_CONDITIONS): vol.All(cv.ensure_list)
})

AWAY_SCHEMA = vol.Schema({
    vol.Required(ATTR_HOME_MODE): vol.In([HOME_MODE_AWAY, HOME_MODE_HOME]),
    vol.Optional(ATTR_STRUCTURE): vol.All(cv.ensure_list, cv.string),
    vol.Optional(ATTR_TRIP_ID): cv.string,
    vol.Optional(ATTR_ETA): cv.time_period,
    vol.Optional(ATTR_ETA_WINDOW): cv.time_period
})

CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Required(CONF_CLIENT_ID): cv.string,
        vol.Required(CONF_CLIENT_SECRET): cv.string,
        vol.Optional(CONF_STRUCTURE): vol.All(cv.ensure_list, cv.string),
        vol.Optional(CONF_SENSORS): SENSOR_SCHEMA,
        vol.Optional(CONF_BINARY_SENSORS): SENSOR_SCHEMA
    })
}, extra=vol.ALLOW_EXTRA)


async def async_nest_update_event_broker(hass, nest):
    """
    Dispatch SIGNAL_NEST_UPDATE to devices when nest stream API received data.

    nest.update_event.wait will block the thread in most of time,
    so specific an executor to save default thread pool.
    """
    _LOGGER.debug("listening nest.update_event")
    with ThreadPoolExecutor(max_workers=1) as executor:
        while True:
            await hass.loop.run_in_executor(executor, nest.update_event.wait)
            if hass.is_running:
                nest.update_event.clear()
                _LOGGER.debug("dispatching nest data update")
                async_dispatcher_send(hass, SIGNAL_NEST_UPDATE)
            else:
                return


async def async_setup(hass, config):
    """Set up Nest components."""
    if DOMAIN not in config:
        return

    conf = config[DOMAIN]

    local_auth.initialize(hass, conf[CONF_CLIENT_ID], conf[CONF_CLIENT_SECRET])

    filename = config.get(CONF_FILENAME, NEST_CONFIG_FILE)
    access_token_cache_file = hass.config.path(filename)

    if await hass.async_add_job(os.path.isfile, access_token_cache_file):
        hass.async_add_job(hass.config_entries.flow.async_init(
            DOMAIN, source='import', data={
                'nest_conf_path': access_token_cache_file,
            }
        ))

    # Store config to be used during entry setup
    hass.data[DATA_NEST_CONFIG] = conf

    return True


async def async_setup_entry(hass, entry):
    """Setup Nest from a config entry."""
    from nest import Nest

    nest = Nest(access_token=entry.data['tokens']['access_token'])

    _LOGGER.debug("proceeding with setup")
    conf = hass.data.get(DATA_NEST_CONFIG, {})
    hass.data[DATA_NEST] = NestDevice(hass, conf, nest)
    await hass.async_add_job(hass.data[DATA_NEST].initialize)

    for component in 'climate', 'camera', 'sensor', 'binary_sensor':
        hass.async_add_job(hass.config_entries.async_forward_entry_setup(
            entry, component))

    def set_mode(service):
        """
        Set the home/away mode for a Nest structure.

        You can set optional eta information when set mode to away.
        """
        if ATTR_STRUCTURE in service.data:
            structures = service.data[ATTR_STRUCTURE]
        else:
            structures = hass.data[DATA_NEST].local_structure

        for structure in nest.structures:
            if structure.name in structures:
                _LOGGER.info("Setting mode for %s", structure.name)
                structure.away = service.data[ATTR_HOME_MODE]

                if service.data[ATTR_HOME_MODE] == HOME_MODE_AWAY \
                        and ATTR_ETA in service.data:
                    now = datetime.utcnow()
                    eta_begin = now + service.data[ATTR_ETA]
                    eta_window = service.data.get(ATTR_ETA_WINDOW,
                                                  timedelta(minutes=1))
                    eta_end = eta_begin + eta_window
                    trip_id = service.data.get(
                        ATTR_TRIP_ID, "trip_{}".format(int(now.timestamp())))
                    _LOGGER.info("Setting eta for %s, eta window starts at "
                                 "%s ends at %s", trip_id, eta_begin, eta_end)
                    structure.set_eta(trip_id, eta_begin, eta_end)
            else:
                _LOGGER.error("Invalid structure %s",
                              service.data[ATTR_STRUCTURE])

    hass.services.async_register(
        DOMAIN, 'set_mode', set_mode, schema=AWAY_SCHEMA)

    def start_up(event):
        """Start Nest update event listener."""
        hass.async_add_job(async_nest_update_event_broker, hass, nest)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, start_up)

    def shut_down(event):
        """Stop Nest update event listener."""
        if nest:
            nest.update_event.set()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shut_down)

    _LOGGER.debug("async_setup_nest is done")

    return True


class NestDevice(object):
    """Structure Nest functions for hass."""

    def __init__(self, hass, conf, nest):
        """Init Nest Devices."""
        self.hass = hass
        self.nest = nest
        self.local_structure = conf.get(CONF_STRUCTURE)

    def initialize(self):
        """Initialize Nest."""
        if self.local_structure is None:
            self.local_structure = [s.name for s in self.nest.structures]

    def structures(self):
        """Generate a list of structures."""
        try:
            for structure in self.nest.structures:
                if structure.name in self.local_structure:
                    yield structure
                else:
                    _LOGGER.debug("Ignoring structure %s, not in %s",
                                  structure.name, self.local_structure)
        except socket.error:
            _LOGGER.error(
                "Connection error logging into the nest web service.")

    def thermostats(self):
        """Generate a list of thermostats and their location."""
        try:
            for structure in self.nest.structures:
                if structure.name in self.local_structure:
                    for device in structure.thermostats:
                        yield (structure, device)
                else:
                    _LOGGER.debug("Ignoring structure %s, not in %s",
                                  structure.name, self.local_structure)
        except socket.error:
            _LOGGER.error(
                "Connection error logging into the nest web service.")

    def smoke_co_alarms(self):
        """Generate a list of smoke co alarms."""
        try:
            for structure in self.nest.structures:
                if structure.name in self.local_structure:
                    for device in structure.smoke_co_alarms:
                        yield (structure, device)
                else:
                    _LOGGER.debug("Ignoring structure %s, not in %s",
                                  structure.name, self.local_structure)
        except socket.error:
            _LOGGER.error(
                "Connection error logging into the nest web service.")

    def cameras(self):
        """Generate a list of cameras."""
        try:
            for structure in self.nest.structures:
                if structure.name in self.local_structure:
                    for device in structure.cameras:
                        yield (structure, device)
                else:
                    _LOGGER.debug("Ignoring structure %s, not in %s",
                                  structure.name, self.local_structure)
        except socket.error:
            _LOGGER.error(
                "Connection error logging into the nest web service.")


class NestSensorDevice(Entity):
    """Representation of a Nest sensor."""

    def __init__(self, structure, device, variable):
        """Initialize the sensor."""
        self.structure = structure
        self.variable = variable

        if device is not None:
            # device specific
            self.device = device
            self._name = "{} {}".format(self.device.name_long,
                                        self.variable.replace('_', ' '))
        else:
            # structure only
            self.device = structure
            self._name = "{} {}".format(self.structure.name,
                                        self.variable.replace('_', ' '))

        self._state = None
        self._unit = None

    @property
    def name(self):
        """Return the name of the nest, if any."""
        return self._name

    @property
    def unit_of_measurement(self):
        """Return the unit the value is expressed in."""
        return self._unit

    @property
    def should_poll(self):
        """Do not need poll thanks using Nest streaming API."""
        return False

    def update(self):
        """Do not use NestSensorDevice directly."""
        raise NotImplementedError

    async def async_added_to_hass(self):
        """Register update signal handler."""
        async def async_update_state():
            """Update sensor state."""
            await self.async_update_ha_state(True)

        async_dispatcher_connect(self.hass, SIGNAL_NEST_UPDATE,
                                 async_update_state)
