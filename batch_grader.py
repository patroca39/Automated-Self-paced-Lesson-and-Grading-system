import os
import time
import json
import io
import re
import gspread
from google.oauth2.credentials import Credentials
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
    # Authenticate using the User Token from GitHub Secrets
    token_dict = json.loads(os.environ.get('GOOGLE_TOKEN_JSON'))
    creds = Credentials.from_authorized_user_info(token_dict)
    
    return (
        gspread.authorize(creds), 
        build('drive', 'v3', credentials=creds),
        build('forms', 'v1', credentials=creds)
    )

def get_prompt(curr, tmpl, assessment_type, score, typed_text):
    is_struggling = score < curr.get("mastery_threshold", 75)
    instruction = f"REMEDIATION: Provide scaffolding based on: {' -> '.join(curr.get('scaffolding_steps', ['Review', 'Practice', 'Apply']))}" if is_struggling else "ENRICHMENT: Provide a complex, real-world business scenario."
    exam_instruction = "Generate a 50-item major exam. STRICT TOS: 60% Easy, 30% Average, 10% Difficult." if assessment_type == "periodical" else ""
    
    return f"""
    You are an expert ABM Teacher at Sagkahan National High School.
    Competency: {curr['learning_competency']}
    Assessment Type: {assessment_type.upper()}
    {instruction}
    {exam_instruction}
    Use official DepEd DLL format:
    1. OBJECTIVES: {tmpl['I_OBJECTIVES']}
    2. PROCEDURES: {tmpl['IV_PROCEDURES']['Mastery']}
    3. EVALUATION: {tmpl['IV_PROCEDURES']['Evaluation']}
    Student Response: {typed_text}
    Return as JSON: {{"score": integer, "lesson": "string", "mcq_quiz": []}}
    """

def create_new_assessment_form(comp_code, assessment_type, form_service):
    form_body = {"info": {"title": f"Assessment: {comp_code} - {assessment_type.upper()}"}}
    created_form = form_service.forms().create(body=form_body).execute()
    
    requests = [
        {"createItem": {"item": {"title": "Full Name", "questionItem": {"question": {"required": True, "textQuestion": {}}}}, "location": {"index": 0}}},
        {"createItem": {"item": {"title": "Student Email", "questionItem": {"question": {"required": True, "textQuestion": {}}}}, "location": {"index": 1}}},
        # The crucial fix: Asking for a Drive Link instead of native file upload
        {"createItem": {"item": {"title": "Upload your work (Paste Google Drive Link here)", "questionItem": {"question": {"required": True, "textQuestion": {}}}}, "location": {"index": 2}}}
    ]
    form_service.forms().batchUpdate(formId=created_form['formId'], body={"requests": requests}).execute()
    return created_form['responderUri']

def main():
    sheet_client, drive_service, form_service = get_google_services()
    with open("curriculum_guide.json", "r") as f: curr_data = json.load(f)["ABM_BM11"]
    with open("dll_template.json", "r") as f: dll_tmpl = json.load(f)["dll_template"]
    
    sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Skill_Analytics")
    
    # Bulletproof fix for duplicate/blank headers
    raw_data = sheet.get_all_values()
    headers = raw_data[0]
    all_records = [dict(zip(headers, row)) for row in raw_data[1:] if any(row)]

    for index, row in enumerate(all_records, start=2):
        # 1. Handle Form Generation
        if row.get("Form_Generation_Status") == "READY":
            try:
                form_url = create_new_assessment_form(row["Topic_Focus"], row["Assessment_Type"], form_service)
                sheet.update_cell(index, headers.index("Form_URL") + 1, form_url)
                sheet.update_cell(index, headers.index("Form_Generation_Status") + 1, "DEPLOYED")
                print(f"Generated form for {row['Topic_Focus']}")
            except Exception as e: print(f"Form Gen Error: {e}")
            continue

        # 2. Handle Grading
        if str(row.get("Remediation_Status", "")).strip() != "Pending": continue

        comp_code = row.get("Topic_Focus")
        curr = curr_data.get(comp_code)
        if not curr: continue
        
        contents = [get_prompt(curr, dll_tmpl, row.get("Assessment_Type"), row.get("Score") or 0, row.get("Digital_Answers", ""))]
        
        # Image Processing with Regex Link Extractor
        raw_link = str(row.get("Log_ID", "")).strip()
        if raw_link:
            try:
                match = re.search(r'/d/([a-zA-Z0-9_-]+)', raw_link)
                file_id = match.group(1) if match else raw_link
                
                fh = io.BytesIO()
                MediaIoBaseDownload(fh, drive_service.files().get_media(fileId=file_id)).next_chunk()
                fh.seek(0)
                contents.append(Image.open(fh))
            except Exception as e: 
                print(f"Image error (Check if link is accessible): {e}")

        # AI Processing
        try:
            res = gen_client.models.generate_content(
                model='gemini-2.0-flash', 
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=GradingSchema)
            )
            data = json.loads(res.text)
            
            # Update Sheet
            sheet.update_cell(index, headers.index("Score") + 1, data['score'])
            sheet.update_cell(index, headers.index("Remediation") + 1, data['lesson'])
            sheet.update_cell(index, headers.index("Remediation_Status") + 1, "Mastered" if data['score'] >= curr.get("mastery_threshold", 75) else "Needs Review")
            print(f"Graded row {index} successfully.")
        except Exception as e:
            print(f"Grading/Update Error: {e}")
        
        time.sleep(45)

if __name__ == '__main__':
    main()
