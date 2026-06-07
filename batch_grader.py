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

# --- THE NEW SDK ---
from google import genai
from google.genai import types

# --- CONFIGURATION ---
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
BASEROW_KEY = os.environ.get("BASEROW_API_KEY")

SPREADSHEET_NAME = "Business_Math_Master_Gradebook"
WORKSHEET_NAME = "Skill_Analytics"
BASEROW_URL = "https://api.baserow.io/api/database/rows/table/1012002/"

# Initialize New Gemini SDK
gen_client = genai.Client(api_key=GEMINI_KEY)

# --- PYDANTIC SCHEMA ENFORCEMENT ---
class GradingSchema(BaseModel):
    score: int
    lesson: str

SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_google_services():
    """Authenticates using in-memory environment variables (No file needed!)"""
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

def main():
    try:
        sheet_client, drive_service = get_google_services()
        sheet = sheet_client.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    except Exception as e:
        print(f"Failed to connect to Google Services: {e}")
        return

    all_rows = sheet.get_all_records()
    print(f"Total rows found in sheet: {len(all_rows)}")

    for index, row in enumerate(all_rows, start=2):
        # FIX 1: Look exactly at your "Remediation_Status" column
        status = str(row.get("Remediation_Status", "")).strip()

        if status != "Pending":
            continue

        student_id = row.get("Student_ID", "unknown")
        file_id = row.get("File_ID", "")
        competency = row.get("Topic_Focus", "ABM_BM11BS-Ig-1")
        strand = str(row.get("Strand_Focus", "BEC")).strip() or "BEC"

        print(f"Processing student {student_id} (File: {file_id})...")

        try:
            student_image = download_image(drive_service, file_id)
        except Exception as img_err:
            print(f"Could not download image {file_id}: {img_err}")
            # FIX 2: Update the error write-back to use the correct header
            sheet.update_cell(index, sheet.row_values(1).index("Remediation_Status") + 1, "Image Error")
            continue

        headers = {"Authorization": f"Token {BASEROW_KEY}"}
        archived_lesson = ""
        try:
            search_url = f"{BASEROW_URL}?user_field_names=true&filter__field_Competency_Code__equal={competency}"
            res = requests.get(search_url, headers=headers, timeout=5).json()
            if res.get('results'): 
                archived_lesson = res['results'][0]['Lesson_Content_4As']
        except Exception:
            pass

        prompt = f"""
        Analyze the handwritten or typed math in the provided image for competency {competency}.
        Score the work (0-100). If the score < 75, generate a specific 4As lesson plan (### 1. Activity, ### 2. Analysis, ### 3. Abstraction, ### 4. Application) for strand {strand}.
        """

        gemini_success = False
        score, lesson, final_status = 0, "", "Needs Review"
        
        for attempt in range(3):
            try:
                response = gen_client.models.generate_content(
                    model='gemini-2.5-flash',
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
                final_status = "Mastered" if score >= 75 else "Needs Review"
                gemini_success = True
                break
            except Exception as gemini_err:
                print(f"Gemini attempt {attempt + 1} failed: {gemini_err}. Retrying...")
                time.sleep(10)

        remediation_content = archived_lesson or lesson
        headers_row = sheet.row_values(1)
        
        try:
            if gemini_success:
                sheet.update_cell(index, headers_row.index("Score") + 1, score)
                sheet.update_cell(index, headers_row.index("Remediation") + 1, remediation_content)
                # FIX 3: Update the final status write-back
                sheet.update_cell(index, headers_row.index("Remediation_Status") + 1, final_status)
                print(f"Successfully graded {student_id}. Score: {score}")
            else:
                sheet.update_cell(index, headers_row.index("Remediation_Status") + 1, "AI Error")
        except ValueError as cell_err:
            print(f"Column mapping error: {cell_err}")

        time.sleep(5)

    print("Batch grading completed.")

if __name__ == '__main__':
    main()
