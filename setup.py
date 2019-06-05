#!/usr/bin/env python

import io
import os
import re

from setuptools import setup

DISCSI_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'src', 'datera', 'datera_iscsi.py')

README_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'README.rst')

VRE = re.compile("""VERSION = ['"](.*)['"]""")


def get_version():
    with io.open(DISCSI_FILE) as f:
        return VRE.search(f.read()).group(1)


def get_readme():
    with io.open(README_FILE) as f:
        return f.read()


version = get_version()

# The datera_cinder folder under src is a symlink that must be created
# before running `python setup.py sdist bdist_wheel`.  You can create it
# by running `cd src && ln -s datera datera_cinder`.
# This was the least intrusive hack required to rename the package from
# `datera` to `datera_cinder` for future compatibility with any python
# packages that might need the `datera` name more than this one.

# Don't use `find_packages` from setuptools, because it will find the
# `datera` packages as well as the `datera_cinder` package.  We just want
# the `datera_cinder` package to be built

setup(
    name='datera-cinder',
    version=version,
    description='Datera OpenStack Cinder Driver',
    long_description=get_readme(),
    author='Datera Ecosystem Team',
    author_email='support@datera.io',
    packages=['datera_cinder'],
    package_dir={'': 'src'},
    install_requires=[
        'dfs_sdk',
    ],
    url='https://github.com/Datera/cinder-driver/',
    download_url='https://github.com/Datera/cinder-driver/tarball/v{}'.format(
        version),
    classifiers=[
        "Programming Language :: Python :: 2",
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
    ],
)
