#!/usr/bin/python3

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
Install `hidpi-daemon`.
"""

import sys
if sys.version_info < (3, 4):
    sys.exit('ERROR: `hidpi-daemon` requires Python 3.4 or newer')

import os
from os import path
import subprocess
from distutils.core import setup
from distutils.cmd import Command

import hidpidaemon
#from hidpidaemon.tests.run import run_tests

SCRIPTS = [
]

def run_pyflakes3():
    pyflakes3 = '/usr/bin/pyflakes3'
    if not os.access(pyflakes3, os.R_OK | os.X_OK):
        print('WARNING: cannot read and execute: {!r}'.format(pyflakes3))
        return
    tree = path.dirname(path.abspath(__file__))
    names = [
        'hidpidaemon',
        'setup.py',
    ] + SCRIPTS
    cmd = [pyflakes3] + [path.join(tree, name) for name in names]
    print('check_call:', cmd)
    subprocess.check_call(cmd)
    print('[pyflakes3 checks passed]')


class Test(Command):
    description = 'run unit tests and doc tests'

    user_options = [
    ]

    def initialize_options(self):
        pass

    def finalize_options(self):
        pass

    def run(self):
        run_pyflakes3()


setup(
    name='hidpidaemon',
    version=hidpidaemon.__version__,
    description='HiDPI daemon to manage HiDPI and LoDPI monitors on X',
    url='https://github.com/pop-os/hidpi-daemon',
    author='System76, Inc.',
    author_email='dev@system76.com',
    license='GPLv2+',
    cmdclass={'test': Test},
    packages=[
        'hidpidaemon',
    ],
    package_data={
    },
    data_files=[
    ],
)
