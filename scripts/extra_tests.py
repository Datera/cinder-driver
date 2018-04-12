#!/usr/bin/env python
from __future__ import unicode_literals, division, print_function

from scaffold import read_cinder_conf, getAPI

import argparse
import functools
import random
import re
import shlex
import socket
import subprocess
import sys
import time
import uuid

from dfs_sdk.exceptions import ApiNotFoundError

VERBOSE = False

O_VOLID_RE = re.compile("\| id.*\| (.*) \|")
O_STATUS_RE = re.compile("\| status.*\| (.*) \|")

WORDS = ["koala", "panda", "teddy", "brown", "grizzly", "polar", "cinnamon",
         "atlas", "blue", "gobi", "sloth", "sun", "ursid", "kodiak", "gummy",
         "asian-black", "bergmans", "formosan", "pakistan-black", "ussuri",
         "mexican-grizzly", "syrian-brown", "east-siberian", "marsican",
         "spectacled", "kermode", "spirit", "glacier"]

_TESTS = []


def testcase(func):
    @functools.wraps(func)
    def _wrapper(*args, **kwargs):
        try:
            print("Running:", func.__name__)
            func(*args, **kwargs)
            print("SUCCESS", func.__name__)
        except Exception as e:
            print("FAILED: ", func.__name__, e)

    _TESTS.append(_wrapper)
    return _wrapper


def vprint(*args, **kwargs):
    if VERBOSE:
        print(*args, **kwargs)


def tname(s):
    return "-".join((s, str(uuid.uuid4())[:4]))


def rname():
    return "-".join((random.choice(WORDS), random.choice(WORDS), "bear"))


def exe(cmd, stdout=None, shell=False):
    vprint(cmd)
    if not shell:
        cmd = shlex.split(cmd)
    if stdout is None:
        return subprocess.check_output(cmd, shell=shell)
    subprocess.check_call(cmd, stdout=stdout, shell=shell)


def objid_from_output(output):
    vprint("Getting volid from output:")
    vprint(output)
    match = O_VOLID_RE.search(output)
    if not match:
        return
    return match.group(1)


def status_from_output(output):
    vprint("Getting status from output:")
    vprint(output)
    match = O_STATUS_RE.search(output)
    if not match:
        return
    return match.group(1)


def create_unmanaged_vol(api, name, cm=False):
    si_name = rname()
    vol_name = rname()
    if cm:
        ai = api.app_instances.create(name=name, create_mode="openstack")
    else:
        ai = api.app_instances.create(name=name)
    si = ai.storage_instances.create(name=si_name)
    si.volumes.create(name=vol_name, replica_count=1, size=5)
    return si_name, vol_name


def poll_available(obj, oid):
    timeout = 5
    while timeout:
        time.sleep(1)
        result = exe("openstack {} show {}".format(obj, oid))
        vprint(result)
        status = status_from_output(result)
        vprint("Status:", status)
        if status.strip() == "available":
            break
        timeout -= 1
    if not timeout:
        raise ValueError("{} {} was not available before timeout was "
                         "reached".format(obj, oid))


@testcase
def test_creation(api):
    name = tname("test-create")
    result = exe("openstack volume create {} --size 5".format(name))
    volid = objid_from_output(result)
    time.sleep(2)
    try:
        api.app_instances.get("OS-{}".format(volid))
    except ApiNotFoundError as e:
        print(e)
        print("Failed to create volume {}".format(name))
        return
    time.sleep(2)
    print(exe("openstack volume delete {}".format(name)))


@testcase
def test_manage_style_1(api):
    name = tname("test-manage-style-1")
    si_name, vol_name = create_unmanaged_vol(api, name)
    hostname = socket.gethostname()
    result = exe("cinder manage {host}@datera "
                 "{app}:{store}:{vol} --name {name}".format(
                     host=hostname, app=name, store=si_name, vol=vol_name,
                     name=name))
    volid = objid_from_output(result)
    poll_available("volume", volid)
    exe("openstack volume delete {}".format(volid))


@testcase
def test_manage_style_2(api):
    name = tname("test-manage-style-2")
    si_name, vol_name = create_unmanaged_vol(api, name)
    hostname = socket.gethostname()
    result = exe("cinder manage {host}@datera "
                 "root:{app}:{store}:{vol} --name {name}".format(
                     host=hostname, app=name, store=si_name, vol=vol_name,
                     name=name))
    volid = objid_from_output(result)
    poll_available("volume", volid)
    exe("openstack volume delete {}".format(volid))


@testcase
def test_manange_then_clone(api):
    name = tname("test-manage-then-clone")
    si_name, vol_name = create_unmanaged_vol(api, name)
    hostname = socket.gethostname()
    result = exe("cinder manage {host}@datera "
                 "{app}:{store}:{vol} --name {name}".format(
                     host=hostname, app=name, store=si_name, vol=vol_name,
                     name=name))
    volid = objid_from_output(result)
    poll_available("volume", volid)
    clone_name = rname()
    try:
        result = exe("openstack volume create {} --source {} --size 5".format(
            clone_name, volid))
        cloneid = objid_from_output(result)
        poll_available("volume", cloneid)
    except Exception:
        failed = True
    exe("openstack volume delete {}".format(cloneid))
    exe("openstack volume delete {}".format(volid))
    if failed:
        raise ValueError("Failed clone command, likely create_mode")


@testcase
def test_manange_then_clone_cm(api):
    """ Setting create_mode so clone can actually go through"""
    name = tname("test-manage-then-clone")
    si_name, vol_name = create_unmanaged_vol(api, name, cm=True)
    hostname = socket.gethostname()
    result = exe("cinder manage {host}@datera "
                 "{app}:{store}:{vol} --name {name}".format(
                     host=hostname, app=name, store=si_name, vol=vol_name,
                     name=name))
    volid = objid_from_output(result)
    poll_available("volume", volid)
    clone_name = rname()
    failed = False
    try:
        result = exe("openstack volume create {} --source {} --size 5".format(
            clone_name, volid))
        cloneid = objid_from_output(result)
        poll_available("volume", cloneid)
    except Exception:
        failed = True
    exe("openstack volume delete {}".format(cloneid))
    exe("openstack volume delete {}".format(volid))
    if failed:
        raise ValueError("Failed clone command, likely create_mode")


@testcase
def test_manage_then_snapshot(api):
    name = tname("test-manage-then-snapshot")
    si_name, vol_name = create_unmanaged_vol(api, name)
    hostname = socket.gethostname()
    result = exe("cinder manage {host}@datera "
                 "{app}:{store}:{vol} --name {name}".format(
                     host=hostname, app=name, store=si_name, vol=vol_name,
                     name=name))
    volid = objid_from_output(result)
    poll_available("volume", volid)
    snap_name = rname()
    output = exe(
        "openstack volume snapshot create {} --volume {}".format(
            snap_name, volid))
    snapid = objid_from_output(output)
    poll_available("volume snapshot", snapid)

    exe("openstack volume snapshot delete {}".format(snapid))
    time.sleep(1)
    exe("openstack volume delete {}".format(volid))


@testcase
def test_unmanage(api):
    name = tname("test-unmanage")
    volid = objid_from_output(exe(
        "openstack volume create {} --size 5".format(name)))
    time.sleep(2)
    vprint(exe("cinder unmanage {}".format(name)))
    uname = "UNMANAGED-{}".format(volid)
    time.sleep(2)
    try:
        vprint("Checking for AppInstance:", uname)
        ai = api.app_instances.get(uname)
        ai.delete()
    except ApiNotFoundError as e:
        print("Unmanaged volume {} not found".format(uname))
        print(e)
        return


def main(args):
    san_ip, san_login, san_password, tenant = read_cinder_conf()
    api = getAPI(
        san_ip, san_login, san_password, tenant=tenant, version="v2.2")
    # Tests
    tests = _TESTS
    if args.filter:
        tests = filter(lambda x: args.filter in x.__name__ or
                       args.filter == x.__name__, tests)
    for test in tests:
        test(api)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-f", "--filter")
    args = parser.parse_args()
    VERBOSE = args.verbose
    sys.exit(main(args))
