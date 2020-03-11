#!/usr/bin/env bash

CHANNEL_ID=$1


CHANNEL_DIR="checkpoint/${CHANNEL_ID}"
THREADS_DIR="${CHANNEL_DIR}/threads"
PAGE_FILE="${CHANNEL_DIR}/page"

mkdir -p ${THREADS_DIR}

# init page file if not exist
[ -e $PAGE_FILE ] || echo 1 > ${PAGE_FILE}

