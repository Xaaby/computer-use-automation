# Cursor Setup for Overnight Auto Development

## Step 1: Open the Repo in Cursor
```
File → Open Folder → C:\Users\AbhishekkumarYadav\Documents\computer-use-automation
```

## Step 2: Set Up the Environment
In Cursor's terminal:
```bash
pip install -e .
playwright install chromium
```

## Step 3: Copy All Project Files Into the Repo
Copy these files from wherever you saved them into the repo root:
- RULES.md
- PHASE_STATUS.md
- PHASE_1_INSTRUCTIONS.md
- PHASE_2_INSTRUCTIONS.md
- PHASE_3_INSTRUCTIONS.md
- PHASE_4_INSTRUCTIONS.md
- PHASE_5_INSTRUCTIONS.md
- PHASE_6_7_INSTRUCTIONS.md
- GPT_RESEARCH_TASKS.md
- pyproject.toml
- .env.example
- .gitignore (already exists)

## Step 4: Create Your .env File
Your .env file should already exist with:
```
AWS_ACCESS_KEY_ID=<your key>
AWS_SECRET_ACCESS_KEY=<your secret>
AWS_REGION=us-east-1
BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-20250514-v1:0
APP_USERNAME=admin
APP_PASSWORD=admin
```

## Step 5: How to Start Each Phase in Cursor

### Method: Agent Mode (Recommended for Overnight)
1. Open Cursor
2. Press Cmd+Shift+P → "Cursor: Open Chat"
3. Select "Agent" mode (not "Edit" mode)
4. Paste this prompt for Phase 1:

```
Read RULES.md and PHASE_1_INSTRUCTIONS.md in the project root.
Implement everything in PHASE_1_INSTRUCTIONS.md exactly as specified.
After each file, run the tests specified in that file.
Fix any errors before moving to the next file.
When all files are complete and tests pass, update PHASE_STATUS.md marking Phase 1 complete.
Do not start Phase 2 until Phase 1 tests pass.
```

5. Set Cursor to auto-accept changes (Settings → enable auto-accept)
6. Let it run. Check back in 4-5 hours.

### For Each Subsequent Phase
When you return and Phase 1 is done, start Phase 2:
```
Read RULES.md and PHASE_2_INSTRUCTIONS.md.
Phase 1 is complete. Implement Phase 2 exactly as specified.
Run checkpoint verification after each file. Fix errors before continuing.
Update PHASE_STATUS.md when done.
```

## Step 6: Cursor Model Settings
- Use Claude Sonnet 4.5 (or max available) for code generation inside Cursor
- Your AWS Bedrock key is for the RUNTIME agent, not for Cursor itself

## Step 7: Your Check-in Commands (Every 4-5 Hours)

```bash
# Quick health check
python -m pytest tests/ -v --tb=short 2>&1 | tail -30

# What phase are we on?
cat PHASE_STATUS.md | head -20

# Is the Flask app working?
python target_app/app.py &
curl -s http://localhost:5000/members/search | head -5
```

## Common Issues and Fixes

**"NotImplementedError" on startup:**
→ ProactorEventLoop not set. Check that every async entrypoint has:
```python
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
```

**"Module not found" errors:**
→ Run `pip install -e .` again

**Playwright browser not found:**
→ Run `playwright install chromium`

**Bedrock credentials error:**
→ Check .env file exists and has correct keys
→ Run: `python -c "import boto3, os; from dotenv import load_dotenv; load_dotenv(); print(boto3.client('bedrock-runtime', region_name=os.environ['AWS_REGION']).list_foundation_models()['modelSummaries'][0]['modelId'])"`

**Tests fail on Windows path issues:**
→ Check all file paths use pathlib.Path, not strings

## What to Tell Cursor If It Goes Off Track

If Cursor has done something wrong, paste this correction:
```
Stop. Read RULES.md again. You have violated rule [X].
Specifically: [what went wrong].
Revert [file] and reimplement following RULES.md exactly.
The correct approach is: [from the relevant phase instruction file].
```