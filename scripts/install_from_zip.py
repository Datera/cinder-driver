#!/usr/bin/env python
from __future__ import (print_function, unicode_literals, division,
                        absolute_import)

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

LOC = '/usr/lib/python2.7/dist-packages/cinder/volume/drivers/'
ETC = "/etc/cinder/cinder.conf"
ETC_DEFAULT_RE = re.compile(r"^\[DEFAULT\]\s*$")
ETC_SECTION_RE = re.compile(r"^\[[Dd]atera\]\s*$")


def unarchive(afile):
    if tarfile.is_tarfile(afile):
        print("Archive is a tarfile")
        with tarfile.open(afile) as tar:
            tar.extractall()
            return tar.namelist()
    elif zipfile.is_zipfile(afile):
        print("Archive is a zipfile")
        with zipfile.ZipFile(afile) as z:
            z.extractall()
            return z.namelist()
    else:
        raise ValueError("Unsupported archive format")


def _cinder_fix_enabled_backends(lines, index):
    line = lines[index]
    v = line.split('=')[-1]
    parts = v.split(',')
    parts.append('datera')
    newline = 'enabled_backends = {}'.format(','.join(parts))
    lines[index] = newline


def _cinder_add_enabled_backends(lines, index):
    lines.insert(index, 'enabled_backends = datera')


def _cinder_fix_default_volume_type(lines, index):
    lines[index] = 'default_volume_type = datera'


def _cinder_add_default_volume_type(lines, index):
    lines.insert(index, 'default_volume_type = datera')


def _cinder_fix_debug(lines, index):
    lines[index] = 'debug = True'


def _cinder_add_debug(lines, index):
    lines.insert(index, 'debug = True')


def _cinder_add_san(lines, index, conf):
    lines.insert(index+1, 'san_ip = {}'.format(conf['mgmt_ip']))


def _cinder_fix_san(lines, index, conf):
    lines[index] = 'san_ip = {}'.format(conf['mgmt_ip'])


def _cinder_add_user(lines, index, conf):
    lines.insert(index+1, 'san_login = {}'.format(conf['username']))


def _cinder_fix_user(lines, index, conf):
    lines[index] = 'san_login = {}'.format(conf['username'])


def _cinder_add_pass(lines, index, conf):
    lines.insert(index+1, 'san_password = {}'.format(conf['password']))


def _cinder_fix_pass(lines, index, conf):
    lines[index] = 'san_password = {}'.format(conf['password'])


def _cinder_add_vbn(lines, index):
    lines.insert(index+1, 'volume_backend_name = datera')


def _cinder_fix_vbn(lines, index):
    lines[index] = 'volume_backend_name = datera'


def _cinder_add_datera_debug(lines, index):
    lines.insert(index+1, 'datera_debug = True')


def _cinder_fix_datera_debug(lines, index):
    lines[index] = 'datera_debug = True'


def _cinder_add_tenant(lines, index, conf):
    lines.insert(index, 'datera_tenant_id = {}'.format(conf['tenant']))


def _cinder_fix_tenant(lines, index, conf):
    lines[index] = 'datera_tenant_id = {}'.format(conf['tenant'])


def _discover_section(lines, conf, name):
    start = None
    end = None
    matcher = re.compile("^\[{}\]\s*$".format(name))
    for i, line in enumerate(lines):
        if matcher.match(line):
            start = i
            break
    if start is None:
        raise EnvironmentError(
            "[DEFAULT] section missing from ETC: {}".format(conf))
    end = start
    section_match = re.compile("^\[.*\]")
    for i, line in enumerate(lines[start+1:]):
        if section_match.match(line):
            break
        end += 1
    return start, end


def cinder_volume(conf, etc_conf, inplace):
    if not os.path.isfile(etc_conf):
        raise EnvironmentError(
            "cinder-volume ETC not found at: {}".format(etc_conf))
    lines = None
    with io.open(etc_conf, 'r') as f:
        lines = [elem.strip() for elem in f.readlines()]

    # Handle [DEFAULT] section
    default_start, default_end = _discover_section(lines, etc_conf, "DEFAULT")
    enabled_backends = None
    default_volume_type = None
    debug = None
    for i, line in enumerate(lines[default_start:default_end+1]):
        if line.startswith("enabled_backends"):
            enabled_backends = default_start + i
        if line.startswith("default_volume_type"):
            default_volume_type = default_start + i
        if line.startswith("debug"):
            debug = default_start + i

    if enabled_backends and "datera" not in lines[enabled_backends]:
        _cinder_fix_enabled_backends(lines, enabled_backends)
    elif not enabled_backends:
        _cinder_add_enabled_backends(lines, default_end)
    if default_volume_type and "datera" not in lines[default_volume_type]:
        _cinder_fix_default_volume_type(lines, default_volume_type)
    elif not default_volume_type:
        _cinder_add_default_volume_type(lines, default_end)
    if debug and 'True' not in lines[debug]:
        _cinder_fix_debug(lines, debug)
    elif not debug:
        _cinder_add_debug(lines, default_end)

    # Handle [datera] section
    dsection_start, dsection_end = _discover_section(lines, ETC, "datera")
    if not dsection_start:
        raise EnvironmentError(
            "[datera] section missing from /etc/cinder/cinder.conf")

    san_check = 0
    user_check = 0
    pass_check = 0
    vbn_check = 0
    debug_check = 0
    tenant_check = 0

    for i, line in enumerate(lines[dsection_start:dsection_end+1]):
        if 'san_ip' in line:
            san_check = dsection_start + i
        if 'san_login' in line:
            user_check = dsection_start + i
        if 'san_password' in line:
            pass_check = dsection_start + i
        if 'volume_backend_name' in line:
            vbn_check = dsection_start + i
        if 'datera_debug ' in line:
            debug_check = dsection_start + i
        if 'datera_tenant_id' in line:
            tenant_check = dsection_start + i

    if not san_check:
        _cinder_add_san(lines, dsection_end, conf)
    else:
        _cinder_fix_san(lines, san_check, conf)

    if not user_check:
        _cinder_add_user(lines, dsection_end, conf)
    else:
        _cinder_fix_user(lines, user_check, conf)

    if not pass_check:
        _cinder_add_pass(lines, dsection_end, conf)
    else:
        _cinder_fix_pass(lines, pass_check, conf)

    if not vbn_check:
        _cinder_add_vbn(lines, dsection_end)
    else:
        _cinder_fix_vbn(lines, vbn_check)

    if not debug_check:
        _cinder_add_datera_debug(lines, dsection_end)
    else:
        _cinder_fix_datera_debug(lines, debug_check)

    if not tenant_check:
        _cinder_add_tenant(lines, dsection_end, conf)
    else:
        _cinder_fix_tenant(lines, tenant_check, conf)

    data = '\n'.join(lines)
    if inplace:
        with io.open(ETC, 'w+') as f:
            f.write(data)
    else:
        print(data)


def main(args):
    conf = None
    with io.open(args.udc_file) as f:
        conf = json.load(f)
        print(conf)

    if not args.just_conf:
        src = None
        print("Unarchiving: ", args.c_archive)
        for name in unarchive(args.c_archive):
            if name.endswith('/src/'):
                src = os.path.join(name, 'datera')

        dat_dir = os.path.join(args.dest, 'datera')
        dat_file = os.path.join(args.dest, 'datera.py')
        dat_file_2 = os.path.join(args.dest, 'datera.pyc')
        # Remove any existing directory or files
        try:
            print("Removing:", dat_file)
            os.remove(dat_file)
        except OSError:
            pass
        try:
            print("Removing:", dat_file_2)
            os.remove(dat_file_2)
        except OSError:
            pass
        try:
            print("Removing:", dat_dir)
            shutil.rmtree(dat_dir)
        except OSError:
            pass

        print("Copying {} to {}".format(src, dat_dir))
        shutil.copytree(src, dat_dir)
        print("Unarchiving: ", args.p_archive)
        unarchive(args.p_archive)
        psdk = None
        for name in unarchive(args.c_archive):
            if name.endswith('/src/'):
                psdk = os.path.join(os.path.split(name)[:-1])
        cmd = ["sudo", "pip", "install", psdk]
        print("Running command: ", " ".join(cmd))
        print(subprocess.check_output(cmd))

    cinder_volume(conf, args.conf, args.inplace)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('c_archive',
                        help='Tarball or zipfile archive of the Datera '
                             'cinder-driver github repository')
    parser.add_argument('p_archive',
                        help='Tarball or zipfile archive of the Datera python-'
                             'sdk')
    parser.add_argument('udc_file',
                        help='Datera Universal Config File')
    parser.add_argument('--dest', default=LOC,
                        help='Destination cinder/volume/drivers folder')
    parser.add_argument('--conf', default=ETC,
                        help='Location of cinder.conf file to modify')
    parser.add_argument('--just-conf', action='store_true')
    parser.add_argument('--inplace', action='store_true')
    args = parser.parse_args()

    main(args)
    sys.exit(0)
