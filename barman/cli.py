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

from argh import ArghParser, alias, arg
from barman.backup import Backup
from barman.lockfile import lockfile
from barman.server import Server
import barman.config
import logging
import os
import sys

_logger = logging.getLogger(__name__)

@alias('list-server')
@arg('--minimal', help='machine readable output', action='store_true')
def list_server(args):
    "list available servers, with useful information"
    servers = barman.__config__.servers()
    for server in servers:
        if server.description and not args.minimal:
            yield "%s - %s" % (server.name, server.description)
        else:
            yield server.name

def cron(args):
    "run maintenance tasks"
    filename = os.path.join(barman.__config__.barman_home, '.cron.lock')
    with lockfile(filename) as locked:
        if not locked:
            yield "ERROR: Another cron is running"
            raise SystemExit, 1
        else:
            servers = [ Server(conf) for conf in barman.__config__.servers()]
            for server in servers:
                for lines in server.cron(verbose=True):
                    yield lines

@arg('server_name', help='specifies the server name for the command')
def backup(args):
    'perform a full backup for the given server'
    server = get_server(args)
    if server == None:
        yield "Unknown server '%s'" % (args.server_name)
        return
    for line in server.backup():
        yield line

@alias('list-backup')
@arg('server_name', help='specifies the server name for the command')
@arg('--minimal', help='machine readable output', action='store_true')
def list_backup(args):
    'list available backups for the given server'
    server = get_server(args)
    if server == None:
        yield "Unknown server '%s'" % (args.server_name)
        return
    for line in server.list_backups():
        yield line

@arg('server_name', help='specifies the server name for the command')
def status(args):
    'shows live information and status of the PostgreSQL server'
    yield "TODO" # TODO: implement this

@arg('server_name', help='specifies the server name for the command')
@arg('--target-tli', help='target timeline', type=int)
@arg('--target-time', help='target time')
@arg('--target-xid', help='target xid')
@arg('--exclusive', help='set target xid to be non inclusive', action="store_true")
@arg('--tablespace', help='tablespace relocation rule', metavar='NAME:LOCATION', action='append')
@arg('backup_id', help='specifies the backup ID to recover')
@arg('destination_directory', help="""
the directory where the new server is created.
If prefixed with "username@server:" it refers to a remote server
with the same syntax accepted by rsync""")
def recover(args):
    'recover a server at a given time or xid'
    server = get_server(args)
    if server == None:
        yield "Unknown server '%s'" % (args.server_name)
        return
    tablespaces = {}
    if args.tablespace:
        for rule in args.tablespace:
            try:
                tablespaces.update([rule.split(':', 1)])
            except:
                yield "Invalid tablespace relocation rule %s" % rule
    for line in server.recover(args.backup_id,
                               args.destination_directory,
                               tablespaces=tablespaces,
                               target_tli=args.target_tli,
                               target_time=args.target_time,
                               target_xid=args.target_xid,
                               exclusive=args.exclusive):
        yield line

@alias('show-server')
@arg('server_name', nargs='+', help="specifies the server names to show ('all' will show all available servers)")
def show_server(args):
    'show all configuration parameters for the specified servers'
    servers = get_server_list(args)

    for name, server in servers.items():
        if server == None:
            yield "Unknown server '%s'" % (name)
            continue
        for line in server.show():
            yield line
        yield ''

@arg('server_name', nargs='+', help="specifies the server names to check ('all' will check all available servers)")
def check(args):
    'check if connection settings work properly for the specified servers'
    servers = get_server_list(args)

    for name, server in servers.items():
        if server == None:
            yield "Unknown server '%s'" % (name)
            continue
        for line in server.check():
            yield line
        yield ''

@alias('show-backup')
@arg('server_name', help='specifies the server name for the command')
@arg('backup_id', help='specifies the backup ID')
def show_backup(args):
    'show a single backup information'
    server = get_server(args)
    if server == None:
        yield "Unknown server '%s'" % (args.server_name)
        return
    # Retrieves the backup info file
    backup_info_file = server.get_backup_info_file(args.backup_id)
    backup = Backup(server, backup_info_file)
    for line in backup.show():
        yield line

@arg('server_name', help='specifies the server name for the command')
@arg('backup_id', help='specifies the backup ID')
def delete(args):
    'delete a backup'
    server = get_server(args)
    if server == None:
        yield "Unknown server '%s'" % (args.server_name)
        return
    # Retrieves the backup info file
    backup_info_file = server.get_backup_info_file(args.backup_id)
    backup = Backup(server, backup_info_file)
    for line in server.delete_backup(backup):
        yield line

class stream_wrapper(object):
    def __init__(self, stream):
        self.stream = stream
    def set_stream(self, stream):
        self.stream = stream
    def __getattr__(self, attr):
        return getattr(self.stream, attr)

_output_stream = stream_wrapper(sys.stdout)

def global_config(args):
    if hasattr(args, 'config'):
        filename = args.config
    else:
        try:
            filename = os.environ['BARMAN_CONFIG_FILE']
        except KeyError:
            filename = None
    barman.__config__ = barman.config.Config(filename)
    _logger.debug('Initialized BaRMan version %s (config: %s)',
                 barman.__version__, barman.__config__.config_file)
    if hasattr(args, 'quiet') and args.quiet:
        _logger.debug("Replacing output stream ")
        global _output_stream
        _output_stream.set_stream(open(os.devnull, 'w'))

def get_server(args):
    config = barman.__config__.get_server(args.server_name)
    if not config:
            return None
    return Server(config)

def get_server_list(args):
    if args.server_name[0] == 'all':
        return dict((conf.name, Server(conf)) for conf in barman.__config__.servers())
    else:
        server_dict = {}
        for server in args.server_name:
            conf = barman.__config__.get_server(server)
            if conf == None:
                server_dict[server] = None
            else:
                server_dict[server] = Server(conf)
        return server_dict

def main():
    p = ArghParser()
    p.add_argument('-v', '--version', action='version', version=barman.__version__)
    p.add_argument('-c', '--config', help='uses a configuration file (defaults: $HOME/.barman.conf, /etc/barman.conf)')
    p.add_argument('-q', '--quiet', help='be quiet', action='store_true')
    p.add_commands(
        [
            cron,
            list_server,
            show_server,
            status,
            check,
            backup,
            list_backup,
            show_backup,
            recover,
            delete,
        ]
    )
    p.dispatch(pre_call=global_config, output_file=_output_stream)

if __name__ == '__main__':
    main()
