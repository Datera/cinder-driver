#!/usr/bin/env python

from __future__ import (print_function, unicode_literals, division,
                        absolute_import)

import argparse
import os
import sys
import threading
import time
import uuid

import cinderclient
import novaclient
from cinderclient import client as cclient
from novaclient import client as nclient
from keystoneauth1.identity import v3
from keystoneauth1 import session

import six.moves.queue as queue


def usage():
    print("OS_USERNAME, OS_PASSWORD, OS_AUTH_URL and "
          "OS_PROJECT_NAME must be set")


def clean_volumes(cc):
    for volume in cc.volumes.list():
        try:
            volume.delete()
        except cinderclient.exceptions.BadRequest:
            pass


def clean_servers(nc):
    for server in nc.servers.list():
        try:
            server.delete()
        except novaclient.exceptions.BadRequest:
            pass
        except novaclient.exceptions.NotFound:
            pass

    timeout = 5
    while timeout:
        if len(nc.servers.list()) == 0:
            return
        else:
            timeout -= 1
            time.sleep(3)


def get_clients():
    # Get environment vars
    username = os.environ.get('OS_USERNAME')
    password = os.environ.get('OS_PASSWORD')
    auth_url = os.environ.get('OS_AUTH_URL')
    project_name = os.environ.get('OS_PROJECT_NAME')
    user_domain_name = os.environ.get('OS_USER_DOMAIN_NAME')
    project_domain_name = os.environ.get('OS_PROJECT_DOMAIN_NAME')

    if not all((username, password, auth_url, project_name)):
        usage()

    auth = v3.Password(auth_url=auth_url,
                       username=username,
                       password=password,
                       project_name=project_name,
                       user_domain_name=user_domain_name,
                       project_domain_name=project_domain_name)

    sess = session.Session(auth=auth)

    nc = nclient.Client('2.1', session=sess)
    cc = cclient.Client('2.1', session=sess)
    return nc, cc


def create_volume(cc, size, vol_ref=None, vols=None, image_ref=None):
    vol = cc.volumes.create(size, source_volid=vol_ref, imageRef=image_ref)
    while True:
        vol.get()
        if vol.status == 'available':
            if vols:
                vols.put(vol)
            return vol


def create_server(nc, root_vol, data_vol, flavor, net_id):
    name = "myvm-{}".format(str(uuid.uuid4()))
    server = nc.servers.create(name, '', flavor,
                               nics=[{'net-id': net_id}],
                               block_device_mapping={'vda': root_vol.id})
    while True:
        server.get()
        if server.status == 'ACTIVE':
            break
    nc.volumes.create_server_volume(server.id, data_vol.id)
    # print(attached.status)
    return server


def main(args):

    nc, cc = get_clients()

    if args.clean:
        clean_volumes(cc)
        clean_servers(nc)

    # Create initial volume:
    vol = create_volume(cc, args.root_size, image_ref=args.image_id)
    vol.detach()

    root_vols = queue.Queue()
    data_vols = queue.Queue()
    for vm in range(args.num_vms):
        threading.Thread(target=create_volume,
                         args=(cc, args.root_size),
                         kwargs={'vols': root_vols,
                                 'vol_ref': vol.id}).start()
        threading.Thread(target=create_volume,
                         args=(cc, args.data_size),
                         kwargs={'vols': data_vols}).start()

    for vm in range(args.num_vms):
        root_vol = root_vols.get()
        data_vol = data_vols.get()
        threading.Thread(target=create_server,
                         args=(nc, root_vol, data_vol, args.flavor_id,
                               args.net_id)).start()

    print("VMs: {}, Root Volume Size: {}, Data Volume Size: {}".format(
        args.num_vms, args.root_size, args.data_size))

    raw_input("Press any key to tear down")

    clean_servers(nc)
    clean_volumes(cc)

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('num_vms', type=int)
    parser.add_argument('root_size', type=int)
    parser.add_argument('data_size', type=int)
    parser.add_argument('image_id',
                        help="ID for image to use in root volume")
    parser.add_argument('net_id')
    parser.add_argument('flavor_id')
    parser.add_argument('-c', '--clean', action='store_true',
                        help='Clean volumes and servers before running')

    args = parser.parse_args()

    sys.exit(main(args))
