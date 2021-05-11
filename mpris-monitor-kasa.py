#! /usr/bin/python3

import threading
import argparse
import logging
import time
import enum

# DBus interaction
import pydbus
from gi.repository import GLib

# Smart switch control
import pyHS100 as kasa

class State(enum.Enum):
  ACTIVE = 1
  PAUSED = 2
  IDLE = 3

class SystemMonitor():
  def __init__(self, long, short):
    self.short_timeout = short
    self.long_timeout = long

    self.state = State.IDLE
    self.timer = None

    self.activate_callback = None
    self.deactivate_callback = None

    self._active_players = set()

  def Update(self, sender, state):
    if state == "Playing":
      # Add the sender to the active list
      self._active_players.add(sender)

      # Activate if necessary
      if self.state == State.IDLE:
        self._activate()

      # Ensure state is active
      self.state = State.ACTIVE

      # Disable the shutdown timer if running
      if self.timer and self.timer.is_alive():
        logger.debug("Disabled shutdown timer.")
        self.timer.cancel()

      # Delete existing timer
      self.timer = None

    elif state == "Paused":
      # Player is not longer active
      self._active_players.discard(sender)

      # No action unless this is the last player
      if len(self._active_players):
        return

      # Shouldn't have a timer unless we somehow went from STOPPED to PAUSED
      assert(self.timer == None)

      # Start shutdown timer with long interval
      logger.debug("Starting long ({0} s) shutdown timer.".format(self.long_timeout))
      self.timer = threading.Timer(self.long_timeout, self._deactivate)
      self.timer.start()
      self.state = State.PAUSED

    elif state == "Stopped":
      # Player is not longer active
      self._active_players.discard(sender)

      # No action unless this is the last player
      if len(self._active_players):
        return

      # Nothing to do if already idle
      if self.state == State.IDLE:
        return

      if self.state == State.PAUSED:
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
    logger.info("Enabling System Power.")

    if self.activate_callback:
      self.activate_callback()

    self.state = State.ACTIVE

  def _deactivate(self):
    logger.info("Disabling System Power.")

    if self.deactivate_callback:
      self.deactivate_callback()

    self.state = State.IDLE
    
    # Stop and destroy timer if present
    if self.timer:
      self.timer.cancel()
      self.timer = None

if __name__ == "__main__":
  # Basic log config
  logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
  logger = logging.getLogger("mpris-monitor")

  # Argument parsing
  parser = argparse.ArgumentParser(description="Automate system power by subscribing to MPRIS D-Bus signals.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument("--pause_timeout", help="Disable system power when paused for this duration (seconds).", default=60, type=int)
  parser.add_argument("--stop_timeout", help="Disable system power when stopped for this duration (seconds).", default=5, type=int)
  parser.add_argument("--discover", help="List Kasa devices discovered on the network and exit.", action="store_true")
  parser.add_argument("kasa_device_alias", help="Kasa device to control.", nargs="?", default=None)
  args = parser.parse_args()

  if args.discover == False and args.kasa_device_alias is None:
    logger.error("Target Kasa device alias must be supplied.")
    exit()

  # Discover available devices
  logger.info("Discovering Kasa devices.")
  kasa_devices = kasa.Discover.discover(timeout=1).values()
  logger.info("Found {0} Kasa devices.".format(len(kasa_devices)))

  # Dump discovered devices if requested
  if args.discover:
    logger.info("Discovered Kasa devices:")
    for device in kasa_devices:
      logger.info(device)
    exit()
  
  # Find first device with matching alias
  strip = next((x for x in kasa_devices if x.alias == args.kasa_device_alias), None)

  if strip is None:
    logger.error("Could not find Kasa device '{0}'.".format(args.kasa_device_alias))
    exit()

  logger.info("Using Kasa device '{0}'.".format(strip.alias))

 # Local functions for monitor callbacks
  def power_on():
    # Preamp = 0
    # Amp 1 = 1
    # Amp 2 = 2
    for i in range(0,3):
      strip.turn_on(index=i)
      time.sleep(1)

  def power_off():
    # Turn off in reverse order
    for i in reversed(range(0,3)):
      strip.turn_off(index=i)
      time.sleep(1)

  # Create system monitor object to handle state
  monitor = SystemMonitor(args.pause_timeout, args.stop_timeout)
  monitor.activate_callback = power_on
  monitor.deactivate_callback = power_off
  
  def signal_fired_callback(sender, object, iface, signal, params):
    _interface, values, *remaining = params

    logger.debug("Got signal from '{0}':'{1}' '{2}' = '{3}'".format(object, iface, signal, params))

    if "PlaybackStatus" in values:
      monitor.Update(sender, values["PlaybackStatus"])

  # Subscribe to MPRIS events
  bus = pydbus.SystemBus()
  bus.subscribe(object="/org/mpris/MediaPlayer2", iface="org.freedesktop.DBus.Properties", signal="PropertiesChanged", signal_fired=signal_fired_callback)
  
  # Start the main loop to monitor for events
  loop = GLib.MainLoop()
  
  logger.info("Monitoring for D-Bus signals.")
  try:
    loop.run()
  except:
    loop.quit()

  logger.info("Shutting down.")