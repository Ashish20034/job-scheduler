document.addEventListener("DOMContentLoaded", () => {
    const outputBox = document.getElementById("outputBox");
    if (outputBox) {
        const eventSource = new EventSource("/stream_logs");
        
        eventSource.onmessage = (e) => {
            if (e.data) {
                const timestamp = new Date().toLocaleTimeString();
                outputBox.innerHTML += `<span class="text-info">[${timestamp}]</span> ${e.data}<br>`;
                outputBox.scrollTop = outputBox.scrollHeight;
            }
        };
        
        eventSource.onerror = (e) => {
            console.error("EventSource failed:", e);
            outputBox.innerHTML += `<span class="text-danger">[ERROR] Connection to log stream lost. Refresh page to reconnect.</span><br>`;
            eventSource.close();
        };
    }
});

function clearLogs() {
    if (confirm("Are you sure you want to clear all logs? This action cannot be undone.")) {
        fetch("/clear_logs")
            .then(response => {
                if (response.ok) {
                    const outputBox = document.getElementById("outputBox");
                    if (outputBox) {
                        outputBox.innerHTML = "Logs cleared successfully. Waiting for new output...<br>";
                    }
                } else {
                    alert("Failed to clear logs. Please try again.");
                }
            })
            .catch(error => {
                console.error("Error clearing logs:", error);
                alert("Error clearing logs. Please try again.");
            });
    }
}

// Handle script execution forms
document.addEventListener("DOMContentLoaded", () => {
    const scriptForms = document.querySelectorAll('form[action="/run_script"]');
    scriptForms.forEach(form => {
        form.addEventListener("submit", function(e) {
            e.preventDefault();
            const formData = new FormData(this);
            const scriptName = formData.get('script_name');
            
            if (confirm(`Run script "${scriptName}"?`)) {
                runScript(scriptName);
            }
        });
    });
});

function runScript(scriptName) {
    const outputBox = document.getElementById("outputBox");
    if (outputBox) {
        outputBox.innerHTML += `<span class="text-warning">[EXECUTING] Running script: ${scriptName}</span><br>`;
        outputBox.scrollTop = outputBox.scrollHeight;
    }
    
    fetch("/run_script", {
        method: "POST",
        headers: {
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body: `script_name=${encodeURIComponent(scriptName)}`
    })
    .then(response => {
        const eventSource = new EventSource("/stream_logs");
        eventSource.onmessage = (e) => {
            if (outputBox && e.data) {
                outputBox.innerHTML += e.data + "<br>";
                outputBox.scrollTop = outputBox.scrollHeight;
            }
        };
        
        setTimeout(() => {
            eventSource.close();
        }, 30000); // Close after 30 seconds
    })
    .catch(error => {
        console.error("Error running script:", error);
        if (outputBox) {
            outputBox.innerHTML += `<span class="text-danger">[ERROR] Failed to run script: ${error}</span><br>`;
        }
    });
}
