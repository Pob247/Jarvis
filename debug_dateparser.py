import dateparser
from dateutil import parser

strings = [
    "next Tuesday at 2pm",
    "next Tuesday 2pm",
    "Tuesday at 2pm",
    "tomorrow morning",
    "tomorrow at 9am",
    "in 2 days"
]

print("--- dateparser ---")
for s in strings:
    print(f"'{s}': {dateparser.parse(s, settings={'PREFER_DATES_FROM': 'future'})}")

print("\n--- dateparser (no settings) ---")
for s in strings:
    print(f"'{s}': {dateparser.parse(s)}")
