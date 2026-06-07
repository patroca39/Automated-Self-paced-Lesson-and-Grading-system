import os
import time
import json
import io
import re
import uuid
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
# TODO: Paste the ID of your Master Template Google Form below
# (Make sure this template only has "Full Name" and "Upload your work" questions)
MASTER_TEMPLATE_ID = "PASTE_YOUR_TEMPLATE_ID_HERE" 

class GradingSchema(BaseModel):
    score: int
    lesson: str
    mcq_quiz: list[dict] = [] 

class MCQ(BaseModel):
    question: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_answer: str 
    difficulty: str 

class LessonSchema(BaseModel):
    lesson_title: str
    lecture_content: str
    quiz: list[MCQ]

def get_google_services():
    token_dict = json.loads(os.environ.get('GOOGLE_TOKEN_JSON'))
    creds = Credentials.from_authorized_user_info(token_dict)
    return (
        gspread.authorize(creds), 
        build('drive', 'v3', credentials=creds),
        build('forms', 'v1', credentials=creds)
    )

def fetch_from_item_bank(sheet_client, comp_code, strand_focus, required_count=5):
    bank_sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    all_items = bank_sheet.get_all_records()
    
    # Filter by BOTH Topic and Strand
    matched_items = [item for item in all_items if item.get('Topic_Focus') == comp_code and str(item.get('Strand_Focus', '')).strip().upper() == str(strand_focus).strip().upper()]
    
    # If we have enough questions banked, return them as a list of dictionaries to build radio buttons
    if len(matched_items) >= required_count:
        return matched_items[:required_count]
    return None

def save_to_item_bank(sheet_client, comp_code, strand_focus, mcq_list):
    bank_sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    new_rows = []
    for q in mcq_list:
        item_id = f"{comp_code}-{str(uuid.uuid4())[:6]}"
        new_rows.append([
            item_id, comp_code, strand_focus, q.question, 
            q.option_a, q.option_b, q.option_c, q.option_d, 
            q.correct_answer, q.difficulty, 0
        ])
    bank_sheet.append_rows(new_rows)

def get_lesson_generation_prompt(curr, tmpl, strand_focus):
    return f"""
    You are an expert Teacher at Sagkahan National High School.
    Create an introductory self-paced lesson module for this competency: {curr['learning_competency']}
    
    CRITICAL CONTEXT: You MUST tailor the examples, business scenarios, and tone specifically for a student in the {strand_focus} strand. Ensure the real-world applications make sense for their specialization.
    
    1. LECTURE: Write a clear, engaging lecture based on the DepEd DLL Objectives: {tmpl['I_OBJECTIVES']} and Procedures: {tmpl['IV_PROCEDURES']['Mastery']}. Make it easy for a student to read independently.
    2. QUIZ: Generate a 5-item multiple choice quiz based on the lecture to test their understanding. Format it clearly with A, B, C, D options.
    
    Return as JSON matching the schema.
    """

def get_grading_prompt(curr, tmpl, assessment_type, score, typed_text, strand_focus):
    is_struggling = score < curr.get("mastery_threshold", 75)
    instruction = f"REMEDIATION: Provide scaffolding based on: {' -> '.join(curr.get('scaffolding_steps', ['Review', 'Practice', 'Apply']))}." if is_struggling else f"ENRICHMENT: Provide a complex, real-world scenario assignment highly relevant to the {strand_focus} strand."
    
    return f"""
    You are an expert Teacher grading a {strand_focus} student's response.
    Competency: {curr['learning_competency']}
    {instruction}
    Student Quiz Answers/Response: {typed_text}
    
    Return as JSON: {{"score": integer, "lesson": "string (The scaffolding or enrichment assignment contextualized for {strand_focus})", "mcq_quiz": []}}
    """

def create_new_assessment_form(comp_code, lesson_data, drive_service, form_service, template_id, banked_questions=None):
    # 1. Duplicate your Master Template
    new_form = drive_service.files().copy(
        fileId=template_id,
        body={"name": f"Module: {comp_code} - {lesson_data.lesson_title}"} if lesson_data else {"name": f"Module: {comp_code}"}
    ).execute()
    new_form_id = new_form['id']
    
    # 2. Inject the AI Lecture at the very top (Index 0)
    requests = []
    if lesson_data:
        requests.append({
            "createItem": {
                "item": {
                    "title": "📖 Reading Module", 
                    "description": lesson_data.lecture_content
                }, 
                "location": {"index": 0}
            }
        })
    
    # 3. Generate REAL Radio Buttons for the 5 Quiz Questions
    # Decide source: AI generated (LessonSchema objects) or Banked (Dictionaries)
    questions_to_build = banked_questions if banked_questions else lesson_data.quiz
    
    for i, q in enumerate(questions_to_build):
        # Handle the slight data structure difference between Banked Dicts and AI Pydantic Objects
        q_text = q.get('Question_Text') if banked_questions else q.question
        opt_a = q.get('Option_A') if banked_questions else q.option_a
        opt_b = q.get('Option_B') if banked_questions else q.option_b
        opt_c = q.get('Option_C') if banked_questions else q.option_c
        opt_d = q.get('Option_D') if banked_questions else q.option_d

        requests.append({
            "createItem": {
                "item": {
                    "title": f"Q{i+1}. {q_text}",
                    "questionItem": {
                        "question": {
                            "required": True,
                            "choiceQuestion": {
                                "type": "RADIO",
                                "options": [
                                    {"value": f"A) {opt_a}"},
                                    {"value": f"B) {opt_b}"},
                                    {"value": f"C) {opt_c}"},
                                    {"value": f"D) {opt_d}"}
                                ]
                            }
                        }
                    }
                },
                # Places them sequentially right after the Reading block
                "location": {"index": i + 1 if lesson_data else i} 
            }
        })

    # 4. Execute the build
    if requests:
        form_service.forms().batchUpdate(formId=new_form_id, body={"requests": requests}).execute()
        
    final_form = form_service.forms().get(formId=new_form_id).execute()
    return final_form['responderUri']

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
        comp_code = row.get("Topic_Focus")
        strand_focus = str(row.get("Strand_Focus", "ABM")).strip() # Defaults to ABM if blank
        curr = curr_data.get(comp_code)
        
        if not curr: continue

        # 1. Handle MODULE GENERATION Phase
        if row.get("Form_Generation_Status") == "READY":
            try:
                # Check Item Bank FIRST
                banked_questions = fetch_from_item_bank(sheet_client, comp_code, strand_focus)
                
                lesson_data = None
                if banked_questions:
                    print(f"Pulled {comp_code} ({strand_focus}) questions from Item Bank.")
                    # Still need the lecture, so ask AI just for the lecture (optional optimization to bank lectures too)
                    gen_prompt = get_lesson_generation_prompt(curr, dll_tmpl, strand_focus)
                    res = gen_client.models.generate_content(
                        model='gemini-2.0-flash', 
                        contents=gen_prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LessonSchema)
                    )
                    lesson_data = LessonSchema.model_validate_json(res.text)
                    form_url = create_new_assessment_form(comp_code, lesson_data, drive_service, form_service, MASTER_TEMPLATE_ID, banked_questions)
                
                else:
                    # Not in bank, generate full module
                    gen_prompt = get_lesson_generation_prompt(curr, dll_tmpl, strand_focus)
                    res = gen_client.models.generate_content(
                        model='gemini-2.0-flash', 
                        contents=gen_prompt,
                        config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=LessonSchema)
                    )
                    lesson_data = LessonSchema.model_validate_json(res.text)
                    
                    # Save the new questions to the bank
                    save_to_item_bank(sheet_client, comp_code, strand_focus, lesson_data.quiz)
                    print(f"Generated new {strand_focus} questions and saved to Item Bank for {comp_code}.")
                    
                    form_url = create_new_assessment_form(comp_code, lesson_data, drive_service, form_service, MASTER_TEMPLATE_ID)
                
                # Update Sheet
                sheet.update_cell(index, headers.index("Form_URL") + 1, form_url)
                sheet.update_cell(index, headers.index("Form_Generation_Status") + 1, "DEPLOYED")
                
            except Exception as e: print(f"Form Gen Error: {e}")
            continue

        # 2. Handle GRADING Phase
        if str(row.get("Remediation_Status", "")).strip() != "Pending": continue
        
        contents = [get_grading_prompt(curr, dll_tmpl, row.get("Assessment_Type"), row.get("Score") or 0, row.get("Digital_Answers", ""), strand_focus)]
        
        # Image Processing with Regex that catches both standard links and native Form uploads
        raw_link = str(row.get("Log_ID", "")).strip()
        if raw_link:
            try:
                match = re.search(r'(?:/d/|id=)([a-zA-Z0-9_-]+)', raw_link)
                file_id = match.group(1) if match else raw_link
                
                fh = io.BytesIO()
                MediaIoBaseDownload(fh, drive_service.files().get_media(fileId=file_id)).next_chunk()
                fh.seek(0)
                contents.append(Image.open(fh))
            except Exception as e: 
                print(f"Image error: {e}")

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
            print(f"Graded row {index} successfully. Module cycle complete.")
        except Exception as e:
            print(f"Grading/Update Error: {e}")
        
        time.sleep(45)

if __name__ == '__main__':
    main()
