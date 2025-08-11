# Bad-Practice Agent


## Introduction
Bad-Practice Agent is a local DevOps *watchdog* that uses a powerful LLM (Large Language Model) to scan your source code for bad practices and potential issues. Running in your own environment via a CLI tool, it catches anti-patterns and security vulnerabilities that traditional linters might miss. When triggered, it automatically reviews the codebase and emails you detailed reports of its findings – providing a full project audit on first run, and focused alerts for individual files on subsequent changes. 

The agent continuously watches DevOps files and only analyzes those: Terraform (.tf, .tfvars, .hcl), Kubernetes / Helm / Kustomize YAML (*.yaml, *.yml, plus chart.yaml, values.yaml, kustomization.yaml), Docker (Dockerfile, *.dockerfile, docker-compose.yml|.yaml, compose.yml|.yaml), Jenkins (Jenkinsfile), and JSON configs (*.json). It also catches Argo CD resources (any YAML with argoproj.io) and MongoDB configs expressed in YAML/JSON, flagging bad practices when they appear.

## Motivation
Modern codebases can suffer from subtle issues that slip through. **Bad-Practice Agent** was created to address this gap by leveraging AI for continuous code quality assurance:
- **Proactive Quality Control:** Detects code smells, deprecated patterns, and security pitfalls early, before they reach production.
- **Immediate Feedback:** Alerts developers via email as soon as bad practices are introduced, enabling quick fixes in the development cycle.
- **Maintain Standards:** Enforces coding best practices consistently across the project, helping teams maintain clean, maintainable code.
- **Local and Secure:** Runs in your environment (via Docker) – your code and analysis stay within your infrastructure.

## Architecture
The BadPractice Agent consists of several components orchestrated with Docker Compose, as illustrated below:

    User CLI ------+             +--------------+       +-----------+ 
                   |             |              |--->   |           | 
                   +-----------> |   Flask API  |       |   LLM     | 
      (triggers via HTTP)        |   (Backend)  |--+    |  (Analyzer)| 
                                 |              |  |    |           | 
                                 +--------------+  |    +-----------+ 
                                     |   ^         | 
                                     v   |         |    +---------+ 
                                         |         +--> |  SMTP   | 
                                MongoDB  |      
                               (Storage) |             (Email Server) 
                                     |   | 
                                     v   | 
                                 +------------+ 
                                 |  React UI  |  (Static frontend 
                                 +------------+   served via Nginx)

**Components:**
- **CLI Tool:** Command-line interface for initiating scans (`init`) and interacting with the agent.
- **Flask API (Backend):** Receives analysis requests from the CLI (and UI), processes file scans using the LLM, and coordinates results storage and email notifications.
- **LLM Engine:** An AI model (e.g. GPT-4 or similar) that reviews code for bad practices and provides contextual feedback/suggestions.
- **MongoDB (Optional):** A database for storing user bad-practices ideas.
- **Email SMTP Service:** Used to send out email reports. The agent composes summary emails of findings and delivers them to the configured recipients via SMTP.
- **React Frontend:** A static web page for users to send their bad-practices ideas.
- **Nginx:** Web server that serves the React frontend and acts as a reverse proxy for API calls.

## Project Structure
    
    ├── backend/               
    │   ├── app.py             
    │   ├── requirements.txt   
    │   └── Dockerfile          
    ├── frontend/
    │   ├── package-lock.json
    │   ├── package.json
    │   ├── nginx.conf
    │   ├── Dockerfile
    │   ├── public/           
    │   ├── src/               
    │   └── build/            
    ├── cli/                
    │   ├── bp.py     
    ├── docker-compose.yml       
    ├── .env
    ├── .gitignore
   

## Setup

1. **Clone the repository** and navigate into it:

        git clone https://github.com/lavi324/badpractice-agent.git
        cd badpractice-agent

2. **Configure environment variables:** 
    - LLM API credentials.
    - SMTP settings for email (host, port, username, password).
    - The recipient email address (where reports should be sent).
    

3. **Launch the services** with Docker Compose:

        docker-compose up -d --build

   This will spin up the Flask API (backend) and the React frontend. 

4. **Initialize the agent** via the CLI:

        python3 path_to_bp.py init

   Running the `init` command performs a full scan of the codebase. The LLM will analyze all source files for issues, and a comprehensive audit report email will be sent out summarizing all findings across the project.  

## Usage

### Initial Audit (Full Project Scan)
On first run (` python3 path_to_bp.py init`), the agent scans the entire repository. It compiles all detected issues into a single email report for easy review. Each issue in the report typically includes the file name, a description of the bad practice, and often a suggestion or example of a better approach (generated by the LLM). This gives you an immediate overview of the state of your codebase and what needs attention.

### Continuous Monitoring & Per-File Alerts
After the initial audit, the agent will monitor your project for any new or modified files:
- **Automatic Scanning:** If you keep the agent running (with Docker Compose up), it will automatically detect changes to source files. Whenever you add or modify a file and introduce a new issue, the backend will run an LLM analysis on that file.
- **Targeted Email Alerts:** Instead of emailing the whole project report again, the agent sends a focused email for that single file, detailing the new bad practice found. This way, you'll receive one email per file with issues, at the time those issues appear.
- **No Noise, No Duplicates:** If a modified file has no bad practices, no email is sent (you won't be bothered unless there's a problem).


### CLI Examples
- `python3 path_to_bp.py init` – Perform a full audit scan of the project (triggers the comprehensive report email).
- 'python3 path_to_bp.py stop' - Stop the agent from monitoring.
- 'python3 path_to_bp.py status' - reports whether your background BadPractice Agent is running and shows the last lines of its log.




