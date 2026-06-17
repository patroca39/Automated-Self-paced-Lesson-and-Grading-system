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
# TEMPLATE_FORM_ID is removed. We generate natively now!

ASSESSMENT_RULES = {
    "QUIZ": {"target_count": 20, "display_count": 10, "has_lecture": True},
    "ASSIGNMENT": {"target_count": 20, "display_count": 10, "has_lecture": True},
    "UNIT_TEST": {"target_count": 30, "display_count": 30, "has_lecture": False},
    "MAJOR_EXAM": {"target_count": 40, "display_count": 40, "has_lecture": False, "hard_mode": True} 
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

class LessonContentSchema(BaseModel):
    lesson_title: str
    lecture_content: str
    remediation_scaffolding: str  
    enrichment_scenario: str      

class QuizSchema(BaseModel):
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
    if not text: return ""
    text = str(text)
    text = text.replace('<br>', '\n').replace('<li>', '\n• ').replace('</p>', '\n\n')
    text = text.replace('<ul>', '').replace('</ul>', '').replace('<ol>', '').replace('</ol>', '')
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('**', '').replace('*', '')
    text = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', text)
    replacements = {"\\times": "×", "\\div": "÷", "\\pm": "±", "\\^2": "²", "$": "", "\\": "", "[NEWLINE]": "\n"}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text.strip()

def call_gemini_with_retry(contents, schema_class, retries=4):
    for attempt in range(retries):
        try:
            res = gen_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema_class)
            )
            return schema_class.model_validate_json(res.text)
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e).upper():
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
    headers = bank.row_values(1)
    new_rows = []
    
    clean_headers = [str(h).strip().lower().replace(" ", "_") for h in headers]
    
    for q in mcq_list:
        item_id = f"{comp_code}-{str(uuid.uuid4())[:6]}"
        row_data = {
            "item_id": item_id,
            "topic_focus": comp_code,
            "strand_focus": strand_focus,
            "question": format_math_text(q.question),
            "question_text": format_math_text(q.question),
            "option_a": format_math_text(q.option_a),
            "option_b": format_math_text(q.option_b),
            "option_c": format_math_text(q.option_c),
            "option_d": format_math_text(q.option_d),
            "correct_answer": q.correct_answer.strip().upper(),
            "difficulty": q.difficulty,
            "sub_concept": q.sub_concept,
            "targeted_remediation": format_math_text(q.targeted_remediation),
            "total_attempts": 0,
            "total_correct": 0,
            "difficulty_index": 0.0,
            "item_status": "NEW"
        }
        
        ordered_row = []
        if headers:
            for ch in clean_headers:
                ordered_row.append(row_data.get(ch, ""))
        else:
            ordered_row = list(row_data.values())
            
        new_rows.append(ordered_row)
        
    bank.append_rows(new_rows)

def save_master_lesson(sheet_client, comp_code, strand_focus, lesson_data):
    vault = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Modules_Vault")
    vault.append_row([comp_code, strand_focus, lesson_data.lesson_title, format_math_text(lesson_data.lecture_content), format_math_text(lesson_data.remediation_scaffolding), lesson_data.enrichment_scenario])

def get_lecture_prompt(curr, strand_focus):
    return f"""
    You are an expert master teacher for the {strand_focus} track.
    Create a highly comprehensive, deeply detailed self-paced reading module for this standard:
    Content Domain: {curr.get('content', 'Business Math')}
    Performance Standard: {curr.get('performance_standard', '')}
    Learning Competency: {curr.get('learning_competency', '')}
    
    🛑 FORMATTING RULES (CRITICAL): 
    - NO RAW LATEX ALLOWED. Do NOT use $.
    - USE UNICODE MATH TYPOGRAPHY: Make it look like a math textbook using unicode characters. Use mathematical italics for variables (e.g., 𝑥, 𝑦, 𝑛), proper superscripts for exponents (e.g., 𝑥², 𝑦³), and standard operators (×, ÷, ≤, ≥, ≠).
    - ABSOLUTELY NO HTML TAGS. Do NOT use <br>, <b>, <i>, <ul>, <li>, <sup>, or <sub>. 
    
    🛑 THE SPACING FIX:
    Because JSON strips invisible return keys, you MUST use the exact placeholder [NEWLINE] wherever you want a line break or paragraph break. 
    
    🛑 LENGTH & STYLE MANDATE:
    - The 'lecture_content' MUST be comprehensive, acting as a standalone textbook chapter.
    - Provide at least 3 detailed, step-by-step real-world {strand_focus} business examples.
    - 📺 YOUTUBE INTEGRATION: At the very end of the lecture_content, determine the best possible YouTube search query for this specific math topic, and output the raw URL EXACTLY like this:
      [NEWLINE][NEWLINE]📺 Recommended Video Lessons:[NEWLINE]https://www.youtube.com/results?search_query=YOUR+URL+ENCODED+SEARCH+QUERY+HERE
    """

def get_quiz_prompt(curr, strand_focus, missing_count, tos_rules=None, hard_mode=False):
    difficulty_context = "CRITICAL THINKING & ADVANCED ANALYSIS ONLY." if hard_mode else "Standard high school difficulty."
    base_prompt = """
    🛑 CRITICAL MATH FORMATTING RULES: 
    - NO RAW LATEX ALLOWED. Do NOT use $. 
    - USE UNICODE MATH TYPOGRAPHY.
    - Write fractions cleanly as plain text: a/b (e.g., 1/4).
    """
    
    tos_context = ""
    if tos_rules and "DepEd_TOS_Distribution" in tos_rules:
        dist = tos_rules["DepEd_TOS_Distribution"]
        tos_context = f"""
        🛑 MANDATORY TABLE OF SPECIFICATIONS (TOS) DISTRIBUTION:
        - Remembering & Understanding: {dist.get('Remembering_Understanding', 40)}%
        - Applying & Analyzing: {dist.get('Applying_Analyzing', 40)}%
        - Evaluating & Creating: {dist.get('Evaluating_Creating', 20)}%
        """
    
    return f"""
    Generate EXACTLY {missing_count} brand new MCQs for the following standard ({strand_focus} track):
    Content Domain: {curr.get('content', 'Business Math')}
    Learning Competency: {curr.get('learning_competency', '')}
    
    DIFFICULTY: {difficulty_context}
    {tos_context}
    {base_prompt}
    🛑 MANDATORY: You MUST output exactly {missing_count} items in your JSON array. No more, no less.
    """

# --- THE FIX: NATIVE FORM GENERATION ---
def deploy_fresh_form(comp_code, instruction_title, instruction_body, combined_quiz, drive_service, form_service):
    # 1. Create a brand new form natively via Forms API (Guaranteed active state)
    new_form = form_service.forms().create(body={
        "info": {
            "title": f"{comp_code} Automated Assessment",
            "documentTitle": f"{comp_code} Automated Assessment"
        }
    }).execute()
    
    new_form_id = new_form['formId']
    
    # 2. Make it public via Drive API
    drive_service.permissions().create(
        fileId=new_form_id,
        body={'type': 'anyone', 'role': 'reader'}
    ).execute()
    
    requests = []
    current_index = 0
    
    # 3. Inject Harvester Gate
    requests.append({
        "createItem": {
            "item": {
                "title": "Enter your Student ID (Required for Grading):",
                "questionItem": {
                    "question": {
                        "required": True,
                        "textQuestion": {"paragraph": False}
                    }
                }
            },
            "location": {"index": current_index}
        }
    })
    current_index += 1
    
    if instruction_body:
        requests.append({"createItem": {"item": {"title": instruction_title, "description": format_math_text(instruction_body), "textItem": {}}, "location": {"index": current_index}}})
        current_index += 1
    
    for i, q in enumerate(combined_quiz):
        if isinstance(q, dict):
            safe_q = {str(k).strip().lower().replace(" ", "_"): str(v) for k, v in q.items()}
            q_text = format_math_text(safe_q.get('question', safe_q.get('question_text', '')))
            opts = [
                f"A) {format_math_text(safe_q.get('option_a', ''))}", 
                f"B) {format_math_text(safe_q.get('option_b', ''))}", 
                f"C) {format_math_text(safe_q.get('option_c', ''))}", 
                f"D) {format_math_text(safe_q.get('option_d', ''))}"
            ]
        else:
            q_text = format_math_text(q.question)
            opts = [f"A) {format_math_text(q.option_a)}", f"B) {format_math_text(q.option_b)}", f"C) {format_math_text(q.option_c)}", f"D) {format_math_text(q.option_d)}"]

        if not q_text or len(q_text) < 2:
            q_text = f"Question {i+1} [Error: Question text was blank in database]"

        requests.append({
            "createItem": {
                "item": {
                    "title": q_text, 
                    "questionItem": {"question": {"required": True, "choiceQuestion": {"type": "RADIO", "options": [{"value": o} for o in opts]}}}
                },
                "location": {"index": current_index}
            }
        })
        current_index += 1

    form_service.forms().batchUpdate(formId=new_form_id, body={"requests": requests}).execute()
    return f"https://docs.google.com/forms/d/{new_form_id}/viewform"

def harvest_responses(form_service, form_url):
    if not form_url or "forms/d/" not in form_url: return {}
    
    form_id = form_url.split("forms/d/")[1].split("/")[0]
    harvested_answers = {}
    
    try:
        form = form_service.forms().get(formId=form_id).execute()
        items = form.get('items', [])
        
        student_id_qId = None
        q_id_to_index = {}
        mcq_index = 0
        
        for item in items:
            if 'questionItem' in item:
                qId = item['questionItem']['question']['questionId']
                title = item.get('title', '').lower()
                if "student id" in title:
                    student_id_qId = qId
                else:
                    q_id_to_index[qId] = mcq_index
                    mcq_index += 1
                    
        resp_data = form_service.forms().responses().list(formId=form_id).execute()
        responses = resp_data.get('responses', [])
        
        for resp in responses:
            answers = resp.get('answers', {})
            if student_id_qId not in answers: continue
            
            raw_id = answers[student_id_qId].get('textAnswers', {}).get('answers', [{'value': ''}])[0].get('value', '').strip()
            if not raw_id: continue
            
            choices = ["MISSING"] * len(q_id_to_index)
            for qId, ans_obj in answers.items():
                if qId == student_id_qId: continue
                if qId in q_id_to_index:
                    idx = q_id_to_index[qId]
                    ans_val = ans_obj.get('textAnswers', {}).get('answers', [{'value': ''}])[0].get('value', '')
                    match = re.search(r'^([A-D])\)', str(ans_val).upper())
                    if match:
                        choices[idx] = match.group(1)
                    else:
                        choices[idx] = str(ans_val)[0].upper() if ans_val else "MISSING"
                        
            harvested_answers[raw_id] = ", ".join(choices)
            
        return harvested_answers
    except Exception as e:
        print(f"Harvester Error on {form_id}: {e}")
        return {}

def grade_submission_natively(student_answers_str, comp_code, strand_focus, sheet_client):
    bank = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    all_data = bank.get_all_values()
    headers = all_data[0]
    
    answer_keys = []
    for idx, row in enumerate(all_data[1:], start=2):
        if str(row[headers.index("Topic_Focus")]).strip().upper() == comp_code.strip().upper() and \
           str(row[headers.index("Strand_Focus")]).strip().upper() == strand_focus.strip().upper():
            item_dict = dict(zip(headers, row))
            item_dict['_row_idx'] = idx 
            answer_keys.append(item_dict)
            
    if not answer_keys: return None, "", f"Error: Answer keys not found for {comp_code} ({strand_focus})."
        
    student_choices = re.findall(r'([A-D])\)', student_answers_str.upper()) or re.findall(r'\b([A-D])\b', student_answers_str.upper())
    student_choices += ['MISSING'] * max(0, len(answer_keys) - len(student_choices))

    correct_count = 0
    feedback_blocks = []
    cell_updates = []
    
    for idx, item in enumerate(answer_keys):
        safe_item = {str(k).strip().lower().replace(" ", "_"): v for k, v in item.items()}
        
        ans = str(safe_item.get('correct_answer', '')).strip().upper()
        row_idx = item['_row_idx']
        
        try: attempts = int(safe_item.get('total_attempts', 0))
        except ValueError: attempts = 0
        try: corrects = int(safe_item.get('total_correct', 0))
        except ValueError: corrects = 0
        
        attempts += 1
        
        if idx < len(student_choices) and student_choices[idx] == ans:
            correct_count += 1
            corrects += 1
        else:
            sub = safe_item.get('sub_concept', f'Concept {idx+1}')
            rem = safe_item.get('targeted_remediation', 'Review this concept.')
            feedback_blocks.append(f"• {sub}: {rem}")

        p_index = round(corrects / attempts, 2) if attempts > 0 else 0.0
        
        if p_index < 0.26:
            status = "REVISE (Too Hard)"
        elif p_index > 0.75:
            status = "REVISE (Too Easy)"
        else:
            status = "RETAIN (Good)"
            
        if "Total_Attempts" in headers:
            cell_updates.append(gspread.Cell(row_idx, headers.index("Total_Attempts") + 1, attempts))
            cell_updates.append(gspread.Cell(row_idx, headers.index("Total_Correct") + 1, corrects))
            cell_updates.append(gspread.Cell(row_idx, headers.index("Difficulty_Index") + 1, p_index))
            cell_updates.append(gspread.Cell(row_idx, headers.index("Item_Status") + 1, status))

    score = int((correct_count / len(answer_keys)) * 100)
    
    if cell_updates:
        bank.update_cells(cell_updates)
        
    return score, format_math_text("\n".join(feedback_blocks)), None

def main():
    print("Initializing Circular Grader System (V2.1)...")
    sheet_client, drive_service, form_service = get_google_services()
    with open("curriculum_guide.json", "r") as f: curr_data = json.load(f)["ABM_BM11"]
    
    tos_rules = None
    if os.path.exists("item_analysis_rules.json"):
        with open("item_analysis_rules.json", "r") as f:
            tos_rules = json.load(f)
    
    sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Skill_Analytics")
    all_values = sheet.get_all_values()
    headers = all_values[0]
    all_records = [dict(zip(headers, row)) for row in all_values[1:]]

    global_harvest_cache = {}

    for row_idx, row in enumerate(all_records, start=2):
        raw_comp_code = str(row.get("Topic_Focus", "")).strip()
        if not raw_comp_code: continue
        comp_code = raw_comp_code.replace("ABM_BM11", "") 
        assessment_type = str(row.get("Assessment_Type", "QUIZ")).strip().upper()
        curr = curr_data.get(comp_code)
        if not curr: continue

        if str(row.get("Form_Generation_Status", "")).strip() == "READY":
            strand_focus = str(row.get("Strand_Focus", "ABM")).strip().upper()
            rules = ASSESSMENT_RULES.get(assessment_type, ASSESSMENT_RULES["QUIZ"])
            
            banked_questions = fetch_banked_questions(sheet_client, comp_code, strand_focus)
            missing_count = max(0, rules["target_count"] - len(banked_questions))
            
            combined_quiz = banked_questions.copy()
            instruction_title = "📖 Reading Module" if rules["has_lecture"] else "📝 Exam Instructions"
            instruction_body = ""

            if missing_count > 0:
                if rules["has_lecture"]:
                    print(f"Calling Gemini (1/2): Generating comprehensive lecture for {comp_code}...")
                    lec_prompt = get_lecture_prompt(curr, strand_focus)
                    lesson_data = call_gemini_with_retry(lec_prompt, LessonContentSchema)
                    
                    print(f"Calling Gemini (2/2): Generating {missing_count}-item quiz bank...")
                    qz_prompt = get_quiz_prompt(curr, strand_focus, missing_count, tos_rules, rules.get("hard_mode", False))
                    quiz_data = call_gemini_with_retry(qz_prompt, QuizSchema)
                    
                    if lesson_data and quiz_data:
                        save_master_lesson(sheet_client, comp_code, strand_focus, lesson_data)
                        save_items_to_bank(sheet_client, comp_code, strand_focus, quiz_data.quiz)
                        combined_quiz.extend(quiz_data.quiz)
                        instruction_body = lesson_data.lecture_content
                else:
                    print(f"Calling Gemini: Generating {missing_count}-item exam bank for {comp_code}...")
                    qz_prompt = get_quiz_prompt(curr, strand_focus, missing_count, tos_rules, rules.get("hard_mode", False))
                    exam_data = call_gemini_with_retry(qz_prompt, QuizSchema)
                    
                    if exam_data:
                        save_items_to_bank(sheet_client, comp_code, strand_focus, exam_data.quiz)
                        combined_quiz.extend(exam_data.quiz)
                        instruction_body = "Please read each question carefully and select the best answer. No calculators allowed."
            else:
                if rules["has_lecture"]:
                    vault = fetch_from_vault(sheet_client, comp_code, strand_focus)
                    instruction_body = vault.get('Lecture_Content', '') if vault else ""
                else:
                    instruction_body = "Please read each question carefully and select the best answer. No calculators allowed."

            try:
                try_count = int(row.get("Tries", 1))
            except ValueError:
                try_count = 1
                
            display_limit = rules.get("display_count", 10)
            
            if try_count == 1:
                final_form_quiz = combined_quiz[:display_limit]
            elif try_count == 2:
                final_form_quiz = combined_quiz[display_limit:(display_limit*2)]
            else:
                final_form_quiz = combined_quiz[:display_limit]

            try:
                # The New V2 Native Generation
                form_url = deploy_fresh_form(comp_code, instruction_title, instruction_body, final_form_quiz, drive_service, form_service)
                
                update_payload = [
                    gspread.Cell(row_idx, headers.index("Form_URL") + 1, form_url),
                    gspread.Cell(row_idx, headers.index("Form_Generation_Status") + 1, "DEPLOYED")
                ]
                sheet.update_cells(update_payload)
                
                print(f"Form Natively Generated and Deployed for {comp_code}. Handing off to n8n for email distribution.")
            except Exception as e: print(f"Form Gen Error: {e}")
            continue

        if str(row.get("Remediation_Status", "")).strip() == "Pending":
            student_id = str(row.get("Student_ID", "")).strip()
            form_url = str(row.get("Form_URL", "")).strip()
            if not student_id: continue 
                
            digital_answers = str(row.get("Digital_Answers", "")).strip()
            
            if not digital_answers and form_url:
                if form_url not in global_harvest_cache:
                    global_harvest_cache[form_url] = harvest_responses(form_service, form_url)
                
                if student_id in global_harvest_cache[form_url]:
                    digital_answers = global_harvest_cache[form_url][student_id]
                    sheet.update_cell(row_idx, headers.index("Digital_Answers") + 1, digital_answers)

            if not digital_answers: continue

            profile = fetch_student_from_roster(sheet_client, student_id)
            if not profile:
                sheet.update_cell(row_idx, headers.index("Remediation_Status") + 1, "Roster_Error")
                continue
            
            strand_focus = str(profile.get("Strand_Focus", "ABM")).strip().upper()
            
            try:
                current_tries = int(row.get("Tries", 1))
            except ValueError:
                current_tries = 1

            print(f"Grading Submission: Student {student_id} | Attempt #{current_tries}")
            score, diag_feedback, error = grade_submission_natively(digital_answers, comp_code, strand_focus, sheet_client)
            
            if error:
                sheet.update_cell(row_idx, headers.index("Remediation_Status") + 1, "Manual_Review")
                continue
                
            mastery_threshold = curr.get("mastery_threshold", 75)
            
            if score >= mastery_threshold:
                status = "Excelling" if score >= 90 else "Passing"
                vault = fetch_from_vault(sheet_client, comp_code, strand_focus)
                final_feedback = vault.get('Enrichment_Text', "Passed successfully!") if vault else "Passed successfully!"
            else:
                status = "Needs Review"
                vault = fetch_from_vault(sheet_client, comp_code, strand_focus)
                final_feedback = format_math_text(vault.get('Remediation_Scaffolding', diag_feedback)) if vault else diag_feedback

            try:
                update_payload = [
                    gspread.Cell(row_idx, headers.index("Score") + 1, score),
                    gspread.Cell(row_idx, headers.index("Remediation") + 1, final_feedback)
                ]
                
                if status == "Needs Review" and current_tries < 2:
                    update_payload.append(gspread.Cell(row_idx, headers.index("Remediation_Status") + 1, "Needs Review_Trigger"))
                    
                    if "Tries" in headers:
                        update_payload.append(gspread.Cell(row_idx, headers.index("Tries") + 1, current_tries + 1))
                else:
                    update_payload.append(gspread.Cell(row_idx, headers.index("Remediation_Status") + 1, status))
                
                sheet.update_cells(update_payload)
                    
                print(f"Processed grading row successfully. Status set to: {status}")
            except Exception as e: print(f"Update Error: {e}")

if __name__ == '__main__':
    main()
