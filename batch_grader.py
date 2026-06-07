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
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"])
    return gspread.authorize(creds), build('drive', 'v3', credentials=creds)

def get_prompt(curr, tmpl, assessment_type, score, typed_text):
    # Differentiate logic: Scaffolding vs. Enrichment
    is_struggling = score < curr.get("mastery_threshold", 75)
    
    if is_struggling:
        instruction = f"REMEDIATION: Provide step-by-step scaffolding based on: {' -> '.join(curr.get('scaffolding_steps', ['Review', 'Practice', 'Apply']))}"
    else:
        instruction = "ENRICHMENT: The student has mastered this. Provide a complex, real-world business scenario for extension."

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

def main():
    sheet_client, drive_service = get_google_services()
    with open("curriculum_guide.json", "r") as f: curr_data = json.load(f)["ABM_BM11"]
    with open("dll_template.json", "r") as f: dll_tmpl = json.load(f)["dll_template"]
    
    sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Skill_Analytics")
    headers = sheet.row_values(1)
    
    # 1. Check Class Mastery (90% threshold rule)
    # Assumes column 'Remediation_Status' is at index 7 (G)
    all_records = sheet.get_all_records()
    mastered_count = sum(1 for r in all_records if r.get("Remediation_Status") == "Mastered")
    class_mastery_pct = mastered_count / len(all_records) if all_records else 0
    is_class_unlocked = class_mastery_pct >= 0.9

    for index, row in enumerate(all_records, start=2):
        if str(row.get("Remediation_Status", "")).strip() != "Pending": continue

        comp_code = row.get("Topic_Focus")
        curr = curr_data.get(comp_code)
        file_id = str(row.get("Log_ID", "")).strip()
        typed_text = str(row.get("Digital_Answers", "")).strip()
        assessment_type = str(row.get("Assessment_Type", "daily_quiz")).strip()
        current_score = row.get("Score") or 0
        
        if not curr: continue

        # 2. Logic Branching: Lock vs Unlock
        # If LOCKED, system forces remediation. If UNLOCKED, teacher can deploy new content.
        contents = [get_prompt(curr, dll_tmpl, assessment_type, current_score, typed_text)]
        
        if file_id:
            try:
                fh = io.BytesIO()
                MediaIoBaseDownload(fh, drive_service.files().get_media(fileId=file_id)).next_chunk()
                fh.seek(0)
                contents.append(Image.open(fh))
            except Exception as e:
                print(f"Image error: {e}")

        # AI Processing
        for attempt in range(5):
            try:
                res = gen_client.models.generate_content(
                    model='gemini-2.0-flash', 
                    contents=contents,
                    config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=GradingSchema)
                )
                data = json.loads(res.text)
                
                sheet.update_cell(index, headers.index("Score") + 1, data['score'])
                sheet.update_cell(index, headers.index("Remediation") + 1, data['lesson'])
                sheet.update_cell(index, headers.index("Remediation_Status") + 1, "Mastered" if data['score'] >= curr.get("mastery_threshold", 75) else "Needs Review")
                break
            except Exception as e:
                time.sleep((attempt + 1) * 60)
        
        time.sleep(45)

if __name__ == '__main__':
    main()
