import os
import sys
import asyncio
import json
import requests
import pandas as pd
import io
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from openai import OpenAI

# --- WINDOWS FIX ---
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# --- CONFIGURATION ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# !!! PASTE YOUR NEW AIPIPE KEY HERE DIRECTLY !!!
# This bypasses the Render Environment Variables so we KNOW it uses the right key.
AIPIPE_HARDCODED_KEY = "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6InByaXlhbnNoY2hhdWRoYXJ5MTc2QGdtYWlsLmNvbSJ9.Jvi9Pdk1hYvFjGd4RxzGfwCZRGRWZTQQn0k8Vbt4Q8k" 

# Setup AIPipe Client
aipipe_client = None
if AIPIPE_HARDCODED_KEY and "PASTE_YOUR" not in AIPIPE_HARDCODED_KEY:
    print("✅ Using Hardcoded AIPipe Key")
    try:
        aipipe_client = OpenAI(
            api_key=AIPIPE_HARDCODED_KEY,
            base_url="https://aipipe.org/openai/v1"
        )
    except Exception as e:
        print(f"❌ Error setting up AIPipe: {e}")
else:
    # Fallback to Env Var if hardcode is skipped
    env_key = os.environ.get("AIPIPE_API_KEY")
    if env_key:
        print("Using Environment Variable for AIPipe")
        aipipe_client = OpenAI(api_key=env_key, base_url="https://aipipe.org/openai/v1")

# YOUR SECRET
MY_SECRET = "UNKNOWN"

app = FastAPI()

class TaskPayload(BaseModel):
    email: str
    secret: str
    url: str

def clean_json_text(text):
    return text.replace("```json", "").replace("```", "").strip()

async def get_llm_plan(prompt_text):
    """
    1. Tries multiple Gemini models (v1beta AND v1).
    2. If all fail, tries AIPipe (Hardcoded Key).
    """
    
    # --- STRATEGY A: GEMINI (Try Multiple Models & Endpoints) ---
    if GEMINI_API_KEY:
        # List of (Model Name, API Version)
        configs_to_try = [
            ("gemini-1.5-flash", "v1beta"),
            ("gemini-1.5-flash-latest", "v1beta"),
            ("gemini-pro", "v1beta"),
            ("gemini-pro", "v1") # Stable endpoint often fixes 404s
        ]

        payload = {
            "contents": [{
                "parts": [{"text": prompt_text}]
            }]
        }
        headers = {"Content-Type": "application/json"}

        for model, version in configs_to_try:
            try:
                print(f"🤖 Trying Gemini: {model} ({version})...")
                url = f"https://generativelanguage.googleapis.com/{version}/models/{model}:generateContent?key={GEMINI_API_KEY}"
                
                response = requests.post(url, json=payload, headers=headers)
                
                if response.status_code == 200:
                    resp_data = response.json()
                    try:
                        raw_text = resp_data['candidates'][0]['content']['parts'][0]['text']
                        print(f"✅ Success with {model}!")
                        return json.loads(clean_json_text(raw_text))
                    except:
                        pass
                else:
                    print(f"⚠️ Failed ({response.status_code})")
            except Exception as e:
                print(f"⚠️ Error: {e}")

    # --- STRATEGY B: AIPIPE (The Backup) ---
    if aipipe_client:
        try:
            print("🤖 Asking AIPipe...")
            response = aipipe_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Return valid JSON only."},
                    {"role": "user", "content": prompt_text}
                ],
                response_format={"type": "json_object"}
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            print(f"❌ AIPipe failed: {e}")

    print("❌ ALL AI MODELS FAILED.")
    return None

async def solve_quiz(task_url: str, email: str, secret: str):
    print(f"\n🚀 STARTING TASK: {task_url}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(task_url, timeout=60000)
            await page.wait_for_load_state("networkidle")
            content = await page.evaluate("document.body.innerText")
            print(f"📄 Scraped: {content[:100]}...")

            prompt = f"""
            You are a Data Science Agent.
            QUIZ TEXT:
            ---
            {content}
            ---
            TASKS:
            1. Identify 'submission_url'.
            2. Solve the question.
               - If data analysis (CSV/PDF, math), WRITE PYTHON CODE.
               - Use `requests`, `pandas`. Print final answer.
               - If text only, provide answer.
            OUTPUT JSON:
            {{
                "submission_url": "https://...",
                "python_code": "import requests... print(ans)", 
                "text_answer": "answer"
            }}
            """

            plan = await get_llm_plan(prompt)
            if not plan:
                return

            submission_url = plan.get("submission_url")
            final_answer = plan.get("text_answer")
            python_code = plan.get("python_code")

            if python_code and python_code != "null":
                print("⚙️ Executing Python Code...")
                old_stdout = sys.stdout
                redirected_output = io.StringIO()
                sys.stdout = redirected_output
                try:
                    exec_globals = {'pd': pd, 'requests': requests, 'print': print}
                    exec(python_code, exec_globals)
                    final_answer = redirected_output.getvalue().strip()
                except Exception as e:
                    print(f"❌ Code Error: {e}")
                    final_answer = "Error"
                finally:
                    sys.stdout = old_stdout
                print(f"✅ Computed Answer: {final_answer}")

            submit_payload = {
                "email": email,
                "secret": secret,
                "url": task_url,
                "answer": final_answer 
            }
            
            print(f"📤 Submitting to {submission_url}...")
            resp = requests.post(submission_url, json=submit_payload)
            print(f"✅ Status: {resp.status_code}")

            try:
                resp_json = resp.json()
                next_url = resp_json.get("url")
                if next_url:
                    print(f"🔄 Next Question Found! Proceeding to: {next_url}")
                    await solve_quiz(next_url, email, secret)
                else:
                    print("🏁 Quiz Complete.")
            except:
                pass

        except Exception as e:
            print(f"❌ Error: {e}")
        finally:
            await browser.close()

@app.post("/run-quiz")
async def run_quiz_endpoint(payload: TaskPayload, background_tasks: BackgroundTasks):
    if payload.secret != MY_SECRET:
        raise HTTPException(status_code=403, detail="Invalid Secret")
    background_tasks.add_task(solve_quiz, payload.url, payload.email, payload.secret)
    return {"message": "Task started"}
