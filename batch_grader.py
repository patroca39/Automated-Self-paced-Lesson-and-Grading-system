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

def fetch_student_from_roster(sheet_client, student_id):
    try:
        roster = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Master_Roster")
        for r in roster.get_all_records():
            if str(r.get('Student_ID', '')).strip() == str(student_id).strip():
                return r
    except Exception as e:
        print(f"Roster Lookup Error: {e}")
    return None

def format_math_text(text):
    # Enforces standard symbols just in case
    return str(text).replace("*", "×").replace("/", "÷")

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
            if str(r.get('Topic_Focus', '')).strip().upper() == comp_code.strip().upper() and str(r.get('Strand_Focus', '')).strip().upper() == strand_focus.strip().upper():
                return r
    except Exception: pass
    return None

def fetch_banked_questions(sheet_client, comp_code, strand_focus):
    bank = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    all_items = bank.get_all_records()
    return [item for item in all_items if str(item.get('Topic_Focus', '')).strip().upper() == comp_code.strip().upper() and str(item.get('Strand_Focus', '')).strip().upper() == strand_focus.strip().upper()]

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
    difficulty_context = "CRITICAL THINKING & ADVANCED ANALYSIS ONLY." if hard_mode else "Standard high school difficulty."
    
    if is_exam:
        return f"""
        Generate exactly {missing_count} brand new multiple-choice questions for competency: {curr['learning_competency']} ({strand_focus} track).
        DIFFICULTY: {difficulty_context}
        🛑 MATH FORMATTING MANDATE: Google Forms CANNOT read LaTeX. 
        - DO NOT use $, \\frac, \\times, or any LaTeX syntax. 
        - Use plain keyboard symbols (e.g., write fractions as a/b, use ×, ÷, =).
        Provide brief 'instructions' for the exam block.
        For each question, provide a 'sub_concept' tag and a 'targeted_remediation' sentence explaining the correct logic.
        """
    else:
        return f"""
        Create a self-paced module for competency: {curr['learning_competency']} ({strand_focus} track).
        
        🛑 FORMATTING MANDATE FOR THE LECTURE & MATH: 
        - Google Forms CANNOT read LaTeX. DO NOT use $, \\frac, \\times, or any LaTeX syntax.
        - Use standard unicode symbols for math (e.g., ½, ², ×, ÷). Write fractions with a slash (e.g., 3/4).
        - Use double line breaks (\\n\\n) to create distinct paragraphs.
        - Use ALL CAPS for section headers.
        - Use unicode bullet points (•) for lists.
        - NEVER output a single, giant wall of text. Break it up.
        
        1. LECTURE: Clear tutorial strictly following the formatting mandate above.
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
            q_text = format_math_text(q.get('Question', q.get('Question_Text')))
            opts = [f"A) {format_math_text(q.get('Option_A'))}", f"B) {format_math_text(q.get('Option_B'))}", f"C) {format_math_text(q.get('Option_C'))}", f"D) {format_math_text(q.get('Option_D'))}"]
        else:
            q_text = format_math_text(q.question)
            opts = [f"A) {format_math_text(q.option_a)}", f"B) {format_math_text(q.option_b)}", f"C) {format_math_text(q.option_c)}", f"D) {format_math_text(q.option_d)}"]

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
    answer_keys = [r for r in bank.get_all_records() if str(r.get('Topic_Focus', '')).strip().upper() == comp_code.strip().upper() and str(r.get('Strand_Focus', '')).strip().upper() == strand_focus.strip().upper()]
    
    if not answer_keys: return None, "", f"Error: Answer keys not found for {comp_code} ({strand_focus})."
        
    student_choices = re.findall(r'([A-D])\)', student_answers_str.upper()) or re.findall(r'\b([A-D])\b', student_answers_str.upper())
    student_choices += ['MISSING'] * max(0, len(answer_keys) - len(student_choices))

    correct_count = 0
    feedback_blocks = []
    
    for idx, item in enumerate(answer_keys):
        ans = next((item[k] for k in item if k.lower() == 'correct_answer'), "")
        if idx < len(student_choices) and student_choices[idx] == str(ans).strip().upper():
            correct_count += 1
        else:
            sub = item.get('Sub_Concept', f'Concept {idx+1}')
            rem = item.get('Targeted_Remediation', 'Review this concept.')
            feedback_blocks.append(f"• {sub}: {rem}")

    score = int((correct_count / len(answer_keys)) * 100)
    return score, format_math_text("\n".join(feedback_blocks)), None

def main():
    print("Starting Script...")
    sheet_client, drive_service, form_service = get_google_services()
    with open("curriculum_guide.json", "r") as f: curr_data = json.load(f)["ABM_BM11"]
    
    sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Skill_Analytics")
    
    # Extract data securely to ensure index alignment
    all_values = sheet.get_all_values()
    headers = all_values[0]
    all_records = [dict(zip(headers, row)) for row in all_values[1:]]

    # --- PANDAS DUPLICATE CHECKER ---
    df = pd.DataFrame(all_records)
    if not df.empty and "Remediation_Status" in df.columns:
        pending_mask = df['Remediation_Status'].str.strip() == 'Pending'
        pending_df = df[pending_mask]
        if not pending_df.empty and 'Student_ID' in pending_df.columns and 'Topic_Focus' in pending_df.columns:
            # ONLY check duplicates for rows that actually have a Student_ID (Ignores Ghost Rows)
            valid_pending = pending_df[pending_df['Student_ID'].str.strip() != '']
            duplicates = valid_pending.duplicated(subset=['Student_ID', 'Topic_Focus'], keep='last')
            for idx in valid_pending[duplicates].index:
                actual_row = idx + 2
                sheet.update_cell(actual_row, headers.index("Remediation_Status") + 1, "Duplicate_Ignored")
                all_records[idx]['Remediation_Status'] = "Duplicate_Ignored"

    # --- MAIN PROCESSING LOOP ---
    for row_idx, row in enumerate(all_records, start=2):
        if row.get("Remediation_Status") == "Duplicate_Ignored": continue
        
        raw_comp_code = str(row.get("Topic_Focus", "")).strip()
        if not raw_comp_code: continue # Skip completely empty rows
        comp_code = raw_comp_code.replace("ABM_BM11", "") 
        
        assessment_type = str(row.get("Assessment_Type", "QUIZ")).strip().upper()
        curr = curr_data.get(comp_code)
        if not curr: continue

        # --- HYBRID MODULE / EXAM GENERATION ---
        # Generation does not strictly require a Student_ID, so we use the sheet's Strand_Focus
        if str(row.get("Form_Generation_Status", "")).strip() == "READY":
            strand_focus = str(row.get("Strand_Focus", "ABM")).strip().upper()
            
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
                print(f"[{comp_code}] Generating {missing_count} NEW questions for {strand_focus}...")
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
                print(f"[{comp_code}] Target count met using Item Bank.")
                if has_lecture:
                    vault = fetch_from_vault(sheet_client, comp_code, strand_focus)
                    instruction_body = vault.get('Lecture_Content', '') if vault else ""
            
            try:
                print(f"[{comp_code}] Updating Google Form...")
                form_url = update_dynamic_form(comp_code, instruction_title, instruction_body, combined_quiz, form_service, DYNAMIC_FORM_ID)
                sheet.update_cell(row_idx, headers.index("Form_URL") + 1, form_url)
                sheet.update_cell(row_idx, headers.index("Form_Generation_Status") + 1, "DEPLOYED")
                print(f"✅ {assessment_type} Deployed for {comp_code}")
            except Exception as e: print(f"Form Gen Error: {e}")
            continue

        # --- NATIVE INSTANT GRADING ---
        if str(row.get("Remediation_Status", "")).strip() == "Pending":
            student_id = str(row.get("Student_ID", "")).strip()
            
            # GHOST ROW PROTECTION: Skip if no Student ID
            if not student_id:
                print(f"Skipping Row {row_idx}: No Student_ID (Ghost Row).")
                continue
                
            digital_answers = str(row.get("Digital_Answers", "")).strip()
            if not digital_answers:
                print(f"Skipping Row {row_idx}: No digital answers provided.")
                continue

            # ROSTER VERIFICATION: Authoritative Strand Sync
            profile = fetch_student_from_roster(sheet_client, student_id)
            if not profile:
                print(f"❌ Error: Student ID {student_id} not found in Master Roster!")
                sheet.update_cell(row_idx, headers.index("Remediation_Status") + 1, "Roster_Error")
                continue
            
            strand_focus = str(profile.get("Strand_Focus", "ABM")).strip().upper()
                
            print(f"⚡ Natively Grading Row {row_idx} ({comp_code}) | Authentic Roster Strand: {strand_focus}")
            score, diag_feedback, error = grade_submission_natively(digital_answers, comp_code, strand_focus, sheet_client)
            
            if error:
                print(f"❌ Grading Error: {error}")
                sheet.update_cell(row_idx, headers.index("Remediation_Status") + 1, "Manual_Review")
                sheet.update_cell(row_idx, headers.index("Remediation") + 1, error)
                continue
                
            status = "Excelling" if score >= 90 else "Passing" if score >= curr.get("mastery_threshold", 75) else "Needs Review"
            
            if score == 100:
                vault = fetch_from_vault(sheet_client, comp_code, strand_focus)
                final_feedback = vault.get('Enrichment_Text', "Perfect score! Keep up the great work.") if vault else "Perfect score!"
            elif score < curr.get("mastery_threshold", 75):
                vault = fetch_from_vault(sheet_client, comp_code, strand_focus)
                final_feedback = format_math_text(vault.get('Remediation_Scaffolding', diag_feedback)) if vault else diag_feedback
            else:
                final_feedback = diag_feedback

            try:
                sheet.update_cell(row_idx, headers.index("Score") + 1, score)
                sheet.update_cell(row_idx, headers.index("Remediation") + 1, final_feedback)
                sheet.update_cell(row_idx, headers.index("Remediation_Status") + 1, status)
                print(f"✅ Row {row_idx} complete. Score: {score}%")
            except Exception as e:
                print(f"Grading/Update Error: {e}")

if __name__ == '__main__':
    main()
