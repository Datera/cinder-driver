#!/usr/bin/env python

from __future__ import unicode_literals, division, print_function

import argparse
import io
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import traceback

import arrow
import boto
from boto.s3.connection import OrdinaryCallingFormat
from gerritlib.gerrit import Gerrit
from jinja2 import Template
import paramiko
import ruamel.yaml as yaml
import simplejson as json
from six.moves.queue import Queue, Empty

try:
    input = raw_input
except NameError:
    pass

PATCH_QUEUE = Queue()

UPLOAD_FILE = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)),
    'upload_logs.sh')

DEVSTACK_FILE = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)),
    'devstack_up.py')

SUCCESS = 0
FAIL = 1
DEBUG = False
ALL_EVENTS = False
BASE_URL = 'http://stkci.daterainc.com.s3-website-us-west-2.amazonaws.com/'
LISTING_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__name__)),
                                'listing.j2')

INITIAL_SSH_TIMEOUT = 600
EXIT = "3pci-exit"


def dprint(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


def tprint(*args, **kwargs):
    t = time.time()
    pt = time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.localtime(t))
    tid = threading.currentThread().ident
    print(tid, pt, *args, **kwargs)


def exe(cmd):
    dprint(cmd)
    return subprocess.check_output(shlex.split(cmd))


def create_indexes(rootdir, ref_name):
    template = Template(open(LISTING_TEMPLATE).read())
    previous_subdir = None
    stripped_subdir = None
    back_dir = None
    for subdir, dirs, file_names in os.walk(rootdir):
        files = []

        if previous_subdir != subdir:
            stripped_subdir = subdir.replace(rootdir, '')

            back_dir = re.sub(r'\/\w+$', '', stripped_subdir)

        for directory in dirs:
            full_path = os.path.join(stripped_subdir, directory)
            f = {
                'name': directory,
                'type': 'folder',
                'relpath': full_path.strip('/'),
                'topkey': ref_name,
                'dir': True}
            files.append(f)

        for file_name in file_names:
            full_path = os.path.join(stripped_subdir, file_name)

            f = {
                'name': file_name,
                'type': 'text',
                'topkey': ref_name,
                'relpath': full_path.strip('/')}
            files.append(f)

        previous_subdir = subdir

        output = template.render({
            'ref_name': ref_name,
            'back': back_dir.strip('/'),
            'files': files,
            'base_url': BASE_URL})

        index_file_path = os.path.join(subdir, 'index.html')

        with open(index_file_path, 'w') as fd:
            fd.write(output)


class SSH(object):
    def __init__(self, ip, username, password, keyfile=None):
        self.ip = ip
        self.username = username
        self.password = password
        self.keyfile = keyfile

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

    def exec_command(self, command, fail_ok=False):
        s = self.ssh
        msg = "Executing command: {} on VM: {}".format(command, self.ip)
        print(msg)
        _, stdout, stderr = s.exec_command(command)
        exit_status = stdout.channel.recv_exit_status()
        result = None
        if int(exit_status) == 0:
            result = stdout.read()
        elif fail_ok:
            result = stderr.read()
        else:
            raise EnvironmentError(
                "Nonzero return code: {} stderr: {}".format(
                    exit_status,
                    stderr.read()))
        return result.decode('utf-8')


class ThirdParty(object):

    def __init__(self, project, ghost, guser, gport, gkeyfile, ci_name,
                 aws_key_id, aws_secret_key, remote_bucket, upload=False,
                 use_existing_devstack=False):
        self.project = project
        self.gerrit = Gerrit(ghost, guser, port=gport, keyfile=gkeyfile)
        self.gerrit.ci_name = ci_name
        self.upload = upload
        self.aws_key_id = aws_key_id
        self.aws_secret_key = aws_secret_key
        self.remote_results_bucket = remote_bucket
        self.use_existing_devstack = use_existing_devstack

    def _post_results(self, ssh, success, log_location, commit_id):
        base_cmd = ("ssh -i {} -p 29418 {}@review.openstack.org "
                    "gerrit review -m ".format(
                        self.gerrit.keyfile, self.gerrit.username) + "'{}'")
        if success:
            cmd = base_cmd.format(
                "\"* {} {} : SUCCESS \" {}".format(
                    self.gerrit.ci_name,
                    log_location,
                    commit_id))
        else:
            cmd = base_cmd.format(
                "\"* {} {} : FAILURE \n You can rerun this CI by commenting "
                "run-Datera\" {}".format(
                    self.gerrit.ci_name,
                    log_location,
                    commit_id))
        dprint("Posting gerrit results:\n{}".format(cmd))
        ssh.exec_command(cmd)

    def run_ci_on_patch(self,
                        node_ip,
                        username,
                        password,
                        cluster_ip,
                        patchset,
                        cinder_driver_version,
                        glance_driver_version,
                        node_keyfile=None):
        """
        This will actually run the CI logic.  If post_failed is set to `False`,
        it will try again if it detects failure in the results.  This should
        hopefully decrease our false failure rate.
        """
        dprint("Running against: ", patchset)
        patch_ref_name = patchset.replace("/", "-")
        # Setup Devstack and run tempest
        if self.use_existing_devstack:
            exe(
                "{devstack_up} {cluster_ip} {node_ip} {username} {password} "
                "--patchset {patchset} "
                "--only-update-drivers "
                "--glance-driver-version none "
                "--keyfile {keyfile} "
                "--run-tempest".format(devstack_up=DEVSTACK_FILE,
                                       cluster_ip=cluster_ip,
                                       node_ip=node_ip,
                                       username=username,
                                       password=password,
                                       patchset=patchset,
                                       keyfile=node_keyfile)
            )
            ssh = SSH(node_ip, username, password, keyfile=node_keyfile)
        else:
            exe(
                "{devstack_up} {cluster_ip} {node_ip} {username} {password} "
                "--patchset {patchset} "
                "--reimage-client "
                "--glance-driver-version none "
                "--run-tempest ".format(devstack_up=DEVSTACK_FILE,
                                        cluster_ip=cluster_ip,
                                        node_ip=node_ip,
                                        username=username,
                                        password=password,
                                        patchset=patchset)
            )
        # Upload logs
        ssh = SSH(node_ip, username, password, keyfile=node_keyfile)
        success, log_location, commit_id = self._upload_logs(
            ssh, patch_ref_name, post_failed=False)
        # Post results
        if self.upload:
            self._post_results(ssh, success, log_location, commit_id)
        if success:
            tprint("SUCCESS: {}\nLOGS: {}".format(patchset, log_location))
        elif not success:
            tprint("FAIL: {}\nLOGS: {}".format(patchset, log_location))

    def _upload_logs(self,
                     host_ssh,
                     patch_ref_name,
                     internal=False,
                     post_failed=False):
        sftp = host_ssh.ssh.open_sftp()
        sftp.put(UPLOAD_FILE, "/tmp/upload_logs.sh")
        host_ssh.exec_command("chmod +x /tmp/upload_logs.sh")
        host_ssh.exec_command(
            "sudo /tmp/upload_logs.sh {}".format(patch_ref_name))
        filename = "{}{}".format(patch_ref_name, ".tar.gz")
        sftp = host_ssh.ssh.open_sftp()
        tempfilename = "/tmp/{}".format(filename)
        tempfiledirectory = tempfilename.replace(".tar.gz", "")
        dprint("SFTP-ing logs locally, remote name: {} local name: {}".format(
            filename, tempfilename))
        sftp.get(filename, tempfilename)
        cmd = "tar -zxvf {} -C {} -m".format(
                tempfilename, '/tmp/')
        dprint("Running: ", cmd)
        exe(cmd)
        create_indexes(tempfiledirectory, patch_ref_name)
        os.chdir(tempfiledirectory)
        with io.open('console.out.log') as f:
            result_data = f.read()
        match = re.search(
            r"^cinder_commit_id\s(?P<commit_id>.*)$", result_data, re.M)
        commit_id = match.group('commit_id')
        dprint("Commit ID: ", commit_id)
        try:
            success = exe("grep 'Failed: 0' console.out.log")
            success = True
            dprint("Found SUCCESS")
        except subprocess.CalledProcessError:
            success = False
            dprint("Found FAILURE")
        os.chdir('..')
        self._boto_up_data(patch_ref_name)
        log_location = "".join((BASE_URL, patch_ref_name, "/index.html"))
        return success, log_location, commit_id

    def _boto_up_data(self, data):
        dprint("Uploading Data:", data)

        bucket = self._get_boto_bucket()
        key = boto.s3.key.Key(bucket)
        key.key = data
        key.set_contents_from_string("")
        key.set_acl("public-read")

        for subdir, dirs, files, in os.walk(data):
            for file in files:
                path = os.path.join(subdir, file)
                print(path)
                subkey = boto.s3.key.Key(bucket)
                subkey.key = path
                subkey.set_contents_from_filename(path)
                subkey.set_acl("public-read")

    def _get_boto_bucket(self):
        access_key = self.aws_key_id
        secret_key = self.aws_secret_key
        bucket_name = self.remote_results_bucket

        conn = boto.s3.connect_to_region(
            'us-west-2',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            calling_format=OrdinaryCallingFormat())
        bucket = conn.get_bucket(bucket_name)
        return bucket

    def purge_old_keys(self):
        bucket = self._get_boto_bucket()
        size = 0
        n = 0
        utc = arrow.utcnow()
        old = utc.shift(months=-4)
        for key in bucket.list(prefix='refs-'):
            if arrow.get(key.last_modified) < old:
                n += 1
                size += key.size
                tprint("Deleting key: ", key.name)
                key.delete()
        tprint("Deleted {} keys with a total size of {}".format(n, size))


def watcher(key, user):

    def _helper():
        cmd = "ssh -i {} -p 29418 {}@review.openstack.org " \
               "\"gerrit stream-events\"".format(key, user)
        dprint("Cmd:", cmd)
        process = subprocess.Popen(
            shlex.split(cmd), stdout=subprocess.PIPE)
        for line in iter(process.stdout.readline, b''):
            event = json.loads(line)
            if event.get('type') == 'comment-added':
                comment = event['comment']
                author = event['author'].get('username')
                project = event['change']['project']
                patchSet = event['patchSet']['ref']
                if ALL_EVENTS:
                    print("project: " + project,
                          "author: " + author,
                          "patchSet: " + patchSet,
                          "comment: " + comment[:25].replace(
                              "\n", " "), sep="|")
                if 'Verified+2' in comment or 'Verified+1' in comment:
                    dprint("project: " + project,
                           "author: " + author,
                           "patchSet: " + patchSet,
                           "comment: " + comment[:25].replace(
                               "\n", " "), sep="|")
                if 'run-Datera' in comment:
                    tprint("Found manual patchset: ", patchSet)
                    PATCH_QUEUE.put(patchSet)
                if (author and author.lower() == "zuul" and
                        project == "openstack/cinder"):
                    if 'Verified+2' in comment or 'Verified+1' in comment:
                        tprint("###############")
                        tprint("Found patchset: ", patchSet)
                        tprint("###############")
                        PATCH_QUEUE.put(patchSet)

    wt = threading.Thread(target=_helper)
    wt.daemon = True
    print("Starting watcher")
    wt.start()


def runner(conf, third_party, upload):

    def _helper():
        count = 0
        while True:
            patchref = None
            try:
                patchref = PATCH_QUEUE.get(block=False)
            except Empty:
                time.sleep(0.5)
                if os.path.exists(EXIT):
                    print("Cleaning up")
                    os.unlink(EXIT)
                    break
                continue
            count += 1
            # Every 10 tests we'll reimage the box
            if count % 10 == 0:
                reimage_datera()
            dprint("Starting CI on: {}".format(patchref))
            try:
                third_party.run_ci_on_patch(
                        # Read these from environment variables
                        conf['node_ip'],
                        conf['node_user'],
                        conf['node_password'],
                        conf['cluster_ip'],
                        patchref,
                        conf['cinder_driver_version'],
                        conf['glance_driver_version'],
                        node_keyfile=conf['keyfile'])
            except Exception:
                print("Exception occurred during CI run:")
                traceback.print_exc()
                raise
            PATCH_QUEUE.task_done()
            print("Finished CI on: {}".format(patchref))

    rt = threading.Thread(target=_helper)
    print("Starting runner")
    rt.start()


def reimage_datera(cluster="tlx222s", train="3.0.PROD", build="3.1.5"):
    # Runs against 3.1.0 for now
    exe("curl -O http://releases.daterainc.com/{}/{}/"
        "pxeboot-from-build.sh".format(train, build))
    exe("chmod +x pxeboot-from-build.sh")
    exe("./pxeboot-from-build.sh -c {} -v {} -b {}".format(
        cluster, train, build))


def parse_config_file(config_file):
    with io.open(config_file) as f:
        return yaml.safe_load(f)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    parser.add_argument('-u', '--upload', action='store_true')
    parser.add_argument('--upload-only', action='store_true')
    parser.add_argument('--use-existing-devstack', action='store_true')
    parser.add_argument('--single-run-patchset')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--show-all-events', action='store_true')
    args = parser.parse_args()

    if args.debug:
        global DEBUG
        DEBUG = True
        dprint("Running with DEBUG on")

    if args.show_all_events:
        global ALL_EVENTS
        ALL_EVENTS = True

    if not os.path.exists(DEVSTACK_FILE):
        print("Missing required devstack_up.py file in current directory")

    conf = parse_config_file(args.config)
    third_party = ThirdParty(
        conf['project'],
        conf['host'],
        conf['username'],
        conf['port'],
        conf['gerrit_key'],
        conf['ci_name'],
        conf['aws_key_id'],
        conf['aws_secret_key'],
        conf['remote_results_bucket'],
        upload=args.upload,
        use_existing_devstack=args.use_existing_devstack)

    if args.single_run_patchset:
        third_party.run_ci_on_patch(conf['node_ip'],
                                    conf['node_user'],
                                    conf['node_password'],
                                    conf['cluster_ip'],
                                    args.single_run_patchset,
                                    conf['cinder_driver_version'],
                                    conf['glance_driver_version'],
                                    node_keyfile=conf['keyfile'])
        return SUCCESS
    elif args.upload_only:
        ssh = SSH(conf['node_ip'], conf['node_user'], conf['node_password'],
                  keyfile=conf['keyfile'])
        head = ssh.exec_command('cd /opt/stack/cinder && git rev-parse HEAD')
        patchset = third_party.gerrit.query(
            '{head} --patch-sets --format json'.format(head=head)
            )['patchSets'][-1]['ref']
        patchset = patchset.replace("/", "-")
        success, log_location, commit_id = third_party._upload_logs(
            ssh, patchset, post_failed=False)
        third_party._post_results(ssh, success, log_location, commit_id)
        return SUCCESS
    else:
        if os.path.exists(EXIT):
            os.unlink(EXIT)
        watcher(conf['gerrit_key'], conf['username'])
        runner(conf, third_party, args.upload)

    while True:
        try:
            while True:
                time.sleep(0.2)
        except KeyboardInterrupt:
            if input("Do you really want to quit? [Y/n]: ") in {"Y", "y"}:
                io.open(EXIT, 'w+').close()
                break

    return SUCCESS


if __name__ == '__main__':
    sys.exit(main())
