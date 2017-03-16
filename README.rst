=======
Datera Cinder Repository
=======

.. list-table:: Datera Driver Versions
   :header-rows: 1
   :class: config-ref-table

   * - OpenStack Release
     - Driver Branch Name
     - Driver Version
     - Additional Capabilities Introduced
     - Supported Datera Product Versions
     - URL
   * - Ocata
     - ocata-v2.3.2
     - 2.3.2
     - Scalability bugfixes, Volume Placement, ACL multi-attach bugfix
     - 1.0, 1.1, 2.1
     - Driver now consists of a folder "datera/" with the following files:
       * datera_iscsi.py
       * datera_api2.py
       * datera_api21.py
       * datera_common.py
       ****IMPORTANT****: cinder.conf must be changed so `volume_driver = 
       cinder.volume.drivers.datera.datera_iscsi.DateraDriver`
   * - Ocata
     - ocata-v2.3.0
     - 2.3.0
     - Templates, Tenants, 2.1 API Support, Code Restructure
     - 1.0, 1.1, 2.1
     - Driver now consists of a folder "datera/" with the following files:
       * datera_iscsi.py
       * datera_api2.py
       * datera_api21.py
       * datera_common.py
       ****IMPORTANT****: cinder.conf must be changed so `volume_driver = 
       cinder.volume.drivers.datera.datera_iscsi.DateraDriver`
   * - Newton
     - newton-v2.3.2
     - 2.3.2
     - Scalability bugfixes, Volume Placement, ACL multi-attach bugfix
     - 1.0, 1.1, 2.1
     - Driver now consists of a folder "datera/" with the following files:
       * datera_iscsi.py
       * datera_api2.py
       * datera_api21.py
       * datera_common.py
       ****IMPORTANT****: cinder.conf must be changed so `volume_driver = 
       cinder.volume.drivers.datera.datera_iscsi.DateraDriver`
   * - Newton
     - newton-v2.3.0
     - 2.3.0
     - Templates, Tenants, 2.1 API Support, Code Restructure
     - 1.0, 1.1, 2.1
     - Driver now consists of a folder "datera/" with the following files:
       * datera_iscsi.py
       * datera_api2.py
       * datera_api21.py
       * datera_common.py
       ****IMPORTANT****: cinder.conf must be changed so `volume_driver = 
       cinder.volume.drivers.datera.datera_iscsi.DateraDriver`
   * - Newton
     - newton-v2.2.1
     - 2.2.1
     - Capabilities List, Extended Volume-Type Support, Naming Convention Change, Manage/Unmanage Snapshot polling
     - 1.0, 1.1
     - https://raw.githubusercontent.com/Datera/cinder-driver/newton-v2.2.1/src/datera.py
   * - Newton
     - newton-v2.2
     - 2.2
     - Capabilities List, Extended Volume-Type Support, Naming Convention Change, Manage/Unmanage
     - 1.0, 1.1
     - https://raw.githubusercontent.com/Datera/cinder-driver/newton-v2.2/src/datera.py
   * - Newton
     - newton-v2.1
     - 2.1
     - Multipathing, ACL
     - 1.0, 1.1
     - https://raw.githubusercontent.com/Datera/cinder-driver/newton-v2.1/src/datera.py
   * - Mitaka
     - mitaka-v2.3.2
     - 2.3.2
     - Scalability bugfixes, Volume Placement, ACL multi-attach bugfix
     - 1.0, 1.1, 2.1
     - Driver now consists of a folder "datera/" with the following files:
       * datera_iscsi.py
       * datera_api2.py
       * datera_api21.py
       * datera_common.py
       ****IMPORTANT****: cinder.conf must be changed so `volume_driver = 
       cinder.volume.drivers.datera.datera_iscsi.DateraDriver`
   * - Mitaka
     - mitaka-v2.3.0
     - 2.3.0
     - Templates, Tenants, 2.1 API Support, Code Restructure
     - 1.0, 1.1, 2.1
     - Driver now consists of a folder "datera/" with the following files:
       * datera_iscsi.py
       * datera_api2.py
       * datera_api21.py
       * datera_common.py
       ****IMPORTANT****: cinder.conf must be changed so `volume_driver = 
       cinder.volume.drivers.datera.datera_iscsi.DateraDriver`
   * - Mitaka
     - mitaka-v2.1.1
     - 2.1.1
     - Multipathing, ACL, Storage Instance Polling
     - 1.0, 1.1, 1.1.6
     - https://raw.githubusercontent.com/Datera/cinder-driver/mitaka-v2.1.1/src/datera.py
   * - Mitaka
     - mitaka-v2.1.2
     - 2.1.2
     - Multipathing, ACL, Storage Instance Polling, Snapshot Polling
     - 1.1, 1.1.6, 1.1.7, 2.0
     - https://raw.githubusercontent.com/Datera/cinder-driver/mitaka-v2.1.2/src/datera.py
   * - Mitaka
     - mitaka-v2.1.3
     - 2.1.3
     - Multipathing, ACL, Storage Instance Polling, Snapshot Polling, IP Pool bugfix
     - 1.1, 1.1.6, 1.1.7, 2.0
     - https://raw.githubusercontent.com/Datera/cinder-driver/mitaka-v2.1.3/src/datera.py
   * - Mitaka
     - mitaka-v2
     - 2.0
     - Baseline Driver
     - 1.0, 1.1
     - https://raw.githubusercontent.com/Datera/cinder-driver/mitaka-v2/src/datera.py
   * - Liberty
     - liberty-v2.1
     - 2.1
     - Multipathing, ACL
     - 1.0, 1.1
     - https://raw.githubusercontent.com/Datera/cinder-driver/liberty-v2.1/src/datera.py
   * - Liberty
     - liberty-v2
     - 2.0
     - Baseline Driver
     - 1.0, 1.1
     - https://raw.githubusercontent.com/Datera/cinder-driver/liberty-v2/src/datera.py

=======
Cinder.conf Options
=======

.. list-table:: Description of Datera volume driver configuration options
   :header-rows: 1
   :class: config-ref-table

   * - Configuration option = Default value
     - Description
   * - ``datera_api_port`` = ``7717``
     - (DEPRECATED) (String) Datera API port.
   * - ``datera_api_version`` = ``2``
     - (DEPRECATED) (String) Datera API version.
   * - ``datera_num_replicas`` = ``1``
     - (String) Number of replicas to create of an inode.
   * - ``driver_client_cert`` = ``None``
     - (String) The path to the client certificate for verification, if the driver supports it.
   * - ``driver_client_cert_key`` = ``None``
     - (String) The path to the client certificate key for verification, if the driver supports it.
   * - ``datera_503_timeout`` = ``120``
     - (Int) Timeout for HTTP 503 retry messages
   * - ``datera_503_interval`` = ``5``
     - (Int) Interval between 503 retries
   * - ``datera_acl_allow_all`` = ``False``
     - (DEPRECATED) (Bool) True to set acl 'allow_all' on volumes created
   * - ``datera_debug`` = ``False``
     - (Bool) True to set function arg and return logging
   * - ``datera_debug_replica_count_override`` = ``False``
     - (Bool) True to set replica_count to 1
   * - ``datera_tenant_id`` = ``None``
     - (String) If set to 'Map' --> OpenStack project ID will be mapped implicitly to Datera tenant ID. If set to 'None' --> Datera tenant ID will not be used during volume provisioning. If set to anything else --> Datera tenant ID will be the provided value
   * - ``datera_disable_profiler`` = ``False``
     - (Bool) Set to True to disable profiling in the Datera driver


=======
Volume-Type ExtraSpecs
=======

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
     - (String) 'single_flash' for single-flash-replica placement.  'all_flash' for all-flash-replica placement. 'hybrid' for hybrid placement.
   * - ``DF:acl_allow_all`` = ``False``
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

