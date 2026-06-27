🔄 Circular Grader V3.3: AI-Powered Autonomous Assessment System

📖 Overview

This repository contains the architecture for a fully autonomous, closed-loop educational grading, remediation, and curriculum navigation engine. Designed originally for Senior High School Business Mathematics, the system natively integrates Google Gemini 2.5 Flash with Google Workspace APIs, n8n Webhooks, and Event-Driven GitHub Actions.

It completely eliminates manual teacher workloads by dynamically generating curriculum content, deploying tiered assessments, harvesting responses without rate-limit bottlenecks, grading natively in Python, auto-advancing students through a curriculum map, and automatically dispatching personalized remediation via email.

📂 Repository Structure

batch_grader.py: The core Python engine. Handles API orchestration, Gemini prompt engineering, JSON schema validation, native grading logic, and the AI Curriculum Navigator.

trigger.gs: The Google Apps Script webhook. Listens for form submissions and fires a repository_dispatch to GitHub to grade immediately.

.github/workflows/: Contains the split CI/CD YAML files:

automated_deployer.yml: Runs on a scheduled cron job (e.g., 5:00 PM drop) to generate heavy AI materials (--mode deploy).

event_driven_grader.yml: Wakes up instantly via webhook to grade student submissions (--mode grade).

curriculum_map.json: The database of Content Domains, Learning Competencies, Prerequisites, and Difficulty Levels used by the AI Navigator to route students.

n8n_email_dispatcher_workflow.json: The exported n8n workflow. Monitors the database state and routes localized email communications via Gmail.

✨ Core Engineering Highlights

🧠 AI Curriculum Navigator: An autonomous agent that reads a student's recent score, cross-references it with the curriculum_map.json, and intelligently selects the next logical topic or triggers a retry based on mastery thresholds.

⚡ Event-Driven Architecture: Replaced inefficient polling with Google Apps Script Webhooks (trigger.gs). The system now only consumes compute time when a student actually hits "Submit", ensuring instant feedback and zero wasted GitHub minutes.

🛡️ Master Memory Caching (Zero-Read Loop): Reads the entire Google Sheets relational database into memory at runtime. This drastically reduces API calls, ensuring high performance and eliminating Google API quota crashes.

🚦 Exponential Backoff & Retry Logic: Implements a custom safe_sheet_action wrapper to safely handle HTTP 429 (Rate Limit Exceeded) and 503 (Service Unavailable) errors in production.

🔄 Closed-Loop Orchestration (n8n): Uses a multi-path Switch node to dispatch highly personalized emails based on a student's Remediation_Status (e.g., Pending, Passing, Needs Review, Excelling).

🏗️ System Architecture

The pipeline operates in two distinct, decoupled phases:

Phase 1: Heavy Generation & Deployment (Cron Schedule)

Scout: Reads the READY status and checks the Deployments_Library cache.

Generate: Calls Gemini to generate missing MCQs and 3-Tiered Slide Decks (Core, Remedial, Advanced).

Build: Programmatically creates a Google Slide presentation and a Google Form.

Handoff: Pushes the deployed URLs back to Google Sheets, updating the state to DEPLOYED to trigger the n8n initial email dispatch.

Phase 2: Instant Harvesting & Dispatch (Event-Driven Webhook)

Trigger: Student submits a Google Form. trigger.gs instantly pings GitHub Actions.

Harvest: The Python Grader wakes up and extracts answers securely.

Grade & Analyze: Compares answers against the Item_Bank, calculating scores and updating the item's Difficulty Index (p-value).

Navigate: The AI Navigator decides if the student should advance (ADVANCE_NEXT_TOPIC) or retry (ADVANCE_RETRY).

Automated Dispatch (n8n): The workflow detects the GRADED state and routes the student through a switch logic:

Needs Review_Trigger: Emails Remedial Slides and prompts a Quiz Retake.

Passing/Excelling: Sends a congratulatory email with Enrichment slides.

🚀 Setup & Installation

1. Environment Variables

You must configure the following environment variables locally or in your GitHub Actions Secrets:

GEMINI_API_KEY: Your Google DeepMind API key.

GOOGLE_TOKEN_JSON: OAuth2 credentials dict for Google Workspace (Drive, Sheets, Forms, Slides).

SPREADSHEET_ID: The ID of your Master Gradebook Google Sheet.

2. Deploying the Webhook (trigger.gs)

Open your Master Google Sheet and click Extensions > Apps Script.

Paste the trigger.gs code.

Generate a Personal Access Token (PAT) in GitHub with repo permissions and update the placeholder variables in the script.

Add an onFormSubmit trigger in the Apps Script dashboard.

3. Deploying the n8n Workflow

Open your n8n instance.

Click Add Workflow -> Import from File and upload n8n_email_dispatcher_workflow.json.

Update the Google Sheets Trigger and Gmail nodes with your OAuth2 credentials.

Ensure the Switch node uses the {{ $json.Form_Generation_Status }}_{{ $json.Remediation_Status }} concatenated logic.

4. Running the Engine

To run the orchestrator manually via CLI:

# Generate new assessments & slides
python batch_grader.py --mode deploy

# Grade pending submissions
python batch_grader.py --mode grade


Otherwise, allow the GitHub Actions .yml files to run the system autonomously.

⚖️ License & Compliance

This system is designed for deployment in live educational environments. When adapting or forking this project, ensure strict compliance with institutional data privacy policies (e.g., FERPA, Data Privacy Act of 2012 / PDPA) regarding the secure handling of real student Personally Identifiable Information (PII).
