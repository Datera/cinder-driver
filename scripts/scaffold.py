from __future__ import (print_function, unicode_literals, division,
                        absolute_import)

import io
import re

from dfs_sdk import get_api

IPRE_STR = r'(\d{1,3}\.){3}\d{1,3}'
IPRE = re.compile(IPRE_STR)

SIP = re.compile(r'san_ip\s+?=\s+?(?P<san_ip>%s)' % IPRE_STR)
SLG = re.compile(r'san_login\s+?=\s+?(?P<san_login>.*)')
SPW = re.compile(r'san_password\s+?=\s+?(?P<san_password>.*)')
TNT = re.compile(r'datera_tenant_id\s+?=\s+?(?P<tenant_id>.*)')

LATEST = "2.2"


def read_cinder_conf():
    data = None
    found_index = 0
    found_last_index = -1
    with io.open('/etc/cinder/cinder.conf') as f:
        for index, line in enumerate(f):
            if '[datera]' == line.strip().lower():
                found_index = index
                break
        for index, line in enumerate(f):
            if '[' in line and ']' in line:
                found_last_index = index + found_index
                break
    with io.open('/etc/cinder/cinder.conf') as f:
        data = "".join(f.readlines()[
            found_index:found_last_index])
    san_ip = SIP.search(data).group('san_ip')
    san_login = SLG.search(data).group('san_login')
    san_password = SPW.search(data).group('san_password')
    tenant = TNT.search(data)
    if tenant:
        tenant = tenant.group('tenant_id')
    return san_ip, san_login, san_password, tenant


def getAPI(san_ip, san_login, san_password, version=None, tenant=None):
    csan_ip, csan_login, csan_password, ctenant = None, None, None, None
    try:
        if not all((san_ip, san_login, san_password, tenant)):
            csan_ip, csan_login, csan_password, ctenant = read_cinder_conf()
    except IOError:
        pass
    # Set from cinder.conf if they don't exist
    # This allows overriding some values in cinder.conf
    if not tenant:
        tenant = ctenant
    if not san_ip:
        san_ip = csan_ip
    if not san_login:
        san_login = csan_login
    if not san_password:
        san_password = csan_password
    if tenant and "root" not in tenant and tenant != "all":
        tenant = "/root/{}".format(tenant)
    if not tenant:
        tenant = "/root"
    if not version:
        version = "v{}".format(LATEST)
    else:
        version = "v{}".format(version.strip("v"))
    return get_api(san_ip,
                   san_login,
                   san_password,
                   version,
                   tenant=tenant,
                   secure=True,
                   immediate_login=True)
