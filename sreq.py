#!/usr/bin/env python

from __future__ import unicode_literals, division, print_function

import argparse
import sys
import re

try:
    import text_histogram
except ImportError:
    text_histogram = None
DREQ = re.compile("""^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*?\r
Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+)\r
Datera Request ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)\r
Datera Request URL: (?P<url>.*?)\r
Datera Request Method: (?P<method>.*?)\r
Datera Request Payload: (?P<payload>.*?)\r
Datera Request Headers: (?P<headers>.*?\})""", re.MULTILINE | re.DOTALL)

DRES = re.compile("""^(?P<time>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d.\d{3}).*?\r
Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+)\r
Datera Response ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)\r
Datera Response TimeDelta: (?P<delta>\d+\.\d\d\d)s\r
Datera Response URL: (?P<url>.*?)\r
Datera Response Payload: (?P<payload>.*?)\r
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


def filter_func(found, loc, val, check_contents):
    if check_contents:
        return filter(lambda x: len(x) >= loc and val in x[loc], found)
    else:
        return filter(lambda x: len(x) >= loc and x[loc] == val, found)


def main(args):
    if args.print_enums:
        for elem in sorted(TUP_VALS.keys()):
            print(str(elem))
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

    if args.filter:
        k, v = args.filter.split("=")
        k = k.strip().upper()
        found = filter_func(found.values(), TUP_VALS[k.upper()], v,
                            args.check_contents)

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
        else:
            print()
            print(*entry)
            print()

    lfound = [float(elem[TUP_VALS["RESDELTA"]]) for elem in result]

    if args.stats:
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
    parser.add_argument("-f", "--filter", default=None,
                        help="Filter by this enum value: Eg: REQID=XXXXXXX")
    parser.add_argument("-c", "--check-contents", action='store_true',
                        help="Check enum contents using value in '--filter'. "
                             "Eg. Contents=={'uuid': 'abcd-xxx-xxx'}, a '--fil"
                             "ter' value of 'abcd' would match with this flag,"
                             " may be MUCH slower")
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
