import os
import time

for i in range(500):
    try:
        if os.fork() == 0:
            time.sleep(3)
            os._exit(0)
    except OSError:
        print("fork blocked after", i)
        break
else:
    print("FORKED ALL 500")
