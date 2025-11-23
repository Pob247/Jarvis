import os
from dotenv import load_dotenv
import datetime
import json
from google import genai

load_dotenv()
GOOGLES_API_KEY = os.getenv("GEMINI_API_KEY")

def extract_meeting_details(email_text):
    client = genai.Client(api_key=GOOGLES_API_KEY)
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    prompt = f"""
    CURRENT TIME: {now_iso}
    
    EXTRACT the following details from the email below in JSON format:
    1. "target_datetime": The calculated ISO 8601 datetime (YYYY-MM-DDTHH:MM:SS) of the meeting based on the current time. If no time is specified, null.
    2. "duration_minutes": The implied or stated duration in minutes (default to 30 if unclear).
    
    EMAIL: "{email_text}"
    
    OUTPUT JSON ONLY: {{ "target_datetime": "...", "duration_minutes": ... }}
    """
    try:
        response = client.models.generate_content(model="gemini-2.5-pro", contents=prompt).text
        cleaned = response.replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned)
    except Exception as e:
        print(f"Extraction Error: {e}")
        return {"target_datetime": None, "duration_minutes": 30}

def test_extraction(text):
    print(f"\nTesting: '{text}'")
    details = extract_meeting_details(text)
    print(f"Extracted: {details}")

if __name__ == "__main__":
    print(f"Current Time: {datetime.datetime.now(datetime.timezone.utc)}")
    test_extraction("Can we meet next Tuesday at 2pm?")
    test_extraction("Are you free tomorrow morning for a quick chat?")
    test_extraction("Let's do lunch next Friday.")
