import os
import sys
import asyncio
import json
import requests
import pandas as pd
import numpy as np
import io
import re
import bs4
import ssl
from urllib.parse import urljoin
from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel
from playwright.async_api import async_playwright
from openai import OpenAI

# --- WINDOWS FIX ---
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# --- SSL FIX ---
ssl._create_default_https_context = ssl._create_unverified_context

# --- CONFIGURATION ---
# !!! PASTE YOUR GROQ KEY HERE !!!
GROQ_API_KEY = "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"

client = None
if GROQ_API_KEY and "PASTE_YOUR" not in GROQ_API_KEY:
    try:
        client = OpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1"
        )
        print("✅ Groq Client Configured")
    except Exception as e:
        print(f"❌ Groq Setup Error: {e}")

MY_SECRET = "UNKNOWN"

app = FastAPI()

class TaskPayload(BaseModel):
    email: str
    secret: str
    url: str

async def get_llm_plan(prompt_text):
    if not client:
        return None
    try:
        print("🤖 Asking Groq (Llama 3.3)...")
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a senior python developer. You write robust, fault-tolerant code."},
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
            await asyncio.sleep(1) 
            
            content = await page.evaluate("document.body.innerText")
            links = await page.evaluate("""
                Array.from(document.querySelectorAll('a')).map(a => 
                    `[LINK: ${a.innerText}] (URL: ${a.href})`
                ).join('\\n')
            """)
            
            full_context = f"MAIN TEXT:\n{content}\n\n--- LINKS FOUND ---\n{links}"
            print(f"📄 Scraped Context (first 500 chars):\n{full_context[:500]}...")

            # 2. Plan (SMART PROMPT WITH AUTO-CORRECTION LOGIC)
            prompt = f"""
            You are a Data Science Agent.
            CURRENT PAGE URL: {task_url}
            
            PAGE CONTENT:
            ---
            {full_context}
            ---
            
            TASKS:
            
            1. IDENTIFY SUBMISSION URL:
               - If relative, make absolute using `urljoin`.
            
            2. GENERATE PYTHON CODE TO SOLVE THE QUESTION:
            
               **SCENARIO A: CSV/EXCEL PROCESSING**
               - If the text implies downloading data (CSV/Excel) and summing/filtering:
               - You MUST write code that handles ANY column name.
               - Code Template to use:
                 df = pd.read_csv(data_url)
                 # Smart Column Detection
                 numeric_cols = df.select_dtypes(include=[np.number]).columns
                 target_col = numeric_cols[0] # Just pick the first numeric column
                 # Check filters in text (e.g. "Cutoff 10000")
                 if 'cutoff' in text_instructions_lower:
                     df = df[df[target_col] > cutoff_value]
                 print(df[target_col].sum())
            
               **SCENARIO B: SCRAPING A LINK**
               - If text says "Scrape [Link]":
               - Code Template to use:
                 resp = requests.get(target_url, headers={{'User-Agent': 'Mozilla/5.0'}})
                 # STRIP HTML TAGS
                 clean_text = bs4.BeautifulSoup(resp.text, 'html.parser').get_text().strip()
                 print(clean_text)
            
            OUTPUT JSON:
            {{
                "submission_url": "https://...",
                "python_code": "import requests... import pandas as pd... (Your robust code here)",
                "text_answer": "answer_if_no_code_needed"
            }}
            """

            plan = await get_llm_plan(prompt)
            if not plan:
                return

            submission_url = plan.get("submission_url")
            final_answer = plan.get("text_answer")
            python_code = plan.get("python_code")

            if submission_url and not submission_url.startswith("http"):
                submission_url = urljoin(task_url, submission_url)

            # 3. Execute Code
            if python_code and python_code != "null":
                print("⚙️ Executing Python Code...")
                old_stdout = sys.stdout
                redirected_output = io.StringIO()
                sys.stdout = redirected_output
                try:
                    # Provide ALL necessary libraries to the execution environment
                    exec_globals = {
                        'pd': pd, 'np': np, 'requests': requests, 'print': print, 
                        'urljoin': urljoin, 'task_url': task_url, 
                        'email': email, 're': re, 'bs4': bs4, 'io': io, 'json': json,
                        'text_instructions_lower': content.lower() # Helper for the prompt logic
                    }
                    exec(python_code, exec_globals)
                    final_answer = redirected_output.getvalue().strip()
                except Exception as e:
                    print(f"❌ Code Error: {e}")
                    final_answer = f"Error: {e}"
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
            print(f"✅ Status: {resp.status_code} | {resp.text}")

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
