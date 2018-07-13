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

import functools
import re
import time
import types
import uuid

import dfs_sdk

from oslo_log import log as logging

from cinder import context
from cinder import exception
from cinder.i18n import _
from cinder.image import glance
from cinder.volume import qos_specs
from cinder.volume import volume_types

from glanceclient import exc as glance_exc

LOG = logging.getLogger(__name__)
OS_PREFIX = "OS-"
UNMANAGE_PREFIX = "UNMANAGED-"

# Taken from this SO post :
# http://stackoverflow.com/a/18516125
# Using old-style string formatting because of the nature of the regex
# conflicting with new-style curly braces
UUID4_STR_RE = ("%s.*([a-f0-9]{8}-?[a-f0-9]{4}-?4[a-f0-9]{3}-?[89ab]"
                "[a-f0-9]{3}-?[a-f0-9]{12})")
UUID4_RE = re.compile(UUID4_STR_RE % OS_PREFIX)
SNAP_RE = re.compile(r"\d{10,}\.\d+")

# Recursive dict to assemble basic url structure for the most common
# API URL endpoints. Most others are constructed from these
DEFAULT_SI_SLEEP = 1
DEFAULT_SI_SLEEP_API_2 = 5
DEFAULT_SNAP_SLEEP = 1
API_VERSIONS = ["2.1", "2.2"]
API_TIMEOUT = 20

###############
# METADATA KEYS
###############

M_TYPE = 'cinder_volume_type'
M_CALL = 'cinder_calls'
M_CLONE = 'cinder_clone_from'
M_MANAGED = 'cinder_managed'

M_KEYS = [M_TYPE, M_CALL, M_CLONE, M_MANAGED]


def _get_name(name):
    return "".join((OS_PREFIX, name))


def _get_unmanaged(name):
    return "".join((UNMANAGE_PREFIX, name))


def lookup(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        obj = args[0]
        name = "_" + func.func_name + "_" + obj.apiv.replace(".", "_")
        LOG.debug("Trying method: %s", name)
        call_id = uuid.uuid4()
        if obj.do_profile:
            LOG.debug("Profiling method: %s, id %s", name, call_id)
            t1 = time.time()
        obj.thread_local.trace_id = call_id
        result = getattr(obj, name)(*args[1:], **kwargs)
        if obj.do_profile:
            t2 = time.time()
            timedelta = round(t2 - t1, 3)
            LOG.debug("Profile for method %s, id %s: %ss",
                      name, call_id, timedelta)
        return result
    return wrapper


def _parse_vol_ref(ref):
    if ref.count(":") not in (2, 3):
        raise exception.ManageExistingInvalidReference(
            _("existing_ref argument must be of this format: "
              "tenant:app_inst_name:storage_inst_name:vol_name or "
              "app_inst_name:storage_inst_name:vol_name"))
    try:
        (tenant, app_inst_name, storage_inst_name,
            vol_name) = ref.split(":")
        if tenant == "root":
            tenant = None
    except (TypeError, ValueError):
        app_inst_name, storage_inst_name, vol_name = ref.split(
            ":")
        tenant = None
    return app_inst_name, storage_inst_name, vol_name, tenant


def _check_snap_ref(ref):
    if not SNAP_RE.match(ref):
        raise exception.ManageExistingInvalidReference(
            _("existing_ref argument must be of this format: "
              "1234567890.12345678"))
    return True


def _get_size(app_inst):
    """Helper method for getting the size of a backend object

    If app_inst is provided, we'll just parse the dict to get
    the size instead of making a separate http request
    """
    if 'data' in app_inst:
        app_inst = app_inst['data']
    sis = app_inst['storage_instances']
    found_si = sis[0]
    found_vol = found_si['volumes'][0]
    return found_vol['size']


def _get_volume_type_obj(driver, resource):
    type_id = resource.get('volume_type_id', None)
    # Handle case of volume with no type.  We still want the
    # specified defaults from above
    if type_id:
        ctxt = context.get_admin_context()
        volume_type = volume_types.get_volume_type(ctxt, type_id)
    else:
        volume_type = None
    return volume_type


def _get_policies_for_resource(driver, resource):
    volume_type = driver._get_volume_type_obj(resource)
    return driver._get_policies_for_volume_type(volume_type)


def _get_policies_for_volume_type(driver, volume_type):
    """Get extra_specs and qos_specs of a volume_type.

    This fetches the scoped keys from the volume type. Anything set from
     qos_specs will override key/values set from extra_specs.
    """
    # Handle case of volume with no type.  We still want the
    # specified defaults from above
    if volume_type:
        specs = volume_type.get('extra_specs', {})
    else:
        specs = {}

    # Set defaults:
    policies = {k.lstrip('DF:'): str(v['default']) for (k, v)
                in driver._init_vendor_properties()[0].items()}

    if volume_type:

        qos_specs_id = volume_type.get('qos_specs_id')
        if qos_specs_id is not None:
            ctxt = context.get_admin_context()
            qos_kvs = qos_specs.get_qos_specs(ctxt, qos_specs_id)['specs']
            if qos_kvs:
                specs.update(qos_kvs)
        # Populate updated value
        for key, value in specs.items():
            if ':' in key:
                fields = key.split(':')
                key = fields[1]
                policies[key] = value
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


def _image_accessible(driver, context, volume, image_meta):
    # Determine if image is accessible by current project
    pid = volume.get('project_id', '')
    public = False
    visibility = image_meta.get('visibility', None)
    LOG.debug("Image %(image)s visibility: %(vis)s",
              {"image": image_meta['id'], "vis": visibility})
    if visibility and visibility in ['public', 'community']:
        public = True
    elif visibility and visibility in ['shared', 'private']:
        # Do membership check.  Newton and before didn't have a 'shared'
        # visibility option, so we have to do this check for 'private'
        # as well
        gclient = glance.get_default_image_service()
        members = []
        # list_members is only available in Rocky+
        try:
            members = gclient.list_members(context, image_meta['id'])
        except AttributeError:
            # This is the fallback method for the same query
            try:
                members = gclient._client.call(context,
                                               'list',
                                               controller='image_members',
                                               image_id=image_meta['id'])
            except glance_exc.HTTPForbidden as e:
                LOG.warn(e)
        except glance_exc.HTTPForbidden as e:
            LOG.warn(e)
        members = list(members)
        LOG.debug("Shared image %(image)s members: %(members)s",
                  {"image": image_meta['id'], "members": members})
        for member in members:
            if (member['member_id'] == pid and
                    member['status'] == 'accepted'):
                public = True
                break
        if image_meta.get('is_public', False):
            public = True
        else:
            if image_meta.get('owner', '') == pid:
                public = True
    if not public:
        LOG.warning("Requested image is not "
                    "accessible by current Project.")
    return public


def _format_tenant(tenant):
    if tenant == "all" or (tenant and ('/root' in tenant or 'root' in tenant)):
        return '/root'
    elif tenant and ('/root' not in tenant and 'root' not in tenant):
        return "/" + "/".join(('root', tenant)).strip('/')
    return tenant


def create_tenant(driver, project_id):
    if driver.tenant_id.lower() == 'map':
        name = _get_name(project_id)
    elif driver.tenant_id:
        name = driver.tenant_id.replace('root', '').strip('/')
    else:
        name = 'root'
    if name:
        try:
            driver.api.tenants.create(name=name)
        except dfs_sdk.exceptions.ApiConflictError:
            LOG.debug("Tenant {} already exists".format(name))
    return _format_tenant(name)


def get_tenant(driver, project_id):
    if driver.tenant_id.lower() == 'map':
        return _format_tenant(_get_name(project_id))
    elif not driver.tenant_id:
        return _format_tenant('root')
    return _format_tenant(driver.tenant_id)


def cvol_to_ai(driver, resource):
    tenant = get_tenant(driver, resource['project_id'])
    try:
        # api.tenants.get needs a non '/'-prefixed tenant id
        driver.api.tenants.get(tenant.strip('/'))
    except dfs_sdk.exceptions.ApiNotFoundError:
        create_tenant(driver, resource['project_id'])
    cid = resource.get('id', None)
    if not cid:
        raise ValueError('Unsure what id key to use for object', resource)
    ais = driver.api.app_instances.list(
        filter='match(name,.*{}.*)'.format(cid),
        tenant=tenant)
    if not ais:
        raise exception.VolumeNotFound(volume_id=cid)
    return ais[0]


def cvol_to_dvol(driver, resource):
    tenant = get_tenant(driver, resource['project_id'])
    ai = cvol_to_ai(driver, resource)
    si = ai.storage_instances.list(tenant=tenant)[0]
    vol = si.volumes.list(tenant=tenant)[0]
    return vol


def register_driver(driver):
    for func in [_get_volume_type_obj,
                 _get_policies_for_resource,
                 _get_policies_for_volume_type,
                 _image_accessible,
                 get_tenant,
                 create_tenant,
                 cvol_to_ai,
                 cvol_to_dvol]:
        # PY27

        f = types.MethodType(func, driver)
        try:
            setattr(driver, func.func_name, f)
        # PY3+
        except AttributeError:
            setattr(driver, func.__name__, f)
