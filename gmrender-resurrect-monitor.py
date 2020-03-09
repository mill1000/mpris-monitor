#! /usr/bin/python3

import threading
import argparse
import logging

# DBus interaction
import pydbus
from gi.repository import GLib

def state_signal_callback(sender, object, iface, signal, params):
  state, *remaining = params

  logger.debug("Got signal from '{0}': '{1}' = '{2}'".format(object, signal, state))

  monitor.Update(state)

class SystemMonitor():
  def __init__(self, long, short):
    self.short_timeout = short
    self.long_timeout = long

    self.active = False
    self.timer = None
    self.paused = False

  def Update(self, state):
    if state == "PLAYING":
      if not self.active:
        self.Activate()

      if self.timer and self.timer.is_alive():
        logger.info("Disabled shutdown timer.")
        self.timer.cancel()

      # Delete existing timer
      self.timer = None
      self.paused = False

    elif state == "PAUSED_PLAYBACK":
      # Shouldn't ever have a running timer when entering paused state unless you somehow went from STOPPED to
      assert(self.timer == None)

      # Start shutdown timer with long interval
      logger.info("Starting long ({0} s) shutdown timer".format(self.long_timeout))
      self.timer = threading.Timer(self.long_timeout, self.Deactivate)
      self.timer.start()
      self.paused = True

    elif state == "STOPPED":
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
      logger.info("Starting short ({0} s) shutdown timer".format(self.short_timeout))
      self.timer = threading.Timer(self.short_timeout, self.Deactivate)
      self.timer.start()

  def Activate(self):
    print("ENABLING SYSTEM POWER")
    self.active = True

  def Deactivate(self):
    print("DISABLING SYSTEM POWER")
    self.active = False
    
    # Stop and destroy timer if present
    if self.timer:
      self.timer.cancel()
      self.timer = None

if __name__ == "__main__":
  # Argument parsing
  parser = argparse.ArgumentParser(description="Automate system power by subscribing to gmrender-resurrect D-Bus signals.", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
  parser.add_argument("--pause-timeout", help="Disable system power when paused for this duration (seconds)", default=60, type=int)
  parser.add_argument("--stop-timeout", help="Disable system power when stopped for this duration (seconds)", default=5, type=int)
  parser.add_argument("--uuid", help="Monitor for a specific gmedia-resurrect instance given by the UUID")
  args = parser.parse_args()

  # Basic log config
  logging.basicConfig(level=logging.INFO)
  logger = logging.getLogger("gmrender-resurrect-monitor")

  # Create system monitor object to handle state
  monitor = SystemMonitor(args.pause_timeout, args.stop_timeout)
  
  # Listen for events from a particular instance
  target = None
  if args.uuid:
    target = "/com/hzeller/gmedia_resurrect/" + args.uuid.replace("-", "_")

  # Subscribe to gmedia-resurrect events
  bus = pydbus.SystemBus()
  bus.subscribe(object=target, iface="com.hzeller.gmedia_resurrect.v1.Transport", signal="State", signal_fired=state_signal_callback)
  
  # Start the main loop to monitor for events
  loop = GLib.MainLoop()
  
  logger.info("Monitoring for D-Bus signals.")
  try:
    loop.run()
  except:
    loop.quit()

  logger.info("Shutting down.")