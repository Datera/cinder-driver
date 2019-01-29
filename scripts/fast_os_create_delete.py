#!/usr/bin/env python

from __future__ import (print_function, unicode_literals, division,
                        absolute_import)

import argparse
import random
import sys
import threading
import uuid

# PY 2/3 COMPAT
try:
    import Queue as queue
    input = raw_input  # noqa
except ImportError:
    import queue

import openstack


PLOCK = threading.Lock()


def tprint(*args, **kwargs):
    with PLOCK:
        print(*args, **kwargs)


def gen_name():
    return "create-delete-test-{}".format(str(uuid.uuid4())[:6])


def create_volume(conn, type_id):
    name = gen_name()
    size = random.randint(1, 6)
    tprint("Creating volume: name {}, size {}".format(name, size))
    return conn.block_store.create_volume(
        name=name, size=size, type=type_id).id


def delete_volume(conn, vid):
    tprint("Deleting volume: {}".format(vid))
    conn.block_store.delete_volume(vid)


def create_worker(conn, q, dq):
    while True:
        try:
            type_id = q.get(block=False)
            vid = create_volume(conn, type_id)
            dq.put(vid)
        except queue.Empty:
            tprint("Create queue empty, returning")
            return


def delete_worker(conn, q):
    while True:
        try:
            vid = q.get(block=False)
            delete_volume(conn, vid)
        except queue.Empty:
            tprint("Delete queue empty returning")
            return


def create_volumes(conn, count, type_id, max_workers):
    print("Starting volume creation. count {}, type_id {}, "
          "max_workers {}".format(count, type_id, max_workers))
    q = queue.Queue()
    dq = queue.Queue()
    for _ in range(count):
        q.put(type_id)
    threads = []
    for _ in range(max_workers):
        thread = threading.Thread(target=create_worker, args=(conn, q, dq))
        thread.start()
        threads.append(thread)
    for t in threads:
        t.join()
    return dq


def delete_volumes(conn, q, max_workers):
    print("Starting volume deletion. max_workers {}".format(max_workers))
    threads = []
    for _ in range(max_workers):
        thread = threading.Thread(target=delete_worker, args=(conn, q))
        thread.start()
        threads.append(thread)
    for t in threads:
        t.join()


def main(args):
    conn = openstack.connect()
    dq = create_volumes(conn, args.count, args.type_id, args.max_workers)

    if not args.no_wait:
        input("Press [ENTER] to start deletion")

    delete_volumes(conn, dq, args.max_workers)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("count", type=int)
    parser.add_argument("-n", "--no-wait", action='store_true',
                        help="Don't wait for creations to finish before "
                             "initiating deletions")
    parser.add_argument("-t", "--type-id",
                        help="Volume Type id to use during volume creation")
    parser.add_argument("--max-workers", type=int, default=10)
    args = parser.parse_args()
    sys.exit(main(args))
