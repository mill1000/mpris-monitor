#!/usr/bin/env python3

import argparse
import asyncio
import enum
import logging

import kasa
from dbus_next import BusType, Message, MessageType
from dbus_next.aio import MessageBus

_LOGGER = logging.getLogger("mpris-monitor")


class MprisDbusMonitor():
    """A MPRIS monitor based on asyncio via dbus-next."""

    def __init__(self):
        self.friendly_names = {}
        self.playback_status_changed = None
        self.player_removed = None
        self.bus = None

    async def start(self):
        # Connect to the system bus
        _LOGGER.info("Connecting to system bus.")
        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

        _LOGGER.info("Fetching names from D-Bus.")
        for name in filter(lambda n: n.startswith("org.mpris.MediaPlayer2"), await self._dbus_list_names()):
            owner = await self._dbus_get_name_owner(name)
            self.friendly_names[owner] = name
            _LOGGER.debug("%s owns %s.", owner, name)

        # Add matches for NameOwnerChanged and MRPIS PropertiesChanged signals
        await self._dbus_add_match(["member='NameOwnerChanged',arg0namespace='org.mpris.MediaPlayer2'"])
        await self._dbus_add_match(["type='signal',member='PropertiesChanged',path='/org/mpris/MediaPlayer2'"])

        # Define a common message handler to filter and call more specific handlers
        def _message_handler(msg):
            # _LOGGER.debug("Got new DBus message: %s", vars(msg))

            if msg.path == "/org/mpris/MediaPlayer2" and msg.member == "PropertiesChanged":
                self._properties_changed(
                    msg.sender, msg.interface, msg.member, msg.body)

            if msg.path == "/org/freedesktop/DBus" and msg.member == "NameOwnerChanged":
                self._name_owner_changed(
                    msg.sender, msg.interface, msg.member, msg.body)

        # Add handler
        _LOGGER.info("Monitoring for D-Bus signals.")
        self.bus.add_message_handler(_message_handler)

        await self.bus.wait_for_disconnect()

    async def _dbus_get_name_owner(self, name):
        """Get the owner of the provided name."""
        reply = await self.bus.call(
            Message(destination='org.freedesktop.DBus',
                    path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus',
                    member='GetNameOwner',
                    signature='s',
                    body=[name]))

        return reply.body[0]

    async def _dbus_list_names(self):
        """List names on the bus."""
        reply = await self.bus.call(
            Message(destination='org.freedesktop.DBus',
                    path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus',
                    member='ListNames'))

        return reply.body[0]

    async def _dbus_add_match(self, body):
        """"Add a match rule on the bus."""
        reply = await self.bus.call(
            Message(
                message_type=MessageType.METHOD_CALL,
                destination='org.freedesktop.DBus',
                interface="org.freedesktop.DBus",
                path='/org/freedesktop/DBus',
                member='AddMatch',
                signature='s',
                body=body))

        assert reply.message_type == MessageType.METHOD_RETURN
        return reply

    def _properties_changed(self, sender, iface, member, body):
        """Callback for PropertiesChanged signal."""
        _interface, values, *remaining = body

        # Fetch friendly name if it exists
        sender = self.friendly_names.get(sender, sender)

        _LOGGER.debug("'%s' '%s' '%s' = '%s'", sender, iface, member, body)

        if self.playback_status_changed and "PlaybackStatus" in values:
            self.playback_status_changed(
                sender, values["PlaybackStatus"].value)

    def _name_owner_changed(self, sender, iface, member, body):
        """Callback for NameOwnerChanged signal."""
        name, old_owner, new_owner = body

        _LOGGER.debug("'%s' '%s' '%s' = '%s'", sender, iface, member, body)

        # Remove old owner
        if old_owner:
            try:
                del self.friendly_names[old_owner]
            except KeyError:
                # Ignore failed removes
                pass

            if self.player_removed:
                self.player_removed(name)

        # Add new owner
        if new_owner:
            self.friendly_names[new_owner] = name


class AsyncTimer():
    """A timer class built on asycio."""

    def __init__(self, timeout, callback):
        self._timeout = timeout
        self._callback = callback

    async def _run(self):
        await asyncio.sleep(self._timeout)
        await self._callback()

    def start(self):
        self._task = asyncio.create_task(self._run())

    def cancel(self):
        self._task.cancel()


class SystemController():
    """Class to manage system state."""

    class State(enum.Enum):
        ACTIVE = 1
        PAUSED = 2
        IDLE = 3

    def __init__(self, short_timeout, long_timeout):
        self.short_timeout = short_timeout
        self.long_timeout = long_timeout

        self.state = SystemController.State.IDLE
        self.timer = None

        self.activate = None
        self.deactivate = None

        self._active_players = set()

    def stop(self):
        # Stop and destroy timer if present
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def remove_player(self, sender):
        # Treat removal like a stopped status
        asyncio.create_task(self._update(sender, "Stopped"))

    def update(self, sender, state):
        asyncio.create_task(self._update(sender, state))

    async def _update(self, sender, state):
        _LOGGER.info("Player '{0}' status: {1}".format(sender, state))

        if state == "Playing":
            # Add the sender to the active list
            _LOGGER.debug("Adding player '{0}' to active list.".format(sender))
            self._active_players.add(sender)

            # Activate if necessary
            if self.state == SystemController.State.IDLE:
                await self._activate()

            # Ensure state is active
            self.state = SystemController.State.ACTIVE

            # Disable the shutdown timer if running
            if self.timer:
                _LOGGER.debug("Disabled shutdown timer.")
                self.timer.cancel()

            # Delete existing timer
            self.timer = None

        elif state == "Paused":
            # Player is not longer active
            self._active_players.discard(sender)
            _LOGGER.debug(
                "Removed player '{0}' from active list.".format(sender))

            # No action unless this is the last player
            if len(self._active_players):
                return

            # Shouldn't have a timer unless we somehow went from STOPPED to PAUSED
            assert (self.timer == None)

            # Start shutdown timer with long interval
            _LOGGER.debug(
                "Starting long ({0} s) shutdown timer.".format(self.long_timeout))
            self.timer = AsyncTimer(self.long_timeout, self._deactivate)
            self.timer.start()
            self.state = SystemController.State.PAUSED

        elif state == "Stopped":
            # Player is not longer active
            self._active_players.discard(sender)
            _LOGGER.debug(
                "Removed player '{0}' from active list.".format(sender))

            # No action unless this is the last player
            if len(self._active_players):
                return

            # Nothing to do if already idle
            if self.state == SystemController.State.IDLE:
                return

            if self.state == SystemController.State.PAUSED:
                # Cancel existing long timer
                assert (self.timer)
                self.timer.cancel()
            elif self.timer:
                return

            # Start shutdown timer with short interval
            _LOGGER.debug(
                "Starting short ({0} s) shutdown timer.".format(self.short_timeout))
            self.timer = AsyncTimer(self.short_timeout, self._deactivate)
            self.timer.start()

    async def _activate(self):
        _LOGGER.info("Enabling system power.")

        if self.activate:
            await self.activate()

        self.state = SystemController.State.ACTIVE

    async def _deactivate(self):
        _LOGGER.info("Disabling system power.")

        if self.deactivate:
            await self.deactivate()

        self.state = SystemController.State.IDLE

        # Stop and destroy timer if present
        if self.timer:
            self.timer.cancel()
            self.timer = None


async def _run(args):
    # Discover available devices
    _LOGGER.info("Discovering Kasa devices.")
    kasa_devices = (await kasa.Discover.discover(timeout=1)).items()
    _LOGGER.info("Found {0} Kasa devices.".format(len(kasa_devices)))

    # Dump discovered devices if requested
    if args.discover:
        _LOGGER.info("Discovered Kasa devices:")
        for addr, device in kasa_devices:
            _LOGGER.info(device)
        exit()

    # Find first device with matching alias
    strip = next((d for a, d in kasa_devices if d.alias ==
                 args.kasa_device_alias), None)

    if strip is None:
        _LOGGER.error("Could not find Kasa device '{0}'.".format(
            args.kasa_device_alias))
        exit()

    # Update strip information
    await strip.update()

    _LOGGER.info("Using Kasa device '{0}'.".format(strip.alias))

    # Local coroutines for controller callback
    async def power_on():
        # Preamp = 0
        # Amp 1 = 1
        # Amp 2 = 2
        for plug in strip.children:
            await plug.turn_on()
            await asyncio.sleep(1)

    async def power_off():
        # Turn off in reverse order
        for plug in reversed(strip.children):
            await plug.turn_off()
            await asyncio.sleep(1)

    # Create controller to handle system state
    controller = SystemController(args.stop_timeout, args.pause_timeout)
    controller.activate = power_on
    controller.deactivate = power_off

    # Start the MPRIS monitor
    mpris_monitor = MprisDbusMonitor()
    mpris_monitor.playback_status_changed = controller.update
    mpris_monitor.player_removed = controller.remove_player

    try:
        await mpris_monitor.start()
    except:
        pass

    _LOGGER.info("Shutting down.")

    # Stop controller timers
    controller.stop()


def main():
    # Basic log config
    logging.basicConfig(
        format='%(levelname)s: %(message)s', level=logging.INFO)

    # Argument parsing
    parser = argparse.ArgumentParser(description="Automate system power by subscribing to MPRIS D-Bus signals.",
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        "--pause_timeout", help="Disable system power when paused for this duration (seconds).", default=60, type=int)
    parser.add_argument(
        "--stop_timeout", help="Disable system power when stopped for this duration (seconds).", default=5, type=int)
    parser.add_argument(
        "--discover", help="List Kasa devices discovered on the network and exit.", action="store_true")
    parser.add_argument(
        "--verbose", help="Enable debug messages.", action="store_true")
    parser.add_argument("kasa_device_alias",
                        help="Kasa device to control.", nargs="?", default=None)
    args = parser.parse_args()

    if args.verbose:
        _LOGGER.setLevel(logging.DEBUG)

    if args.discover == False and args.kasa_device_alias is None:
        _LOGGER.error("Target Kasa device alias must be supplied.")
        exit()

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
