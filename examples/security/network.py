import socket

s = socket.socket()
s.settimeout(3)
s.connect(("1.1.1.1", 80))
print("CONNECTED")
