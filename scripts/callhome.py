#!/usr/bin/env python
from __future__ import unicode_literals, division, print_function

import argparse
import errno
import io
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

import arrow
import six
import requests

from scaffold import readCinderConf

VERSION = '1.1'
VERSION_HISTORY = """
    1.0 - Initial Callhome Version
    1.1 - Added Journalctl support
"""
SUCCESS = 0
FAILURE = 1
LOGFILE_VAR = "CH_LOGFILES"
LOCAL_LOGDIR = "/var/log/datera"
ARCHIVE_FILENAME = ("datera-cinder-driver.{node}.{controller}.log.DEBUG"
                    ".{datetime}.UTC.tar.gz")
INTERVAL = 1 * 60 * 60


# def gen_filename(node, controller):
#     return ARCHIVE_FILENAME.format(
#         node=node,
#         controller=controller,
#         datetime=datetime.datetime.fromtimestamp(time.time()).strftime(
#             "%Y%m%d-%H%M%S%f"))
debug = True


def dprint(*args, **kwargs):
    if debug:
        print(*args, **kwargs)


def exe(cmd, stdout=None, shell=False):
    dprint(cmd)
    if not shell:
        cmd = shlex.split(cmd)
    if stdout is None:
        return subprocess.check_output(cmd, shell=shell)
    subprocess.check_call(cmd, stdout=stdout, shell=shell)


def mk_archive(directory, output_fn):
    with tarfile.open(output_fn, "w:gz") as f:
        f.add(directory, arcname=os.path.basename(directory))


def post_archive(fn):
    ip, user, password, cert, cert_key = readCinderConf()
    schema = 'http'
    port = 7717
    cert_data = None
    if cert and cert_key:
        schema = 'https'
        port = 7718
        cert_data = (cert, cert_key)
    # login
    resp = requests.put("{}://{}:{}/v2.1/login".format(
        schema, ip, port), data={"name": user, "password": password},
        cert=cert_data)
    key = resp.json()["key"]
    print(key)
    headers = {"Auth-Token": key,
               "Datera-Driver": "Cinder-Logging-".format(VERSION)}
    # Put file
    fname = os.path.basename(fn)
    files = {'file': (fname, io.open(fn, 'rb'))}
    print("Uploading File: ", fn)
    resp = requests.put("{}://{}:{}/v2.1/logs_upload".format(
        schema, ip, port), files=files, headers=headers,
        data={'ecosystem': 'openstack'})
    print("Response: ", resp.text)


def copy_filter_files(files, pts, ts, journalctl=False):
    # Create temp directory and archive filename
    tmpd = tempfile.mkdtemp()
    host = exe("hostname")
    tmpfn = "cinder-logs.{}.{}.{}.tar.gz".format(
        host, pts.timestamp, ts.timestamp)
    tmpdfn = os.path.join(tmpd, tmpfn)

    # Copy files to temp directory
    for file in files:
        fname = os.path.join(tmpd, os.path.basename(file))
        if journalctl:
            cmd = ("journalctl --utc --unit {} --since '{}' --until '{}' "
                   "--output short-iso > {}".format(
                    file,
                    pts.format(
                        "YYYY-MM-DD HH:MM:SS"),
                    ts.format(
                        "YYYY-MM-DD HH:MM:SS"),
                    fname))
            exe(cmd, shell=True)
        else:
            shutil.copyfile(file, fname)

    # Create filter filenames
    rfile = os.path.join(tmpd, "requests.json")
    afile = os.path.join(tmpd, "attach_detach.json")

    tmpfiles = " ".join((os.path.join(tmpd, os.path.basename(file))
                        for file in files))
    # Filter for requests
    if journalctl:
        jstring = "--journalctl"
    else:
        jstring = ""
    print("tmpfiles: ", tmpfiles)
    with io.open(rfile, "w+") as f:
        exe("./sreq.py {} --json --filter REQTIME@@{} "
            "--filter REQTIME**{} {}".format(
                tmpfiles, ts.timestamp, pts.timestamp, jstring), stdout=f)

    # Filter for attach_detach
    with io.open(afile, "w+") as f:
        exe("./sreq.py {} --json --attach-detach".format(tmpfiles), stdout=f)

    # Compress Files
    mk_archive(tmpd, tmpdfn)

    # Send archive to Datera backend
    post_archive(tmpdfn)

    shutil.copyfile(tmpdfn, os.path.join(LOCAL_LOGDIR, tmpfn))

    # More robust tmp dir removal
    try:
        shutil.rmtree(tmpd)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise


def main(args):
    if args.version:
        print("Cinder Call-Home Script:", VERSION)
        return SUCCESS

    # Timestamp for when we ended log collection
    timestamp = arrow.get(time.gmtime(time.time()))

    if args.logfiles:
        logfiles = args.logfiles
    elif os.getenv(LOGFILE_VAR):
        logfiles = os.getenv(LOGFILE_VAR).split(":")
    else:
        print("At least one logfile must be specified")
        return FAILURE

    if not os.path.isdir(LOCAL_LOGDIR):
        os.makedirs(LOCAL_LOGDIR)

    tsfile = os.path.join(LOCAL_LOGDIR, "last")
    while True:
        prev_timestamp = arrow.get(0)
        if os.path.isfile(tsfile):
            with io.open(tsfile) as f:
                prev_timestamp = arrow.get(f.read().strip())

        copy_filter_files(logfiles, prev_timestamp, timestamp, args.journalctl)

        # Save timestamp to file so we know where to start gathering logs again
        with io.open(tsfile, 'w') as f:
            f.write(six.u(str(timestamp.timestamp)))

        # Default collect every hour
        time.sleep(INTERVAL)
    return SUCCESS

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('logfiles', nargs='*')
    parser.add_argument('-j', '--journalctl', action='store_true',
                        help='If present, logfiles argument (or {} environment'
                             ' variable value) will be interpreted as a '
                             '"journalctl" unit'.format(LOGFILE_VAR))
    parser.add_argument('-v', '--version', action='store_true',
                        help='Show callhome script version')
    args = parser.parse_args()
    sys.exit(main(args))
