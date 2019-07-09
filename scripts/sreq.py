#!/usr/bin/env python
from __future__ import unicode_literals, division, print_function

import argparse
import functools
import hashlib
import json
import io
import os
import re
import shutil
import sys
import zlib

from sreq_common import OPERATORS, OPERATORS_FUNC

try:
    import text_histogram
except ImportError:
    text_histogram = None

"""
See Requests
------------

This script started out as a fun idea I had one evening where I thought it
would be cool to be able to pull out all the API requests from a busy Cinder
Volume log and then perform actions on them such as filtering or sorting.

It sort of ballooned from there and as you'll see from the code below, it
kind of grew organically without any real direction.  I apologize for some of
the code idioms you'll find in this as I was having fun writing it and that's
ALWAYS a bad sign.

I had intensions of factoring this out into several different scripts meant for
different ecosystem logging plugins, but eventually just settled on a common
logging format that this parses using regexes.  Unfortunately that means any
changes to the Python-SDK, Golang SDK or Powershell SDK connection libraries
can potentially break this.

Of all the stuff I've written for ecosystem, this has been the most helpful
for diagnosing customer issues quickly as it lets me instantly construct a
history of the requests to the Datera backend.

VERSION HISTORY:
    1.0.0 -- Initial sreq.py version
    1.0.1 -- Addition of timestamp filtering
    1.1.0 -- Adding journalctl compatibility
    1.2.0 -- Added ability to leave off field in --filter to check all fields
    1.2.1 -- Moved some stuff out into sreq_common
    1.2.2 -- Compatibility updates
    1.2.3 -- Journalctl timestamp compatibility update
    1.2.4 -- Go-SDK support, results caching, bugfixes
"""
VERSION = "v1.2.4"

CACHE = '/tmp/.sreq/'

USAGE = """

SREQ
----
The Datera Cinder Driver Request Sorter

Basic
    $ ./sreq.py /your/cinder-volume/log/location.log

Processed logfiles are compressed and cached in {} for faster subsequent runs

Multiple Log Files
    $ ./sreq.py /your/cinder-volume/log/location.log \
/your/cinder-volume/log/location2.log

Pretty print the results
    $ ./sreq.py /your/cinder-volume/log/location.log --pretty

Output JSON
    $ ./sreq.py /your/cinder-volume/log/location.log --json

Display available enum values
    $ ./sreq.py /your/cinder-volume/log/location.log --print-enums

Sort the results by an enum value
    $ ./sreq.py /your/cinder-volume/log/location.log --pretty --sort RESDELTA

Filter by an exact enum value
    $ ./sreq.py /your/cinder-volume/log/location.log \
--pretty \
--filter REQTRACE=7913f69f-3d56-49e0-a347-e095b982fb6a

Filter by enum contents

    $ ./sreq.py /your/cinder-volume/log/location.log \
--pretty \
--filter "REQPAYLOAD##OS-7913f69f-3d56-49e0-a347-e095b982fb6a"

Filter by all enum contents

    $ ./sreq.py /your/cinder-volume/log/location.log \
--pretty \
--filter "##OS-7913f69f-3d56-49e0-a347-e095b982fb6a"

Show only requests without replies
    $ ./sreq.py /your/cinder-volume/log/location.log \
--pretty \
--orphans

Show Volume Attach/Detach (useful for mapping volume to instance)
    $ ./sreq.py /your/cinder-volume/log/location.log --attach-detach

Get the slowest 10 requests
    $ ./sreq.py /your/cinder-volume/log/location.log
--pretty \
--sort RESDELTA \
--limit 10

Get the average duration of all requests
    $ ./sreq.py /your/cinder-volume/log/location.log
--pretty \
| grep RESDELTA | awk '{ total += $3; count++ } END { print total/count }'
""".format(CACHE)

DREQ = re.compile(r"""^(?P<time>\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d{3}|Z)).*?
Datera Trace ID: (?P<trace>(\w+-\w+-\w+-\w+-\w+)|None)
Datera Request ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)
Datera Request URL: (?P<url>.*?)
Datera Request Method: (?P<method>.*?)
Datera Request Payload: (?P<payload>.*?)
Datera Request Headers: (?P<headers>.*?[\*\}])""", re.DOTALL)

DRES = re.compile(r"""^(?P<time>\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d{3}|Z)).*?
Datera Trace ID: (?P<trace>(\w+-\w+-\w+-\w+-\w+)|None)
Datera Response ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)
Datera Response TimeDelta: (?P<delta>\d+\.\d*)s
Datera Response URL: (?P<url>.*?)
Datera Response Payload: (?P<payload>.*?)
Datera Response Object:""", re.DOTALL)

# Journalctl matchers
# ISO8601 2018-01-08T20:04:47+0000
DREQ_J = re.compile(r"""^(?P<time>(\w+ \d\d \d\d:\d\d:\d\d)|(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+\d{4})).*
.*Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+|None)
.*Datera Request ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)
.*Datera Request URL: (?P<url>.*?)
.*Datera Request Method: (?P<method>.*?)
.*Datera Request Payload: (?P<payload>.*?)
.*Datera Request Headers: (?P<headers>.*?\})""")

# ISO8601 2018-01-08T20:04:47+0000
DRES_J = re.compile(r"""^(?P<time>(\w+ \d\d \d\d:\d\d:\d\d)|(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+\d{4})).*
.*Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+|None)
.*Datera Response ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)
.*Datera Response TimeDelta: (?P<delta>\d+\.\d\d?\d?)s
.*Datera Response URL: (?P<url>.*?)
.*Datera Response Payload: (?P<payload>.*)
""")
#######

ATTACH = re.compile(r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*"
                    r"Attaching volume (?P<vol>(\w+-){4}\w+) to instance "
                    r"(?P<vm>(\w+-){4}\w+) at mountpoint (?P<device>\S+) "
                    r"on host (?P<host>\S+)\.")

DETACH = re.compile(r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*"
                    r"Detaching volume (?P<vol>(\w+-){4}\w+) from instance "
                    r"(?P<vm>(\w+-){4}\w+)")

# Journalctl matchers
ATTACH_J = re.compile(r"^(?P<time>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+\d{4}).*"
                      r"Attaching volume (?P<vol>(\w+-){4}\w+) to instance "
                      r"(?P<vm>(\w+-){4}\w+) at mountpoint (?P<device>\S+) "
                      r"on host (?P<host>\S+)\.")

DETACH_J = re.compile(r"^(?P<time>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+\d{4}).*"
                      r"Detaching volume (?P<vol>(\w+-){4}\w+) from instance "
                      r"(?P<vm>(\w+-){4}\w+)")
#######

TUP_VALS = {"REQTIME":     0,
            "REQTRACE":    1,
            "REQID":       2,
            "REQURL":      3,
            "REQMETHOD":   4,
            "REQPAYLOAD":  5,
            "REQHEADERS":  6,
            "RESTIME":     7,
            "RESTRACE":    8,
            "RESID":       9,
            "RESDELTA":   10,
            "RESURL":     11,
            "RESPAYLOAD": 12}

TUP_DESCRIPTIONS = {"REQTIME":     "Request Timestamp",
                    "REQTRACE":    "Request Trace ID",
                    "REQID":       "Request ID",
                    "REQURL":      "Request URL",
                    "REQMETHOD":   "Request Method",
                    "REQPAYLOAD":  "Request Payload",
                    "REQHEADERS":  "Request Headers",
                    "RESTIME":     "Response Timestamp",
                    "RESTRACE":    "Response Trace ID",
                    "RESID":       "Response ID",
                    "RESDELTA":    "Response Time Delta",
                    "RESURL":      "Response URL",
                    "RESPAYLOAD":  "Response Payload"}

AD_VALS = {"TIME": 0,
           "TYPE": 1,
           "VOLID": 2,
           "VMID": 3,
           "DEVICE": 4,
           "HOST": 5}


def _sort_helper(sort, x):
    if sort == "RESDELTA":
        return float(x[TUP_VALS[sort.upper()]])
    try:
        return x[TUP_VALS[sort.upper()]]
    except IndexError:
        return x[AD_VALS[sort.upper()]]


def filter_func(found, loc, val, operator):
    if operator:
        return (x for x in found if len(x) >= loc and operator(x[loc], val))
    return found


def filter_all(found, val, operator, fields):
    for f in found:
        for field in fields.values():
            if operator(f[field], val):
                yield f


def get_filtered(filters, data, vals):
    fdata = data
    for f in filters:
        k, v, operator = None, None, None
        for operator in OPERATORS.keys():
            sp = f.split(operator, 1)
            if len(sp) == 2:
                k, v = sp
                break
        if not k:
            fdata = filter_all(fdata, v, OPERATORS_FUNC[operator], vals)
        else:
            k = k.strip().upper()
            fdata = filter_func(fdata,
                                vals[k.upper()],
                                v,
                                OPERATORS_FUNC[operator])
    return fdata


# TODO: Currently only finds attach/detach for boot-from-volume attachments.
# Manual attach/detach does not generate the necessary log messages for us
# to track it (weirdly).
def find_attach_detach(f, lines, journalctl=False):
    attach_detach = []
    for line in lines:
        if journalctl:
            amatch = ATTACH_J.match(line)
            dmatch = DETACH_J.match(line)
        else:
            amatch = ATTACH.match(line)
            dmatch = DETACH.match(line)
        if amatch:
            attach_detach.append((
                amatch.group("time"),
                'attach',
                amatch.group("vol"),
                amatch.group("vm"),
                amatch.group("device"),
                amatch.group("host")))
        if dmatch:
            attach_detach.append((
                dmatch.group("time"),
                'detach',
                dmatch.group("vol"),
                dmatch.group("vm"),
                None,
                None))
    if f:
        attach_detach = get_filtered(f, attach_detach, AD_VALS)
    return attach_detach


def get_attach_detach(f, data):
    jsond = []
    ad = find_attach_detach(f, data, args.journalctl)
    limit = args.limit if args.limit else None
    if args.sort.upper() == 'RESDELTA':
        sort = 'TIME'
    else:
        sort = args.sort.upper()
    for entry in reversed(sorted(ad, key=lambda x: x[AD_VALS[sort]])):
        if limit == 0:
            break
        elif args.json:
            d = {}
            for enum, val in AD_VALS.items():
                d[enum] = entry[val]
            jsond.append(d)
        elif args.pretty:
            print()
            for enum, val in sorted(AD_VALS.items(), key=lambda x: x[1]):
                print(enum, ":", entry[val])
            print()
        elif not args.quiet:
            print()
            print(*entry)
            print()

        if limit:
            limit -= 1
    if jsond:
        print(json.dumps(jsond))
    sys.exit(0)


# Turns multiple files into one big file generator
def gen_file_data(files):
    for file in files:
        with io.open(file) as f:
            for line in f:
                yield line


# Generator for log block data
def gen_log_blocks(data):
    prev = None
    for line in data:
        if "Datera Trace ID" in line:
            found = [prev, line]
            while True:
                this = next(data)
                found.append(this)
                if ("Datera Response Object" in this or
                        "Datera Request Headers" in this):
                    break
            yield "".join(found).replace("\r", "")
        prev = line


def get_match_dict(args, data, journalctl=False):
    found = {}

    for logb in gen_log_blocks(data):
        if args.journalctl:
            req_match = DREQ_J.match(logb)
        else:
            req_match = DREQ.match(logb)
        if req_match:
            found[req_match.group("rid")] = [req_match.group("time"),
                                             req_match.group("trace"),
                                             req_match.group("rid"),
                                             req_match.group("url"),
                                             req_match.group("method"),
                                             req_match.group("payload"),
                                             req_match.group("headers")]
        else:
            if args.journalctl:
                res_match = DRES_J.match(logb)
            else:
                res_match = DRES.match(logb)
            if not res_match:
                raise ValueError("Regex didn't match\n{}".format(logb))
            rrid = res_match.group("rid")
            if rrid in found:
                found[res_match.group("rid")].extend(
                        [res_match.group("time"),
                         res_match.group("trace"),
                         res_match.group("rid"),
                         res_match.group("delta"),
                         res_match.group("url"),
                         res_match.group("payload")])
    return found


def orphan_filter(found):
    for entry in found.values() if type(found) == dict else found:
        if len(entry) < len(TUP_VALS):
            yield entry


def print_results(result):
    limit = args.limit if args.limit else None
    out = []
    sh = functools.partial(_sort_helper, args.sort)
    for entry in reversed(sorted(result, key=sh)):
        if limit == 0:
            break
        elif args.json:
            d = {}
            for enum, val in TUP_VALS.items():
                try:
                    d[enum] = entry[val]
                except IndexError:
                    pass
            out.append(d)
        elif args.pretty:
            print()
            for enum, val in sorted(TUP_VALS.items(), key=lambda x: x[1]):
                try:
                    print(enum, ":", entry[val])
                except IndexError:
                    pass
            print()
        elif not args.quiet:
            print()
            print(*entry)
            print()
        if limit:
            limit -= 1
    if args.json:
        print(json.dumps(out))


def print_stats(result):
    lfound = [float(elem[TUP_VALS["RESDELTA"]]) for elem in result]
    if args.limit:
        lfound = lfound[:args.limit]
    print("\n=========================\n")
    print("Count:", len(lfound))

    print("Highest:", max(lfound))
    print("Lowest:", min(lfound))

    mean = round(sum(lfound) / len(lfound), 3)
    print("Mean:", mean)

    rfound = [round(elem, 2) for elem in lfound]
    print("Mode:", max(set(rfound), key=lambda x: rfound.count(x)))

    quotient, remainder = divmod(len(lfound), 2)
    if remainder:
        median = sorted(lfound)[quotient]
    else:
        median = sum(sorted(lfound)[quotient - 1:quotient + 1]) / 2
    print("Median:", median)
    print()
    print()

    if text_histogram:
        hg = text_histogram.histogram
        hg(lfound, buckets=5, calc_msvd=False)
        print()
        print()


def print_operators():
    for k, v in sorted(OPERATORS.items()):
        print(str("{:>10}: {}".format(k, v)))


def print_enums():
    for elem in sorted(TUP_VALS.keys()):
        print(str("{:>10}: {}".format(elem, TUP_DESCRIPTIONS[elem])))


def main(args):
    if args.version:
        print("SREQ\n----\nThe Datera Cinder Driver Request Sorter\n",
              VERSION)
        sys.exit(0)
    if args.print_enums:
        print_enums()
        sys.exit(0)
    if args.print_operators:
        print_operators()
        sys.exit(0)
    if args.clear_cache:
        shutil.rmtree(CACHE)

    if not os.path.exists(CACHE):
        os.mkdir(CACHE)

    files = args.logfiles

    pfname = ':'.join(sorted(map(os.path.basename, files))).encode('utf-8')
    md5 = hashlib.md5()
    md5.update(pfname)
    cname = os.path.join(CACHE, md5.hexdigest())
    if not args.no_cache and os.path.exists(cname) and not args.attach_detach:
        with io.open(cname, 'rb') as f:
            found = json.loads(zlib.decompress(f.read()).decode('utf-8'))
    else:
        data = gen_file_data(files)
        if args.attach_detach:
            get_attach_detach(args.filter, data)
            sys.exit(0)

        found = get_match_dict(args, data, args.journalctl).values()
        with io.open(cname, 'wb+') as f:
            data = zlib.compress(json.dumps(list(found)).encode('utf-8'))
            f.write(data)

    if args.filter:
        found = get_filtered(args.filter, found, TUP_VALS)

    result = orphan_filter(found) if args.orphans else found

    print_results(result)

    if args.stats:
        print_stats(result)
    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(USAGE)
    parser.add_argument("logfiles", nargs="*",
                        help="Must be uncompressed text files")
    parser.add_argument("--print-enums", action="store_true",
                        help="Print available sorting/filtering enums")
    parser.add_argument("--print-operators", action="store_true",
                        help="Print available filtering operators")
    parser.add_argument("-q", "--quiet", action='store_true',
                        help="Don't print anything except explicit options")
    parser.add_argument("-f", "--filter", default=[], action='append',
                        help="Filter by this enum value and operator: "
                        "'REQPAYLOAD##someid'.  MAKE SURE TO QUOTE ARGUMENT")
    parser.add_argument("-s", "--sort", default="REQTIME",
                        help="Sort by this enum value: Eg: REQTIME")
    parser.add_argument("-l", "--limit", default=None, type=int,
                        help="Limit results to provided value")
    parser.add_argument("-p", "--pretty", action="store_true",
                        help="Pretty print results")
    parser.add_argument("-j", "--json", action="store_true",
                        help="Print results in JSON")
    parser.add_argument("-o", "--orphans", action="store_true",
                        help="Display only orphan requests, ie. requests that "
                             "did not recieve any response")
    parser.add_argument("-a", "--attach-detach", action="store_true",
                        help="Show attaches/detaches")
    parser.add_argument("-v", "--version", action="store_true",
                        help="Print version info")
    parser.add_argument("--journalctl", action="store_true",
                        help="Log is journalctl based, use journalctl parsers")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--no-cache", action="store_true",
                        help="Don't read from cached results")
    parser.add_argument("--clear-cache", action="store_true",
                        help="Delete cached results from system")
    args = parser.parse_args()
    sys.exit(main(args))
