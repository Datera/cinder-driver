# Copyright 2016 Datera
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

import functools
import json
import re
import time
import uuid

import eventlet
import ipaddress
from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import units
import requests
import six

from cinder import context
from cinder import exception
from cinder.i18n import _, _LE, _LI, _LW
from cinder import interface
from cinder import utils
from cinder.volume.drivers.san import san
from cinder.volume import qos_specs
from cinder.volume import utils as volutils
from cinder.volume import volume_types

LOG = logging.getLogger(__name__)

d_opts = [
    cfg.StrOpt('datera_api_port',
               default='7717',
               help='Datera API port.'),
    cfg.StrOpt('datera_api_version',
               default='2',
               deprecated_for_removal=True,
               help='Datera API version.'),
    cfg.IntOpt('datera_503_timeout',
               default='120',
               help='Timeout for HTTP 503 retry messages'),
    cfg.IntOpt('datera_503_interval',
               default='5',
               help='Interval between 503 retries'),
    cfg.BoolOpt('datera_debug',
                default=False,
                help="True to set function arg and return logging"),
    cfg.BoolOpt('datera_debug_replica_count_override',
                default=False,
                help="ONLY FOR DEBUG/TESTING PURPOSES\n"
                     "True to set replica_count to 1"),
    cfg.StrOpt('datera_tenant_id',
               default=None,
               help="If set to 'Map' --> OpenStack project ID will be mapped "
                    "implicitly to Datera tenant ID\n"
                    "If set to 'None' --> Datera tenant ID will not be used "
                    "during volume provisioning\n"
                    "If set to anything else --> Datera tenant ID will be the "
                    "provided value")
]


CONF = cfg.CONF
CONF.import_opt('driver_use_ssl', 'cinder.volume.driver')
CONF.register_opts(d_opts)

DEFAULT_SI_SLEEP = 10
DEFAULT_SNAP_SLEEP = 5
INITIATOR_GROUP_PREFIX = "IG-"
OS_PREFIX = "OS-"
UNMANAGE_PREFIX = "UNMANAGED-"
API_VERSIONS = ["2", "2.1"]
API_TIMEOUT = 20

###############
# METADATA KEYS
###############

M_TYPE = 'cinder_volume_type'
M_CALL = 'cinder_calls'
M_CLONE = 'cinder_clone_from'
M_MANAGED = 'cinder_managed'

M_KEYS = [M_TYPE, M_CALL, M_CLONE, M_MANAGED]

# Taken from this SO post :
# http://stackoverflow.com/a/18516125
# Using old-style string formatting because of the nature of the regex
# conflicting with new-style curly braces
UUID4_STR_RE = ("%s[a-f0-9]{8}-?[a-f0-9]{4}-?4[a-f0-9]{3}-?[89ab]"
                "[a-f0-9]{3}-?[a-f0-9]{12}")
UUID4_RE = re.compile(UUID4_STR_RE % OS_PREFIX)

# Recursive dict to assemble basic url structure for the most common
# API URL endpoints. Most others are constructed from these
URL_TEMPLATES = {
    'ai': lambda: 'app_instances',
    'ai_inst': lambda: (URL_TEMPLATES['ai']() + '/{}'),
    'si': lambda: (URL_TEMPLATES['ai_inst']() + '/storage_instances'),
    'si_inst': lambda storage_name: (
        (URL_TEMPLATES['si']() + '/{}').format(
            '{}', storage_name)),
    'vol': lambda storage_name: (
        (URL_TEMPLATES['si_inst'](storage_name) + '/volumes')),
    'vol_inst': lambda storage_name, volume_name: (
        (URL_TEMPLATES['vol'](storage_name) + '/{}').format(
            '{}', volume_name)),
    'at': lambda: 'app_templates/{}'}


def _get_name(name):
    return "".join((OS_PREFIX, name))


def _get_unmanaged(name):
    return "".join((UNMANAGE_PREFIX, name))


def _authenticated(func):
    """Ensure the driver is authenticated to make a request.

    In do_setup() we fetch an auth token and store it. If that expires when
    we do API request, we'll fetch a new one.
    """
    @functools.wraps(func)
    def func_wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except exception.NotAuthorized:
            # Prevent recursion loop. After the self arg is the
            # resource_type arg from _issue_api_request(). If attempt to
            # login failed, we should just give up.
            if args[0] == 'login':
                raise

            # Token might've expired, get a new one, try again.
            self.login()
            return func(self, *args, **kwargs)
    return func_wrapper


def _api_lookup(func):
    """Perform a dynamic API implementation lookup for a call

    Naming convention follows this pattern:

        # original_func(args) --> _original_func_X_?Y?(args)
        # where X and Y are the major and minor versions of the latest
        # supported API version

        # From the Datera box we've determined that it supports API
        # versions ['2', '2.1']
        # This is the original function call
        @_api_lookup
        def original_func(arg1, arg2):
            print("I'm a shim, this won't get executed!")
            pass

        # This is the function that is actually called after determining
        # the correct API version to use
        def _original_func_2_1(arg1, arg2):
            some_version_2_1_implementation_here()

        # This is the function that would be called if the previous function
        # did not exist:
        def _original_func_2(arg1, arg2):
            some_version_2_implementation_here()

        # This function would NOT be called, because the connected Datera box
        # does not support the 1.5 version of the API
        def _original_func_1_5(arg1, arg2):
            some_version_1_5_implementation_here()
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        obj = args[0]
        api_versions = obj._get_supported_api_versions()
        api_version = None
        index = -1
        while True:
            try:
                api_version = api_versions[index]
            except (IndexError, KeyError):
                msg = _("No compatible API version found for this product: "
                        "api_versions -> %s")
                LOG.error(msg, api_version)
                raise exception.DateraAPIException(msg % api_version)
            name = "_" + "_".join(
                (func.func_name, api_version.replace(".", "_")))
            try:
                return getattr(obj, name)(*args[1:], **kwargs)
            except (AttributeError, NotImplementedError):
                index -= 1
            except exception.DateraAPIException as e:
                if "UnsupportedVersionError" in e[0]:
                    index -= 1
                else:
                    raise

    return wrapper


@interface.volumedriver
@six.add_metaclass(utils.TraceWrapperWithABCMetaclass)
class DateraDriver(san.SanISCSIDriver):

    """The OpenStack Datera Driver

    Version history:
        1.0 - Initial driver
        1.1 - Look for lun-0 instead of lun-1.
        2.0 - Update For Datera API v2
        2.1 - Multipath, ACL and reorg
        2.2 - Capabilites List, Extended Volume-Type Support
              Naming convention change,
              Volume Manage/Unmanage support
        2.3 - Templates, Tenants, Snapshot Polling,
              2.1 Api Version Support
    """
    VERSION = '2.3'

    CI_WIKI_NAME = "datera-ci"

    HEADER_DATA = {'Datera-Driver': 'OpenStack-Cinder-{}'.format(VERSION)}

    def __init__(self, *args, **kwargs):
        super(DateraDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(d_opts)
        self.username = self.configuration.san_login
        self.password = self.configuration.san_password
        self.cluster_stats = {}
        self.datera_api_token = None
        self.interval = self.configuration.datera_503_interval
        self.retry_attempts = (self.configuration.datera_503_timeout /
                               self.interval)
        self.driver_prefix = str(uuid.uuid4())[:4]
        self.datera_debug = self.configuration.datera_debug
        self.datera_api_versions = []

        if self.datera_debug:
            utils.setup_tracing(['method'])
        self.tenant_id = self.configuration.datera_tenant_id
        self.api_check = time.time()
        self.api_cache = []

    def do_setup(self, context):
        # If we can't authenticate through the old and new method, just fail
        # now.
        if not all([self.username, self.password]):
            msg = _("san_login and/or san_password is not set for Datera "
                    "driver in the cinder.conf. Set this information and "
                    "start the cinder-volume service again.")
            LOG.error(msg)
            raise exception.InvalidInput(msg)

        self.login()
        # Create the Datera tenant if specified in the config
        tid = self.tenant_id.lower() if self.tenant_id else self.tenant_id
        if tid is not None and tid != 'map':
            self._create_tenant(self.tenant_id)

    # =================
    # = Create Volume =
    # =================

    @_api_lookup
    def create_volume(self, volume):
        """Create a logical volume."""
        pass

    def _create_volume_2(self, volume):
        # Generate App Instance, Storage Instance and Volume
        # Volume ID will be used as the App Instance Name
        # Storage Instance and Volumes will have standard names
        policies = self._get_policies_for_resource(volume)
        num_replicas = int(policies['replica_count'])
        storage_name = policies['default_storage_name']
        volume_name = policies['default_volume_name']
        template = policies['template']

        if template:
            app_params = (
                {
                    'create_mode': "openstack",
                    # 'uuid': str(volume['id']),
                    'name': _get_name(volume['id']),
                    'app_template': '/app_templates/{}'.format(template)
                })
        else:

            app_params = (
                {
                    'create_mode': "openstack",
                    'uuid': str(volume['id']),
                    'name': _get_name(volume['id']),
                    'access_control_mode': 'deny_all',
                    'storage_instances': {
                        storage_name: {
                            'name': storage_name,
                            'volumes': {
                                volume_name: {
                                    'name': volume_name,
                                    'size': volume['size'],
                                    'replica_count': num_replicas,
                                    'snapshot_policies': {
                                    }
                                }
                            }
                        }
                    }
                })
        self._issue_api_request(
            URL_TEMPLATES['ai'](), 'post', body=app_params, api_version='2')
        self._update_qos(volume, policies)

    def _create_volume_2_1(self, volume):
        raise NotImplementedError()
        policies = self._get_policies_for_resource(volume)
        num_replicas = int(policies['replica_count'])
        storage_name = policies['default_storage_name']
        volume_name = policies['default_volume_name']
        template = policies['template']

        if template:
            app_params = (
                {
                    'create_mode': "openstack",
                    # 'uuid': str(volume['id']),
                    'name': _get_name(volume['id']),
                    'app_template': '/app_templates/{}'.format(template)
                })

        else:

            app_params = (
                {
                    'create_mode': "openstack",
                    'uuid': str(volume['id']),
                    'name': _get_name(volume['id']),
                    'access_control_mode': 'deny_all',
                    'storage_instances': [
                        {
                            'name': storage_name,
                            'volumes': [
                                {
                                    'name': volume_name,
                                    'size': volume['size'],
                                    'replica_count': num_replicas,
                                    'snapshot_policies': [
                                    ]
                                }
                            ]
                        }
                    ]
                })
        self._issue_api_request(
            URL_TEMPLATES['ai'](), 'post', body=app_params, api_version='2.1')
        self._update_qos(volume, policies)

        metadata = {}
        volume_type = self._get_volume_type_obj(volume)
        if volume_type:
            metadata.update({M_TYPE: volume_type['name']})
        metadata.update(self.HEADER_DATA)
        url = URL_TEMPLATES['ai_inst']().format(_get_name(volume['id']))
        self._store_metadata(url, metadata, "create_volume_2_1")

    # =================

    # =================
    # = Extend Volume =
    # =================

    @_api_lookup
    def extend_volume(self, volume, new_size):
        pass

    def _extend_volume_2(self, volume, new_size):
        # Current product limitation:
        # If app_instance is bound to template resizing is not possible
        # Once policies are implemented in the product this can go away
        policies = self._get_policies_for_resource(volume)
        template = policies['template']
        if template:
            LOG.warn(_LW("Volume size not extended due to template binding: "
                         "volume: %s, template: %s"), volume, template)
            return

        # Offline App Instance, if necessary
        reonline = False
        app_inst = self._issue_api_request(
            URL_TEMPLATES['ai_inst']().format(_get_name(volume['id'])),
            api_version='2')
        if app_inst['admin_state'] == 'online':
            reonline = True
            self._detach_volume_2(None, volume, delete_initiator=False)
        # Change Volume Size
        app_inst = _get_name(volume['id'])
        data = {
            'size': new_size
        }
        store_name, vol_name = self._scrape_template(policies)
        self._issue_api_request(
            URL_TEMPLATES['vol_inst'](
                store_name, vol_name).format(app_inst),
            method='put',
            body=data,
            api_version='2')
        # Online Volume, if it was online before
        if reonline:
            self._create_export_2(None, volume, None)

    def _extend_volume_2_1(self, volume, new_size):
        self._extend_volume_2(volume, new_size)
        policies = self._get_policies_for_resource(volume)
        store_name, vol_name = self._scrape_template(policies)
        url = URL_TEMPLATES['vol_inst'](
                store_name, vol_name).format(_get_name(volume['id']))
        metadata = {}
        self._store_metadata(url, metadata, "extend_volume_2_1")

    # =================

    # =================
    # = Cloned Volume =
    # =================

    @_api_lookup
    def create_cloned_volume(self, volume, src_vref):
        pass

    def _create_cloned_volume_2(self, volume, src_vref):
        policies = self._get_policies_for_resource(volume)

        store_name, vol_name = self._scrape_template(policies)

        src = "/" + URL_TEMPLATES['vol_inst'](
            store_name, vol_name).format(_get_name(src_vref['id']))
        data = {
            'create_mode': 'openstack',
            'name': _get_name(volume['id']),
            'uuid': str(volume['id']),
            'clone_src': src,
        }
        self._issue_api_request(
            URL_TEMPLATES['ai'](), 'post', body=data, api_version='2')

        if volume['size'] > src_vref['size']:
            self.extend_volume(volume, volume['size'])

    def _create_cloned_volume_2_1(self, volume, src_vref):
        self._create_cloned_volume_2(volume, src_vref)
        url = URL_TEMPLATES['ai_inst']().format(_get_name(volume['id']))
        volume_type = self._get_volume_type_obj(volume)
        metadata = {M_TYPE: volume_type['name'],
                    M_CLONE: _get_name(src_vref['id'])}
        self._store_metadata(url, metadata, "create_cloned_volume_2_1")

    # =================

    # =================
    # = Delete Volume =
    # =================

    @_api_lookup
    def delete_volume(self, volume):
        pass

    def _delete_volume_2(self, volume):
        self.detach_volume(None, volume)
        app_inst = _get_name(volume['id'])
        try:
            self._issue_api_request(URL_TEMPLATES['ai_inst']().format(
                app_inst),
                method='delete',
                api_version='2')
        except exception.NotFound:
            msg = _LI("Tried to delete volume %s, but it was not found in the "
                      "Datera cluster. Continuing with delete.")
            LOG.info(msg, _get_name(volume['id']))

    def _delete_volume_2_1(self, volume):
        self._delete_volume_2(volume)
        # No need for metadata update on a deleted object

    # =================

    # =================
    # = Ensure Export =
    # =================

    @_api_lookup
    def ensure_export(self, context, volume, connector):
        """Gets the associated account, retrieves CHAP info and updates."""

    def _ensure_export_2(self, context, volume, connector):
        return self._create_export_2(context, volume, connector)

    # =================

    # =========================
    # = Initialize Connection =
    # =========================

    @_api_lookup
    def initialize_connection(self, volume, connector):
        pass

    def _initialize_connection_2(self, volume, connector):
        # Now online the app_instance (which will online all storage_instances)
        multipath = connector.get('multipath', False)
        url = URL_TEMPLATES['ai_inst']().format(_get_name(volume['id']))
        data = {
            'admin_state': 'online'
        }
        app_inst = self._issue_api_request(
            url, method='put', body=data, api_version='2')
        storage_instances = app_inst["storage_instances"]
        si_names = list(storage_instances.keys())

        portal = storage_instances[si_names[0]]['access']['ips'][0] + ':3260'
        iqn = storage_instances[si_names[0]]['access']['iqn']
        if multipath:
            portals = [p + ':3260' for p in
                       storage_instances[si_names[0]]['access']['ips']]
            iqns = [iqn for _ in
                    storage_instances[si_names[0]]['access']['ips']]
            lunids = [self._get_lunid() for _ in
                      storage_instances[si_names[0]]['access']['ips']]

            return {
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
            return {
                'driver_volume_type': 'iscsi',
                'data': {
                    'target_discovered': False,
                    'target_iqn': iqn,
                    'target_portal': portal,
                    'target_lun': self._get_lunid(),
                    'volume_id': volume['id'],
                    'discard': False}}

    def _initialize_connection_2_1(self, volume, connector):
        result = self._initialize_connection_2(volume, connector)

        url = URL_TEMPLATES['ai_inst']().format(_get_name(volume['id']))
        self._store_metadata(url, {}, "initialize_connection_2_1")
        return result

    # =========================

    # =================
    # = Create Export =
    # =================

    @_api_lookup
    def create_export(self, context, volume, connector):
        pass

    def _create_export_2(self, context, volume, connector):
        # Online volume in case it hasn't been already
        url = URL_TEMPLATES['ai_inst']().format(_get_name(volume['id']))
        data = {
            'admin_state': 'online'
        }
        self._issue_api_request(url, method='put', body=data, api_version='2')
        # Check if we've already setup everything for this volume
        url = (URL_TEMPLATES['si']().format(_get_name(volume['id'])))
        storage_instances = self._issue_api_request(url, api_version='2')
        # Handle adding initiator to product if necessary
        # Then add initiator to ACL
        policies = self._get_policies_for_resource(volume)

        store_name, _ = self._scrape_template(policies)

        if (connector and
                connector.get('initiator') and
                not policies['acl_allow_all']):
            initiator_name = "OpenStack_{}_{}".format(
                self.driver_prefix, str(uuid.uuid4())[:4])
            initiator_group = INITIATOR_GROUP_PREFIX + volume['id']
            found = False
            initiator = connector['initiator']
            current_initiators = self._issue_api_request(
                'initiators', api_version='2')
            for iqn, values in current_initiators.items():
                if initiator == iqn:
                    found = True
                    break
            # If we didn't find a matching initiator, create one
            if not found:
                data = {'id': initiator, 'name': initiator_name}
                # Try and create the initiator
                # If we get a conflict, ignore it because race conditions
                self._issue_api_request("initiators",
                                        method="post",
                                        body=data,
                                        conflict_ok=True,
                                        api_version='2')
            # Create initiator group with initiator in it
            initiator_path = "/initiators/{}".format(initiator)
            initiator_group_path = "/initiator_groups/{}".format(
                initiator_group)
            ig_data = {'name': initiator_group, 'members': [initiator_path]}
            self._issue_api_request("initiator_groups",
                                    method="post",
                                    body=ig_data,
                                    conflict_ok=True,
                                    api_version='2')
            # Create ACL with initiator group as reference for each
            # storage_instance in app_instance
            # TODO(_alastor_): We need to avoid changing the ACLs if the
            # template already specifies an ACL policy.
            for si_name in storage_instances.keys():
                acl_url = (URL_TEMPLATES['si']() + "/{}/acl_policy").format(
                    _get_name(volume['id']), si_name)
                existing_acl = self._issue_api_request(acl_url,
                                                       method="get",
                                                       api_version='2')
                existing_acl.append(initiator_group_path)
                data = {'initiator_groups': existing_acl}
                self._issue_api_request(acl_url,
                                        method="put",
                                        body=data,
                                        api_version='2')

        if connector and connector.get('ip'):
            try:
                # Case where volume_type has non default IP Pool info
                if policies['ip_pool'] != 'default':
                    initiator_ip_pool_path = self._issue_api_request(
                        "access_network_ip_pools/{}".format(
                            policies['ip_pool']), api_version='2')['path']
                # Fallback to trying reasonable IP based guess
                else:
                    initiator_ip_pool_path = self._get_ip_pool_for_string_ip(
                        connector['ip'])

                ip_pool_url = URL_TEMPLATES['si_inst'](
                    store_name).format(_get_name(volume['id']))
                ip_pool_data = {'ip_pool': initiator_ip_pool_path}
                self._issue_api_request(ip_pool_url,
                                        method="put",
                                        body=ip_pool_data,
                                        api_version='2')
            except exception.DateraAPIException:
                # Datera product 1.0 support
                pass

        # Check to ensure we're ready for go-time
        self._si_poll(volume, policies)

    def _create_export_2_1(self, context, volume, connector):
        self._create_export_2(context, volume, connector)
        url = URL_TEMPLATES['ai_inst']().format(_get_name(volume['id']))
        metadata = {}
        # TODO(_alastor_): Figure out what we want to post with a create_export
        # call
        self._store_metadata(url, metadata, "create_export_2_1")

    # =================

    # =================
    # = Detach Volume =
    # =================

    @_api_lookup
    def detach_volume(self, context, volume, attachment=None):
        pass

    def _detach_volume_2(self, context, volume, attachment=None):
        url = URL_TEMPLATES['ai_inst']().format(_get_name(volume['id']))
        data = {
            'admin_state': 'offline',
            'force': True
        }
        try:
            self._issue_api_request(url, method='put', body=data,
                                    api_version='2')
        except exception.NotFound:
            msg = _LI("Tried to detach volume %s, but it was not found in the "
                      "Datera cluster. Continuing with detach.")
            LOG.info(msg, volume['id'])
        # TODO(_alastor_): Make acl cleaning multi-attach aware
        self._clean_acl(volume)

    def _detach_volume_2_1(self, context, volume, attachment=None):
        self._detach_volume_2(context, volume, attachment)
        url = URL_TEMPLATES['ai_inst']().format(_get_name(volume['id']))
        metadata = {}
        self._store_metadata(url, metadata, "detach_volume_2_1")

    def _check_for_acl(self, initiator_path):
        """Returns True if an acl is found for initiator_path """
        # TODO(_alastor_) when we get a /initiators/:initiator/acl_policies
        # endpoint use that instead of this monstrosity
        initiator_groups = self._issue_api_request("initiator_groups",
                                                   api_version='2')
        for ig, igdata in initiator_groups.items():
            if initiator_path in igdata['members']:
                LOG.debug("Found initiator_group: %s for initiator: %s",
                          ig, initiator_path)
                return True
        LOG.debug("No initiator_group found for initiator: %s", initiator_path)
        return False

    def _clean_acl(self, volume):
        policies = self._get_policies_for_resource(volume)

        store_name, _ = self._scrape_template(policies)

        acl_url = (URL_TEMPLATES["si_inst"](
            store_name) + "/acl_policy").format(_get_name(volume['id']))
        try:
            initiator_group = self._issue_api_request(
                acl_url, api_version='2')['initiator_groups'][0]
            initiator_iqn_path = self._issue_api_request(
                initiator_group.lstrip("/"))["members"][0]
            # Clear out ACL and delete initiator group
            self._issue_api_request(acl_url,
                                    method="put",
                                    body={'initiator_groups': []},
                                    api_version='2')
            self._issue_api_request(initiator_group.lstrip("/"),
                                    method="delete",
                                    api_version='2')
            if not self._check_for_acl(initiator_iqn_path):
                self._issue_api_request(initiator_iqn_path.lstrip("/"),
                                        method="delete",
                                        api_version='2')
        except (IndexError, exception.NotFound):
            LOG.debug("Did not find any initiator groups for volume: %s",
                      volume)

    # =================

    # ===================
    # = Create Snapshot =
    # ===================

    @_api_lookup
    def create_snapshot(self, snapshot):
        pass

    def _create_snapshot_2(self, snapshot):
        policies = self._get_policies_for_resource(snapshot)

        store_name, vol_name = self._scrape_template(policies)

        url_template = URL_TEMPLATES['vol_inst'](
            store_name, vol_name) + '/snapshots'
        url = url_template.format(_get_name(snapshot['volume_id']))

        snap_params = {
            'uuid': snapshot['id'],
        }
        snap = self._issue_api_request(url, method='post', body=snap_params,
                                       api_version='2')
        snapu = "/".join((url, snap['timestamp']))
        self._snap_poll(snapu)

    # def _create_snapshot_2_1(self, snapshot):
    #     self._create_snapshot_2(snapshot)
    #     policies = self._get_policies_for_resource(snapshot)
    #     store_name, vol_name = self._scrape_template(policies)
    #     url = URL_TEMPLATES['vol_inst'](store_name, vol_name).format(
    #             _get_name(snapshot['volume_id']))
    #     metadata = {}
    #     self._store_metadata(url, metadata, "create_snapshot_2_1")

    # ===================

    # ===================
    # = Delete Snapshot =
    # ===================

    @_api_lookup
    def delete_snapshot(self, snapshot):
        pass

    def _delete_snapshot_2(self, snapshot):
        policies = self._get_policies_for_resource(snapshot)

        store_name, vol_name = self._scrape_template(policies)

        snap_temp = URL_TEMPLATES['vol_inst'](
            store_name, vol_name) + '/snapshots'
        snapu = snap_temp.format(_get_name(snapshot['volume_id']))
        snapshots = self._issue_api_request(snapu, method='get',
                                            api_version='2')

        try:
            for ts, snap in snapshots.items():
                if snap['uuid'] == snapshot['id']:
                    url_template = snapu + '/{}'
                    url = url_template.format(ts)
                    self._issue_api_request(url, method='delete',
                                            api_version='2')
                    break
            else:
                raise exception.NotFound
        except exception.NotFound:
            msg = _LI("Tried to delete snapshot %s, but was not found in "
                      "Datera cluster. Continuing with delete.")
            LOG.info(msg, _get_name(snapshot['id']))

    # def _delete_snapshot_2_1(self, snapshot):
    #     self._delete_snapshot_2(snapshot)
    #     policies = self._get_policies_for_resource(snapshot)
    #     store_name, vol_name = self._scrape_template(policies)
    #     url = URL_TEMPLATES['vol_inst'](store_name, vol_name).format(
    #         _get_name(snapshot['volume_id']))
    #     metadata = {}
    #     self._store_metadata(
    #         url, metadata, "create_volume_from_snapshot_2_1")

    # ===================

    # ========================
    # = Volume From Snapshot =
    # ========================

    @_api_lookup
    def create_volume_from_snapshot(self, volume, snapshot):
        pass

    def _create_volume_from_snapshot_2(self, volume, snapshot):
        policies = self._get_policies_for_resource(snapshot)

        store_name, vol_name = self._scrape_template(policies)

        snap_temp = URL_TEMPLATES['vol_inst'](
            store_name, vol_name) + '/snapshots'
        snapu = snap_temp.format(_get_name(snapshot['volume_id']))
        snapshots = self._issue_api_request(snapu, method='get',
                                            api_version='2')
        for ts, snap in snapshots.items():
            if snap['uuid'] == snapshot['id']:
                found_ts = ts
                break
        else:
            raise exception.NotFound

        snap_url = (snap_temp + '/{}').format(
            _get_name(snapshot['volume_id']), found_ts)

        self._snap_poll(snap_url)

        src = "/" + snap_url
        app_params = (
            {
                'create_mode': 'openstack',
                'uuid': str(volume['id']),
                'name': _get_name(volume['id']),
                'clone_src': src,
            })
        self._issue_api_request(
            URL_TEMPLATES['ai'](),
            method='post',
            body=app_params,
            api_version='2')

    # def _create_volume_from_snapshot_2_1(self, volume, snapshot):
    #     self._create_volume_from_snapshot_2(volume, snapshot)
    #     policies = self._get_policies_for_resource(snapshot)
    #     store_name, vol_name = self._scrape_template(policies)
    #     url = URL_TEMPLATES['vol_inst'](store_name, vol_name).format(
    #         _get_name(snapshot['volume_id']))
    #     metadata = {}
    #     self._store_metadata(
    #         url, metadata, "create_volume_from_snapshot_2_1")

    # ========================

    # ==========
    # = Manage =
    # ==========

    @_api_lookup
    def manage_existing(self, volume, existing_ref):
        """Manage an existing volume on the Datera backend

        The existing_ref must be either the current name or Datera UUID of
        an app_instance on the Datera backend in a colon separated list with
        the storage instance name and volume name.  This means only
        single storage instances and single volumes are supported for
        managing by cinder.

        Eg.

        existing_ref['source-name'] == app_inst_name:storage_inst_name:vol_name

        :param volume:       Cinder volume to manage
        :param existing_ref: Driver-specific information used to identify a
                             volume
        """
        pass

    def _manage_existing_2(self, volume, existing_ref):
        existing_ref = existing_ref['source-name']
        if existing_ref.count(":") != 2:
            raise exception.ManageExistingInvalidReference(
                _("existing_ref argument must be of this format:"
                  "app_inst_name:storage_inst_name:vol_name"))
        app_inst_name = existing_ref.split(":")[0]
        LOG.debug("Managing existing Datera volume %(volume)s.  "
                  "Changing name to %(existing)s",
                  existing=existing_ref, volume=_get_name(volume['id']))
        data = {'name': _get_name(volume['id'])}
        self._issue_api_request(URL_TEMPLATES['ai_inst']().format(
            app_inst_name), method='put', body=data, api_version='2')

    # def _manage_existing_2_1(self, volume, existing_ref):
    #     self._manage_existing_2(volume, existing_ref)
    #     app_inst_name, si_name, vol_name = existing_ref.split(":")
    #     url = URL_TEMPLATES['vol_inst'](si_name, vol_name).format(
    #         app_inst_name)
    #     metadata = {M_MANAGED: True}
    #     self._store_metadata(
    #         url, metadata, "manage_existing_2_1")

    # ==========

    # ===================
    # = Manage Get Size =
    # ===================

    @_api_lookup
    def manage_existing_get_size(self, volume, existing_ref):
        """Get the size of an unmanaged volume on the Datera backend

        The existing_ref must be either the current name or Datera UUID of
        an app_instance on the Datera backend in a colon separated list with
        the storage instance name and volume name.  This means only
        single storage instances and single volumes are supported for
        managing by cinder.

        Eg.

        existing_ref == app_inst_name:storage_inst_name:vol_name

        :param volume:       Cinder volume to manage
        :param existing_ref: Driver-specific information used to identify a
                             volume on the Datera backend
        """
        pass

    def _manage_existing_get_size_2(self, volume, existing_ref):
        existing_ref = existing_ref['source-name']
        if existing_ref.count(":") != 2:
            raise exception.ManageExistingInvalidReference(
                _("existing_ref argument must be of this format:"
                  "app_inst_name:storage_inst_name:vol_name"))
        app_inst_name, si_name, vol_name = existing_ref.split(":")
        app_inst = self._issue_api_request(
            URL_TEMPLATES['ai_inst']().format(app_inst_name),
            api_version='2')
        return self._get_size(volume, app_inst, si_name, vol_name)

    # def _manage_existing_get_size_2_1(self, volume, existing_ref):
    #     result = self._manage_existing_get_size_2(self, volume, existing_ref)
    #     app_inst_name, si_name, vol_name = existing_ref.split(":")
    #     url = URL_TEMPLATES['vol_inst'](si_name, vol_name).format(
    #         app_inst_name)
    #     metadata = {}
    #     self._store_metadata(url, metadata, "manage_existing_get_size_2_1")
    #     return result

    def _get_size(self, volume, app_inst=None, si_name=None, vol_name=None):
        """Helper method for getting the size of a backend object

        If app_inst is provided, we'll just parse the dict to get
        the size instead of making a separate http request
        """
        policies = self._get_policies_for_resource(volume)
        si_name = si_name if si_name else policies['default_storage_name']
        vol_name = vol_name if vol_name else policies['default_volume_name']
        if not app_inst:
            vol_url = URL_TEMPLATES['ai_inst']().format(
                _get_name(volume['id']))
            app_inst = self._issue_api_request(vol_url)
        size = app_inst[
            'storage_instances'][si_name]['volumes'][vol_name]['size']
        return size

    # ===================

    # =========================
    # = Get Manageable Volume =
    # =========================

    @_api_lookup
    def get_manageable_volumes(self, cinder_volumes, marker, limit, offset,
                               sort_keys, sort_dirs):
        """List volumes on the backend available for management by Cinder.

        Returns a list of dictionaries, each specifying a volume in the host,
        with the following keys:
        - reference (dictionary): The reference for a volume, which can be
          passed to "manage_existing".
        - size (int): The size of the volume according to the storage
          backend, rounded up to the nearest GB.
        - safe_to_manage (boolean): Whether or not this volume is safe to
          manage according to the storage backend. For example, is the volume
          in use or invalid for any reason.
        - reason_not_safe (string): If safe_to_manage is False, the reason why.
        - cinder_id (string): If already managed, provide the Cinder ID.
        - extra_info (string): Any extra information to return to the user

        :param cinder_volumes: A list of volumes in this host that Cinder
                               currently manages, used to determine if
                               a volume is manageable or not.
        :param marker:    The last item of the previous page; we return the
                          next results after this value (after sorting)
        :param limit:     Maximum number of items to return
        :param offset:    Number of items to skip after marker
        :param sort_keys: List of keys to sort results by (valid keys are
                          'identifier' and 'size')
        :param sort_dirs: List of directions to sort by, corresponding to
                          sort_keys (valid directions are 'asc' and 'desc')
        """
        pass

    def _get_manageable_volumes_2(self, cinder_volumes, marker, limit, offset,
                                  sort_keys, sort_dirs):
        LOG.debug("Listing manageable Datera volumes")
        app_instances = self._issue_api_request(
            URL_TEMPLATES['ai'](), api_version='2').values()

        results = []

        cinder_volume_ids = [vol['id'] for vol in cinder_volumes]

        for ai in app_instances:
            ai_name = ai['name']
            reference = None
            size = None
            safe_to_manage = False
            reason_not_safe = None
            cinder_id = None
            extra_info = None
            if re.match(UUID4_RE, ai_name):
                cinder_id = ai_name.lstrip(OS_PREFIX)
            if (not cinder_id and
                    ai_name.lstrip(OS_PREFIX) not in cinder_volume_ids):
                safe_to_manage = self._is_manageable(ai)
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

    # ========================

    # ============
    # = Unmanage =
    # ============

    @_api_lookup
    def unmanage(self, volume):
        """Unmanage a currently managed volume in Cinder

        :param volume:       Cinder volume to unmanage
        """
        pass

    def _unmanage_2(self, volume):
        LOG.debug("Unmanaging Cinder volume %s.  Changing name to %s",
                  volume['id'], _get_unmanaged(volume['id']))
        data = {'name': _get_unmanaged(volume['id'])}
        self._issue_api_request(URL_TEMPLATES['ai_inst']().format(
            _get_name(volume['id'])), method='put', body=data, api_version='2')

    # def _unmanage_2_1(self, volume):
    #     self._unmanage_2(volume)
    #     policies = self._get_policies_for_resource(volume)
    #     store_name, vol_name = self._scrape_template(policies)
    #     url = URL_TEMPLATES['vol_inst'](store_name, vol_name).format(
    #         _get_name(volume['id']))
    #     metadata = {M_MANAGED: False}
    #     self._store_metadata(url, metadata, "unmanage_2_1")

    # ============

    # ================
    # = Volume Stats =
    # ================

    @_api_lookup
    def get_volume_stats(self, refresh=False):
        """Get volume stats.

        If 'refresh' is True, run update first.
        The name is a bit misleading as
        the majority of the data here is cluster
        data.
        """
        pass

    def _get_volume_stats_2(self, refresh=False):
        if refresh or not self.cluster_stats:
            try:
                LOG.debug("Updating cluster stats info.")

                results = self._issue_api_request('system', api_version='2')

                if 'uuid' not in results:
                    LOG.error(_LE(
                        'Failed to get updated stats from Datera Cluster.'))

                backend_name = self.configuration.safe_get(
                    'volume_backend_name')
                stats = {
                    'volume_backend_name': backend_name or 'Datera',
                    'vendor_name': 'Datera',
                    'driver_version': self.VERSION,
                    'storage_protocol': 'iSCSI',
                    'total_capacity_gb': (
                        int(results['total_capacity']) / units.Gi),
                    'free_capacity_gb': (
                        int(results['available_capacity']) / units.Gi),
                    'reserved_percentage': 0,
                }

                self.cluster_stats = stats
            except exception.DateraAPIException:
                LOG.error(_LE('Failed to get updated stats from Datera '
                              'cluster.'))
        return self.cluster_stats

    # =================

    def _is_manageable(self, app_inst):
        if len(app_inst['storage_instances']) == 1:
            si = list(app_inst['storage_instances'].values())[0]
            if len(si['volumes']) == 1:
                return True
        return False

    def _scrape_template(self, policies):
        sname = policies['default_storage_name']
        vname = policies['default_volume_name']

        template = policies['template']
        if template:
            result = self._issue_api_request(
                URL_TEMPLATES['at']().format(template))
            sname, st = list(result['storage_templates'].items())[0]
            vname = list(st['volume_templates'].keys())[0]
        return sname, vname

    # =========
    # = Login =
    # =========

    @_api_lookup
    def login(self):
        pass

    def _login_2(self):
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
            results = self._issue_api_request('login', 'put', body=body,
                                              sensitive=True, api_version='2')
            self.datera_api_token = results['key']
        except exception.NotAuthorized:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE('Logging into the Datera cluster failed. Please '
                              'check your username and password set in the '
                              'cinder.conf and start the cinder-volume '
                              'service again.'))
    # ==========

    # ===========
    # = Tenancy =
    # ===========

    def _create_tenant(self, tenant):
        params = {'name': tenant}
        self._issue_api_request(
            'tenants', method='post', body=params, conflict_ok=True,
            api_version='2.1')

    # ===========

    # ============
    # = Metadata =
    # ============

    def _get_metadata(self, obj_url):
        url = "/".join((obj_url.rstrip("/"), "metadata"))
        mdata = self._issue_api_request(url, api_version="2.1").get("data")
        # Make sure we only grab the relevant keys
        filter_mdata = {k: json.loads(mdata[k]) for k in mdata if k in M_KEYS}
        # Metadata lists are strings separated by the "|" character
        return filter_mdata

    def _store_metadata(self, obj_url, data, calling_func_name):
        mdata = self._get_metadata(obj_url)
        new_call_entry = (calling_func_name, self.HEADER_DATA['Datera-Driver'])
        if mdata.get(M_CALL):
            mdata[M_CALL].append(new_call_entry)
        else:
            mdata[M_CALL] = [new_call_entry]
        mdata.update(data)
        mdata.update(self.HEADER_DATA)
        data_s = {k: json.dumps(v) for k, v in data.items()}
        url = "/".join((obj_url.rstrip("/"), "metadata"))
        return self._issue_api_request(url, method="put", api_version="2.1",
                                       body=data_s)
    # ============

    # =======
    # = QoS =
    # =======

    def _update_qos(self, resource, policies):
        url = URL_TEMPLATES['vol_inst'](
            policies['default_storage_name'],
            policies['default_volume_name']) + '/performance_policy'
        url = url.format(_get_name(resource['id']))
        type_id = resource.get('volume_type_id', None)
        if type_id is not None:
            # Filter for just QOS policies in result. All of their keys
            # should end with "max"
            fpolicies = {k: int(v) for k, v in
                         policies.items() if k.endswith("max")}
            # Filter all 0 values from being passed
            fpolicies = dict(filter(lambda _v: _v[1] > 0, fpolicies.items()))
            if fpolicies:
                self._issue_api_request(url, 'post', body=fpolicies,
                                        api_version='2')

    # =======

    # ============
    # = IP Pools =
    # ============

    def _get_ip_pool_for_string_ip(self, ip):
        """Takes a string ipaddress and return the ip_pool API object dict """
        pool = 'default'
        ip_obj = ipaddress.ip_address(six.text_type(ip))
        ip_pools = self._issue_api_request('access_network_ip_pools',
                                           api_version='2')
        for ip_pool, ipdata in ip_pools.items():
            for access, adata in ipdata['network_paths'].items():
                if not adata.get('start_ip'):
                    continue
                pool_if = ipaddress.ip_interface(
                    "/".join((adata['start_ip'], str(adata['netmask']))))
                if ip_obj in pool_if.network:
                    pool = ip_pool
        return self._issue_api_request(
            "access_network_ip_pools/{}".format(pool), api_version='2')['path']

    # ============

    # ===========
    # = Polling =
    # ===========

    def _snap_poll(self, url):
        eventlet.sleep(DEFAULT_SNAP_SLEEP)
        TIMEOUT = 10
        retry = 0
        poll = True
        while poll and not retry >= TIMEOUT:
            retry += 1
            snap = self._issue_api_request(url, api_version='2')
            if snap['op_state'] == 'available':
                poll = False
            else:
                eventlet.sleep(1)
        if retry >= TIMEOUT:
            raise exception.VolumeDriverException(
                message=_('Snapshot not ready.'))

    def _si_poll(self, volume, policies):
        # Initial 4 second sleep required for some Datera versions
        eventlet.sleep(DEFAULT_SI_SLEEP)
        TIMEOUT = 10
        retry = 0
        check_url = URL_TEMPLATES['si_inst'](
            policies['default_storage_name']).format(_get_name(volume['id']))
        poll = True
        while poll and not retry >= TIMEOUT:
            retry += 1
            si = self._issue_api_request(check_url, api_version='2')
            if si['op_state'] == 'available':
                poll = False
            else:
                eventlet.sleep(1)
        if retry >= TIMEOUT:
            raise exception.VolumeDriverException(
                message=_('Resource not ready.'))

    # ===========

    def _get_lunid(self):
        return 0

    # ============================
    # = Volume-Types/Extra-Specs =
    # ============================

    def _init_vendor_properties(self):
        """Create a dictionary of vendor unique properties.

        This method creates a dictionary of vendor unique properties
        and returns both created dictionary and vendor name.
        Returned vendor name is used to check for name of vendor
        unique properties.

        - Vendor name shouldn't include colon(:) because of the separator
          and it is automatically replaced by underscore(_).
          ex. abc:d -> abc_d
        - Vendor prefix is equal to vendor name.
          ex. abcd
        - Vendor unique properties must start with vendor prefix + ':'.
          ex. abcd:maxIOPS

        Each backend driver needs to override this method to expose
        its own properties using _set_property() like this:

        self._set_property(
            properties,
            "vendorPrefix:specific_property",
            "Title of property",
            _("Description of property"),
            "type")

        : return dictionary of vendor unique properties
        : return vendor name

        prefix: DF --> Datera Fabric
        """

        properties = {}

        if self.configuration.get('datera_debug_replica_count_override'):
            replica_count = 1
        else:
            replica_count = 3
        self._set_property(
            properties,
            "DF:replica_count",
            "Datera Volume Replica Count",
            _("Specifies number of replicas for each volume. Can only be "
              "increased once volume is created"),
            "integer",
            minimum=1,
            default=replica_count)

        self._set_property(
            properties,
            "DF:acl_allow_all",
            "Datera ACL Allow All",
            _("True to set acl 'allow_all' on volumes created.  Cannot be "
              "changed on volume once set"),
            "boolean",
            default=False)

        self._set_property(
            properties,
            "DF:ip_pool",
            "Datera IP Pool",
            _("Specifies IP pool to use for volume"),
            "string",
            default="default")

        self._set_property(
            properties,
            "DF:template",
            "Datera Template",
            _("Specifies Template to use for volume provisioning"),
            "string",
            default="")

        # ###### QoS Settings ###### #
        self._set_property(
            properties,
            "DF:read_bandwidth_max",
            "Datera QoS Max Bandwidth Read",
            _("Max read bandwidth setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=0)

        self._set_property(
            properties,
            "DF:default_storage_name",
            "Datera Default Storage Instance Name",
            _("The name to use for storage instances created"),
            "string",
            default="storage-1")

        self._set_property(
            properties,
            "DF:default_volume_name",
            "Datera Default Volume Name",
            _("The name to use for volumes created"),
            "string",
            default="volume-1")

        self._set_property(
            properties,
            "DF:write_bandwidth_max",
            "Datera QoS Max Bandwidth Write",
            _("Max write bandwidth setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=0)

        self._set_property(
            properties,
            "DF:total_bandwidth_max",
            "Datera QoS Max Bandwidth Total",
            _("Max total bandwidth setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=0)

        self._set_property(
            properties,
            "DF:read_iops_max",
            "Datera QoS Max iops Read",
            _("Max read iops setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=0)

        self._set_property(
            properties,
            "DF:write_iops_max",
            "Datera QoS Max IOPS Write",
            _("Max write iops setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=0)

        self._set_property(
            properties,
            "DF:total_iops_max",
            "Datera QoS Max IOPS Total",
            _("Max total iops setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=0)
        # ###### End QoS Settings ###### #

        return properties, 'DF'

    def _get_volume_type_obj(self, resource):
        type_id = resource.get('volume_type_id', None)
        # Handle case of volume with no type.  We still want the
        # specified defaults from above
        if type_id:
            ctxt = context.get_admin_context()
            volume_type = volume_types.get_volume_type(ctxt, type_id)
        else:
            volume_type = None
        return volume_type

    def _get_policies_for_resource(self, resource):
        """Get extra_specs and qos_specs of a volume_type.

        This fetches the scoped keys from the volume type. Anything set from
         qos_specs will override key/values set from extra_specs.
        """
        volume_type = self._get_volume_type_obj(resource)
        # Handle case of volume with no type.  We still want the
        # specified defaults from above
        if volume_type:
            specs = volume_type.get('extra_specs')
        else:
            specs = {}

        # Set defaults:
        policies = {k.lstrip('DF:'): str(v['default']) for (k, v)
                    in self._init_vendor_properties()[0].items()}

        if volume_type:
            # Populate updated value
            for key, value in specs.items():
                if ':' in key:
                    fields = key.split(':')
                    key = fields[1]
                    policies[key] = value

            qos_specs_id = volume_type.get('qos_specs_id')
            if qos_specs_id is not None:
                ctxt = context.get_admin_context()
                qos_kvs = qos_specs.get_qos_specs(ctxt, qos_specs_id)['specs']
                if qos_kvs:
                    policies.update(qos_kvs)
        # Cast everything except booleans int that can be cast
        for k, v in policies.items():
            # Handle String Boolean case
            if v == 'True' or v == 'False':
                policies[k] = policies[k] == 'True'
                continue
            # Int cast
            try:
                policies[k] = int(v)
            except ValueError:
                pass
        return policies

    # ============================

    # ================
    # = API Requests =
    # ================

    def _request(self, connection_string, method, payload, header, cert_data):
        LOG.debug("Endpoint for Datera API call: %s", connection_string)
        try:
            response = getattr(requests, method)(connection_string,
                                                 data=payload, headers=header,
                                                 verify=False, cert=cert_data)
            return response
        except requests.exceptions.RequestException as ex:
            msg = _(
                'Failed to make a request to Datera cluster endpoint due '
                'to the following reason: %s') % six.text_type(
                ex.message)
            LOG.error(msg)
            raise exception.DateraAPIException(msg)

    def _get_supported_api_versions(self):
        t = time.time()
        if self.api_cache and self.api_timeout - t < API_TIMEOUT:
            return self.api_cache
        results = []
        host = self.configuration.san_ip
        port = self.configuration.datera_api_port
        client_cert = self.configuration.driver_client_cert
        client_cert_key = self.configuration.driver_client_cert_key
        cert_data = None
        header = {'Content-Type': 'application/json; charset=utf-8',
                  'Datera-Driver': 'OpenStack-Cinder-{}'.format(self.VERSION)}
        protocol = 'http'
        if client_cert:
            protocol = 'https'
            cert_data = (client_cert, client_cert_key)
        try:
            url = '%s://%s:%s/api_versions' % (protocol, host, port)
            resp = self._request(url, "get", None, header, cert_data)
            data = resp.json()
            results = [elem.strip("v") for elem in data['api_versions']]
        except exception.DateraAPIException:
            # Fallback to pre-endpoint logic
            for version in API_VERSIONS:
                url = '%s://%s:%s/v%s' % (protocol, host, port, version)
                resp = self._request(url, "get", None, header, cert_data)
                if ("api_req" in resp.json() or
                        str(resp.json().get("code")) == "99"):
                    results.append(version)
        return results

    def _raise_response(self, response):
        msg = _('Request to Datera cluster returned bad status:'
                ' %(status)s | %(reason)s') % {
                    'status': response.status_code,
                    'reason': response.reason}
        LOG.error(msg)
        raise exception.DateraAPIException(msg)

    def _handle_bad_status(self,
                           response,
                           connection_string,
                           method,
                           payload,
                           header,
                           cert_data,
                           sensitive=False,
                           conflict_ok=False):
        if (response.status_code == 400 and
                connection_string.endswith("api_versions")):
            # Raise the exception, but don't log any error.  We'll just fall
            # back to the old style of determining API version.  We make this
            # request a lot, so logging it is just noise
            raise exception.DateraAPIException
        if not sensitive:
            LOG.debug(("Datera Response URL: %s\n"
                       "Datera Response Payload: %s\n"
                       "Response Object: %s\n"),
                      response.url,
                      payload,
                      vars(response))
        if response.status_code == 404:
            raise exception.NotFound(response.json()['message'])
        elif response.status_code in [403, 401]:
            raise exception.NotAuthorized()
        elif response.status_code == 409 and conflict_ok:
            # Don't raise, because we're expecting a conflict
            pass
        elif response.status_code == 503:
            current_retry = 0
            while current_retry <= self.retry_attempts:
                LOG.debug("Datera 503 response, trying request again")
                eventlet.sleep(self.interval)
                resp = self._request(connection_string,
                                     method,
                                     payload,
                                     header,
                                     cert_data)
                if resp.ok:
                    return response.json()
                elif resp.status_code != 503:
                    self._raise_response(resp)
        else:
            self._raise_response(response)

    @_authenticated
    def _issue_api_request(self, resource_url, method='get', body=None,
                           sensitive=False, conflict_ok=False,
                           api_version='2', tenant=None):
        """All API requests to Datera cluster go through this method.

        :param resource_url: the url of the resource
        :param method: the request verb
        :param body: a dict with options for the action_type
        :param sensitive: Bool, whether request should be obscured from logs
        :param conflict_ok: Bool, True to suppress ConflictError exceptions
        during this request
        :param api_version: The Datera api version for the request
        :param tenant: The tenant header value for the request (only applicable
        to 2.1 product versions and later)
        :returns: a dict of the response from the Datera cluster
        """
        host = self.configuration.san_ip
        port = self.configuration.datera_api_port
        api_token = self.datera_api_token

        payload = json.dumps(body, ensure_ascii=False)
        payload.encode('utf-8')

        header = {'Content-Type': 'application/json; charset=utf-8'}
        header.update(self.HEADER_DATA)

        protocol = 'http'
        if self.configuration.driver_use_ssl:
            protocol = 'https'

        if api_token:
            header['Auth-Token'] = api_token

        if tenant:
            header['tenant'] = tenant

        client_cert = self.configuration.driver_client_cert
        client_cert_key = self.configuration.driver_client_cert_key
        cert_data = None

        if client_cert:
            protocol = 'https'
            cert_data = (client_cert, client_cert_key)

        connection_string = '%s://%s:%s/v%s/%s' % (protocol, host, port,
                                                   api_version, resource_url)

        response = self._request(connection_string,
                                 method,
                                 payload,
                                 header,
                                 cert_data)

        data = response.json()

        if not response.ok:
            self._handle_bad_status(response,
                                    connection_string,
                                    method,
                                    payload,
                                    header,
                                    cert_data,
                                    conflict_ok=conflict_ok)

        return data

    # ================
