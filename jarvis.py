# Jarvis v15.1 - Complete Intelligence
import os
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
import json
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()
GOOGLES_API_KEY = os.getenv("GEMINI_API_KEY")

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/calendar'
]

# IGNORE LIST (Don't reply to these people)
BOT_EMAILS = ['calendar-notification@google.com', 'no-reply@google.com', 'mailer-daemon@googlemail.com']
STATE_FILE = "calendar_state.json"

print(">> Connecting to Long-Term Memory...")
try:
    chroma_client = chromadb.PersistentClient(path="jarvis_memory")
    memory_collection = chroma_client.get_or_create_collection(name="user_preferences")
except Exception as e:
    print(f"Warning: Memory Error ({e}). Continuing without memory for now.")
    memory_collection = None

# --- AUTH & SERVICES ---

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

# --- CALENDAR INTELLIGENCE ---

def get_busy_slots(calendar_service):
    now = datetime.datetime.now(datetime.timezone.utc)
    events_result = calendar_service.events().list(
        calendarId='primary', timeMin=now.isoformat().replace("+00:00", "Z"),
        maxResults=30, singleEvents=True, orderBy='startTime').execute()
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
    if requested_dt.tzinfo is None:
        requested_dt = requested_dt.replace(tzinfo=datetime.timezone.utc)
    
    req_end = requested_dt + datetime.timedelta(minutes=30) # Default check duration
    
    for start, end, summary in busy_slots:
        if start.tzinfo is None: start = start.replace(tzinfo=datetime.timezone.utc)
        if end.tzinfo is None: end = end.replace(tzinfo=datetime.timezone.utc)
        
        if start < req_end and end > requested_dt:
            return False, f"Conflict with '{summary}'"
            
    return True, None

def find_alternative_slots(meeting_type, duration_minutes, busy_slots, limit=3):
    """Finds up to [limit] alternative slots based on meeting type."""
    windows = {
        "breakfast": (8, 11.75),
        "lunch": (12, 14.5),
        "dinner": (17, 21),
        "general": (9, 17)
    }
    
    start_hour, end_hour = windows.get(meeting_type, windows["general"])
    
    # Helper to handle float hours
    def get_time_parts(float_hour):
        h = int(float_hour)
        m = int((float_hour - h) * 60)
        return h, m

    s_h, s_m = get_time_parts(start_hour)
    e_h, e_m = get_time_parts(end_hour)
    
    now = datetime.datetime.now(datetime.timezone.utc)
    start_day = now + datetime.timedelta(days=1)
    
    found_slots = []
    
    for day_offset in range(5): # Look ahead 5 days
        current_day = start_day + datetime.timedelta(days=day_offset)
        
        window_start = current_day.replace(hour=s_h, minute=s_m, second=0, microsecond=0)
        window_end = current_day.replace(hour=e_h, minute=e_m, second=0, microsecond=0)
        
        current_slot = window_start
        while current_slot + datetime.timedelta(minutes=duration_minutes) <= window_end:
            is_free, _ = is_time_free(current_slot, busy_slots)
            if is_free:
                found_slots.append(current_slot)
                if len(found_slots) >= limit:
                    return found_slots
            current_slot += datetime.timedelta(minutes=30)
            
    return found_slots

# --- ACTIONS (CREATE, DELETE, SEND) ---

def create_calendar_event(service, start_dt, summary, duration_minutes=30):
    end_dt = start_dt + datetime.timedelta(minutes=duration_minutes)
    event = {
      'summary': summary,
      'start': {'dateTime': start_dt.isoformat()},
      'end': {'dateTime': end_dt.isoformat()},
    }
    try:
        event = service.events().insert(calendarId='primary', body=event).execute()
        print(f" [CALENDAR] Slot LOCKED: {start_dt.strftime('%H:%M')} (Link: {event.get('htmlLink')})")
        return True
    except Exception as e:
        print(f" [ERROR] Could not book calendar: {e}")
        return False

def delete_calendar_event_by_summary(service, summary_query, sender_email=None):
    """Finds a future event matching the summary/sender and deletes it."""
    try:
        now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        events_result = service.events().list(
            calendarId='primary', timeMin=now, singleEvents=True, orderBy='startTime').execute()
        
        for event in events_result.get('items', []):
            # Fuzzy match: Checks if the query or sender is in the event title/attendees
            event_summary = event.get('summary', '').lower()
            attendees = [a.get('email') for a in event.get('attendees', [])]
            
            match_summary = summary_query.lower() in event_summary
            # Sender check is optional but helps precision
            match_sender = sender_email and (sender_email in attendees or sender_email in event_summary)
            
            if match_summary or match_sender:
                print(f" [DELETE] Found '{event.get('summary')}'. Deleting ID: {event['id']}")
                service.events().delete(calendarId='primary', eventId=event['id'], sendUpdates='all').execute()
                return True
        return False
    except Exception as e:
        print(f" [ERROR] Delete failed: {e}")
        return False

def send_email(service, user_id, recipient, subject, body_text):
    message = MIMEText(body_text)
    message['to'] = recipient
    message['subject'] = subject
    raw_string = base64.urlsafe_b64encode(message.as_bytes()).decode()
    body = {'raw': raw_string}
    try:
        service.users().messages().send(userId=user_id, body=body).execute()
        print(f" [EMAIL] Sent to {recipient}")
        return True
    except: return False

# --- WATCHDOG (MANUAL DELETION MONITOR) ---

def get_future_events_map(calendar_service):
    """Returns a dict of {event_id: {data}} for all future events."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    events_result = calendar_service.events().list(
        calendarId='primary', timeMin=now,
        maxResults=50, singleEvents=True, orderBy='startTime').execute()
    
    events_map = {}
    for event in events_result.get('items', []):
        attendee_email = None
        if 'attendees' in event:
            for att in event['attendees']:
                if not att.get('self', False):
                    attendee_email = att.get('email')
                    break
        
        # Only track events with an external attendee
        if attendee_email:
            events_map[event['id']] = {
                'summary': event.get('summary', 'Meeting'),
                'start': event['start'].get('dateTime'),
                'attendee': attendee_email
            }
    return events_map

def check_calendar_watchdog(calendar_service, gmail_service, busy_slots):
    """Detects manual deletions and emails the victim with options."""
    current_events = get_future_events_map(calendar_service)
    
    if not os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'w') as f:
            json.dump(current_events, f)
        return

    try:
        with open(STATE_FILE, 'r') as f:
            previous_events = json.load(f)
    except: previous_events = {}

    # Compare: Did an ID disappear?
    for event_id, info in previous_events.items():
        if event_id not in current_events:
            print(f" [WATCHDOG] Event '{info['summary']}' removed manually!")
            
            m_type = "general"
            if "lunch" in info['summary'].lower(): m_type = "lunch"
            elif "dinner" in info['summary'].lower(): m_type = "dinner"
            elif "breakfast" in info['summary'].lower(): m_type = "breakfast"
            
            new_slots = find_alternative_slots(m_type, 30, busy_slots, limit=3)
            
            suggestion_text = ""
            if new_slots:
                suggestion_text = "Here are a few alternative times that work for me:\n"
                for slot in new_slots:
                    suggestion_text += f"- {slot.strftime('%A, %B %d at %I:%M %p')}\n"
            else:
                suggestion_text = "I'll need to check my schedule for next week."
            
            subject = f"Rescheduling: {info['summary']}"
            body = f"Hi,\n\nApologies, but I have had to move our meeting ('{info['summary']}') originally scheduled for {info['start']}.\n\n{suggestion_text}\n\nPlease let me know if one of these works for you.\n\nBest,\nJarvis"
            
            send_email(gmail_service, 'me', info['attendee'], subject, body)

    with open(STATE_FILE, 'w') as f:
        json.dump(current_events, f)

# --- AI DECISION CORE ---

def recall_memories(queries):
    if not memory_collection: return "No memory available."
    try:
        results = memory_collection.query(query_texts=queries, n_results=2)
        combined = []
        if results['documents']:
            for doc_list in results['documents']:
                for doc in doc_list:
                    if doc and doc not in combined: combined.append(doc)
        return "\n".join(combined) if combined else "No specific memory found."
    except: return "Memory access failed."

def extract_meeting_intent_json(client, email_text):
    # STRUCTURED INTENT: Handles Create, Reschedule, and Cancel
    extraction_schema = {
        "type": "OBJECT",
        "properties": {
            "intent": {"type": "STRING", "enum": ["create", "reschedule", "cancel", "spam"], "description": "Primary goal of email."},
            "new_time_phrase": {"type": "STRING", "description": "The NEW requested time (e.g. 'tomorrow at 3pm')."},
            "duration_minutes": {"type": "INTEGER"},
            "meeting_type": {"type": "STRING", "enum": ["breakfast", "lunch", "dinner", "general"]}
        },
        "required": ["intent", "new_time_phrase", "duration_minutes", "meeting_type"]
    }
    
    prompt = f"Analyze for scheduling. Current Date: {datetime.datetime.now().strftime('%A %Y-%m-%d')}. Email: '{email_text}'"
    
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash", 
            contents=prompt,
            config={'response_mime_type': 'application/json', 'response_schema': extraction_schema}
        )
        return json.loads(response.text)
    except Exception as e:
        print(f" [AI ERROR] {e}")
        return None

def decide_action(email_text, sender, busy_slots):
    client = genai.Client(api_key=GOOGLES_API_KEY)
    memories = recall_memories([sender, email_text])
    
    data = extract_meeting_intent_json(client, email_text)
    if not data: return "KEEP"
    
    intent = data.get("intent")
    new_time_phrase = data.get("new_time_phrase", "")
    duration = data.get("duration_minutes", 30)
    m_type = data.get("meeting_type", "general")
    
    # 1. SPAM
    if intent == "spam": return "DELETE"

    # 2. CANCELLATION
    if intent == "cancel":
        return f"DELETE_EVENT: Meeting with {sender}"

    # 3. CREATE or RESCHEDULE
    availability_status = "UNKNOWN"
    parsed_time_dt = None
    alt_slots_text = "None available"

    if new_time_phrase:
        try:
            settings = {'TIMEZONE': 'UTC', 'RETURN_AS_TIMEZONE_AWARE': True, 'PREFER_DATES_FROM': 'future'}
            parsed_time_dt = dateparser.parse(new_time_phrase, settings=settings)
            
            if parsed_time_dt:
                is_free, conflict = is_time_free(parsed_time_dt, busy_slots)
                availability_status = "AVAILABLE" if is_free else f"BUSY: {conflict}"
            else:
                availability_status = "Could not parse time"
        except: availability_status = "Date parsing error"

    # If busy or unclear, find alternatives
    if availability_status != "AVAILABLE":
        alts = find_alternative_slots(m_type, duration, busy_slots, limit=3)
        if alts:
            alt_slots_text = ", ".join([dt.strftime("%A, %b %d at %I:%M %p") for dt in alts])

    formatted_time = parsed_time_dt.isoformat() if parsed_time_dt else 'N/A'
    
    prompt = f"""
    You are Jarvis, an executive assistant.
    CONTEXT: User Memory: {memories} 
    INTENT: {intent.upper()}
    REQUESTED TIME STATUS: {availability_status} (Time: {new_time_phrase})
    AVAILABLE ALTERNATIVES: {alt_slots_text}
    
    INSTRUCTIONS:
    1. IF STATUS is 'AVAILABLE' and INTENT is 'create': Output 'BOOK: {formatted_time} || DURATION: {duration} || SEND: [Confirmation email]'
    2. IF STATUS is 'AVAILABLE' and INTENT is 'reschedule': Output 'RESCHEDULE: {formatted_time} || DURATION: {duration} || SEND: [Confirmation of move]'
    3. IF STATUS is 'BUSY' or invalid: Output 'SEND: [Polite decline, mentioning conflict, and listing the AVAILABLE ALTERNATIVES]'
    
    EMAIL CONTENT: "{email_text}"
    """
    
    return client.models.generate_content(model="gemini-2.0-flash", contents=prompt).text.strip()

# --- MAIN LOOP ---

def main():
    print("--- JARVIS v15.1 (COMPLETE INTELLIGENCE) ---")
    print("Press Ctrl+C to stop.\n")
    gmail_service, calendar_service = authenticate_services()
    processed_ids = set()
    
    # Initialize Watchdog
    check_calendar_watchdog(calendar_service, gmail_service, get_busy_slots(calendar_service))
    
    while True:
        try:
            print(f"[{time.strftime('%H:%M:%S')}] Scanning...")
            busy_slots = get_busy_slots(calendar_service)
            
            # 1. WATCHDOG
            check_calendar_watchdog(calendar_service, gmail_service, busy_slots)
            
            # 2. EMAIL CHECK
            results = gmail_service.users().messages().list(userId='me', q='is:unread -in:sent', maxResults=3).execute()
            messages = results.get('messages', [])

            if not messages: print("No new unread emails.")
            else:
                for message in messages:
                    if message['id'] in processed_ids: continue
                    
                    msg = gmail_service.users().messages().get(userId='me', id=message['id']).execute()
                    snippet = msg.get('snippet', '')
                    headers = msg['payload']['headers']
                    sender = next((h['value'] for h in headers if h['name'] == 'From'), "Unknown")
                    
                    # Extract pure email for logic checks
                    clean_sender_email = re.search(r'<([^>]+)>', sender)
                    sender_email = clean_sender_email.group(1) if clean_sender_email else sender

                    # Skip bots
                    if any(bot in sender.lower() for bot in BOT_EMAILS):
                        gmail_service.users().messages().modify(userId='me', id=message['id'], body={'removeLabelIds': ['UNREAD']}).execute()
                        processed_ids.add(message['id']); continue

                    print(f"\nNew Email from: {sender}")
                    decision = decide_action(snippet, sender, busy_slots)
                    print(f"DEBUG ACTION: {decision[:100]}...") 
                    
                    # --- ACTION EXECUTION ---
                    
                    # 1. DELETE / SPAM
                    if decision.startswith("DELETE") and "EVENT" not in decision:
                        print(" >> Deleting Email...")
                        gmail_service.users().messages().trash(userId='me', id=message['id']).execute()

                    # 2. CANCEL MEETING
                    elif "DELETE_EVENT:" in decision:
                        target = decision.split("DELETE_EVENT:")[1].strip()
                        # FIX: Passing sender_email to verify ownership
                        if delete_calendar_event_by_summary(calendar_service, target, sender_email):
                            send_email(gmail_service, 'me', sender_email, "Meeting Cancelled", "I've removed it from the calendar.")
                        else:
                            print(" >> Could not find event to delete.")

                    # 3. BOOK NEW MEETING
                    elif "BOOK:" in decision:
                        print(" >> Booking...")
                        parts = decision.split("||")
                        try:
                            book_dt = parser.parse(parts[0].replace("BOOK:", "").strip())
                            duration = int(parts[1].replace("DURATION:", "").strip())
                            
                            if create_calendar_event(calendar_service, book_dt, f"Meeting with {sender}", duration):
                                if len(parts) > 2: 
                                    reply_body = parts[2].replace("SEND:", "").strip()
                                    send_email(gmail_service, 'me', sender_email, "Confirmed", reply_body)
                        except Exception as e: print(f"Booking Error: {e}")

                    # 4. RESCHEDULE (Delete Old + Book New)
                    elif "RESCHEDULE:" in decision:
                        print(" >> Rescheduling...")
                        parts = decision.split("||")
                        try:
                            new_dt = parser.parse(parts[0].replace("RESCHEDULE:", "").strip())
                            duration = int(parts[1].replace("DURATION:", "").strip())
                            
                            # A. Delete old meeting
                            delete_calendar_event_by_summary(calendar_service, "Meeting with", sender_email)
                            
                            # B. Book new meeting
                            if create_calendar_event(calendar_service, new_dt, f"Meeting with {sender}", duration):
                                if len(parts) > 2: 
                                    reply_body = parts[2].replace("SEND:", "").strip()
                                    send_email(gmail_service, 'me', sender_email, "Rescheduled", reply_body)
                        except Exception as e: print(f"Reschedule Error: {e}")

                    # 5. JUST REPLY (Decline / Info)
                    elif "SEND:" in decision:
                        print(" >> Replying...")
                        reply_text = decision.replace("SEND:", "").strip()
                        send_email(gmail_service, 'me', sender_email, "Re: Meeting", reply_text)
                    
                    # Mark processed
                    gmail_service.users().messages().modify(userId='me', id=message['id'], body={'removeLabelIds': ['UNREAD']}).execute()
                    processed_ids.add(message['id'])
            
            time.sleep(60)
        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Error: {e}. Pausing 5m."); time.sleep(300)

if __name__ == '__main__':
    main()