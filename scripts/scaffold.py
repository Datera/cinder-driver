from __future__ import (print_function, unicode_literals, division,
                        absolute_import)

import io
import re

IPRE_STR = r'(\d{1,3}\.){3}\d{1,3}'
IPRE = re.compile(IPRE_STR)

SIP = re.compile(r'san_ip\s+?=\s+?(?P<san_ip>%s)' % IPRE_STR)
SLG = re.compile(r'san_login\s+?=\s+?(?P<san_login>.*)')
SPW = re.compile(r'san_password\s+?=\s+?(?P<san_password>.*)')
DC = re.compile(r'driver_client_cert\s+?=\s+?(?P<driver_client_cert>.*)')
DCK = re.compile(
    r'driver_client_cert_key\s+?=\s+?(?P<driver_client_cert_key>.*)')


def readCinderConf(file=None):
    if not file:
        file = '/etc/cinder/cinder.conf'
    data = None
    with io.open(file) as f:
        data = f.read()
    san_ip = SIP.search(data).group('san_ip')
    san_login = SLG.search(data).group('san_login')
    san_password = SPW.search(data).group('san_password')
    try:
        driver_cert = DC.search(data).group('driver_client_cert')
        driver_cert_key = DC.search(data).group('driver_client_cert_key')
    except AttributeError:
        driver_cert = None
        driver_cert_key = None
    return san_ip, san_login, san_password, driver_cert, driver_cert_key
