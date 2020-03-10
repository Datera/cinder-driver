#!/usr/bin/env python

"""
Requires:

    Python
    ------
    paramiko

    Utilities
    ---------
    nslookup
    ipmitool
    perl
    git
    sed
"""

from __future__ import unicode_literals, print_function, division

import argparse
import sys
import subprocess
import time

try:
    import paramiko
except ImportError:
    print("Paramiko required to run script, run 'pip install paramiko'")
    sys.exit(1)

# Py 2/3 compat
try:
    str = unicode
except NameError:
    pass


SUCCESS = 0
FAILURE = 1
WAIT_TIME = 60
REBOOT_WAIT = 60 * 60
DEVSTACK_WAIT = 3600
TEMPEST_WAIT = 3600
INITIAL_SSH_TIMEOUT = 600
DAT_CINDER_URL = "http://github.com/Datera/cinder-driver"
DAT_GLANCE_URL = "http://github.com/Datera/glance-driver"
DEV_DRIVER_LOC = "/opt/stack/cinder/cinder/"
DEV_GLANCE_CONF = "/etc/glance/glance-api.conf"

LOCALCONF = r"""
[[local|localrc]]
SERVICE_HOST=127.0.0.1
ACTIVE_TIMEOUT=90
BOOT_TIMEOUT=90
ASSOCIATE_TIMEOUT=60
TERMINATE_TIMEOUT=60
MYSQL_PASSWORD=secrete
DATABASE_PASSWORD=secrete
RABBIT_PASSWORD=secrete
ADMIN_PASSWORD=secrete
SERVICE_PASSWORD=secrete
SERVICE_TOKEN=111222333444
LIBVIRT_TYPE=kvm

IP_VERSION=4
USE_PYTHON3=True

# Screen console logs will capture service logs.
LOGDIR=/opt/stack/logs
LOGFILE=/opt/stack/devstacklog.txt
VERBOSE=True
VIRT_DRIVER=libvirt
CINDER_PERIODIC_INTERVAL=20
CINDER_SECURE_DELETE=False
API_RATE_LIMIT=False
TEMPEST_HTTP_IMAGE=http://127.0.0.1/

# Add these until pbr 1.8 lands in reqs
REQUIREMENTS_MODE=strict
# Set to False to disable the use of upper-constraints.txt
# if you want to experience the wild freedom of uncapped
# dependencies from PyPI
# USE_CONSTRAINTS=True

# Currently skipped in the gate, so lets skip them too
SKIP_EXERCISES=boot_from_volume,bundle,client-env,euca

# Settings to enable use of Datera
CINDER_ENABLED_BACKENDS=datera
TEMPEST_VOLUME_DRIVER=DateraDriver
TEMPEST_VOLUME_VENDOR=Datera
TEMPEST_STORAGE_PROTOCOL=iSCSI

disable_service horizon

CINDER_BRANCH={patchset}

[[post-config|/etc/cinder/cinder.conf]]
[DEFAULT]
iscsi_target_prefix=iqn:
CINDER_ENABLED_BACKENDS=datera
default_volume_type = datera
enabled_backends = datera

[datera]
volume_driver=cinder.volume.drivers.datera.datera_iscsi.DateraDriver
san_is_local=True
san_ip={mgmt_ip}
san_login=admin
san_password=password
datera_tenant_id={tenant}
volume_backend_name=datera
datera_debug_replica_count_override=True
"""

class SSH(object):
    def __init__(self, ip, username, password, keyfile=None):
        self.ip = ip
        self.username = username
        self.password = password
        self.keyfile = keyfile
        # print(self.ip, self.username, self.password, self.keyfile)

        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(
            paramiko.AutoAddPolicy())
        # Normal username/password usage
        self.ssh.connect(
            hostname=self.ip,
            username=self.username,
            password=self.password,
            key_filename=self.keyfile,
            banner_timeout=INITIAL_SSH_TIMEOUT)

    def reconnect(self, timeout):
        self.ssh.connect(
            hostname=self.ip,
            username=self.username,
            password=self.password,
            key_filename=self.keyfile,
            banner_timeout=timeout)

    def exec_command(self, command, fail_ok=False, wait=True, quiet=False):
        s = self.ssh
        if not quiet:
            msg = "Executing command: {} on VM: {}".format(command, self.ip)
            print(msg)
        _, stdout, stderr = s.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        result = None
        if int(exit_status) == 0:
            if wait:
                result = stdout.read()
            else:
                return result
        elif fail_ok:
            result = stderr.read()
        else:
            raise EnvironmentError(
                "Nonzero return code: {} stderr: {}".format(
                    exit_status,
                    stderr.read()))
        return result.decode('utf-8')

def setup_interfaces(ssh):
    cmd = "ip link set dev eth3 up"
    ssh.exec_command(cmd)

    cmd = "ip addr add 172.28.41.8/24 dev eth3"
    ssh.exec_command(cmd, fail_ok=True)

    cmd = "ip link set dev eth2 up"
    ssh.exec_command(cmd)

    cmd = "ip addr add 172.29.41.8/24 dev eth2"
    ssh.exec_command(cmd, fail_ok=True)

def disable_ipv6(ssh):
    cmd = "echo 'net.ipv6.conf.all.disable_ipv6 = 1' |  sudo tee --append /etc/sysctl.d/99-sysctl.conf"
    ssh.exec_command(cmd)

    cmd = "echo 'net.ipv6.conf.default.disable_ipv6 = 1' |  sudo tee --append /etc/sysctl.d/99-sysctl.conf"
    ssh.exec_command(cmd)

    cmd = "echo 'net.ipv6.conf.lo.disable_ipv6 = 1' |  sudo tee --append /etc/sysctl.d/99-sysctl.conf"
    ssh.exec_command(cmd)

    cmd = "sudo sysctl -p"
    ssh.exec_command(cmd)

def setup_stack_user(ssh):
    cmd = ""
    try:
        ssh.exec_command("which yum")
        cmd = "yum install git python-setuptools -y"
    except EnvironmentError:
        cmd = "apt-get install git -y"
    ssh.exec_command(cmd)

    cmd = "rm -rf devstack"
    ssh.exec_command(cmd)

    cmd = "git clone http://github.com/openstack-dev/devstack"
    ssh.exec_command(cmd)

    cmd = "cd devstack/tools && ./create-stack-user.sh"
    ssh.exec_command(cmd)

    cmd = "passwd stack"
    msg = "Executing command: {} on VM: {}".format(cmd, ssh.ip)
    print(msg)
    stdin, _, _ = ssh.ssh.exec_command(cmd)
    stdin.write('stack\n')
    stdin.write('stack\n')

def install_devstack(ssh, cluster_ip, tenant, patchset, version):
    print("Installing Devstack...")

    cmd = "sudo apt-get install python-pip -y"
    ssh.exec_command(cmd)
    cmd = "sudo pip install --upgrade pip"
    ssh.exec_command(cmd)

    cmd = "rm -rf devstack"
    ssh.exec_command(cmd)

    cmd = "git clone http://github.com/openstack-dev/devstack"
    ssh.exec_command(cmd)
    lcnf = LOCALCONF.format(
        mgmt_ip=cluster_ip,
        tenant=tenant,
        patchset=patchset)
    ssh.exec_command('echo "{}" > devstack/local.conf'.format(lcnf))
    if _install_devstack(ssh, version) == "SetupTools":
        # For some reason this is removed by devstack during initial setup
        # but sticks after we reinstall it and re-stack
        ssh.exec_command("sudo yum install python-setuptools -y")
        _unstack(ssh)
        _install_devstack(ssh, version)


def _unstack(ssh):
    ssh.exec_command("cd devstack && ./unstack.sh >/dev/null 2>&1 &")
    time.sleep(30)


def _install_devstack(ssh, version):
    if version != "master":
        cmd = ("cd devstack && git checkout {}".format(version))
        ssh.exec_command(cmd)
    cmd = "cd devstack && nohup ./stack.sh >/dev/null 2>&1 &"
    ssh.exec_command(cmd, wait=False)

    count = 0
    while count <= DEVSTACK_WAIT:
        try:
            ssh.exec_command(
                ("grep 'This is your host IP address:' "
                 "/opt/stack/devstacklog.txt"), quiet=True)
            break
        except EnvironmentError:
            try:
                # This means python-setuptools needs to be reinstalled again
                ssh.exec_command(
                    ("grep 'operator not allowed in environment markers' "
                     "/opt/stack/devstacklog.txt"), quiet=True)
                return "SetupTools"
            except EnvironmentError:
                pass
            time.sleep(WAIT_TIME)
            count += WAIT_TIME
            print(ssh.exec_command(
                "tail -1 /opt/stack/devstacklog.txt.summary", quiet=True).strip())
    if count >= DEVSTACK_WAIT:
        raise EnvironmentError("Timeout expired before stack.sh "
                               "completed")


def _update_drivers(ssh, mgmt_ip, patchset, cinder_version, glance_version):
    # Install python sdk to ensure we're using latest version
    ssh.exec_command("sudo pip install dfs_sdk")

    # Check out upstream cinder version and make sure it's clean master
    ssh.exec_command("cd /opt/stack/cinder && git clean -f"
                     "                     && git checkout master"
                     "                     && git reset --hard"
                     "                     && git pull")

    # Check out Datera cinder-driver
    ssh.exec_command("rm -rf -- cinder-driver")
    ssh.exec_command("git clone {} cinder-driver".format(DAT_CINDER_URL))

    if cinder_version != "master":
        ssh.exec_command("cd cinder-driver && git checkout {}".format(
            cinder_version))
    # Rsync the current directory tree from ./src to DEV_DRIVER_LOC
    # Currently backup/ subdirectory excluded because of missing tests. FIXME
    ssh.exec_command("cd cinder-driver/ && rsync -a --exclude '__init__.py' "
                     "--exclude 'backup/' src/cinder/ {}".format(DEV_DRIVER_LOC))

    # If a patchset is provided, overwrite the driver with the given patchset
    # Useful for gerrit gating
    if patchset != "master":
        ssh.exec_command(
            "cd /opt/stack/cinder"
            " && git checkout ."
            " && git fetch https://review.opendev.org/openstack/cinder {patchset}"
            " && git checkout FETCH_HEAD".format(patchset=patchset))

    ssh.exec_command("sudo systemctl restart devstack@c-vol.service")
    ssh.exec_command("sudo systemctl restart devstack@c-sch.service")

    # Cause sometimes we just don't need this complexity
    if glance_version == "none":
        return
    # Install glance driver
    install, entryp = _find_glance_dirs(ssh)
    if not install or not entryp:
        print("Could not find all glance install directories: [{}, {}]".format(
            install, entryp))
    ssh.exec_command("rm -rf -- glance-driver")
    ssh.exec_command("git clone {}".format(DAT_GLANCE_URL))
    ssh.exec_command("cd glance-driver/src && sudo cp *.py {}".format(
        "/".join((install, "_drivers"))))
    # Modify entry_points.txt file
    cmd = ("sudo sed -i 's/vmware = glance_store._drivers.vmware_datastore:"
           "Store/datera = glance_store._drivers.datera:Store/' {}".format(
               entryp))
    ssh.exec_command(cmd)
    # Modify backend.py file
    backend = "/".join((install, "backend.py"))
    cmd = "sudo sed -i 's/vsphere/datera/' {}".format(backend)
    ssh.exec_command(cmd)
    # Modify glance-api.conf file, this is gross, but the easiest way
    # of doing a straight replace of these strings.  We have to use
    # perl instead of sed because sed really doesn't want to match
    # multiline strings
    glance_store = """
[glance_store]
filesystem_store_datadir = /opt/stack/data/glance/images/
""".replace("/", "\\/").replace('[', '\\[').replace(']', '\\]')
    new_glance_store = """
[glance_store]
filesystem_store_datadir = /opt/stack/data/glance/images/
stores = file,datera
default_store = datera
datera_san_ip = {}
datera_san_login = admin
datera_san_password = password
datera_replica_count = 1
""".format(mgmt_ip).replace("/", "\\/").replace('[', '\\[').replace(']', '\\]')
    try:
        ssh.exec_command("grep datera_san_ip {}".format(DEV_GLANCE_CONF))
    except EnvironmentError:
        cmd = "perl -i -0pe 's/{}/{}/' {}".format(
                glance_store, new_glance_store, DEV_GLANCE_CONF)
        ssh.exec_command(cmd)
    # Modify glance filters
    cmd = "cd glance-driver/etc/glance && sudo cp -r * /etc/glance/"
    ssh.exec_command(cmd)
    ssh.exec_command("sudo systemctl restart devstack@g-api.service")


def _find_glance_dirs(ssh):
    cmd = "sudo find /usr -name 'glance_store' 2>/dev/null"
    install = ssh.exec_command(cmd).strip()
    cmd = "sudo find /usr -name 'glance_store*dist-info' 2>/dev/null"
    info = ssh.exec_command(cmd).strip()
    return (install, "/".join((info, "entry_points.txt")))

def run_tempest(ssh):
    ssh.exec_command("cd /opt/stack/cinder && echo cinder_commit_id "
                     "$(git rev-parse HEAD) > "
                     "/opt/stack/tempest/console.out.log")
    ssh.exec_command("cd /opt/stack/tempest && "
                     "tox -e all -- volume "
                     ">console.out.log 2>/dev/null &", wait = False)
    count = 0
    while count <= TEMPEST_WAIT:
        try:
            print("Checking test results ...")
            result = ssh.exec_command(
                "grep -oP 'Failed: \d+' /opt/stack/tempest/console.out.log", quiet=True)
            return int(result.split(":")[-1])
            break
        except EnvironmentError:
            time.sleep(WAIT_TIME)
            count += WAIT_TIME
    if count >= TEMPEST_WAIT:
        raise EnvironmentError("Timeout expired before tempest tests "
                               "completed")

def run_tox(ssh):
    ssh.exec_command("sudo apt-get -y install python3-dev")
    ssh.exec_command("sudo apt-get -y install python3.7-dev")
    ssh.exec_command("cd /opt/stack/cinder && tox -e genopts")
    ssh.exec_command("cd /opt/stack/cinder && "
                     "tox >console.out.log 2>/dev/null &", wait = False)
    count = 0
    while count <= TEMPEST_WAIT:
        try:
            print("Checking tox test results ...")
            result = ssh.exec_command(
                "grep -oP 'congratulations' /opt/stack/cinder/console.out.log", quiet=True)
            return SUCCESS
            break
        except EnvironmentError:
            time.sleep(WAIT_TIME)
            count += WAIT_TIME
    if count >= TEMPEST_WAIT:
        raise EnvironmentError("Timeout expired before tox tests completed")

def reinstall_node(ip, username, password):
    print("Wiping node: {}".format(ip))
    cmd = "resolveip -s {}".format(ip)
    print("Executing: {}".format(cmd))
    host = subprocess.check_output(cmd.split()).rstrip().split('.')[0]
    if not host:
        raise ValueError("Couldn't determine hostname from ip: {}".format(ip))
    cmd = "ipmitool -H {}-ipmi.tlx.daterainc.com -U root -P carnifex -I lanplus chassis bootdev pxe".format(host)
    print("Executing: {}".format(cmd))
    subprocess.check_output(cmd.split())
    cmd = "ipmitool -H {}-ipmi.tlx.daterainc.com -U root -P carnifex -I lanplus chassis power cycle".format(host)
    print("Executing: {}".format(cmd))
    subprocess.check_output(cmd.split())
    # Wait for node to reboot
    time.sleep(10)
    # Poll for node availability
    count = 0
    while count <= REBOOT_WAIT:
        try:
            ssh = SSH(ip, username, password)
            ssh.exec_command("uname -a")
            print("Wipe complete, node accessible")
            break
        except Exception as e:
            print("Sleeping, node inaccessible: {}".format(e))
            if count >= REBOOT_WAIT:
                print(e)
                raise EnvironmentError(
                    "Timeout expired before node became reachable")
            time.sleep(WAIT_TIME)
            count += WAIT_TIME
    print("Ubuntu node ready...")

def main(node_ip, username, password, cluster_ip, tenant, patchset,
         skip_tempest, skip_tox,
         devstack_version, cinder_driver_version, glance_driver_version,
         only_update_drivers, reimage_client):

    if only_update_drivers:
        if args.keyfile:
            ssh = SSH(node_ip, username, password, keyfile=args.keyfile)
        else:
            ssh = SSH(node_ip, 'stack', 'stack')
        _update_drivers(
            ssh, cluster_ip, patchset, cinder_driver_version,
            glance_driver_version)
    else:
        if reimage_client:
            reinstall_node(args.node_ip, args.username, args.password)
        root_ssh = SSH(args.node_ip, args.username, args.password)
        setup_stack_user(root_ssh)
        setup_interfaces(root_ssh)
        disable_ipv6(root_ssh)

        ssh = SSH(node_ip, 'stack', 'stack')
        install_devstack(ssh, cluster_ip, tenant, patchset, devstack_version)
        _update_drivers(ssh, cluster_ip, patchset, cinder_driver_version,
                        glance_driver_version)

    result = SUCCESS
    result2 = SUCCESS
    if not skip_tempest:
        result = run_tempest(ssh)
        if result == 0:
            print('Tempest Passed!!!')
        else:
            print("Tempest Failed :(, Failures: {}".format(result))
    else:
        print('Not running tempest tests...')

    if not skip_tox:
        result2 = run_tox(ssh)
        if result2 == SUCCESS:
            print('Tox tests Passed!!!')
        else:
            print("Tox tests Failed :(")
            print(ssh.exec_command(
                "tail -10 /opt/stack/cinder/console.out.log", quiet=True))
    else:
        print('Not running tox tests...')

    if result == 0:
        return result2
    else:
        return result

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('cluster_ip')
    parser.add_argument('node_ip')
    parser.add_argument('username')
    parser.add_argument('password')
    parser.add_argument('--keyfile')
    parser.add_argument('--cinder-driver-version', default='master')
    parser.add_argument('--glance-driver-version', default='master')
    parser.add_argument('--devstack-version', default='master')
    parser.add_argument('--tenant', default='')
    parser.add_argument('--patchset', default='master')
    parser.add_argument('--skip-tempest', action='store_true')
    parser.add_argument('--skip-tox', action='store_true')
    parser.add_argument('--reimage-client', action='store_true')
    parser.add_argument('--only-update-drivers', action='store_true')
    args = parser.parse_args()
    if args.password in {"", "none", "None"}:
        args.password = None
    # print(args)
    sys.exit(main(
        args.node_ip,
        args.username,
        args.password,
        args.cluster_ip,
        args.tenant,
        args.patchset,
        args.skip_tempest,
        args.skip_tox,
        args.devstack_version,
        args.cinder_driver_version,
        args.glance_driver_version,
        args.only_update_drivers,
        args.reimage_client))
