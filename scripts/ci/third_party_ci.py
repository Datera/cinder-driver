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
import ruamel.yaml as yaml
import simplejson as json
from six.moves.queue import Queue

from ci import devstack_up

PATCH_QUEUE = Queue()

UPLOAD_FILE = os.path.join(
    os.path.dirname(
        os.path.abspath(__file__)),
    'upload_logs.sh')

SUCCESS = 0
FAIL = 1
DEBUG = False
BASE_URL = 'http://stkci.daterainc.com.s3-website-us-west-2.amazonaws.com/'
LISTING_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__name__)),
                                'listing.j2')


def dprint(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)


def tprint(*args, **kwargs):
    t = time.time()
    pt = time.strftime("%a, %d %b %Y %H:%M:%S %Z", time.localtime(t))
    tid = threading.currentThread().ident
    print(tid, pt, *args, **kwargs)


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


class ThirdParty(object):

    def __init__(self, project, ghost, guser, gport, gkeyfile, ci_name,
                 aws_key_id, aws_secret_key, remote_bucket, upload=False):
        self.project = project
        self.gerrit = Gerrit(ghost, guser, port=gport, keyfile=gkeyfile)
        self.gerrit.ci_name = ci_name
        self.upload = upload
        self.aws_key_id = aws_key_id
        self.aws_secret_key = aws_secret_key
        self.remote_results_bucket = remote_bucket

    def _post_results(self, success, log_location, commit_id):
        base_cmd = "gerrit review -m {}"
        if success:
            cmd = base_cmd.format(
                "\"* {} {} : SUCCESS \" {}".format(
                    self.gerrit.ci_name,
                    log_location,
                    commit_id))
        else:
            cmd = base_cmd.format(
                "\"* {} {} : FAILURE \" {}".format(
                    self.gerrit.ci_name,
                    log_location,
                    commit_id))
        dprint("Posting gerrit results:\n{}".format(cmd))
        self.gerrit._ssh(cmd)

    def run_ci_on_patch(self,
                        node_ip,
                        username,
                        password,
                        cluster_ip,
                        patchset,
                        devstack_version,
                        cinder_driver_version,
                        glance_driver_version):
        """
        This will actually run the CI logic.  If post_failed is set to `False`,
        it will try again if it detects failure in the results.  This should
        hopefully decrease our false failure rate.
        """
        dprint("Running against: ", patchset)
        patch_ref_name = patchset.replace("/", "-")
        # Setup Devstack and run tempest
        devstack_up.main(
            node_ip,
            username,
            password,
            cluster_ip,
            '',
            patchset,
            devstack_version,
            cinder_driver_version,
            glance_driver_version,
            False)

        # Upload logs
        success, log_location, commit_id = self._upload_logs(
            node_ip, patch_ref_name, post_failed=False)
        # Post results
        if self.upload:
            self._post_results(success, log_location, commit_id)
        if success:
            tprint("SUCCESS: {}\nLOGS: {}".format(patchset, log_location))
        elif not success:
            tprint("FAIL: {}\nLOGS: {}".format(patchset, log_location))

    def _upload_logs(self,
                     host,
                     patch_ref_name,
                     internal=False,
                     post_failed=False):
        sftp = host._ssh.open_sftp()
        sftp.put(UPLOAD_FILE, "/tmp/upload_logs.sh")
        host.exec_command("chmod +x /tmp/upload_logs.sh")
        host.exec_command(
            "sudo /tmp/upload_logs.sh {}".format(patch_ref_name))
        filename = "{}{}".format(patch_ref_name, ".tar.gz")
        sftp = host._ssh.open_sftp()
        tempfilename = "/tmp/{}".format(filename)
        tempfiledirectory = tempfilename.replace(".tar.gz", "")
        dprint("SFTP-ing logs locally, remote name: {} local name: {}".format(
            filename, tempfilename))
        sftp.get(filename, tempfilename)
        cmd = "tar -zxvf {} -C {} --warning=no-timestamp".format(
                tempfilename, '/tmp/')
        dprint("Running: ", cmd)
        dprint(subprocess.check_output(shlex.split(cmd)))
        create_indexes(tempfiledirectory, patch_ref_name)
        os.chdir(tempfiledirectory)
        match = re.match(
            r"^cinder_commit_id\s(?P<commit_id>.*)$",
            io.open('console.log.out').read(),
            re.M)
        commit_id = match.group('commit_id')
        dprint("Commit ID: ", commit_id)
        try:
            success = subprocess.check_output(
                shlex.split("grep 'Failed: 0' console.log.out"))
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


def watcher(key):

    def _helper():
        process = subprocess.Popen(shlex.split(
            "ssh -i {} -p 29418 datera-ci@review.openstack.org "
            "\"gerrit stream-events\"".format(key)),
            stdout=subprocess.PIPE)
        for line in iter(process.stdout.readline, b''):
            event = json.loads(line)
            if event.get('type') == 'comment-added':
                comment = event['comment']
                author = event['author'].get('username')
                project = event['change']['project']
                patchSet = event['patchSet']['ref']
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
    wt.start()


def runner(conf, upload):

    third_party = ThirdParty(
        conf['project'],
        conf['host'],
        conf['user'],
        conf['port'],
        conf['ssh_key'],
        conf['ci_name'],
        conf['aws_key_id'],
        conf['aws_secret_key'],
        conf['remote_results_bucket'],
        upload=False)

    def _helper():
        count = 0
        while True:
            patchref = PATCH_QUEUE.get()
            count += 1
            # Every 10 tests we'll reimage the box
            if count % 10 == 0:
                reimage_datera()
            dprint("Starting CI on: {}".format(patchref))
            try:
                third_party.run_ci_on_patch(
                        # Read these from environment variables
                        conf['node_ip'],
                        conf['node_password'],
                        conf['cluster_ip'],
                        conf['patchset'],
                        conf['devstack_version'],
                        conf['cinder_driver_version'],
                        conf['glance_driver_version'])
            except Exception:
                print("Exception occurred during CI run:")
                traceback.print_exc()
                raise
            PATCH_QUEUE.task_done()
            print("Finished CI on: {}".format(patchref))

    rt = threading.Thread(target=_helper)
    rt.start()


def reimage_datera(cluster="tlx222s", train="3.0.PROD", build="3.1.5"):
    # Runs against 3.1.0 for now
    subprocess.check_call(shlex.split(
        "curl -O http://releases.daterainc.com/{}/{}/"
        "pxeboot-from-build.sh".format(train, build)))
    subprocess.check_call(shlex.split("chmod +x pxeboot-from-build.sh"))
    subprocess.check_call(shlex.split(
        "./pxeboot-from-build.sh -c {} -v {} -b {}".format(
            cluster, train, build)))


def parse_config_file(config_file):
    with io.open(config_file) as f:
        return yaml.safe_load(f)


def main():

    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    parser.add_argument('-u', '--upload', action='store_true')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    if args.debug:
        global debug
        DEBUG = True
        dprint("Running with DEBUG set to:", DEBUG)

    conf = parse_config_file(args.config)

    watcher(conf['ssh_key'])
    runner(conf, args.upload)

    return SUCCESS


if __name__ == '__main__':
    sys.exit(main())
