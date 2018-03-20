from __future__ import unicode_literals, division, print_function

import arrow


def timestamp_filter_before(x, y):
    tx, ty = arrow.get(x), arrow.get(y)
    return tx <= ty


def timestamp_filter_after(x, y):
    tx, ty = arrow.get(x), arrow.get(y)
    return tx >= ty


OPERATORS_FUNC = {"=": lambda x, y: str(x) == str(y),
                  ">": lambda x, y: float(x) > float(y),
                  "<": lambda x, y: float(x) < float(y),
                  ">=": lambda x, y: float(x) >= float(y),
                  "<=": lambda x, y: float(x) <= float(y),
                  "##": lambda x, y: str(y) in str(x),
                  "@@": timestamp_filter_after,
                  "**": timestamp_filter_before}

OPERATORS = {"=": "X equals Y",
             ">": "X greater than Y",
             "<": "X less than Y",
             ">=": "X greather than or equals Y",
             "<=": "X less than or equals Y",
             "##": "X contains Y",
             "@@": "Timestamp X >= Timestamp Y",
             "**": "Timestamp X <= Timestamp Y"}
