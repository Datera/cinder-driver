import argparse
import io
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from queue import Empty, Queue

import arrow
import boto
import coloredlogs
import ruamel.yaml as yaml
from boto.s3.connection import OrdinaryCallingFormat
from jinja2 import Template
from plumbum import SshMachine, local
from plumbum.cmd import ssh  # pylint: disable=import-error
from plumbum.cmd import chmod, curl, rm, ssh_keygen, tar
from plumbum.commands.processes import ProcessExecutionError

LOGGER = logging.getLogger("third_party_ci")
dprint = LOGGER.debug
iprint = LOGGER.info
eprint = LOGGER.error

COND = {"QUIT": False, "ALL_EVENTS": False}

PATCH_QUEUE = Queue()

UPLOAD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload_logs.sh")

DEVSTACK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "devstack_up.py"
)

SUCCESS = 0
FAIL = 255
UNSTABLE = 254
BASE_URL = "http://stkci.daterainc.com.s3-website-us-west-2.amazonaws.com/"
LISTING_TEMPLATE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "listing.j2"
)


def create_indexes(rootdir, ref_name):
    template = Template(open(LISTING_TEMPLATE).read())
    previous_subdir = None
    stripped_subdir = None
    back_dir = None
    for subdir, dirs, file_names in os.walk(rootdir):
        files = []

        if previous_subdir != subdir:
            stripped_subdir = subdir.replace(rootdir, "")

            back_dir = re.sub(r"\/\w+$", "", stripped_subdir)

        for directory in dirs:
            full_path = os.path.join(stripped_subdir, directory)
            f = {
                "name": directory,
                "type": "folder",
                "relpath": full_path.strip("/"),
                "topkey": ref_name,
                "dir": True,
            }
            files.append(f)

        for file_name in file_names:
            full_path = os.path.join(stripped_subdir, file_name)

            f = {
                "name": file_name,
                "type": "text",
                "topkey": ref_name,
                "relpath": full_path.strip("/"),
            }
            files.append(f)

        previous_subdir = subdir

        output = template.render(
            {
                "ref_name": ref_name,
                "back": back_dir.strip("/"),
                "files": files,
                "base_url": BASE_URL,
            }
        )

        index_file_path = os.path.join(subdir, "index.html")

        with open(index_file_path, "w") as fd:
            fd.write(output)


class ThirdParty:
    def __init__(
        self,
        project,
        ghost,
        guser,
        gport,
        gkeyfile,
        ci_name,
        aws_key_id,
        aws_secret_key,
        remote_bucket,
        upload=False,
        use_existing_devstack=False,
        nocleanup=False,
    ):
        self.project = project
        self.ci_name = ci_name
        self.upload = upload
        self.aws_key_id = aws_key_id
        self.aws_secret_key = aws_secret_key
        self.remote_results_bucket = remote_bucket
        self.use_existing_devstack = use_existing_devstack
        self.nocleanup = nocleanup

        self.ssh_gerrit = ssh[
            "-i", gkeyfile, "-p", gport, f"{guser}@{ghost}",
        ]

    def run_ci_on_patch(
        self,
        node_ip,
        username,
        password,
        cluster_ip,
        patchset,
        cinder_driver_version,
        glance_driver_version,
    ):
        """
        This will actually run the CI logic.  If post_failed is set to `False`,
        it will try again if it detects failure in the results.  This should
        hopefully decrease our false failure rate.
        """
        dprint("Running against: %s", patchset)

        # Remove any offending keys
        cmd = ssh_keygen["-R", node_ip]
        dprint(cmd)
        cmd()

        try:
            # Run tests on devstack
            local.python[
                DEVSTACK_FILE,
                cluster_ip,
                node_ip,
                username,
                password,
                "--patchset",
                patchset,
                "--only-update-drivers",
                "--glance-driver-version",
                "none",
                "--skip-tox",
                "--reimage-client" if not self.use_existing_devstack else None,
            ]()
        except ProcessExecutionError as error:
            eprint(
                "Devstack_up ran into an error. Still trying to figure out the"
                "results, but this should be fixed"
            )
            eprint(error.stdout)
            return UNSTABLE

        commit_id, success, log_location = self._upload_logs(
            node_ip, username, password, patchset
        )
        if self.upload:
            self._post_results(commit_id, success, log_location)
        if success:
            return SUCCESS
        else:
            return FAIL

    def _upload_logs(self, node_ip, username, password, patchset):
        patch_ref_name = os.environ.get(
            "GERRIT_PATCHSET_REVISION", patchset.replace("/", "-")
        )

        # Collect logs
        filename = f"{patch_ref_name}.tar.gz"
        tempfilename = f"/tmp/{filename}"
        tempfiledirectory = tempfilename.replace(".tar.gz", "")

        # Remove any offending keys
        cmd = ssh_keygen["-R", node_ip]
        dprint(cmd)
        cmd()

        dprint("SSHing: %s, %s, %s", node_ip, username, password)
        with SshMachine(node_ip, user=username, password=password) as devstack:
            devstack.upload(UPLOAD_FILE, "/tmp/upload_logs.sh")
            devstack["chmod"]("+x", "/tmp/upload_logs.sh")
            devstack["sudo"]("/tmp/upload_logs.sh", patch_ref_name)
            devstack.download(filename, tempfilename)

        # Analyze
        cmd = tar["-zxvf", tempfilename, "-C", "/tmp/"]
        dprint("Running: %s", cmd)
        cmd()

        create_indexes(tempfiledirectory, patch_ref_name)

        with io.open(f"{tempfiledirectory}/console.out.log") as f:
            logs = f.read()
            # Find the commit id
            match = re.search(r"^cinder_commit_id\s(?P<commit_id>.*)$", logs, re.M)
            if match:
                commit_id = match.group("commit_id")
                dprint("Commit ID: %s", commit_id)
            # Find failures
            success = re.search(r"Failed: 0", logs)
            if success:
                dprint("Success:")
            else:
                dprint("Failure:")

        # Upload logs
        self._boto_up_data(patch_ref_name)

        # Cleanup artifacts
        if not self.nocleanup:
            rm["-rf", tempfiledirectory]()

        log_location = "".join((BASE_URL, patch_ref_name, "/index.html"))
        return commit_id, success, log_location

    def _post_results(self, commit_id, success, log_location):
        dprint("Logs: %s", log_location)

        # Post results
        if success:
            msg = f"* {self.ci_name} {log_location} : SUCCESS "
            iprint("Gerrit results: %s", msg)
        else:
            msg = (
                f"* {self.ci_name} {log_location} : FAILURE \n"
                + f"You can rerun this CI by commenting run-Datera"
            )
            eprint("Gerrit results: %s", msg)
        cmd = self.ssh_gerrit[f"gerrit review -m '{msg}' {commit_id}"]
        dprint(" -- command: %s", cmd)
        cmd()

    def _boto_up_data(self, data):
        dprint("Uploading Data: %s", data)

        bucket = self._get_boto_bucket()
        key = boto.s3.key.Key(bucket)
        key.key = data
        key.set_contents_from_string("")
        key.set_acl("public-read")

        curdir = os.path.abspath(os.curdir)
        os.chdir("/tmp/")
        try:
            for subdir, _, files, in os.walk(data):
                for file in files:
                    path = os.path.join(subdir, file)
                    dprint("-- path: %s", path)
                    subkey = boto.s3.key.Key(bucket)
                    subkey.key = path
                    subkey.set_contents_from_filename(path)
                    subkey.set_acl("public-read")
        finally:
            os.chdir(curdir)

    def _get_boto_bucket(self):
        access_key = self.aws_key_id
        secret_key = self.aws_secret_key
        bucket_name = self.remote_results_bucket

        dprint("aws conf: %s, %s, %s", access_key, secret_key, bucket_name)

        conn = boto.s3.connect_to_region(
            "us-west-2",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            calling_format=OrdinaryCallingFormat(),
        )
        dprint("conn: %s", conn)
        bucket = conn.get_bucket(bucket_name)
        return bucket

    def purge_old_keys(self):
        bucket = self._get_boto_bucket()
        size = 0
        n = 0
        utc = arrow.utcnow()
        old = utc.shift(months=-4)
        for key in bucket.list(prefix="refs-"):
            if arrow.get(key.last_modified) < old:
                n += 1
                size += key.size
                dprint("Deleting key: %s", key.name)
                key.delete()
        dprint("Deleted %s keys with a total size of %s", n, size)


def watcher(third_party):
    def _helper():
        dprint("Cmd: %s", third_party.ssh_gerrit["gerrit stream-events"])
        for line in iter(
            third_party.ssh_gerrit["gerrit stream-events"].popen().stdout.readline, b""
        ):
            event = json.loads(line)
            if event["type"] == "comment-added":
                try:
                    comment = event["comment"]
                    author = event["author"]["username"]
                    project = event["change"]["project"]
                    patchSet = event["patchSet"]["ref"]
                    branch = event["change"]["branch"]
                except KeyError as err:
                    eprint("Error parsing event. Line: %s", line)
                    eprint(err)
                    continue
                if (
                    COND["ALL_EVENTS"]
                    or "Verified+2" in comment
                    or "Verified+1" in comment
                ):
                    iprint(
                        "project: %s | author: %s | patchSet: %s |"
                        "branch: %s | comment: %s",
                        project,
                        author,
                        patchSet,
                        branch,
                        comment[:25].replace("\n", " "),
                    )
                if "run-Datera" in comment:
                    iprint("Found manual patchset: %s", patchSet)
                    PATCH_QUEUE.put(patchSet)
                if (
                    author
                    and author.lower() == "zuul"
                    and project == "openstack/cinder"
                    and branch == "master"
                ):
                    if "Verified+2" in comment or "Verified+1" in comment:
                        iprint("Found patchset: %s", patchSet)
                        PATCH_QUEUE.put(patchSet)

    wt = threading.Thread(target=_helper, name="WatcherThread")
    wt.daemon = True
    iprint("Starting watcher")
    wt.start()


def runner(conf, third_party, upload):
    def _helper():
        # count = 0
        while True:
            patchref = None
            try:
                patchref = PATCH_QUEUE.get(block=False)
            except Empty:
                time.sleep(1.5)
                if COND["QUIT"]:
                    dprint("Cleaning up...")
                    break
                continue
            # count += 1
            # # Every 10 tests we'll reimage the box
            # if count % 10 == 0:
            #     reimage_datera()
            dprint("Starting CI on: %s", patchref)
            try:
                third_party.run_ci_on_patch(
                    # Read these from environment variables
                    conf["node_ip"],
                    conf["node_user"],
                    conf["node_password"],
                    conf["cluster_ip"],
                    patchref,
                    conf["cinder_driver_version"],
                    conf["glance_driver_version"],
                )
                iprint("Finished CI on: %s", patchref)
            except Exception:
                eprint("Exception occurred during CI run:")
                traceback.print_exc()
            PATCH_QUEUE.task_done()

    rt = threading.Thread(target=_helper, name="RunnerThread")
    iprint("Starting runner")
    rt.start()


def reimage_datera(cluster="tlx222s", train="3.0.PROD", build="3.1.5"):
    pxeboot = local["./pxeboot-from-build.sh"]

    curl["-O", f"http://releases.daterainc.com/{train}/{build}/pxeboot-from-build.sh"]()
    chmod["+x", "pxeboot-from-build.sh"]()
    pxeboot["-c", cluster, "-v", train, "-b", build]()


def parse_config_file(config_file):
    with io.open(config_file) as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("config")
    parser.add_argument("-u", "--upload", action="store_true")
    parser.add_argument("--upload-only", action="store_true")
    parser.add_argument("--use-existing-devstack", action="store_true")
    parser.add_argument("--single-run-patchset")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--show-all-events", action="store_true")
    parser.add_argument("--no-cleanup", action="store_true")
    args = parser.parse_args()

    if args.debug:
        dprint("Running with DEBUG on")

    coloredlogs.install(
        level="DEBUG" if args.debug else "INFO",
        logger=LOGGER,
        fmt="%(asctime)s %(hostname)s %(name)s[%(threadName)s] "
        + "%(levelname)s %(message)s",
    )

    if args.show_all_events:
        COND["ALL_EVENTS"] = True

    if not os.path.exists(DEVSTACK_FILE):
        eprint("Missing required devstack_up.py file in current directory")

    conf = parse_config_file(args.config)

    # Remove any offending keys
    cmd = ssh_keygen["-R", conf["node_ip"]]
    dprint(cmd)
    cmd()

    third_party = ThirdParty(
        conf["project"],
        conf["host"],
        conf["username"],
        conf["port"],
        conf["gerrit_key"],
        conf["ci_name"],
        conf["aws_key_id"],
        conf["aws_secret_key"],
        conf["remote_results_bucket"],
        upload=args.upload,
        use_existing_devstack=args.use_existing_devstack,
        nocleanup=args.no_cleanup,
    )

    if args.single_run_patchset:
        return third_party.run_ci_on_patch(
            conf["node_ip"],
            conf["node_user"],
            conf["node_password"],
            conf["cluster_ip"],
            args.single_run_patchset,
            conf["cinder_driver_version"],
            conf["glance_driver_version"],
        )

    if args.upload_only:
        dprint(
            "SSHing: %s, %s, %s",
            conf["node_ip"],
            conf["node_user"],
            conf["node_password"],
        )
        with SshMachine(
            conf["node_ip"], user=conf["node_user"], password=conf["node_password"]
        ) as devstack:
            with devstack.cwd("/opt/stack/cinder"):
                head = devstack["git"]("rev-parse", "HEAD")

        dprint("head: %s", head)
        patchsets_json = third_party.ssh_gerrit(
            f"gerrit query {head} --patch-sets --format json"
        )
        patchset_line = json.loads(patchsets_json.split("\n")[0])
        if not "patchSets" in patchset_line:
            eprint("Could not retrieve patchsets for %s", head)
            dprint("Line: %s", patchset_line)
            return FAIL

        patchset = patchset_line["patchSets"][-1]["ref"]
        dprint("patchSet: %s", patchset)
        commit_id, success, log_location = third_party._upload_logs(
            conf["node_ip"], conf["node_user"], conf["node_password"], patchset
        )
        if args.upload:
            third_party._post_results(commit_id, success, log_location)
        dprint(head)
        dprint(patchset)
        if success:
            return SUCCESS
        else:
            return FAIL

    watcher(third_party)
    runner(conf, third_party, args.upload)

    while True:
        try:
            while True:
                time.sleep(0.2)
        except KeyboardInterrupt:
            if input("Do you really want to quit? [Y/n]: ") in {"Y", "y"}:
                COND["QUIT"] = True
                break
            continue

    return SUCCESS


if __name__ == "__main__":
    sys.exit(main())
