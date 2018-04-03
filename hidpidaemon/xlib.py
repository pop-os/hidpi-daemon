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
Patches for Xlib bindings.  The version included in debian-based distros (0.14) was several years out of date since python-xlib moved from sourceforge to github.  Not needed for releases that include 0.20.
"""

from Xlib import X
from Xlib.ext import randr
from Xlib.protocol import rq

extname = 'RANDR'

class _GetOutputInfo(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(9),
        rq.RequestLength(),
        rq.Card32('output'),
        rq.Card32('config_timestamp'),
        )
    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Card8('status'),
        rq.Card16('sequence_number'),
        rq.ReplyLength(),
        rq.Card32('timestamp'),
        rq.Card32('crtc'),
        rq.Card32('mm_width'),
        rq.Card32('mm_height'),
        rq.Card8('connection'),
        rq.Card8('subpixel_order'),
        rq.LengthOf('crtcs', 2),
        rq.LengthOf('modes', 2),
        rq.Card16('num_preferred'),
        rq.LengthOf('clones', 2),
        rq.LengthOf('name', 2),
        rq.List('crtcs', rq.Card32Obj),
        rq.List('modes', rq.Card32Obj),
        rq.List('clones', rq.Card32Obj),
        rq.String8('name'),
)

def _get_output_info(d, output, config_timestamp):
    return _GetOutputInfo(
        display=d.display,
        opcode=d.display.get_extension_major(extname),
        output=output,
        config_timestamp=config_timestamp,
)


class _GetCrtcInfo(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(20),
        rq.RequestLength(),
        rq.Card32('crtc'),
        rq.Card32('config_timestamp'),
        )
    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Card8('status'),
        rq.Card16('sequence_number'),
        rq.ReplyLength(),
        rq.Card32('timestamp'),
        rq.Int16('x'),
        rq.Int16('y'),
        rq.Card16('width'),
        rq.Card16('height'),
        rq.Card32('mode'),
        rq.Card16('rotation'),
        rq.Card16('possible_rotations'),
        rq.LengthOf('outputs', 2),
        rq.LengthOf('possible_outputs', 2),
        rq.List('outputs', rq.Card32Obj),
        rq.List('possible_outputs', rq.Card32Obj),
        )

def _get_crtc_info(d, crtc, config_timestamp):
    return _GetCrtcInfo (
        display=d.display,
        opcode=d.display.get_extension_major(extname),
        crtc=crtc,
        config_timestamp=config_timestamp,
)


class _CreateMode(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(16),
        rq.RequestLength(),
        rq.Window('window'),
        rq.Object('mode', randr.RandR_ModeInfo),
        rq.String8('name'),
        )
    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Pad(1),
        rq.Card16('sequence_number'),
        rq.ReplyLength(),
        rq.Card32('mode'),
        rq.Pad(20),
        )

def _create_mode(w, mode, name):
    return _CreateMode (
        display=w.display,
        opcode=w.display.get_extension_major(extname),
        window=w,
        mode=mode,
        name=name,
)


class _AddOutputMode(rq.Request):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(18),
        rq.RequestLength(),
        rq.Card32('output'),
        rq.Card32('mode'),
        )

def _add_output_mode(d, output, mode):
    return _AddOutputMode(
        display=d.display,
        opcode=d.display.get_extension_major(extname),
        output=output,
        mode=mode,
)


class _SetCrtcConfig(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(21),
        rq.RequestLength(),
        rq.Card32('crtc'),
        rq.Card32('timestamp'),
        rq.Card32('config_timestamp'),
        rq.Int16('x'),
        rq.Int16('y'),
        rq.Card32('mode'),
        rq.Card16('rotation'),
        rq.Pad(2),
        rq.List('outputs', rq.Card32Obj),
        )
    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Card8('status'),
        rq.Card16('sequence_number'),
        rq.ReplyLength(),
        rq.Card32('new_timestamp'),
        rq.Pad(20),
        )

def _set_crtc_config(d, crtc, config_timestamp, x, y, mode, rotation, outputs, timestamp=X.CurrentTime):
    return _SetCrtcConfig (
        display=d.display,
        opcode=d.display.get_extension_major(extname),
        crtc=crtc,
        config_timestamp=config_timestamp,
        x=x,
        y=y,
        mode=mode,
        rotation=rotation,
        outputs=outputs,
        timestamp=timestamp,
)

class _GetOutputProperty(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8('opcode'),
        rq.Opcode(15),
        rq.RequestLength(),
        rq.Card32('output'),
        rq.Card32('property'),
        rq.Card32('type'),
        rq.Card32('long_offset'),
        rq.Card32('long_length'),
        rq.Bool('delete'),
        rq.Bool('pending'),
        rq.Pad(2),
        )
    _reply = rq.Struct(
        rq.ReplyCode(),
        rq.Format('value', 1),
        rq.Card16('sequence_number'),
        rq.ReplyLength(),
        rq.Card32('property_type'),
        rq.Card32('bytes_after'),
        rq.LengthOf('value', 4),
        rq.Pad(12),
        rq.List('value', rq.Card8Obj),
        )

def _get_output_property(d, output, property, type, long_offset, long_length, delete=False, pending=False):
    print('get output property override')
    return _GetOutputProperty(
        display=d.display,
        opcode=d.display.get_extension_major(extname),
        output=output,
        property=property,
        type=type,
        long_offset=long_offset,
        long_length=long_length,
        delete=delete,
        pending=pending,
)
