# Bad-Practice Agent


## Introduction
Bad-Practice Agent is a local DevOps *watchdog* that uses a powerful LLM (Large Language Model) to scan your source code for bad practices and potential issues. Running in your own environment via a CLI tool, it catches anti-patterns, bugs, and security vulnerabilities that traditional linters might miss. When triggered, it automatically reviews the codebase and emails you detailed reports of its findings – providing a full project audit on first run, and focused alerts for individual files on subsequent changes. 

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
    .
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

        git clone https://github.com/YourUsername/badpractice-agent.git
        cd badpractice-agent

2. **Configure environment variables:** Copy `.env.example` to `.env` and fill in the required settings. At minimum, set up:
    - LLM API credentials (e.g. an OpenAI API key for GPT-4).
    - SMTP settings for email (host, port, username, password).
    - The recipient email address (where reports should be sent).
    - (Optional) MongoDB connection URI if using an external Mongo instance. (By default, the Docker setup uses the included `mongo` service for local data storage.)

3. **Launch the services** with Docker Compose:

        docker-compose up -d --build

   This will spin up the Flask API (backend), the React frontend (served via Nginx), and the MongoDB service (if enabled). The web UI will be accessible at **http://localhost** (default port 80), and the API will listen at **http://localhost/api/**.

4. **Initialize the agent** via the CLI:

        badpractice init

   Running the `init` command performs a full scan of the codebase. The LLM will analyze all source files for issues, and a comprehensive audit report email will be sent out summarizing all findings across the project.  
   *Tip:* Ensure your SMTP credentials and recipient email are configured correctly – you should receive the initial audit report in your inbox once the scan completes.

## Usage

### Initial Audit (Full Project Scan)
On first run (`badpractice init`), the agent scans the entire repository. It compiles all detected issues into a single email report for easy review. Each issue in the report typically includes the file name, a description of the bad practice, and often a suggestion or example of a better approach (generated by the LLM). This gives you an immediate overview of the state of your codebase and what needs attention.

### Continuous Monitoring & Per-File Alerts
After the initial audit, BadPractice Agent can monitor your project for any new or modified files:
- **Automatic Scanning:** If you keep the agent running (with Docker Compose up), it will automatically detect changes to source files. Whenever you add or modify a file and introduce a new issue, the backend will run an LLM analysis on that file.
- **Targeted Email Alerts:** Instead of emailing the whole project report again, the agent sends a focused email for that single file, detailing the new bad practice found. This way, you'll receive one email per file with issues, at the time those issues appear.
- **No Noise, No Duplicates:** If a modified file has no bad practices, no email is sent (you won't be bothered unless there's a problem). Additionally, the agent (with help from MongoDB) tracks issues that have been reported previously, so you won't get repeat alerts for the exact same issue unless it reappears or regresses.
- **Web Dashboard:** You can also open the web UI (via Nginx) at **http://localhost** to view a dashboard of all findings. The React app displays a list of files with issues, descriptions of each issue, and timestamps. This provides a convenient overview of code health without digging through emails.

### CLI Examples
- `badpractice init` – Perform a full audit scan of the project (triggers the comprehensive report email).
- *Planned:* `badpractice scan <path>` – Scan a specific file or folder on demand (e.g. to re-check a file after fixes).
- *Planned:* `badpractice status` – Show a summary of current issues or last audit results in the console.

*(The above "Planned" commands are potential future additions.)*

## License
Distributed under the **MIT License**. See the [LICENSE](LICENSE) file for details.
