#! /usr/bin/python3

import threading
import argparse
import logging
import time
import subprocess

# DBus interaction
import pydbus
from gi.repository import GLib

def state_signal_callback(sender, object, iface, signal, params):
  _interface, values, *remaining = params

  logger.debug("Got signal from '{0}':'{1}' '{2}' = '{3}'".format(object, iface, signal, params))

  if "PlaybackStatus" in values:
    monitor.Update(values["PlaybackStatus"])

class SystemMonitor():
  def __init__(self, long, short):
    self.short_timeout = short
    self.long_timeout = long

    self.active = False
    self.timer = None
    self.paused = False

    self.activate_callback = None
    self.deactivate_callback = None

  def Update(self, state):
    if state == "Playing":
      if not self.active:
        self.Activate()

      if self.timer and self.timer.is_alive():
        logger.debug("Disabled shutdown timer.")
        self.timer.cancel()

      # Delete existing timer
      self.timer = None
      self.paused = False

    elif state == "Paused":
      # Shouldn't ever have a running timer when entering paused state unless you somehow went from STOPPED to
      assert(self.timer == None)

      # Start shutdown timer with long interval
      logger.debug("Starting long ({0} s) shutdown timer.".format(self.long_timeout))
      self.timer = threading.Timer(self.long_timeout, self.Deactivate)
      self.timer.start()
      self.paused = True

    elif state == "Stopped":
      if not self.active:
        return

      if self.paused:
        assert(self.timer)
        # If paused, we want to start a short timer
        self.timer.cancel()
      elif self.timer:
        # If timer exists, it should be running
        assert(self.timer.is_alive())
        return

      # Start shutdown timer with short interval
      logger.debug("Starting short ({0} s) shutdown timer.".format(self.short_timeout))
      self.timer = threading.Timer(self.short_timeout, self.Deactivate)
      self.timer.start()

  def Activate(self):
    logger.info("Enabling System Power.")

    if self.activate_callback:
      self.activate_callback()

    self.active = True

  def Deactivate(self):
    logger.info("Disabling System Power.")

    if self.deactivate_callback:
      self.deactivate_callback()

    self.active = False
    
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
  args = parser.parse_args()

  # Local functions to send on and off IR commands
  def power_on():
    subprocess.run(["ir-ctl", "-d", "/dev/lirc1","--scancode", "necx:0x404003"])

  def power_off():
    subprocess.run(["ir-ctl", "-d", "/dev/lirc1","--scancode", "necx:0x404000"])

  # Create system monitor object to handle state
  monitor = SystemMonitor(args.pause_timeout, args.stop_timeout)
  monitor.activate_callback = power_on
  monitor.deactivate_callback = power_off
  
  # Subscribe to MPRIS events
  bus = pydbus.SystemBus()
  bus.subscribe(object="/org/mpris/MediaPlayer2", iface="org.freedesktop.DBus.Properties", signal="PropertiesChanged", signal_fired=state_signal_callback)
  
  # Start the main loop to monitor for events
  loop = GLib.MainLoop()
  
  logger.info("Monitoring for D-Bus signals.")
  try:
    loop.run()
  except:
    loop.quit()

  logger.info("Shutting down.")