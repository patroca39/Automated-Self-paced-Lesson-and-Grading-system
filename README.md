🔄 Automated Self-Paced Lesson and Grading System

📖 Overview

This repository contains the architecture for a fully autonomous, closed-loop educational grading and remediation engine. Designed originally for Senior High School Business Mathematics, the system natively integrates Google Gemini 2.5 Flash with Google Workspace APIs and n8n Webhooks.

It completely eliminates manual teacher workloads by dynamically generating curriculum content, deploying tiered assessments, harvesting responses without rate-limit bottlenecks, grading natively in Python, and automatically dispatching personalized remediation via email.

📂 Repository Structure

batch_grader.py: The core Python engine. Handles API orchestration, Gemini prompt engineering, JSON schema validation, and native grading logic.

n8n_email_dispatcher_workflow.json: The exported n8n workflow. Monitors the database state and routes localized email communications via Gmail.

.github/workflows/: Contains the CI/CD YAML files configured to run the batch grading Python script on a scheduled cron job for fully hands-free execution.

curriculum_guide.json: The database of Content Domains and Learning Competencies.

item_analysis_rules.json: Configuration for the Table of Specifications (TOS) targeting varying cognitive difficulty levels.

dll_template.json: The schema template mapping to standardized Daily Lesson Logs.

✨ Core Engineering Highlights

🧠 LLM-Driven Structured Output: Utilizes pydantic schemas to force Gemini to output strictly validated JSON for Multiple Choice Questions (MCQs) and 3-Tiered Lecture Slides (Core, Remedial, Advanced).

⚡ Native Google API Integration: Bypasses third-party form builders. The Python engine directly calls Google Forms and Google Slides APIs to build and format tests and presentations on the fly.

🛡️ Master Memory Caching (Zero-Read Loop): Reads the entire Google Sheets relational database into memory at runtime. This drastically reduces API calls, ensuring high performance and eliminating Google API quota crashes.

🚦 Exponential Backoff & Retry Logic: Implements a custom safe_sheet_action wrapper to safely handle HTTP 429 (Rate Limit Exceeded) and 503 (Service Unavailable) errors in production.

🔄 Closed-Loop Orchestration (n8n): Uses a multi-path Switch node to dispatch highly personalized emails based on a student's Remediation_Status (e.g., Pending, Passing, Needs Review, Excelling).

🏗️ System Architecture

The pipeline operates in two distinct phases, orchestrated by state flags in a Google Sheets database:

Phase 1: Generation & Deployment (batch_grader.py)

Scout: Reads curriculum competencies and checks the Modules_Vault cache.

Generate: Calls Gemini to generate missing MCQs and 3-Tiered Slide Decks.

Build: Programmatically creates a Google Slide presentation and a Google Form.

Handoff: Pushes the deployed URLs back to Google Sheets, updating the state to DEPLOYED to trigger n8n.

Phase 2: Harvesting & Dispatch (n8n & Python)

Harvest: Securely connects to the live Google Form to extract student answers dynamically.

Grade & Analyze: Compares extracted answers against the Item_Bank, calculating scores and updating the item's Difficulty Index (p-value).

Automated Dispatch (n8n): The n8n workflow detects the row update and routes the student through a switch logic:

Pending: Emails the Core Slides and initial Quiz URL.

Needs Review_Trigger: Emails Remedial Slides and prompts a Quiz Retake.

Passing: Sends a congratulatory email.

Excelling: Sends Advanced/Enrichment slides.

🚀 Setup & Installation

1. Environment Variables

You must configure the following environment variables locally or in your GitHub Actions Secrets:

GEMINI_API_KEY: Your Google DeepMind API key.

GOOGLE_TOKEN_JSON: OAuth2 credentials dict for Google Workspace (must include scopes for Drive, Sheets, Forms, and Slides).

SPREADSHEET_ID: The ID of your Master Gradebook Google Sheet.

2. Deploying the n8n Workflow

Open your n8n instance.

Click Add Workflow -> Import from File.

Upload the n8n_email_dispatcher_workflow.json located in this repository.

Update the Google Sheets Trigger and Gmail nodes with your own OAuth2 credentials.

3. Running the Engine

To run the orchestrator manually, execute:

python batch_grader.py


Alternatively, allow the pre-configured .github/workflows cron job to run the batch operations automatically based on your schedule.

⚖️ License & Compliance

This system is designed for deployment in live educational environments. When adapting or forking this project, ensure strict compliance with institutional data privacy policies (e.g., FERPA, Data Privacy Act of 2012 / PDPA) regarding the secure handling of real student Personally Identifiable Information (PII).
