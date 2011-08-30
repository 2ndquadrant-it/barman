#
# barman - Backup and Recovery Manager for PostgreSQL
#
# Copyright (C) 2011  Devise.IT S.r.l. <info@2ndquadrant.it>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from barman.command_wrappers import Command

class Server(object):
    def __init__(self, config):
        self.config = config

    def check(self):
        cmd = Command("%s -o BatchMode=yes -o StrictHostKeyChecking=no true" % (self.config.ssh_command), shell=True)
        ret = cmd()
        if ret == 0:
            yield "%s: OK" % (self.config.name)
        else:
            yield "%s: FAILED (return code: %s)" % (self.config.name, ret)
