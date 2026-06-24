import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# !!! REPLACE THIS WITH YOUR ACTUAL SHEET ID FROM THE URL !!!
SAMPLE_SPREADSHEET_ID = 'YOUR_GOOGLE_SHEET_ID_HERE'

def main():
    creds = None
    # The file token.json stores the user's access and refresh tokens.
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save the credentials for the next run
        with open('token.json', 'w') as token:
            token.write(creds.to_json())

    try:
        service = build('sheets', 'v4', credentials=creds)

        # 1. Prepare the Tabs (Sheets)
        # We use a batchUpdate to add the tabs we need
        requests = [
            {'addSheet': {'properties': {'title': 'To Evaluate'}}},
            {'addSheet': {'properties': {'title': 'Interview Bank'}}},
            {'addSheet': {'properties': {'title': 'Decision Engine'}}}
        ]
        
        try:
            service.spreadsheets().batchUpdate(
                spreadsheetId=SAMPLE_SPREADSHEET_ID, 
                body={'requests': requests}).execute()
        except Exception as e:
            print("Note: Tabs might already exist, skipping creation...")

        # 2. Set Headers for "To Evaluate"
        headers_eval = [[
            'Date Added', 'Company', 'Job URL', 'Status', 
            'Score', 'Pros', 'Cons', 'Greenfield Flag', 
            'Pricing Model', 'Founder Type'
        ]]
        service.spreadsheets().values().update(
            spreadsheetId=SAMPLE_SPREADSHEET_ID, range="'To Evaluate'!A1",
            valueInputOption='RAW', body={'values': headers_eval}).execute()

        # 3. Set Headers for "Interview Bank"
        headers_bank = [[
            'Company', 'Persona (CFO/CRO)', 'Question', 
            'Priority (H/M/L)', 'Metric Focus', 'Answer Notes'
        ]]
        service.spreadsheets().values().update(
            spreadsheetId=SAMPLE_SPREADSHEET_ID, range="'Interview Bank'!A1",
            valueInputOption='RAW', body={'values': headers_bank}).execute()

        # 4. Set Headers for "Decision Engine"
        headers_decision = [[
            'Company', 'Strategic Debt Notes', 'Divergence Found', 
            'Devil\'s Advocate Response', 'Follow-up Draft'
        ]]
        service.spreadsheets().values().update(
            spreadsheetId=SAMPLE_SPREADSHEET_ID, range="'Decision Engine'!A1",
            valueInputOption='RAW', body={'values': headers_decision}).execute()

        print("✅ Spreadsheet structure built successfully!")
        print("Go check your Google Sheet now.")

    except Exception as err:
        print(f"An error occurred: {err}")

if __name__ == '__main__':
    main()