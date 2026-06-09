import os
import time
import json
import io
import re
import uuid
import gspread
import pandas as pd  # Required for Idempotency Check
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image
from pydantic import BaseModel
from google import genai
from google.genai import types

# --- CONFIGURATION ---
gen_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# 🚨 UPDATE THIS: Paste the ID of your ONE permanent Google Form here
DYNAMIC_FORM_ID = "16uOwbZbu86xWv1o7fl99TrjRRzlhOiyvg-QgybQr3MA" 

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

# 🚀 The AI Armor Engine
def call_gemini_with_retry(contents, schema_class, retries=4):
    for attempt in range(retries):
        try:
            print(f"Calling Gemini Core (Attempt {attempt + 1}/{retries})...")
            res = gen_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema_class
                )
            )
            
            if hasattr(schema_class, 'model_validate_json'):
                return schema_class.model_validate_json(res.text)
            else:
                return json.loads(res.text)
                
        except Exception as e:
            print(f"🛑 GenAI System Exception (Attempt {attempt + 1}): {e}")
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e).upper():
                print("Quota exceeded. Triggering 50-second cooldown block...")
                time.sleep(50) 
            else:
                time.sleep(35)
                
    print("❌ Gemini interface drop-out. Max retries reached.")
    return None

def fetch_from_item_bank(sheet_client, comp_code, strand_focus, required_count=5):
    bank_sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    all_items = bank_sheet.get_all_records()
    matched_items = [item for item in all_items if item.get('Topic_Focus') == comp_code and str(item.get('Strand_Focus', '')).strip().upper() == str(strand_focus).strip().upper()]
    if len(matched_items) >= required_count: return matched_items[:required_count]
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
    
    🛑 FORMATTING MANDATE FOR THE LECTURE: 
    Google Forms requires plain text, so you MUST structure the `lecture_content` string beautifully using spacing:
    - Use double line breaks (\\n\\n) to create distinct, bite-sized paragraphs.
    - Use ALL CAPS for section headers (e.g., INTRODUCTION, CORE CONCEPT, {strand_focus} APPLICATION).
    - Use unicode bullet points (•) for lists, key takeaways, or step-by-step procedures.
    - NEVER output a single, giant wall of text. Break it up so it is easy on the eyes.
    
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

def update_dynamic_form(comp_code, lesson_data, form_service, form_id, banked_questions=None):
    """
    Clears the previous module's questions (leaving the top 4 static fields intact)
    and injects the newly generated lesson and quiz into the permanent form.
    """
    form = form_service.forms().get(formId=form_id).execute()
    items = form.get('items', [])
    requests = []
    
    # 1. Update the Title of the Form
    requests.append({
        "updateFormInfo": {
            "info": {
                "title": f"Module: {comp_code} - {lesson_data.lesson_title}" if lesson_data else f"Module: {comp_code}"
            },
            "updateMask": "title"
        }
    })
    
    # 2. Wipe old lesson/quiz items safely (Preserving Name, LRN, Topic, Upload at indices 0, 1, 2, 3)
    for i in range(len(items) - 1, 3, -1):
        requests.append({
            "deleteItem": {
                "location": {"index": i}
            }
        })
        
    current_index = 4 # Start injecting below the File Upload question
    
    if lesson_data:
        requests.append({
            "createItem": {
                "item": {
                    "title": "📖 Reading Module", 
                    "description": lesson_data.lecture_content,
                    "textItem": {} 
                }, 
                "location": {"index": current_index}
            }
        })
        current_index += 1
    
    questions_to_build = banked_questions if banked_questions else lesson_data.quiz
    
    for i, q in enumerate(questions_to_build):
        q_text = q.get('Question_Text') if banked_questions else q.question
        
        # 🚨 STATIC COLUMNS RULE: Title is generic, Description holds the real question.
        requests.append({
            "createItem": {
                "item": {
                    "title": f"Question {i+1}", 
                    "description": q_text,
                    "questionItem": {
                        "question": {
                            "required": True,
                            "choiceQuestion": {
                                "type": "RADIO",
                                "options": [{"value": f"A) {q.get('Option_A', q.get('option_a'))}"}, 
                                            {"value": f"B) {q.get('Option_B', q.get('option_b'))}"}, 
                                            {"value": f"C) {q.get('Option_C', q.get('option_c'))}"}, 
                                            {"value": f"D) {q.get('Option_D', q.get('option_d'))}"}]
                            }
                        }
                    }
                },
                "location": {"index": current_index} 
            }
        })
        current_index += 1

    if requests:
        form_service.forms().batchUpdate(formId=form_id, body={"requests": requests}).execute()
        
    return f"https://docs.google.com/forms/d/{form_id}/viewform"


def main():
    sheet_client, drive_service, form_service = get_google_services()
    with open("curriculum_guide.json", "r") as f: curr_data = json.load(f)["ABM_BM11"]
    with open("dll_template.json", "r") as f: dll_tmpl = json.load(f)["dll_template"]
    
    sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Skill_Analytics")
    
    raw_data = sheet.get_all_values()
    headers = raw_data[0]
    all_records = [dict(zip(headers, row)) for row in raw_data[1:] if any(row)]

    # ---------------------------------------------------------
    # 🛡️ THE IDEMPOTENCY CHECK (Spam Prevention)
    # ---------------------------------------------------------
    df = pd.DataFrame(all_records)
    if not df.empty and "Remediation_Status" in df.columns:
        pending_mask = df['Remediation_Status'].str.strip() == 'Pending'
        pending_df = df[pending_mask]
        
        if not pending_df.empty and 'Student_ID' in pending_df.columns and 'Topic_Focus' in pending_df.columns:
            duplicates = pending_df.duplicated(subset=['Student_ID', 'Topic_Focus'], keep='last')
            duplicate_indices = pending_df[duplicates].index
            
            for idx in duplicate_indices:
                sheet_row = idx + 2  
                sheet.update_cell(sheet_row, headers.index("Remediation_Status") + 1, "Duplicate_Ignored")
                print(f"[SYSTEM] Cleaned duplicate submission for row {sheet_row}")
                all_records[idx]['Remediation_Status'] = "Duplicate_Ignored"
    # ---------------------------------------------------------

    for index, row in enumerate(all_records, start=2):
        raw_comp_code = str(row.get("Topic_Focus", ""))
        comp_code = raw_comp_code.replace("ABM_BM11", "") 
        strand_focus = str(row.get("Strand_Focus", "ABM")).strip()
        curr = curr_data.get(comp_code)
        
        if not curr: continue

        # ---------------------------------------------------------
        # 1. Handle MODULE GENERATION Phase
        # ---------------------------------------------------------
        if row.get("Form_Generation_Status") == "READY":
            try:
                banked_questions = fetch_from_item_bank(sheet_client, comp_code, strand_focus)
                lesson_data = None
                
                if banked_questions:
                    print(f"[{comp_code}] Pulled {strand_focus} questions from Item Bank.")
                    gen_prompt = get_lesson_generation_prompt(curr, dll_tmpl, strand_focus)
                    lesson_data = call_gemini_with_retry(gen_prompt, LessonSchema)
                    if not lesson_data: continue
                    form_url = update_dynamic_form(comp_code, lesson_data, form_service, DYNAMIC_FORM_ID, banked_questions)
                else:
                    print(f"[{comp_code}] Generating NEW questions and lesson via Gemini...")
                    gen_prompt = get_lesson_generation_prompt(curr, dll_tmpl, strand_focus)
                    lesson_data = call_gemini_with_retry(gen_prompt, LessonSchema)
                    if not lesson_data: continue
                    
                    save_to_item_bank(sheet_client, comp_code, strand_focus, lesson_data.quiz)
                    form_url = update_dynamic_form(comp_code, lesson_data, form_service, DYNAMIC_FORM_ID)
                
                sheet.update_cell(index, headers.index("Form_URL") + 1, form_url)
                sheet.update_cell(index, headers.index("Form_Generation_Status") + 1, "DEPLOYED")
                print(f"✅ Module Deployed for {comp_code}")
                
            except Exception as e: print(f"Form Gen Error: {e}")
            continue

        # ---------------------------------------------------------
        # 2. Handle GRADING Phase
        # ---------------------------------------------------------
        if str(row.get("Remediation_Status", "")).strip() != "Pending": continue
        
        digital_answers = str(row.get("Digital_Answers", "")).strip()
        raw_link = str(row.get("Log_ID", "")).strip()
        
        if not digital_answers and not raw_link:
            print(f"Skipping Row {index}: No digital answers or image link provided.")
            continue
            
        print(f"Grading Row {index} for {comp_code}...")
        contents = [get_grading_prompt(curr, dll_tmpl, row.get("Assessment_Type"), row.get("Score") or 0, digital_answers, strand_focus)]
        
        if raw_link:
            try:
                print(" -> Image link detected. Downloading from Drive...")
                match = re.search(r'(?:/d/|id=)([a-zA-Z0-9_-]+)', raw_link)
                file_id = match.group(1) if match else raw_link
                fh = io.BytesIO()
                MediaIoBaseDownload(fh, drive_service.files().get_media(fileId=file_id)).next_chunk()
                fh.seek(0)
                contents.append(Image.open(fh))
                print(" -> Image successfully attached to AI prompt.")
            except Exception as e: 
                print(f" -> Image error (Skipping image, grading text only): {e}")
        else:
            print(" -> No image link detected. Executing text-only grading.")

        data = call_gemini_with_retry(contents, GradingSchema)
        
        # 🚨 THE MANUAL REVIEW FLAG (Safety Guard)
        if not data:
            print(f"⚠️ AI failed to grade Row {index} (Possible Safety Block). Flagging for manual review.")
            try:
                sheet.update_cell(index, headers.index("Remediation_Status") + 1, "Manual_Review_Required")
                sheet.update_cell(index, headers.index("Remediation") + 1, "System Error: Please see instructor.")
            except Exception as e:
                print(f"Failed to update error status for row {index}: {e}")
            continue
        
        try:
            # 3-Tier grading system calculation
            final_status = "Excelling" if data.score >= 90 else "Passing" if data.score >= curr.get("mastery_threshold", 75) else "Needs Review"
            
            sheet.update_cell(index, headers.index("Score") + 1, data.score)
            sheet.update_cell(index, headers.index("Remediation") + 1, data.lesson)
            sheet.update_cell(index, headers.index("Remediation_Status") + 1, final_status)
            print(f"✅ Graded row {index} successfully. Status: {final_status}")
        except Exception as e:
            print(f"Grading/Update Error: {e}")
            
        time.sleep(15)

if __name__ == '__main__':
    main()
