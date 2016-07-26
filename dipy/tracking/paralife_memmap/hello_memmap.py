#!/usr/bin/python
from __future__ import print_function
import numpy as np
from tempfile import mkdtemp
import os.path as path

#
#DFZ: prepare the memmap file
#
data = np.arange(12, dtype='float32')
data.resize((3,4))
filename = path.join(mkdtemp(), 'newfile.dat')
filename = "/tmp/dfz_memmap.file"
print('filename =', filename)
#DFZ: open a memmap file:
fp = np.memmap(filename, dtype='float32', mode='w+', shape=(3,4), offset = 3*4*4*1)
#DFZ: assign the data
fp[:] = data[:]
#DFZ: somehow deletion of file pointer flushes it to the disk...
del fp

#
#DFZ: load the entire memmap file;
#    So by default the numpy data is stored as a 1-D array
#
newfp = np.memmap(filename, dtype='float32', mode='r')
print('newfp =', newfp)

#
#DFZ: load memmap file by offsets
#
fpo = np.memmap(filename, dtype='float32', mode='r', offset=16)
print('fpo =', fpo)


print("Done")
