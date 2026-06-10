import os, time, json, re, uuid, gspread, pandas as pd
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel
from google import genai
from google.genai import types

# --- CONFIGURATION ---
gen_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
DYNAMIC_FORM_ID = "16uOwbZbu86xWv1o7fl99TrjRRzlhOiyvg-QgybQr3MA" 

# --- SERVICES ---
def get_google_services():
    token_dict = json.loads(os.environ.get('GOOGLE_TOKEN_JSON'))
    creds = Credentials.from_authorized_user_info(token_dict)
    return gspread.authorize(creds), build('forms', 'v1', credentials=creds)

def fetch_student_profile(sheet_client, student_id):
    """ Authoritative source for Strand_Focus. """
    roster = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Master_Roster")
    for r in roster.get_all_records():
        if str(r.get('Student_ID', '')).strip() == str(student_id).strip():
            return r
    return None

def get_generation_prompt(curr, strand_focus, missing_count):
    return f"""
    Create a module for: {curr['learning_competency']} ({strand_focus}).
    🛑 MATH FORMATTING MANDATE:
    - Use LaTeX for ALL formulas and variables (e.g., $P = R - C$).
    - Use unicode multiplication (×) and division (÷).
    - Use double line breaks (\\n\\n) for paragraphs and ALL CAPS for headers.
    - 1. LECTURE (Use LaTeX) 2. REMEDIATION 3. ENRICHMENT 4. QUIZ ({missing_count} questions).
    """

def format_math_text(text):
    return str(text).replace("*", "×").replace("/", "÷")

def grade_submission(answers, comp_code, strand_focus, sheet_client):
    bank = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Item_Bank")
    # Strict key fetching
    keys = [r for r in bank.get_all_records() if str(r.get('Topic_Focus', '')).strip().upper() == comp_code.strip().upper() and str(r.get('Strand_Focus', '')).strip().upper() == strand_focus.strip().upper()]
    
    if not keys: return None, "", f"Error: No keys found for {comp_code} | {strand_focus}"
        
    choices = re.findall(r'([A-D])\)', answers.upper())
    correct_count = 0
    feedback = []
    
    for idx, item in enumerate(keys):
        ans = next((item[k] for k in item if k.lower() == 'correct_answer'), "")
        if idx < len(choices) and choices[idx] == str(ans).strip().upper():
            correct_count += 1
        else:
            feedback.append(f"• {item.get('Sub_Concept', 'Concept')}: {item.get('Targeted_Remediation', 'Review.')}")

    score = int((correct_count / len(keys)) * 100)
    return score, format_math_text("\n".join(feedback)), None

def main():
    sheet_client, form_service = get_google_services()
    with open("curriculum_guide.json", "r") as f: curr_data = json.load(f)["ABM_BM11"]
    
    sheet = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Skill_Analytics")
    records = [dict(zip(sheet.row_values(1), row)) for row in sheet.get_all_values()[1:] if any(row)]

    for index, row in enumerate(records, start=2):
        student_id = str(row.get("Student_ID", "")).strip()
        comp_code = str(row.get("Topic_Focus", "")).replace("ABM_BM11", "").strip()
        
        # 1. Authoritative Strand Lookup
        profile = fetch_student_profile(sheet_client, student_id)
        if not profile: continue
        strand = str(profile.get("Strand_Focus", "ABM")).strip().upper()
        
        # 2. Grading Path
        if str(row.get("Remediation_Status", "")).strip() == "Pending":
            ans = str(row.get("Digital_Answers", "")).strip()
            if not ans: continue
            
            score, feedback, error = grade_submission(ans, comp_code, strand, sheet_client)
            if error:
                print(f"Skipping Row {index}: {error}")
                continue
            
            # Injection of custom scaffolding from Vault
            if score < 75:
                vault = sheet_client.open("Business_Math_Master_Gradebook").worksheet("Modules_Vault")
                for r in vault.get_all_records():
                    if str(r.get('Topic_Focus', '')).strip() == comp_code:
                        feedback = format_math_text(r.get('Remediation_Scaffolding', feedback))
            
            sheet.update_cell(index, 7, score)
            sheet.update_cell(index, 8, feedback)
            sheet.update_cell(index, 6, "Excelling" if score >= 90 else "Passing" if score >= 75 else "Needs Review")
            print(f"✅ Graded {student_id} ({strand}). Score: {score}%")

if __name__ == '__main__':
    main()
