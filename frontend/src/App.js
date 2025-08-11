import React, { useState } from 'react';

function App() {
  // Form state
  const [ideaText, setIdeaText] = useState('');
  const [ideaCategory, setIdeaCategory] = useState('General');
  const [submitMessage, setSubmitMessage] = useState('');

  // Submit new bad practice idea to backend
  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!ideaText.trim()) {
      return;
    }
    try {
      const response = await fetch('/api/suggestions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
          domain: ideaCategory, 
          description: ideaText 
        })
      });
      if (response.ok) {
        setSubmitMessage('Thank you! Your bad-practice idea has been submitted.');
        setIdeaText('');
        // Optionally reset category or keep the same
      } else {
        setSubmitMessage('Error: Could not submit your idea. Please try again later.');
      }
    } catch (error) {
      console.error('Submit failed:', error);
      setSubmitMessage('Network error. Please try again.');
    }
  };

  return (
    <div className="container">
      <header className="header">
        <h1>🛡️ Bad Practice AI Agent</h1>
        <p className="subtitle">Your AI DevOps code reviewer – detecting bad practices before they hit production.</p>
      </header>

      <main>
        <section className="instructions">
          <h2>How to Use</h2>
          <ol>
            <li><strong>Install the CLI:</strong> <code>pip install bp-agent</code> (for example).</li>
            <li><strong>Initialize:</strong> In your project directory, run <code>bp init</code> to set up the agent.</li>
            <li><strong>Scan Code:</strong> Run <code>bp scan</code> to scan your code for bad practices. The agent will output warnings for any issues found.</li>
          </ol>
          <p>After scanning, you'll see a list of warnings if any bad practices are detected. For example, a warning might say a Dockerfile is using the <code>latest</code> tag (which is a bad practice).</p>
          <p className="note">*Make sure you have an internet connection when scanning, as the AI will query the knowledge base and ChatGPT.</p>
        </section>

        <section className="suggestion-form">
          <h2>Contribute an Idea</h2>
          <p>Know a bad practice that's not covered yet? Help us improve by submitting it below:</p>
          <form onSubmit={handleSubmit}>
            <label>
              Category: 
              <select 
                value={ideaCategory} 
                onChange={(e) => setIdeaCategory(e.target.value)}
              >
                <option value="General">General</option>
                <option value="Docker">Docker</option>
                <option value="Kubernetes">Kubernetes</option>
                <option value="Terraform">Terraform</option>
                <option value="Jenkins">Jenkins</option>
                <option value="ArgoCD">ArgoCD</option>
              </select>
            </label>
            <br />
            <label>
              Bad Practice Description:<br/>
              <textarea 
                value={ideaText} 
                onChange={(e) => setIdeaText(e.target.value)} 
                placeholder="Describe the bad practice and why it's harmful..." 
                rows="4" cols="50" 
                required
              />
            </label>
            <br />
            <button type="submit" className="submit-btn">Submit Idea</button>
          </form>
          {submitMessage && <p className="submit-msg">{submitMessage}</p>}
        </section>
      </main>

      <footer>
        <p>Bad Practice AI Agent &copy; 2025. Built with ❤️ and ChatGPT.</p>
      </footer>
    </div>
  );
}

export default App;
