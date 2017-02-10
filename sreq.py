#!/usr/bin/env python

from __future__ import unicode_literals, division, print_function

import argparse
import sys
import re

try:
    import text_histogram
except ImportError:
    text_histogram = None

DRE = """Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+)\r
Datera Response ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)\r
Datera Response TimeDelta: (?P<delta>\d+\.\d\d\d)s\r
Datera Response URL: (?P<url>.*?)\r
Datera Response Payload: (?P<payload>.*?)\r
Datera Response Object"""


def main(args):
    file = args.logfile
    with open(file) as f:
        idre = re.compile(DRE, re.MULTILINE | re.DOTALL)
        matches = idre.finditer(f.read())
        found = [(match.group("delta"),
                  match.group("url"),
                  match.group("payload"),
                  match.group("rid"))
                 for match in matches]
        for entry in sorted(found, key=lambda x: x[0]):
            print("{}, {}, {}, {}".format(*entry))
        lfound = [float(elem[0]) for elem in found]

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
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args()
    sys.exit(main(args))
