#!/bin/bash

BRANCH=$1

function _helper () {
    git checkout ${BRANCH}
    git tag ${BRANCH}
    git checkout master
    git branch -D ${BRANCH}
    git push --tags
    git push origin :refs/heads/${BRANCH}
}

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 branch-name"
    exit
fi

while true; do
    read -p "Are you sure you want to convert branch '${BRANCH}' to a tag? " yn
    case $yn in
        [Yy]* ) _helper ; break;;
        [Nn]* ) exit;;
        * ) echo "Please answer yes or no.";;
    esac
done

