# HiDPI Daemon

This program is for maanging HiDPI and LoDPI monitors on X. This program is installed by default in Pop!\_OS and Ubuntu (if installed by System76 and can be added with [this article](https://support.system76.com/articles/system76-software)). 

## Building

### Installing depends

`sudo apt install debhelper dh-python python3-all pyflakes3 python3-gi python3-pydbus python3-xlib gir1.2-notify-0.7`

### Build without signing

`sudo dpkg-buildpackage -us uc`

## Removing

This program can be removed with this command:

`sudo apt remove hidpi-daemon`

## Making changes

1. Checkout new branch
2. Push new branch
3. Bump the version (`./bump-version.py`)
4. Make changes
5. Make pull request
6. Get PR approved and merged
7. Make a release from master branch (`./make-release.py`)

## License

This software is made available under the terms of the GNU General Public
License; either version 2 of the License, or (at your option) any later
version. See [LICENSE](LICENSE) for details.

