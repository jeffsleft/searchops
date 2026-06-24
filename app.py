import modal
import os
import json
import time
import re
import logging
import requests
from datetime import datetime
from bs4 import BeautifulSoup

# --- IMAGE & APP CONFIG ---
image = modal.Image.debian_slim().pip_install(
    "google-genai", "google-api-python-client", "google-auth", "beautifulsoup4", "requests"
)
app = modal.App("recruiting-engine-v3-robust", image=image)

# --- GLOBAL SCHEMA (19 HEADERS) ---
HEADERS = [
    "Date Added", "Company", "Job URL", "Status", "Score", "Pros", "Cons", "Greenfield?",
    "Funding Round", "Total Raised", "Headcount Velocity", "Revenue DNA", "Pricing Model",
    "Tech Profile", "Leadership", "Debt Score", "Red Flag", "Competitors", "Strategic Hook"
]

class PipelineMonitor:
    """Enterprise logging to track every row's success/failure in the Modal logs."""
    @staticmethod
    def log_start(row_idx, company):
        print(f"🚀 [Row {row_idx}] Starting analysis for: {company}")
        
    @staticmethod
    def log_success(row_idx, company, phase):
        print(f"✅ [Row {row_idx}] {phase} complete for {company}")

    @staticmethod
    def log_error(row_idx, company, error):
        print(f"🚨 [Row {row_idx}] ERROR at {company}: {error}")

class IntelligenceEngine:
    def __init__(self, client, sheet_service, sheet_id):
        self.client = client
        self.sheet = sheet_service
        self.sheet_id = sheet_id

    def scrape_with_retry(self, url, retries=2):
        for _ in range(retries):
            try:
                res = requests.get(url, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
                res.raise_for_status()
                soup = BeautifulSoup(res.text, 'html.parser')
                for s in soup(["script", "style"]): s.decompose()
                return " ".join(soup.get_text().split())[:10000]
            except Exception:
                time.sleep(2)
        return None

    def call_gemini_fortress(self, prompt, use_search=False):
        """Forces JSON output and handles 429 Resource Exhausted errors."""
        config = {'response_mime_type': 'application/json'}
        if use_search:
            config['tools'] = [{'google_search': {}}]

        # Exponential Backoff Logic
        for attempt in range(3):
            try:
                response = self.client.models.generate_content(
                    model="gemini-3-flash-preview",
                    contents=[prompt],
                    config=config
                )
                return json.loads(response.text)
            except Exception as e:
                if "429" in str(e):
                    wait = 120 * (attempt + 1)
                    print(f"⚠️ Quota Hit. Backing off for {wait}s...")
                    time.sleep(wait)
                else:
                    raise e
        return None

# --- MAIN WATCHDOG ---

@app.function(
    secrets=[modal.Secret.from_name("recruiting-secrets"), modal.Secret.from_name("google-token-file")],
    timeout=3600
)
def watchdog():
    from google import genai
    from googleapiclient.discovery import build
    from google.oauth2.credentials import Credentials

    # 1. AUTH & INIT
    token_json = os.environ["TOKEN_JSON_CONTENT"]
    creds = Credentials.from_authorized_user_info(json.loads(token_json))
    service = build('sheets', 'v4', credentials=creds)
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    genai_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    
    engine = IntelligenceEngine(genai_client, service, sheet_id)

    # 2. READ DATA
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="'To Evaluate'!A2:S100"
    ).execute()
    rows = result.get('values', [])
    
    for i, row in enumerate(rows):
        # Normalize row to 19 columns
        p = row + [""] * (19 - len(row))
        url, company = p[2], p[1]
        row_num = i + 2

        if not url or not url.startswith("http"): continue
        
        # SKIP LOGIC: If 'Funding Round' (Col I / Index 8) is filled, we don't re-run.
        if p[8].strip(): continue

        PipelineMonitor.log_start(row_num, company or "Unknown Company")

        try:
            # PHASE 1: JD ANALYSIS (If company or score is missing)
            if not company or not p[4]:
                jd_text = engine.scrape_with_retry(url)
                if jd_text:
                    prompt = f"Analyze this JD: {jd_text}. Return JSON: {{'company': str, 'score': float, 'pros': str, 'cons': str, 'greenfield': 'Yes/No'}}"
                    eval_data = engine.call_gemini_fortress(prompt)
                    if eval_data:
                        company = eval_data.get('company', company)
                        update_v = [[
                            datetime.now().strftime("%Y-%m-%d"), company, url, "JD_SCORED",
                            eval_data.get('score'), eval_data.get('pros'), eval_data.get('cons'), eval_data.get('greenfield')
                        ]]
                        service.spreadsheets().values().update(
                            spreadsheetId=sheet_id, range=f"'To Evaluate'!A{row_num}:H{row_num}",
                            valueInputOption='RAW', body={'values': update_v}
                        ).execute()

            # PHASE 2: DEEP GTM RESEARCH (The "Thorough" Part)
            if company:
                print(f"🔍 Performing Search-Grounding for {company}...")
                research_prompt = f"""
                Research {company} as a GTM Operations expert. Use Google Search.
                Provide details for Jeff Beaumont (VP level). 
                Return JSON only: {{
                    "funding": "Latest Round", "raised": "Total Amount", "velocity": "Headcount Trend",
                    "dna": "PLG/SLG", "pricing": "Model", "stack": "Key Tech", 
                    "debt": 1-10, "red_flag": "Risks", "competitors": "Top 3", "hook": "Custom Intro"
                }}
                """
                intel = engine.call_gemini_fortress(research_prompt, use_search=True)
                
                if intel:
                    # Map JSON to Columns I through S
                    def g(k): return str(intel.get(k, "N/A"))
                    research_v = [[
                        g('funding'), g('raised'), g('velocity'), g('dna'), 
                        g('pricing'), g('stack'), "Verified", g('debt'), 
                        g('red_flag'), g('competitors'), g('hook')
                    ]]
                    
                    service.spreadsheets().values().update(
                        spreadsheetId=sheet_id, range=f"'To Evaluate'!I{row_num}:S{row_num}",
                        valueInputOption='RAW', body={'values': research_v}
                    ).execute()
                    
                    # FINAL STATUS
                    service.spreadsheets().values().update(
                        spreadsheetId=sheet_id, range=f"'To Evaluate'!D{row_num}",
                        valueInputOption='RAW', body={'values': [["COMPLETE"]]}
                    ).execute()
                    
                    PipelineMonitor.log_success(row_num, company, "Full Intel")
                
                # Critical: 60s pause between rows to stay under Free Tier Search Tool limits
                time.sleep(60)

        except Exception as e:
            PipelineMonitor.log_error(row_num, company, str(e))
            continue

    return "Processing Complete."