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
BASEROW_URL = "https://api.baserow.io/api/database/rows/table/1012002/"

gen_client = genai.Client(api_key=GEMINI_KEY)

class GradingSchema(BaseModel):
    score: int
    lesson: str

SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_google_services():
    creds_dict = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, SCOPES)
    sheet_client = gspread.authorize(creds)
    drive_service = build('drive', 'v3', credentials=creds)
    return sheet_client, drive_service

def download_image(drive_service, file_id):
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while done is False:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return Image.open(fh)

def get_prompt(competency, strand, score=None):
    if score is not None and score >= 75:
        return f"The student mastered {competency}. Generate 1 advanced 'Challenge Problem' to push their skills further in {strand}."
    
    return f"""
    Analyze the math in this image for competency {competency}. 
    1. GRADE: Provide a score (0-100) and a brief critique.
    2. TUTOR: Generate a 'Self-Paced Guide' for {strand}:
       ### 1. YOUR CHALLENGE: (A relatable scenario for the student)
       ### 2. EXPLORE: (Ask 2 probing questions to help them find their own error)
       ### 3. THE FORMULA: (Define P, R, T, and I clearly)
       ### 4. PRACTICE: (Provide 1 new problem to solve)
    Return as JSON: {{"score": integer, "lesson": "string"}}
    """

def main():
    try:
        sheet_client, drive_service = get_google_services()
        sheet = sheet_client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    except Exception as e:
        print(f"Failed to connect to Google Services: {e}")
        return

    all_rows = sheet.get_all_records()
    print(f"Total rows found in sheet: {len(all_rows)}")
    headers_row = sheet.row_values(1)

    for index, row in enumerate(all_rows, start=2):
        status = str(row.get("Remediation_Status", "")).strip()
        if status != "Pending": continue

        student_id = row.get("Student_ID", "unknown")
        file_id = str(row.get("Log_ID", "")).strip()
        competency = row.get("Topic_Focus", "ABM_BM11BS-Ig-1")
        strand = str(row.get("Strand_Focus", "BEC")).strip() or "BEC"

        if not file_id:
            try: sheet.update_cell(index, headers_row.index("Remediation_Status") + 1, "Missing ID")
            except: pass
            continue

        try:
            student_image = download_image(drive_service, file_id)
        except Exception:
            sheet.update_cell(index, headers_row.index("Remediation_Status") + 1, "Image Error")
            continue

        # --- HARDENED RETRY LOGIC ---
        gemini_success = False
        score, lesson = 0, "No feedback generated."
        
        for attempt in range(5):
            try:
                # We use the dynamic prompt function
                prompt = get_prompt(competency, strand)
                response = gen_client.models.generate_content(
                    model='gemini-2.0-flash',
                    contents=[prompt, student_image],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=GradingSchema,
                        temperature=0.2
                    )
                )
                gemini_data = json.loads(response.text)
                score = int(gemini_data.get("score", 0))
                lesson = gemini_data.get("lesson", "").strip()
                gemini_success = True
                break
            except Exception as e:
                wait_time = (attempt + 1) * 60
                print(f"🛑 API busy. Waiting {wait_time}s (Attempt {attempt+1}/5). Error: {e}")
                time.sleep(wait_time)

        # Write results
        if gemini_success:
            sheet.update_cell(index, headers_row.index("Score") + 1, score)
            sheet.update_cell(index, headers_row.index("Remediation") + 1, lesson)
            sheet.update_cell(index, headers_row.index("Remediation_Status") + 1, "Mastered" if score >= 75 else "Needs Review")
        else:
            sheet.update_cell(index, headers_row.index("Remediation_Status") + 1, "AI Error")

        print("Cooling down 45s...")
        time.sleep(45) 

    print("Batch grading completed.")

if __name__ == '__main__':
    main()
