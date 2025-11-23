import google.genai as genai
import re
import os

# Hardcoded key from jarvis.py for testing
GOOGLES_API_KEY = os.getenv("GEMINI_API_KEY")

def test_extraction(email_text):
    client = genai.Client(api_key=GOOGLES_API_KEY)
    
    # UPDATED PROMPT FROM jarvis.py
    time_phrase_prompt = f"""
    You are an expert time extractor.
    
    1. EXTRACT FULL TIME REQUEST: Find the complete date and time request (e.g., '3pm next Tuesday', 'dinner tonight').
    2. IGNORE NON-TIME NUMBERS: Do NOT extract phone numbers, prices, quantities, or room numbers as times.
    3. IF NO CLEAR TIME REQUEST: Output 'N/A'.
    
    OUTPUT FORMAT (MUST BE EXACT):
    TIME_REQUEST: [The complete date/time phrase OR 'N/A']
    
    EMAIL: "{email_text}"
    """
    
    try:
        raw_extraction = client.models.generate_content(model="gemini-2.5-pro", contents=time_phrase_prompt).text.strip()
        print(f"Input: '{email_text}'")
        
        match = re.search(r'TIME_REQUEST:\s*(.*?)\s*$', raw_extraction, re.DOTALL)
        if match:
            extracted = match.group(1).strip()
            print(f"Extracted: '{extracted}'")
        else:
            print(f"Raw Output (No Match): {raw_extraction}")
        print("-" * 20)
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_cases = [
        "Let's meet at 3pm tomorrow.",
        "I have 3 questions for you.",
        "Can you call me at 555-1234?",
        "The budget is 2000 dollars.",
        "See you on Monday at 10.",
        "I'll be there in 5 minutes.",
        "Lets do lunch.",
        "Report 2024 is due.",
        "Room 404 is available.",
        "I need 2 copies by 5pm."
    ]
    
    print("Testing IMPROVED Time Extraction Logic...\n")
    for case in test_cases:
        test_extraction(case)
