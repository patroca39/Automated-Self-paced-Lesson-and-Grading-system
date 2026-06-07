import os
import time
import requests
import json
import io
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image
from pydantic import BaseModel
from google import genai
from google.genai import types

# --- CONFIGURATION ---
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
BASEROW_KEY = os.environ.get("BASEROW_API_KEY")
SPREADSHEET_NAME = "Business_Math_Master_Gradebook"
WORKSHEET_NAME = "Skill_Analytics"

gen_client = genai.Client(api_key=GEMINI_KEY)

class GradingSchema(BaseModel):
    score: int
    lesson: str

def get_google_services():
    creds_dict = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds), build('drive', 'v3', credentials=creds)

def get_config_files():
    with open("curriculum_guide.json", "r") as f: curr = json.load(f)
    with open("dll_template.json", "r") as f: tmpl = json.load(f)
    return curr["ABM_BM11"], tmpl["dll_template"]

def get_prompt(competency, strand, score, curr, tmpl):
    # Logic: If mastered, provide a challenge. If not, provide DLL-structured remediation.
    if score >= curr.get("mastery_threshold", 75):
        return f"Student mastered {curr['topic']}. Provide a short 'Challenge Problem' aligned with: {curr['performance_standard']}."
    
    return f"""
    You are an expert teacher at Sagkahan National High School. 
    Create a remediation lesson for {curr['topic']} using this official DepEd DLL format:
    
    1. OBJECTIVES: {tmpl['I_OBJECTIVES']} (Target: {curr['learning_competency']})
    2. PROCEDURES (Mastery): {tmpl['IV_PROCEDURES']['Mastery']}
    3. EVALUATION: {tmpl['IV_PROCEDURES']['Evaluation']}
    
    Student Strand: {strand}.
    Return as JSON: {{"score": integer, "lesson": "string"}}
    """

def main():
    sheet_client, drive_service = get_google_services()
    curr_data, dll_tmpl = get_config_files()
    sheet = sheet_client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    headers_row = sheet.row_values(1)

    for index, row in enumerate(sheet.get_all_records(), start=2):
        if str(row.get("Remediation_Status", "")).strip() != "Pending": continue

        comp_code = row.get("Topic_Focus")
        curr = curr_data.get(comp_code)
        file_id = str(row.get("Log_ID", "")).strip()
        
        if not curr or not file_id: continue

        # Download Image
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        MediaIoBaseDownload(fh, request).next_chunk()
        fh.seek(0)
        img = Image.open(fh)

        # AI Grading & Remediation
        for attempt in range(5):
            try:
                prompt = get_prompt(comp_code, row.get("Strand_Focus"), 0, curr, dll_tmpl)
                res = gen_client.models.generate_content(model='gemini-2.0-flash', contents=[prompt, img], 
                       config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=GradingSchema))
                data = json.loads(res.text)
                
                # Write back to sheet
                sheet.update_cell(index, headers_row.index("Score") + 1, data['score'])
                sheet.update_cell(index, headers_row.index("Remediation") + 1, data['lesson'])
                sheet.update_cell(index, headers_row.index("Remediation_Status") + 1, "Mastered" if data['score'] >= 75 else "Needs Review")
                break
            except Exception as e:
                time.sleep((attempt + 1) * 60)
        
        time.sleep(45)

if __name__ == '__main__':
    main()
