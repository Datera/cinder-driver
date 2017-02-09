#!/usr/bin/env python

from __future__ import unicode_literals, division, print_function


import sys
import re

DRE = """Datera Trace ID: (?P<trace>\w+-\w+-\w+-\w+-\w+)\r
Datera Response ID: (?P<rid>\w+-\w+-\w+-\w+-\w+)\r
Datera Response TimeDelta: (?P<delta>\d+\.\d\d\d)s\r
Datera Response URL: (?P<url>.*?)\r
Datera Response Payload: (?P<payload>.*?)\r
Datera Response Object"""


def main(args):
    file = args[0]
    with open(file) as f:
        idre = re.compile(DRE, re.MULTILINE | re.DOTALL)
        matches = idre.finditer(f.read())
        found = ((match.group("delta"),
                  match.group("url"),
                  match.group("payload"),
                  match.group("rid"))
                 for match in matches)
        for entry in sorted(found, key=lambda x: x[0]):
            print("{}, {}, {}, {}".format(*entry))


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(main(args))
