# Copyright (C) 2013-2016 2ndQuadrant Italia Srl
#
# This file is part of Barman.
#
# Barman is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Barman is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Barman.  If not, see <http://www.gnu.org/licenses/>.

import os

import mock
from mock import patch

from barman.copy_controller import RsyncCopyController
from testing_helpers import (build_backup_manager, build_real_server,
                             build_test_backup_info)


# noinspection PyMethodMayBeStatic
class TestRsyncCopyController(object):
    """
    This class tests the methods of the RsyncCopyController object
    """

    def test_rsync_backup_executor_init(self):
        """
        Test the construction of a RsyncCopyController
        """

        # Build the prerequisites
        backup_manager = build_backup_manager()
        server = backup_manager.server
        config = server.config
        executor = server.executor

        # Test
        assert RsyncCopyController(
            path=server.path,
            ssh_command=executor.ssh_command,
            ssh_options=executor.ssh_options,
            network_compression=config.network_compression,
            reuse_backup=None,
            safe_horizon=None)

    def test_reuse_args(self):
        """
        Simple test for the _reuse_args method

        The method is necessary for the execution of incremental backups,
        we need to test that the method build correctly the rsync option that
        enables the incremental backup
        """
        # Build the prerequisites
        backup_manager = build_backup_manager()
        server = backup_manager.server
        config = server.config
        executor = server.executor

        rcc = RsyncCopyController(
            path=server.path,
            ssh_command=executor.ssh_command,
            ssh_options=executor.ssh_options,
            network_compression=config.network_compression,
            reuse_backup=None,
            safe_horizon=None)

        reuse_dir = "some/dir"

        # Test for disabled incremental
        assert rcc._reuse_args(reuse_dir) == []

        # Test for link incremental
        rcc.reuse_backup = 'link'
        assert rcc._reuse_args(reuse_dir) == \
            ['--link-dest=some/dir']

        # Test for copy incremental
        rcc.reuse_backup = 'copy'
        assert rcc._reuse_args(reuse_dir) == \
            ['--copy-dest=some/dir']

    @patch('barman.copy_controller.Rsync')
    @patch('barman.copy_controller.RsyncCopyController._smart_copy')
    def test_full_copy(self, smart_copy_mock, rsync_mock, tmpdir):
        """
        Test the execution of a rsync copy

        :param rsync_mock: mock for the rsync command
        :param tmpdir: temporary dir
        """

        # Build the prerequisites
        server = build_real_server(global_conf={
            'barman_home': tmpdir.mkdir('home').strpath
        })
        config = server.config
        executor = server.backup_manager.executor

        rcc = RsyncCopyController(
            path=server.path,
            ssh_command=executor.ssh_command,
            ssh_options=executor.ssh_options,
            network_compression=config.network_compression,
            reuse_backup=None,
            safe_horizon=None)

        backup_info = build_test_backup_info(
            server=server,
            pgdata="/pg/data",
            config_file="/etc/postgresql.conf",
            hba_file="/pg/data/pg_hba.conf",
            ident_file="/pg/data/pg_ident.conf",
            begin_xlog="0/2000028",
            begin_wal="000000010000000000000002",
            begin_offset=28)
        backup_info.save()
        # This is to check that all the preparation is done correctly
        assert os.path.exists(backup_info.filename)

        # Silence the access to string properties
        rsync_mock.return_value.out = ''
        rsync_mock.return_value.err = ''

        rcc.add_directory(
            label='tbs1',
            src=':/fake/location/',
            dst=backup_info.get_data_directory(16387),
            reuse=None,
            bwlimit=None,
            item_class=rcc.TABLESPACE_CLASS),
        rcc.add_directory(
            label='tbs2',
            src=':/another/location/',
            dst=backup_info.get_data_directory(16405),
            reuse=None,
            bwlimit=None,
            item_class=rcc.TABLESPACE_CLASS),
        rcc.add_directory(
            label='pgdata',
            src=':/pg/data/',
            dst=backup_info.get_data_directory(),
            reuse=None,
            bwlimit=None,
            item_class=rcc.PGDATA_CLASS,
            exclude=['/pg_xlog/*',
                     '/pg_log/*',
                     '/recovery.conf',
                     '/postmaster.pid'],
            exclude_and_protect=['pg_tblspc/16387', 'pg_tblspc/16405']),
        rcc.add_file(
            label='pg_control',
            src=':/pg/data/global/pg_control',
            dst='%s/global/pg_control' % backup_info.get_data_directory(),
            item_class=rcc.PGCONTROL_CLASS),
        rcc.add_file(
            label='config_file',
            src=':/etc/postgresql.conf',
            dst=backup_info.get_data_directory(),
            item_class=rcc.CONFIG_CLASS,
            optional=False),
        rcc.copy(),

        assert rsync_mock.mock_calls == [
            mock.call(network_compression=False,
                      args=['-rLKpts',
                            '--delete-excluded',
                            '--inplace',
                            '--itemize-changes',
                            '--itemize-changes'],
                      bwlimit=None, ssh='ssh', path=None,
                      ssh_options=['-c', '"arcfour"', '-p', '22',
                                   'postgres@pg01.nowhere', '-o',
                                   'BatchMode=yes', '-o',
                                   'StrictHostKeyChecking=no'],
                      exclude=None, exclude_and_protect=None),
            mock.call(network_compression=False,
                      args=['-rLKpts',
                            '--delete-excluded',
                            '--inplace',
                            '--itemize-changes',
                            '--itemize-changes'],
                      bwlimit=None, ssh='ssh', path=None,
                      ssh_options=['-c', '"arcfour"', '-p', '22',
                                   'postgres@pg01.nowhere', '-o',
                                   'BatchMode=yes', '-o',
                                   'StrictHostKeyChecking=no'],
                      exclude=None, exclude_and_protect=None),
            mock.call(network_compression=False,
                      args=['-rLKpts',
                            '--delete-excluded',
                            '--inplace',
                            '--itemize-changes',
                            '--itemize-changes'],
                      bwlimit=None, ssh='ssh', path=None,
                      ssh_options=['-c', '"arcfour"', '-p', '22',
                                   'postgres@pg01.nowhere', '-o',
                                   'BatchMode=yes', '-o',
                                   'StrictHostKeyChecking=no'],
                      exclude=[
                          '/pg_xlog/*',
                          '/pg_log/*',
                          '/recovery.conf',
                          '/postmaster.pid'],
                      exclude_and_protect=[
                          'pg_tblspc/16387',
                          'pg_tblspc/16405']),
            mock.call(network_compression=False,
                      args=['-rLKpts',
                            '--delete-excluded',
                            '--inplace',
                            '--itemize-changes',
                            '--itemize-changes'],
                      bwlimit=None, ssh='ssh', path=None,
                      ssh_options=['-c', '"arcfour"', '-p', '22',
                                   'postgres@pg01.nowhere', '-o',
                                   'BatchMode=yes', '-o',
                                   'StrictHostKeyChecking=no'],
                      exclude=None, exclude_and_protect=None),
            mock.call()(
                ':/pg/data/global/pg_control',
                '%s/global/pg_control' % backup_info.get_data_directory()),
            mock.call(network_compression=False,
                      args=['-rLKpts',
                            '--delete-excluded',
                            '--inplace',
                            '--itemize-changes',
                            '--itemize-changes'],
                      bwlimit=None, ssh='ssh', path=None,
                      ssh_options=['-c', '"arcfour"', '-p', '22',
                                   'postgres@pg01.nowhere', '-o',
                                   'BatchMode=yes', '-o',
                                   'StrictHostKeyChecking=no'],
                      exclude=None, exclude_and_protect=None),
            mock.call()(
                ':/etc/postgresql.conf',
                backup_info.get_data_directory()),
        ]

        assert smart_copy_mock.mock_calls == [
            mock.call(mock.ANY,
                      ':/fake/location/',
                      backup_info.get_data_directory(16387),
                      None, None),
            mock.call(mock.ANY,
                      ':/another/location/',
                      backup_info.get_data_directory(16405),
                      None, None),
            mock.call(mock.ANY,
                      ':/pg/data/',
                      backup_info.get_data_directory(),
                      None, None),
        ]
