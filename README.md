# Jira Oversight · Service

Jira Oversight checks Jira boards against an agreed delivery process and shows where the work breaks it. This service reads Jira (read-only), keeps a local copy in SQLite, runs the rules and serves the dashboard's API, including Ask (plain-English questions answered by Claude) and rules drafted with AI.

**Dashboard:** [jira-oversight-ui](https://github.com/MinhazulHasan/jira-oversight-ui)

## Set up on a new machine

Requirements: Python 3.13, and a read-only Jira Cloud account with an API token.

1. Clone both repositories into the same folder.

```powershell
git clone https://github.com/MinhazulHasan/jira-oversight-sys.git
git clone https://github.com/MinhazulHasan/jira-oversight-ui.git
```

2. Create the virtual environment and install the dependencies.

```powershell
cd jira-oversight-sys
py -3.13 -m venv jira_env
.\jira_env\Scripts\Activate.ps1    # macOS/Linux: source jira_env/bin/activate
pip install -r requirements.txt
```

3. Create `.env` from the template and fill in at least `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN`, `JIRA_PROJECTS` and `API_SERVICE_KEY`. Use `LLM_MODE=stub` to run without a model, or `LLM_MODE=live` with the AWS Bedrock keys for Claude.

```powershell
Copy-Item .env.example .env
```

4. Check the Jira connection. This also writes `config/fields.yaml` for your Jira site.

```powershell
python -m oversight doctor
```

5. Load the data: copy `data/oversight.db` from an existing machine, or run the first full sync (a few minutes per board).

```powershell
python -m oversight cycle --full
```

6. Start the API on port 3300.

```powershell
python run.py
```

## More
- Tests: `python -m pytest -q`
