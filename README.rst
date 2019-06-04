========================
Datera Cinder Repository
========================

----------------------------------------
Datera Cinder Volume Driver Installation
----------------------------------------

.. code-block::

    1. Clone Repository ``git clone http://github.com/Datera/cinder-driver``
    2. Copy Driver ``cd cinder-driver && cp src/datera/*.py /usr/local/lib/pythonX.X/dist-packages/cinder/volume/drivers/datera``
    3. Install Python-SDK ``sudo pip install -U dfs_sdk``
    4. Modify ``/etc/cinder/cinder.conf``
      * Under [DEFAULT]
        * ``default_volume_type = datera``
        * ``enabled_backends = datera``
        * ``debug = True``
      * Under [datera]
        * ``volume_driver = cinder.volume.drivers.datera.datera_iscsi.DateraDriver``
        * ``san_ip = <datera_mgmt_ip>``
        * ``san_login = <datera_username>``
        * ``san_password = <datera_password>``
        * ``volume_backend_name = datera``
        * ``datera_tenant_id = <datera_tenant>``
    4. Restart Cinder ``service cinder-volume restart``


If you get an error like:

.. code-block::

    ImportError: No module named dfs_sdk

Then you can install the Python-SDK manually via

.. code-block::

    $ pip install -U dfs_sdk


------------------------------------
Datera Volume Driver Version History
------------------------------------

.. list-table:: Version History for Datera Volume Driver
   :header-rows: 1
   :class: config-ref-table

   * - Version
     - Changes
   * - 2019.2.25.0
     - Scalability fixes and utility script updates
   * - 2019.1.24.0
     - Python-SDK requirements update, README updates
   * - 2018.11.14.0
     - Bugfixes for v2.1 API support and unicode character support
   * - 2018.11.8.0
     - Fixing bug that broke 2.2.X support
   * - 2018.11.1.0
     - Added flash and hybrid capacity info to get_volume_stats
   * - 2018.10.30.0
     - Adding template_override support.  Added datera_disable_template_override cfgOpt to disable this feature.  Updated required requests version to >=2.20.0 because of a security vulnerability in <=2.19.X.  Added support for filter_function and goodness_function.
   * - 2018.10.8.1
     - Adding thread_local to Python-SDK constructor call. This preserves trace_id in the logs
   * - 2018.10.8.0
     - Added extra_headers to Python-SDK constructor call.  This allows for the SDK to send the type of driver performing each request along with the request.  This functionality existed before the Python-SDK revamp, so this change adds the functionality back in.
   * - 2018.9.17.0
     - Readme and requirements.txt changes
   * - 2018.7.13.0
     - Massive update porting to use the Datera Python-SDK
   * - 2018.6.7.0
     - Bugfix for missing project_id during image clone
   * - 2018.5.18.0
     - Bugfix for None tenant handling
   * - 2018.5.1.0
     - Bugfix for Map tenant auto-creation
   * - 2018.4.27.0
     - Major driver revamp/restructure, no functionality change
   * - 2018.4.17.1
     - Bugfixes to IP Pools, Templates and Initiators
   * - 2018.4.5.0
     - Switch to new date-based versioning scheme.  Removed v2 API support
   * - 2.9.2
     - Made ensure_export a no-op.  Removed usage of initiator-groups
   * - 2.9.1
     - Added extended metadata attributes during volume creation and attachment.  Added datera_disable_extended_metadata option to disable it.
   * - 2.9.0
     - Volumes now correctly renamed during backend migration. Implemented update_migrated_volume (API 2.1+ only)
   * - 2.8.7
     - Bugfix for missing tenancy calls in offline_flip
   * - 2.8.6
     - Added LDAP support and CHAP support
   * - 2.8.5
     - Membership check for fast image cloning. Metadata API pinning
   * - 2.8.4
     - Fixed missing API version pinning in _offline_flip
   * - 2.8.3
     - Bugfix for not allowing fast clones for shared/community volumes
   * - 2.8.2
     - Updated most retype operations to not detach volume
   * - 2.8.1
     - Bugfix for missing default dict during retype
   * - 2.8.0
     - LDAP Support
   * - 2.7.2
     - Allowing DF: QoS Spec prefix, QoS type leak bugfix
   * - 2.7.0
     - IOPS/GB and BW/GB settings, driver level overrides
   * - 2.6.1
     - Glance interoperability fix
   * - 2.6.0
     - Api 2.2 support
   * - 2.5.0
     - Glance Image Caching, retyping/QoS bugfixes
   * - 2.4.0
     - Fast Retype Support
   * - 2.3.2
     - Volume Placement, ACL multi-attach bugfix
   * - 2.3.1
     - Scalability bugfixes
   * - 2.3
     - Templates, Tenants, Snapshot Polling, 2.1 Api Version Support, Restructure
   * - 2.2
     - Capabilites List, Extended Volume-Type Support Naming convention change, Volume Manage/Unmanage support
   * - 2.1
     - Multipath, ACL and reorg
   * - 2.0
     - Update For Datera API v2
   * - 1.1
     - Look for lun-0 instead of lun-1.
   * - 1.0
     - Initial driver

---------------------------------
Volume Driver Cinder.conf Options
---------------------------------

.. list-table:: Description of Datera volume driver configuration options
   :header-rows: 1
   :class: config-ref-table

   * - Configuration option = Default value
     - Description
   * - ``san_ip`` = ``None``
     - (String) The IP address of the Datera management interface (REQUIRED)
   * - ``san_login`` = ``None``
     - (String) The username of the Datera account (REQUIRED)
   * - ``san_password`` = ``None``
     - (String) The password of the Datera account (REQUIRED)
   * - ``use_chap_auth`` = ``False``
     - (Bool) Set to True for CHAP authentication
   * - ``chap_username`` = ``None``
     - (String) Username to use for CHAP authentication with ISCSI volumes
   * - ``chap_password`` = ``None``
     - (String) Password to use for CHAP authentication with ISCSI volumes
   * - ``driver_client_cert_key`` = ``None``
     - (String) The path to the client certificate key for verification, if the driver supports it.
   * - ``datera_503_timeout`` = ``120``
     - (Int) Timeout for HTTP 503 retry messages
   * - ``datera_503_interval`` = ``5``
     - (Int) Interval between 503 retries
   * - ``datera_ldap_server`` = ``None``
     - (String) LDAP authentication server
   * - ``datera_debug`` = ``False``
     - (Bool) True to set function arg and return logging
   * - ``datera_debug_replica_count_override`` = ``False``
     - (Bool) True to set replica_count to 1
   * - ``datera_tenant_id`` = ``None``
     - (String) If set to 'Map' --> OpenStack project ID will be mapped implicitly to Datera tenant ID. If set to 'None' --> Datera tenant ID will not be used during volume provisioning. If set to anything else --> Datera tenant ID will be the provided value
   * - ``datera_disable_profiler`` = ``False``
     - (Bool) Set to True to disable profiling in the Datera driver
   * - ``datera_volume_type_defaults`` = ``None``
     - (Dict) Settings here will be used as volume-type defaults if the volume-type setting is not provided.  This can be used, for example, to set a very low total_iops_max value if none is specified in the volume-type to prevent accidental overusage.  Options are specified via the following format, WITHOUT ANY 'DF:' PREFIX: 'datera_volume_type_defaults= iops_per_gb:100,bandwidth_per_gb:200...etc'
   * - ``datera_enable_image_cache`` = ``False``
     - (Bool) Set to True to enable Datera backend image caching
   * - ``datera_image_cache_volume_type_id`` = ``None``
     - (String) Cinder volume type id to use for cached images

----------------------
Volume-Type ExtraSpecs
----------------------

.. list-table:: Description of Datera volume-type extra specs
   :header-rows: 1
   :class: config-ref-table

   * - Configuration option = Default value
     - Description
   * - ``DF:replica_count`` = ``3``
     - (Int) Specifies number of replicas for each volume. Can only increase, never decrease after volume creation
   * - ``DF:round_robin`` = ``False``
     - (Bool) True to round robin the provided portals for a target
   * - ``DF:placement_mode`` = ``hybrid``
     - (Bool) True to set acl 'allow_all' on volume created.  Cannot be changed on volume once set
   * - ``DF:ip_pool`` = ``default``
     - (String) Specifies IP pool to use for volume
   * - ``DF:template`` = ``""``
     - (String) Specifies Datera Template to use for volume provisioning
   * - ``DF:default_storage_name`` = ``storage-1``
     - (String) The name to use for storage instances created
   * - ``DF:default_volume_name`` = ``volume-1``
     - (String) The name to use for volumes created
   * - ``DF:read_bandwidth_max`` = ``0``
     - (Int) Max read bandwidth setting for volume QoS.  Use 0 for unlimited
   * - ``DF:write_bandwidth_max`` = ``0``
     - (Int) Max write bandwidth setting for volume QoS.  Use 0 for unlimited
   * - ``DF:total_bandwidth_max`` = ``0``
     - (Int) Total write bandwidth setting for volume QoS.  Use 0 for unlimited
   * - ``DF:read_iops_max`` = ``0``
     - (Int) Max read IOPS setting for volume QoS.  Use 0 for unlimited
   * - ``DF:write_iops_max`` = ``0``
     - (Int) Max write IOPS setting for volume QoS.  Use 0 for unlimited
   * - ``DF:total_iops_max`` = ``0``
     - (Int) Total write IOPS setting for volume QoS.  Use 0 for unlimited
   * - ``DF:iops_per_gb`` = ``0``
     - (Int) IOPS per GB of data allocated for the volume.  If this value exceeds the total_max_iops value, the total_max_iops will be used instead
   * - ``DF:bandwidth_per_gb`` = ``0``
     - (Int) Bandwidth (KB/s) per GB of data allocated for the volume.  If this value exceeds the total_max_bandwidth value, the total_max_bandwidth will be used instead

------------------------------------
Collecting Logs for OpenStack Cinder
------------------------------------

If for some reason there is a problem with the OpenStack Cinder driver.  You
MUST collect the cinder-volume service logs.  Without logs, debugging issues
is significantly more difficult.

The location of the cinder-volume logs varies between installations but here
are a few places to check:

- /var/log/cinder
- /opt/stack/cinder
- journalctl -u cinder-volume

These places are usually only valid on the controller node running the
cinder-volume service

If they're not in any of the above places you will have to look into where
the service is saving logs.  Often they will be inside a container for the
service.

------------------------------------
Datera Cinder Backup Version History
------------------------------------

.. list-table:: Datera Backup Driver Versions
   :header-rows: 1
   :class: config-ref-table

   * - Version
     - Changes
   * - 1.0
     - Initial driver


---------------------------------
Backup Driver Cinder.conf Options
---------------------------------
.. list-table:: Description of Datera backup driver configuration options
   :header-rows: 1
   :class: config-ref-table

   * - Configuration option = Default value
     - Description
   * - ``backup_datera_san_ip`` = ``None``
     - (Required) (String) Datera EDF Mgmt IP
   * - ``backup_datera_san_login`` = ``None``
     - (Required) (String) Datera EDF Username
   * - ``backup_datera_san_password`` = ``None``
     - (Required) (String) Datera EDF Password
   * - ``backup_datera_tenant_id`` = ``/root``
     - (Required) (String) Datera EDF Tenant
   * - ``backup_datera_chunk_size`` = ``1``
     - (Int) Total chunk size (in GB, min 1 GB) to use for backup
   * - ``backup_datera_progress_timer`` = ``False``
     - (Bool) Enable progress timer for backup
   * - ``backup_datera_replica_count`` = ``3``
     - (Int) Number of replicas for each backup container
   * - ``backup_datera_placement_mode`` = ``hybrid``
     - (String) Options: hybrid, single_flash, all_flash
   * - ``backup_datera_api_port`` = ``7717``
     - (String) Datera EDF API port
   * - ``backup_datera_secondary_backup_drivers`` = []
     - (List) Secondary backup drivers for the Datera EDF driver to manage

--------------------------------------
Backup Driver Dispatching/Multiplexing
--------------------------------------
As of backup driver version 1.0.1 we allow for managing multiple secondary
backup driver backends.  Vanilla Cinder supports only a single backup driver
backend in an OpenStack cluster.  We've added backup driver dispatching to the
Datera EDF backup driver to allow for multiple backup driver backends to be used
along side the Datera EDF backup driver backend.

To utilize this function, set the following in your cinder.conf:

.. code-block:: bash

    backup_datera_secondary_backup_drivers = your.backup.driver.module

If you wanted to use Ceph, you would set this to:

.. code-block:: bash

    backup_datera_secondary_backup_drivers = cinder.backup.drivers.ceph

You would then use the following naming convention to select which backend you
want to store the backup on:

.. code-block:: bash

    openstack volume backup create your_volume --name <driver_module>_you_backup_name

Where <driver_module> is replaced by the module of the driver you want to use.
In the case of Ceph it would be "ceph".  Example:

.. code-block:: bash

    openstack volume backup create hadoop1 --name ceph_hadoop1_backup

If no name is specified the Datera EDF driver will be used, but you can also use
the following to manually specify the Datera EDF backup driver:

.. code-block:: bash

    openstack volume backup create cassandra1 --name datera_cassandra1_backup
