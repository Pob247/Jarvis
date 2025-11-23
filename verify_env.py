import os
from dotenv import load_dotenv

load_dotenv()

key = os.getenv("GEMINI_API_KEY")
if key and key.startswith("AIza"):
    print("SUCCESS: API Key loaded correctly.")
else:
    print(f"FAILURE: API Key not found or invalid. Value: {key}")
