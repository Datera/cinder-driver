#!/usr/bin/env python
from __future__ import unicode_literals, division, print_function

import argparse
import json
import sys
import re

try:
    import text_histogram
except ImportError:
    text_histogram = None

VERSION = "v1.0.0"

USAGE = """

SREQ
----
The Datera Cinder Driver Request Sorter

Basic
    $ ./sreq.py /your/cinder-volume/log/location.log

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
--filter REQPAYLOAD=OS-7913f69f-3d56-49e0-a347-e095b982fb6a
--check-contents

Show only requests without replies
    $ ./sreq.py /your/cinder-volume/log/location.log \
--pretty \
--orphans

Show Volume Attach/Detach (useful for mapping volume to instance)
    $ ./sreq.py /your/cinder-volume/log/location.log --attach-detach
"""
DREQ = re.compile(r"""^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*?
Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+)
Datera Request ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)
Datera Request URL: (?P<url>.*?)
Datera Request Method: (?P<method>.*?)
Datera Request Payload: (?P<payload>.*?)
Datera Request Headers: (?P<headers>.*?\})""")

DRES = re.compile(r"""^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*?
Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+)
Datera Response ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)
Datera Response TimeDelta: (?P<delta>\d+\.\d\d?\d?)s
Datera Response URL: (?P<url>.*?)
Datera Response Payload: (?P<payload>.*?)
Datera Response Object.*""")

ATTACH = re.compile(r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*"
                    r"Attaching volume (?P<vol>(\w+-){4}\w+) to instance "
                    r"(?P<vm>(\w+-){4}\w+) at mountpoint (?P<device>\S+) "
                    r"on host (?P<host>\S+)\.")

DETACH = re.compile(r"^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*"
                    r"Detaching volume (?P<vol>(\w+-){4}\w+) from instance "
                    r"(?P<vm>(\w+-){4}\w+)")

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

OPERATORS = {"=": "X equals Y",
             ">": "X greater than Y",
             "<": "X less than Y",
             ">=": "X greather than or equals Y",
             "<=": "X less than or equals Y",
             "##": "X contains Y"}

OPERATORS_FUNC = {"=": lambda x, y: x == y,
                  ">": lambda x, y: float(x) > float(y),
                  "<": lambda x, y: float(x) < float(y),
                  ">=": lambda x, y: float(x) >= float(y),
                  "<=": lambda x, y: float(x) <= float(y),
                  "##": lambda x, y: y in x}


def filter_func(found, loc, val, operator):
    if operator:
        return filter(lambda x: len(x) >= loc and operator(x[loc], val), found)
    else:
        return found


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
            raise ValueError("No valid operator detected in {}".format(
                filters))
        k = k.strip().upper()
        fdata = filter_func(fdata,
                            vals[k.upper()],
                            v,
                            OPERATORS_FUNC[operator])
    return fdata


def find_attach_detach(lines):
    attach_detach = []
    for line in lines:
        amatch = ATTACH.match(line)
        if amatch:
            attach_detach.append((
                amatch.group("time"),
                'attach',
                amatch.group("vol"),
                amatch.group("vm"),
                amatch.group("device"),
                amatch.group("host")))
        dmatch = DETACH.match(line)
        if dmatch:
            attach_detach.append((
                dmatch.group("time"),
                'detach',
                dmatch.group("vol"),
                dmatch.group("vm"),
                None,
                None))
    if args.filter:
        attach_detach = get_filtered(args.filter, attach_detach, AD_VALS)
    return attach_detach


def get_attach_detach(args, data):
    jsond = []
    ad = find_attach_detach(data)
    limit = args.limit if args.limit else len(ad)
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

        limit -= 1
    if jsond:
        print(json.dumps(jsond))
    sys.exit(0)


def main(args):
    if args.version:
        print("SREQ\n----\nThe Datera Cinder Driver Request Sorter\n",
              VERSION)
        sys.exit(0)
    if args.print_enums:
        for elem in sorted(TUP_VALS.keys()):
            print(str("{:>10}: {}".format(elem, TUP_DESCRIPTIONS[elem])))
        sys.exit(0)
    if args.print_operators:
        for k, v in sorted(OPERATORS.items()):
            print(str("{:>10}: {}".format(k, v)))
        sys.exit(0)
    files = args.logfiles
    data = []
    for file in files:
        with open(file) as f:
            data.extend(f.readlines())

    if args.attach_detach:
        get_attach_detach(args, data)

    found = {}

    log_blocks = []
    for index, line in enumerate(data):
        if line.startswith("Datera Trace"):
            log_blocks.append(
                "".join(data[index - 1: index + 6]).replace("\r", ""))

    for logb in log_blocks:
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
            res_match = DRES.match(logb)
            if not res_match:
                raise ValueError("No match\n{}".format(logb))
            found[res_match.group("rid")].extend([res_match.group("time"),
                                                  res_match.group("trace"),
                                                  res_match.group("rid"),
                                                  res_match.group("delta"),
                                                  res_match.group("url"),
                                                  res_match.group("payload")])

    found = found.values()
    if args.filter:
        found = get_filtered(args.filter, found, TUP_VALS)

    orphans = []
    normies = []
    for entry in found.values() if type(found) == dict else found:
        if len(entry) < len(TUP_VALS):
            orphans.append(entry)
        else:
            normies.append(entry)

    if args.orphans:
        result = orphans
    else:
        result = normies

    def _helper(x):
        try:
            return x[TUP_VALS[args.sort.upper()]]
        except IndexError:
            return x[0]

    limit = args.limit if args.limit else len(result)
    jsond = []
    for entry in reversed(sorted(result, key=_helper)):
        if limit == 0:
            break
        elif args.json:
            d = {}
            for enum, val in TUP_VALS.items():
                d[enum] = entry[val]
            jsond.append(d)
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
        limit -= 1

    if jsond:
        print(json.dumps(jsond))
    lfound = None

    if args.stats:
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
    parser.add_argument("-s", "--sort", default="RESDELTA",
                        help="Sort by this enum value: Eg: RESDELTA")
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
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args))
