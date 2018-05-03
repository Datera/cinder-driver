#!/usr/bin/env python

from __future__ import (print_function, unicode_literals, division,
                        absolute_import)

import argparse
import random
import subprocess
import sys
import threading
import time
import uuid

try:
    import queue
except ImportError:
    import Queue
    queue = Queue

# import openstack

CEPH_VT = "ceph-vt"
VOL_INTERVAL = 20
WORKERS = 5
VERBOSE = True
PLOCK = threading.Lock()

pqueue = queue.Queue()


def vprint(*args, **kwargs):
    if VERBOSE:
        PLOCK.acquire()
        print(*args, **kwargs)
        PLOCK.release()


def gen_name():
    return "ceph-to-datera-{}".format(str(uuid.uuid4())[:6])


def create_ceph_volume_type(conn):
    vprint("Creating Ceph Volume Type:", CEPH_VT)
    return conn.block_storage.create_volume_type(name=CEPH_VT).id


def create_volume(conn, type_id):
    name = gen_name()
    size = random.randint(5, 20)
    vprint("Creating Volume:", name, "Size:", size)
    conn.block_storage.create_volume(name=name, size=size, type=type_id)
    return name


def migrate_ceph_to_datera(conn, vol_id):
    vprint("Migrating Ceph Volume:", vol_id, "To Datera")
    conn.block_storage
    pass


def delete_volume(conn, vol_id):
    vprint("Deleting Volume:", vol_id)
    conn.block_storage.delete_volume(vol_id)


def workflow(q, conn):
    vprint("Starting Workflow")
    while True:
        q.get()
        vprint("Got Workflow Request")
        ceph_type_id = create_ceph_volume_type(conn)
        cvid = create_volume(conn, ceph_type_id)
        migrate_ceph_to_datera(conn, cvid)
        delete_volume(conn, cvid)


def queue_adder(q):
    while True:
        for _ in range(WORKERS):
            vprint("Adding to queue")
            q.put("")
        time.sleep(VOL_INTERVAL)


def exe(cmd):
    vprint("Running cmd:", cmd)
    return subprocess.check_output(cmd, shell=True).decode("utf-8")


def main(args):
    # conn = openstack.connect()
    conn = None
    threads = []
    q = queue.Queue()
    # Start queue thread
    qt = threading.Thread(target=queue_adder, args=(q,))
    qt.daemon = True
    qt.start()

    # Start threads
    for _ in range(WORKERS):
        thread = threading.Thread(target=workflow, args=(q, conn))
        threads.append(thread)
        thread.daemon = True
        thread.start()

    time.sleep(60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('ceph_type_id')
    parser.add_argument('datera_type_id')
    args = parser.parse_args()
    sys.exit(main(args))
