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
Configuration Options
=======

.. list-table:: Description of Datera volume driver configuration options
   :header-rows: 1
   :class: config-ref-table

   * - Configuration option = Default value
     - Description
   * - **[DEFAULT]**
     -
   * - ``datera_api_port`` = ``7717``
     - (String) Datera API port.
   * - ``datera_api_version`` = ``2``
     - (String) Datera API version.
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
     - (Bool) True to set acl 'allow_all' on volumes created
   * - ``datera_debug`` = ``False``
     - (Bool) True to set function arg and return logging
