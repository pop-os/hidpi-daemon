# hidpi-daemon: HiDPI daemon to manage HiDPI and LoDPI monitors on X
# Copyright (C) 2017-2018 System76, Inc.
#
# This file is part of `hidpi-daemon`.
#
# `hidpi-daemon` is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# `hidpi-daemon` is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with `hidpi-daemon`; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.

"""
HiDPI daemon backend listens for display changes and configures displays to match scale factor between hidpi and lodpi.
"""

from hidpidaemon import xlib
import Xlib
from Xlib import X
from Xlib import display as xdisplay
from Xlib.ext import randr

import logging
import time
import os
import gi

gi.require_version('Gtk', '3.0')

from gi.repository import Gio, GObject, GLib, Gtk
from pydbus import SessionBus
from pydbus.publication import Publication
from pydbus.generic import signal as PyDBusSignal
import signal

import subprocess
import re
import threading, queue
from shutil import which
from collections import namedtuple

from hidpidaemon import dbusutil
from hidpidaemon import monitorsxml

log = logging.getLogger(__name__)

NEEDS_HIDPI_AUTOSCALING = (
    'addw1',
    'addw2',
    'bonw12',
    'galp2',
    'galp3',
    'oryp2-ess',
    'oryp3-ess',
    'oryp3',
    'serw10',
    'serw11'
)

NVIDIA = {
    'addw1',
    'addw2',
    'bonw12',
    'oryp2-ess',
    'oryp3-ess',
    'serw10',
    'serw11'
}

INTEL = {
    'galp2',
    'galp3',
}

MODEL_MODES = {
    'galp2': '1600x900  118.25  1600 1696 1856 2112  900 903 908 934 -hsync +vsync',
    'galp3': '1600x900  118.25  1600 1696 1856 2112  900 903 908 934 -hsync +vsync',
}

#            name       pclk   hdisp,hsyncstart,hsyncend,hsyncend,htotal, v..., flags
#            '1600x900  118.25  1600 1696 1856 2112  900 903 908 934 -hsync +vsync',


# INCLUDING Patched python-xlib code (upstream since 2011), since the Ubuntu packages are even older.
# the patched code fixes a bug where part/all of the display name is missing when a display is plugged in.
major, minor = Xlib.__version__
if major < 0 or minor < 20:
    randr.GetOutputInfo = xlib._GetOutputInfo
    randr.get_output_info = xlib._get_output_info

    randr.GetCrtcInfo = xlib._GetCrtcInfo
    randr.get_crtc_info = xlib._get_crtc_info

    randr.CreateMode = xlib._CreateMode
    randr.create_mode = xlib._create_mode

    randr.AddOutputMode = xlib._AddOutputMode
    randr.add_output_mode = xlib._add_output_mode

    randr.SetCrtcConfig = xlib._SetCrtcConfig
    randr.set_crtc_config = xlib._set_crtc_config

    randr.GetOutputProperty = xlib._GetOutputProperty
    randr.get_output_property = xlib._get_output_property

XRes = namedtuple('XRes', ['x', 'y'])

class HiDPIGSettings(GObject.GObject):
    enable = GObject.Property(type=bool, default=True)
    mode = GObject.Property(type=str, default='lodpi')
    def __init__(self):
        GObject.GObject.__init__(self)

class HiDPIDBusServer(object):
    """
        <node>
            <interface name='com.system76.hidpi'>
                <method name="getstate"/>
                <signal name="state">
                    <arg type="s" name="mode" direction="out"/>
                    <arg type="s" name="monitor-types" direction="out"/>
                    <arg type="s" name="lodpi-capability" direction="out"/>
                </signal>
            </interface>
        </node>
    """

    def __init__(self, hidpi='lowdpi', display_types='lodpi', capability='native'):
        object.__init__(self)
        self.hidpi = hidpi
        self.display_types = display_types
        self.capability = capability

    def getstate(self):
        self.send_state_signal(hidpi=self.hidpi, display_types=self.display_types, capability=self.capability)

    def send_state_signal(self, hidpi='lowdpi', display_types='lodpi', capability='native'):
        self.hidpi = hidpi
        self.display_types = display_types
        self.capability = capability

        self.state(self.hidpi, self.display_types, self.capability)

    state = PyDBusSignal()

class HiDPIAutoscaling:
    def __init__(self, model):
        self.model = model
        self.displays = dict() # {'LVDS-0': 'connected', 'HDMI-0': 'disconnected'}
        self.screen_maximum = XRes(x=8192, y=8192)
        self.pixel_doubling = False
        self.scale_mode = 'hidpi' # If we have nvidia with the proprietary driver, set to hidpi for pixel doubling
        self.notification = None
        self.queue = queue.Queue()
        self.unforce = False
        self.saved = True
        self.calculated_display_size = (0,0) # Used to hack around intel black band bug (wrong XScreen size)
        self.prev_lid_state = self.get_internal_lid_state()
        self.dbs = HiDPIDBusServer()
        self.pub = None

        self.init_gsettings()
        self.init_xlib()

    def init_gsettings(self):
        #self.gsettings = HiDPIGSettings()
        self.settings = Gio.Settings('com.system76.hidpi')
        #self.settings.bind('mode', self.gsettings, 'mode', Gio.SettingsBindFlags.DEFAULT)

    def init_xlib(self):
        self.xlib_display = xdisplay.Display()
        screen = self.xlib_display.screen()
        self.xlib_window = screen.root.create_window(10,10,10,10,0, 0, window_class=X.InputOnly, visual=X.CopyFromParent, event_mask=0)
        self.xlib_window.xrandr_select_input(randr.RRScreenChangeNotifyMask)
        #            | randr.RROutputChangeNotifyMask
        #            | randr.RROutputPropertyNotifyMask)

        self.update_display_connections()
        if self.get_gpu_vendor() == 'nvidia':
            self.scale_mode = 'hidpi'
            self.screen_maximum = XRes(x=32768, y=32768)
        else:
            self.add_output_mode()

        self.displays_xml = self.get_displays_xml()

    #Test for nvidia proprietary driver and nvidia-settings
    def get_gpu_vendor(self):
        if self.model in INTEL:
            return 'intel'
        modules = open('/proc/modules', 'r')
        if 'nvidia ' in modules.read() and which('nvidia-settings') is not None:
            return 'nvidia'
        else:
            return 'intel'

    def add_output_mode(self):
        # GALP2 EXAMPLE
        # name       pclk   hdisp,hsyncstart,hsyncend,hsyncend,htotal, v..., flags
        # '1600x900  118.25  1600 1696 1856 2112  900 903 908 934 -hsync +vsync',
        if self.model not in MODEL_MODES:
            return
        modeline = MODEL_MODES[self.model].split()
        mode_id = 0
        #mode_name = modeline[0]
        mode_clk = int(round(float(modeline[1])))
        mode_horizontal = [int(modeline[2]), int(modeline[3]), int(modeline[4]), int(modeline[5])]
        mode_vertical = [int(modeline[6]), int(modeline[7]), int(modeline[8])]
        mode_name_length = len(modeline[0])
        #flags = modeline[10:]
        mode_flags = int(randr.HSyncNegative | randr.VSyncPositive)
        newmode = (mode_id, 1600, 900, mode_clk) +  tuple(mode_horizontal) + tuple(mode_vertical) + ( mode_name_length, mode_flags )
        try:
            randr.create_mode(self.xlib_window, newmode, '1600x900')
        except:
            # We got an error, but it's fine.
            # Eventually, we'll need to handle picking a 'close' mode if we can't make one.
            pass

        time.sleep(0.1)
        resources = self.xlib_window.xrandr_get_screen_resources()._data
        selected_output = None
        for output in resources['outputs']:
            info = randr.get_output_info(self.xlib_display, output, resources['config_timestamp'])._data
            if info['name'] == 'eDP-1':
                selected_output = output
        for mode in resources['modes']:
            if mode['width'] == 1600 and mode['height'] == 900:
                randr.add_output_mode(self.xlib_display, selected_output, mode['id'])

        # Need to refresh display modes to reflect the mode we just added
        self.update_display_connections()

    def get_displays_xml(self):
        mon_list = []
        resources = self.xlib_window.xrandr_get_screen_resources()._data
        for output in resources['outputs']:
            info = randr.get_output_info(self.xlib_display, output, resources['config_timestamp'])._data

            properties_list = self.xlib_display.xrandr_list_output_properties(output)._data
            for atom in properties_list['atoms']:
                atom_name = self.xlib_display.get_atom_name(atom)
                if atom_name == 'EDID':
                    prop = randr.get_output_property(self.xlib_display, output, atom, 19, 0, 128)._data
                    edid = bytes(prop['value'])
                    # get edid vendor code
                    edidv = prop['value'][9] + (prop['value'][8] << 8)
                    char1 = (int(edidv) & 0x7C00) >> 10
                    char2 = (int(edidv) & 0x3E0) >> 5
                    char3 = (int(edidv) & 0x001F) >> 0
                    table = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z']
                    edid_vendor = table[char1-1] + table[char2-1] + table[char3-1]

                    edidp = prop['value'][10] + (prop['value'][11] << 8)
                    modelname = None
                    for i in range(0x36, 0x7E, 0x12):
                        if edid[i] == 0x00 and edid[i+3] ==0xfc:
                            modelname = []
                            for j in range(0,13):
                                if edid[i+5+j] == 0x0a:
                                    modelname.append(0x00)
                                else:
                                    modelname.append(edid[i+5+j])
                    if not modelname:
                        edid_product = str(hex(edidp))
                    else:
                        edid_product = bytes(modelname).decode('utf-8').rstrip(' ').rstrip('\x00')

                    edids = prop['value'][12] + (prop['value'][13] << 8) + (prop['value'][14] << 16) + (prop['value'][15] << 24)
                    edid_serial = str.format('0x{:08x}', edids)
                    serial = None
                    for i in range(0x36, 0x7E, 0x12):
                        if edid[i] == 0x00 and edid[i+3] ==0xff:
                            serial = []
                            for j in range(0,13):
                                if edid[i+5+j] == 0x0a:
                                    serial.append(0x00)
                                else:
                                    serial.append(edid[i+5+j])
                    if not serial:
                        edid_serial = str.format('0x{:08x}', edids)
                    else:
                        edid_serial = bytes(serial).decode('utf-8').rstrip('\x00')

                    mon_list.append({'connector': info['name'], 'vendor': edid_vendor, 'product': edid_product, 'serial': edid_serial})


        xml = monitorsxml.MonitorsXml()
        c = xml.get_config_from_monitors(mon_list)
        return c


    def update_display_connections(self):
        resources = self.xlib_window.xrandr_get_screen_resources()._data
        self.resources = resources

        modes = dict()
        for mode in resources['modes']:
            modes[mode['id']] = mode

        try:
            primary_output = self.xlib_window.xrandr_get_output_primary()._data['output']
        except:
            primary_output = None

        new_displays = dict()
        for output in resources['outputs']:
            info = randr.get_output_info(self.xlib_display, output, resources['config_timestamp'])._data
            modelist = []
            for mode_id in info['modes']:
                mode = modes[mode_id]
                modelist.append(mode._data)
            new_displays[info['name']] = dict()
            new_displays[info['name']]['connected'] = not bool(info['connection'])
            new_displays[info['name']]['mm_width'] = info['mm_width']
            new_displays[info['name']]['mm_height'] = info['mm_height']
            new_displays[info['name']]['modes'] = modelist
            new_displays[info['name']]['crtc'] = info['crtc']
            if primary_output == output:
                new_displays[info['name']]['primary'] = True

            # Get connector type for each display. 'Panel' indicates internal display.
            new_displays[info['name']]['connector_type'] = ''
            properties_list = self.xlib_display.xrandr_list_output_properties(output)._data
            for atom in properties_list['atoms']:
                atom_name = self.xlib_display.get_atom_name(atom)
                if atom_name == randr.PROPERTY_CONNECTOR_TYPE:
                    prop = randr.get_output_property(self.xlib_display, output, atom, 4, 0, 100)._data
                    connector_type = self.xlib_display.get_atom_name(prop['value'][0])
                    new_displays[info['name']]['connector_type'] = connector_type
                if atom_name == 'PRIME Synchronization':
                    new_displays[info['name']]['prime'] = True


        # In some cases, the CRTC won't have changed when the lid opens.
        # So update displays if the lid state has changed.
        lid_state = self.get_internal_lid_state()
        if lid_state != self.prev_lid_state:
            self.prev_lid_state = lid_state

            # Always update displays on lid open
            if lid_state:
                self.displays = new_displays
                # delay to prevent race
                time.sleep(1)
                return True
            # Only update displays on lid close if an external display is connected.
            # This prevents mutter crashes.
            else:
                for display in new_displays:
                    status = new_displays[display]['connected']
                    connector_type = new_displays[display]['connector_type']
                    if 'eDP' in display or connector_type == 'Panel':
                        pass
                    elif status == True:
                        self.displays = new_displays
                        return True
        else:
            self.prev_lid_state = lid_state


        for display in new_displays:
            status = new_displays[display]['connected']
            if display in self.displays:
                old_status = self.displays[display]['connected']
                if status != old_status:
                    self.displays = new_displays
                    return True
                # Need to check for laptop lid closed.
                # When laptop lid is closed, crtc is 0, when open it should be a positive integer.
                new_crtc = new_displays[display]['crtc']
                old_crtc = self.displays[display]['crtc']
                if new_crtc != old_crtc:
                    if new_crtc == 0 or old_crtc == 0:
                        self.displays = new_displays
                        return True
            else:
                self.displays = new_displays
                return True

        self.displays = new_displays
        return False

    def acpid_listen(self):
        import socket

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect("/var/run/acpid.socket")

        while True:
            for event in s.recv(4096).decode('utf-8').split('\n'):
                event = event.split(' ')
                if event[0] == 'button/lid':
                    if event[2] == 'open':
                        self.notification_update_scaling()
                    elif event[2] == 'close':
                        pass


    def notification_terminate(self, status):
        self.pub.unpublish()
        os._exit(0)

    def notification_send_signal(self):
        gpu_vendor = self.get_gpu_vendor()
        if gpu_vendor == 'intel':
            capability = 'native'
        else:
            capability = 'pixel-doubling'

        has_mixed_dpi, has_hidpi, has_lowdpi = self.has_mixed_hi_low_dpi_displays()

        display_types = ''
        if has_mixed_dpi:
            display_types = display_types + 'mixed' + ', '
        if has_hidpi:
            display_types = display_types + 'hidpi' + ', '
        if has_lowdpi:
            display_types = display_types + 'lodpi' + ', '

        mode = self.settings.get_string('mode')

        self.dbs.send_state_signal(hidpi=mode, display_types=display_types, capability=capability)

    def notification_update_scaling(self, restart=True):
        if self.queue is not None:
            if self.get_gpu_vendor() == 'nvidia':
                if self.settings.get_string('mode') == 'hidpi':
                    self.scale_mode = 'hidpi'
                else:
                    self.scale_mode = 'lowdpi'
            else:
                if self.settings.get_string('mode') == 'lodpi':
                    self.unforce = False
                else:
                    self.unforce = True
            self.queue.put(self.scale_mode)
            self.queue.put(self.unforce)
        if self.get_gpu_vendor() == 'intel':
            # for threading reasons, create a new autoscaling instance...but do not call run() on it!
            h = HiDPIAutoscaling(self.model)
            h.unforce = self.unforce
            h.saved = not self.unforce
            h.set_scaled_display_modes(notification=False)
            # HiDPI scale factor doesn't always take on first mode set with
            # lid closed and only marginally hidpi external monitor.  If the
            # mode should be hidpi, check for scale factor and set again if
            # needed.
            has_mixed_dpi, has_hidpi, has_lowdpi = self.has_mixed_hi_low_dpi_displays()
            if not has_lowdpi and self.unforce:
                if dbusutil.get_scale() < 2:
                    h.set_scaled_display_modes(notification=False)
        if self.get_gpu_vendor() == 'nvidia': # nvidia
            h = HiDPIAutoscaling(self.model)
            h.scale_mode = self.scale_mode
            h.set_scaled_display_modes(notification=False)
            if self.workaround_prime_detect_lowdpi_primary():
                self.scale_mode = h.scale_mode
                self.notification_send_signal()

    def on_notification_mode(self, obj, gparamstring):
        self.notification_send_signal()
        self.notification_update_scaling(restart=False)

    def notification_register_dbus(self, has_mixed_dpi, unforce):
        settings = HiDPIGSettings()
        self.settings.bind('mode', settings, 'mode', Gio.SettingsBindFlags.DEFAULT)
        settings.connect('notify::mode', self.on_notification_mode)

        self.dbs = HiDPIDBusServer()

        bus = SessionBus()
        self.pub = Publication(bus, "com.system76.hidpi", self.dbs, allow_replacement=True, replace=True)
        if self.queue is not None:
            self.queue.put(self.dbs)
            self.queue.put(self.pub)

        self.loop = GLib.MainLoop()
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, signal.SIGINT, self.notification_terminate, None)
        self.loop.run()


    def workaround_prime_detect_lowdpi_primary(self):
        if self.scale_mode != 'hidpi':
            return

        has_lowdpi_prime, has_hidpi_prime = self.has_prime_displays()
        if not has_hidpi_prime:
            return False

        for display in self.displays:
            if 'primary' in self.displays[display]:
                dpi = self.get_display_dpi(display)
                if dpi is None:
                    return False
                elif dpi < 192: #GNOME's threshold
                    return True

        return False

    def workaround_show_prime_set_primary_dialog(self):
        # Show dialog to request switching hidpi display if the primary display is lowdpi
        if not self.workaround_prime_detect_lowdpi_primary():
            return

        output = subprocess.check_output('/usr/lib/hidpi-daemon/prime-dialog').decode('utf-8')
        response = Gtk.ResponseType(int(output))

        if response == Gtk.ResponseType.CANCEL:
            self.scale_mode = 'lowdpi'
            self.settings.set_string('mode', 'lodpi')
        elif response == Gtk.ResponseType.OK:
            self.scale_mode = 'hidpi'
            resources = self.xlib_window.xrandr_get_screen_resources()._data
            for output in resources['outputs']:
                info = randr.get_output_info(self.xlib_display, output, resources['config_timestamp'])._data
                if 'eDP-1' in info['name']:
                    self.xlib_window.xrandr_set_output_primary(output)
                    return


    def get_display_position(self, display_name, align=(0,0)):
        # For performance reasons, self.resources must be set with self.xlib_window.xrandr_get_screen_resources() before calling.
        #resources = self.xlib_window.xrandr_get_screen_resources()._data
        resources = self.resources._data
        crtc = self.displays[display_name]['crtc']
        connected = self.displays[display_name]['connected']
        if self.displays_xml:
            for log_mon in self.displays_xml['logical_monitors']:
                if log_mon['monitor_spec']['connector'] == display_name:
                    try:
                        x = int(log_mon['x']) + align[0] * int(log_mon['mode']['width'])
                        y = int(log_mon['y']) + align[1] * int(log_mon['mode']['height'])
                        return x, y
                    except:
                        -1, -1

        if crtc != 0:
            crtc_info = randr.get_crtc_info(self.xlib_display, crtc, resources['config_timestamp'])._data
            if align != (0,0):
                # Align to integer for easier/more consistent math elsewhere
                mode = dict()
                mode['width'] = crtc_info['width']
                mode['height'] = crtc_info['height']
                x = int(crtc_info['x'] + align[0] * mode['width'])
                y = int(crtc_info['y'] + align[1] * mode['height'])
                return x, y
            else:
                return crtc_info['x'], crtc_info['y']
        elif connected == True and not self.panel_activation_override(display_name):
            return 0, 0
        else:
            return -1, -1

    def get_display_dpi(self, display_name, current=False, saved=False):
        width = self.displays[display_name]['mm_width']
        height = self.displays[display_name]['mm_height']

        mode = {}

        if current:
            try:
                resources = self.resources._data
                crtc = self.displays[display_name]['crtc']
                if crtc != 0:
                    crtc_info = randr.get_crtc_info(self.xlib_display, crtc, resources['config_timestamp'])._data
                    mode = dict()
                    mode['width'] = crtc_info['width']
                    mode['height'] = crtc_info['height']
                else:
                    # No current mode is set, fallback to default resolution.
                    current = False
                    mode = self.displays[display_name]['modes'][0]
            except:
                return None
        elif saved and self.displays_xml:
                for log_mon in self.displays_xml['logical_monitors']:
                    if log_mon['monitor_spec']['connector'] == display_name:
                        mode = dict()
                        try:
                            mode['width'] = int(log_mon['mode']['width'])
                            mode['height'] = int(log_mon['mode']['height'])
                        except:
                            return None
                if mode == {}:
                    return None
        else:
            try:
                mode = self.displays[display_name]['modes'][0]
            except:
                return None

        x_res = mode['width']
        y_res = mode['height']

        # Some displays report aspect ratio instead of actual dimensions.
        if width == 160 and height == 90:
            if x_res >= 3840 and y_res >= 2160:
                return 192
            else:
                return 96

        if width > 0 and height > 0:
            dpi_x = x_res/width * 25.4
            dpi_y = y_res/height * 25.4
            return max(dpi_x, dpi_y)
        elif width == 0 and height == 0:
            return 0
        else:
            return None

    def get_display_logical_resolution(self, display_name, scale_factor, saved=False):
        try:
            mode = self.displays[display_name]['modes'][0]
            x_res = mode['width']
            y_res = mode['height']
            if saved and self.displays_xml:
                for log_mon in self.displays_xml['logical_monitors']:
                    if log_mon['monitor_spec']['connector'] == display_name:
                        x_res = int(log_mon['mode']['width'])
                        y_res = int(log_mon['mode']['height'])
            return int(x_res/scale_factor), int(y_res/scale_factor)
        except:
            return 0, 0

    def get_aligned_layout_entries(self, alignment):
        position_lookup_entries_x = dict()
        position_lookup_entries_y = dict()

        for display in self.displays:
            position_x, position_y = self.get_display_position(display, align=alignment)
            if position_x != -1 and position_y != -1:
                if position_x in position_lookup_entries_x:
                    position_lookup_entries_x[position_x].append(display)
                else:
                    position_lookup_entries_x[position_x] = [display]
                if position_y in position_lookup_entries_y:
                    position_lookup_entries_y[position_y].append(display)
                else:
                    position_lookup_entries_y[position_y] = [display]

        return position_lookup_entries_x, position_lookup_entries_y

    def get_adjacent_displays(self, display, display_graph, lookup_entries):
        display_left, display_top = self.get_display_position(display, align=(0,0))
        display_right, display_bottom = self.get_display_position(display, align=(1,1))

        #center_lookup_entries_x       = lookup_entries['center_x']
        top_left_lookup_entries_x     = lookup_entries['top_left_x']
        bottom_right_lookup_entries_x = lookup_entries['bottom_right_x']
        #center_lookup_entries_y       = lookup_entries['center_y']
        top_left_lookup_entries_y     = lookup_entries['top_left_y']
        bottom_right_lookup_entries_y = lookup_entries['bottom_right_y']

        display_graph[display] = []
        has_adjacent = False

        if display_left != -1:
            if display_left in bottom_right_lookup_entries_x:
                for adjacent_display in bottom_right_lookup_entries_x[display_left]:
                    adjacent_left, adjacent_top = self.get_display_position(adjacent_display, (0,0))
                    adjacent_right, adjacent_bottom = self.get_display_position(adjacent_display, (1,1))
                    if adjacent_top < display_bottom and adjacent_bottom > display_top:
                        has_adjacent = True
                        display_graph[display].append((adjacent_display, 'left'))

            if display_right in top_left_lookup_entries_x:
                for adjacent_display in top_left_lookup_entries_x[display_right]:
                    adjacent_left, adjacent_top = self.get_display_position(adjacent_display, (0,0))
                    adjacent_right, adjacent_bottom = self.get_display_position(adjacent_display, (1,1))
                    if adjacent_top < display_bottom and adjacent_bottom > display_top:
                        has_adjacent = True
                        display_graph[display].append((adjacent_display, 'right'))

            if display_top in bottom_right_lookup_entries_y:
                for adjacent_display in bottom_right_lookup_entries_y[display_top]:
                    adjacent_left, adjacent_top = self.get_display_position(adjacent_display, (0,0))
                    adjacent_right, adjacent_bottom = self.get_display_position(adjacent_display, (1,1))
                    if adjacent_left < display_right and adjacent_right > display_left:
                        has_adjacent = True
                        display_graph[display].append((adjacent_display, 'top'))

            if display_bottom in top_left_lookup_entries_y:
                for adjacent_display in top_left_lookup_entries_y[display_bottom]:
                    adjacent_left, adjacent_top = self.get_display_position(adjacent_display, (0,0))
                    adjacent_right, adjacent_bottom = self.get_display_position(adjacent_display, (1,1))
                    if adjacent_left < display_right and adjacent_right > display_left:
                        has_adjacent = True
                        display_graph[display].append((adjacent_display, 'bottom'))


            # Remove any (adjacent) closed internal displays from graph.
            # It can cause mutter to refuse to set scale if we give it space in layout.
            for adjacent_pair in display_graph[display]:
                adjacent, direction = adjacent_pair
                if self.panel_activation_override(adjacent):
                    display_graph[display].remove(adjacent_pair)
                    if len(display_graph[display]) == 0:
                       has_adjacent = False

            # If there's no adjacent display, find nearest one and flag it as adjacent with direction.
            # System will favor top/bottom over left/right, so adjust vertical offset by 9/16.
            if not has_adjacent:
                closest_display = None
                closest_distance = -1
                closest_direction = None
                for near in self.displays:
                    if self.displays[near]['connected'] and not self.panel_activation_override(near) and near != display:
                        dist_x = -1
                        dist_y = -1
                        near_left, near_top = self.get_display_position(near, (0,0))
                        near_right, near_bottom = self.get_display_position(near, (1,1))

                        if near_left <= display_right and near_right >= display_left:
                            bottom_x = near_top - display_bottom
                            top_x = display_top - near_bottom
                            dist_x = min(bottom_x, top_x, key=abs)
                            if dist_x == bottom_x:
                                direction = 'bottom'
                            else:
                                direction = 'top'

                            if not closest_display or abs(dist_x) < abs(closest_distance):
                                closest_display = near
                                closest_distance = dist_x
                                closest_direction = direction

                        elif near_top <= display_bottom and near_bottom >= display_top:
                            right_y = near_left - display_right
                            left_y = display_left - near_right
                            dist_y = min(right_y, left_y, key=abs)
                            if dist_y == right_y:
                                direction = 'right'
                            else:
                                direction = 'left'

                            dist_y = int(dist_y * 9 / 16)

                            if not closest_display or abs(dist_y) < abs(closest_distance):
                                closest_display = near
                                closest_distance = dist_y
                                closest_direction = direction

                if closest_display:
                    display_graph[display].append((closest_display, closest_direction))

        # Remove from graph if this is a closed internal display.
        # It can cause mutter to refuse to set scale if we give it space in layout.
        if self.panel_activation_override(display):
            display_graph[display] = []

        return display_graph[display]

    def get_display_graph(self, lookup_entries, revert=False):
        display_graph = {}
        for display in self.displays:
            # Find adjacencies
            display_graph[display] = self.get_adjacent_displays(display, display_graph, lookup_entries)

            # Remove display from graph if no adjacenies
            if not display_graph[display]:
                del display_graph[display]

        return display_graph

    def align_display_with_adjacent_x(self, display_left, display_right, adjacent_left, adjacent_right, adjacent_logical_resolution_x, offset, logical_resolution_x):
        # Left edges are aligned, keep them snapped
        if adjacent_left == display_left:
            new_display_left_x = offset
        # Right edges are aligned, keep them snapped
        elif adjacent_right == display_right:
            new_display_left_x = offset - adjacent_logical_resolution_x + adjacent_logical_resolution_x
        else:
            span_range = (adjacent_right - adjacent_left) + (display_right - display_left)
            span = adjacent_right - display_left
            new_span_range = int(adjacent_logical_resolution_x) + logical_resolution_x
            new_span = span * (new_span_range / span_range)

            new_adjacent_right = adjacent_left + offset + adjacent_logical_resolution_x
            new_display_left_x = (int(new_adjacent_right - new_span) - (adjacent_left))
        return new_display_left_x

    def calculate_layout2(self, revert=False):
        # Layout displays without overlap.  We need to make sure not to exceed
        # the maximum X screen size.  Intel graphics are limited to 8192x8192,
        # so a hidpi internal display and two external displays can exceed this
        # limit.

        # First, we calculate lookups for edge positions and use these to find
        # adjacent displays.  We build and traverse a graph of these adjacent
        # displays and position each display relative to its neighbor.

        self.resources = self.xlib_window.xrandr_get_screen_resources()

        center_lookup_entries_x,       center_lookup_entries_y       = self.get_aligned_layout_entries((0.5,0.5))
        top_left_lookup_entries_x,     top_left_lookup_entries_y     = self.get_aligned_layout_entries((0.0,0.0))
        bottom_right_lookup_entries_x, bottom_right_lookup_entries_y = self.get_aligned_layout_entries((1.0,1.0))

        lookup_entries = {'top_left_x': top_left_lookup_entries_x,
                          'top_left_y': top_left_lookup_entries_y,
                          'center_x': center_lookup_entries_x,
                          'center_y': center_lookup_entries_y,
                          'bottom_right_x': bottom_right_lookup_entries_x,
                          'bottom_right_y': bottom_right_lookup_entries_y}

        display_graph = dict()
        display_positions = dict()
        display_scales = dict()

        has_lowdpi_prime, has_hidpi_prime = self.has_prime_displays()

        # Calculate display scales
        for display in self.displays:
            display_left, display_top = self.get_display_position(display, align=(0,0))
            display_right, display_bottom = self.get_display_position(display, align=(1,1))

            # Get correct dpi and scale factor based on context.
            # Revert needs native resolution
            # Otherwise we need to use current resolution or value stored in monitors.xml if available.
            if revert:
                dpi = self.get_display_dpi(display)
            else:
                if self.displays_xml:
                    dpi = self.get_display_dpi(display, saved=self.saved)
                elif self.get_gpu_vendor() == 'intel':
                    dpi = self.get_display_dpi(display, current=True)
                else:
                    dpi = self.get_display_dpi(display)
            if dpi is None:
                scale_factor = 1
            elif dpi > 170 and revert == False:
                scale_factor = 2
            else:
                scale_factor = 1

            if self.get_gpu_vendor() == 'nvidia':
                if self.scale_mode == 'hidpi' and revert == False:
                    scale_factor = scale_factor / 2
                    if dpi is None:
                        pass
                    elif dpi <= 170:
                        if 'prime' in self.displays[display]:
                            scale_factor = scale_factor * 2
                        elif has_lowdpi_prime:
                            scale_factor = scale_factor * 2

            display_scales[display] = scale_factor

        # Generate graph of adjacent displays.
        display_graph = self.get_display_graph(lookup_entries, revert=revert)

        #Single display has no adjacencies!
        if len(display_graph) < 1:
            for display in self.displays:
                if self.displays[display]['connected'] == True:
                    display_positions[display] = (0, 0)

        # Walk adjacent display graph to generate new positions for each display.
        max_negative_offset_x = 32769 # Keep track of leftmost value and offset all displays by it.
        max_negative_offset_y = 32769 # Keep track of topmost value and  offset all displays by it.
        for adjacent_display in display_graph:
            if len(display_positions) < 1:
                display_positions[adjacent_display] = (0, 0)

            # Run through adjacent displays until all have positions.
            adjacent_displays = []
            for display, direction in display_graph[adjacent_display]:
                adjacent_displays.append((display, direction))
            while len(adjacent_displays) > 0:
                skip_pair = False
                display, direction = adjacent_displays[0]
                adjacent = adjacent_display
                # Set display position from adjacent if possible.
                # If not, try to swap pair and set position of adjacent display.
                # If neither has position available, skip this adjacent for now
                # and set it later once display position has been set.
                if adjacent_display in display_positions:
                    offset_x = display_positions[adjacent_display][0]
                    offset_y = display_positions[adjacent_display][1]
                elif display in display_positions:
                    # Swap display with adjacent display
                    offset_x = display_positions[display][0]
                    offset_y = display_positions[display][1]
                    temp_display = adjacent_display
                    adjacent = display
                    display = temp_display
                    # Need to invert directions so the math works out
                    if direction == 'left':
                        direction = 'right'
                    elif direction == 'right':
                        direction = 'left'
                    elif direction == 'top':
                        direction = 'bottom'
                    elif direction == 'bottom':
                        direction = 'top'
                else:
                    # Don't have either display position.  Try next adjacent display in list and come back to this pair.
                    skip_pair = True
                    del adjacent_displays[0]
                    if len(adjacent_displays) > 0:
                        adjacent_displays.append((display, direction))
                    #log.warning("Cannot find adjacent display in layout")

                if not skip_pair:
                    # Get positions, scale, and logical resolution for both displays
                    scale_factor = display_scales[display]
                    # getting correct logical resolution depends on whether to use native or saved values
                    logical_resolution_x, logical_resolution_y = self.get_display_logical_resolution(display, scale_factor, saved=(self.saved and not revert))

                    display_left, display_top = self.get_display_position(display, align=(0,0))
                    display_right, display_bottom = self.get_display_position(display, align=(1,1))

                    adjacent_left, adjacent_top = self.get_display_position(adjacent, (0,0))
                    adjacent_right, adjacent_bottom = self.get_display_position(adjacent, (1,1))

                    # getting correct logical resolution depends on whether to use native or saved values
                    adjacent_logical_resolution_x, adjacent_logical_resolution_y = self.get_display_logical_resolution(adjacent, display_scales[adjacent], saved=(self.saved and not revert))
                    # Calculate new display position based on adjacent.
                    if direction == 'left':
                        new_current_display_left = offset_x - logical_resolution_x
                        new_current_display_top = self.align_display_with_adjacent_x(display_top, display_bottom, adjacent_top, adjacent_bottom, adjacent_logical_resolution_y, offset_y, logical_resolution_y)
                    elif direction == 'right':
                        new_current_display_left = offset_x + adjacent_logical_resolution_x
                        new_current_display_top = self.align_display_with_adjacent_x(display_top, display_bottom, adjacent_top, adjacent_bottom, adjacent_logical_resolution_y, offset_y, logical_resolution_y)
                    elif direction == 'top':
                        new_current_display_left = self.align_display_with_adjacent_x(display_left, display_right, adjacent_left, adjacent_right, adjacent_logical_resolution_x, offset_x, logical_resolution_x)
                        new_current_display_top = offset_y - logical_resolution_y
                    elif direction == 'bottom':
                        new_current_display_left = self.align_display_with_adjacent_x(display_left, display_right, adjacent_left, adjacent_right, adjacent_logical_resolution_x, offset_x, logical_resolution_x)
                        new_current_display_top = offset_y + adjacent_logical_resolution_y

                    if new_current_display_left < max_negative_offset_x:
                        max_negative_offset_x = new_current_display_left
                    if new_current_display_top < max_negative_offset_y:
                        max_negative_offset_y = new_current_display_top

                    del adjacent_displays[0]

                    # Now add display to list
                    display_positions[display] = (new_current_display_left, new_current_display_top)

        # If we didn't set an offset coordinate (e.g. when there is only one display)
        # then set offset to zero, to prevent setting bad display modes.
        if max_negative_offset_x == 32769:
            max_negative_offset_x = 0
        if max_negative_offset_y == 32769:
            max_negative_offset_y = 0

        # Offset display positions so all coordinates are non-negative
        for display in display_positions:
            display_positions[display] = (display_positions[display][0] - max_negative_offset_x, display_positions[display][1] - max_negative_offset_y)

        return display_positions


    def calculate_layout(self, revert=False):
        position_lookup_entries_x = dict()
        position_lookup_entries_y = dict()
        cur_position_entries_x = list()
        cur_position_entries_y = list()

        display_positions = dict()

        for display in self.displays:
            position_x, position_y = self.get_display_position(display)
            if position_x != -1 and position_y != -1:
                if position_x in position_lookup_entries_x:
                    cur_position_entries_x = position_lookup_entries_x[position_x]
                    cur_position_entries_x.append(display)
                else:
                    cur_position_entries_x = [display]
                if position_y in position_lookup_entries_y:
                    cur_position_entries_y = position_lookup_entries_y[position_y]
                    cur_position_entries_y.append(display)
                else:
                    cur_position_entries_y = [display]
                position_lookup_entries_x[position_x] = cur_position_entries_x
                position_lookup_entries_y[position_y] = cur_position_entries_y

        prev_right = 0
        prev_top = 0
        prev_bottom = 0
        # Layout displays without overlap starting with top-left-most display,
        # working to the left and down.  We need to make sure not to exceed
        # the maximum X screen size.  Intel graphics are limited to 8192x8192,
        # so a hidpi internal display and two external displays can exceed this
        # limit.
        for y in sorted(position_lookup_entries_y):
            for x in sorted(position_lookup_entries_x):
                for display_name in position_lookup_entries_x[x]:
                    if display_name in position_lookup_entries_y[y]:
                        display = None
                        for d in self.displays:
                            if d == display_name:
                                display = d

                        dpi = self.get_display_dpi(display)
                        if dpi is None:
                            scale_factor = 1
                        elif dpi > 170 and revert == False:
                            scale_factor = 2
                        else:
                            scale_factor = 1

                        if self.scale_mode == 'hidpi' and revert == False:
                            scale_factor = scale_factor / 2

                        logical_resolution_x, logical_resolution_y = self.get_display_logical_resolution(display, scale_factor)

                        display_left = prev_right
                        display_top = prev_top
                        if display_left + logical_resolution_x > self.screen_maximum.x:
                            display_left = 0
                            display_top = prev_bottom
                            if display_top + logical_resolution_y > self.screen_maximum.y:
                                log.info("Too many displays to position within X screen boundaries.")
                                pass

                        display_positions[display_name] = (display_left, display_top)

                        prev_right = display_left + logical_resolution_x
                        prev_top = display_top
                        if prev_bottom < display_top + logical_resolution_y:
                            prev_bottom = display_top + logical_resolution_y

        # Work around Mutter(?) bug where the X Screen (not output) resolution is set too small.
        if self.get_gpu_vendor() == 'intel':
            self.calculated_display_size = (prev_right, prev_bottom)

        return display_positions

    def get_internal_lid_state(self):
        try:
            lids_path = '/proc/acpi/button/lid/'
            lid_file_path = os.path.join(lids_path, 'LID0', 'state')
            if not os.path.isfile(lid_file_path):
                # Default LID0 not found. Look for another subdirectory with a state file.
                lid_dirs = [d for d in os.listdir(lids_path) if os.path.isfile(os.path.join(lids_path, d, 'state'))]
                if len(lid_dirs) < 1:
                    return True # No lids found: System may not be a laptop.
                else:
                    lid_file_path = os.path.join(lids_path, lid_dirs[0], 'state')
            lid_file = open(lid_file_path, 'r')
            if 'open' in lid_file.read():
                return True
            else:
                return False
        except:
            return True

    def panel_activation_override(self, display_name):
        try:
            if 'eDP' in display_name or self.displays[display_name]['connector_type'] == 'Panel':
                if not self.get_internal_lid_state():
                    #Don't activate display
                    return True
        except:
            return False
        return False

    def get_nvidia_settings_options(self, display_name, viewportin, viewportout):
        cmd = [ 'nvidia-settings', '-q', 'CurrentMetaMode' ]
        output = subprocess.check_output(cmd).decode("utf-8")
        deprettified_currentmetamode = re.sub(r'(\n )|(\n\n)', r'', output)

        dpys = subprocess.check_output(['nvidia-settings', '-q', 'dpys'])
        reg = re.compile(r'\[([0-9])\] (?:.*?)\[dpy\:([.0-9])\] \((.*?)\)')
        tokens = reg.findall(str(dpys))
        dpy_mapping = {}
        for entry in tokens:
            idx, dpy_num, connector_name = entry
            dpy_mapping["DPY-" + dpy_num] = connector_name

        reg = re.compile(r'((?:DPY\-\d).*?})')
        reg = re.compile(r'(DPY\-\d).*?(\{.*?\})')
        display_attribute_pairs = reg.findall(deprettified_currentmetamode)
        attribute_mapping = {}
        for pair in display_attribute_pairs:
            connector_name = dpy_mapping[pair[0]]
            if connector_name == display_name:
                attributes = pair[1]
                attributes = re.sub(r'ViewPortIn\=\d*x\d*(, )?', r'', attributes)
                attributes = re.sub(r'ViewPortOut\=\d*x\d*\+\d\+\d(, )?', r'', attributes)
                attributes = re.sub(r'ForceCompositionPipeline=\w*(, )?', r'', attributes)
                attributes = re.sub(r'{', r'{ViewPortOut=' + viewportout + ', ', attributes)
                attributes = re.sub(r'{', r'{ViewPortIn=' + viewportin + ', ', attributes)
                attributes = re.sub(r'}', r'ForceCompositionPipeline=On}, ', attributes)
                attribute_mapping[connector_name] = attributes

        # Create new attributes if we are activating a currently inactive display.
        # This fixes issues when plugging multiple displays in at the same time.
        if display_name not in attribute_mapping:
            attributes = '{ViewPortIn=' + viewportin + ', ' + \
                        'ViewPortOut=' + viewportout + ', ' + \
                        'ForceCompositionPipeline=On}, '
            attribute_mapping[display_name] = attributes

        return attribute_mapping[display_name]


    def set_display_scaling_nvidia_settings(self, display_name, layout, scale_mode):
        #DP-0: nvidia-auto-select @3840x2160 +0+0 {ViewPortIn=3840x2160, ViewPortOut=3840x2160+0+0, ForceCompositionPipeline=On}
        #DISPLAY_NAME
        #nvidia-auto-select
        #@panning res
        #+pan_x+pan_y
        #{ViewPortIn=,ViewPortOut=
        #other attributes from matched display
        #ForceCompositionPipeline=On}

        # Don't generate config for laptop display if the lid is closed.
        if self.panel_activation_override(display_name):
            return ''

        dpi = self.get_display_dpi(display_name)
        if dpi is None:
            return ''
        display_str = display_name + ": nvidia-auto-select "

        mode = self.displays[display_name]['modes'][0]
        res_out_x = mode['width']
        res_out_y = mode['height']

        if scale_mode == 'lowdpi_prime':
            res_in_x = mode['width']
            res_in_y = mode['height']
        elif scale_mode == 'hidpi':
            if dpi > 170:
                res_in_x = mode['width']
                res_in_y = mode['height']
            else:
                res_in_x = 2 * mode['width']
                res_in_y = 2 * mode['height']
        else:
            if dpi > 170:
                res_in_x = round(mode['width'] / 2)
                res_in_y = round(mode['height'] / 2)
            else:
                res_in_x = mode['width']
                res_in_y = mode['height']

        if display_name in layout:
            pan_x, pan_y = layout[display_name]
        else:
            return ''
        panning_pos = "+" + str(pan_x) + "+" + str(pan_y)

        viewportin = str(res_in_x) + "x" + str(res_in_y) + " "
        viewportout = str(res_out_x) + "x" + str(res_out_y) + panning_pos

        display_str = display_str + "@" + str(res_in_x) + "x" + str(res_in_y) + " "
        display_str = display_str + "+" + str(pan_x) + "+" + str(pan_y) + " "
        display_str = display_str + self.get_nvidia_settings_options(display_name, viewportin, viewportout)
        return display_str

    def set_display_scaling_xrandr(self, display_name, layout, force_lowdpi=True):
        native_dpi = self.get_display_dpi(display_name)
        saved_dpi = self.get_display_dpi(display_name, saved=True)
        current_dpi = self.get_display_dpi(display_name, current=True)
        if current_dpi is None:
            current_dpi = 0
        dpi = None

        resources = self.xlib_window.xrandr_get_screen_resources()._data
        crtc = self.displays[display_name]['crtc']
        mode = None

        try:
            crtc_info = randr.get_crtc_info(self.xlib_display, crtc, resources['config_timestamp'])._data
        except:
            return ''

        # Get appropriate Mode and DPI
        if crtc != 0 and current_dpi <= 170 and force_lowdpi:
            # use current dpi and resolution
            try:
                crtc_info = randr.get_crtc_info(self.xlib_display, crtc, resources['config_timestamp'])._data
                mode = dict()
                mode['width'] = crtc_info['width']
                mode['height'] = crtc_info['height']
                dpi = current_dpi
            except:
                return ''
        if self.displays_xml:
            if saved_dpi <= 170 and force_lowdpi:
                #use saved resolution
                for log_mon in self.displays_xml['logical_monitors']:
                    if log_mon['monitor_spec']['connector'] == display_name:
                        mode = dict()
                        mode['width'] = int(log_mon['mode']['width'])
                        mode['height'] = int(log_mon['mode']['height'])
                        dpi = saved_dpi
            elif saved_dpi > 170 and force_lowdpi:
                # use half of max redolution
                try:
                    crtc_info = randr.get_crtc_info(self.xlib_display, crtc, resources['config_timestamp'])._data
                    mode = self.displays[display_name]['modes'][0]
                    dpi = native_dpi
                except:
                    return ''
                # later halve it
            else:
                try:
                    crtc_info = randr.get_crtc_info(self.xlib_display, crtc, resources['config_timestamp'])._data
                    mode = self.displays[display_name]['modes'][0]
                    dpi = native_dpi
                except:
                    return ''

        else:
            # use native resolution
            try:
                crtc_info = randr.get_crtc_info(self.xlib_display, crtc, resources['config_timestamp'])._data
                mode = self.displays[display_name]['modes'][0]
                dpi = native_dpi
            except:
                return ''


        if dpi is None:
            return ''


        if force_lowdpi == True and dpi > 170:
            x_res = round(mode['width'] / 2)
            y_res = round(mode['height'] / 2)
        else:
            x_res = mode['width']
            y_res = mode['height']

        if display_name in layout:
            pan_x, pan_y = layout[display_name]
        else:
            return ''

        #now find the mode we want
        new_mode = None
        for mode in self.displays[display_name]['modes']:
            if mode['width'] == x_res and mode['height'] == y_res:
                new_mode = mode
                break

        if self.panel_activation_override(display_name):
            return ''

        try:
            randr.set_crtc_config(self.xlib_display,crtc, int(time.time()), int(pan_x), int(pan_y), new_mode['id'], crtc_info['rotation'], crtc_info['outputs'])
        except:
            log.info("Could not set CRTC for " + str(display_name))

        return ''

    def set_display_scaling(self, display, layout, force=False, lowdpi_prime=False):
        if self.displays[display]['modes'] == []:
            return ''
        if self.get_gpu_vendor() == 'nvidia':
            if 'prime' in self.displays[display]:
                return self.set_display_scaling_xrandr(display, layout, force_lowdpi=force)
            else:
                # If a lowdpi prime display is present, don't pixel-double any lowdpi displays.
                if lowdpi_prime and self.scale_mode == 'hidpi':
                    mode = 'lowdpi_prime'
                else:
                    mode = self.scale_mode
                return self.set_display_scaling_nvidia_settings(display, layout, scale_mode=mode)
        elif self.get_gpu_vendor() == 'intel':
            return self.set_display_scaling_xrandr(display, layout, force_lowdpi=force)


    def has_prime_displays(self):
        found_hidpi = False
        found_lowdpi = False
        for display in self.displays:
            if self.displays[display]['connected'] == True and 'prime' in self.displays[display]:
                dpi = self.get_display_dpi(display)
                if dpi == None:
                    pass
                elif self.panel_activation_override(display):
                    pass
                elif dpi > 170:
                    found_hidpi = True
                elif 'prime' in self.displays[display]:
                    found_lowdpi = True
        return found_lowdpi, found_hidpi

    def has_mixed_hi_low_dpi_displays(self):
        found_hidpi = False
        found_lowdpi = False
        has_mixed_dpi = False
        for display in self.displays:
            if self.displays[display]['connected'] == True:
                dpi = self.get_display_dpi(display)
                if dpi == None:
                    pass
                elif self.panel_activation_override(display):
                    pass
                elif dpi > 170:
                    found_hidpi = True
                else:
                    found_lowdpi = True

        if found_hidpi == True and found_lowdpi == True:
            has_mixed_dpi = True

        return has_mixed_dpi, found_hidpi, found_lowdpi

    def set_scaled_display_modes(self, notification=True):
        # Don't set resolutions at all if disabled to prevent issues.
        if self.settings.get_boolean('enable') == False:
            return

        has_mixed_dpi, has_hidpi, has_lowdpi = self.has_mixed_hi_low_dpi_displays()
        has_lowdpi_prime, has_hidpi_prime = self.has_prime_displays()

        if has_hidpi_prime and has_lowdpi and self.scale_mode == 'hidpi':
            self.workaround_show_prime_set_primary_dialog()

        self.displays_xml = self.get_displays_xml()
        layout = self.calculate_layout2(revert=self.unforce)

        # INTEL: match display scales unless user selects 'native resolution'
        if not self.unforce:
            force = has_hidpi
        else:
            force = False

        # For each connected display, configure display modes.
        cmd = ''
        off_displays = []
        if self.get_gpu_vendor() == 'nvidia' and self.displays_xml is not None:
            for d in self.displays_xml['disabled']:
                if 'monitor_spec' in d:
                    off_displays.append(d['monitor_spec']['connector'])
        for display in self.displays:
            if self.displays[display]['connected'] == True:
                if 'prime' in self.displays[display]:
                    if self.scale_mode == 'hidpi':
                        force = False
        for display in self.displays:
            if self.displays[display]['connected'] == True:
                # INTEL: set the display crtc
                # NVIDIA: just get display parameters for nvidia-settings line
                if self.displays[display]['crtc'] == 0:
                    off_displays.append(display)
                elif 'prime' in self.displays[display]:
                    self.set_display_scaling(display, layout, force=force)
                else:
                    cmd = cmd + self.set_display_scaling(display, layout, force=force, lowdpi_prime=has_lowdpi_prime)
        # NVIDIA: got parameters for nvidia-settings - actually set display modes
        if self.get_gpu_vendor() == 'nvidia':
            if has_hidpi:
                # First set scale mode manually since Mutter can't see the effective display resolution.
                # Step 1) Let's try setting scale.  If this works, we can skip the later steps (less flickering).
                # Step 2) That didn't work.  We'll need to set everything up at the native resolution for Mutter
                #         to accept the display configuration.  Calculate a layout and nvidia-settings cmd at
                #         native resolution and set it momentarily.
                # Step 3) Try setting the scale with displays at native resolution.  This should almost always work.
                if self.scale_mode == 'lowdpi':
                    try:
                        dbusutil.set_scale(1)
                    except:
                        # Need to setup displays at native resolution before setting scale.
                        layout_native = self.calculate_layout2(revert=True)
                        cmd_native = ''
                        for display in self.displays:
                            if self.displays[display]['connected'] == True:
                                cmd_native = cmd_native + self.set_display_scaling(display, layout_native, force=force)
                        subprocess.call('nvidia-settings --assign CurrentMetaMode="' + cmd_native + '"', shell=True)
                        try:
                            dbusutil.set_scale(1)
                        except:
                            log.info("Could not set Mutter scale mode lowdpi")
                elif dbusutil.get_scale() < 2.0:
                    #Need to set a display mode Mutter is happy with before setting scale
                    try:
                        dbusutil.set_scale(2)
                    except:
                        # Need to setup displays at native resolution before setting scale.
                        layout_native = self.calculate_layout2(revert=True)
                        cmd_native = ''
                        for display in self.displays:
                            if self.displays[display]['connected'] == True:
                                cmd_native = cmd_native + self.set_display_scaling(display, layout_native, force=force)
                        subprocess.call('nvidia-settings --assign CurrentMetaMode="' + cmd_native + '"', shell=True)
                        try:
                            dbusutil.set_scale(2)
                        except:
                            log.info("Could not set Mutter scale mode hidpi")
                # Let things settle down.
                time.sleep(0.1)
                for display in self.displays:
                    if self.displays[display]['connected'] == True and 'prime' in self.displays[display]:
                        self.set_display_scaling(display, layout, force=force)
                # Now call nvidia settings with the metamodes we calculated in set_display_scaling()
                if cmd != "":
                    subprocess.call('nvidia-settings --assign CurrentMetaMode="' + cmd + '"', shell=True)
                if self.scale_mode == 'lowdpi' and dbusutil.get_scale() > 1.0:
                    try:
                        dbusutil.set_scale(1)
                    except:
                        log.info("Could not set Mutter scale mode lowdpi")
            # We don't have any hidpi displays (maybe one was disconnected).
            # No need to call nvidia-settings, but the scale could still be 2x.
            # Set scale back to 1x, so the user isn't stuck with everything unusably large.
            elif has_lowdpi and dbusutil.get_scale() > 1:
                try:
                    dbusutil.set_scale(1)
                except:
                    log.info("Could not set Mutter scale mode only lowdpi")
        # Special cases on INTEL.  Specifically 'native resolution' mode has some quirks.
        elif self.get_gpu_vendor() == 'intel' and force == False:
            try:
                current_scale = dbusutil.get_scale()
            except:
                current_scale = 2
            if current_scale < 2:
                for display in self.displays:
                    if self.displays[display]['connected']:
                        # Under some circumstances, Mutter may not set the scaling.
                        # In 'native resolution' ('unforced') mode, we must set scaling if:
                        # a) - the internal panel is hidpi
                        # b) - there is an external panel between 170 and 192 dpi (mutter already sets scale if above 192)
                        #    - and no lowdpi monitors are present (1x scaling is better if there are)
                        if self.panel_activation_override(display):
                            pass
                        elif ('eDP' in display or self.displays[display]['connector_type'] == 'Panel'):
                            if self.get_display_dpi(display) > 192:
                                try:
                                    dbusutil.set_scale(2)
                                except:
                                    log.info("Could not set Mutter scale internal hidpi")
                        elif self.get_display_dpi(display) > 170 and not has_lowdpi: # same thing for external displays
                            try:
                                dbusutil.set_scale(2)
                            except:
                                log.info("Could not set Mutter scale external hidpi")

        # Work around Mutter(?) bug where the X Screen (not output) resolution is set too small.
        # Because of this, sometimes some displays may be rendered partially or completely black.
        # Calling 'xrandr --auto' causes the correct screen size to be set without other notable changes.
        if self.get_gpu_vendor() == 'intel':
            size_x, size_y = self.calculated_display_size
            size_str = 'current ' + str(size_x) + ' x ' + str(size_y)
            xrandr_output = subprocess.check_output(['xrandr']).decode('utf-8')
            if size_str not in xrandr_output:
                if self.get_internal_lid_state():
                    subprocess.call('xrandr --auto', shell=True)
                    # Force Scale to 2x (unless we have only low-dpi + almost-hidpi)
                    if force == False and dbusutil.get_scale() < 2:
                        workaround_set_hidpi = False
                        if has_lowdpi == False:
                            workaround_set_hidpi = True
                        for display in self.displays:
                            if self.displays[display]['connected']:
                                if self.get_display_dpi(display) > 192:
                                    workaround_set_hidpi = True
                        if workaround_set_hidpi:
                            try:
                                dbusutil.set_scale(2)
                            except:
                                log.info("Could not set Mutter scale for workaround.")
                else:
                    subprocess.call('xrandr --output eDP-1 --off', shell=True)

            # Setting the other displays' modes with xlib will also activate previously disabled displays.
            # We need to turn them off manually.  Using xrandr since I haven't found a better method.
            for off_display in off_displays:
                subprocess.call(['xrandr', '--output', off_display, '--off'])

        # Displays are all setup - Notify the user!
        self.prev_display_types = (has_mixed_dpi, has_hidpi, has_lowdpi)
        self.notification_send_signal()

    def update(self, e):
        time.sleep(.1)
        if self.update_display_connections():
            has_mixed_dpi, has_hidpi, has_lowdpi = self.has_mixed_hi_low_dpi_displays()
            # NVIDIA: always remember user's selected mode
            # INTEL: only remember while in same display combination type
            #        When switching from hidpi-only to mixed-dpi or vice versa, set the appropriate default mode
            #        remember setting if eg, a user plugs a hidpi display into a hidpi laptop
            #        or if another display is plugged into an already mixed-dpi config.
            if self.get_gpu_vendor() == 'nvidia':
                pass
            elif not has_lowdpi and self.prev_display_types[2]:
                self.unforce = True
                self.settings.set_string('mode', 'hidpi')
            elif has_mixed_dpi and not self.prev_display_types[0]:
                self.unforce = False
                self.settings.set_string('mode', 'lodpi')

            # Work around bug where display event triggers update with bad data, destroying layout
            if self.get_gpu_vendor() == 'nvidia':
                time.sleep(0.1)
                self.update_display_connections()

            if self.get_gpu_vendor() == 'nvidia':
                if self.workaround_prime_detect_lowdpi_primary():
                    self.scale_mode = 'lowdpi'
                    self.settings.set_string('mode', 'lodpi')

            if self.settings.get_boolean('enable') == False:
                return False

            # Don't override user configuration when only lodpi displays are connected.
            # This appears to be safe for now.
            if not has_hidpi:
                return False

            self.set_scaled_display_modes()
        return False

    def run(self):
        thread = threading.Thread(target = self.notification_register_dbus, args=(None, self.unforce), daemon=True)
        thread.start()

        thread = threading.Thread(target = self.acpid_listen)
        thread.start()

        #fix cassidy bug
        self.update_display_connections()
        # First set appropriate initial display configuration
        self.prev_display_types = self.has_mixed_hi_low_dpi_displays()
        if self.get_gpu_vendor() == 'nvidia':
            if self.workaround_prime_detect_lowdpi_primary():
                self.scale_mode = 'lowdpi'
                self.settings.set_string('mode', 'lodpi')
            else:
                has_mixed_dpi, has_hidpi, has_lowdpi = self.has_mixed_hi_low_dpi_displays()
                if has_hidpi:
                    self.set_scaled_display_modes()
        elif not self.prev_display_types[2]:
            self.unforce = True
            self.settings.set_string('mode', 'hidpi')
            self.set_scaled_display_modes()
        elif self.prev_display_types[0]:
            self.unforce = False
            self.settings.set_string('mode', 'lodpi')
            self.set_scaled_display_modes()

        # calling update fixes overlap bug on first mode set.
        if self.get_gpu_vendor() == 'intel':
            self.update(None)

        running = True
        prev_timestamp = 0
        #mapping_notify_sequence = 0

        # Disabling displays is a bit precarious on NVIDIA right now.
        # We want the user to be able to turn displays off,  but doing so is only safe in lowdpi mode.
        # When in connecting an external lowdpi monitor in HiDPI mode, Mutter turns it off.
        # 1) Poll instead of relying on events.
        #    a) Autoset only when a monitor has been physically connected or disconnected.
        #    b) Ignore manual config changes.  Let gnome-control-center and the projector toggle do their thing.
        # 2) Switch to lowdpi when we detect a lowdpi external monitor via polling
        # 3) Turn on all displays when setting, except those disabled in monitors.xml
        while(running):
            # Get subscribed xlib RANDR events. Blocks until next event is received.
            ex = 0
            try:
                e = self.xlib_display.next_event()
            except:
                time.sleep(0.1)
                ex = 1
            if ex == 0:
                if e.type == self.xlib_display.extension_event.ScreenChangeNotify:
                    pass
                elif e.type == 34:
                    # Received MappingNotify event.
                    pass
                    #if e.sequence_number > mapping_notify_sequence:
                    #   mapping_notify_sequence = e.sequence_number
                    #   self.update(e)
                else:
                    if (e.type + e.sub_code) == self.xlib_display.extension_event.OutputPropertyNotify:
                            # MUST set e to correct type from binary data.  Otherwise
                            # we'll have wrong contents, including nonsense timestamp.
                            e = randr.OutputPropertyNotify(display=self.xlib_display.display, binarydata = e._binary)
                # Multiple events are fired in quick succession, only act once.
                try:
                    new_timestamp = e.timestamp
                except:
                    new_timestamp = 0
                if new_timestamp > prev_timestamp:
                    prev_timestamp = new_timestamp
                    self.update(e)



def _run_hidpi_autoscaling(model):
    if model in MODEL_MODES:
        try:
            # Using subprocess.call() with shell=True because of way xrandr
            # --newmode needs its arguments.  A better method would be nice.
            cmd = 'xrandr' + ' --newmode ' + MODEL_MODES[model]
            subprocess.call(cmd, shell=True)
        except:
            log.info('Failed to create new xrandr mode. It may exist already.')
        try:
            cmd = ['xrandr', '--addmode'] + ['eDP-1', '1600x900']
            print(cmd)
            ##SubProcess.check_output(cmd)
            #subprocess.call('xrandr --addmode eDP-1 1600x900', shell=True)
        except:
            log.warning("Failed to add xrandr mode to display.")

    hidpi = HiDPIAutoscaling(model)
    hidpi.run()

    return hidpi

def run_hidpi_autoscaling(model):
    try:
        return _run_hidpi_autoscaling(model)
    except Exception:
        log.exception('Error calling _run_hidpi_autoscaling(%r):', model)
