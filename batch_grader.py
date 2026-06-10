import os, time, json, re, uuid, gspread, pandas as pd
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from google import genai
from google.genai import types

# --- CONFIGURATION ---
print("--- INITIALIZING SYSTEM ---")
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
    sub_concept: str; question: str; option_a: str; option_b: str; option_c: str; option_d: str; correct_answer: str; targeted_remediation: str; difficulty: str 

class LessonSchema(BaseModel):
    lesson_title: str; lecture_content: str; remediation_scaffolding: str; enrichment_scenario: str; quiz: list[MCQ]

class ExamSchema(BaseModel):
    instructions: str; quiz: list[MCQ]

def get_google_services():
    print("Connecting to Google Services...")
    token_dict = json.loads(os.environ.get('GOOGLE_TOKEN_JSON'))
    creds = Credentials.from_authorized_user_info(token_dict)
    return gspread.authorize(creds), build('drive', 'v3', credentials=creds), build('forms', 'v1', credentials=creds)

# --- ROSTER SYNC (The "Tank" Bridge) ---
def fetch_student_from_roster(sheet_client, student_id):
    print(f"DEBUG: Looking up Student ID {student_id} in Master_Roster...")
    roster = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Master_Roster")
    for r in roster.get_all_records():
        if str(r.get('Student_ID', '')).strip() == str(student_id).strip():
            return r
    return None

# --- GENERATION WITH MATH FORMATTING ---
def get_generation_prompt(curr, strand_focus, missing_count, is_exam, hard_mode=False):
    return f"""
    Create a module for competency: {curr['learning_competency']} ({strand_focus}).
    🛑 MATH FORMATTING MANDATE:
    - Use LaTeX for all mathematical formulas and variables (e.g., $P = R - C$).
    - Use × for multiplication and ÷ for division.
    - Use double line breaks (\\n\\n) for paragraphs and ALL CAPS for section headers.
    - 1. LECTURE (Use LaTeX) 2. REMEDIATION 3. ENRICHMENT 4. QUIZ ({missing_count} questions).
    """

# --- GRADING ENGINE (With Defensive Lookup) ---
def grade_submission_natively(digital_answers, comp_code, strand_focus, sheet_client):
    bank = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    keys = [r for r in bank.get_all_records() if str(r.get('Topic_Focus', '')).strip().upper() == comp_code.strip().upper() and str(r.get('Strand_Focus', '')).strip().upper() == strand_focus.strip().upper()]
    
    if not keys: 
        print(f"DEBUG: No keys for {comp_code} | {strand_focus}")
        return None, "", "Error: No keys found."
        
    choices = re.findall(r'([A-D])\)', digital_answers.upper())
    correct_count = 0
    feedback = []
    
    for idx, item in enumerate(keys):
        # INDESTRUCTIBLE LOOKUP: Finds 'correct_answer' regardless of case
        ans = next((item[k] for k in item if k.lower() == 'correct_answer'), "")
        if idx < len(choices) and choices[idx] == str(ans).strip().upper():
            correct_count += 1
        else:
            feedback.append(f"• {item.get('Sub_Concept')}: {item.get('Targeted_Remediation')}")

    score = int((correct_count / len(keys)) * 100)
    return score, "\n".join(feedback), None

# --- MAIN EXECUTION ---
def main():
    sheet_client, drive_service, form_service = get_google_services()
    with open("curriculum_guide.json", "r") as f: curr_data = json.load(f)["ABM_BM11"]
    
    sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Skill_Analytics")
    all_records = [dict(zip(sheet.row_values(1), row)) for row in sheet.get_all_values()[1:] if any(row)]

    for index, row in enumerate(all_records, start=2):
        student_id = str(row.get("Student_ID", "")).strip()
        comp_code = str(row.get("Topic_Focus", "")).replace("ABM_BM11", "").strip()
        
        print(f"Processing Row {index} | ID: {student_id} | Topic: {comp_code}")
        
        # --- ROSTER SYNC ---
        profile = fetch_student_from_roster(sheet_client, student_id)
        if not profile:
            print(f"   ! Skipping: ID {student_id} not found in Master_Roster.")
            continue
            
        strand_focus = str(profile.get("Strand_Focus", "ABM")).strip().upper()
        print(f"   -> Strand Verified: {strand_focus}")
        
        curr = curr_data.get(comp_code)
        if not curr: continue

        # --- GENERATION ---
        if str(row.get("Form_Generation_Status", "")).strip() == "READY":
            # ... [Keep your existing generation logic here] ...
            print("   -> Generation Triggered.")

        # --- NATIVE INSTANT GRADING ---
        if str(row.get("Remediation_Status", "")).strip() == "Pending":
            ans = str(row.get("Digital_Answers", "")).strip()
            if not ans: continue
            
            score, feedback, error = grade_submission_natively(ans, comp_code, strand_focus, sheet_client)
            if error:
                print(f"   ! Grading Error: {error}")
                sheet.update_cell(index, headers.index("Remediation_Status") + 1, "Manual_Review")
                continue
                
            status = "Excelling" if score >= 90 else "Passing" if score >= 75 else "Needs Review"
            
            # Scaffolding injection
            if score < 75:
                vault = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Modules_Vault")
                for r in vault.get_all_records():
                    if str(r.get('Topic_Focus', '')).strip() == comp_code:
                        feedback = r.get('Remediation_Scaffolding', feedback)
            
            sheet.update_cell(index, headers.index("Score") + 1, score)
            sheet.update_cell(index, headers.index("Remediation") + 1, feedback)
            sheet.update_cell(index, headers.index("Remediation_Status") + 1, status)
            print(f"   ✅ SUCCESS: Graded {student_id} ({strand_focus}). Score: {score}%")

if __name__ == '__main__':
    main()
