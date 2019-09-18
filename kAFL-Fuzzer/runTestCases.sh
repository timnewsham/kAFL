#!/bin/sh
# run kafl fuzzer on zircon
# requires a fucshia build with kafl patches and configured for kafl build
#    https://fuchsia-review.googlesource.com/c/fuchsia/+/309954 

rm -f serout.txt debug.log exception.log

FUCHSIA=/home/newsham/src/fuchsia/
ZKAFL=$FUCHSIA/zircon/system/core/kafl

# I got this from the boot messages during kernel startup
IP0="0xffffffff00100000-0xffffffff00255000"

# we pass the panic addrs in with the program buf
PANICADDRS=$ZKAFL/panicAddrs

python ./runTestCases.py $ZKAFL/snapshot/ram.qcow2 $ZKAFL/snapshot $PANICADDRS 512 $ZKAFL/inputs $ZKAFL/work -ip0 $IP0 -v --Purge -zircon -- "$@"

