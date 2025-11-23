import os.path
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

# --- CONFIGURATION ---
GOOGLES_API_KEY = "AIzaSyC6J83-dF45qh0fpRgZxe-JDaXwKbAtDiE"

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
            return False, summary
    return True, None

def infer_duration(email_text):
    text = email_text.lower()
    if "quick chat" in text: return 15
    if "deep dive" in text: return 60
    if "lunch" in text: return 60
    return 30

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

def get_all_future_events(service):
    now = datetime.datetime.now(datetime.timezone.utc)
    events_result = service.events().list(
        calendarId='primary', timeMin=now.isoformat().replace("+00:00", "Z"),
        singleEvents=True, orderBy='startTime').execute()
    events = events_result.get('items', [])
    
    future_events = {}
    for event in events:
        # Only track events created by Jarvis (or relevant ones) if possible, 
        # but for now track all to be safe, or maybe filter by some criteria?
        # The prompt implies "if an event is deleted", so we track everything.
        # We need the attendee email to send cancellation.
        
        # Attempt to find a valid attendee email (not self)
        attendee_email = None
        if 'attendees' in event:
            for attendee in event['attendees']:
                if not attendee.get('self', False):
                    attendee_email = attendee.get('email')
                    break # Just take the first non-self attendee for now
        
        if attendee_email:
            future_events[event['id']] = {
                'summary': event.get('summary', 'Meeting'),
                'start': event['start'].get('dateTime', event['start'].get('date')),
                'attendee_email': attendee_email
            }
    return future_events

def check_for_cancellations(calendar_service, gmail_service):
    state_file = "calendar_state.json"
    current_events = get_all_future_events(calendar_service)
    
    if not os.path.exists(state_file):
        # First run, just save state
        with open(state_file, 'w') as f:
            json.dump(current_events, f)
        return

    try:
        with open(state_file, 'r') as f:
            previous_events = json.load(f)
    except:
        previous_events = {}

    # Check for deletions
    for event_id, event_data in previous_events.items():
        if event_id not in current_events:
            # Event was in previous state but not in current -> DELETED
            # Check if it was in the past (we don't care about past events expiring)
            try:
                event_start = parser.parse(event_data['start'])
                if event_start > datetime.datetime.now(datetime.timezone.utc):
                    print(f" [CANCELLATION DETECTED] {event_data['summary']} with {event_data['attendee_email']}")
                    
                    # Send cancellation email
                    subject = f"Cancellation: {event_data['summary']}"
                    body = f"Hi,\n\nThe event '{event_data['summary']}' scheduled for {event_data['start']} has been cancelled.\n\nBest,\nJarvis"
                    send_email(gmail_service, 'me', event_data['attendee_email'], subject, body)
            except Exception as e:
                print(f"Error checking cancellation for {event_id}: {e}")

    # Update state
    with open(state_file, 'w') as f:
        json.dump(current_events, f)

def recall_memories(queries):
    if not memory_collection: return "No memory available."
    results = memory_collection.query(query_texts=queries, n_results=2)
    combined_memories = []
    if results['documents']:
        for doc_list in results['documents']:
    CONFLICT REASON: {conflict_reason if conflict_reason else 'None'}
    TIME VERIFIED: {parsed_time_dt.isoformat() if parsed_time_dt else 'N/A'} (Duration: {duration_minutes}m)
    
    INSTRUCTIONS:
    1. IF AVAILABLE and TIME IS VERIFIED: Accept.
       - OUTPUT: 'BOOK: {parsed_time_dt.isoformat() if parsed_time_dt else 'N/A'} || DURATION: {duration_minutes} || SEND: [Reply]'
    2. IF BUSY or TIME IS NOT VERIFIED: Decline politely.
       - IF BUSY: Mention a vague reason based on CONFLICT REASON (e.g., "I have a prior commitment" or "I'm traveling"). DO NOT reveal specific details.
       - Suggest the soonest available time (the next day if today is full).
       - OUTPUT: 'SEND: [Reply]'
    3. IF SPAM/BOT: - OUTPUT: 'DELETE'
       
    EMAIL: "{email_text}"
    """
    return client.models.generate_content(model="gemini-2.5-pro", contents=prompt).text.strip()

def main():
    print("--- JARVIS v10.3 (CANCELLATION CLEANUP) ---")
    print("Press Ctrl+C to stop.\n")
    gmail_service, calendar_service = authenticate_services()
    processed_ids = set()
    
    # Initial state load
    check_for_cancellations(calendar_service, gmail_service)
    
    while True:
        try:
            print(f"[{time.strftime('%H:%M:%S')}] Scanning inbox & calendar...")
            
            # Check for cancellations every loop
            check_for_cancellations(calendar_service, gmail_service)
            
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
                        print(" >> SPAM. Deleting...")
                        gmail_service.users().messages().trash(userId='me', id=message['id']).execute()
                        processed_ids.add(message['id'])

                    elif "BOOK:" in decision:
                        print(" >> ACCEPTED. Rescheduling & Booking...")
                        parts = decision.split("||")
                        book_cmd = parts[0].replace("BOOK:", "").strip()
                        duration_cmd = parts[1].replace("DURATION:", "").strip()
                        
                        try:
                            # The time parsing is now fully robust
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