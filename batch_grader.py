import os
import time
import json
import re
import uuid
import argparse
import datetime
import gspread
import pandas as pd
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from google import genai
from google.genai import types

# --- CONFIGURATION ---
gen_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# Matched exactly to DepEd SHS Memorandum No. 001, s. 2026 constraints
ASSESSMENT_RULES = {
    "QUIZ": {"target_count": 20, "display_count": 10, "has_lecture": True},
    "SUMMATIVE_TEST": {"target_count": 40, "display_count": 30, "has_lecture": False}, 
    "TERM_EXAM": {"target_count": 80, "display_count": 60, "has_lecture": False, "hard_mode": True} 
}

# --- TIME-GATED EXAM SCHEDULE (From DepEd Memo No. 001, s 2026) ---
EXAM_SCHEDULE = [
    # Term 1
    {"start": "2026-07-06", "end": "2026-07-07", "exam_type": "SUMMATIVE_TEST", "topic_code": "TERM1_SUMMATIVE_1"},
    {"start": "2026-07-28", "end": "2026-07-29", "exam_type": "SUMMATIVE_TEST", "topic_code": "TERM1_SUMMATIVE_2"},
    {"start": "2026-08-28", "end": "2026-09-02", "exam_type": "TERM_EXAM", "topic_code": "TERM1_FINAL_EXAM"},
    # Term 2
    {"start": "2026-10-07", "end": "2026-10-08", "exam_type": "SUMMATIVE_TEST", "topic_code": "TERM2_SUMMATIVE_1"},
    {"start": "2026-10-29", "end": "2026-10-30", "exam_type": "SUMMATIVE_TEST", "topic_code": "TERM2_SUMMATIVE_2"},
    {"start": "2026-12-03", "end": "2026-12-04", "exam_type": "TERM_EXAM", "topic_code": "TERM2_FINAL_EXAM"},
    # Term 3
    {"start": "2027-01-25", "end": "2027-01-26", "exam_type": "SUMMATIVE_TEST", "topic_code": "TERM3_SUMMATIVE_1"},
    {"start": "2027-02-26", "end": "2027-02-27", "exam_type": "SUMMATIVE_TEST", "topic_code": "TERM3_SUMMATIVE_2"},
    {"start": "2027-03-22", "end": "2027-03-24", "exam_type": "TERM_EXAM", "topic_code": "TERM3_FINAL_EXAM"} # Grade 11 Schedule
]

def get_active_scheduled_exam():
    """Checks if today falls within a mandated examination window."""
    today = datetime.date.today().isoformat()
    for exam in EXAM_SCHEDULE:
        if exam["start"] <= today <= exam["end"]:
            return exam
    return None

# --- DIAGNOSTIC SCHEMAS (INCLUDES SLIDES) ---
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

class Slide(BaseModel):
    title: str
    bullet_points: list[str]

class PresentationSet(BaseModel):
    core_slides: list[Slide]
    remedial_slides: list[Slide]
    advanced_slides: list[Slide]

class LessonContentSchema(BaseModel):
    lesson_title: str
    lecture_content: str
    remediation_scaffolding: str  
    enrichment_scenario: str      
    visual_decks: PresentationSet

class QuizSchema(BaseModel):
    quiz: list[MCQ]

# --- AI NAVIGATOR TOOL ---
def update_student_progress(selected_topic_code: str, reasoning: str, is_course_complete: bool):
    """
    Updates the student's progress in the database based on their assessment score.
    """
    pass

# --- GOOGLE AUTHENTICATION ---
def get_google_services():
    token_dict = json.loads(os.environ.get('GOOGLE_TOKEN_JSON'))
    creds = Credentials.from_authorized_user_info(token_dict)
    return (
        gspread.authorize(creds), 
        build('drive', 'v3', credentials=creds), 
        build('forms', 'v1', credentials=creds),
        build('slides', 'v1', credentials=creds)
    )

# --- SAFE RETRY WRAPPER ---
def safe_sheet_action(action_func, *args, **kwargs):
    for attempt in range(4):
        try:
            return action_func(*args, **kwargs)
        except Exception as e:
            print(f"⚠️ Sheets API Rate Limit. Pausing 15s... (Attempt {attempt+1}/4)")
            time.sleep(15)
    print("❌ CRITICAL: Google Sheets API failed after 4 retries.")
    return None

# --- CONTEXTUAL PROFILE FETCHER ---
def find_student_profile(context_data, target_specialization):
    """Recursively searches the nested JSON to find the student's specific TVL or Academic track profile."""
    if target_specialization == "DEFAULT": return context_data.get("DEFAULT")
    
    for key, value in context_data.items():
        if key == target_specialization:
            return value
        if isinstance(value, dict):
            found = find_student_profile(value, target_specialization)
            if found: return found
            
    return context_data.get("DEFAULT", {})

# --- MEMORY CACHE FUNCTIONS ---
def fetch_student_from_roster(roster_data, student_id):
    for r in roster_data:
        if str(r.get('Student_ID', '')).strip() == str(student_id).strip():
            return r
    return None

def fetch_from_vault(vault_data, comp_code, strand_focus):
    for r in vault_data:
        if str(r.get('Topic_Focus', '')).strip().upper() == comp_code.strip().upper() and str(r.get('Strand_Focus', '')).strip().upper() == strand_focus.strip().upper():
            return r
    return None

def fetch_from_deployments(deploy_data, comp_code, strand_focus, try_count):
    for r in deploy_data:
        if str(r.get('Topic_Focus', '')).strip().upper() == comp_code.strip().upper() and \
           str(r.get('Strand_Focus', '')).strip().upper() == strand_focus.strip().upper() and \
           str(r.get('Tries', '1')) == str(try_count):
            return r
    return None

def save_to_deployments(deploy_sheet, deploy_data, comp_code, strand_focus, try_count, form_url, core_url, rem_url, adv_url):
    new_row = [comp_code, strand_focus, try_count, form_url, core_url, rem_url, adv_url]
    safe_sheet_action(deploy_sheet.append_row, new_row)
    deploy_data.append({
        "Topic_Focus": comp_code, "Strand_Focus": strand_focus, "Tries": try_count,
        "Form_URL": form_url, "Core_Slides": core_url, "Remedial_Slides": rem_url, "Advanced_Slides": adv_url
    })

def fetch_banked_questions(bank_data, comp_code, strand_focus):
    headers = bank_data[0]
    records = [dict(zip(headers, row)) for row in bank_data[1:]]
    return [item for item in records if str(item.get('Topic_Focus', '')).strip().upper() == comp_code.strip().upper() and str(item.get('Strand_Focus', '')).strip().upper() == strand_focus.strip().upper()]

def save_items_to_bank(bank_sheet, bank_data, comp_code, strand_focus, mcq_list):
    headers = bank_data[0]
    new_rows = []
    clean_headers = [str(h).strip().lower().replace(" ", "_") for h in headers]
    
    for q in mcq_list:
        item_id = f"{comp_code}-{str(uuid.uuid4())[:6]}"
        row_data = {
            "item_id": item_id, "topic_focus": comp_code, "strand_focus": strand_focus,
            "question": format_math_text(q.question), "question_text": format_math_text(q.question),
            "option_a": format_math_text(q.option_a), "option_b": format_math_text(q.option_b),
            "option_c": format_math_text(q.option_c), "option_d": format_math_text(q.option_d),
            "correct_answer": q.correct_answer.strip().upper(), "difficulty": q.difficulty,
            "sub_concept": q.sub_concept, "targeted_remediation": format_math_text(q.targeted_remediation),
            "total_attempts": 0, "total_correct": 0, "difficulty_index": 0.0, "item_status": "NEW"
        }
        ordered_row = [row_data.get(ch, "") for ch in clean_headers] if headers else list(row_data.values())
        new_rows.append(ordered_row)
        
    safe_sheet_action(bank_sheet.append_rows, new_rows)
    bank_data.extend(new_rows)

def save_master_lesson(vault_sheet, vault_data, comp_code, strand_focus, lesson_data, core_url, rem_url, adv_url):
    new_row = [
        comp_code, strand_focus, lesson_data.lesson_title, 
        format_math_text(lesson_data.lecture_content), format_math_text(lesson_data.remediation_scaffolding), 
        lesson_data.enrichment_scenario, core_url, rem_url, adv_url
    ]
    safe_sheet_action(vault_sheet.append_row, new_row)
    
    vault_data.append({
        "Topic_Focus": comp_code, "Strand_Focus": strand_focus, "Lesson_Title": lesson_data.lesson_title,
        "Lecture_Content": format_math_text(lesson_data.lecture_content), "Remediation_Scaffolding": format_math_text(lesson_data.remediation_scaffolding),
        "Enrichment_Scenario": lesson_data.enrichment_scenario, "Core_Slides": core_url, "Remedial_Slides": rem_url, "Advanced_Slides": adv_url
    })

def append_to_performance_log(log_sheet, student_id, comp_code, score, feedback):
    try:
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        safe_sheet_action(log_sheet.append_row, [current_time, student_id, comp_code, score, f"{score}%", feedback])
    except Exception as e:
        print(f"⚠️ Failed to write to Performance Log: {e}")

def format_math_text(text):
    if not text: return ""
    text = str(text).replace('<br>', '\n').replace('<li>', '\n• ').replace('</p>', '\n\n')
    text = text.replace('<ul>', '').replace('</ul>', '').replace('<ol>', '').replace('</ol>', '')
    text = re.sub(r'<[^>]+>', '', text).replace('**', '').replace('*', '')
    text = re.sub(r'\\frac\{([^}]+)\}\{([^}]+)\}', r'\1/\2', text)
    replacements = {"\\times": "×", "\\div": "÷", "\\pm": "±", "\\^2": "²", "$": "", "\\": "", "[NEWLINE]": "\n"}
    for old, new in replacements.items(): text = text.replace(old, new)
    return text.strip()

def call_gemini_with_retry(contents, schema_class, retries=4):
    for attempt in range(retries):
        try:
            res = gen_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=contents,
                config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=schema_class)
            )
            raw_json = res.text.strip()
            
            code_block_marker = "```"
            json_block_marker = "```json"
            
            if raw_json.startswith(json_block_marker):
                raw_json = raw_json[7:-3].strip()
            elif raw_json.startswith(code_block_marker):
                raw_json = raw_json[3:-3].strip()
                
            return schema_class.model_validate_json(raw_json)
        except Exception as e:
            print(f"⚠️ Gemini API Error (Attempt {attempt+1}/{retries}): {e}")
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e).upper():
                time.sleep(50) 
            else:
                time.sleep(35)
    return None

# --- PROMPT ENGINES ---
def get_lecture_prompt(curr, strand_focus, context_profile):
    return f"""
    You are an expert master teacher for the {strand_focus} track.
    Create a highly comprehensive reading module AND 3 tiered slide decks (Core, Remedial, Advanced) for:
    Content Domain: {curr.get('content', 'Mathematics')}
    Learning Competency: {curr.get('learning_competency', '')}
    
    🛑 CONTEXTUALIZATION RULES (CRITICAL):
    Target Audience: {context_profile.get('description', '')}
    Teaching Strategy: {context_profile.get('teaching_strategy', '')}
    Real-World Context: ALL examples, scenarios, and word problems MUST utilize: {context_profile.get('real_world_context', '')}.
    
    🛑 FORMATTING RULES (CRITICAL): 
    - NO RAW LATEX ALLOWED. Do NOT use $.
    - USE UNICODE MATH TYPOGRAPHY: Make it look like a math textbook using unicode characters.
    - ABSOLUTELY NO HTML TAGS. Do NOT use <br>, <b>, <i>, <ul>, <li>, <sup>, or <sub>. 
    - Because JSON strips invisible return keys, you MUST use the exact placeholder [NEWLINE] wherever you want a line break or paragraph break. 
    
    🛑 SLIDE RULES: Provide 3-5 slides per tier. Keep bullet points concise and impactful.
    """

def get_quiz_prompt(curr, strand_focus, missing_count, context_profile, tos_rules=None, hard_mode=False):
    difficulty_context = "CRITICAL THINKING & ADVANCED ANALYSIS ONLY." if hard_mode else "Standard high school difficulty."
    base_prompt = "🛑 CRITICAL MATH FORMATTING RULES: NO RAW LATEX ALLOWED. Do NOT use $. USE UNICODE MATH TYPOGRAPHY. Write fractions cleanly as plain text: a/b."
    
    tos_context = ""
    if tos_rules and "DepEd_TOS_Distribution" in tos_rules:
        dist = tos_rules["DepEd_TOS_Distribution"]
        tos_context = f"🛑 MANDATORY TABLE OF SPECIFICATIONS (TOS) DISTRIBUTION: Remembering & Understanding: {dist.get('Remembering_Understanding', 40)}% | Applying & Analyzing: {dist.get('Applying_Analyzing', 40)}% | Evaluating & Creating: {dist.get('Evaluating_Creating', 20)}%"
    
    return f"""
    Generate EXACTLY {missing_count} brand new MCQs for the following standard ({strand_focus} track):
    Content Domain: {curr.get('content', 'Mathematics')}
    Learning Competency: {curr.get('learning_competency', '')}
    DIFFICULTY: {difficulty_context}
    
    🛑 CONTEXTUALIZATION RULES (CRITICAL):
    Real-World Context: Frame the math word problems using scenarios related to: {context_profile.get('real_world_context', '')}. Make it highly relevant to their track!
    
    {tos_context}
    {base_prompt}
    🛑 MANDATORY: You MUST output exactly {missing_count} items in your JSON array. No more, no less.
    🛑 CRITICAL INSTRUCTION: For 'correct_answer', output ONLY the single uppercase letter (A, B, C, or D). Do not write the full answer text!
    """

def get_navigator_prompt(student_id, recent_score, current_topic, curr_data):
    map_context = json.dumps(curr_data, indent=2)
    return f"""
    You are an automated Curriculum Navigator for a Senior High School class.
    
    STUDENT CONTEXT:
    - Student ID: {student_id}
    - Just completed topic: {current_topic}
    - Score on that topic: {recent_score}%
    
    CURRICULUM MAP:
    {map_context}
    
    YOUR MISSION:
    Look at the curriculum map and the student's score. 
    1. If the score is >= 75%, select the next logical topic based on the 'prerequisites' and 'difficulty_level'.
    2. If the score is incredibly high (>= 95%), check if they can skip a basic topic and go straight to an advanced one.
    3. If there are no more topics left to take, set 'is_course_complete' to true.
    
    Use the `update_student_progress` tool to enact your decision.
    """

# --- THE SLIDE BUILDER ENGINE ---
def create_google_slides(comp_code, tier_name, slide_data, drive_service, slides_service):
    print(f"🎨 Building {tier_name} Slides for {comp_code}...")
    presentation = slides_service.presentations().create(body={'title': f"{comp_code} {tier_name} Visual Lecture"}).execute()
    pres_id = presentation.get('presentationId')
    
    requests = []
    for i, slide in enumerate(slide_data):
        slide_id = f"s_{i}_{uuid.uuid4().hex[:6]}"
        title_id = f"t_{i}_{uuid.uuid4().hex[:6]}"
        body_id = f"b_{i}_{uuid.uuid4().hex[:6]}"
        
        requests.append({
            'createSlide': {
                'objectId': slide_id,
                'slideLayoutReference': {'predefinedLayout': 'TITLE_AND_BODY'},
                'placeholderIdMappings': [
                    {'layoutPlaceholder': {'type': 'TITLE', 'index': 0}, 'objectId': title_id},
                    {'layoutPlaceholder': {'type': 'BODY', 'index': 0}, 'objectId': body_id}
                ]
            }
        })
        
        bullet_text = "\n".join([f"• {format_math_text(p)}" for p in slide.bullet_points])
        requests.append({'insertText': {'objectId': title_id, 'text': format_math_text(slide.title)}})
        requests.append({'insertText': {'objectId': body_id, 'text': bullet_text}})

    default_slide_id = presentation.get('slides')[0].get('objectId')
    requests.append({'deleteObject': {'objectId': default_slide_id}})

    slides_service.presentations().batchUpdate(presentationId=pres_id, body={'requests': requests}).execute()
    drive_service.permissions().create(fileId=pres_id, body={'type': 'anyone', 'role': 'reader'}).execute()
    
    return f"https://docs.google.com/presentation/d/{pres_id}/export/pptx"

# --- THE FORM BUILDER ---
def deploy_fresh_form(comp_code, instruction_title, instruction_body, combined_quiz, drive_service, form_service):
    new_form = form_service.forms().create(body={"info": {"title": f"{comp_code} Automated Assessment", "documentTitle": f"{comp_code} Automated Assessment"}}).execute()
    new_form_id = new_form['formId']
    drive_service.permissions().create(fileId=new_form_id, body={'type': 'anyone', 'role': 'reader'}).execute()
    
    requests = []
    current_index = 0
    requests.append({"createItem": {"item": {"title": "Enter your Student ID (Required for Grading):", "questionItem": {"question": {"required": True, "textQuestion": {"paragraph": False}}}}, "location": {"index": current_index}}})
    current_index += 1
    
    if instruction_body:
        requests.append({"createItem": {"item": {"title": instruction_title, "description": format_math_text(instruction_body), "textItem": {}}, "location": {"index": current_index}}})
        current_index += 1
    
    for i, q in enumerate(combined_quiz):
        safe_q = {str(k).strip().lower().replace(" ", "_"): str(v) for k, v in q.items()} if isinstance(q, dict) else q.__dict__
        q_text = format_math_text(safe_q.get('question', safe_q.get('question_text', '')))
        opts = [f"A) {format_math_text(safe_q.get('option_a', ''))}", f"B) {format_math_text(safe_q.get('option_b', ''))}", f"C) {format_math_text(safe_q.get('option_c', ''))}", f"D) {format_math_text(safe_q.get('option_d', ''))}"]
        if not q_text or len(q_text) < 2: q_text = f"Question {i+1} [Error: Question text was blank in database]"
        requests.append({"createItem": {"item": {"title": q_text, "questionItem": {"question": {"required": True, "choiceQuestion": {"type": "RADIO", "options": [{"value": o} for o in opts]}}}}, "location": {"index": current_index}}})
        current_index += 1

    form_service.forms().batchUpdate(formId=new_form_id, body={"requests": requests}).execute()
    return f"https://docs.google.com/forms/d/{new_form_id}/viewform"

# --- HARVESTER & GRADER ---
def harvest_responses(form_service, form_url):
    if not form_url or "forms/d/" not in form_url: return {}
    form_id = form_url.split("forms/d/")[1].split("/")[0]
    harvested_answers = {}
    try:
        form = form_service.forms().get(formId=form_id).execute()
        student_id_qId = None
        q_id_to_index = {}
        mcq_index = 0
        for item in form.get('items', []):
            if 'questionItem' in item:
                qId = item['questionItem']['question']['questionId']
                if "student id" in item.get('title', '').lower(): student_id_qId = qId
                else:
                    q_id_to_index[qId] = mcq_index
                    mcq_index += 1
                    
        responses = form_service.forms().responses().list(formId=form_id).execute().get('responses', [])
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
                    choices[idx] = match.group(1) if match else (str(ans_val)[0].upper() if ans_val else "MISSING")
            harvested_answers[raw_id] = ", ".join(choices)
        return harvested_answers
    except Exception as e: print(f"Harvester Error on {form_id}: {e}")
    return {}

def grade_submission_natively(student_answers_str, comp_code, strand_focus, bank_sheet, bank_data, try_count, display_limit):
    headers = bank_data[0]
    all_keys = []
    
    for idx, row in enumerate(bank_data[1:], start=2):
        if str(row[headers.index("Topic_Focus")]).strip().upper() == comp_code.strip().upper() and str(row[headers.index("Strand_Focus")]).strip().upper() == strand_focus.strip().upper():
            item_dict = dict(zip(headers, row))
            item_dict['_row_idx'] = idx 
            all_keys.append(item_dict)
            
    if not all_keys: return None, "", f"Error: Answer keys not found for {comp_code} ({strand_focus})."
    
    if try_count == 1: answer_keys = all_keys[:display_limit]
    elif try_count == 2: answer_keys = all_keys[display_limit:(display_limit*2)]
    else: answer_keys = all_keys[:display_limit]
        
    student_choices = [s.strip().upper() for s in student_answers_str.split(',')]
    student_choices += ['MISSING'] * max(0, len(answer_keys) - len(student_choices))

    correct_count = 0
    feedback_blocks = []
    cell_updates = []
    
    for idx, item in enumerate(answer_keys):
        safe_item = {str(k).strip().lower().replace(" ", "_"): v for k, v in item.items()}
        ans = str(safe_item.get('correct_answer', '')).strip().upper()
        
        if ans not in ['A', 'B', 'C', 'D']:
            if ans == str(safe_item.get('option_a', '')).strip().upper(): ans = 'A'
            elif ans == str(safe_item.get('option_b', '')).strip().upper(): ans = 'B'
            elif ans == str(safe_item.get('option_c', '')).strip().upper(): ans = 'C'
            elif ans == str(safe_item.get('option_d', '')).strip().upper(): ans = 'D'
            else:
                match = re.search(r'\b([A-D])\b', ans)
                if match: ans = match.group(1)

        row_idx = item['_row_idx']
        attempts, corrects = int(safe_item.get('total_attempts', 0) or 0), int(safe_item.get('total_correct', 0) or 0)
        attempts += 1
        
        if idx < len(student_choices) and student_choices[idx] == ans:
            correct_count += 1
            corrects += 1
        else:
            sub = safe_item.get('sub_concept', f'Concept {idx+1}')
            rem = safe_item.get('targeted_remediation', 'Review this concept.')
            feedback_blocks.append(f"• {sub}: {rem}")

        p_index = round(corrects / attempts, 2) if attempts > 0 else 0.0
        status = "REVISE (Too Hard)" if p_index < 0.26 else ("REVISE (Too Easy)" if p_index > 0.75 else "RETAIN (Good)")
        
        if "Total_Attempts" in headers:
            bank_data[row_idx - 1][headers.index("Total_Attempts")] = attempts
            bank_data[row_idx - 1][headers.index("Total_Correct")] = corrects
            bank_data[row_idx - 1][headers.index("Difficulty_Index")] = p_index
            bank_data[row_idx - 1][headers.index("Item_Status")] = status

            cell_updates.append(gspread.Cell(row_idx, headers.index("Total_Attempts") + 1, attempts))
            cell_updates.append(gspread.Cell(row_idx, headers.index("Total_Correct") + 1, corrects))
            cell_updates.append(gspread.Cell(row_idx, headers.index("Difficulty_Index") + 1, p_index))
            cell_updates.append(gspread.Cell(row_idx, headers.index("Item_Status") + 1, status))

    score = int((correct_count / len(answer_keys)) * 100)
    if cell_updates: safe_sheet_action(bank_sheet.update_cells, cell_updates)
    
    unique_feedback = list(dict.fromkeys(feedback_blocks))
    if len(unique_feedback) > 5:
        unique_feedback = unique_feedback[:5]
        unique_feedback.append("• ...and additional concepts. Please review the visual slides attached!")
        
    return score, format_math_text("\n".join(unique_feedback)), None

# --- MAIN LOOP ---
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["all", "deploy", "grade"], default="all")
    args = parser.parse_args()
    run_mode = args.mode

    print(f"Initializing Circular Grader System (V3.4 - Contextualization Edition) [MODE: {run_mode.upper()}]...")
    sheet_client, drive_service, form_service, slides_service = get_google_services()
    
    # 🔄 UPDATED PATHS: Pulling from the newly structured files
    with open("busmath_cur.json", "r") as f: bm_data = json.load(f)
    with open("genmath_cur.json", "r") as f: gm_data = json.load(f)
    
    try:
        with open("contextualization_profile.json", "r") as f: context_data = json.load(f)
    except FileNotFoundError:
        context_data = {"DEFAULT": {"description": "", "teaching_strategy": "", "real_world_context": ""}}
    
    # Map the Subject_Code to the correct curriculum file
    curr_maps = {
        "ABM_BM11": bm_data,
        "CORE_GENMATH11": gm_data
    }
    
    tos_rules = None
    if os.path.exists("item_analysis_rules.json"):
        with open("item_analysis_rules.json", "r") as f:
            tos_rules = json.load(f)
            
    print("📦 Caching Google Sheets into memory to prevent API rate limits...")
    workbook = sheet_client.open("Business_Math_Master_Gradebook")
    
    sheet = workbook.worksheet("Skill_Analytics")
    bank_sheet = workbook.worksheet("Item_Bank")
    vault_sheet = workbook.worksheet("Modules_Vault")
    roster_sheet = workbook.worksheet("Master_Roster")
    log_sheet = workbook.worksheet("Performance_Logs")
    deploy_sheet = workbook.worksheet("Deployments_Library") 

    all_values = sheet.get_all_values()
    headers = all_values[0]
    all_records = [dict(zip(headers, row)) for row in all_values[1:]]

    bank_data = bank_sheet.get_all_values()
    vault_data = vault_sheet.get_all_records()
    roster_data = roster_sheet.get_all_records()
    deploy_data = deploy_sheet.get_all_records()
    print("✅ Databases loaded successfully. Read requests drop to 0 during loop!")

    global_harvest_cache = {}
    deployment_cache = {} 

    for row_idx, row in enumerate(all_records, start=2):
        raw_comp_code = str(row.get("Topic_Focus", "")).strip()
        if not raw_comp_code: continue
        
        subject_code = str(row.get("Subject_Code", "ABM_BM11")).strip()
        comp_code = raw_comp_code.replace(subject_code, "") 
        assessment_type = str(row.get("Assessment_Type", "QUIZ")).strip().upper()
        
        active_curr_map = curr_maps.get(subject_code, {})
        curr = active_curr_map.get(comp_code)
        if not curr: continue
        
        rules = ASSESSMENT_RULES.get(assessment_type, ASSESSMENT_RULES["QUIZ"])
        display_limit = rules.get("display_count", 10)
        
        strand_focus = str(row.get("Strand_Focus", "ABM")).strip().upper()
        # 🔄 UPDATED: Use recursive function to find exact TVL/Academic profile
        student_context_profile = find_student_profile(context_data, strand_focus)

        # --- MODE ROUTER (GATEKEEPER) ---
        form_gen_status = str(row.get("Form_Generation_Status", "")).strip().upper()
        rem_status = str(row.get("Remediation_Status", "")).strip()

        if run_mode == "deploy" and form_gen_status not in ["ADVANCE_NEXT_TOPIC", "ADVANCE_RETRY", "READY"]:
            continue
            
        if run_mode == "grade" and rem_status != "Pending":
            continue

        # --- PHASE 0: AUTO-ADVANCEMENT STATE MACHINE ---
        student_id = str(row.get("Student_ID", "")).strip()
        
        if form_gen_status == "ADVANCE_NEXT_TOPIC":
            recent_score = row.get("Score", 0)
            print(f"🧠 AI Navigator analyzing progression for Student {student_id} (Last Score: {recent_score}%)...")
            
            # --- SCHEDULE INTERCEPTOR ---
            active_exam = get_active_scheduled_exam()
            is_course_complete = False
            
            if active_exam:
                print(f"📅 Schedule Override: Activating {active_exam['exam_type']} for {student_id}")
                raw_exam_code = active_exam['topic_code']
                
                # 🔄 UPDATED: Dynamically map the exam code based on the student's subject
                if subject_code == "CORE_GENMATH11":
                    next_comp = f"GENMATH_{raw_exam_code}"
                else:
                    next_comp = raw_exam_code
                    
                next_type = active_exam['exam_type']
                
                safe_sheet_action(sheet.update_cells, [
                    gspread.Cell(row_idx, headers.index("Topic_Focus") + 1, f"{subject_code}{next_comp}"),
                    gspread.Cell(row_idx, headers.index("Assessment_Type") + 1, next_type),
                    gspread.Cell(row_idx, headers.index("Form_Generation_Status") + 1, "READY"),
                    gspread.Cell(row_idx, headers.index("Tries") + 1, 1),
                    gspread.Cell(row_idx, headers.index("Digital_Answers") + 1, ""),
                    gspread.Cell(row_idx, headers.index("Score") + 1, ""),
                    gspread.Cell(row_idx, headers.index("Remediation_Status") + 1, "Pending"),
                    gspread.Cell(row_idx, headers.index("Remediation") + 1, ""),
                    gspread.Cell(row_idx, headers.index("Form_URL") + 1, "")
                ])
                continue

            # --- FUNCTION CALLING EXECUTION ---
            nav_prompt = get_navigator_prompt(student_id, recent_score, comp_code, active_curr_map)
            res = gen_client.models.generate_content(
                model='gemini-2.5-flash',
                contents=nav_prompt,
                config=types.GenerateContentConfig(tools=[update_student_progress], temperature=0.2)
            )
            
            if res.function_calls:
                args = res.function_calls[0].args
                next_comp = args.get("selected_topic_code")
                is_course_complete = args.get("is_course_complete")
                print(f"⏩ AI Selected Next Topic: {next_comp} | Reasoning: {args.get('reasoning')}")
                
                if next_comp and not is_course_complete:
                    safe_sheet_action(sheet.update_cells, [
                        gspread.Cell(row_idx, headers.index("Topic_Focus") + 1, f"{subject_code}{next_comp}"),
                        gspread.Cell(row_idx, headers.index("Assessment_Type") + 1, "QUIZ"),
                        gspread.Cell(row_idx, headers.index("Form_Generation_Status") + 1, "READY"),
                        gspread.Cell(row_idx, headers.index("Tries") + 1, 1),
                        gspread.Cell(row_idx, headers.index("Digital_Answers") + 1, ""),
                        gspread.Cell(row_idx, headers.index("Score") + 1, ""),
                        gspread.Cell(row_idx, headers.index("Remediation_Status") + 1, "Pending"),
                        gspread.Cell(row_idx, headers.index("Remediation") + 1, ""),
                        gspread.Cell(row_idx, headers.index("Form_URL") + 1, "")
                    ])
                else:
                    print(f"🎓 Course Complete for Student {student_id}!")
                    safe_sheet_action(sheet.update_cell, row_idx, headers.index("Form_Generation_Status") + 1, "COURSE_COMPLETE")
            else:
                print("⚠️ AI Navigator failed to execute the tool.")
            continue
            
        elif form_gen_status == "ADVANCE_RETRY":
            print(f"🔄 Prepping Student {row.get('Student_ID')} for Attempt #2 on {comp_code}")
            safe_sheet_action(sheet.update_cells, [
                gspread.Cell(row_idx, headers.index("Form_Generation_Status") + 1, "READY"),
                gspread.Cell(row_idx, headers.index("Tries") + 1, 2),
                gspread.Cell(row_idx, headers.index("Digital_Answers") + 1, ""),
                gspread.Cell(row_idx, headers.index("Score") + 1, ""),
                gspread.Cell(row_idx, headers.index("Remediation_Status") + 1, "Pending"),
                gspread.Cell(row_idx, headers.index("Form_URL") + 1, "")
            ])
            continue

        # --- PHASE 1: GENERATION (Forms & Slides) ---
        if form_gen_status == "READY":
            try_count = int(row.get("Tries", 1) or 1)
            cache_key = f"{subject_code}_{comp_code}_Try_{try_count}"
            
            vault_rec = fetch_from_vault(vault_data, comp_code, strand_focus)
            deploy_rec = fetch_from_deployments(deploy_data, comp_code, strand_focus, try_count)
            
            core_url, rem_url, adv_url = "", "", ""
            instruction_body = ""

            if rules["has_lecture"]:
                if not vault_rec:
                    print(f"🚀 Master Content Missing for {comp_code}. Calling Gemini to build Reading & Slides...")
                    lec_prompt = get_lecture_prompt(curr, strand_focus, student_context_profile)
                    lesson_data = call_gemini_with_retry(lec_prompt, LessonContentSchema)
                    
                    if lesson_data:
                        core_url = create_google_slides(comp_code, "Core", lesson_data.visual_decks.core_slides, drive_service, slides_service)
                        rem_url = create_google_slides(comp_code, "Remedial", lesson_data.visual_decks.remedial_slides, drive_service, slides_service)
                        adv_url = create_google_slides(comp_code, "Advanced", lesson_data.visual_decks.advanced_slides, drive_service, slides_service)
                        
                        save_master_lesson(vault_sheet, vault_data, comp_code, strand_focus, lesson_data, core_url, rem_url, adv_url)
                        instruction_body = format_math_text(lesson_data.lecture_content)
                else:
                    instruction_body = format_math_text(vault_rec.get('Lecture_Content', ''))
                    core_url = vault_rec.get('Core_Slides', '')
                    rem_url = vault_rec.get('Remedial_Slides', '')
                    adv_url = vault_rec.get('Advanced_Slides', '')
            else:
                instruction_body = "Please read each question carefully and select the best answer. No calculators allowed."

            if deploy_rec:
                form_url = deploy_rec.get('Form_URL', '')
                print(f"⚡ Sharing globally cached form URL for {comp_code} (Attempt #{try_count}) from Deployments Library...")
            elif cache_key in deployment_cache:
                form_url = deployment_cache[cache_key]
                print(f"⚡ Sharing session-cached form URL for {comp_code} (Attempt #{try_count})...")
            else:
                banked_questions = fetch_banked_questions(bank_data, comp_code, strand_focus)
                missing_count = max(0, rules["target_count"] - len(banked_questions))
                
                combined_quiz = banked_questions.copy()
                instruction_title = "📖 Reading Module" if rules["has_lecture"] else "📝 Exam Instructions"

                if missing_count > 0:
                    print(f"Calling Gemini: Generating {missing_count}-item exam bank for {comp_code} ({strand_focus})...")
                    qz_prompt = get_quiz_prompt(curr, strand_focus, missing_count, student_context_profile, tos_rules, rules.get("hard_mode", False))
                    quiz_data = call_gemini_with_retry(qz_prompt, QuizSchema)
                    
                    if quiz_data:
                        save_items_to_bank(bank_sheet, bank_data, comp_code, strand_focus, quiz_data.quiz)
                        combined_quiz.extend(quiz_data.quiz)
                
                if try_count == 1: final_form_quiz = combined_quiz[:display_limit]
                elif try_count == 2: final_form_quiz = combined_quiz[display_limit:(display_limit*2)]
                else: final_form_quiz = combined_quiz[:display_limit]

                if not final_form_quiz:
                    print(f"❌ ERROR: Quiz data is empty for {comp_code}. Gemini likely failed to generate. Skipping deployment.")
                    sheet.update_cell(row_idx, headers.index("Form_Generation_Status") + 1, "GEMINI_ERROR")
                    continue

                try:
                    form_url = deploy_fresh_form(comp_code, instruction_title, instruction_body, final_form_quiz, drive_service, form_service)
                    deployment_cache[cache_key] = form_url
                    save_to_deployments(deploy_sheet, deploy_data, comp_code, strand_focus, try_count, form_url, core_url, rem_url, adv_url)
                    print(f"✅ Form Natively Generated and Deployed for {comp_code}. Saved to Library.")
                except Exception as e: print(f"Form Gen Error: {e}")

            try:
                update_payload = [
                    gspread.Cell(row_idx, headers.index("Form_URL") + 1, form_url),
                    gspread.Cell(row_idx, headers.index("Form_Generation_Status") + 1, "DEPLOYED"),
                    gspread.Cell(row_idx, headers.index("Remediation_Status") + 1, "Pending")
                ]
                
                if "Core_Slides" in headers and core_url: update_payload.append(gspread.Cell(row_idx, headers.index("Core_Slides") + 1, core_url))
                if "Remedial_Slides" in headers and rem_url: update_payload.append(gspread.Cell(row_idx, headers.index("Remedial_Slides") + 1, rem_url))
                if "Advanced_Slides" in headers and adv_url: update_payload.append(gspread.Cell(row_idx, headers.index("Advanced_Slides") + 1, adv_url))

                safe_sheet_action(sheet.update_cells, update_payload)
                print(f"✅ Handing off {comp_code} to n8n for email distribution.")
            except Exception as e: print(f"Payload Update Error: {e}")
            
            time.sleep(2)
            continue

        # --- PHASE 2: HARVESTING & GRADING ---
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
                    safe_sheet_action(sheet.update_cell, row_idx, headers.index("Digital_Answers") + 1, digital_answers)

            if not digital_answers: continue

            profile = fetch_student_from_roster(roster_data, student_id)
            if not profile:
                safe_sheet_action(sheet.update_cell, row_idx, headers.index("Remediation_Status") + 1, "Roster_Error")
                continue
            
            strand_focus = str(profile.get("Strand_Focus", "ABM")).strip().upper()
            try_count = int(row.get("Tries", 1) or 1)

            print(f"Grading Submission: Student {student_id} | Attempt #{try_count}")
            score, diag_feedback, error = grade_submission_natively(digital_answers, comp_code, strand_focus, bank_sheet, bank_data, try_count, display_limit)
            
            if error:
                safe_sheet_action(sheet.update_cell, row_idx, headers.index("Remediation_Status") + 1, "Manual_Review")
                continue
                
            mastery_threshold = curr.get("mastery_threshold", 75)
            
            if score >= mastery_threshold:
                status = "Excelling" if score >= 90 else "Passing"
                vault_rec = fetch_from_vault(vault_data, comp_code, strand_focus)
                final_feedback = vault_rec.get('Enrichment_Text', "Passed successfully!") if vault_rec else "Passed successfully!"
            else:
                status = "Needs Review"
                vault_rec = fetch_from_vault(vault_data, comp_code, strand_focus)
                final_feedback = format_math_text(vault_rec.get('Remediation_Scaffolding', diag_feedback)) if vault_rec else diag_feedback

            core_url = vault_rec.get('Core_Slides', '') if vault_rec else ""
            rem_url = vault_rec.get('Remedial_Slides', '') if vault_rec else ""
            adv_url = vault_rec.get('Advanced_Slides', '') if vault_rec else ""

            try:
                update_payload = [
                    gspread.Cell(row_idx, headers.index("Score") + 1, score),
                    gspread.Cell(row_idx, headers.index("Remediation") + 1, final_feedback),
                    gspread.Cell(row_idx, headers.index("Form_Generation_Status") + 1, "GRADED") 
                ]
                
                if status == "Needs Review" and try_count < 2:
                    update_payload.append(gspread.Cell(row_idx, headers.index("Remediation_Status") + 1, "Needs Review_Trigger"))
                    if "Tries" in headers: update_payload.append(gspread.Cell(row_idx, headers.index("Tries") + 1, try_count + 1))
                else:
                    update_payload.append(gspread.Cell(row_idx, headers.index("Remediation_Status") + 1, status))
                
                if "Core_Slides" in headers and core_url: update_payload.append(gspread.Cell(row_idx, headers.index("Core_Slides") + 1, core_url))
                if "Remedial_Slides" in headers and rem_url: update_payload.append(gspread.Cell(row_idx, headers.index("Remedial_Slides") + 1, rem_url))
                if "Advanced_Slides" in headers and adv_url: update_payload.append(gspread.Cell(row_idx, headers.index("Advanced_Slides") + 1, adv_url))

                safe_sheet_action(sheet.update_cells, update_payload)
                print(f"Processed grading row successfully. Status set to: {status}")
                
                append_to_performance_log(log_sheet, student_id, comp_code, score, final_feedback)
                
            except Exception as e: print(f"Update Error: {e}")
            
            time.sleep(2)

if __name__ == '__main__':
    main()
