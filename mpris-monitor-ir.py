#! /usr/bin/python3

import threading
import argparse
import logging
import time
import enum
import threading
import subprocess

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

  def __init__(self, long, short):
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
      self.activate()

    self.state = SystemController.State.ACTIVE

  def _deactivate(self):
    logger.info("Disabling system power.")

    if self.deactivate:
      self.deactivate()

    self.state = SystemController.State.IDLE
    
    # Stop and destroy timer if present
    if self.timer:
      self.timer.cancel()
      self.timer = None

def main(args):
  # Local functions to send on and off IR commands
  def power_on():
    subprocess.run(["ir-ctl", "-d", "/dev/lirc1","--scancode", "necx:0x404003"])

  def power_off():
    subprocess.run(["ir-ctl", "-d", "/dev/lirc1","--scancode", "necx:0x404000"])

  # Create controller to handle system state
  controller = SystemController(args.stop_timeout, args.pause_timeout)
  controller.activate = power_on
  controller.deactivate = power_off

  # Start the MPRIS monitor
  mpris_monitor = MprisDbusMonitor()
  mpris_monitor.playback_status_changed = controller.update
  mpris_monitor.player_removed = controller.remove_player
  mpris_monitor.start()

  # Wait on the monitor thread
  try:
    mpris_monitor.join()
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
  parser.add_argument("--verbose", help="Enable debug messages.", action="store_true")
  args = parser.parse_args()
  
  if args.verbose:
    logger.setLevel(logging.DEBUG)

  try:
    main(args)
  except KeyboardInterrupt:
    pass