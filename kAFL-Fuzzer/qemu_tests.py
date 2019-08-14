#!/usr/bin/env python
"""
Run the qemu module through its paces to make sure its running zircon test cases properly.

FUCHSIA=/home/newsham/src/fuchsia/
ZKAFL=$FUCHSIA/zircon/system/core/kafl
IP0="0xffffffff00100000-0xffffffff00255000"
PANICADDRS=$ZKAFL/panicAddrs
python ./qemu_tests.py $ZKAFL/snapshot/ram.qcow2 $ZKAFL/snapshot $PANICADDRS 512 $ZKAFL/inputs $ZKAFL/work -ip0 $IP0 -v --Purge -zircon
"""

import time, sys
from common.config import FuzzerConfiguration
from common.qemu import qemu
from common.debug import log_info, enable_logging

def timediff(msg, start) :
    dt = time.time() - start
    print "%s: %f" % (msg, dt)

def test(dat) :
    start = time.time()
    q.set_payload(dat)
    bitmap = q.send_payload()
    timediff("run test", start)
    pop = 0
    for ch in bitmap :
        v = ord(ch)
        pop += sum(1 for b in (1,2,4,8,16,32,64,128) if (v&b) == 0)
    print "bitmap:", len(bitmap), pop
    print "crash %d, timeout %d, kasan %d" % (q.crashed, q.timeout, q.kasan)
    print
    sys.stdout.flush()
    time.sleep(0.2)

def testFile(fn) :
    print fn
    dat = file(fn, 'rb').read()
    return test(dat)

# -----------

config = FuzzerConfiguration()
enable_logging()

start = time.time()
q = qemu(0, config)
q.start()
timediff("start qemu", start)

testFile('tests/ok')
testFile('tests/ok')
testFile('tests/panic')
q.soft_reload()
testFile('tests/ok')
testFile('tests/exit')
q.soft_reload()
testFile('tests/timeout')
q.soft_reload()
testFile('tests/ok')

print "done"

q.__del__()

