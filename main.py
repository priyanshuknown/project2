import os
import json
import sys          # <--- ADD THIS
import asyncio
import requests
import pandas as pd
import io
import sys
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
import google.generativeai as genai
from openai import OpenAI

# --- CONFIGURATION ---
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# 1. Setup Gemini (Primary)
GEMINI_API_KEY = os.environ.get("AIzaSyAoqeUEfAYYwUlLEuxJFLEmI0as0DMEiOc")
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# 2. Setup AIPipe (Fallback)
# Note: Check if AIPipe needs a custom base_url (e.g., "https://api.langpipe.ai/v1")
AIPIPE_API_KEY = os.environ.get("eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6IjI0ZjIwMDUzNjVAZHMuc3R1ZHkuaWl0bS5hYy5pbiJ9.jTSZ0cfZb5tDCakKTBeEFjM8K5gmBPTqP-Ku39MbkPw")
aipipe_client = OpenAI(
    api_key=AIPIPE_API_KEY,
    base_url="https://aipipe.org/openai/v1" # <--- VERIFY THIS URL
)

# 3. Your Secret
MY_SECRET = "UNKNOWN"

app = FastAPI()

class TaskPayload(BaseModel):
    email: str
    secret: str
    url: str

def clean_json_text(text):
    """Helper to clean markdown code blocks from LLM response"""
    return text.replace("```json", "").replace("```", "").strip()

async def get_llm_plan(prompt_text):
    """
    Tries Gemini first. If it fails, switches to AIPipe.
    Returns: Parsed JSON dictionary.
    """
    # --- ATTEMPT 1: GEMINI ---
    try:
        print("🤖 Asking Gemini...")
        model = genai.GenerativeModel('gemini-1.5-flash')
        response = model.generate_content(prompt_text)
        return json.loads(clean_json_text(response.text))
    except Exception as e:
        print(f"⚠️ Gemini failed ({e}). Switching to AIPipe...")

    # --- ATTEMPT 2: AIPIPE (FALLBACK) ---
    try:
        print("🤖 Asking AIPipe...")
        response = aipipe_client.chat.completions.create(
            model="gpt-4o-mini", # Check which models AIPipe supports
            messages=[
                {"role": "system", "content": "You return valid JSON only."},
                {"role": "user", "content": prompt_text}
            ],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"❌ Both LLMs failed: {e}")
        return None

async def solve_quiz(task_url: str, email: str, secret: str):
    print(f"🚀 Starting task for: {task_url}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            # 1. Scrape
            await page.goto(task_url, timeout=60000)
            await page.wait_for_load_state("networkidle")
            content = await page.evaluate("document.body.innerText")
            print(f"📄 Scraped: {content[:100]}...")

            # 2. Construct Prompt
            prompt = f"""
            You are a Data Science Agent taking a quiz.
            
            QUIZ TEXT:
            ---
            {content}
            ---
            
            TASKS:
            1. Identify the 'submission_url'.
            2. Solve the question.
               - If it requires data analysis (CSV/PDF, math, counting), WRITE PYTHON CODE.
               - Use `requests` to download, `pandas` to analyze.
               - The code MUST PRINT the final answer.
               - If it is a simple text question, just provide the answer.

            OUTPUT JSON:
            {{
                "submission_url": "https://...",
                "python_code": "import requests... print(ans)", 
                "text_answer": "answer_if_no_code_needed"
            }}
            """

            # 3. Get Plan (Gemini -> Fallback -> AIPipe)
            plan = await get_llm_plan(prompt)
            
            if not plan:
                print("❌ Could not get a plan from any LLM.")
                return

            submission_url = plan.get("submission_url")
            final_answer = plan.get("text_answer")
            python_code = plan.get("python_code")

            # 4. Execute Code
            if python_code and python_code != "null":
                print("⚙️ Executing Python Code...")
                old_stdout = sys.stdout
                redirected_output = io.StringIO()
                sys.stdout = redirected_output
                try:
                    # Allow the code to use installed libraries
                    exec_globals = {'pd': pd, 'requests': requests, 'print': print}
                    exec(python_code, exec_globals)
                    final_answer = redirected_output.getvalue().strip()
                except Exception as e:
                    print(f"❌ Code Error: {e}")
                    final_answer = "Error calculating"
                finally:
                    sys.stdout = old_stdout
                print(f"✅ Computed Answer: {final_answer}")

            # 5. Submit
            submit_payload = {
                "email": email,
                "secret": secret,
                "url": task_url,
                "answer": final_answer 
            }
            
            print(f"📤 Submitting to {submission_url}...")
            resp = requests.post(submission_url, json=submit_payload)
            print(f"✅ Result: {resp.status_code} | {resp.text}")

        except Exception as e:
            print(f"❌ Critical Error: {e}")
        finally:
            await browser.close()

@app.post("/run-quiz")
async def run_quiz_endpoint(payload: TaskPayload, background_tasks: BackgroundTasks):
    if payload.secret != MY_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Secret")
    background_tasks.add_task(solve_quiz, payload.url, payload.email, payload.secret)
    return {"message": "Task started"}
