# passenger_wsgi.py
import sys, os

# If your app lives in a subfolder, add it to sys.path:
APP_DIR = os.path.dirname(__file__)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from app_main import app as application  # expose WSGI app as 'application'
