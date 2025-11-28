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
AIPIPE_API_KEY = os.environ.get("AIPIPE_API_KEY")

# Setup AIPipe Client
aipipe_client = None
if AIPIPE_API_KEY:
    try:
        aipipe_client = OpenAI(
            api_key=AIPIPE_API_KEY,
            base_url="https://aipipe.org/openai/v1"
        )
    except:
        pass

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
    Tries Gemini (v1 Stable) then AIPipe.
    """
    
    # --- STRATEGY A: GEMINI (v1 Stable Endpoint) ---
    if GEMINI_API_KEY:
        try:
            print(f"🤖 Trying Gemini Pro (v1 Stable)...")
            # CHANGED: Using 'v1' instead of 'v1beta'
            url = f"https://generativelanguage.googleapis.com/v1/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
            
            payload = {
                "contents": [{
                    "parts": [{"text": prompt_text}]
                }]
            }
            headers = {"Content-Type": "application/json"}
            
            response = requests.post(url, json=payload, headers=headers)
            
            if response.status_code == 200:
                resp_data = response.json()
                raw_text = resp_data['candidates'][0]['content']['parts'][0]['text']
                print(f"✅ Success with Gemini v1!")
                return json.loads(clean_json_text(raw_text))
            else:
                print(f"⚠️ Gemini v1 Failed ({response.status_code}): {response.text}")
        except Exception as e:
            print(f"⚠️ Error on Gemini: {e}")

    # --- STRATEGY B: AIPIPE (Fallback) ---
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
