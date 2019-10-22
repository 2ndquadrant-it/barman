# Copyright (C) 2018-2019 2ndQuadrant Limited
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

import datetime
import logging
import os
import re
import tarfile
from io import BytesIO

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

import barman
from barman.backup_executor import ConcurrentBackupStrategy
from barman.fs import path_allowed
from barman.infofile import BackupInfo
from barman.postgres import PostgreSQLConnection
from barman.postgres_plumbing import EXCLUDE_LIST, PGDATA_EXCLUDE_LIST

try:
    import argparse
except ImportError:
    raise SystemExit("Missing required python module: argparse")

try:
    # Python 3.x
    from urllib.parse import urlparse
except ImportError:
    # Python 2.x
    from urlparse import urlparse


DEFAULT_CHUNK_SIZE = 10 << 21

LOGGING_FORMAT = "%(asctime)s %(levelname)s %(message)s"

_find_space = re.compile(r'[\s]').search


def quote_conninfo(value):
    """
    Quote a connection info parameter

    :param str value:
    :rtype: str
    """
    if not value:
        return "''"
    if not _find_space(value):
        return value
    return "'%s'" % value.replace("\\", "\\\\").replace("'", "\\'")


def build_conninfo(config):
    """
    Build a DSN to connect to postgres using command-line arguments
    """
    conn_parts = []
    if config.host:
        conn_parts.append("host=%s" % quote_conninfo(config.host))
    if config.port:
        conn_parts.append("port=%s" % quote_conninfo(config.port))
    if config.user:
        conn_parts.append("user=%s" % quote_conninfo(config.user))
    return ' '.join(conn_parts)


def main(args=None):
    """
    The main script entry point

    :param list[str] args: the raw arguments list. When not provided
        it defaults to sys.args[1:]
    """
    config = parse_arguments(args)
    configure_logging(config)
    postgres = None
    try:
        conninfo = build_conninfo(config)
        postgres = PostgreSQLConnection(conninfo, config.immediate_checkpoint)

        cloud_interface = CloudInterface(
            destination_url=config.destination_url,
            encryption=config.encryption,
            profile_name=config.profile)

        uploader = S3BackupUploader(
            server_name=config.server_name,
            compression=config.compression,
            postgres=postgres,
            cloud_interface=cloud_interface)

        # If test is requested just test connectivity and exit
        # TODO: add postgresql connectivity test
        if config.test:
            if cloud_interface.test_connectivity():
                raise SystemExit(0)
            raise SystemExit(1)

        # TODO: Should the setup be optional?
        cloud_interface.setup_bucket()

        # Perform the backup
        uploader.backup()
    except Exception as exc:
        logging.exception("Barman cloud backup exception: %s", exc)
        raise SystemExit(1)
    finally:
        if postgres:
            postgres.close()


def parse_arguments(args=None):
    """
    Parse command line arguments

    :return: The options parsed
    """

    parser = argparse.ArgumentParser(
        description='This script can be used to perform a backup '
                    'of a local PostgreSQL instance and ship '
                    'the resulting tarball(s) to the Cloud. '
                    'Currently only AWS S3 is supported.',
        add_help=False
    )
    parser.add_argument(
        'destination_url',
        help='URL of the cloud destination, such as a bucket in AWS S3.'
             ' For example: `s3://bucket/path/to/folder`.'
    )
    parser.add_argument(
        'server_name',
        help='the name of the server as configured in Barman.'
    )
    parser.add_argument(
        '-V', '--version',
        action='version', version='%%(prog)s %s' % barman.__version__
    )
    parser.add_argument(
        '--help',
        action='help',
        help='show this help message and exit')
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        '-v', '--verbose',
        action='count',
        default=0,
        help='increase output verbosity (e.g., -vv is more than -v)')
    verbosity.add_argument(
        '-q', '--quiet',
        action='count',
        default=0,
        help='decrease output verbosity (e.g., -qq is less than -q)')
    parser.add_argument(
        '-P', '--profile',
        help='profile name (e.g. INI section in AWS credentials file)',
    )
    compression = parser.add_mutually_exclusive_group()
    compression.add_argument(
        "-z", "--gzip",
        help="gzip-compress the WAL while uploading to the cloud",
        action='store_const',
        const='gz',
        dest='compression',
    )
    compression.add_argument(
        "-j", "--bzip2",
        help="bzip2-compress the WAL while uploading to the cloud",
        action='store_const',
        const='bz2',
        dest='compression',
    )
    parser.add_argument(
        "-e", "--encryption",
        help="Enable server-side encryption for the transfer. "
             "Allowed values: 'AES256'|'aws:kms'.",
        choices=['AES256', 'aws:kms'],
    )
    parser.add_argument(
        "-t", "--test",
        help="Test cloud connectivity and exit",
        action="store_true",
        default=False
    )
    parser.add_argument(
        '-h', '--host',
        help='host or Unix socket for PostgreSQL connection '
             '(default: libpq settings)',
    )
    parser.add_argument(
        '-p', '--port',
        help='port for PostgreSQL connection (default: libpq settings)',
    )
    parser.add_argument(
        '-U', '--user',
        help='user name for PostgreSQL connection (default: libpq settings)',
    )

    parser.add_argument(
        '--immediate-checkpoint',
        help='forces the initial checkpoint to be done as quickly as possible',
        action='store_true')
    return parser.parse_args(args=args)


def configure_logging(config):
    """
    Get a nicer output from the Python logging package
    """
    verbosity = config.verbose - config.quiet
    log_level = max(logging.WARNING - verbosity * 10, logging.DEBUG)
    logging.basicConfig(format=LOGGING_FORMAT, level=log_level)


class S3BackupUploader(object):
    """
    S3 upload client
    """

    def __init__(self, server_name, postgres, cloud_interface,
                 compression=None):
        """
        Object responsible for handling interactions with S3

        :param str server_name: The name of the server as configured in Barman
        :param PostgreSQLConnection postgres: The PostgreSQL connection info
        :param CloudInterface cloud_interface: The interface to use to
          upload the backup
        :param str compression: Compression algorithm to use
        """

        self.compression = compression
        self.server_name = server_name
        self.postgres = postgres
        self.cloud_interface = cloud_interface

        # Stats
        self.copy_start_time = None
        self.copy_end_time = None

    def backup_copy(self, controller, backup_info):
        """
        Perform the actual copy of the backup uploading it to S3.

        First, it copies one tablespace at a time, then the PGDATA directory,
        and finally configuration files (if outside PGDATA).
        Bandwidth limitation, according to configuration, is applied in
        the process.
        This method is the core of base backup copy using Rsync+Ssh.

        :param S3UploadController controller: upload controller
        :param barman.infofile.BackupInfo backup_info: backup information
        """

        # Store the start time
        self.copy_start_time = datetime.datetime.now()

        # List of paths to be excluded by the PGDATA copy
        exclude = []

        # Process every tablespace
        if backup_info.tablespaces:
            for tablespace in backup_info.tablespaces:
                # If the tablespace location is inside the data directory,
                # exclude and protect it from being copied twice during
                # the data directory copy
                if tablespace.location.startswith(backup_info.pgdata):
                    exclude += [
                        tablespace.location[len(backup_info.pgdata):]]

                # Exclude and protect the tablespace from being copied again
                # during the data directory copy
                exclude += ["pg_tblspc/%s" % tablespace.oid]

                # Copy the tablespace directory.
                # NOTE: Barman should archive only the content of directory
                #    "PG_" + PG_MAJORVERSION + "_" + CATALOG_VERSION_NO
                # but CATALOG_VERSION_NO is not easy to retrieve, so we copy
                #    "PG_" + PG_MAJORVERSION + "_*"
                # It could select some spurious directory if a development or
                # a beta version have been used, but it's good enough for a
                # production system as it filters out other major versions.
                controller.upload_directory(
                    label=tablespace.name,
                    src=tablespace.location,
                    dst='%s' % tablespace.oid,
                    exclude=['/*'] + EXCLUDE_LIST,
                    include=['/PG_%s_*' %
                             self.postgres.server_major_version],
                )

        # Copy PGDATA directory
        controller.upload_directory(
            label='pgdata',
            src=backup_info.pgdata,
            dst='data',
            exclude=PGDATA_EXCLUDE_LIST + EXCLUDE_LIST + exclude
        )

        # At last copy pg_control
        controller.add_file(
            label='pg_control',
            src='%s/global/pg_control' % backup_info.pgdata,
            dst='data',
            path='global/pg_control'
        )

        # Copy configuration files (if not inside PGDATA)
        external_config_files = backup_info.get_external_config_files()
        included_config_files = []
        for config_file in external_config_files:
            # Add included files to a list, they will be handled later
            if config_file.file_type == 'include':
                included_config_files.append(config_file)
                continue

            # If the ident file is missing, it isn't an error condition
            # for PostgreSQL.
            # Barman is consistent with this behavior.
            optional = False
            if config_file.file_type == 'ident_file':
                optional = True

            # Create the actual copy jobs in the controller
            controller.add_file(
                label=config_file.file_type,
                src=config_file.path,
                dst='data',
                path=os.path.basename(config_file.path),
                optional=optional,
            )

        # Store the end time
        self.copy_end_time = datetime.datetime.now()

        # Store statistics about the copy
        backup_info.copy_stats = controller.statistics()

        # Check for any include directives in PostgreSQL configuration
        # Currently, include directives are not supported for files that
        # reside outside PGDATA. These files must be manually backed up.
        # Barman will emit a warning and list those files
        if any(included_config_files):
            msg = ("The usage of include directives is not supported "
                   "for files that reside outside PGDATA.\n"
                   "Please manually backup the following files:\n"
                   "\t%s\n" %
                   "\n\t".join(icf.path for icf in included_config_files))
            logging.warning(msg)

    def backup(self):
        """
        Upload a Backup  to S3
        """
        backup_info = BackupInfo(
            backup_id=datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
        key_prefix = os.path.join(
            self.cloud_interface.path,
            self.server_name,
            'base',
            backup_info.backup_id
        )
        controller = S3UploadController(
            self.cloud_interface, key_prefix, self.compression)
        strategy = ConcurrentBackupStrategy(self.postgres)
        logging.info("Starting backup %s", backup_info.backup_id)
        strategy.start_backup(backup_info)
        try:
            self.backup_copy(controller, backup_info)
            logging.info("Stopping backup %s", backup_info.backup_id)
            strategy.stop_backup(backup_info)
            pgdata_stat = os.stat(backup_info.pgdata)
            controller.add_fileobj(
                label='backup_label',
                fileobj=BytesIO(backup_info.backup_label.encode('UTF-8')),
                dst='data',
                path='backup_label',
                uid=pgdata_stat.st_uid,
                gid=pgdata_stat.st_gid,
            )
        finally:
            with BytesIO() as backup_info_file:
                backup_info.save(file_object=backup_info_file)
                backup_info_file.seek(0, os.SEEK_SET)
                controller.upload_fileobj(
                    label='backup_info',
                    fileobj=backup_info_file,
                    dst='backup.info'
                )
            controller.close()


class S3TarUploader(object):
    def __init__(self, cloud_interface, key,
                 compression=None, chunk_size=DEFAULT_CHUNK_SIZE):
        """
        A tar archive that resides on S3

        :param CloudInterface cloud_interface: cloud interface instance
        :param str key: path inside the bucket
        :param str compression: required compression
        :param int chunk_size: the upload chunk size
        """
        self.cloud_interface = cloud_interface
        self.key = key
        self.mpu = None
        self.chunk_size = chunk_size
        self.buffer = BytesIO()
        self.counter = 1
        self.parts = []
        tar_mode = 'w|%s' % (compression or '')
        self.tar = tarfile.open(fileobj=self,
                                mode=tar_mode,
                                format=tarfile.PAX_FORMAT)

    def write(self, buf):
        if self.buffer.tell() > self.chunk_size:
            self.flush()
        self.buffer.write(buf)

    def flush(self):
        if not self.mpu:
            self.mpu = self.cloud_interface.create_multipart_upload(self.key)
        self.buffer.seek(0, os.SEEK_SET)
        part = self.cloud_interface.upload_part(
            mpu=self.mpu,
            key=self.key,
            body=self.buffer,
            part_number=self.counter)
        self.parts.append(part)
        self.counter += 1
        self.buffer.seek(0, os.SEEK_SET)
        self.buffer.truncate()

    def close(self):
        if self.tar:
            self.tar.close()
        self.flush()
        self.cloud_interface.complete_multipart_upload(
            mpu=self.mpu,
            key=self.key,
            parts=self.parts)


class S3UploadController(object):
    def __init__(self, cloud_interface, key_prefix, compression):
        """
        Create a new controller that upload the backup in S3

        :param CloudInterface cloud_interface: cloud interface instance
        :param str|None key_prefix: path inside the bucket
        :param str|None compression: required compression
        """

        self.cloud_interface = cloud_interface
        if key_prefix and key_prefix[0] == '/':
            key_prefix = key_prefix[1:]
        self.key_prefix = key_prefix
        self.compression = compression
        self.s3_list = {}

    def _build_dest_name(self, name):
        """
        Get the name suffix
        """
        if self.compression == 'gz':
            return "%s.tar.gz" % name
        elif self.compression == 'bz2':
            return "%s.tar.bz2" % name
        else:
            return "%s.tar" % name

    def _get_tar(self, name):
        if name not in self.s3_list or not self.s3_list[name]:

            self.s3_list[name] = S3TarUploader(
                cloud_interface=self.cloud_interface,
                key=os.path.join(self.key_prefix, self._build_dest_name(name)),
                compression=self.compression
            )
        return self.s3_list[name].tar

    def upload_directory(self, label, src, dst, exclude=None, include=None):
        logging.info("S3UploadController.upload_directory(%r, %r, %r)",
                     label, src, dst)
        tar = self._get_tar(dst)
        for root, dirs, files in os.walk(src):
            tar_root = os.path.relpath(root, src)
            if not path_allowed(exclude, include,
                                tar_root, True):
                continue
            tar.add(root, arcname=tar_root, recursive=False)
            for item in files:
                tar_item = os.path.join(tar_root, item)
                if not path_allowed(exclude, include,
                                    tar_item, False):
                    continue
                logging.debug("Uploading %s", tar_item)
                tar.add(os.path.join(root, item), arcname=tar_item)

    def add_file(self, label, src, dst, path, optional=False):
        logging.info("S3UploadController.add_file(%r, %r, %r, %r, %r)",
                     label, src, dst, path, optional)
        if optional and not os.path.exists(src):
            return
        tar = self._get_tar(dst)
        tar.add(src, arcname=path)

    def add_fileobj(self, label, fileobj, dst, path,
                    mode=None, uid=None, gid=None):
        logging.info("S3UploadController.add_fileobj(%r, %r, %r)",
                     label, dst, path)
        tar = self._get_tar(dst)
        tarinfo = tar.tarinfo(path)
        fileobj.seek(0, os.SEEK_END)
        tarinfo.size = fileobj.tell()
        if mode is not None:
            tarinfo.mode = mode
        if uid is not None:
            tarinfo.gid = uid
        if gid is not None:
            tarinfo.gid = gid
        fileobj.seek(0, os.SEEK_SET)
        tar.addfile(tarinfo, fileobj)

    def upload_fileobj(self, label, fileobj, dst):
        logging.info("S3UploadController.upload_file(%r, %r)",
                     label, dst)
        key = os.path.join(self.key_prefix, dst)
        self.cloud_interface.upload_fileobj(fileobj, key)

    def close(self):
        logging.info("S3UploadController.close()")
        for name in self.s3_list:
            s3 = self.s3_list[name]
            if s3:
                s3.close()
            self.s3_list[name] = None

    def statistics(self):
        """TODO: Write statistic code"""
        logging.info("S3UploadController.statistics()")
        return dict()


class CloudInterface:
    def __init__(self, destination_url, encryption, profile_name=None):
        """
        Create a new S3 interface given the S3 destination url and the profile
        name

        :param str destination_url: Full URL of the cloud destination
        :param str encryption: Encryption type string
        :param str profile_name: Amazon auth profile identifier
        """
        self.destination_url = destination_url
        self.profile_name = profile_name
        self.encryption = encryption

        # Extract information from the destination URL
        parsed_url = urlparse(destination_url)
        # If netloc is not present, the s3 url is badly formatted.
        if parsed_url.netloc == '' or parsed_url.scheme != 's3':
            raise ValueError('Invalid s3 URL address: %s' % destination_url)
        self.bucket_name = parsed_url.netloc
        self.path = parsed_url.path

        # Build a session, so we can extract the correct resource
        session = boto3.Session(profile_name=profile_name)
        self.s3 = session.resource('s3')

        # TODO: handle encryption

    def test_connectivity(self):
        """
        Test the S3 connectivity trying to access a bucket
        """
        try:
            self.s3.Bucket(self.bucket_name).load()
            # We are not even interested in the existence of the bucket,
            # we just want to try if aws is reachable
            return True
        except EndpointConnectionError as exc:
            logging.error("Can't connect to Amazon AWS/S3: %s", exc)
            return False

    def setup_bucket(self):
        """
        Search for the target bucket. Create it if not exists
        """
        try:
            # Search the bucket on s3
            self.s3.meta.client.head_bucket(Bucket=self.bucket_name)
        except ClientError as exc:
            # If a client error is thrown, then check that it was a 405 error.
            # If it was a 404 error, then the bucket does not exist.
            error_code = exc.response['Error']['Code']
            if error_code == '404':
                logging.debug("Bucket %s does not exist, creating it",
                              self.bucket_name)
                self.s3.Bucket(self.bucket_name).create()
            else:
                raise

    def upload_fileobj(self, fileobj, key):
        """
        Synchronously upload the content of a file-like object to a cloud key
        :param fileobj:
        :param str key:
        :return:
        """
        self.s3.meta.client.upload_fileobj(
            Fileobj=fileobj, Bucket=self.bucket_name, Key=key)

    def create_multipart_upload(self, key):
        """
        Create a new multipart upload

        :param key: The key to use in the cloud service
        :return: The multipart upload handle
        """
        return self.s3.meta.client.create_multipart_upload(
            Bucket=self.bucket_name, Key=key)

    def upload_part(self, mpu, key, body, part_number):
        """
        Upload a part into this multipart upload

        :param mpu: The multipart upload handle
        :param str key: The key to use in the cloud service
        :param object body: A stream-like object to upload
        :param int part_number: Part number, starting from 1
        :return: The part handle
        """
        part = self.s3.meta.client.upload_part(
            Body=body,
            Bucket=self.bucket_name,
            Key=key,
            UploadId=mpu["UploadId"],
            PartNumber=part_number)
        return {
            'PartNumber': part_number,
            'ETag': part['ETag'],
        }

    def complete_multipart_upload(self, mpu, key, parts):
        """
        Finish a certain multipart upload

        :param mpu:  The multipart upload handle
        :param str key: The key to use in the cloud service
        :param parts: The list of parts composing the multipart upload
        """
        self.s3.meta.client.complete_multipart_upload(
            Bucket=self.bucket_name,
            Key=key,
            UploadId=mpu["UploadId"],
            MultipartUpload={"Parts": parts})


if __name__ == '__main__':
    main()
