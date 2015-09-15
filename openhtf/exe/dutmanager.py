# Copyright 2014 Google Inc. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""Module for handling connect, disconnect, and serial number of a DUT.

In order for the CellExecutor (see executor.py) to know when to start a test,
it needs a way to know when a DUT has been connected.  Also, the test can't
restart until the DUT is removed and re-appears.  The serial for the TestRun
can be read from the DUT, or from the frontend.

This module handles these various functions.
"""

import collections
import logging
import threading
import time

from openhtf import conf
from openhtf.plugs.usb import adb_device
from openhtf.plugs.usb import fastboot_device
from openhtf.plugs.usb import local_usb
from openhtf.plugs.usb import usb_exceptions

conf.Declare('test_start', 'Mechanism to use for starting a test.',
             default_value='auto')
conf.Declare('unknown_dut_id', 'Value to be used for DUT ID when test_start is '
             '"auto".', default_value='UNKNOWN_DUT_ID', required=False)


class InvalidTestStartError(Exception):
  """Raised when an invalid value is provided for test_start."""


class StubHandler(object):
  """Noop handler for testing."""

  def __init__(self, dummy_cell_number, config):
    self.config = config

  def TestStart(self):
    """Return the dummy serial."""
    return self.config.unknown_dut_id

  @staticmethod
  def TestStop():
    """Noop Stop method."""
    pass


class AndroidHandler(object):
  """Class encapsulating ADB/Fastboot access to a DUT."""

  def __init__(self, dummy_cell_number, dummy_config):
    self.serial_number = None

  def _TryOpen(self):
    """Try to open a USB handle."""
    handle = None
    for cls, subcls, protocol in [(adb_device.CLASS,
                                   adb_device.SUBCLASS,
                                   adb_device.PROTOCOL),
                                  (fastboot_device.CLASS,
                                   fastboot_device.SUBCLASS,
                                   fastboot_device.PROTOCOL)]:
      try:
        handle = local_usb.LibUsbHandle.Open(
            serial_number=self.serial_number,
            interface_class=cls,
            interface_subclass=subcls,
            interface_protocol=protocol)
        self.serial_number = handle.serial_number
        return True
      except usb_exceptions.DeviceNotFoundError:
        pass
      except usb_exceptions.MultipleInterfacesFoundError:
        logging.warning('Multiple Android devices found, ignoring!')
      finally:
        if handle:
          handle.Close()
    return False

  def TestStart(self):
    """Returns serial when the test is ready to start."""
    while not self._TryOpen():
      time.sleep(1)
    return self.serial_number

  def TestStop(self):
    """Returns True when the test is completed and can restart."""
    while self._TryOpen():
      time.sleep(1)
    self.serial_number = None


class FrontendHandler(object):
  """Class encapsulating start interactions from the frontend.

  We keep a class-level map of cell number to deque to store incoming events
  from the frontend.  Interactions with that map and deque are thread-safe.
  TestStart() and TestStop(), however, are not thread-safe with each other,
  which is fine because only the framework will ever interally call these, and
  only ever sequentially.
  """

  # Map cell number to corresponding deque.
  DEQUE_MAP = collections.defaultdict(collections.deque)
  DEQUE_COND = threading.Condition()

  def __init__(self, cell_number, dummy_config):
    self.cell_number = cell_number
    self.serial = None

  @classmethod
  def Enqueue(cls, cell_number, serial=''):
    """Trigger actual test start."""
    with cls.DEQUE_COND:
      cls.DEQUE_MAP[cell_number].append(serial)
      cls.DEQUE_COND.notifyAll()

  def _WaitForFrontend(self):
    """Returns serial when received from the frontend."""
    with self.DEQUE_COND:
      while not len(self.DEQUE_MAP[self.cell_number]):
        self.DEQUE_COND.wait()
      return self.DEQUE_MAP[self.cell_number].popleft()

  def TestStart(self):
    """Get a serial from the frontend and return it."""
    if self.serial is not None:
      self.serial = None
      return self.serial
    return self._WaitForFrontend()

  def TestStop(self):
    """Returns when the test is completed and can restart.

    In this case, we don't stop the last test and start the next until a new
    start event has been received from the frontend.  This means we have to save
    the serial in that event for the next call to TestStart().
    """
    self.serial = self._WaitForFrontend()


class FrontendAndroidHandler(FrontendHandler):
  """Grab start events from frontend, but serial from Android."""

  def __init__(self, *args):
    super(FrontendAndroidHandler, self).__init__(*args)
    self.android_handler = AndroidHandler(*args)

  def TestStart(self):
    super(FrontendAndroidHandler, self).TestStart()
    return self.android_handler.TestStart()


class DutManager(object):
  """Manage the connection, disconnection, and serial number of a DUT."""

  HANDLERS = {
      'android': AndroidHandler,
      'frontend': FrontendAndroidHandler,
      'frontend_serial': FrontendHandler,
      'auto': StubHandler,
  }

  def __init__(self, handler):
    self.handler = handler

  def WaitForTestStart(self):
    """Delegate test start to the configured handler."""
    return self.handler.TestStart()

  def WaitForTestStop(self):
    """Delegate test stop to the configured handler."""
    self.handler.TestStop()

  @classmethod
  def FromConfig(cls, cell_number, config):
    """Create a handler from config."""
    if config.test_start not in cls.HANDLERS:
      raise InvalidTestStartError(
          '%s not a recognized test_start, expected: %s',
          config.test_start, cls.HANDLERS.keys())
    return cls(cls.HANDLERS[config.test_start](cell_number, config))
