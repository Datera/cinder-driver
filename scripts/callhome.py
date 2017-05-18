#!/usr/bin/env python
from __future__ import unicode_literals, division, print_function

import argparse
import datetime
import os
import random
import tempfile
import time
import shlex
import shutil
import subprocess
import sys

import paramiko

VERSION = '1.0'
SUCCESS = 0
FAILURE = 1
PASS_VAR = "CH_ROOT_PASS"
KEY_VAR = "CH_ROOT_KEY"
LOGFILE_VAR = "CH_LOGFILES"
LOCAL_LOGDIR = "/var/log/datera"
ARCHIVE_FILENAME = ("datera-cinder-driver.{node}.{controller}.log.DEBUG"
                    ".{datetime}.UTC.{random}.tar.gz")
SSH_TIMEOUT = 60
USAGE = """

    $ export {pv}='username:passwd:ip'
    or
    $ export {kv}='username:keyfile:ip'
    then
    $ export {lv}='logfile1:logfile2:logfile3'
    $ callhome.py logfile
""".format(pv=PASS_VAR, kv=KEY_VAR, lv=LOGFILE_VAR)


def exec_command(ssh, command, fail_ok=False):
    s = ssh
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
    return result


def gen_filename(node, controller):
    return ARCHIVE_FILENAME.format(
        node=node,
        controller=controller,
        datetime=datetime.datetime.fromtimestamp(time.time()).strftime(
            "%Y%m%d-%H%M%S%f"),
        random=str(random.randint(1000, 9999)))


def copy_filter_files(ssh, files):
    there_hostname = exec_command(ssh, "uname -a").split()[1]
    here_hostname = subprocess.check_output(["uname", "-a"]).split()[1]
    tmpd = tempfile.mkdtemp()
    tmpfn = "cinder-logs.{}.tar.gz".format(str(random.randint(1, 99999)))
    tmpdfn = "{}/{}".format(tmpd, tmpfn)
    # We use -h to dereference symbolic links

    # Copy Archive to /tmp
    exec_command(ssh, "tar -hczf {} {}".format(tmpfn, " ".join(files)))
    sftp = ssh.open_sftp()
    sftp.get(tmpfn, tmpdfn)

    # Extract
    subprocess.check_call(
        shlex.split("tar -zxf {} -C {}".format(tmpdfn, tmpd)))
    os.remove(tmpdfn)

    # Filter
    tmpfiles = " ".join(("{}/{}".format(tmpd, file) for file in files))
    rfile = "{}/requests.json".format(tmpd)
    afile = "{}/attach_detach.json".format(tmpd)
    with open(rfile, "w+") as f:
        subprocess.check_call(
            shlex.split("./sreq.py {} --json".format(tmpfiles)), stdout=f)
    with open(afile, "w+") as f:
        subprocess.check_call(
            shlex.split(
                "./sreq.py {} --json --attach-detach".format(tmpfiles)),
            stdout=f)

    # Re-Archive
    fn = gen_filename(here_hostname, there_hostname)
    subprocess.check_call(
        shlex.split(
            "tar -czf {}/{} -C {} requests.json attach_detach.json".format(
                tmpd, fn, tmpd)))
    # Copy to location
    shutil.move("{}/{}".format(tmpd, fn), "{}/{}".format(LOCAL_LOGDIR, fn))


def main(args):
    if args.version:
        print("Cinder Call-Home Script:", VERSION)
        return SUCCESS

    if args.logfiles:
        logfiles = args.logfiles
    elif os.getenv(LOGFILE_VAR):
        logfiles = os.getenv(LOGFILE_VAR).split(":")
    else:
        print("At least one logfile must be specified")
        return FAILURE

    username, password, ip = None, None, None

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(
        paramiko.AutoAddPolicy())
    if os.getenv(KEY_VAR):
        try:
            username, keyfile, ip = os.getenv(KEY_VAR).split(":")
        except ValueError:
            print("Please set {kv} environment variable with\n"
                  "root credentials for Datera EDF box\n"
                  "{kv}='username:passwd:ip'".format(kv=KEY_VAR))
            return FAILURE
        ssh.connect(
            hostname=ip,
            username=username,
            banner_timeout=SSH_TIMEOUT,
            key_filename=keyfile)
    elif os.getenv(PASS_VAR):
        try:
            username, password, ip = os.getenv(PASS_VAR).split(":")
        except ValueError:
            print("Please set {pv} environment variable with\n"
                  "root credentials for Datera EDF box\n"
                  "{pv}='username:passwd:ip'".format(pv=PASS_VAR))
            return FAILURE
        # Normal username/password usage
        ssh.connect(
            hostname=ip,
            username=username,
            password=password,
            banner_timeout=SSH_TIMEOUT)
    else:
        print("Neither {pv} nor {kv} are set.\n"
              "Please set one or the other".format(pv=PASS_VAR, kv=KEY_VAR))
        return FAILURE

    copy_filter_files(ssh, logfiles)
    return SUCCESS

if __name__ == "__main__":
    parser = argparse.ArgumentParser(USAGE)
    parser.add_argument('logfiles', nargs='*')
    parser.add_argument('-v', '--version', action='store_true',
                        help='Show callhome script version')
    args = parser.parse_args()
    sys.exit(main(args))
