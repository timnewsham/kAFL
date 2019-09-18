#!/usr/bin/env python
"""
Run test cases through qemu and gather serout

FUCHSIA=/home/newsham/src/fuchsia/
ZKAFL=$FUCHSIA/zircon/system/core/kafl
IP0="0xffffffff00100000-0xffffffff00255000"
PANICADDRS=$ZKAFL/panicAddrs
python ./qemu_tests.py $ZKAFL/snapshot/ram.qcow2 $ZKAFL/snapshot $PANICADDRS 512 $ZKAFL/inputs $ZKAFL/work -ip0 $IP0 -v --Purge -zircon -- tests/*
"""

import time, sys
from common.config import FuzzerConfiguration
from common.qemu import qemu
from common.debug import log_info, enable_logging

def test(dat) :
    sys.stdout.flush()
    q.soft_reload()
    q.set_payload(dat)
    bitmap = q.send_payload(timeout_detection=False)
    pop = 0
    for ch in bitmap :
        v = ord(ch)
        pop += sum(1 for b in (1,2,4,8,16,32,64,128) if (v&b) == 0)
    print "bitmap:", len(bitmap), pop
    print "crash %d, timeout %d, kasan %d" % (q.crashed, q.timeout, q.kasan)
    print
    sys.stdout.flush()

def testFile(fn) :
    print 'Input from %s at time %f' % (fn, time.time())
    dat = file(fn, 'rb').read()
    return test(dat)

# -----------

fns = []
if '--' in sys.argv :
    idx = sys.argv.index('--')
    fns = sys.argv[idx+1:]
    sys.argv = sys.argv[:idx]
 
config = FuzzerConfiguration()
enable_logging()

start = time.time()
q = qemu(0, config)
q.start(serout=sys.stdout)

for fn in fns :
    testFile(fn)

print "done"
q.__del__()

