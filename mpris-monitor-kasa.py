#! /usr/bin/python3

import threading
import argparse
import logging
import time
import enum
import threading

# Smart switch control
import kasa
import asyncio

# DBus interaction
import pydbus
from gi.repository import GLib
from gi.repository import Gio

class MprisDbusMonitor(threading.Thread):
  def __init__(self):
    threading.Thread.__init__(self)

    self.friendly_names = {}
    self.playback_status_changed = None
    self.player_removed = None

  def run(self):
    bus = pydbus.SystemBus()

    # Subscribe to MPRIS events
    bus.subscribe(object="/org/mpris/MediaPlayer2", iface="org.freedesktop.DBus.Properties", signal="PropertiesChanged", signal_fired=self.properties_changed)

    # Subscribe to name changes
    bus.subscribe(object="/org/freedesktop/DBus", iface="org.freedesktop.DBus", signal="NameOwnerChanged", arg0="org.mpris.MediaPlayer2", flags=Gio.DBusSignalFlags.MATCH_ARG0_NAMESPACE, signal_fired=self.name_owner_changed)
    
    logger.info("Fetching names from D-Bus.")
    
    # Load friendly names from bus
    obj = bus.get("org.freedesktop.DBus")
    for name in filter(lambda n: n.startswith("org.mpris.MediaPlayer2"), obj.ListNames()):
      owner = obj.GetNameOwner(name)
      self.friendly_names[owner] = name
      logger.debug("{0} owns {1}.".format(owner, name))

    # Start the main loop to monitor for events
    self.loop = GLib.MainLoop()
    
    logger.info("Monitoring for D-Bus signals.")
    try:
      self.loop.run()
    except:
      self.loop.quit()

  def stop(self):
    # Stop the Dbus loop
    if self.loop:
      self.loop.quit()

  # Callback for PropertiesChanged signal
  def properties_changed(self, sender, object, iface, signal, params):
    _interface, values, *remaining = params

    # Fetch friendly name if it exists
    sender = self.friendly_names.get(sender, sender)

    logger.debug("'{0}' '{1}' '{2}' '{3}' = '{4}'".format(sender, object, iface, signal, params))

    if self.playback_status_changed and "PlaybackStatus" in values:
      self.playback_status_changed(sender, values["PlaybackStatus"])
  
  # Callback for NameOwnerChanged signal
  def name_owner_changed(self, sender, object, iface, signal, params):
    name, old_owner, new_owner = params
    
    logger.debug("'{0}' '{1}' '{2}' '{3}' = '{4}'".format(sender, object, iface, signal, params))

    # Remove old owner
    if old_owner:
      del self.friendly_names[old_owner]
      
      if self.player_removed:
        self.player_removed(name)

    # Add new owner
    if new_owner:
      self.friendly_names[new_owner] = name
    
class SystemController():
  class State(enum.Enum):
    ACTIVE = 1
    PAUSED = 2
    IDLE = 3

  def __init__(self, loop, short, long):
    self.async_loop = loop
    self.short_timeout = short
    self.long_timeout = long

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
    self.update(sender, "Stopped")
  
  def update(self, sender, state):
    logger.info("Player '{0}' status: {1}".format(sender, state))

    if state == "Playing":
      # Add the sender to the active list
      logger.debug("Adding player '{0}' to active list.".format(sender))
      self._active_players.add(sender)

      # Activate if necessary
      if self.state == SystemController.State.IDLE:
        self._activate()

      # Ensure state is active
      self.state = SystemController.State.ACTIVE

      # Disable the shutdown timer if running
      if self.timer and self.timer.is_alive():
        logger.debug("Disabled shutdown timer.")
        self.timer.cancel()

      # Delete existing timer
      self.timer = None

    elif state == "Paused":
      # Player is not longer active
      self._active_players.discard(sender)
      logger.debug("Removed player '{0}' from active list.".format(sender))

      # No action unless this is the last player
      if len(self._active_players):
        return

      # Shouldn't have a timer unless we somehow went from STOPPED to PAUSED
      assert(self.timer == None)

      # Start shutdown timer with long interval
      logger.debug("Starting long ({0} s) shutdown timer.".format(self.long_timeout))
      self.timer = threading.Timer(self.long_timeout, self._deactivate)
      self.timer.start()
      self.state = SystemController.State.PAUSED

    elif state == "Stopped":
      # Player is not longer active
      self._active_players.discard(sender)
      logger.debug("Removed player '{0}' from active list.".format(sender))

      # No action unless this is the last player
      if len(self._active_players):
        return

      # Nothing to do if already idle
      if self.state == SystemController.State.IDLE:
        return

      if self.state == SystemController.State.PAUSED:
        # Cancel existing long timer
        assert(self.timer)
        self.timer.cancel()
      elif self.timer:
        # If timer exists, it should be running
        assert(self.timer.is_alive())
        return

      # Start shutdown timer with short interval
      logger.debug("Starting short ({0} s) shutdown timer.".format(self.short_timeout))
      self.timer = threading.Timer(self.short_timeout, self._deactivate)
      self.timer.start()

  def _activate(self):
    logger.info("Enabling system power.")

    if self.activate:
      asyncio.run_coroutine_threadsafe(self.activate(), self.async_loop)

    self.state = SystemController.State.ACTIVE

  def _deactivate(self):
    logger.info("Disabling system power.")

    if self.deactivate:
      asyncio.run_coroutine_threadsafe(self.deactivate(), self.async_loop)

    self.state = SystemController.State.IDLE
    
    # Stop and destroy timer if present
    if self.timer:
      self.timer.cancel()
      self.timer = None

async def main(args):
  # Discover available devices
  logger.info("Discovering Kasa devices.")
  kasa_devices = (await kasa.Discover.discover(timeout=1)).items()
  logger.info("Found {0} Kasa devices.".format(len(kasa_devices)))

  # Dump discovered devices if requested
  if args.discover:
    logger.info("Discovered Kasa devices:")
    for addr, device in kasa_devices:
      logger.info(device)
    exit()
  
  # Find first device with matching alias
  strip = next((d for a, d in kasa_devices if d.alias == args.kasa_device_alias), None)

  if strip is None:
    logger.error("Could not find Kasa device '{0}'.".format(args.kasa_device_alias))
    exit()

  # Update strip information
  await strip.update()

  logger.info("Using Kasa device '{0}'.".format(strip.alias))

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

  # Fetch the asyncio loop
  event_loop = asyncio.get_running_loop()

  # Create controller to handle system state
  controller = SystemController(event_loop, args.stop_timeout, args.pause_timeout)
  controller.activate = power_on
  controller.deactivate = power_off

  # Start the MPRIS monitor
  mpris_monitor = MprisDbusMonitor()
  mpris_monitor.playback_status_changed = controller.update
  mpris_monitor.player_removed = controller.remove_player
  mpris_monitor.start()

  # Await indefinitely to allow other asyncio tasks to run
  try:
    while True:
      await asyncio.sleep(1)
  except:
    pass

  logger.info("Shutting down.")

  # Stop controller timers
  controller.stop()

  # Stop monitor thread
  mpris_monitor.stop()
  mpris_monitor.join()

if __name__ == "__main__":
  # Basic log config
  logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
  logger = logging.getLogger("mpris-monitor")

  # Argument parsing
  parser = argparse.ArgumentParser(description="Automate system power by subscribing to MPRIS D-Bus signals.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument("--pause_timeout", help="Disable system power when paused for this duration (seconds).", default=60, type=int)
  parser.add_argument("--stop_timeout", help="Disable system power when stopped for this duration (seconds).", default=5, type=int)
  parser.add_argument("--discover", help="List Kasa devices discovered on the network and exit.", action="store_true")
  parser.add_argument("--verbose", help="Enable debug messages.", action="store_true")
  parser.add_argument("kasa_device_alias", help="Kasa device to control.", nargs="?", default=None)
  args = parser.parse_args()
  
  if args.verbose:
    logger.setLevel(logging.DEBUG)

  if args.discover == False and args.kasa_device_alias is None:
    logger.error("Target Kasa device alias must be supplied.")
    exit()
  
  try:
    asyncio.run(main(args))
  except KeyboardInterrupt:
    pass