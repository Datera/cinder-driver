#!/usr/bin/env python
"""
SREQ -- The Request Sorter

Usage:

    Basic
    $ ./sreq.py /your/cinder-volume/log/location.log

    Pretty print the results
    $ ./sreq.py /your/cinder-volume/log/location.log --pretty

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
"""
from __future__ import unicode_literals, division, print_function

import argparse
import sys
import re

try:
    import text_histogram
except ImportError:
    text_histogram = None
DREQ = re.compile("""^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*?
Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+)
Datera Request ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)
Datera Request URL: (?P<url>.*?)
Datera Request Method: (?P<method>.*?)
Datera Request Payload: (?P<payload>.*?)
Datera Request Headers: (?P<headers>.*?\})""", re.MULTILINE | re.DOTALL)

DRES = re.compile("""^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*?
Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+)
Datera Response ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)
Datera Response TimeDelta: (?P<delta>\d+\.\d\d\d)s
Datera Response URL: (?P<url>.*?)
Datera Response Payload: (?P<payload>.*?)
Datera Response Object""", re.MULTILINE | re.DOTALL)

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


def main(args):
    if args.print_enums:
        for elem in sorted(TUP_VALS.keys()):
            print(str("{:>10}: {}".format(elem, TUP_DESCRIPTIONS[elem])))
        sys.exit(0)
    if args.print_operators:
        for k, v in sorted(OPERATORS.items()):
            print(str("{:>10}: {}".format(k, v)))
        sys.exit(0)
    file = args.logfile
    with open(file) as f:
        data = f.read()
        req_matches = DREQ.finditer(data)
        res_matches = DRES.finditer(data)
    found = {match.group("rid"): [match.group("time"),
                                  match.group("trace"),
                                  match.group("rid"),
                                  match.group("url"),
                                  match.group("method"),
                                  match.group("payload"),
                                  match.group("headers")]
             for match in req_matches}
    for match in res_matches:
        found[match.group("rid")].extend([match.group("time"),
                                          match.group("trace"),
                                          match.group("rid"),
                                          match.group("delta"),
                                          match.group("url"),
                                          match.group("payload")])

    found = found.values()
    for f in args.filter:
        k, v, operator = None, None, None
        for operator in OPERATORS.keys():
            sp = f.split(operator, 1)
            if len(sp) == 2:
                k, v = sp
                break
        if not k:
            raise ValueError("No valid operator detected in {}".format(
                args.filter))
        k = k.strip().upper()
        found = filter_func(found,
                            TUP_VALS[k.upper()],
                            v,
                            OPERATORS_FUNC[operator])

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

    for entry in sorted(result, key=_helper):
        if args.pretty:
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

    lfound = None

    if args.stats:
        lfound = [float(elem[TUP_VALS["RESDELTA"]]) for elem in result]
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
    parser = argparse.ArgumentParser()
    parser.add_argument("logfile")
    parser.add_argument("--print-enums", action="store_true",
                        help="Print available sorting/filtering enums")
    parser.add_argument("--print-operators", action="store_true",
                        help="Print available filtering operators")
    parser.add_argument("-q", "--quiet", action='store_true',
                        help="Don't print anything except explicit options")
    parser.add_argument("-f", "--filter", default=None, action='append',
                        help="Filter by this enum value and operator: "
                        "'REQPAYLOAD##someid'.  MAKE SURE TO QUOTE ARGUMENT")
    parser.add_argument("-s", "--sort", default="RESDELTA",
                        help="Sort by this enum value: Eg: RESDELTA")
    parser.add_argument("-p", "--pretty", action="store_true",
                        help="Pretty print results")
    parser.add_argument("-o", "--orphans", action="store_true",
                        help="Display only orphan requests, ie. requests that "
                             "did not recieve any response")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args))
