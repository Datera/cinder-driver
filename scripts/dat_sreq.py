#!/usr/bin/env python
from __future__ import unicode_literals, division, print_function

import argparse
import binascii
import gzip
import json
import io
import sys

from sreq_common import OPERATORS, OPERATORS_FUNC

import arrow
import demjson

VERSION = "v1.0.0"
VERBOSE = False
GZIP_MAGIC = "1f8b"


ACK_F = "msg=Sending HB ACK"
SYS_F = "path##/system"
API_F = "path=/api_versions"


def filtered(data, f):
    k, v, operator = None, None, None
    for operator in OPERATORS.keys():
        sp = f.split(operator, 1)
        if len(sp) == 2:
            k, v = sp
            break
    # if not k:
    #     fdata = filter_all(fdata, v, OPERATORS_FUNC[operator], vals)
    k = k.strip()
    if OPERATORS_FUNC[operator](data.get(k, ''), v):
        return True


def is_gzip(filehandle):
    dat = filehandle.read(2)
    try:
        dat = bytes(dat, "utf-8")
    except ValueError:
        pass
    dat = binascii.hexlify(dat)
    filehandle.seek(0)
    return dat == GZIP_MAGIC


def pos_filters(data, filters):
    if not filters:
        return True
    for f in filters:
        if not filtered(data, f):
            return False


def neg_filters(data, filters):
    if not filters:
        return False
    for f in filters:
        if filtered(data, f):
            return True


def gen_entries(logfiles, neg_filter, pos_filter):
    for log in sorted(logfiles):
        with io.open(log) as f:
            if is_gzip(f):
                f = gzip.open(log)
            buffer = ''
            load = False
            for line in f:
                line = line.rstrip()
                if line.endswith('}'):
                    buffer += line
                    load = True
                else:
                    buffer += line

                if load:
                    try:
                        data = json.loads(buffer)
                    except json.decoder.JSONDecodeError:
                        # This is needed because some JSON lines in the
                        # logfiles haven't been run through javascript's
                        # Stringify method.
                        data = demjson.decode(buffer)
                    load = False
                    buffer = ''
                    if neg_filters(data, neg_filter):
                        continue
                    if pos_filters(data, pos_filter):
                        yield data


def print_normal(entries, pretty, timefmt=False, key="all"):
    for entry in entries:
        if not entry:
            continue
        if timefmt and "time" in entry:
            entry["time"] = entry["time"].format("YYYY-MM-DD:HH:mm.SSS")
        if key == "all":
            if pretty:
                print(json.dumps(entry, indent=4))
            else:
                print(entry)
        else:
            print(entry[key])


def print_limit(entries, sort, reverse, pretty):
    slots = [{}] * args.limit
    for entry in entries:
        if sort == "time" and "time" in entry:
            entry['time'] = arrow.get(entry['time'])
        if entry.get(sort) is None:
            continue
        for index, slot in enumerate(slots):
            if not slot:
                slots[index] = entry
                break
            elif reverse and entry[sort] < slot[sort]:
                slots.pop()
                slots.insert(index, entry)
                break
            elif not reverse and entry[sort] > slot[sort]:
                slots.pop()
                slots.insert(index, entry)
                break
    print_normal(slots, pretty, timefmt=True, key=sort)


def main(args):
    logfiles = args.logfiles
    entries = gen_entries(logfiles, args.neg_filter, args.pos_filter)

    if args.limit:
        print_limit(entries, args.sort, args.reverse, args.pretty)
    else:
        print_normal(entries, args.pretty)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("logfiles", nargs="*",
                        help="Logfiles, must either be uncompressed or gziped")
    parser.add_argument("-n", "--neg_filter", default=[ACK_F, SYS_F, API_F],
                        action="append")
    parser.add_argument("-p", "--pos_filter", default=[], action="append")
    parser.add_argument("-l", "--limit", default=0, type=int)
    # parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-s", "--sort")
    parser.add_argument("-r", "--reverse", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    if args.sort and args.limit == 0:
        parser.error("If --sort is specified, --limit must be greater than 0")
    sys.exit(main(args))
