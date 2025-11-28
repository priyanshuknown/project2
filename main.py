import os
import sys
import asyncio
import json
import requests
import pandas as pd
import io
from urllib.parse import urljoin
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from openai import OpenAI

# --- WINDOWS FIX ---
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# --- CONFIGURATION ---
# !!! PASTE YOUR GROQ KEY HERE !!!
GROQ_API_KEY = "PASTE_YOUR_GROQ_KEY_HERE"

client = None
if GROQ_API_KEY and "PASTE_YOUR" not in GROQ_API_KEY:
    try:
        client = OpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1"
        )
    except Exception as e:
        print(f"❌ Groq Setup Error: {e}")

MY_SECRET = "UNKNOWN"

app = FastAPI()

class TaskPayload(BaseModel):
    email: str
    secret: str
    url: str

def clean_json_text(text):
    return text.replace("```json", "").replace("```", "").strip()

async def get_llm_plan(prompt_text):
    if not client:
        return None
    try:
        print("🤖 Asking Groq (Llama 3.3)...")
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a precise data extraction agent. Output valid JSON only."},
                {"role": "user", "content": prompt_text}
            ],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"❌ Groq Failed: {e}")
        return None

async def solve_quiz(task_url: str, email: str, secret: str):
    print(f"\n🚀 STARTING TASK: {task_url}")
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            # 1. Scrape
            await page.goto(task_url, timeout=60000)
            await page.wait_for_load_state("networkidle")
            content = await page.evaluate("document.body.innerText")
            print(f"📄 Scraped: {content[:100]}...")

            # 2. Plan (UPDATED PROMPT)
            prompt = f"""
            You are a Data Science Agent.
            CURRENT PAGE URL: {task_url}
            
            PAGE TEXT:
            ---
            {content}
            ---
            
            YOUR TASKS:
            1. IDENTIFY SUBMISSION URL:
               - Look for "Post to..." or "Submit to...".
               - If it is relative (e.g. "/submit"), use `urljoin` in your head to make it absolute (e.g. "https://domain.com/submit").
               - WARNING: "/submit" usually means the ROOT path, not relative to the current folder.
            
            2. SOLVE THE QUESTION:
               - Does the text say "Scrape /some-path"?
                 -> You MUST write Python code to `requests.get()` that specific URL.
                 -> Do NOT return the HTML of the main page as the answer.
               - Does it require math/counting?
                 -> Write Python code to calculate it.
               - Code Constraints: Use `pd`, `requests`, `urljoin`. Print the FINAL SINGLE VALUE only.
            
            OUTPUT JSON:
            {{
                "submission_url": "https://...",
                "python_code": "import requests... url = urljoin(task_url, '/path')... resp = requests.get(url)... print(resp.text)",
                "text_answer": "answer_if_no_code_needed"
            }}
            """

            plan = await get_llm_plan(prompt)
            if not plan:
                return

            submission_url = plan.get("submission_url")
            final_answer = plan.get("text_answer")
            python_code = plan.get("python_code")

            # Validate Submission URL (Fix common relative path issues)
            if submission_url and not submission_url.startswith("http"):
                submission_url = urljoin(task_url, submission_url)
                print(f"🔗 Fixed Relative Submission URL: {submission_url}")

            # 3. Execute Code
            if python_code and python_code != "null":
                print("⚙️ Executing Python Code...")
                old_stdout = sys.stdout
                redirected_output = io.StringIO()
                sys.stdout = redirected_output
                try:
                    # Pass context variables to the code
                    exec_globals = {
                        'pd': pd, 
                        'requests': requests, 
                        'print': print, 
                        'urljoin': urljoin,
                        'task_url': task_url,
                        'email': email
                    }
                    exec(python_code, exec_globals)
                    final_answer = redirected_output.getvalue().strip()
                except Exception as e:
                    print(f"❌ Code Error: {e}")
                    final_answer = "Error"
                finally:
                    sys.stdout = old_stdout
                print(f"✅ Computed Answer: {final_answer}")

            # 4. Submit
            submit_payload = {
                "email": email,
                "secret": secret,
                "url": task_url,
                "answer": final_answer 
            }
            
            print(f"📤 Submitting to {submission_url}...")
            resp = requests.post(submission_url, json=submit_payload)
            print(f"✅ Status: {resp.status_code}")

            # 5. RECURSIVE LOOP
            try:
                resp_json = resp.json()
                next_url = resp_json.get("url")
                if next_url:
                    if not next_url.startswith("http"):
                        next_url = urljoin(task_url, next_url)
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
