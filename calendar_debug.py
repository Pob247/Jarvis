import os.path
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def main():
    print("--- CALENDAR INVESTIGATOR ---")
    
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # If we need to login again, handle it
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    service = build('calendar', 'v3', credentials=creds)

    # Get time now in UTC
    now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    print(f"Time Now (UTC): {now}")
    
    print("\nFetching next 15 events...")
    events_result = service.events().list(
        calendarId='primary', timeMin=now,
        maxResults=15, singleEvents=True,
        orderBy='startTime').execute()
    events = events_result.get('items', [])

    if not events:
        print("No events found.")
        return

    for event in events:
        summary = event.get('summary', 'No Title')
        
        # Check Start Time
        start = event['start'].get('dateTime', event['start'].get('date'))
        
        # Check Availability Status
        # 'transparency' key ONLY exists if the event is set to "Free".
        # If the key is missing, it defaults to "opaque" (BUSY).
        status = event.get('transparency', 'opaque (BUSY)')
        
        print("-" * 40)
        print(f"EVENT:   {summary}")
        print(f"TIME:    {start}")
        print(f"STATUS:  {status.upper()}")
        
        if status == 'opaque (BUSY)':
            print(">> JARVIS SEES THIS AS A BLOCKER.")
        else:
            print(">> Jarvis ignores this (correctly).")

if __name__ == '__main__':
    main()