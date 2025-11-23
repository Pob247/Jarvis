import os
from dotenv import load_dotenv
import time
import datetime
import base64
import chromadb
import re
from dateutil import parser
import dateparser
from email.mime.text import MIMEText
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from google import genai
from google.genai import types
import json

load_dotenv() # Load API Key from .env file

# --- CONFIGURATION ---
GOOGLES_API_KEY = os.getenv("GEMINI_API_KEY")

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/calendar'
]

# IGNORE LIST (Don't reply to these people)
BOT_EMAILS = ['calendar-notification@google.com', 'no-reply@google.com', 'mailer-daemon@googlemail.com']

print(">> Connecting to Long-Term Memory...")
try:
    chroma_client = chromadb.PersistentClient(path="jarvis_memory")
    memory_collection = chroma_client.get_or_create_collection(name="user_preferences")
except Exception as e:
    print(f"Warning: Memory Error ({e}). Continuing without memory for now.")
    memory_collection = None

# --- CORE SCHEDULING LOGIC FUNCTIONS ---

def authenticate_services():
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    return build('gmail', 'v1', credentials=creds), build('calendar', 'v3', credentials=creds)

def get_busy_slots(calendar_service):
    now = datetime.datetime.now(datetime.timezone.utc)
    events_result = calendar_service.events().list(
        calendarId='primary', timeMin=now.isoformat().replace("+00:00", "Z"),
        maxResults=15, singleEvents=True, orderBy='startTime').execute()
    events = events_result.get('items', [])
    busy_times = []
    for event in events:
        if event.get('transparency') == 'transparent': continue
        try:
            start_raw = event['start'].get('dateTime', event['start'].get('date'))
            end_raw = event['end'].get('dateTime', event['end'].get('date'))
            summary = event.get('summary', 'Busy')
            busy_times.append((parser.parse(start_raw), parser.parse(end_raw), summary))
        except: pass
    return busy_times

def is_time_free(requested_dt, busy_slots):
    for start, end, summary in busy_slots:
        if start <= requested_dt < end: 
            return False, summary # Returns False and the reason for the conflict
    return True, None

def infer_meeting_details(email_text):
    """Infers duration and meeting type from text."""
    text = email_text.lower()
    duration = 30
    m_type = "general"
    
    if "quick chat" in text: duration = 15
    elif "deep dive" in text: duration = 60
    elif "lunch" in text: 
        duration = 60
        m_type = "lunch"
    elif "dinner" in text: 
        duration = 60
        m_type = "dinner"
    elif "breakfast" in text:
        duration = 60
        m_type = "breakfast"
        
    return duration, m_type

def find_alternative_slot(meeting_type, duration_minutes, busy_slots):
    # This function calculates the next available time based on meal/type windows.
    windows = {
        "breakfast": (8, 11),
        "lunch": (12, 15),
        "dinner": (18, 21),
        "general": (9, 17)
    }
    
    start_hour, end_hour = windows.get(meeting_type, windows["general"])
    
    now = datetime.datetime.now(datetime.timezone.utc)
    start_day = now + datetime.timedelta(days=1) # Always start searching tomorrow
    
    for day_offset in range(4): 
        current_day = start_day + datetime.timedelta(days=day_offset)
        
        window_start = current_day.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        window_end = current_day.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        
        current_slot = window_start
        while current_slot + datetime.timedelta(minutes=duration_minutes) <= window_end:
            # Check for conflict
            is_busy, _ = is_time_free(current_slot, busy_slots)
            
            if not is_busy:
                return current_slot
            
            current_slot += datetime.timedelta(minutes=30)
            
    return None

def clear_existing_meetings(service, sender_name, target_date_dt):
    day_start = target_date_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = target_date_dt.replace(hour=23, minute=59, second=59, microsecond=0)
    
    events_result = service.events().list(
        calendarId='primary', 
        timeMin=day_start.isoformat(), 
        timeMax=day_end.isoformat(),
        singleEvents=True).execute()
        
    for event in events_result.get('items', []):
        if f"Meeting with {sender_name}" in event.get('summary', ''):
            print(f" [RESCHEDULE] Found old meeting '{event['summary']}'... Deleting.")
            try:
                service.events().delete(calendarId='primary', eventId=event['id']).execute()
            except: pass

def create_calendar_event(service, start_dt, summary, duration_minutes=30):
    end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)
    event = {
      'summary': summary,
      'start': {'dateTime': start_dt.isoformat()},
      'end': {'dateTime': end_dt.isoformat()},
    }
    try:
        event = service.events().insert(calendarId='primary', body=event).execute()
        print(f" [CALENDAR] Slot LOCKED: {start_dt.strftime('%H:%M')} for {duration_minutes}m (Link: {event.get('htmlLink')})")
        return True
    except Exception as e:
        print(f" [ERROR] Could not book calendar: {e}")
        return False

def send_email(service, user_id, recipient, subject, body_text):
    message = MIMEText(body_text)
    message['to'] = recipient
    message['subject'] = f"Re: {subject}"
    raw_string = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body = {'raw': raw_string}
    try:
        service.users().messages().send(userId=user_id, body=body).execute()
        print(f" [EMAIL] Reply Sent to {recipient}")
        return True
    except Exception as e:
        print(f" [ERROR] Failed to send email: {e}")
        return False

def recall_memories(queries):
    if not memory_collection: return "No memory available."
    try:
        results = memory_collection.query(query_texts=queries, n_results=2)
        combined_memories = []
        if results['documents']:
            for doc_list in results['documents']:
                for doc in doc_list:
                    if doc and doc not in combined_memories: combined_memories.append(doc)
        if not combined_memories: return "No specific memory found."
        return "\n".join(combined_memories)
    except:
        return "Memory access failed."

def delete_calendar_event_by_summary(calendar_service, summary):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat() + 'Z'
    try:
        events_result = calendar_service.events().list(
            calendarId='primary', 
            q=summary, 
            timeMin=now, 
            maxResults=1).execute()
        events = events_result.get('items', [])
        
        if events:
            event_id = events[0]['id']
            calendar_service.events().delete(calendarId='primary', eventId=event_id, sendUpdates='all').execute()
            print(f" [CALENDAR] DELETED event '{summary}'. Sent cancellation to attendees.")
            return True
    except Exception as e:
        print(f" [ERROR] Failed to delete calendar event: {e}")
        return False

# --- MAIN DECISION FUNCTION (Integrated Logic) ---

def decide_action(email_text, sender, busy_slots):
    client = genai.Client(api_key=GOOGLES_API_KEY)
    
    # 0. INFER PYTHON DETAILS
    duration_minutes, meeting_type = infer_meeting_details(email_text)
    
    # 1. MEMORY LOOKUP
    memories = recall_memories([sender, email_text, "My name is"])
    
    # 2. TIME/DURATION EXTRACTION (Reliable)
    time_phrase_prompt = f"""
    You are an expert time extractor.
    1. EXTRACT FULL TIME REQUEST: Find the complete date and time request (e.g., '3pm next Tuesday', 'dinner tonight').
    OUTPUT FORMAT (MUST BE EXACT):
    TIME_REQUEST: [The complete date/time phrase]
    
    EMAIL: "{email_text}"
    """
    
    raw_extraction = client.models.generate_content(model="gemini-2.5-pro", contents=time_phrase_prompt).text.strip()
    
    # 3. PYTHON PARSING (Hard Logic)
    time_phrase_match = re.search(r'TIME_REQUEST:\s*(.*?)\s*$', raw_extraction, re.DOTALL)
    
    availability_status = "UNKNOWN"
    parsed_time_dt = None
    conflict_reason = None
    suggested_alternative_slot = None
    
    if time_phrase_match:
        time_phrase = time_phrase_match.group(1).strip()
        
        try:
            settings = {'TIMEZONE': 'UTC', 'RETURN_AS_TIMEZONE_AWARE': True, 'PREFER_DATES_FROM': 'future'}
            
            parsed_time_dt = dateparser.parse(time_phrase, settings=settings, languages=['en'])

            if parsed_time_dt is None: 
                availability_status = "Could not parse specific time."
            else:
                # Core hard logic check
                is_free, conflict_reason = is_time_free(parsed_time_dt, busy_slots)
                
                if is_free:
                    availability_status = "AVAILABLE"
                else:
                    availability_status = f"BUSY: {conflict_reason}"
            
        except Exception as e:
            availability_status = "Could not parse specific time."

    # 4. PROACTIVE RESCHEDULING (Only if busy or unparsable)
    if availability_status != "AVAILABLE" or not parsed_time_dt:
        alt_slot = find_alternative_slot(meeting_type, duration_minutes, busy_slots)
        if alt_slot:
            suggested_alternative_slot = alt_slot.strftime("%A, %B %d at %I:%M %p UTC")

    # 5. DECISION PROMPT
    prompt = f"""
    You are Jarvis.
    
    CONTEXT: Memory: {memories} | LOGIC RESULT: User is {availability_status} at requested time.
    TIME VERIFIED: {parsed_time_dt.isoformat() if parsed_time_dt else 'N/A'} (Duration: {duration_minutes}m)
    SUGGESTED ALTERNATIVE: {suggested_alternative_slot if suggested_alternative_slot else 'None available'}
    
    INSTRUCTIONS:
    1. CHECK FOR CANCEL/DELETE: Does the email contain the word 'cancel' or 'delete' AND a reference to a specific meeting?
       - If YES: Output: 'DELETE_EVENT: [Brief Meeting Summary (e.g., Meeting with David)]'
    2. IF AVAILABLE and TIME IS VERIFIED: Accept.
       - OUTPUT: 'BOOK: {parsed_time_dt.isoformat() if parsed_time_dt else 'N/A'} || DURATION: {duration_minutes} || SEND: [Reply]'
    3. IF BUSY or TIME IS NOT VERIFIED: Decline politely.
       - IF BUSY: Mention the conflict reason and PROPOSE THE ALTERNATIVE SLOT.
       - OUTPUT: 'SEND: [Reply]'
    4. IF SPAM/BOT: - OUTPUT: 'DELETE'
       
    EMAIL: "{email_text}"
    """
    return client.models.generate_content(model="gemini-2.5-pro", contents=prompt).text.strip()

def main():
    print("--- JARVIS v12.0 (PROACTIVE RESCHEDULING) ---")
    print("Press Ctrl+C to stop.\n")
    gmail_service, calendar_service = authenticate_services()
    processed_ids = set()
    
    while True:
        try:
            print(f"[{time.strftime('%H:%M:%S')}] Scanning inbox...")
            results = gmail_service.users().messages().list(userId='me', q='is:unread -in:sent', maxResults=3).execute()
            messages = results.get('messages', [])

            if not messages:
                print("No new unread emails.")
            else:
                busy_slots = get_busy_slots(calendar_service)
                for message in messages:
                    if message['id'] in processed_ids: continue
                    
                    msg = gmail_service.users().messages().get(userId='me', id=message['id']).execute()
                    snippet = msg.get('snippet', '')
                    headers = msg['payload']['headers']
                    sender = next((h['value'] for h in headers if h['name'] == 'From'), "Unknown")
                    sender_email = sender
                    if "<" in sender: sender_email = sender.split("<")[1].replace(">", "")

                    if sender_email in BOT_EMAILS:
                        gmail_service.users().messages().modify(userId='me', id=message['id'], body={'removeLabelIds': ['UNREAD']}).execute()
                        processed_ids.add(message['id'])
                        continue

                    print(f"\nNew Email from: {sender}")
                    # CORRECTED CALL SITE: Calling the primary decision function
                    decision = decide_action(snippet, sender, busy_slots) 
                    print(f"DEBUG: {decision[:60]}...") 
                    
                    if "DELETE" in decision and "SEND" in decision and "BOOK" not in decision:
                        decision = "DELETE"

                    elif decision.startswith("DELETE_EVENT:"):
                        event_summary = decision.replace("DELETE_EVENT:", "").strip()
                        if delete_calendar_event_by_summary(calendar_service, event_summary):
                            send_email(gmail_service, 'me', sender, "Cancellation Confirmed", "The event has been successfully cancelled from your calendar.")
                        
                        gmail_service.users().messages().modify(userId='me', id=message['id'], body={'removeLabelIds': ['UNREAD']}).execute()
                        processed_ids.add(message['id'])

                    elif decision.startswith("DELETE"):
                        print(" >> SPAM. Deleting...")
                        gmail_service.users().messages().trash(userId='me', id=message['id']).execute()
                        processed_ids.add(message['id'])

                    elif "BOOK:" in decision:
                        print(" >> ACCEPTED. Rescheduling & Booking...")
                        parts = decision.split("||")
                        book_cmd = parts[0].replace("BOOK:", "").strip()
                        duration_cmd = parts[1].replace("DURATION:", "").strip()
                        
                        try:
                            book_dt = parser.parse(book_cmd)
                            duration = int(duration_cmd.split()[0])
                            
                            clear_existing_meetings(calendar_service, sender, book_dt)
                            create_calendar_event(calendar_service, book_dt, summary=f"Meeting with {sender}", duration_minutes=duration)
                        except Exception as e:
                            print(f" [ERROR] Booking failed: {e}")
                        
                        if len(parts) > 2:
                            email_body = parts[2].replace("SEND:", "").strip()
                            send_email(gmail_service, 'me', sender, "Meeting", email_body)
                        
                        gmail_service.users().messages().modify(userId='me', id=message['id'], body={'removeLabelIds': ['UNREAD']}).execute()
                        processed_ids.add(message['id'])

                    elif decision.startswith("SEND:"):
                        print(" >> REPLYING...")
                        reply_text = decision.replace("SEND:", "").strip()
                        send_email(gmail_service, 'me', sender, "Reply", reply_text)
                        gmail_service.users().messages().modify(userId='me', id=message['id'], body={'removeLabelIds': ['UNREAD']}).execute()
                        processed_ids.add(message['id'])
                    else:
                        print(" >> KEEP.")
                        processed_ids.add(message['id'])
            
            print("Sleeping for 60s...")
            time.sleep(60) # Normal sleep for scanning
        except KeyboardInterrupt:
            print("\nStopping Jarvis...")
            break
        except Exception as e:
            print(f"Error: {e}")
            print("Quota exhausted or major error detected. Pausing for 5 minutes.")
            time.sleep(300) # Long sleep for quota reset

if __name__ == '__main__':
    main()