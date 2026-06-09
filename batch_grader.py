import os
import time
import json
import re
import uuid
import gspread
import pandas as pd
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from google import genai
from google.genai import types

# --- CONFIGURATION ---
gen_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
DYNAMIC_FORM_ID = "16uOwbZbu86xWv1o7fl99TrjRRzlhOiyvg-QgybQr3MA" 

# --- ASSESSMENT RULES ENGINE ---
ASSESSMENT_RULES = {
    "QUIZ": {"target_count": 10, "has_lecture": True},
    "ASSIGNMENT": {"target_count": 10, "has_lecture": True},
    "UNIT_TEST": {"target_count": 30, "has_lecture": False},
    "MAJOR_EXAM": {"target_count": 40, "has_lecture": False, "hard_mode": True} 
}

# --- DIAGNOSTIC SCHEMAS ---
class MCQ(BaseModel):
    sub_concept: str          
    question: str
    option_a: str
    option_b: str
    option_c: str
    option_d: str
    correct_answer: str 
    targeted_remediation: str 
    difficulty: str 

class LessonSchema(BaseModel):
    lesson_title: str
    lecture_content: str
    remediation_scaffolding: str  
    enrichment_scenario: str      
    quiz: list[MCQ]

class ExamSchema(BaseModel):
    instructions: str
    quiz: list[MCQ]

def get_google_services():
    token_dict = json.loads(os.environ.get('GOOGLE_TOKEN_JSON'))
    creds = Credentials.from_authorized_user_info(token_dict)
    return gspread.authorize(creds), build('drive', 'v3', credentials=creds), build('forms', 'v1', credentials=creds)

def call_gemini_with_retry(contents, schema_class, retries=4):
    for attempt in range(retries):
        try:
            print(f"Calling Gemini Core (Attempt {attempt + 1}/{retries})...")
            res = gen_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema_class)
            )
            return schema_class.model_validate_json(res.text)
        except Exception as e:
            print(f"🛑 GenAI Exception: {e}")
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e).upper():
                print("Quota exceeded. Triggering 50-second cooldown block...")
                time.sleep(50) 
            else:
                time.sleep(35)
    return None

def fetch_from_vault(sheet_client, comp_code, strand_focus):
    try:
        vault = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Modules_Vault")
        for r in vault.get_all_records():
            if str(r.get('Topic_Focus')) == comp_code and str(r.get('Strand_Focus')).strip().upper() == str(strand_focus).strip().upper():
                return r
    except Exception: pass
    return None

def fetch_banked_questions(sheet_client, comp_code, strand_focus):
    bank = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    all_items = bank.get_all_records()
    return [item for item in all_items if item.get('Topic_Focus') == comp_code and str(item.get('Strand_Focus', '')).strip().upper() == str(strand_focus).strip().upper()]

def save_items_to_bank(sheet_client, comp_code, strand_focus, mcq_list):
    bank = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    new_rows = []
    for q in mcq_list:
        item_id = f"{comp_code}-{str(uuid.uuid4())[:6]}"
        new_rows.append([
            item_id, comp_code, strand_focus, q.question, 
            q.option_a, q.option_b, q.option_c, q.option_d, 
            q.correct_answer.strip().upper(), q.difficulty, 0,
            q.sub_concept, q.targeted_remediation
        ])
    bank.append_rows(new_rows)

def save_master_lesson(sheet_client, comp_code, strand_focus, lesson_data):
    vault = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Modules_Vault")
    vault.append_row([comp_code, strand_focus, lesson_data.lesson_title, lesson_data.lecture_content, lesson_data.remediation_scaffolding, lesson_data.enrichment_scenario])

def get_generation_prompt(curr, strand_focus, missing_count, is_exam, hard_mode=False):
    difficulty_context = "CRITICAL THINKING & ADVANCED ANALYSIS ONLY. These questions must be extremely difficult, requiring multi-step logic and deep synthesis." if hard_mode else "Standard high school difficulty."
    
    if is_exam:
        return f"""
        Generate exactly {missing_count} brand new multiple-choice questions for competency: {curr['learning_competency']} ({strand_focus} track).
        DIFFICULTY: {difficulty_context}
        Provide brief 'instructions' for the exam block.
        For each question, provide a 'sub_concept' tag and a 'targeted_remediation' sentence explaining the correct logic.
        """
    else:
        return f"""
        Create a self-paced module for competency: {curr['learning_competency']} ({strand_focus} track).
        1. LECTURE: Clear tutorial (use double line breaks and bullets).
        2. REMEDIATION: Scaffolding breakdown for struggling students.
        3. ENRICHMENT: Complex scenario for excelling students.
        4. QUIZ: {missing_count} questions. For each, provide a 'sub_concept' tag and a 'targeted_remediation' explanation.
        """

def update_dynamic_form(comp_code, instruction_title, instruction_body, combined_quiz, form_service, form_id):
    form = form_service.forms().get(formId=form_id).execute()
    items = form.get('items', [])
    requests = [{"updateFormInfo": {"info": {"title": f"{comp_code} Assessment"}, "updateMask": "title"}}]
    
    for i in range(len(items) - 1, 3, -1):
        requests.append({"deleteItem": {"location": {"index": i}}})
        
    current_index = 4
    if instruction_body:
        requests.append({
            "createItem": {
                "item": {"title": instruction_title, "description": instruction_body, "textItem": {}}, 
                "location": {"index": current_index}
            }
        })
        current_index += 1
    
    for i, q in enumerate(combined_quiz):
        if isinstance(q, dict):
            q_text = q.get('Question', q.get('Question_Text'))
            opts = [f"A) {q.get('Option_A')}", f"B) {q.get('Option_B')}", f"C) {q.get('Option_C')}", f"D) {q.get('Option_D')}"]
        else:
            q_text = q.question
            opts = [f"A) {q.option_a}", f"B) {q.option_b}", f"C) {q.option_c}", f"D) {q.option_d}"]

        requests.append({
            "createItem": {
                "item": {
                    "title": f"Question {i+1}", "description": q_text,
                    "questionItem": {"question": {"required": True, "choiceQuestion": {"type": "RADIO", "options": [{"value": o} for o in opts]}}}
                },
                "location": {"index": current_index}
            }
        })
        current_index += 1

    if requests:
        form_service.forms().batchUpdate(formId=form_id, body={"requests": requests}).execute()
    return f"https://docs.google.com/forms/d/{form_id}/viewform"

def grade_submission_natively(student_answers_str, comp_code, strand_focus, sheet_client):
    bank = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    answer_keys = [r for r in bank.get_all_records() if r.get('Topic_Focus') == comp_code and str(r.get('Strand_Focus')).strip().upper() == str(strand_focus).strip().upper()]
    
    if not answer_keys: return None, "", "Error: Answer keys not found."
        
    student_choices = re.findall(r'([A-D])\)', student_answers_str.upper()) or re.findall(r'\b([A-D])\b', student_answers_str.upper())
    student_choices += ['MISSING'] * max(0, len(answer_keys) - len(student_choices))

    correct_count = 0
    feedback_blocks = []
    
    for idx, item in enumerate(answer_keys):
        if idx < len(student_choices) and student_choices[idx] == str(item.get('Correct_Answer', '')).strip().upper():
            correct_count += 1
        else:
            sub = item.get('Sub_Concept', f'Concept {idx+1}')
            rem = item.get('Targeted_Remediation', 'Review this concept.')
            feedback_blocks.append(f"• {sub}: {rem}")

    score = int((correct_count / len(answer_keys)) * 100)
    return score, "\n".join(feedback_blocks), None

def main():
    sheet_client, drive_service, form_service = get_google_services()
    with open("curriculum_guide.json", "r") as f: curr_data = json.load(f)["ABM_BM11"]
    
    sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Skill_Analytics")
    headers = sheet.row_values(1)
    all_records = [dict(zip(headers, row)) for row in sheet.get_all_values()[1:] if any(row)]

    df = pd.DataFrame(all_records)
    if not df.empty and "Remediation_Status" in df.columns:
        pending_mask = df['Remediation_Status'].str.strip() == 'Pending'
        pending_df = df[pending_mask]
        if not pending_df.empty and 'Student_ID' in pending_df.columns and 'Topic_Focus' in pending_df.columns:
            duplicates = pending_df.duplicated(subset=['Student_ID', 'Topic_Focus'], keep='last')
            for idx in pending_df[duplicates].index:
                sheet.update_cell(idx + 2, headers.index("Remediation_Status") + 1, "Duplicate_Ignored")
                all_records[idx]['Remediation_Status'] = "Duplicate_Ignored"

    for index, row in enumerate(all_records, start=2):
        if row.get("Remediation_Status") == "Duplicate_Ignored": continue
        
        raw_comp_code = str(row.get("Topic_Focus", ""))
        comp_code = raw_comp_code.replace("ABM_BM11", "") 
        strand_focus = str(row.get("Strand_Focus", "STEM")).strip()
        assessment_type = str(row.get("Assessment_Type", "QUIZ")).strip().upper()
        curr = curr_data.get(comp_code)
        
        if not curr: continue

        # --- HYBRID MODULE / EXAM GENERATION ---
        if row.get("Form_Generation_Status") == "READY":
            rules = ASSESSMENT_RULES.get(assessment_type, ASSESSMENT_RULES["QUIZ"])
            target_count = rules["target_count"]
            has_lecture = rules["has_lecture"]
            hard_mode = rules.get("hard_mode", False)

            banked_questions = fetch_banked_questions(sheet_client, comp_code, strand_focus)
            missing_count = max(0, target_count - len(banked_questions))
            
            combined_quiz = banked_questions.copy()
            instruction_title = "📖 Reading Module" if has_lecture else "📝 Exam Instructions"
            instruction_body = "Please answer the following questions carefully."

            if missing_count > 0:
                print(f"[{comp_code}] Generating {missing_count} NEW questions to reach {target_count}...")
                gen_prompt = get_generation_prompt(curr, strand_focus, missing_count, not has_lecture, hard_mode)
                
                if has_lecture:
                    with open("dll_template.json", "r") as f: dll_tmpl = json.load(f)["dll_template"]
                    lesson_data = call_gemini_with_retry(gen_prompt, LessonSchema)
                    if not lesson_data: continue
                    save_master_lesson(sheet_client, comp_code, strand_focus, lesson_data)
                    save_items_to_bank(sheet_client, comp_code, strand_focus, lesson_data.quiz)
                    combined_quiz.extend(lesson_data.quiz)
                    instruction_body = lesson_data.lecture_content
                else:
                    exam_data = call_gemini_with_retry(gen_prompt, ExamSchema)
                    if not exam_data: continue
                    save_items_to_bank(sheet_client, comp_code, strand_focus, exam_data.quiz)
                    combined_quiz.extend(exam_data.quiz)
                    instruction_body = exam_data.instructions
            else:
                print(f"[{comp_code}] Target count ({target_count}) met entirely using Item Bank.")
                if has_lecture:
                    vault = fetch_from_vault(sheet_client, comp_code, strand_focus)
                    instruction_body = vault.get('Lecture_Content', '') if vault else ""
            
            try:
                form_url = update_dynamic_form(comp_code, instruction_title, instruction_body, combined_quiz, form_service, DYNAMIC_FORM_ID)
                sheet.update_cell(index, headers.index("Form_URL") + 1, form_url)
                sheet.update_cell(index, headers.index("Form_Generation_Status") + 1, "DEPLOYED")
                print(f"✅ {assessment_type} Deployed for {comp_code}")
            except Exception as e: print(f"Form Gen Error: {e}")
            continue

        # --- NATIVE INSTANT GRADING ---
        if str(row.get("Remediation_Status", "")).strip() != "Pending": continue
        
        digital_answers = str(row.get("Digital_Answers", "")).strip()
        if not digital_answers:
            print(f"Skipping Row {index}: No digital answers provided.")
            continue
            
        print(f"⚡ Natively Grading {assessment_type} for Row {index} ({comp_code})...")
        score, diag_feedback, error = grade_submission_natively(digital_answers, comp_code, strand_focus, sheet_client)
        
        if error:
            print(f"❌ Grading Error: {error}")
            sheet.update_cell(index, headers.index("Remediation_Status") + 1, "Manual_Review_Required")
            sheet.update_cell(index, headers.index("Remediation") + 1, error)
            continue
            
        status = "Excelling" if score >= 90 else "Passing" if score >= curr.get("mastery_threshold", 75) else "Needs Review"
        
        if score == 100:
            vault = fetch_from_vault(sheet_client, comp_code, strand_focus)
            final_feedback = vault.get('Enrichment_Text', "Perfect score! Keep up the great work.") if vault else "Perfect score!"
        else:
            final_feedback = diag_feedback

        try:
            sheet.update_cell(index, headers.index("Score") + 1, score)
            sheet.update_cell(index, headers.index("Remediation") + 1, final_feedback)
            sheet.update_cell(index, headers.index("Remediation_Status") + 1, status)
            print(f"✅ Row {index} complete. Score: {score}%")
        except Exception as e:
            print(f"Grading/Update Error: {e}")

if __name__ == '__main__':
    main()
