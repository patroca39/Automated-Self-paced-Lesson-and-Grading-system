import os
import time
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
gen_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

class GradingSchema(BaseModel):
    score: int
    lesson: str
    mcq_quiz: list[dict] = [] 

def get_google_services():
    creds_dict = json.loads(os.environ.get('GOOGLE_CREDENTIALS_JSON'))
    # Added forms.body scope
    scopes = [
        "https://spreadsheets.google.com/feeds", 
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/forms.body"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scopes)
    return (
        gspread.authorize(creds), 
        build('drive', 'v3', credentials=creds),
        build('forms', 'v1', credentials=creds)
    )

def create_new_assessment_form(comp_code, assessment_type, form_service, folder_id):
    form_body = {"info": {"title": f"Assessment: {comp_code} - {assessment_type.upper()}"}}
    created_form = form_service.forms().create(body=form_body).execute()
    
    # Add standardized fields
    requests = [
        {"createItem": {"item": {"title": "Full Name", "questionItem": {"question": {"required": True, "textQuestion": {}}}}, "location": {"index": 0}}},
        {"createItem": {"item": {"title": "Student Email", "questionItem": {"question": {"required": True, "textQuestion": {}}}}, "location": {"index": 1}}},
        {"createItem": {"item": {"title": "Upload your work", "questionItem": {"question": {"required": True, "fileUploadQuestion": {"folderId": folder_id}}}}, "location": {"index": 2}}}
    ]
    form_service.forms().batchUpdate(formId=created_form['formId'], body={"requests": requests}).execute()
    return created_form['responderUri']

def main():
    sheet_client, drive_service, form_service = get_google_services()
    with open("curriculum_guide.json", "r") as f: curr_data = json.load(f)["ABM_BM11"]
    with open("dll_template.json", "r") as f: dll_tmpl = json.load(f)["dll_template"]
    
    sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Skill_Analytics")
    headers = sheet.row_values(1)
    
    # 1. Check Class Mastery (90% rule)
    all_records = sheet.get_all_records()
    mastered_count = sum(1 for r in all_records if r.get("Remediation_Status") == "Mastered")
    is_class_unlocked = (mastered_count / len(all_records)) >= 0.9 if all_records else False

    for index, row in enumerate(all_records, start=2):
        # 2. Automated Form Generation Trigger
        if row.get("Form_Generation_Status") == "READY":
            form_url = create_new_assessment_form(row["Topic_Focus"], row["Assessment_Type"], form_service, "YOUR_DRIVE_FOLDER_ID")
            sheet.update_cell(index, headers.index("Form_URL") + 1, form_url)
            sheet.update_cell(index, headers.index("Form_Generation_Status") + 1, "DEPLOYED")
            continue

        if str(row.get("Remediation_Status", "")).strip() != "Pending": continue

        # Processing Logic (Scaffolding vs Enrichment)
        comp_code = row.get("Topic_Focus")
        curr = curr_data.get(comp_code)
        if not curr: continue
        
        contents = [get_prompt(curr, dll_tmpl, row.get("Assessment_Type"), row.get("Score") or 0, row.get("Digital_Answers", ""))]
        
        # ... (Image handling logic remains the same)

        # AI Processing
        # ... (API call remains the same)
        
        time.sleep(45)

if __name__ == '__main__':
    main()
