import urllib.request
import os

# Force Python to use system proxy
proxy = urllib.request.ProxyHandler(urllib.request.getproxies())
opener = urllib.request.build_opener(proxy)
urllib.request.install_opener(opener)