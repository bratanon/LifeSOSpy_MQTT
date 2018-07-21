"""
This module contains the Translator class.
"""

import asyncio
import json
import logging
import signal
import sys
from typing import List, Optional, Any
import dateutil
from hbmqtt.client import MQTTClient, QOS_1
from hbmqtt.session import ApplicationMessage
from lifesospy.baseunit import BaseUnit
from lifesospy.contactid import ContactID
from lifesospy.device import Device, SpecialDevice
from lifesospy.enums import (
    SwitchNumber, DeviceEventCode, DCFlags, ESFlags, SSFlags, SwitchFlags,
    OperationMode, BaseUnitState, ContactIDEventQualifier as EventQualifier,
    ContactIDEventCategory as EventCategory, DeviceType)
from lifesospy.propertychangedinfo import PropertyChangedInfo
from lifesospy_mqtt.config import (
    Config, TranslatorDeviceConfig, TranslatorSwitchConfig)
from lifesospy_mqtt.const import PROJECT_NAME
from lifesospy_mqtt.enums import OnOff, OpenClosed
from lifesospy_mqtt.subscribetopic import SubscribeTopic

_LOGGER = logging.getLogger(__name__)


class Translator(object):
    """Translates messages between the LifeSOS and MQTT interfaces."""

    # Default interval to wait before resetting Trigger device state to Off
    AUTO_RESET_INTERVAL = 30

    # Keys for Home Assistant MQTT discovery configuration
    HA_AVAILABILITY_TOPIC = 'availability_topic'
    HA_COMMAND_TOPIC = 'command_topic'
    HA_DEVICE_CLASS = 'device_class'
    HA_NAME = 'name'
    HA_PAYLOAD_AVAILABLE = 'payload_available'
    HA_PAYLOAD_NOT_AVAILABLE = 'payload_not_available'
    HA_PAYLOAD_OFF = 'payload_off'
    HA_PAYLOAD_ON = 'payload_on'
    HA_STATE_TOPIC = 'state_topic'
    HA_UNIQUE_ID = 'unique_id'
    HA_UNIT_OF_MEASUREMENT = 'unit_of_measurement'

    # Device class to classify the sensor type in Home Assistant
    HA_DC_DOOR = 'door'
    HA_DC_GAS = 'gas'
    HA_DC_HUMIDITY = 'humidity'
    HA_DC_ILLUMINANCE = 'illuminance'
    HA_DC_MOISTURE = 'moisture'
    HA_DC_MOTION = 'motion'
    HA_DC_SAFETY = 'safety'
    HA_DC_SMOKE = 'smoke'
    HA_DC_TEMPERATURE = 'temperature'
    HA_DC_VIBRATION = 'vibration'
    HA_DC_WINDOW = 'window'

    # Platforms in Home Assistant to represent our devices
    HA_PLATFORM_BINARY_SENSOR = 'binary_sensor'
    HA_PLATFORM_SENSOR = 'sensor'
    HA_PLATFORM_SWITCH = 'switch'

    # Unit of measurement for Home Assistant sensors
    HA_UOM_CURRENT = 'A'
    HA_UOM_HUMIDITY = '%'
    HA_UOM_ILLUMINANCE = 'Lux'
    HA_UOM_TEMPERATURE = '°C'

    # Ping MQTT broker this many seconds apart to check we're connected
    KEEP_ALIVE = 30

    # Attempt reconnection this many seconds apart
    RECONNECT_INTERVAL = 30

    # Sub-topic to clear the alarm/warning LEDs on base unit and stop siren
    TOPIC_CLEAR_STATUS = 'clear_status'

    # Sub-topic to access the remote date/time
    TOPIC_DATETIME = 'datetime'

    # Sub-topic to provide alarm state that is recognised by Home Assistant
    TOPIC_HASTATE = 'ha_state'

    # Sub-topic that will be subscribed to on topics that can be set
    TOPIC_SET = 'set'

    def __init__(self, config: Config):
        self._config = config
        self._loop = asyncio.get_event_loop()
        self._shutdown = False
        self._auto_reset_handles = {}

        # Create LifeSOS base unit instance and attach callbacks
        self._baseunit = BaseUnit(
            self._config.lifesos.host,
            self._config.lifesos.port)
        if self._config.lifesos.password:
            self._baseunit.password = self._config.lifesos.password
        self._baseunit.on_device_added = self._baseunit_device_added
        self._baseunit.on_device_deleted = self._baseunit_device_deleted
        self._baseunit.on_event = self._baseunit_event
        self._baseunit.on_properties_changed = self._baseunit_properties_changed
        self._baseunit.on_switch_state_changed = self._baseunit_switch_state_changed

        # Create MQTT client instance
        self._mqtt = MQTTClient(
            client_id=self._config.mqtt.client_id,
            config={
                'keep_alive': Translator.KEEP_ALIVE,
                'reconnect_retries': sys.maxsize,
                'reconnect_max_interval': Translator.RECONNECT_INTERVAL,
                'default_qos': QOS_1,
                'will': {
                    'topic': '{}/{}'.format(
                        self._config.translator.baseunit,
                        BaseUnit.PROP_IS_CONNECTED),
                    'message': str(False).encode(),
                    'qos': QOS_1,
                    'retain': True,
                },
            })

        # Generate a list of topics we'll need to subscribe to
        self._subscribetopics = []
        self._subscribetopics.append(
            SubscribeTopic(
                '{}/{}'.format(
                    self._config.translator.baseunit,
                    Translator.TOPIC_CLEAR_STATUS),
                self._on_message_clear_status))
        self._subscribetopics.append(
            SubscribeTopic(
                '{}/{}/{}'.format(
                    self._config.translator.baseunit,
                    Translator.TOPIC_DATETIME,
                    Translator.TOPIC_SET),
                self._on_message_set_datetime))
        names = [BaseUnit.PROP_OPERATION_MODE]
        for name in names:
            self._subscribetopics.append(
                SubscribeTopic(
                    '{}/{}/{}'.format(
                        self._config.translator.baseunit,
                        name, Translator.TOPIC_SET),
                    self._on_message_baseunit,
                    args=name))
        for switch_number in self._config.translator.switches.keys():
            switch_config = self._config.translator.switches.get(switch_number)
            if switch_config and switch_config.topic:
                self._subscribetopics.append(
                    SubscribeTopic(
                        '{}/{}'.format(
                            switch_config.topic,
                            Translator.TOPIC_SET),
                        self._on_message_switch,
                        args=switch_number))

        # Also create a lookup dict for the topics to subscribe to
        self._subscribetopics_lookup = \
            {st.topic: st for st in self._subscribetopics}

    #
    # METHODS - Public
    #

    async def async_start(self) -> None:
        """Starts up the LifeSOS interface and connects to MQTT broker."""

        self._shutdown = False

        # Start up the LifeSOS interface
        self._baseunit.start()

        # Connect to the MQTT broker
        await self._mqtt.connect(
            uri=self._config.mqtt.uri,
            cleansession=False,
            cafile=self._config.mqtt.cafile,
            capath=self._config.mqtt.capath,
            cadata=self._config.mqtt.cadata,
        )

        # Subscribe to topics we are capable of actioning
        await self._mqtt.subscribe(self._subscribetopics)

        # When HA discovery is enabled, publish switch configuration to it
        # (devices done later elsewhere, after we've enumerated them)
        if self._config.translator.ha_discovery_prefix:
            for switch_number in self._config.translator.switches.keys():
                switch_config = self._config.translator.switches[switch_number]
                if switch_config.ha_name:
                    self._publish_ha_switch_config(switch_number, switch_config)

    async def async_loop(self) -> None:
        """Loop indefinitely to process messages from our subscriptions."""

        # Trap SIGINT so that we can shutdown gracefully
        signal.signal(signal.SIGINT, self.signal_shutdown)
        try:
            while not self._shutdown:
                # Wait for next message
                try:
                    message = await self._mqtt.deliver_message(timeout=1)
                except asyncio.TimeoutError:
                    continue
                except Exception: # pylint: disable=broad-except
                    # Log any exception but keep going
                    _LOGGER.error(
                        "Exception waiting for message to be delivered",
                        exc_info=True)
                    continue

                # Do subscribed topic callback to handle message
                try:
                    subscribetopic = self._subscribetopics_lookup[message.topic]
                    subscribetopic.on_message(subscribetopic, message)
                except Exception: # pylint: disable=broad-except
                    _LOGGER.error(
                        "Exception processing message from subscribed topic: %s",
                        message.topic, exc_info=True)
        finally:
            signal.signal(signal.SIGINT, signal.SIG_DFL)

    async def async_stop(self) -> None:
        """Shuts down the LifeSOS interface and disconnects from MQTT broker."""

        # Stop the LifeSOS interface
        self._baseunit.stop()

        # Cancel any outstanding auto reset tasks
        for item in self._auto_reset_handles.copy().items():
            item[1].cancel()
            self._auto_reset_handles.pop(item[0])

        # Disconnect from the MQTT broker
        await self._mqtt.disconnect()

    def signal_shutdown(self, sig, frame):
        """Flag shutdown when signal received."""
        _LOGGER.debug('%s received; shutting down...',
                      signal.Signals(sig).name) # pylint: disable=no-member
        self._shutdown = True

    #
    # METHODS - Private / Internal
    #

    def _baseunit_device_added(self, baseunit: BaseUnit, device: Device) -> None:
        # Hook up callbacks for device that was added / discovered
        device.on_event = self._device_on_event
        device.on_properties_changed = self._device_on_properties_changed

        # Publish initial property values for device
        device_config = self._config.translator.devices.get(device.device_id)
        if device_config and device_config.topic:
            props = device.as_dict()
            for name in props.keys():
                self._publish_device_property(
                    device_config.topic, device, name, getattr(device, name))

        # When HA discovery is enabled, publish device configuration to it
        if self._config.translator.ha_discovery_prefix and device_config.ha_name:
            self._publish_ha_device_config(device, device_config)

    def _baseunit_device_deleted(self, baseunit: BaseUnit, device: Device) -> None: # pylint: disable=no-self-use
        # Remove callbacks from deleted device
        device.on_event = None
        device.on_properties_changed = None

    def _baseunit_event(self, baseunit: BaseUnit, contact_id: ContactID):
        # When base unit event occurs, publish the event data
        # (don't bother retaining; events are time sensitive)
        event_data = json.dumps(contact_id.as_dict())
        self._publish('{}/event'.format(self._config.translator.baseunit),
                      event_data, False)

        # For clients that can't handle json, we will also provide the event
        # qualifier and code via these topics
        if contact_id.event_code:
            if contact_id.event_qualifier == EventQualifier.Event:
                self._publish(
                    '{}/event_code'.format(self._config.translator.baseunit),
                    contact_id.event_code, False)
            elif contact_id.event_qualifier == EventQualifier.Restore:
                self._publish(
                    '{}/restore_code'.format(self._config.translator.baseunit),
                    contact_id.event_code, False)

        # This is just for Home Assistant; the 'alarm_control_panel.mqtt'
        # component currently requires these hard-coded state values
        if contact_id.event_qualifier == EventQualifier.Event and \
                contact_id.event_category == EventCategory.Alarm:
            self._publish(
                '{}/{}'.format(self._config.translator.baseunit,
                               Translator.TOPIC_HASTATE),
                'triggered', True)

    def _baseunit_properties_changed(self, baseunit: BaseUnit,
                                     changes: List[PropertyChangedInfo]) -> None:
        # When base unit properties change, publish them
        for change in changes:
            self._publish_baseunit_property(change.name, change.new_value)

    def _baseunit_switch_state_changed(self, baseunit: BaseUnit,
                                       switch_number: SwitchNumber,
                                       state: Optional[bool]) -> None:
        # When switch state changes, publish it
        switch_config = self._config.translator.switches.get(switch_number)
        if switch_config and switch_config.topic:
            self._publish(switch_config.topic, OnOff.parse_value(state), True)

    def _device_on_event(self, device: Device, event_code: DeviceEventCode) -> None:
        device_config = self._config.translator.devices.get(device.device_id)
        if device_config and device_config.topic:
            # When device event occurs, publish the event code
            # (don't bother retaining; events are time sensitive)
            self._publish('{}/event_code'.format(device_config.topic), event_code, False)

            # When it is a Trigger event, set state to On and schedule an
            # auto reset callback to occur after specified interval
            if event_code == DeviceEventCode.Trigger:
                self._publish(device_config.topic, OnOff.parse_value(True), True)
                handle = self._auto_reset_handles.get(device.device_id)
                if handle:
                    handle.cancel()
                handle = self._loop.call_later(
                    device_config.auto_reset_interval or Translator.AUTO_RESET_INTERVAL,
                    self._auto_reset, device.device_id)
                self._auto_reset_handles[device.device_id] = handle

    def _auto_reset(self, device_id: int):
        # Auto reset a Trigger device to Off state
        device_config = self._config.translator.devices.get(device_id)
        if device_config and device_config.topic:
            self._publish(device_config.topic, OnOff.parse_value(False), True)
        self._auto_reset_handles.pop(device_id)

    def _device_on_properties_changed(self, device: Device, changes: List[PropertyChangedInfo]):
        # When device properties change, publish them
        device_config = self._config.translator.devices.get(device.device_id)
        if device_config and device_config.topic:
            for change in changes:
                self._publish_device_property(
                    device_config.topic, device, change.name, change.new_value)

    def _publish_baseunit_property(self, name: str, value: Any) -> None:
        topic_parent = self._config.translator.baseunit

        # Base Unit topic holds the state
        if name == BaseUnit.PROP_STATE:
            self._publish(topic_parent, value, True)

            # This is just for Home Assistant; the 'alarm_control_panel.mqtt'
            # component currently requires these hard-coded state values
            topic = '{}/{}'.format(topic_parent, Translator.TOPIC_HASTATE)
            if value in {BaseUnitState.Disarm, BaseUnitState.Monitor}:
                self._publish(topic, 'disarmed', True)
            elif value == BaseUnitState.Home:
                self._publish(topic, 'armed_home', True)
            elif value == BaseUnitState.Away:
                self._publish(topic, 'armed_away', True)
            elif value in {BaseUnitState.AwayExitDelay,
                           BaseUnitState.AwayEntryDelay}:
                self._publish(topic, 'pending', True)

        # Other supported properties in a topic using property name
        elif name in {
                BaseUnit.PROP_IS_CONNECTED, BaseUnit.PROP_ROM_VERSION,
                BaseUnit.PROP_EXIT_DELAY, BaseUnit.PROP_ENTRY_DELAY,
                BaseUnit.PROP_OPERATION_MODE}:
            self._publish('{}/{}'.format(topic_parent, name), value, True)

    def _publish_device_property(self, topic_parent: str, device: Device,
                                 name: str, value: Any) -> None:
        # Device topic holds the state
        if (not isinstance(device, SpecialDevice)) and \
                name == Device.PROP_IS_CLOSED:
            # For regular device; this is the Is Closed property for magnet
            # sensors, otherwise default to Off for trigger-based devices
            if device.type == DeviceType.DoorMagnet:
                self._publish(topic_parent, OpenClosed.parse_value(value), True)
            else:
                self._publish(topic_parent, OnOff.Off, True)
        elif isinstance(device, SpecialDevice) and \
                name == SpecialDevice.PROP_CURRENT_READING:
            # For special device, this is the current reading
            self._publish(topic_parent, value, True)

        # Category will have sub-topics for it's properties
        elif name == Device.PROP_CATEGORY:
            for prop in value.as_dict().items():
                if prop[0] in {'code', 'description'}:
                    self._publish('{}/{}/{}'.format(
                        topic_parent, name, prop[0]), prop[1], True)

        # Flag enums; expose as sub-topics with a bool state per flag
        elif name == Device.PROP_CHARACTERISTICS:
            for item in iter(DCFlags):
                self._publish(
                    '{}/{}/{}'.format(topic_parent, name, item.name),
                    bool(value & item.value), True)
        elif name == Device.PROP_ENABLE_STATUS:
            for item in iter(ESFlags):
                self._publish(
                    '{}/{}/{}'.format(topic_parent, name, item.name),
                    bool(value & item.value), True)
        elif name == Device.PROP_SWITCHES:
            for item in iter(SwitchFlags):
                self._publish(
                    '{}/{}/{}'.format(topic_parent, name, item.name),
                    bool(value & item.value), True)
        elif name == SpecialDevice.PROP_SPECIAL_STATUS:
            for item in iter(SSFlags):
                self._publish(
                    '{}/{}/{}'.format(topic_parent, name, item.name),
                    bool(value & item.value), True)

        # Other supported properties in a topic using property name
        elif name in {
                Device.PROP_DEVICE_ID, Device.PROP_ZONE, Device.PROP_TYPE,
                Device.PROP_RSSI_DB, Device.PROP_RSSI_BARS,
                SpecialDevice.PROP_HIGH_LIMIT, SpecialDevice.PROP_LOW_LIMIT,
                SpecialDevice.PROP_CONTROL_LIMIT_FIELDS_EXIST,
                SpecialDevice.PROP_CONTROL_HIGH_LIMIT,
                SpecialDevice.PROP_CONTROL_LOW_LIMIT}:
            self._publish('{}/{}'.format(topic_parent, name), value, True)

    def _publish_ha_device_config(self, device: Device,
                                  device_config: TranslatorDeviceConfig):
        # Generate message that can be used to automatically configure the
        # device in Home Assistant using it's MQTT Discovery functionality
        message = {
            Translator.HA_NAME: device_config.ha_name,
            Translator.HA_UNIQUE_ID: '{}_{:06x}'.format(
                PROJECT_NAME, device.device_id),
            Translator.HA_STATE_TOPIC: device_config.topic,
            Translator.HA_AVAILABILITY_TOPIC: '{}/{}'.format(
                self._config.translator.baseunit, BaseUnit.PROP_IS_CONNECTED),
            Translator.HA_PAYLOAD_AVAILABLE: str(True),
            Translator.HA_PAYLOAD_NOT_AVAILABLE: str(False),
        }
        if device.type in {DeviceType.FloodDetector, DeviceType.FloodDetector2}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_MOISTURE
            message[Translator.HA_PAYLOAD_ON] = str(OnOff.On)
            message[Translator.HA_PAYLOAD_OFF] = str(OnOff.Off)
        elif device.type in {DeviceType.MedicalButton}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_SAFETY
            message[Translator.HA_PAYLOAD_ON] = str(OnOff.On)
            message[Translator.HA_PAYLOAD_OFF] = str(OnOff.Off)
        elif device.type in {DeviceType.AnalogSensor, DeviceType.AnalogSensor2}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_PAYLOAD_ON] = str(OnOff.On)
            message[Translator.HA_PAYLOAD_OFF] = str(OnOff.Off)
        elif device.type in {DeviceType.SmokeDetector}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_SMOKE
            message[Translator.HA_PAYLOAD_ON] = str(OnOff.On)
            message[Translator.HA_PAYLOAD_OFF] = str(OnOff.Off)
        elif device.type in {DeviceType.PressureSensor, DeviceType.PressureSensor2}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_MOTION
            message[Translator.HA_PAYLOAD_ON] = str(OnOff.On)
            message[Translator.HA_PAYLOAD_OFF] = str(OnOff.Off)
        elif device.type in {DeviceType.CODetector, DeviceType.CO2Sensor,
                             DeviceType.CO2Sensor2, DeviceType.GasDetector}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_GAS
            message[Translator.HA_PAYLOAD_ON] = str(OnOff.On)
            message[Translator.HA_PAYLOAD_OFF] = str(OnOff.Off)
        elif device.type in {DeviceType.DoorMagnet}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_DOOR
            message[Translator.HA_PAYLOAD_ON] = str(OpenClosed.Open)
            message[Translator.HA_PAYLOAD_OFF] = str(OpenClosed.Closed)
        elif device.type in {DeviceType.VibrationSensor}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_VIBRATION
            message[Translator.HA_PAYLOAD_ON] = str(OnOff.On)
            message[Translator.HA_PAYLOAD_OFF] = str(OnOff.Off)
        elif device.type in {DeviceType.PIRSensor}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_MOTION
            message[Translator.HA_PAYLOAD_ON] = str(OnOff.On)
            message[Translator.HA_PAYLOAD_OFF] = str(OnOff.Off)
        elif device.type in {DeviceType.GlassBreakDetector}:
            ha_platform = Translator.HA_PLATFORM_BINARY_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_WINDOW
            message[Translator.HA_PAYLOAD_ON] = str(OnOff.On)
            message[Translator.HA_PAYLOAD_OFF] = str(OnOff.Off)
        elif device.type in {DeviceType.HumidSensor, DeviceType.HumidSensor2}:
            ha_platform = Translator.HA_PLATFORM_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_HUMIDITY
            message[Translator.HA_UNIT_OF_MEASUREMENT] = Translator.HA_UOM_HUMIDITY
        elif device.type in {DeviceType.TempSensor, DeviceType.TempSensor2}:
            ha_platform = Translator.HA_PLATFORM_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_TEMPERATURE
            message[Translator.HA_UNIT_OF_MEASUREMENT] = Translator.HA_UOM_TEMPERATURE
        elif device.type in {DeviceType.LightSensor, DeviceType.LightDetector}:
            ha_platform = Translator.HA_PLATFORM_SENSOR
            message[Translator.HA_DEVICE_CLASS] = Translator.HA_DC_ILLUMINANCE
            message[Translator.HA_UNIT_OF_MEASUREMENT] = Translator.HA_UOM_ILLUMINANCE
        elif device.type in {
                DeviceType.ACCurrentMeter, DeviceType.ACCurrentMeter2,
                DeviceType.ThreePhaseACMeter}:
            ha_platform = Translator.HA_PLATFORM_SENSOR
            message[Translator.HA_UNIT_OF_MEASUREMENT] = Translator.HA_UOM_CURRENT
        else:
            _LOGGER.warning("Device type '%s' cannot be represented in Home "
                            "Assistant and will be skipped.", str(device.type))
            return
        self._publish(
            '{}/{}/{}/config'.format(
                self._config.translator.ha_discovery_prefix,
                ha_platform,
                message[Translator.HA_UNIQUE_ID]),
            json.dumps(message), True)

    def _publish_ha_switch_config(self, switch_number: SwitchNumber,
                                  switch_config: TranslatorSwitchConfig):
        # Generate message that can be used to automatically configure the
        # switch in Home Assistant using it's MQTT Discovery functionality
        message = {
            Translator.HA_NAME: switch_config.ha_name,
            Translator.HA_UNIQUE_ID: '{}_{}'.format(
                PROJECT_NAME, str(switch_number).lower()),
            Translator.HA_STATE_TOPIC: switch_config.topic,
            Translator.HA_COMMAND_TOPIC: '{}/{}'.format(
                switch_config.topic, Translator.TOPIC_SET),
            Translator.HA_PAYLOAD_ON: str(OnOff.On),
            Translator.HA_PAYLOAD_OFF: str(OnOff.Off),
            Translator.HA_AVAILABILITY_TOPIC: '{}/{}'.format(
                self._config.translator.baseunit, BaseUnit.PROP_IS_CONNECTED),
            Translator.HA_PAYLOAD_AVAILABLE: str(True),
            Translator.HA_PAYLOAD_NOT_AVAILABLE: str(False),
        }
        self._publish(
            '{}/{}/{}/config'.format(
                self._config.translator.ha_discovery_prefix,
                Translator.HA_PLATFORM_SWITCH,
                message[Translator.HA_UNIQUE_ID]),
            json.dumps(message), True)

    def _publish(self, topic: str, message: Any, retain: bool) -> None:
        # Wrapper to start the async method
        coro = self._async_publish(topic, message, retain)
        self._loop.create_task(coro)

    async def _async_publish(self, topic: str, message: Any, retain: bool) -> None:
        # Replace null with empty string; HBMQTT doesn't like it
        if message is None:
            message = ''

        # Convert to bytearray when needed
        if not isinstance(message, bytearray):
            # Convert to string first, if not already
            message = str(message)

            # Convert string to UTF-8 bytearray
            message = message.encode()

        await self._mqtt.publish(topic, message, retain=retain)

    def _on_message_baseunit(self,
                             subscribetopic: SubscribeTopic,
                             message: ApplicationMessage) -> None:
        if subscribetopic.args == BaseUnit.PROP_OPERATION_MODE:
            # Set operation mode
            name = None if not message.data else message.data.decode()
            operation_mode = OperationMode.parse_name(name)
            if operation_mode is None:
                _LOGGER.warning("Cannot set operation_mode to '%s'", name)
                return
            self._loop.create_task(
                self._baseunit.async_set_operation_mode(operation_mode))
        else:
            raise NotImplementedError

    def _on_message_clear_status(self,
                                 subscribetopic: SubscribeTopic,
                                 message: ApplicationMessage) -> None:
        # Clear the alarm/warning LEDs on base unit and stop siren
        self._loop.create_task(
            self._baseunit.async_clear_status())

    def _on_message_set_datetime(self,
                                 subscribetopic: SubscribeTopic,
                                 message: ApplicationMessage) -> None:
        # Set remote date/time to specified date/time (or current if None)
        value = None if not message.data else message.data.decode()
        if value:
            value = dateutil.parser.parse(value)
        self._loop.create_task(
            self._baseunit.async_set_datetime(value))

    def _on_message_switch(self,
                           subscribetopic: SubscribeTopic,
                           message: ApplicationMessage) -> None:
        # Turn a switch on / off
        switch_number = subscribetopic.args
        name = None if not message.data else message.data.decode()
        state = OnOff.parse_name(name)
        if state is None:
            _LOGGER.warning("Cannot set switch %s to '%s'", switch_number, name)
            return
        self._loop.create_task(
            self._baseunit.async_set_switch_state(
                switch_number, bool(state.value)))
