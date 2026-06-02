import sys
print(f"Python: {sys.version}")
try:
    import PyQt6
    print("PyQt6: ✅")
except ImportError:
    print("PyQt6: ❌")

try:
    import requests
    print("requests: ✅")
except ImportError:
    print("requests: ❌")
