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
import re
import time
import uuid

import eventlet
import ipaddress
import six

from oslo_log import log as logging
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

    # =================
    # = Create Volume =
    # =================

    def _create_volume_2_2(self, volume):
        tenant = self._create_tenant_2_2(volume)
        policies = self._get_policies_for_resource(volume)
        num_replicas = int(policies['replica_count'])
        storage_name = policies['default_storage_name']
        volume_name = policies['default_volume_name']
        template = policies['template']
        placement = policies['placement_mode']

        if template:
            app_params = (
                {
                    'create_mode': "openstack",
                    # 'uuid': str(volume['id']),
                    'name': datc._get_name(volume['id']),
                    'app_template': '/app_templates/{}'.format(template)
                })

        else:

            app_params = (
                {
                    'create_mode': "openstack",
                    'uuid': str(volume['id']),
                    'name': datc._get_name(volume['id']),
                    'access_control_mode': 'deny_all',
                    'storage_instances': [
                        {
                            'name': storage_name,
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
        self._issue_api_request(
            datc.URL_TEMPLATES['ai'](),
            'post',
            body=app_params,
            api_version=API_VERSION,
            tenant=tenant)
        self._update_qos_2_2(volume, policies, tenant)

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
        tenant = self._create_tenant_2_2(volume)
        policies = self._get_policies_for_resource(volume)
        template = policies['template']
        if template:
            LOG.warning("Volume size not extended due to template binding:"
                        " volume: %(volume)s, template: %(template)s",
                        {'volume': volume, 'template': template})
            return

        # Offline App Instance, if necessary
        reonline = False
        app_inst = self._issue_api_request(
            datc.URL_TEMPLATES['ai_inst']().format(
                datc._get_name(volume['id'])),
            api_version=API_VERSION, tenant=tenant)
        if app_inst['data']['admin_state'] == 'online':
            reonline = True
            self._detach_volume_2_2(None, volume)
        # Change Volume Size
        app_inst = datc._get_name(volume['id'])
        data = {
            'size': new_size
        }
        store_name, vol_name = self._scrape_template(policies)
        self._issue_api_request(
            datc.URL_TEMPLATES['vol_inst'](
                store_name, vol_name).format(app_inst),
            method='put',
            body=data,
            api_version=API_VERSION,
            tenant=tenant)
        # Online Volume, if it was online before
        if reonline:
            self._create_export_2_2(None, volume, None)

    # =================
    # = Cloned Volume =
    # =================

    def _create_cloned_volume_2_2(self, volume, src_vref):
        policies = self._get_policies_for_resource(volume)
        tenant = self._create_tenant_2_2(volume)
        store_name, vol_name = self._scrape_template(policies)

        src = "/" + datc.URL_TEMPLATES['vol_inst'](
            store_name, vol_name).format(datc._get_name(src_vref['id']))
        data = {
            'create_mode': 'openstack',
            'name': datc._get_name(volume['id']),
            'uuid': str(volume['id']),
            'clone_volume_src': {'path': src},
        }
        self._issue_api_request(
            datc.URL_TEMPLATES['ai'](), 'post', body=data,
            api_version=API_VERSION, tenant=tenant)

        if volume['size'] > src_vref['size']:
            self._extend_volume_2_2(volume, volume['size'])

    # =================
    # = Delete Volume =
    # =================

    def _delete_volume_2_2(self, volume):
        self._detach_volume_2_2(None, volume)
        tenant = self._create_tenant_2_2(volume)
        app_inst = datc._get_name(volume['id'])
        try:
            self._issue_api_request(
                datc.URL_TEMPLATES['ai_inst']().format(app_inst),
                method='delete',
                api_version=API_VERSION,
                tenant=tenant)
        except exception.NotFound:
            msg = ("Tried to delete volume %s, but it was not found in the "
                   "Datera cluster. Continuing with delete.")
            LOG.info(msg, datc._get_name(volume['id']))

    # =================
    # = Ensure Export =
    # =================

    def _ensure_export_2_2(self, context, volume, connector=None):
        self.create_export(context, volume, connector)

    # =========================
    # = Initialize Connection =
    # =========================

    def _initialize_connection_2_2(self, volume, connector):
        # Now online the app_instance (which will online all storage_instances)
        multipath = connector.get('multipath', False)
        tenant = self._create_tenant_2_2(volume)
        url = datc.URL_TEMPLATES['ai_inst']().format(
            datc._get_name(volume['id']))
        data = {
            'admin_state': 'online'
        }
        app_inst = self._issue_api_request(
            url, method='put', body=data, api_version=API_VERSION,
            tenant=tenant)['data']
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

        return result

    # =================
    # = Create Export =
    # =================

    def _create_export_2_2(self, context, volume, connector):
        tenant = self._create_tenant_2_2(volume)
        url = datc.URL_TEMPLATES['ai_inst']().format(
            datc._get_name(volume['id']))
        data = {
            'admin_state': 'offline',
            'force': True
        }
        self._issue_api_request(
            url, method='put', body=data, api_version=API_VERSION,
            tenant=tenant)
        policies = self._get_policies_for_resource(volume)
        store_name, _ = self._scrape_template(policies)
        if connector and connector.get('ip'):
            # Case where volume_type has non default IP Pool info
            if policies['ip_pool'] != 'default':
                initiator_ip_pool_path = self._issue_api_request(
                    "access_network_ip_pools/{}".format(
                        policies['ip_pool']),
                    api_version=API_VERSION,
                    tenant=tenant)['path']
            # Fallback to trying reasonable IP based guess
            else:
                initiator_ip_pool_path = self._get_ip_pool_for_string_ip_2_2(
                    connector['ip'], tenant)

            ip_pool_url = datc.URL_TEMPLATES['si_inst'](
                store_name).format(datc._get_name(volume['id']))
            ip_pool_data = {'ip_pool': {'path': initiator_ip_pool_path}}
            self._issue_api_request(ip_pool_url,
                                    method="put",
                                    body=ip_pool_data,
                                    api_version=API_VERSION,
                                    tenant=tenant)
        url = datc.URL_TEMPLATES['ai_inst']().format(
            datc._get_name(volume['id']))
        data = {
            'admin_state': 'online'
        }
        self._issue_api_request(
            url, method='put', body=data, api_version=API_VERSION,
            tenant=tenant)
        # Check if we've already setup everything for this volume
        url = (datc.URL_TEMPLATES['si']().format(datc._get_name(volume['id'])))
        storage_instances = self._issue_api_request(
            url, api_version=API_VERSION, tenant=tenant)
        # Handle adding initiator to product if necessary
        # Then add initiator to ACL
        if (connector and
                connector.get('initiator') and
                not policies['acl_allow_all']):
            initiator_name = "OpenStack_{}_{}".format(
                self.driver_prefix, str(uuid.uuid4())[:4])
            initiator_group = datc.INITIATOR_GROUP_PREFIX + str(uuid.uuid4())
            found = False
            initiator = connector['initiator']
            if not found:
                # TODO(_alastor_): Take out the 'force' flag when we fix
                # DAT-15931
                data = {'id': initiator, 'name': initiator_name, 'force': True}
                # Try and create the initiator
                # If we get a conflict, ignore it
                self._issue_api_request("initiators",
                                        method="post",
                                        body=data,
                                        conflict_ok=True,
                                        api_version=API_VERSION,
                                        tenant=tenant)
            # Create initiator group with initiator in it
            initiator_path = "/initiators/{}".format(initiator)
            initiator_group_path = "/initiator_groups/{}".format(
                initiator_group)
            ig_data = {'name': initiator_group,
                       'members': [{'path': initiator_path}]}
            self._issue_api_request("initiator_groups",
                                    method="post",
                                    body=ig_data,
                                    conflict_ok=True,
                                    api_version=API_VERSION,
                                    tenant=tenant)
            # Create ACL with initiator group as reference for each
            # storage_instance in app_instance
            # TODO(_alastor_): We need to avoid changing the ACLs if the
            # template already specifies an ACL policy.
            for si in storage_instances['data']:
                acl_url = (datc.URL_TEMPLATES['si']() +
                           "/{}/acl_policy").format(
                    datc._get_name(volume['id']), si['name'])
                existing_acl = self._issue_api_request(
                        acl_url, method="get", api_version=API_VERSION,
                        tenant=tenant)['data']
                data = {}
                # Grabbing only the 'path' key from each existing initiator
                # within the existing acl. eacli --> existing acl initiator
                eacli = []
                for acl in existing_acl['initiators']:
                    nacl = {}
                    nacl['path'] = acl['path']
                    eacli.append(nacl)
                data['initiators'] = eacli
                # Grabbing only the 'path' key from each existing initiator
                # group within the existing acl. eaclig --> existing
                # acl initiator group
                eaclig = []
                for acl in existing_acl['initiator_groups']:
                    nacl = {}
                    nacl['path'] = acl['path']
                    eaclig.append(nacl)
                data['initiator_groups'] = eaclig
                data['initiator_groups'].append({"path": initiator_group_path})
                self._issue_api_request(acl_url,
                                        method="put",
                                        body=data,
                                        api_version=API_VERSION,
                                        tenant=tenant)
        # Check to ensure we're ready for go-time
        self._si_poll_2_2(volume, policies, tenant)

    # =================
    # = Detach Volume =
    # =================

    def _detach_volume_2_2(self, context, volume, attachment=None):
        tenant = self._create_tenant_2_2(volume)
        url = datc.URL_TEMPLATES['ai_inst']().format(
            datc._get_name(volume['id']))
        data = {
            'admin_state': 'offline',
            'force': True
        }
        try:
            self._issue_api_request(
                url, method='put', body=data, api_version=API_VERSION,
                tenant=tenant)
        except exception.NotFound:
            msg = ("Tried to detach volume %s, but it was not found in the "
                   "Datera cluster. Continuing with detach.")
            LOG.info(msg, volume['id'])
        # TODO(_alastor_): Make acl cleaning multi-attach aware
        self._clean_acl_2_2(volume, tenant)

    def _check_for_acl_2_2(self, initiator_path):
        """Returns True if an acl is found for initiator_path """
        # TODO(_alastor_) when we get a /initiators/:initiator/acl_policies
        # endpoint use that instead of this monstrosity
        initiator_groups = self._issue_api_request(
            "initiator_groups", api_version=API_VERSION)
        for ig, igdata in initiator_groups.items():
            if initiator_path in igdata['members']:
                LOG.debug("Found initiator_group: %s for initiator: %s",
                          ig, initiator_path)
                return True
        LOG.debug("No initiator_group found for initiator: %s", initiator_path)
        return False

    def _clean_acl_2_2(self, volume, tenant):
        policies = self._get_policies_for_resource(volume)

        store_name, _ = self._scrape_template(policies)

        acl_url = (datc.URL_TEMPLATES["si_inst"](
            store_name) + "/acl_policy").format(datc._get_name(volume['id']))
        try:
            initiator_group = self._issue_api_request(
                acl_url, api_version=API_VERSION, tenant=tenant)['data'][
                    'initiator_groups'][0]['path']
            # TODO(_alastor_): Re-enable this when we get a force-delete
            # option on the /initiators endpoint
            # initiator_iqn_path = self._issue_api_request(
            #     initiator_group.lstrip("/"), api_version=API_VERSION,
            #     tenant=tenant)[
            #         "data"]["members"][0]["path"]
            # Clear out ACL and delete initiator group
            self._issue_api_request(acl_url,
                                    method="put",
                                    body={'initiator_groups': []},
                                    api_version=API_VERSION,
                                    tenant=tenant)
            self._issue_api_request(initiator_group.lstrip("/"),
                                    method="delete",
                                    api_version=API_VERSION,
                                    tenant=tenant)
            # TODO(_alastor_): Re-enable this when we get a force-delete
            # option on the /initiators endpoint
            # if not self._check_for_acl_2_2(initiator_iqn_path):
            #     self._issue_api_request(initiator_iqn_path.lstrip("/"),
            #                             method="delete",
            #                             api_version=API_VERSION,
            #                             tenant=tenant)
        except (IndexError, exception.NotFound):
            LOG.debug("Did not find any initiator groups for volume: %s",
                      volume)

    # ===================
    # = Create Snapshot =
    # ===================

    def _create_snapshot_2_2(self, snapshot):
        tenant = self._create_tenant_2_2(snapshot)
        policies = self._get_policies_for_resource(snapshot)

        store_name, vol_name = self._scrape_template(policies)

        url_template = datc.URL_TEMPLATES['vol_inst'](
            store_name, vol_name) + '/snapshots'
        url = url_template.format(datc._get_name(snapshot['volume_id']))

        snap_params = {
            'uuid': snapshot['id'],
        }
        snap = self._issue_api_request(
            url, method='post', body=snap_params, api_version=API_VERSION,
            tenant=tenant)
        snapu = "/".join((url, snap['data']['timestamp']))
        self._snap_poll_2_2(snapu, tenant)

    # ===================
    # = Delete Snapshot =
    # ===================

    def _delete_snapshot_2_2(self, snapshot):
        tenant = self._create_tenant_2_2(snapshot)
        policies = self._get_policies_for_resource(snapshot)

        store_name, vol_name = self._scrape_template(policies)

        snap_temp = datc.URL_TEMPLATES['vol_inst'](
            store_name, vol_name) + '/snapshots'
        snapu = snap_temp.format(datc._get_name(snapshot['volume_id']))
        snapshots = []
        try:
            snapshots = self._issue_api_request(snapu,
                                                method='get',
                                                api_version=API_VERSION,
                                                tenant=tenant)
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
                    self._issue_api_request(
                        url,
                        method='delete',
                        api_version=API_VERSION,
                        tenant=tenant)
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
        tenant = self._create_tenant_2_2(volume)
        policies = self._get_policies_for_resource(snapshot)

        store_name, vol_name = self._scrape_template(policies)

        snap_temp = datc.URL_TEMPLATES['vol_inst'](
            store_name, vol_name) + '/snapshots'
        snapu = snap_temp.format(datc._get_name(snapshot['volume_id']))
        snapshots = self._issue_api_request(
            snapu, method='get', api_version=API_VERSION, tenant=tenant)

        for snap in snapshots['data']:
            if snap['uuid'] == snapshot['id']:
                found_ts = snap['utc_ts']
                break
        else:
            raise exception.NotFound

        snap_url = (snap_temp + '/{}').format(
            datc._get_name(snapshot['volume_id']), found_ts)

        self._snap_poll_2_2(snap_url, tenant)

        src = "/" + snap_url
        app_params = (
            {
                'create_mode': 'openstack',
                'uuid': str(volume['id']),
                'name': datc._get_name(volume['id']),
                'clone_snapshot_src': {'path': src},
            })
        self._issue_api_request(
            datc.URL_TEMPLATES['ai'](),
            method='post',
            body=app_params,
            api_version=API_VERSION,
            tenant=tenant)

        if (volume['size'] > snapshot['volume_size']):
            self._extend_volume_2_2(volume, volume['size'])

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

            tenant = self._create_tenant_2_2(volume)
            self._update_qos_2_2(volume, new_pol, tenant, clear_old=True)
            vol_params = (
                {
                    'placement_mode': new_pol['placement_mode'],
                    'replica_count': new_pol['replica_count'],
                })
            url = datc.URL_TEMPLATES['vol_inst'](
                old_pol['default_storage_name'],
                old_pol['default_volume_name']).format(
                    datc._get_name(volume['id']))
            self._issue_api_request(
                url, method='put', body=vol_params,
                api_version=API_VERSION, tenant=tenant)
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
        tenant = self._create_tenant_2_2(volume)
        existing_ref = existing_ref['source-name']
        if existing_ref.count(":") not in (2, 3):
            raise exception.ManageExistingInvalidReference(
                _("existing_ref argument must be of this format: "
                  "tenant:app_inst_name:storage_inst_name:vol_name or "
                  "app_inst_name:storage_inst_name:vol_name"))
        app_inst_name = existing_ref.split(":")[0]
        try:
            (tenant, app_inst_name, storage_inst_name,
                vol_name) = existing_ref.split(":")
        except TypeError:
            app_inst_name, storage_inst_name, vol_name = existing_ref.split(
                ":")
            tenant = None
        LOG.debug("Managing existing Datera volume %s  "
                  "Changing name to %s",
                  datc._get_name(volume['id']), existing_ref)
        data = {'name': datc._get_name(volume['id'])}
        self._issue_api_request(datc.URL_TEMPLATES['ai_inst']().format(
            app_inst_name), method='put', body=data,
            api_version=API_VERSION, tenant=tenant)

    # ===================
    # = Manage Get Size =
    # ===================

    def _manage_existing_get_size_2_2(self, volume, existing_ref):
        tenant = self._create_tenant_2_2(volume)
        existing_ref = existing_ref['source-name']
        if existing_ref.count(":") != 2:
            raise exception.ManageExistingInvalidReference(
                _("existing_ref argument must be of this format:"
                  "app_inst_name:storage_inst_name:vol_name"))
        app_inst_name, si_name, vol_name = existing_ref.split(":")
        app_inst = self._issue_api_request(
            datc.URL_TEMPLATES['ai_inst']().format(app_inst_name),
            api_version=API_VERSION, tenant=tenant)
        return self._get_size_2_2(
            volume, tenant, app_inst, si_name, vol_name)

    def _get_size_2_2(self, volume, tenant=None, app_inst=None, si_name=None,
                      vol_name=None):
        """Helper method for getting the size of a backend object

        If app_inst is provided, we'll just parse the dict to get
        the size instead of making a separate http request
        """
        policies = self._get_policies_for_resource(volume)
        si_name = si_name if si_name else policies['default_storage_name']
        vol_name = vol_name if vol_name else policies['default_volume_name']
        if not app_inst:
            vol_url = datc.URL_TEMPLATES['ai_inst']().format(
                datc._get_name(volume['id']))
            app_inst = self._issue_api_request(
                vol_url, api_version=API_VERSION, tenant=tenant)['data']
        if 'data' in app_inst:
            app_inst = app_inst['data']
        sis = app_inst['storage_instances']
        found_si = None
        for si in sis:
            if si['name'] == si_name:
                found_si = si
                break
        found_vol = None
        for vol in found_si['volumes']:
            if vol['name'] == vol_name:
                found_vol = vol
        size = found_vol['size']
        return size

    # =========================
    # = Get Manageable Volume =
    # =========================

    def _get_manageable_volumes_2_2(self, cinder_volumes, marker, limit,
                                    offset, sort_keys, sort_dirs):
        # Use the first volume to determine the tenant we're working under
        if cinder_volumes:
            tenant = self._create_tenant_2_2(cinder_volumes[0])
        else:
            tenant = None
        LOG.debug("Listing manageable Datera volumes")
        app_instances = self._issue_api_request(
            datc.URL_TEMPLATES['ai'](), api_version=API_VERSION,
            tenant=tenant)['data']

        results = []

        cinder_volume_ids = [vol['id'] for vol in cinder_volumes]

        for ai in app_instances:
            ai_name = ai['name']
            reference = None
            size = None
            safe_to_manage = False
            reason_not_safe = ""
            cinder_id = None
            extra_info = None
            if re.match(datc.UUID4_RE, ai_name):
                cinder_id = ai_name.lstrip(datc.OS_PREFIX)
            if (not cinder_id and
                    ai_name.lstrip(datc.OS_PREFIX) not in cinder_volume_ids):
                safe_to_manage, reason_not_safe = self._is_manageable_2_2(ai)
            if safe_to_manage:
                si = list(ai['storage_instances'].values())[0]
                si_name = si['name']
                vol = list(si['volumes'].values())[0]
                vol_name = vol['name']
                size = vol['size']
                reference = {"source-name": "{}:{}:{}".format(
                    ai_name, si_name, vol_name)}

            results.append({
                'reference': reference,
                'size': size,
                'safe_to_manage': safe_to_manage,
                'reason_not_safe': reason_not_safe,
                'cinder_id': cinder_id,
                'extra_info': extra_info})

        page_results = volutils.paginate_entries_list(
            results, marker, limit, offset, sort_keys, sort_dirs)

        return page_results

    def _is_manageable_2_2(self, app_inst):
        if len(app_inst['storage_instances']) == 1:
            si = list(app_inst['storage_instances'].values())[0]
            if len(si['volumes']) == 1:
                return (True, "")
        return (False,
                "App Instance has more than one storage instance or volume")
    # ============
    # = Unmanage =
    # ============

    def _unmanage_2_2(self, volume):
        tenant = self._create_tenant_2_2(volume)
        LOG.debug("Unmanaging Cinder volume %s.  Changing name to %s",
                  volume['id'], datc._get_unmanaged(volume['id']))
        data = {'name': datc._get_unmanaged(volume['id'])}
        self._issue_api_request(datc.URL_TEMPLATES['ai_inst']().format(
            datc._get_name(volume['id'])),
            method='put',
            body=data,
            api_version=API_VERSION,
            tenant=tenant)

    # ====================
    # = Fast Image Clone =
    # ====================

    def _clone_image_2_2(self, context, volume, image_location, image_meta,
                         image_service):
        # We're not going to fast image clone if the feature is not enabled
        # and/or we can't reach the image being requested
        if (not self.image_cache or
                not self._image_accessible(volume, image_meta)):
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

        LOG.debug("Starting fast image clone")
        # TODO(_alastor_): determine if Datera is already an image backend
        # for this request and direct clone instead of caching

        # Dummy volume, untracked by Cinder
        src_vol = {'id': image_meta['id'],
                   'volume_type_id': self.image_type,
                   'size': volume['size']}

        # Determine if we have a cached version of the image
        cached = self._vol_exists_2_2(src_vol)

        if cached:
            metadata = self._get_metadata(src_vol)
            # Check to see if the master image has changed since we created
            # The cached version
            ts = self._get_vol_timestamp(src_vol)
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
        self._update_metadata(vol, {'type': 'cached_image'})

    def _image_accessible(self, volume, image_meta):
        # Determine if image is accessible by current project
        public = False
        visibility = image_meta.get('visibility', None)
        if visibility and visibility == 'public':
            public = True
        elif image_meta.get('is_public', False):
            public = True
        else:
            if image_meta['owner'] == volume['project_id']:
                public = True
        if not public:
            LOG.warning("Requested image is not "
                        "accessible by current Project.")
        return public

    def _get_vol_timestamp(self, volume):
        tenant = self._create_tenant_2_2()
        policies = self._get_policies_for_resource(volume)
        store_name, vol_name = self._scrape_template(policies)

        snap_temp = datc.URL_TEMPLATES['vol_inst'](
            store_name, vol_name) + '/snapshots'
        snapu = snap_temp.format(datc._get_name(volume['id']))
        snapshots = self._issue_api_request(snapu,
                                            method='get',
                                            api_version=API_VERSION,
                                            tenant=tenant)
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
        tenant = self._create_tenant_2_2(volume)
        try:
            return self._issue_api_request(
                datc.URL_TEMPLATES['ai_inst']().format(
                    datc._get_name(volume['id'])),
                api_version=API_VERSION, tenant=tenant)
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

    # ===========
    # = Tenancy =
    # ===========

    def _create_tenant_2_2(self, volume=None):
        # Create the Datera tenant if specified in the config
        # Otherwise use the tenant provided
        if self.tenant_id is None:
            tenant = None
        elif self.tenant_id.lower() == "map" and volume:
            # Convert dashless uuid to uuid with dashes
            # Eg: 0e33e95a9b154d348c675a1d8ea5b651 -->
            #       0e33e95a-9b15-4d34-8c67-5a1d8ea5b651
            tenant = datc._get_name(str(uuid.UUID(volume["project_id"])))
        elif self.tenant_id.lower() == "map" and not volume:
            tenant = None
        else:
            tenant = self.tenant_id

        if tenant:
            params = {'name': tenant}
            self._issue_api_request(
                'tenants', method='post', body=params, conflict_ok=True,
                api_version=API_VERSION)
        return tenant

    # =========
    # = Login =
    # =========

    def _login_2_2(self):
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
                'login', 'put', body=body, sensitive=True,
                api_version=API_VERSION, tenant=None)
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

    def _snap_poll_2_2(self, url, tenant):
        eventlet.sleep(datc.DEFAULT_SNAP_SLEEP)
        TIMEOUT = 20
        retry = 0
        poll = True
        while poll and not retry >= TIMEOUT:
            retry += 1
            snap = self._issue_api_request(url,
                                           api_version=API_VERSION,
                                           tenant=tenant)['data']
            if snap['op_state'] == 'available':
                poll = False
            else:
                eventlet.sleep(1)
        if retry >= TIMEOUT:
            raise exception.VolumeDriverException(
                message=_('Snapshot not ready.'))

    def _si_poll_2_2(self, volume, policies, tenant):
        # Initial 4 second sleep required for some Datera versions
        eventlet.sleep(datc.DEFAULT_SI_SLEEP)
        TIMEOUT = 10
        retry = 0
        check_url = datc.URL_TEMPLATES['si_inst'](
            policies['default_storage_name']).format(
                datc._get_name(volume['id']))
        poll = True
        while poll and not retry >= TIMEOUT:
            retry += 1
            si = self._issue_api_request(check_url,
                                         api_version=API_VERSION,
                                         tenant=tenant)['data']
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

                results = self._issue_api_request(
                    'system', api_version=API_VERSION)['data']

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

    def _update_qos_2_2(self, resource, policies, tenant, clear_old=False):
        url = datc.URL_TEMPLATES['vol_inst'](
            policies['default_storage_name'],
            policies['default_volume_name']) + '/performance_policy'
        url = url.format(datc._get_name(resource['id']))
        type_id = resource.get('volume_type_id', None)
        if type_id is not None:
            iops_per_gb = int(policies.get('iops_per_gb', 0))
            bw_per_gb = int(policies.get('bw_per_gb', 0))
            # Filter for just QOS policies in result. All of their keys
            # should end with "max"
            fpolicies = {k: int(v) for k, v in
                         policies.items() if k.endswith("max")}
            # Filter all 0 values from being passed
            fpolicies = dict(filter(lambda _v: _v[1] > 0, fpolicies.items()))
            # Calculate and set iops/gb and bw/gb, but only if
            # total_iops_max and total_bw_max aren't set since they take
            # priority
            if iops_per_gb and 'total_iops_max' not in fpolicies:
                fpolicies['total_iops_max'] = iops_per_gb * resource['size']
            if bw_per_gb:
                fpolicies['total_bw_max'] = bw_per_gb * resource['size']
            if fpolicies or clear_old:
                try:
                    self._issue_api_request(
                        url, 'delete', api_version=API_VERSION,
                        tenant=tenant)
                except exception.NotFound:
                    LOG.debug("No existing performance policy found")
            if fpolicies:
                self._issue_api_request(
                    url, 'post', body=fpolicies, api_version=API_VERSION,
                    tenant=tenant)

    # ============
    # = IP Pools =
    # ============

    def _get_ip_pool_for_string_ip_2_2(self, ip, tenant):
        """Takes a string ipaddress and return the ip_pool API object dict """
        pool = 'default'
        ip_obj = ipaddress.ip_address(six.text_type(ip))
        ip_pools = self._issue_api_request('access_network_ip_pools',
                                           api_version=API_VERSION,
                                           tenant=tenant)
        for ipdata in ip_pools['data']:
            for adata in ipdata['network_paths']:
                if not adata.get('start_ip'):
                    continue
                pool_if = ipaddress.ip_interface(
                    "/".join((adata['start_ip'], str(adata['netmask']))))
                if ip_obj in pool_if.network:
                    pool = ipdata['name']
        return self._issue_api_request(
            "access_network_ip_pools/{}".format(pool),
            api_version=API_VERSION, tenant=tenant)['data']['path']

    # ============
    # = Metadata =
    # ============

    def _get_metadata(self, volume):
        tenant = self._create_tenant_2_2(volume)
        url = datc.URL_TEMPLATES['ai_inst']().format(
            datc._get_name(volume['id'])) + "/metadata"
        return self._issue_api_request(url, api_version=API_VERSION,
                                       tenant=tenant)['data']

    def _update_metadata(self, volume, keys):
        tenant = self._create_tenant_2_2(volume)
        url = datc.URL_TEMPLATES['ai_inst']().format(
            datc._get_name(volume['id'])) + "/metadata"
        self._issue_api_request(
            url, method='put', body=keys, api_version=API_VERSION,
            tenant=tenant)
