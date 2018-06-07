# Copyright 2017 Datera
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

import contextlib
import math
import random
import time
import uuid

import eventlet
import ipaddress
import six

from oslo_log import log as logging
from oslo_serialization import jsonutils as json
from oslo_utils import excutils
from oslo_utils import units

from cinder import exception
from cinder.i18n import _
from cinder.image import image_utils
from cinder.volume import utils as volutils
from cinder import utils
from cinder.volume import volume_types

from os_brick import exception as brick_exception

import cinder.volume.drivers.datera.datera_common as datc

LOG = logging.getLogger(__name__)

API_VERSION = "2.2"


class DateraApi(object):

    def _api22(self, *args, **kwargs):
        return self._issue_api_request(
            *args, api_version=API_VERSION, **kwargs)
    # =================
    # = Create Volume =
    # =================

    def _create_volume_2_2(self, volume):
        policies = self._get_policies_for_resource(volume)
        num_replicas = int(policies['replica_count'])
        storage_name = 'storage-1'
        volume_name = 'volume-1'
        template = policies['template']
        placement = policies['placement_mode']
        ip_pool = policies['ip_pool']

        if template:
            app_params = (
                {
                    'create_mode': 'openstack',
                    # 'uuid': str(volume['id']),
                    'name': datc._get_name(volume['id']),
                    'app_template': {'path': '/app_templates/{}'.format(
                        template)}
                })

        else:

            app_params = (
                {
                    'create_mode': 'openstack',
                    'uuid': str(volume['id']),
                    'name': datc._get_name(volume['id']),
                    'access_control_mode': 'deny_all',
                    'storage_instances': [
                        {
                            'name': storage_name,
                            'ip_pool': {'path': ('/access_network_ip_pools/'
                                                 '{}'.format(ip_pool))},
                            'volumes': [
                                {
                                    'name': volume_name,
                                    'size': volume['size'],
                                    'placement_mode': placement,
                                    'replica_count': num_replicas,
                                    'snapshot_policies': [
                                    ]
                                }
                            ]
                        }
                    ]
                })
        self._api22(
            datc.URL_T['ai'](),
            'post',
            volume['project_id'],
            body=app_params)
        self._update_qos_2_2(volume, policies)
        self._add_vol_meta_2_2(volume)

    # =================
    # = Extend Volume =
    # =================

    def _extend_volume_2_2(self, volume, new_size):
        if volume['size'] >= new_size:
            LOG.warning("Volume size not extended due to original size being "
                        "greater or equal to new size.  Originial: "
                        "%(original)s, New: %(new)s", {
                            'original': volume['size'],
                            'new': new_size})
            return
        policies = self._get_policies_for_resource(volume)
        template = policies['template']
        if template:
            LOG.warning("Volume size not extended due to template binding:"
                        " volume: %(volume)s, template: %(template)s",
                        {'volume': volume, 'template': template})
            return

        with self._offline_flip_2_2(volume):
            # Change Volume Size
            app_inst = datc._get_name(volume['id'])
            data = {
                'size': new_size
            }
            store_name, vol_name = self._scrape_ai_2_2(volume)
            self._api22(
                datc.URL_T['vol_inst'](app_inst, store_name, vol_name),
                'put',
                volume['project_id'],
                body=data,
            )

    # =================
    # = Cloned Volume =
    # =================

    def _create_cloned_volume_2_2(self, volume, src_vref):
        store_name, vol_name = self._scrape_ai_2_2(src_vref)

        src = "/" + datc.URL_T['vol_inst'](
            datc._get_name(src_vref['id']), store_name, vol_name)
        data = {
            'create_mode': 'openstack',
            'name': datc._get_name(volume['id']),
            'uuid': str(volume['id']),
            'clone_volume_src': {'path': src},
        }
        self._api22(
            datc.URL_T['ai'](), 'post',
            volume['project_id'], body=data)

        if volume['size'] > src_vref['size']:
            self._extend_volume_2_2(volume, volume['size'])
        self._add_vol_meta_2_2(volume)

    # =================
    # = Delete Volume =
    # =================

    def _delete_volume_2_2(self, volume):
        self._detach_volume_2_2(None, volume)
        app_inst = datc._get_name(volume['id'])
        try:
            self._api22(
                datc.URL_T['ai_inst'](app_inst),
                'delete',
                volume['project_id'])
        except exception.NotFound:
            msg = ("Tried to delete volume %s, but it was not found in the "
                   "Datera cluster. Continuing with delete.")
            LOG.info(msg, datc._get_name(volume['id']))

    # =================
    # = Ensure Export =
    # =================

    def _ensure_export_2_2(self, context, volume, connector=None):
        pass

    # =========================
    # = Initialize Connection =
    # =========================

    def _initialize_connection_2_2(self, volume, connector):
        # Now online the app_instance (which will online all storage_instances)
        multipath = connector.get('multipath', False)
        url = datc.URL_T['ai_inst'](datc._get_name(volume['id']))
        data = {
            'admin_state': 'online'
        }
        app_inst = self._api22(
            url, 'put',  volume['project_id'], body=data)['data']
        storage_instances = app_inst["storage_instances"]
        si = storage_instances[0]

        # randomize portal chosen
        choice = 0
        policies = self._get_policies_for_resource(volume)
        if policies["round_robin"]:
            choice = random.randint(0, 1)
        portal = si['access']['ips'][choice] + ':3260'
        iqn = si['access']['iqn']
        if multipath:
            portals = [p + ':3260' for p in si['access']['ips']]
            iqns = [iqn for _ in si['access']['ips']]
            lunids = [self._get_lunid() for _ in si['access']['ips']]

            result = {
                'driver_volume_type': 'iscsi',
                'data': {
                    'target_discovered': False,
                    'target_iqn': iqn,
                    'target_iqns': iqns,
                    'target_portal': portal,
                    'target_portals': portals,
                    'target_lun': self._get_lunid(),
                    'target_luns': lunids,
                    'volume_id': volume['id'],
                    'discard': False}}
        else:
            result = {
                'driver_volume_type': 'iscsi',
                'data': {
                    'target_discovered': False,
                    'target_iqn': iqn,
                    'target_portal': portal,
                    'target_lun': self._get_lunid(),
                    'volume_id': volume['id'],
                    'discard': False}}

        if self.use_chap_auth:
            result['data'].update(
                auth_method="CHAP",
                auth_username=self.chap_username,
                auth_password=self.chap_password)

        return result

    # =================
    # = Create Export =
    # =================

    def _create_export_2_2(self, context, volume, connector):
        url = datc.URL_T['ai_inst'](datc._get_name(volume['id']))
        data = {
            'admin_state': 'offline',
            'force': True
        }
        self._api22(
            url, 'put',  volume['project_id'], body=data)
        policies = self._get_policies_for_resource(volume)
        store_name, _ = self._scrape_ai_2_2(volume)
        if connector and connector.get('ip'):
            # Case where volume_type has non default IP Pool info
            if policies['ip_pool'] != 'default':
                initiator_ip_pool_path = self._api22(
                    "access_network_ip_pools/{}".format(
                        policies['ip_pool']),
                    'get',

                    volume['project_id'])['path']
            # Fallback to trying reasonable IP based guess
            else:
                initiator_ip_pool_path = self._get_ip_pool_for_string_ip_2_2(
                    connector['ip'], volume['project_id'])

            ip_pool_url = datc.URL_T['si_inst'](
                datc._get_name(volume['id']), store_name)
            ip_pool_data = {'ip_pool': {'path': initiator_ip_pool_path}}
            self._api22(ip_pool_url,
                        "put",
                        volume['project_id'],
                        body=ip_pool_data,
                        )
        url = datc.URL_T['ai_inst'](datc._get_name(volume['id']))
        data = {
            'admin_state': 'online'
        }
        self._api22(
            url, 'put',  volume['project_id'], body=data)
        # Check if we've already setup everything for this volume
        url = datc.URL_T['si'](datc._get_name(volume['id']))
        storage_instances = self._api22(
            url, 'get',  volume['project_id'])
        # Handle adding initiator to product if necessary
        # Then add initiator to ACL
        if connector and connector.get('initiator'):
            initiator_name = "OpenStack_{}_{}".format(
                self.driver_prefix, str(uuid.uuid4())[:4])
            # TODO(_alastor_): actually check for existing initiator
            found = False
            initiator = connector['initiator']
            initiator_path = "/initiators/{}".format(initiator)
            if not found:
                # TODO(_alastor_): Take out the 'force' flag when we fix
                # DAT-15931
                data = {'id': initiator, 'name': initiator_name, 'force': True}
                # Try and create the initiator
                # If we get a conflict, ignore it
                self._api22("initiators",
                            "post",
                            volume['project_id'],
                            body=data,
                            conflict_ok=True,
                            )
            # Create ACL with initiator group as reference for each
            # storage_instance in app_instance
            # TODO(_alastor_): We need to avoid changing the ACLs if the
            # template already specifies an ACL policy.
            for si in storage_instances['data']:
                acl_url = (datc.URL_T['si_inst'](
                    datc._get_name(volume['id']), si['name']) + "/acl_policy")
                existing_acl = self._api22(
                    acl_url, "get",  volume['project_id'])['data']
                data = {}
                # Grabbing only the 'path' key from each existing initiator
                # within the existing acl. eacli --> existing acl initiator
                eacli = []
                for acl in existing_acl['initiators']:
                    nacl = {}
                    nacl['path'] = acl['path']
                    eacli.append(nacl)
                data['initiators'] = eacli
                data['initiators'].append({"path": initiator_path})
                # Grabbing only the 'path' key from each existing initiator
                # group within the existing acl. eaclig --> existing
                # acl initiator group
                eaclig = []
                for acl in existing_acl['initiator_groups']:
                    nacl = {}
                    nacl['path'] = acl['path']
                    eaclig.append(nacl)
                data['initiator_groups'] = eaclig
                self._api22(acl_url,
                            "put",
                            volume['project_id'],
                            body=data)
        if self.use_chap_auth:
            for si in storage_instances['data']:
                auth_url = (datc.URL_T['si_inst'](
                    datc._get_name(volume['id']), si['name']) + "/auth")
                data = {'type': 'chap',
                        'target_user_name': self.chap_username,
                        'target_pswd': self.chap_password}
                self._api22(
                    auth_url, "put",  volume['project_id'],
                    body=data, sensitive=True)
        # Check to ensure we're ready for go-time
        self._si_poll_2_2(volume, store_name)
        self._add_vol_meta_2_2(volume, connector=connector)

    # =================
    # = Detach Volume =
    # =================

    def _detach_volume_2_2(self, context, volume, attachment=None):
        url = datc.URL_T['ai_inst'](datc._get_name(volume['id']))
        data = {
            'admin_state': 'offline',
            'force': True
        }
        try:
            self._api22(
                url, 'put',  volume['project_id'], body=data)
            # TODO(_alastor_): Make acl cleaning multi-attach aware
            self._clean_acl_2_2(volume)
        except exception.NotFound:
            msg = ("Tried to detach volume %s, but it was not found in the "
                   "Datera cluster. Continuing with detach.")
            LOG.info(msg, volume['id'])

    def _clean_acl_2_2(self, volume):
        store_name, _ = self._scrape_ai_2_2(volume)

        acl_url = datc.URL_T["si_inst"](
            datc._get_name(volume['id']), store_name) + "/acl_policy"
        try:
            initiator_group = self._api22(
                acl_url, 'get',  volume['project_id'])['data'][
                    'initiator_groups'][0]['path']
            # Clear out ACL and delete initiator group
            self._api22(acl_url,
                        "put",
                        volume['project_id'],
                        body={'initiator_groups': []},
                        )
            self._api22(initiator_group.lstrip("/"),
                        "delete",
                        volume['project_id'],
                        )
        except (IndexError, exception.NotFound):
            LOG.debug("Did not find any initiator groups for volume: %s",
                      volume)

    # ===================
    # = Create Snapshot =
    # ===================

    def _create_snapshot_2_2(self, snapshot):

        dummy_vol = {'id': snapshot['volume_id'],
                     'project_id': snapshot['project_id']}
        store_name, vol_name = self._scrape_ai_2_2(dummy_vol)

        url = datc.URL_T['vol_inst'](
            datc._get_name(snapshot['volume_id']), store_name, vol_name)
        url += '/snapshots'

        snap_params = {
            'uuid': snapshot['id'],
        }
        snap = self._api22(
            url, 'post',  snapshot['project_id'], body=snap_params)
        snapu = "/".join((url, snap['data']['timestamp']))
        self._snap_poll_2_2(snapu, snapshot['project_id'])

    # ===================
    # = Delete Snapshot =
    # ===================

    def _delete_snapshot_2_2(self, snapshot):
        # Handle case where snapshot is "managed"
        dummy_vol = {'id': snapshot['volume_id'],
                     'project_id': snapshot['project_id']}
        store_name, vol_name = self._scrape_ai_2_2(dummy_vol)
        vol_id = datc._get_name(snapshot['volume_id'])

        snapu = datc.URL_T['vol_inst'](
            vol_id, store_name, vol_name) + '/snapshots'
        snapshots = []

        # Shortcut if this is a managed snapshot
        if snapshot.get('provider_location'):
            url_template = snapu + '/{}'
            url = url_template.format(snapshot.get('provider_location'))
            self._api22(url, 'delete',
                        snapshot['project_id'])
            return

        # Long-way.  UUID identification
        try:
            snapshots = self._api22(snapu, 'get',
                                    snapshot['project_id'])
        except exception.NotFound:
            msg = ("Tried to delete snapshot %s, but parent volume %s was "
                   "not found in Datera cluster. Continuing with delete.")
            LOG.info(msg,
                     datc._get_name(snapshot['id']),
                     datc._get_name(snapshot['volume_id']))
            return

        try:
            for snap in snapshots['data']:
                if snap['uuid'] == snapshot['id']:
                    url_template = snapu + '/{}'
                    url = url_template.format(snap['timestamp'])
                    self._api22(url, 'delete',
                                snapshot['project_id'])
                    break
            else:
                raise exception.NotFound
        except exception.NotFound:
            msg = ("Tried to delete snapshot %s, but was not found in "
                   "Datera cluster. Continuing with delete.")
            LOG.info(msg, datc._get_name(snapshot['id']))

    # ========================
    # = Volume From Snapshot =
    # ========================

    def _create_volume_from_snapshot_2_2(self, volume, snapshot):
        # Handle case where snapshot is "managed"
        dummy_vol = {'id': snapshot['volume_id'],
                     'project_id': snapshot['project_id']}
        store_name, vol_name = self._scrape_ai_2_2(dummy_vol)
        vol_id = datc._get_name(snapshot['volume_id'])

        snapu = datc.URL_T['vol_inst'](
            vol_id, store_name, vol_name) + '/snapshots'
        found_ts = None
        if snapshot.get('provider_location'):
            found_ts = snapshot['provider_location']
        else:
            snapshots = self._api22(
                snapu, 'get',  volume['project_id'])

            for snap in snapshots['data']:
                if snap['uuid'] == snapshot['id']:
                    found_ts = snap['utc_ts']
                    break
            else:
                raise exception.NotFound

        snap_url = datc.URL_T['vol_inst'](
            datc._get_name(snapshot['volume_id']), store_name, vol_name)
        snap_url += '/snapshots/{}'.format(found_ts)

        self._snap_poll_2_2(snap_url, snapshot['project_id'])

        src = "/" + snap_url
        app_params = (
            {
                'create_mode': 'openstack',
                'uuid': str(volume['id']),
                'name': datc._get_name(volume['id']),
                'clone_snapshot_src': {'path': src},
            })
        self._api22(
            datc.URL_T['ai'](),
            'post',
            volume['project_id'],
            body=app_params)

        if (volume['size'] > snapshot['volume_size']):
            self._extend_volume_2_2(volume, volume['size'])
        self._add_vol_meta_2_2(volume)

    # ==========
    # = Retype =
    # ==========

    def _retype_2_2(self, ctxt, volume, new_type, diff, host):
        LOG.debug("Retype called\n"
                  "Volume: %(volume)s\n"
                  "NewType: %(new_type)s\n"
                  "Diff: %(diff)s\n"
                  "Host: %(host)s\n", {'volume': volume, 'new_type': new_type,
                                       'diff': diff, 'host': host})
        store_name, vol_name = self._scrape_ai_2_2(volume)

        def _put(vol_params, si, vol):
            url = datc.URL_T['vol_inst'](
                datc._get_name(volume['id']), si, vol)
            self._api22(
                url, 'put',  volume['project_id'], body=vol_params)
        # We'll take the fast route only if the types share the same backend
        # And that backend matches this driver
        old_pol = self._get_policies_for_resource(volume)
        new_pol = self._get_policies_for_volume_type(new_type)
        if (host['capabilities']['vendor_name'].lower() ==
                self.backend_name.lower()):
            LOG.debug("Starting fast volume retype")

            if old_pol.get('template') or new_pol.get('template'):
                LOG.warning(
                    "Fast retyping between template-backed volume-types "
                    "unsupported.  Type1: %s, Type2: %s",
                    volume['volume_type_id'], new_type)

            self._update_qos_2_2(volume, new_pol, clear_old=True)
            # Only replica_count ip_pool requires offlining the app_instance
            if (new_pol['replica_count'] != old_pol['replica_count'] or
                    new_pol['ip_pool'] != old_pol['ip_pool']):
                with self._offline_flip_2_2(volume):
                    vol_params = (
                        {
                            'placement_mode': new_pol['placement_mode'],
                            'replica_count': new_pol['replica_count'],
                        })
                    _put(vol_params, store_name, vol_name)
            elif new_pol['placement_mode'] != old_pol['placement_mode']:
                vol_params = (
                    {
                        'placement_mode': new_pol['placement_mode'],
                    })
                _put(vol_params, store_name, vol_name)
            self._add_vol_meta_2_2(volume)
            return True

        else:
            LOG.debug("Couldn't fast-retype volume between specified types")
            return False

    # ==========
    # = Manage =
    # ==========

    def _manage_existing_2_2(self, volume, existing_ref):
        # Only volumes created under the requesting tenant can be managed in
        # the v2.1+ API.  Eg.  If tenant A is the tenant for the volume to be
        # managed, it must also be tenant A that makes this request.
        # This will be fixed in a later API update
        existing_ref = existing_ref['source-name']
        app_inst_name, _, _, _ = datc._parse_vol_ref(existing_ref)
        LOG.debug("Managing existing Datera volume %s  "
                  "Changing name to %s",
                  datc._get_name(volume['id']), existing_ref)
        data = {'name': datc._get_name(volume['id'])}
        # Rename AppInstance
        self._api22(datc.URL_T['ai_inst'](app_inst_name), 'put',
                    volume['project_id'], body=data)
        self._add_vol_meta_2_2(volume)

    # ===================
    # = Manage Get Size =
    # ===================

    def _manage_existing_get_size_2_2(self, volume, existing_ref):
        existing_ref = existing_ref['source-name']
        app_inst_name, storage_inst_name, vol_name, _ = datc._parse_vol_ref(
            existing_ref)
        app_inst = self._api22(
            datc.URL_T['ai_inst'](app_inst_name),
            'get',  volume['project_id'])
        return datc._get_size(app_inst=app_inst)

    # =========================
    # = Get Manageable Volume =
    # =========================

    def _list_manageable_2_2(self, cinder_volumes):
        # Use the first volume to determine the tenant we're working under
        app_instances = self._api22(
            datc.URL_T['ai'](), 'get',
            cinder_volumes[0]['project_id'])['data']

        results = []

        if cinder_volumes and 'volume_id' in cinder_volumes[0]:
            cinder_volume_ids = [vol['volume_id'] for vol in cinder_volumes]
        elif cinder_volumes:
            cinder_volume_ids = [vol['id'] for vol in cinder_volumes]

        for ai in app_instances:
            ai_name = ai['name']
            reference = None
            size = None
            safe_to_manage = False
            reason_not_safe = ""
            cinder_id = None
            extra_info = {}
            (safe_to_manage, reason_not_safe,
                cinder_id) = self._is_manageable_2_2(ai, cinder_volume_ids)
            si = ai['storage_instances'][0]
            si_name = si['name']
            vol = si['volumes'][0]
            vol_name = vol['name']
            size = vol['size']
            snaps = [(snap['utc_ts'], snap['uuid'])
                     for snap in vol['snapshots']]
            extra_info["snapshots"] = json.dumps(snaps)
            reference = {"source-name": "{}:{}:{}".format(
                ai_name, si_name, vol_name)}

            results.append({
                'reference': reference,
                'size': size,
                'safe_to_manage': safe_to_manage,
                'reason_not_safe': _(reason_not_safe),
                'cinder_id': cinder_id,
                'extra_info': extra_info})
        return results

    def _get_manageable_volumes_2_2(self, cinder_volumes, marker, limit,
                                    offset, sort_keys, sort_dirs):
        LOG.debug("Listing manageable Datera volumes")
        results = self._list_manageable_2_2(cinder_volumes)
        page_results = volutils.paginate_entries_list(
            results, marker, limit, offset, sort_keys, sort_dirs)

        return page_results

    def _is_manageable_2_2(self, app_inst, cinder_volume_ids):
        cinder_id = None
        ai_name = app_inst['name']
        if datc.UUID4_RE.match(ai_name):
            cinder_id = ai_name.lstrip(datc.OS_PREFIX)
        if cinder_id and cinder_id in cinder_volume_ids:
            return (False,
                    "App Instance already managed by Cinder",
                    cinder_id)
        if len(app_inst['storage_instances']) == 1:
            si = app_inst['storage_instances'][0]
            if len(si['volumes']) == 1:
                return (True, "", cinder_id)
        return (False,
                "App Instance has more than one storage instance or volume",
                cinder_id)
    # ============
    # = Unmanage =
    # ============

    def _unmanage_2_2(self, volume):
        LOG.debug("Unmanaging Cinder volume %s.  Changing name to %s",
                  volume['id'], datc._get_unmanaged(volume['id']))
        data = {'name': datc._get_unmanaged(volume['id'])}
        self._api22(datc.URL_T['ai_inst'](
            datc._get_name(volume['id'])),
            'put',
            volume['project_id'],
            body=data)

    # ===================
    # = Manage Snapshot =
    # ===================

    def _manage_existing_snapshot_2_2(self, snapshot, existing_ref):
        existing_ref = existing_ref['source-name']
        datc._check_snap_ref(existing_ref)
        LOG.debug("Managing existing Datera volume snapshot %s for volume %s",
                  existing_ref, datc._get_name(snapshot['volume_id']))
        return {'provider_location': existing_ref}

    def _manage_existing_snapshot_get_size_2_2(self, snapshot, existing_ref):
        existing_ref = existing_ref['source-name']
        datc._check_snap_ref(existing_ref)
        app_inst = self._api22(
            datc.URL_T['ai_inst'](
                datc._get_name(snapshot['volume_id'])),
            'get',
            snapshot['project_id'])
        return datc._get_size(app_inst=app_inst)

    def _get_manageable_snapshots_2_2(self, cinder_snapshots, marker, limit,
                                      offset, sort_keys, sort_dirs):
        LOG.debug("Listing manageable Datera snapshots")
        results = self._list_manageable_2_2(cinder_snapshots)
        snap_results = []
        snapids = set((snap['id'] for snap in cinder_snapshots))
        snaprefs = set((snap.get('provider_location')
                        for snap in cinder_snapshots))
        for volume in results:
            snaps = json.loads(volume["extra_info"]["snapshots"])
            for snapshot in snaps:
                reference = snapshot[0]
                uuid = snapshot[1]
                size = volume["size"]
                safe_to_manage = True
                reason_not_safe = ""
                cinder_id = ""
                extra_info = {}
                source_reference = volume["reference"]
                if uuid in snapids or reference in snaprefs:
                    safe_to_manage = False
                    reason_not_safe = _("already managed by Cinder")
                elif not volume['safe_to_manage'] and not volume['cinder_id']:
                    safe_to_manage = False
                    reason_not_safe = _("parent volume not safe to manage")
                snap_results.append({
                    'reference': {'source-name': reference},
                    'size': size,
                    'safe_to_manage': safe_to_manage,
                    'reason_not_safe': reason_not_safe,
                    'cinder_id': cinder_id,
                    'extra_info': extra_info,
                    'source_reference': source_reference})
        page_results = volutils.paginate_entries_list(
            snap_results, marker, limit, offset, sort_keys, sort_dirs)

        return page_results

    def _unmanage_snapshot_2_2(self, snapshot):
        return {'provider_location': None}

    # ====================
    # = Fast Image Clone =
    # ====================

    def _clone_image_2_2(self, context, volume, image_location, image_meta,
                         image_service):
        # We're not going to fast image clone if the feature is not enabled
        # and/or we can't reach the image being requested
        if (not self.image_cache or
                not self._image_accessible(context, volume, image_meta)):
            return None, False
        # Check to make sure we're working with a valid volume type
        try:
            found = volume_types.get_volume_type(context, self.image_type)
        except (exception.VolumeTypeNotFound, exception.InvalidVolumeType):
            found = None
        if not found:
            msg = _("Invalid volume type: %s")
            LOG.error(msg, self.image_type)
            raise ValueError("Option datera_image_cache_volume_type_id must be"
                             " set to a valid volume_type id")
        # Check image format
        fmt = image_meta.get('disk_format', '')
        if fmt.lower() != 'raw':
            LOG.debug("Image format is not RAW, image requires conversion "
                      "before clone.  Image format: [%s]", fmt)
            return None, False

        LOG.debug("Starting fast image clone")
        # TODO(_alastor_): determine if Datera is already an image backend
        # for this request and direct clone instead of caching

        # Dummy volume, untracked by Cinder
        src_vol = {'id': image_meta['id'],
                   'volume_type_id': self.image_type,
                   'size': volume['size'],
                   'project_id': volume['project_id']}

        # Determine if we have a cached version of the image
        cached = self._vol_exists_2_2(src_vol)

        if cached:
            metadata = self._get_metadata_2_2(src_vol)
            # Check to see if the master image has changed since we created
            # The cached version
            ts = self._get_vol_timestamp_2_2(src_vol)
            mts = time.mktime(image_meta['updated_at'].timetuple())
            LOG.debug("Original image timestamp: %s, cache timestamp %s",
                      mts, ts)
            # If the image is created by Glance, we'll trust that even if the
            # timestamps don't match up, the data is ok to clone as it's not
            # managed by this driver
            if metadata.get('type') == 'image':
                LOG.debug("Found Glance volume-backed image for %s",
                          src_vol['id'])
            # If the master image time is greater than the volume creation
            # time, we invalidate the cache and delete the volume.  The
            # exception is if the cached volume was created by Glance.  We
            # NEVER want to delete this volume.  It's annotated with
            # 'type': 'image' in the metadata, so we'll check for that
            elif mts > ts and metadata.get('type') != 'image':
                LOG.debug("Cache is older than original image, deleting cache")
                cached = False
                self._delete_volume_2_2(src_vol)

        # If we don't have the image, we'll cache it
        if not cached:
            LOG.debug("No image cache found for: %s, caching image",
                      image_meta['id'])
            self._cache_vol(context, src_vol, image_meta, image_service)

        # Now perform the clone of the found image or newly cached image
        self._create_cloned_volume_2_2(volume, src_vol)
        # Force volume resize
        vol_size = volume['size']
        volume['size'] = 0
        self._extend_volume_2_2(volume, vol_size)
        volume['size'] = vol_size
        # Determine if we need to retype the newly created volume
        vtype_id = volume.get('volume_type_id')
        if vtype_id and self.image_type and vtype_id != self.image_type:
            vtype = volume_types.get_volume_type(context, vtype_id)
            LOG.debug("Retyping newly cloned volume from type: %s to type: %s",
                      self.image_type, vtype_id)
            diff, discard = volume_types.volume_types_diff(
                context, self.image_type, vtype_id)
            host = {'capabilities': {'vendor_name': self.backend_name}}
            self._retype_2_2(context, volume, vtype, diff, host)
        return None, True

    def _cache_vol(self, context, vol, image_meta, image_service):
        image_id = image_meta['id']
        # Pull down image and determine if valid
        with image_utils.TemporaryImages.fetch(image_service,
                                               context,
                                               image_id) as tmp_image:
            data = image_utils.qemu_img_info(tmp_image)
            fmt = data.file_format
            if fmt is None:
                raise exception.ImageUnacceptable(
                    reason=_("'qemu-img info' parsing failed."),
                    image_id=image_id)

            backing_file = data.backing_file
            if backing_file is not None:
                raise exception.ImageUnacceptable(
                    image_id=image_id,
                    reason=_("fmt=%(fmt)s backed by:%(backing_file)s")
                    % {'fmt': fmt, 'backing_file': backing_file, })

            vsize = int(
                math.ceil(float(data.virtual_size) / units.Gi))
            vol['size'] = vsize
            vtype = vol['volume_type_id']
            LOG.info("Creating cached image with volume type: %(vtype)s and "
                     "size %(size)s", {'vtype': vtype, 'size': vsize})
            self._create_volume_2_2(vol)
            with self._connect_vol(context, vol) as device:
                LOG.debug("Moving image %s to volume %s",
                          image_meta['id'], datc._get_name(vol['id']))
                image_utils.convert_image(tmp_image,
                                          device,
                                          'raw',
                                          run_as_root=True)
                LOG.debug("Finished moving image %s to volume %s",
                          image_meta['id'], datc._get_name(vol['id']))
                data = image_utils.qemu_img_info(device, run_as_root=True)
                if data.file_format != 'raw':
                    raise exception.ImageUnacceptable(
                        image_id=image_id,
                        reason=_(
                            "Converted to %(vol_format)s, but format is "
                            "now %(file_format)s") % {
                                'vol_format': 'raw',
                                'file_format': data.file_format})
        # TODO(_alastor_): Remove this snapshot creation when we fix
        # "created_at" attribute in the frontend
        # We don't actually care about the snapshot uuid, we just want
        # a single snapshot
        snapshot = {'id': str(uuid.uuid4()),
                    'volume_id': vol['id']}
        self._create_snapshot_2_2(snapshot)
        self._update_metadata_2_2(vol, {'type': 'cached_image'})
        # Cloning offline AI is ~4 seconds faster than cloning online AI
        self._detach_volume_2_2(None, vol)

    def _get_vol_timestamp_2_2(self, volume):
        store_name, vol_name = self._scrape_ai_2_2(volume)

        snapu = datc.URL_T['vol_inst'](
            datc._get_name(volume['id']), store_name, vol_name) + '/snapshots'
        snapshots = self._api22(snapu, 'get',
                                volume['project_id'])
        if len(snapshots['data']) == 1:
            return float(snapshots['data'][0]['utc_ts'])
        else:
            # We'll return 0 if we find no snapshots (or the incorrect number)
            # to ensure the timestamp comparison with the master copy fails
            # since the master copy will always have a timestamp > 0.
            LOG.debug("Number of snapshots found: %s", len(snapshots['data']))
            return 0

    def _vol_exists_2_2(self, volume):
        LOG.debug("Checking if volume %s exists", volume['id'])
        try:
            return self._api22(
                datc.URL_T['ai_inst'](datc._get_name(volume['id'])),
                'get',  volume['project_id'])
            LOG.debug("Volume %s exists", volume['id'])
        except exception.NotFound:
            LOG.debug("Volume %s not found", volume['id'])
            return {}

    @contextlib.contextmanager
    def _connect_vol(self, context, vol):
        connector = None
        try:
            # Start connection, get the connector object and create the
            # export (ACL, IP-Pools, etc)
            conn = self._initialize_connection_2_2(
                vol, {'multipath': False})
            connector = utils.brick_get_connector(
                conn['driver_volume_type'],
                use_multipath=False,
                device_scan_attempts=10,
                conn=conn)
            connector_info = {'initiator': connector.get_initiator()}
            self._create_export_2_2(None, vol, connector_info)
            retries = 10
            attach_info = conn['data']
            while True:
                try:
                    attach_info.update(
                        connector.connect_volume(conn['data']))
                    break
                except brick_exception.FailedISCSITargetPortalLogin:
                    retries -= 1
                    if not retries:
                        LOG.error(_("Could not log into portal before end of "
                                    "polling period"))
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

    # =========
    # = Login =
    # =========

    def _login_2_2(self):
        """Use the san_login and san_password to set token."""
        body = {
            'name': self.username,
            'password': self.password
        }

        if self.ldap:
            body['remote_server'] = self.ldap

        # Unset token now, otherwise potential expired token will be sent
        # along to be used for authorization when trying to login.
        self.datera_api_token = None

        try:
            LOG.debug('Getting Datera auth token.')
            results = self._api22(
                'login', 'put',  'LOGIN', body=body, sensitive=True)
            self.datera_api_token = results['key']
        except exception.NotAuthorized:
            with excutils.save_and_reraise_exception():
                LOG.error('Logging into the Datera cluster failed. Please '
                          'check your username and password set in the '
                          'cinder.conf and start the cinder-volume '
                          'service again.')

    # ===========
    # = Polling =
    # ===========

    def _snap_poll_2_2(self, url, project_id):
        eventlet.sleep(datc.DEFAULT_SNAP_SLEEP)
        TIMEOUT = 20
        retry = 0
        poll = True
        while poll and not retry >= TIMEOUT:
            retry += 1
            snap = self._api22(url, 'get',
                               project_id)['data']
            if snap['op_state'] == 'available':
                poll = False
            else:
                eventlet.sleep(1)
        if retry >= TIMEOUT:
            raise exception.VolumeDriverException(
                message=_('Snapshot not ready.'))

    def _si_poll_2_2(self, volume, si):
        # Initial 4 second sleep required for some Datera versions
        eventlet.sleep(datc.DEFAULT_SI_SLEEP)
        TIMEOUT = 10
        retry = 0
        check_url = datc.URL_T['si_inst'](
            datc._get_name(volume['id']), si)
        poll = True
        while poll and not retry >= TIMEOUT:
            retry += 1
            si = self._api22(check_url, 'get',
                             volume['project_id'])[
                'data']
            if si['op_state'] == 'available':
                poll = False
            else:
                eventlet.sleep(1)
        if retry >= TIMEOUT:
            raise exception.VolumeDriverException(
                message=_('Resource not ready.'))

    # ================
    # = Volume Stats =
    # ================

    def _get_volume_stats_2_2(self, refresh=False):
        if refresh or not self.cluster_stats:
            try:
                LOG.debug("Updating cluster stats info.")

                results = self._api22('system', 'get',  'STATS')['data']

                if 'uuid' not in results:
                    LOG.error(
                        'Failed to get updated stats from Datera Cluster.')

                stats = {
                    'volume_backend_name': self.backend_name,
                    'vendor_name': 'Datera',
                    'driver_version': self.VERSION,
                    'storage_protocol': 'iSCSI',
                    'total_capacity_gb': (
                        int(results['total_capacity']) / units.Gi),
                    'free_capacity_gb': (
                        int(results['available_capacity']) / units.Gi),
                    'reserved_percentage': 0,
                    'QoS_support': True,
                }

                self.cluster_stats = stats
            except exception.DateraAPIException:
                LOG.error('Failed to get updated stats from Datera cluster.')
        return self.cluster_stats

    # =======
    # = QoS =
    # =======

    def _update_qos_2_2(self, volume, policies, clear_old=False):
        si, vol = self._scrape_ai_2_2(volume)
        url = datc.URL_T['vol_inst'](datc._get_name(volume['id']), si, vol)
        url += '/performance_policy'
        type_id = volume.get('volume_type_id', None)
        if type_id is not None:
            iops_per_gb = int(policies.get('iops_per_gb', 0))
            bandwidth_per_gb = int(policies.get('bandwidth_per_gb', 0))
            # Filter for just QOS policies in result. All of their keys
            # should end with "max"
            fpolicies = {k: int(v) for k, v in
                         policies.items() if k.endswith("max")}
            # Filter all 0 values from being passed
            fpolicies = dict(filter(lambda _v: _v[1] > 0, fpolicies.items()))
            # Calculate and set iops/gb and bw/gb, but only if they don't
            # exceed total_iops_max and total_bw_max aren't set since they take
            # priority
            if iops_per_gb:
                ipg = iops_per_gb * volume['size']
                # Not using zero, because zero means unlimited
                im = fpolicies.get('total_iops_max', 1)
                r = ipg
                if ipg > im:
                    r = im
                fpolicies['total_iops_max'] = r
            if bandwidth_per_gb:
                bpg = bandwidth_per_gb * volume['size']
                # Not using zero, because zero means unlimited
                bm = fpolicies.get('total_bandwidth_max', 1)
                r = bpg
                if bpg > bm:
                    r = bm
                fpolicies['total_bandwidth_max'] = r
            if fpolicies or clear_old:
                try:
                    self._api22(
                        url, 'delete',  volume['project_id'])
                except exception.NotFound:
                    LOG.debug("No existing performance policy found")
            if fpolicies:
                self._api22(url, 'post',
                            volume['project_id'], body=fpolicies)

    # ============
    # = IP Pools =
    # ============

    def _get_ip_pool_for_string_ip_2_2(self, ip, project_id):
        """Takes a string ipaddress and return the ip_pool API object dict """
        pool = 'default'
        ip_obj = ipaddress.ip_address(six.text_type(ip))
        ip_pools = self._api22('access_network_ip_pools',
                               'get',

                               project_id)
        for ipdata in ip_pools['data']:
            for adata in ipdata['network_paths']:
                if not adata.get('start_ip'):
                    continue
                pool_if = ipaddress.ip_interface(
                    "/".join((adata['start_ip'], str(adata['netmask']))))
                if ip_obj in pool_if.network:
                    pool = ipdata['name']
        return self._api22(
            "access_network_ip_pools/{}".format(pool), 'get',
            project_id)['data']['path']

    # ====================
    # = Volume Migration =
    # ====================

    def _update_migrated_volume_2_2(self, context, volume, new_volume,
                                    volume_status):
        """Rename the newly created volume to the original volume so we
           can find it correctly"""
        url = datc.URL_T['ai_inst'](datc._get_name(new_volume['id']))
        data = {'name': datc._get_name(volume['id'])}
        self._api22(url, 'put',  volume['project_id'],
                    body=data)
        return {'_name_id': None}

    # ============
    # = Metadata =
    # ============

    def _get_metadata_2_2(self, volume):
        url = datc.URL_T['ai_inst'](datc._get_name(volume['id']))
        url += "/metadata"
        return self._api22(url, 'get',
                           volume['project_id'])['data']

    def _update_metadata_2_2(self, volume, keys):
        url = datc.URL_T['ai_inst'](datc._get_name(volume['id']))
        url += "/metadata"
        self._api22(
            url, 'put',  volume['project_id'], body=keys)

    @contextlib.contextmanager
    def _detach_flip_2_2(self, volume):
        # Offline App Instance, if necessary
        reonline = False
        app_inst = self._api22(
            datc.URL_T['ai_inst'](datc._get_name(volume['id'])),
            'get',  volume['project_id'])
        if app_inst['data']['admin_state'] == 'online':
            reonline = True
        self._detach_volume_2_2(None, volume)
        yield
        # Online Volume, if it was online before
        if reonline:
            self._create_export_2_2(None, volume, None)

    @contextlib.contextmanager
    def _offline_flip_2_2(self, volume):
        reonline = False
        app_inst = self._api22(
            datc.URL_T['ai_inst'](datc._get_name(volume['id'])), 'get',
            volume['project_id'])
        if app_inst['data']['admin_state'] == 'online':
            reonline = True
        data = {'admin_state': 'offline'}
        self._api22(datc.URL_T['ai_inst'](
            datc._get_name(volume['id'])), 'put', volume['project_id'],
            body=data)
        yield
        if reonline:
            data = {'admin_state': 'online'}
            self._api22(datc.URL_T['ai_inst'](
                datc._get_name(volume['id'])), 'put',
                volume['project_id'], body=data)

    def _add_vol_meta_2_2(self, volume, connector=None):
        if not self.do_metadata:
            return
        metadata = {'host': volume.get('host', ''),
                    'display_name': volume.get('display_name', ''),
                    'bootable': str(volume.get('bootable', False)),
                    'availability_zone': volume.get('availability_zone', '')}
        if connector:
            metadata.update(connector)
        LOG.debug("Adding volume metadata: %s", metadata)
        self._update_metadata_2_2(volume, metadata)

    def _scrape_ai_2_2(self, volume):
        ai = self._api22(datc.URL_T['ai_inst'](
            datc._get_name(volume['id'])), 'get',
            volume['project_id'])['data']
        si = ai['storage_instances'][0]
        sname = si['name']
        vname = si['volumes'][0]['name']
        return sname, vname
