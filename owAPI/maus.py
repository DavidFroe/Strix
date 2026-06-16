import pyautogui
import time

print("Bewege die Maus. STRG+C zum Beenden.\n")
try:
    while True:
        x, y = pyautogui.position()
        print(f"X={x}  Y={y}")
        time.sleep(0.2)
except KeyboardInterrupt:
    print("\nFertig.")
