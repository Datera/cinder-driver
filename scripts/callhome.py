#!/usr/bin/env python
from __future__ import unicode_literals, division, print_function

import argparse
import datetime
import os
import random
import time
import subprocess
import sys

import paramiko

VERSION = '1.0'
SUCCESS = 0
FAILURE = 1
PASS_VAR = "CH_ROOT_PASS"
KEY_VAR = "CH_ROOT_KEY"
LOCAL_LOGDIR = "/var/log/datera"
ARCHIVE_FILENAME = ("datera-cinder-driver.{node}.{controller}.log.DEBUG"
                    ".{datetime}.UTC.{random}.tar.gz")
SSH_TIMEOUT = 60
USAGE = """

    $ export {pv}='username:passwd:ip'
    or
    $ export {kv}='username:keyfile:ip'
    then
    $ callhome.py logfile
""".format(pv=PASS_VAR, kv=KEY_VAR)


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


def copy_files(ssh, files):
    there_hostname = exec_command(ssh, "uname -a").split()[1]
    here_hostname = subprocess.check_output(["uname", "-a"]).split()[1]
    # TODO make compatible with Datera logs
    fn = ARCHIVE_FILENAME.format(
        node=here_hostname,
        controller=there_hostname,
        datetime=datetime.datetime.fromtimestamp(time.time()).strftime(
            "%Y%m%d-%H%M%S%f"),
        random=str(random.randint(1000, 9999)))
    # We use -h to dereference symbolic links
    exec_command(ssh, "tar -hczf {} {}".format(fn, " ".join(files)))
    sftp = ssh.open_sftp()
    sftp.get(fn, '{}/{}'.format(LOCAL_LOGDIR, fn))


def main(args):
    if args.version:
        print("Cinder Call-Home Script:", VERSION)
        return SUCCESS

    if not args.logfiles:
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

    copy_files(ssh, args.logfiles)
    return SUCCESS

if __name__ == "__main__":
    parser = argparse.ArgumentParser(USAGE)
    parser.add_argument('logfiles', nargs='*')
    parser.add_argument('-v', '--version', action='store_true',
                        help='Show callhome script version')
    args = parser.parse_args()
    sys.exit(main(args))
