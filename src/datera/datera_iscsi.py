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

import time
import uuid

import dfs_sdk
from eventlet.green import threading
from oslo_config import cfg
from oslo_log import log as logging
import six

from cinder import exception
from cinder.i18n import _
from cinder import utils
from cinder.volume.drivers.san import san

import cinder.volume.drivers.datera.datera_api21 as api21
import cinder.volume.drivers.datera.datera_api22 as api22
import cinder.volume.drivers.datera.datera_common as datc


LOG = logging.getLogger(__name__)

d_opts = [
    cfg.StrOpt('datera_api_port',
               default='7717',
               deprecated_for_removal=True,
               help='Datera API port.'),
    cfg.StrOpt('datera_api_version',
               default='2.2',
               deprecated_for_removal=True,
               help='Datera API version.'),
    cfg.StrOpt('datera_ldap_server',
               default=None,
               help='LDAP authentication server'),
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
               default='',
               help="If set to 'Map' --> OpenStack project ID will be mapped "
                    "implicitly to Datera tenant ID\n"
                    "If set to 'None' --> Datera tenant ID will not be used "
                    "during volume provisioning\n"
                    "If set to anything else --> Datera tenant ID will be the "
                    "provided value"),
    cfg.BoolOpt('datera_enable_image_cache',
                default=False,
                help="Set to True to enable Datera backend image caching"),
    cfg.StrOpt('datera_image_cache_volume_type_id',
               default=None,
               help="Cinder volume type id to use for cached volumes"),
    cfg.BoolOpt('datera_disable_profiler',
                default=False,
                help="Set to True to disable profiling in the Datera driver"),
    cfg.BoolOpt('datera_disable_extended_metadata',
                default=False,
                help="Set to True to disable sending additional metadata to "
                     "the Datera backend"),
    cfg.BoolOpt('datera_disable_template_override',
                default=False,
                help="Set to True to disable automatic template override of "
                     "the size attribute when creating from a template"),
    cfg.DictOpt('datera_volume_type_defaults',
                default={},
                help="Settings here will be used as volume-type defaults if "
                     "the volume-type setting is not provided.  This can be "
                     "used, for example, to set a very low total_iops_max "
                     "value if none is specified in the volume-type to "
                     "prevent accidental overusage.  Options are specified "
                     "via the following format, WITHOUT ANY 'DF:' PREFIX: "
                     "'datera_volume_type_defaults="
                     "iops_per_gb:100,bandwidth_per_gb:200...etc'."),
]


CONF = cfg.CONF
CONF.import_opt('driver_use_ssl', 'cinder.volume.driver')
CONF.register_opts(d_opts)


@six.add_metaclass(utils.TraceWrapperWithABCMetaclass)
class DateraDriver(san.SanISCSIDriver, api21.DateraApi, api22.DateraApi):

    VERSION = '2019.6.4.1'

    CI_WIKI_NAME = "datera-ci"

    HEADER_DATA = {'Datera-Driver': 'OpenStack-Cinder-{}'.format(VERSION)}

    def __init__(self, *args, **kwargs):
        super(DateraDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(d_opts)
        self.username = self.configuration.san_login
        self.password = self.configuration.san_password
        self.ldap = self.configuration.datera_ldap_server
        self.cluster_stats = {}
        self.datera_api_token = None
        self.interval = self.configuration.datera_503_interval
        self.retry_attempts = (self.configuration.datera_503_timeout /
                               self.interval)
        self.driver_prefix = str(uuid.uuid4())[:4]
        self.datera_debug = self.configuration.datera_debug

        if self.datera_debug:
            utils.setup_tracing(['method'])
        self.tenant_id = self.configuration.datera_tenant_id
        if self.tenant_id is None:
            self.tenant_id = ''
        self.defaults = self.configuration.datera_volume_type_defaults
        if self.tenant_id and self.tenant_id.lower() == 'none':
            self.tenant_id = None
        self.template_override = (
            not self.configuration.datera_disable_template_override)
        self.api_check = time.time()
        self.api_cache = []
        self.api_timeout = 0
        self.do_profile = not self.configuration.datera_disable_profiler
        self.do_metadata = (
            not self.configuration.datera_disable_extended_metadata)
        self.image_cache = self.configuration.datera_enable_image_cache
        self.image_type = self.configuration.datera_image_cache_volume_type_id
        self.thread_local = threading.local()
        self.datera_version = None
        self.apiv = None
        self.api = None
        self.filterf = self.get_filter_function()
        self.goodnessf = self.get_goodness_function()

        self.use_chap_auth = self.configuration.use_chap_auth
        self.chap_username = self.configuration.chap_username
        self.chap_password = self.configuration.chap_password

        backend_name = self.configuration.safe_get(
            'volume_backend_name')
        self.backend_name = backend_name or 'Datera'
        datc.register_driver(self)

    def do_setup(self, context):
        # If we can't authenticate through the old and new method, just fail
        # now.
        if not all([self.username, self.password]):
            msg = _("san_login and/or san_password is not set for Datera "
                    "driver in the cinder.conf. Set this information and "
                    "start the cinder-volume service again.")
            LOG.error(msg)
            raise exception.InvalidInput(msg)

        # Try each valid api version starting with the latest until we find
        # one that works
        for apiv in reversed(datc.API_VERSIONS):
            try:
                api = dfs_sdk.get_api(self.configuration.san_ip,
                                      self.username,
                                      self.password,
                                      'v{}'.format(apiv),
                                      disable_log=True,
                                      extra_headers=self.HEADER_DATA,
                                      thread_local=self.thread_local,
                                      ldap_server=self.ldap)
                system = api.system.get()
                LOG.debug('Connected successfully to cluster: %s', system.name)
                self.api = api
                self.apiv = apiv
                break
            except Exception as e:
                LOG.warning(e)

    # =================

    # =================
    # = Create Volume =
    # =================

    @datc.lookup
    def create_volume(self, volume):
        """Create a logical volume."""
        pass

    # =================
    # = Extend Volume =
    # =================

    @datc.lookup
    def extend_volume(self, volume, new_size):
        pass

    # =================

    # =================
    # = Cloned Volume =
    # =================

    @datc.lookup
    def create_cloned_volume(self, volume, src_vref):
        pass

    # =================
    # = Delete Volume =
    # =================

    @datc.lookup
    def delete_volume(self, volume):
        pass

    # =================
    # = Ensure Export =
    # =================

    @datc.lookup
    def ensure_export(self, context, volume, connector=None):
        """Gets the associated account, retrieves CHAP info and updates."""

    # =========================
    # = Initialize Connection =
    # =========================

    @datc.lookup
    def initialize_connection(self, volume, connector):
        pass

    # =================
    # = Create Export =
    # =================

    @datc.lookup
    def create_export(self, context, volume, connector):
        pass

    # =================
    # = Detach Volume =
    # =================

    @datc.lookup
    def detach_volume(self, context, volume, attachment=None):
        pass

    # ===================
    # = Create Snapshot =
    # ===================

    @datc.lookup
    def create_snapshot(self, snapshot):
        pass

    # ===================
    # = Delete Snapshot =
    # ===================

    @datc.lookup
    def delete_snapshot(self, snapshot):
        pass

    # ========================
    # = Volume From Snapshot =
    # ========================

    @datc.lookup
    def create_volume_from_snapshot(self, volume, snapshot):
        pass

    # ==========
    # = Retype =
    # ==========

    @datc.lookup
    def retype(self, ctxt, volume, new_type, diff, host):
        """Convert the volume to be of the new type.

        Returns a boolean indicating whether the retype occurred.
        :param ctxt: Context
        :param volume: A dictionary describing the volume to migrate
        :param new_type: A dictionary describing the volume type to convert to
        :param diff: A dictionary with the difference between the two types
        :param host: A dictionary describing the host to migrate to, where
                     host['host'] is its name, and host['capabilities'] is a
                     dictionary of its reported capabilities (Not Used).
        """
        pass

    # ==========
    # = Manage =
    # ==========

    @datc.lookup
    def manage_existing(self, volume, existing_ref):
        """Manage an existing volume on the Datera backend

        The existing_ref must be either the current name or Datera UUID of
        an app_instance on the Datera backend in a colon separated list with
        the storage instance name and volume name.  This means only
        single storage instances and single volumes are supported for
        managing by cinder.

        Eg.

        (existing_ref['source-name'] ==
             tenant:app_inst_name:storage_inst_name:vol_name)
        if using Datera 2.1 API

        or

        (existing_ref['source-name'] ==
             app_inst_name:storage_inst_name:vol_name)

        if using 2.0 API

        :param volume:       Cinder volume to manage
        :param existing_ref: Driver-specific information used to identify a
                             volume
        """
        pass

    @datc.lookup
    def manage_existing_snapshot(self, snapshot, existing_ref):
        """Brings an existing backend storage object under Cinder management.

        existing_ref is passed straight through from the API request's
        manage_existing_ref value, and it is up to the driver how this should
        be interpreted.  It should be sufficient to identify a storage object
        that the driver should somehow associate with the newly-created cinder
        snapshot structure.

        There are two ways to do this:

        1. Rename the backend storage object so that it matches the
           snapshot['name'] which is how drivers traditionally map between a
           cinder snapshot and the associated backend storage object.

        2. Place some metadata on the snapshot, or somewhere in the backend,
           that allows other driver requests (e.g. delete) to locate the
           backend storage object when required.

        If the existing_ref doesn't make sense, or doesn't refer to an existing
        backend storage object, raise a ManageExistingInvalidReference
        exception.

        :param snapshot:     Cinder volume snapshot to manage
        :param existing_ref: Driver-specific information used to identify a
                             volume snapshot
        """
        pass

    # ===================
    # = Manage Get Size =
    # ===================

    @datc.lookup
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

    @datc.lookup
    def manage_existing_snapshot_get_size(self, snapshot, existing_ref):
        """Return size of snapshot to be managed by manage_existing.

        When calculating the size, round up to the next GB.

        :param snapshot:     Cinder volume snapshot to manage
        :param existing_ref: Driver-specific information used to identify a
                             volume snapshot
        :returns size:       Volume snapshot size in GiB (integer)
        """
        pass

    # =========================
    # = Get Manageable Volume =
    # =========================

    @datc.lookup
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

    # ============================
    # = Get Manageable Snapshots =
    # ============================

    @datc.lookup
    def get_manageable_snapshots(self, cinder_snapshots, marker, limit,
                                 offset, sort_keys, sort_dirs):
        """List snapshots on the backend available for management by Cinder.

        Returns a list of dictionaries, each specifying a snapshot in the host,
        with the following keys:
        - reference (dictionary): The reference for a snapshot, which can be
        passed to "manage_existing_snapshot".
        - size (int): The size of the snapshot according to the storage
        backend, rounded up to the nearest GB.
        - safe_to_manage (boolean): Whether or not this snapshot is safe to
        manage according to the storage backend. For example, is the snapshot
        in use or invalid for any reason.
        - reason_not_safe (string): If safe_to_manage is False, the reason why.
        - cinder_id (string): If already managed, provide the Cinder ID.
        - extra_info (string): Any extra information to return to the user
        - source_reference (string): Similar to "reference", but for the
        snapshot's source volume.

        :param cinder_snapshots: A list of snapshots in this host that Cinder
                                 currently manages, used to determine if
                                 a snapshot is manageable or not.
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

    # ============
    # = Unmanage =
    # ============

    @datc.lookup
    def unmanage(self, volume):
        """Unmanage a currently managed volume in Cinder

        :param volume:       Cinder volume to unmanage
        """
        pass

    # ====================
    # = Fast Image Clone =
    # ====================

    @datc.lookup
    def clone_image(self, context, volume, image_location, image_meta,
                    image_service):
        """Clone an existing image volume."""
        pass

    # ====================
    # = Volume Migration =
    # ====================

    @datc.lookup
    def update_migrated_volume(self, context, volume, new_volume,
                               volume_status):
        """Return model update for migrated volume.
        Each driver implementing this method needs to be responsible for the
        values of _name_id and provider_location. If None is returned or either
        key is not set, it means the volume table does not need to change the
        value(s) for the key(s).
        The return format is {"_name_id": value, "provider_location": value}.
        :param volume: The original volume that was migrated to this backend
        :param new_volume: The migration volume object that was created on
                           this backend as part of the migration process
        :param original_volume_status: The status of the original volume
        :returns: model_update to update DB with any needed changes
        """
        pass

    # ================
    # = Volume Stats =
    # ================

    @datc.lookup
    def get_volume_stats(self, refresh=False):
        """Get volume stats.

        If 'refresh' is True, run update first.
        The name is a bit misleading as
        the majority of the data here is cluster
        data.
        """
        pass

    # =========
    # = Login =
    # =========

    @datc.lookup
    def login(self):
        pass

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
        LOG.debug("Using the following volume-type defaults: %s",
                  self.defaults)

        properties = {}

        self._set_property(
            properties,
            "DF:iops_per_gb",
            "Datera IOPS Per GB Setting",
            _("Setting this value will calculate IOPS for each volume of "
              "this type based on their size.  Eg. A setting of 100 will "
              "give a 1 GB volume 100 IOPS, but a 10 GB volume 1000 IOPS. "
              "A setting of '0' is unlimited.  This value is applied to "
              "total_iops_max and will be overridden by total_iops_max if "
              "iops_per_gb is set and a large enough volume is provisioned "
              "which would exceed total_iops_max"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('iops_per_gb', 0)))

        self._set_property(
            properties,
            "DF:bandwidth_per_gb",
            "Datera Bandwidth Per GB Setting",
            _("Setting this value will calculate bandwidth for each volume of "
              "this type based on their size in KiB/s.  Eg. A setting of 100 "
              "will give a 1 GB volume 100 KiB/s bandwidth, but a 10 GB "
              "volume 1000 KiB/s bandwidth. A setting of '0' is unlimited. "
              "This value is applied to total_bandwidth_max and will be "
              "overridden by total_bandwidth_max if set and a large enough "
              "volume is provisioned which woudl exceed total_bandwidth_max"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('bandwidth_per_gb', 0)))

        self._set_property(
            properties,
            "DF:placement_mode",
            "Datera Volume Placement Mode (deprecated)",
            _("'DEPRECATED: PLEASE USE 'placement_policy' on 3.3.X+ versions "
              " of the Datera product.  'single_flash' for "
              "single-flash-replica placement, "
              "'all_flash' for all-flash-replica placement, "
              "'hybrid' for hybrid placement"),
            "string",
            default=self.defaults.get('placement_mode', 'hybrid'))

        self._set_property(
            properties,
            "DF:placement_policy",
            "Datera Volume Placement Policy",
            _("Valid path to a media placement policy.  Example: "
              "/placement_policies/all-flash"),
            "string",
            default=self.defaults.get('placement_policy',
                                      'default'))

        self._set_property(
            properties,
            "DF:round_robin",
            "Datera Round Robin Portals",
            _("True to round robin the provided portals for a target"),
            "boolean",
            default="True" == self.defaults.get('round_robin', "False"))

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
            default=int(self.defaults.get('replica_count', replica_count)))

        self._set_property(
            properties,
            "DF:ip_pool",
            "Datera IP Pool",
            _("Specifies IP pool to use for volume.  If provided string "
              "contains commas, it will be split on the commas and each "
              "substring will be uses as a separate IP pool and the volume's "
              "IP pool will be chosen randomly from the list.  Example: "
              "'my-ip-pool1,my-ip-pool2,my-ip-pool3', next attach "
              "my-ip-pool2 was chosen randomly as the volume IP pool"),
            "string",
            default=self.defaults.get('ip_pool', 'default'))

        self._set_property(
            properties,
            "DF:template",
            "Datera Template",
            _("Specifies Template to use for volume provisioning"),
            "string",
            default=self.defaults.get('template', ''))

        # ###### QoS Settings ###### #
        self._set_property(
            properties,
            "DF:read_bandwidth_max",
            "Datera QoS Max Bandwidth Read",
            _("Max read bandwidth setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('read_bandwidth_max', 0)))

        self._set_property(
            properties,
            "DF:write_bandwidth_max",
            "Datera QoS Max Bandwidth Write",
            _("Max write bandwidth setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('write_bandwidth_max', 0)))

        self._set_property(
            properties,
            "DF:total_bandwidth_max",
            "Datera QoS Max Bandwidth Total",
            _("Max total bandwidth setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('total_bandwidth_max', 0)))

        self._set_property(
            properties,
            "DF:read_iops_max",
            "Datera QoS Max iops Read",
            _("Max read iops setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('read_iops_max', 0)))

        self._set_property(
            properties,
            "DF:write_iops_max",
            "Datera QoS Max IOPS Write",
            _("Max write iops setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('write_iops_max', 0)))

        self._set_property(
            properties,
            "DF:total_iops_max",
            "Datera QoS Max IOPS Total",
            _("Max total iops setting for volume qos, "
              "use 0 for unlimited"),
            "integer",
            minimum=0,
            default=int(self.defaults.get('total_iops_max', 0)))
        # ###### End QoS Settings ###### #

        return properties, 'DF'
