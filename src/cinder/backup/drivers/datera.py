# Copyright (C) 2017 Datera Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Implementation of a backup service using Datera Elastic Data Fabric

Architecture:
    Container/Bucket --> Datera EDF Volume (single volume AppInstance)
    Object --> Datera EDF Volume Snapshot
    ObjectID --> Datera EDF Volume Snapshot Timestamp


Essentially, we create a Volume as a "Bucket", then write data to that volume
and snapshot it.  The snapshots serves as our "Object" analogue.  We can
restore a backup by restoring snapshots in reverse order and reading the data
back.

Since our minimum volume size is 1 GB, we'll use that as our minimum chunk size

Multiplexing:
        This version of the driver also handles multiplexing between different
        drivers.  We determine the driver type by something!

"""
import contextlib
import hashlib
import os
import shlex
import six
import struct
import subprocess
import time
import uuid

import eventlet

from eventlet.green import threading

from oslo_concurrency import processutils as putils
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import importutils
from oslo_utils import units

from cinder.backup import chunkeddriver
from cinder import exception
from cinder.i18n import _
from cinder import interface
from cinder import utils

from os_brick import exception as brick_exception

import cinder.volume.drivers.datera.datera_common as datc

LOG = logging.getLogger(__name__)

bd_opts = [
    cfg.StrOpt('backup_datera_san_ip',
               default=None,
               help='(REQUIRED) IP address of Datera EDF backend'),
    cfg.StrOpt('backup_datera_san_login',
               default=None,
               help='(REQUIRED) Username for Datera EDF backend account'),
    cfg.StrOpt('backup_datera_san_password',
               default=None,
               help='(REQUIRED) Password for Datera EDF backend account'),
    cfg.StrOpt('backup_datera_tenant_id',
               default='/root',
               help='Datera tenant_id under which backup should be stored'),
    cfg.IntOpt('backup_datera_chunk_size',
               default=1,
               help='Total chunk size (in GB, min 1 GB) to use for backup'),
    cfg.BoolOpt('backup_datera_progress_timer',
                default=False,
                help='Enable progress timer for backup'),
    cfg.IntOpt('backup_datera_replica_count',
               default=3,
               help='Number of replicas for container'),
    cfg.StrOpt('backup_datera_placement_mode',
               default='hybrid',
               help='Options: hybrid, single_flash, all_flash'),
    cfg.StrOpt('backup_datera_api_port',
               default='7717',
               help='Datera API port.'),
    cfg.ListOpt('backup_datera_secondary_backup_drivers',
                default=[],
                help='Secondary drivers to manage with this driver.  This is '
                     'done as a way to simulate a scheduler for backups. '
                     'Takes the form:\n'
                     '["cinder.backup.drivers.driver1",\n'
                     ' "cinder.backup.drivers.driver2"]'),
    cfg.BoolOpt('backup_datera_debug',
                default=False,
                help="True to set function arg and return logging"),
    cfg.IntOpt('backup_datera_503_timeout',
               default='120',
               help='Timeout for HTTP 503 retry messages'),
    cfg.IntOpt('backup_datera_503_interval',
               default='5',
               help='Interval between 503 retries'),
    cfg.BoolOpt('backup_datera_disable_profiler',
                default=False,
                help="Set to True to disable profiling in the Datera driver"),
    cfg.BoolOpt('backup_driver_use_ssl', default=False,
                help="Set True to use SSL. Must also provide cert options"),
    cfg.StrOpt('backup_driver_client_cert',
               default=None,
               help="Path to client certificate file"),
    cfg.StrOpt('backup_driver_client_cert_key',
               default=None,
               help="Path to client certificate key file")
]

CONF = cfg.CONF
CONF.register_opts(bd_opts)

METADATA = "_metadata"
SHA256 = "_sha256file"
PREFIX = "DAT"
SI_NAME = 'storage-1'
VOL_NAME = 'volume-1'
PACK = "iQ32sxx"
TOTAL_OFFSET = 50


@interface.backupdriver
@six.add_metaclass(utils.TraceWrapperWithABCMetaclass)
class DateraBackupDriver(chunkeddriver.ChunkedBackupDriver):

    """Provides backup, restore and delete of backup objects within Datera EDF.

    Version history:
        1.0.0 - Initial driver
        1.0.1 - Added secondary backup driver dispatching/multiplexing
        2018.5.1.0 - Switched to date-based versioning scheme
    """

    VERSION = '2018.5.1.0'

    HEADER_DATA = {'Datera-Driver': 'OpenStack-Backup-{}'.format(VERSION)}

    def __init__(self, context, db_driver=None):
        # Ensure we have room for offset headers
        chunk_size = CONF.backup_datera_chunk_size * units.Gi - TOTAL_OFFSET
        # We don't care about chunks any smaller than our normal chunk size
        sha_size = chunk_size
        container_name = "replace-me"
        super(DateraBackupDriver, self).__init__(context, chunk_size,
                                                 sha_size, container_name,
                                                 db_driver)
        self.ctxt = context
        self.db_driver = db_driver
        self.support_force_delete = True
        self._backup = None
        self.san_ip = CONF.backup_datera_san_ip
        self.username = CONF.backup_datera_san_login
        self.password = CONF.backup_datera_san_password
        self.api_port = CONF.backup_datera_api_port
        self.driver_use_ssl = CONF.backup_driver_use_ssl
        self.driver_client_cert = CONF.backup_driver_client_cert
        self.driver_client_cert_key = CONF.backup_driver_client_cert_key
        self.replica_count = CONF.backup_datera_replica_count
        self.placement_mode = CONF.backup_datera_placement_mode
        self.driver_strs = CONF.backup_datera_secondary_backup_drivers
        self.driver = None
        self.drivers = {}
        self.type = 'datera'
        self.cluster_stats = {}
        self.datera_api_token = None
        self.interval = CONF.backup_datera_503_interval
        self.retry_attempts = (CONF.backup_datera_503_timeout /
                               self.interval)
        self.driver_prefix = str(uuid.uuid4())[:4]
        self.datera_debug = CONF.backup_datera_debug
        self.datera_api_versions = []

        if self.datera_debug:
            utils.setup_tracing(['method'])
        self.tenant_id = CONF.backup_datera_tenant_id
        if self.tenant_id and self.tenant_id.lower() == 'none':
            self.tenant_id = None
        self.api_check = time.time()
        self.api_cache = []
        self.api_timeout = 0
        self.do_profile = not CONF.backup_datera_disable_profiler
        self.thread_local = threading.local()
        self.thread_local.trace_id = ""

        self._populate_secondary_drivers()

        datc.register_driver(self)
        self._check_options()

    def _populate_secondary_drivers(self):
        for dstr in self.driver_strs:
            driver = importutils.import_module(dstr)
            self.drivers[dstr.split(".")[-1]] = driver

    @staticmethod
    def _execute(cmd):
        parts = shlex.split(cmd)
        putils.execute(*parts, root_helper=utils.get_root_helper(),
                       run_as_root=True)

    def login(self):
        """Use the san_login and san_password to set token."""
        body = {
            'name': self.username,
            'password': self.password
        }

        # Unset token now, otherwise potential expired token will be sent
        # along to be used for authorization when trying to login.
        self.datera_api_token = None

        try:
            LOG.debug('Getting Datera auth token.')
            results = self._issue_api_request(
                'login', 'put', body=body, sensitive=True, api_version='2.1',
                tenant=None)
            self.datera_api_token = results['key']
        except exception.NotAuthorized:
            with excutils.save_and_reraise_exception():
                LOG.error('Logging into the Datera cluster failed. Please '
                          'check your username and password set in the '
                          'cinder.conf and start the cinder-volume '
                          'service again.')

    def _check_options(self):
        req_opts = ('backup_datera_san_ip',
                    'backup_datera_san_login',
                    'backup_datera_san_password')
        no_opts = filter(lambda opt: not getattr(CONF, opt, None), req_opts)
        if no_opts:
            raise exception.InvalidInput(
                reason=_('Missing required opts %s') % no_opts)

    def _create_volume(self, name, size):
        tenant = self.tenant_id
        app_params = (
            {
                'create_mode': "openstack",
                'name': name,
                'access_control_mode': 'deny_all',
                'storage_instances': [
                    {
                        'name': SI_NAME,
                        'volumes': [
                            {
                                'name': VOL_NAME,
                                'size': size,
                                'placement_mode': self.placement_mode,
                                'replica_count': self.replica_count,
                                'snapshot_policies': [
                                ]
                            }
                        ]
                    }
                ]
            })
        self._issue_api_request(datc.URL_TEMPLATES['ai'](), 'post',
                                body=app_params, api_version='2.1',
                                tenant=tenant)

    def _detach_volume(self, name):
        url = datc.URL_TEMPLATES['ai_inst']().format(name)
        data = {
            'admin_state': 'offline',
            'force': True
        }
        try:
            self._issue_api_request(url, method='put', body=data,
                                    api_version='2.1', tenant=self.tenant_id)
        except exception.NotFound:
            msg = _("Tried to detach volume %s, but it was not found in the "
                    "Datera cluster. Continuing with detach.")
            LOG.info(msg, name)

    def _delete_volume(self, name):
        self._detach_volume(name)
        try:
            self._issue_api_request(
                datc.URL_TEMPLATES['ai_inst']().format(name), 'delete',
                api_version='2.1', tenant=self.tenant_id)
        except (exception.DateraAPIException, exception.NotFound):
            LOG.debug("Couldn't find volume: {}".format(name))

    def _volume_exists(self, bname):
        try:
            self._issue_api_request(datc.URL_TEMPLATES['ai_inst']().format(
                bname), 'get', api_version='2.1', tenant=self.tenant_id)
            return True
        except exception.NotFound:
            return False

    def _create_snapshot(self, bname):
        snap = self._issue_api_request(datc.URL_TEMPLATES['vol_inst'](
            SI_NAME, VOL_NAME).format(bname) + '/snapshots', 'post',
            body={}, api_version='2.1', tenant=self.tenant_id)
        # Polling the snapshot is absolutely necessary otherwise we hit race
        # conditions that can cause the snapshot to fail
        self._snap_poll_2_1(snap['path'].strip("/"))
        return snap['data']

    def _restore_snapshot(self, bname, timestamp):
        url = datc.URL_TEMPLATES['ai_inst']().format(bname)
        self._detach_volume(bname)
        self._issue_api_request(datc.URL_TEMPLATES['vol_inst'](
            SI_NAME, VOL_NAME).format(bname), 'put',
            body={'restore_point': timestamp}, api_version='2.1',
            tenant=self.tenant_id)
        data = {
            'admin_state': 'online'
        }
        self._issue_api_request(
            url, method='put', body=data, api_version='2.1',
            tenant=self.tenant_id)
        # Trying a sleep here to give the snapshot a moment to restore
        LOG.debug("Sleeping for 5s to give the snapshot a chance")
        eventlet.sleep(5)

    def _list_snapshots(self, bname):
        snaps = self._issue_api_request(datc.URL_TEMPLATES['vol_inst'](
            SI_NAME, VOL_NAME).format(bname) + '/snapshots', 'get',
            api_version='2.1', tenant=self.tenant_id)
        return snaps['data']

    def _get_snapshot(self, bname, timestamp):
        return self._issue_api_request(datc.URL_TEMPLATES['vol_inst'](
            SI_NAME, VOL_NAME).format(
                bname) + '/snapshots/{}'.format(timestamp), 'get',
            api_version='2.1', tenant=self.tenant_id)

    def _delete_snapshot(self, bname, timestamp):
        for snapshot in self._list_snapshots(bname):
            if snapshot['utc_ts'] == timestamp:
                self._issue_api_request(datc.URL_TEMPLATES['vol_inst'](
                    SI_NAME, VOL_NAME).format(bname) + '/snapshots/{'
                    '}'.format(timestamp), 'delete', api_version='2.1')
                return
        LOG.debug('Did not find snapshot {} to delete'.format(timestamp))

    def _get_sis_iqn_portal(self, bname):
        iqn = None
        portal = None
        url = datc.URL_TEMPLATES['ai_inst']().format(bname)
        data = {
            'admin_state': 'online'
        }
        app_inst = self._issue_api_request(
            url, method='put', body=data, api_version='2.1',
            tenant=self.tenant_id)['data']
        storage_instances = app_inst["storage_instances"]
        si = storage_instances[0]
        portal = si['access']['ips'][0] + ':3260'
        iqn = si['access']['iqn']
        return storage_instances, iqn, portal

    def _register_acl(self, bname, initiator, storage_instances):
        initiator_name = "OpenStack_{}_{}".format(
            self.driver_prefix, str(uuid.uuid4())[:4])
        found = False
        if not found:
            data = {'id': initiator, 'name': initiator_name}
            # Try and create the initiator
            # If we get a conflict, ignore it
            self._issue_api_request("initiators",
                                    method="post",
                                    body=data,
                                    conflict_ok=True,
                                    api_version='2.1',
                                    tenant=self.tenant_id)
        initiator_path = "/initiators/{}".format(initiator)
        # Create ACL with initiator for storage_instances
        for si in storage_instances:
            acl_url = (datc.URL_TEMPLATES['si']() +
                       "/{}/acl_policy").format(bname, si['name'])
            existing_acl = self._issue_api_request(acl_url,
                                                   method="get",
                                                   api_version='2.1',
                                                   tenant=self.tenant_id)[
                'data']
            data = {}
            data['initiators'] = existing_acl['initiators']
            data['initiators'].append({"path": initiator_path})
            data['initiator_groups'] = existing_acl['initiator_groups']
            self._issue_api_request(acl_url,
                                    method="put",
                                    body=data,
                                    api_version='2.1',
                                    tenant=self.tenant_id)
        self._si_poll(bname)

    def _si_poll(self, bname):
        TIMEOUT = 10
        retry = 0
        check_url = datc.URL_TEMPLATES['si_inst'](SI_NAME).format(bname)
        poll = True
        while poll and not retry >= TIMEOUT:
            retry += 1
            si = self._issue_api_request(check_url,
                                         api_version='2.1',
                                         tenant=self.tenant_id)['data']
            if si['op_state'] == 'available':
                poll = False
            else:
                eventlet.sleep(1)
        if retry >= TIMEOUT:
            raise exception.VolumeDriverException(
                message=_('Resource not ready.'))

    def _snap_poll_2_1(self, url):
        tenant = self.tenant_id
        eventlet.sleep(datc.DEFAULT_SNAP_SLEEP)
        TIMEOUT = 20
        retry = 0
        poll = True
        while poll and not retry >= TIMEOUT:
            retry += 1
            snap = self._issue_api_request(url,
                                           api_version='2.1',
                                           tenant=tenant)['data']
            if snap['op_state'] == 'available':
                poll = False
            else:
                eventlet.sleep(1)
        if retry >= TIMEOUT:
            raise exception.VolumeDriverException(
                message=_('Snapshot not ready.'))

    @contextlib.contextmanager
    def _connect_target(self, container):
        connector = None
        try:
            sis, iqn, portal = self._get_sis_iqn_portal(container)
            conn = {'driver_volume_type': 'iscsi',
                    'data': {
                        'target_discovered': False,
                        'target_iqn': iqn,
                        'target_portal': portal,
                        'target_lun': 0,
                        'volume_id': None,
                        'discard': False}}
            connector = utils.brick_get_connector(
                conn['driver_volume_type'],
                use_multipath=False,
                device_scan_attempts=10,
                conn=conn)

            # Setup ACL
            initiator = connector.get_initiator()
            self._register_acl(container, initiator, sis)

            # Attach Target
            attach_info = {}
            attach_info['target_portal'] = portal
            attach_info['target_iqn'] = iqn
            attach_info['target_lun'] = 0
            retries = 10
            while True:
                try:
                    attach_info.update(
                        connector.connect_volume(conn['data']))
                    break
                except brick_exception.FailedISCSITargetPortalLogin:
                    retries -= 1
                    if not retries:
                        LOG.error("Could not log into portal before end of "
                                  "polling period")
                        raise
                    LOG.debug("Failed to login to portal, retrying")
                    eventlet.sleep(2)
            device_path = attach_info['path']
            yield device_path
        finally:
            # Close target connection
            if connector:
                # Best effort disconnection
                try:
                    connector.disconnect_volume(attach_info, attach_info)
                except Exception:
                    pass

    def _parse_name(self, name):
        return int(name.split("-")[-1])

    def _get_driver(self):
        if not self.driver:
            supported_list = []
            for dstr in self.driver_strs:
                supported_list.append(dstr.split(".")[-1])
            name = (self._backup['display_name'].lower()
                    if self._backup['display_name'] else None)
            if not name or 'datera' in name:
                self.type = 'datera'
                return
            for supported in supported_list:
                if supported in name:
                    self.type = supported
                    self.driver = self.drivers[self.type].get_backup_driver(
                        self.ctxt)
            if not self.driver:
                raise EnvironmentError(
                    "Unsupported driver: {}, display name of backup must "
                    "contain name of driver to use.  Supported drivers: {}"
                    "".format(name, self.drivers.keys()))
        return self.driver

    def put_container(self, bucket):
        """Create the bucket if not exists."""
        driver = self._get_driver()
        if not driver:
            if self._volume_exists(bucket):
                return
            else:
                vol_size = CONF.backup_datera_chunk_size
                self._create_volume(bucket, vol_size)
                return
        return driver.put_container(bucket)

    def get_container_entries(self, bucket, prefix):
        """Get bucket entry names."""
        driver = self._get_driver()
        if not driver:
            return ["-".join((prefix, "{:05d}".format(i + 1)))
                    for i, _ in enumerate(self._list_snapshots(bucket))][:-2]
        return driver.get_container_entries(bucket, prefix)

    def get_object_writer(self, bucket, object_name, extra_metadata=None):
        """Return a writer object.

        Returns a writer object that stores a chunk of volume data in a
        Datera volume
        """
        driver = self._get_driver()
        if not driver:
            return DateraObjectWriter(bucket, object_name, self)
        return driver.get_object_reader(bucket, object_name, extra_metadata)

    def get_object_reader(self, bucket, object_name, extra_metadata=None):
        """Return reader object.

        Returns a reader object that retrieves a chunk of backed-up volume data
        from a Datera EDF object store.
        """
        driver = self._get_driver()
        if not driver:
            return DateraObjectReader(bucket, object_name, self)
        return driver.get_object_reader(bucket, object_name, extra_metadata)

    def delete_object(self, bucket, object_name):
        """Deletes a backup object from a Datera EDF object store."""
        driver = self._get_driver()
        if not driver:
            return self._delete_snapshot(bucket, object_name)
        return driver.delete_object(bucket, object_name)

    def backup(self, backup, volume_file, backup_metadata=False):
        self._backup = backup
        driver = self._get_driver()
        if not driver:
            # We should always backup metadata in the Datera driver
            # It costs practically nothing and Tempest expects metadata to
            # be backed up.
            return super(DateraBackupDriver, self).backup(
                backup, volume_file, backup_metadata=True)
        return driver.backup(backup, volume_file, backup_metadata)

    def restore(self, backup, volume_id, volume_file):
        self._backup = backup
        driver = self._get_driver()
        if not driver:
            return super(DateraBackupDriver, self).restore(
                backup, volume_id, volume_file)
        return driver.restore(backup, volume_id, volume_file)

    # def get_metadata(self, volume_id):
    #     driver = self._get_driver()
    #     if not driver:
    #         return super(DateraBackupDriver, self).get_metadata(volume_id)
    #     return driver.get_metadata(volume_id)

    # def put_metadata(self, volume_id, json_metadata):
    #     driver = self._get_driver()
    #     if not driver:
    #         return super(DateraBackupDriver, self).put_metadata(
    #             volume_id, json_metadata)
    #     return driver.put_metadata(volume_id, json_metadata)

    def delete(self, backup):
        self._backup = backup
        driver = self._get_driver()
        if not driver:
            container = backup['container']
            object_prefix = backup['service_metadata']
            LOG.debug('delete started, backup: %(id)s, container: %(cont)s, '
                      'prefix: %(pre)s.',
                      {'id': backup['id'],
                       'cont': container,
                       'pre': object_prefix})
            if container is not None:
                self._delete_volume(container)
            LOG.debug('delete %s finished.', backup['id'])
            return
        return driver.delete(backup)

    def export_record(self, backup):
        """Export driver specific backup record information.

        If backup backend needs additional driver specific information to
        import backup record back into the system it must overwrite this method
        and return it here as a dictionary so it can be serialized into a
        string.

        Default backup driver implementation has no extra information.

        :param backup: backup object to export
        :returns: driver_info - dictionary with extra information
        """
        self._backup = backup
        driver = self._get_driver()
        if not driver:
            return super(DateraBackupDriver, self).export_record(backup)
        return driver.export_record(backup)

    def import_record(self, backup, driver_info):
        """Import driver specific backup record information.

        If backup backend needs additional driver specific information to
        import backup record back into the system it must overwrite this method
        since it will be called with the extra information that was provided by
        export_record when exporting the backup.

        Default backup driver implementation does nothing since it didn't
        export any specific data in export_record.

        :param backup: backup object to export
        :param driver_info: dictionary with driver specific backup record
                            information
        :returns: nothing
        """
        self._backup = backup
        driver = self._get_driver()
        if not driver:
            return super(DateraBackupDriver, self).import_record(
                backup, driver_info)
        return driver.import_record(backup, driver_info)

    def _generate_object_name_prefix(self, backup):
        """Generates a Datera EDF backup object name prefix."""
        driver = self._get_driver()
        if not driver:
            return PREFIX
        return driver._generate_object_name_prefix(self, backup)

    def update_container_name(self, backup, bucket):
        """Use the bucket name as provided - don't update."""
        driver = self._get_driver()
        if not driver:
            if not backup['container']:
                return "-".join(("BACKUP", str(self._backup['id'])))
            else:
                return
        return driver.update_container_name(self, backup, bucket)

    def get_extra_metadata(self, backup, volume):
        """Datera EDF driver does not use any extra metadata."""
        driver = self._get_driver()
        if not driver:
            return
        return driver.get_extra_metadata(backup, volume)


class DateraObjectWriter(object):
    def __init__(self, container, object_name, driver):
        LOG.debug("Object writer. container: %(container)s, "
                  "object_name: %(object)s",
                  {'container': container,
                   'object': object_name})
        self.container = container
        self.object_name = object_name
        self.driver = driver
        self.data = None
        self.write_metadata = True if object_name.endswith(METADATA) else False
        self.write_sha256 = True if object_name.endswith(SHA256) else False

        if self.write_metadata and self.write_sha256:
            raise ValueError("We're misunderstanding the requirements...")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def write(self, data):
        # Assuming a single write
        self.data = data

    def close(self):
        LOG.debug("Writing backup.Container: %(container)s, "
                  "object_name: %(object)s",
                  {'container': self.container,
                   'object': self.object_name})
        with self.driver._connect_target(self.container) as device_path:
            # Write backup data
            self.driver._execute("chmod o+w {}".format(device_path))
            f = os.open(device_path, os.O_SYNC | os.O_WRONLY)
            # Write number, length and MD5 to initial offset
            if self.write_sha256:
                n = -2
            elif self.write_metadata:
                n = -1
            else:
                n = self.driver._parse_name(self.object_name)
            l = len(self.data)
            h = hashlib.md5(self.data).hexdigest()
            os.write(f, struct.pack(PACK, n, l, h))
            LOG.debug("Writing Headers.\n Number: %(number)s\n"
                      "Length: %(length)s\n"
                      "MD5: %(md5)s",
                      {'number': n,
                       'length': len(self.data),
                       'md5': h})
            # Write actual data
            # os.lseek(f, TOTAL_OFFSET, 0)
            os.write(f, self.data)
            # If we're writing a really small amount of data (< 1 KiB), then
            # we should write additional data to ensure the block device
            # recognizes that we wrote data.  We'll just write 5 KiB of random
            # data after the data we care about so as to not impact performance
            if l <= 1 * units.Ki:
                LOG.debug("Writing additional data to ensure write takes")
                # Pad 8 bytes for visual debugging
                os.write(f, "\x00" * 8)
                # Random data
                os.write(f, os.urandom(5 * units.Ki))
            os.close(f)
            # for short writes we need to let the cache flush
            subprocess.check_call("sync")
            # Then sleep so the flush occurs
            eventlet.sleep(3)
            self.driver._execute("chmod o-w {}".format(device_path))
            self.driver._create_snapshot(self.container)


class DateraObjectReader(object):
    def __init__(self, container, object_name, driver):
        LOG.debug("Object reader. Container: %(container)s, "
                  "object_name: %(object)s",
                  {'container': container,
                   'object': object_name})
        self.container = container
        self.object_name = object_name
        self.driver = driver
        self.read_metadata = True if object_name.endswith(METADATA) else False
        self.read_sha256 = True if object_name.endswith(SHA256) else False

        if self.read_metadata and self.read_sha256:
            raise ValueError("We're misunderstanding the requirements...")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return

    def read(self):
        LOG.debug("Reading backup. Container: %(container)s, "
                  "object_name: %(object)s",
                  {'container': self.container,
                   'object': self.object_name})
        data = self.driver._list_snapshots(self.container)
        if self.read_sha256:
            snap = data[-2]["utc_ts"]
        elif self.read_metadata:
            snap = data[-1]["utc_ts"]
        else:
            # Backups start at 00001, convert to zero index
            snap = data[self.driver._parse_name(self.object_name) - 1][
                "utc_ts"]
        LOG.debug("Restoring Snapshot: {}".format(snap))
        self.driver._restore_snapshot(self.container, snap)
        # self.driver._delete_snapshot(self.container, most_recent)
        with self.driver._connect_target(self.container) as device_path:
            # Read backup data
            self.driver._execute("chmod o+r {}".format(device_path))
            f = os.open(device_path, os.O_RDONLY)
            # Read headers
            rawh = os.read(f, TOTAL_OFFSET)
            n, l, h = struct.unpack(PACK, rawh)
            LOG.debug("Reading Headers.\n Number: %(number)s\n"
                      "Length: %(length)s\n"
                      "MD5: %(md5)s",
                      {'number': n,
                       'length': l,
                       'md5': h})
            # Read data
            data = os.read(f, l)
            os.close(f)
            # Compare hashes
            newh = hashlib.md5(data).hexdigest()
            if newh != h:
                raise ValueError("Data hash read off backup doesn't match "
                                 "calculated hash. new hash: %(new)s "
                                 "read hash: %(read)s",
                                 {'new': newh,
                                  'read': h})
            self.driver._execute("chmod o-r {}".format(device_path))
            return data


def get_backup_driver(context, db_driver=None):
    return DateraBackupDriver(context, db_driver=db_driver)
