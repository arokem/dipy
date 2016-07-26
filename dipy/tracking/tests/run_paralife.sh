#!/bin/bash

export PARALIFE_DEBUG=1
export MYRIA_ENABLE=1
python -W ignore ./test_life.py
unset MYRIA_ENABLE
unset PARALIFE_DEBUG
