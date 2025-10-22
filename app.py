from flask import Flask, render_template, request, redirect, url_for, session, flash, Response, jsonify
import subprocess
import os
import sqlite3
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import threading
import json

app = Flask(__name__)
app.secret_key = "supersecretkey"

# Get the absolute path of the current directory
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cron.db")
LOG_FILE = os.path.join(BASE_DIR, "cron_output.log")
SCRIPT_DIR = os.path.join(BASE_DIR, "scripts")
ANACRON_DIR = os.path.join(BASE_DIR, "anacron")
ANACRON_JOBS_FILE = os.path.join(ANACRON_DIR, "jobs.json")

print(f"📁 Base Directory: {BASE_DIR}")
print(f"📁 Script Directory: {SCRIPT_DIR}")
print(f"📁 Log File: {LOG_FILE}")
print(f"📁 Anacron Directory: {ANACRON_DIR}")

# Email Configuration - Updated with your credentials
EMAIL_CONFIG = {
    'smtp_server': 'smtp.gmail.com',
    'smtp_port': 587,
    'sender_email': 'ashishshinde20034@gmail.com',
    'sender_password': 'vrrxnbrwrieqlstk',
    'use_tls': True
}

# ---------- DATABASE SETUP ----------
def init_db():
    """Create the necessary tables if they don't exist"""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Create users table (ensure email column exists)
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password_hash TEXT,
        email TEXT
    )''')

    # Create jobs table with expected columns
    c.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule TEXT,
        command TEXT,
        created_at TEXT,
        last_run TEXT,
        status TEXT DEFAULT 'active',
        email_notifications BOOLEAN DEFAULT 1
    )''')

    # Ensure columns exist in case this DB was created with an older schema
    # (safe guards: only attempt ALTER if column missing)
    # For jobs table, check and add missing columns
    c.execute("PRAGMA table_info(jobs)")
    job_cols = [row[1] for row in c.fetchall()]
    if 'email_notifications' not in job_cols:
        try:
            c.execute("ALTER TABLE jobs ADD COLUMN email_notifications BOOLEAN DEFAULT 1")
            print("🔄 Added email_notifications column to jobs")
        except sqlite3.OperationalError:
            print("⚠️ Could not add email_notifications column (maybe already exists)")
    if 'last_run' not in job_cols:
        try:
            c.execute("ALTER TABLE jobs ADD COLUMN last_run TEXT")
            print("🔄 Added last_run column to jobs")
        except sqlite3.OperationalError:
            print("⚠️ Could not add last_run column (maybe already exists)")
    if 'status' not in job_cols:
        try:
            c.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'active'")
            print("🔄 Added status column to jobs")
        except sqlite3.OperationalError:
            print("⚠️ Could not add status column (maybe already exists)")

    # For users table, ensure email exists
    c.execute("PRAGMA table_info(users)")
    user_cols = [row[1] for row in c.fetchall()]
    if 'email' not in user_cols:
        try:
            c.execute("ALTER TABLE users ADD COLUMN email TEXT")
            print("🔄 Added email column to users")
        except sqlite3.OperationalError:
            print("⚠️ Could not add email column to users (maybe already exists)")

    conn.commit()

    # Create default user if not exists
    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        hashed_password = generate_password_hash("admin123", method='pbkdf2:sha256')
        c.execute("INSERT INTO users (username, password_hash, email) VALUES (?, ?, ?)",
                  ("admin", hashed_password, "ashishshinde20034@gmail.com"))
        conn.commit()
        print("✅ Default user created -> Username: admin | Password: admin123 | Email: ashishshinde20034@gmail.com")

    conn.close()


def migrate_database():
    """Migrate existing database to new schema in a safe way."""
    # If DB file doesn't even exist, nothing to migrate (init_db will create tables).
    if not os.path.exists(DB_PATH):
        print("ℹ️ Database file not found; migration skipped (it will be created).")
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    # Check what tables exist
    c.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in c.fetchall()]
    print(f"📊 Existing tables in DB: {tables}")

    # If jobs table doesn't exist yet, nothing to migrate for jobs
    if 'jobs' not in tables:
        print("ℹ️ 'jobs' table not present; migration for jobs skipped (init_db will create it).")
    else:
        # Get list of columns in jobs table and add missing ones only
        c.execute("PRAGMA table_info(jobs)")
        columns = [col[1] for col in c.fetchall()]
        print(f"📊 Current jobs table columns: {columns}")

        if 'email_notifications' not in columns:
            try:
                print("🔄 Adding email_notifications column...")
                c.execute("ALTER TABLE jobs ADD COLUMN email_notifications BOOLEAN DEFAULT 1")
            except sqlite3.OperationalError as e:
                print(f"⚠️ Could not add email_notifications: {e}")

        if 'last_run' not in columns:
            try:
                print("🔄 Adding last_run column...")
                c.execute("ALTER TABLE jobs ADD COLUMN last_run TEXT")
            except sqlite3.OperationalError as e:
                print(f"⚠️ Could not add last_run: {e}")

        if 'status' not in columns:
            try:
                print("🔄 Adding status column...")
                c.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'active'")
            except sqlite3.OperationalError as e:
                print(f"⚠️ Could not add status: {e}")

    # If users table exists but missing email column, add it
    if 'users' in tables:
        c.execute("PRAGMA table_info(users)")
        ucols = [col[1] for col in c.fetchall()]
        print(f"📊 Current users table columns: {ucols}")
        if 'email' not in ucols:
            try:
                print("🔄 Adding email column to users table...")
                c.execute("ALTER TABLE users ADD COLUMN email TEXT")
            except sqlite3.OperationalError as e:
                print(f"⚠️ Could not add email column to users: {e}")

    conn.commit()
    conn.close()
    print("✅ Database migration completed!")

# Call this before init_db to ensure migration
migrate_database()
init_db()

# ---------- ANACRON SETUP ----------
def init_anacron():
    """Initialize anacron directory and files"""
    os.makedirs(ANACRON_DIR, exist_ok=True)
    if not os.path.exists(ANACRON_JOBS_FILE):
        with open(ANACRON_JOBS_FILE, 'w') as f:
            json.dump({"jobs": [], "last_run": {}}, f)


def load_anacron_jobs():
    """Load anacron jobs from JSON file"""
    try:
        with open(ANACRON_JOBS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {"jobs": [], "last_run": {}}


def save_anacron_jobs(data):
    """Save anacron jobs to JSON file"""
    with open(ANACRON_JOBS_FILE, 'w') as f:
        json.dump(data, f, indent=2)


def get_anacron_jobs():
    """Get all anacron jobs"""
    data = load_anacron_jobs()
    return data["jobs"]


def add_anacron_job(job_id, schedule, command, email_notifications=True):
    """Add a new anacron job"""
    data = load_anacron_jobs()

    # Convert cron schedule to anacron format (days)
    days = convert_cron_to_days(schedule)

    job = {
        "id": job_id,
        "schedule": schedule,
        "anacron_days": days,
        "command": command,
        "created_at": datetime.now().isoformat(),
        "email_notifications": email_notifications,
        "status": "active"
    }

    data["jobs"].append(job)
    save_anacron_jobs(data)
    return job


def convert_cron_to_days(schedule):
    """Convert cron schedule to anacron days format"""
    parts = schedule.split()
    if len(parts) != 5:
        return 1  # Default to daily

    minute, hour, day, month, weekday = parts

    # Enhanced conversion logic
    if day != '*' and day != '*/1':
        try:
            return int(day) if day.isdigit() else 1
        except:
            return 1
    elif weekday != '*' and weekday != '*/1':
        return 7  # Weekly
    elif hour != '*' and hour != '*/1':
        return 1  # Daily
    else:
        return 1  # Default daily


def remove_anacron_job(job_id):
    """Remove an anacron job"""
    data = load_anacron_jobs()
    data["jobs"] = [job for job in data["jobs"] if job["id"] != job_id]
    save_anacron_jobs(data)


# ---------- EMAIL FUNCTIONS ----------
def send_email(recipient, subject, body):
    """Send email notification"""
    try:
        print(f"📧 Attempting to send email to {recipient}...")

        msg = MIMEMultipart()
        msg['From'] = EMAIL_CONFIG['sender_email']
        msg['To'] = recipient
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'html'))

        server = smtplib.SMTP(EMAIL_CONFIG['smtp_server'], EMAIL_CONFIG['smtp_port'])
        server.ehlo()

        if EMAIL_CONFIG['use_tls']:
            server.starttls()
            server.ehlo()

        server.login(EMAIL_CONFIG['sender_email'], EMAIL_CONFIG['sender_password'])
        text = msg.as_string()
        server.sendmail(EMAIL_CONFIG['sender_email'], recipient, text)
        server.quit()

        print(f"✅ Email sent successfully to {recipient}")
        return True
    except Exception as e:
        print(f"❌ Failed to send email: {str(e)}")
        return False


def send_job_added_email(username, email, job_details):
    """Send job added notification"""
    subject = "✅ Anacron Job Added Successfully - Cron Dashboard"
    body = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .header {{ background: linear-gradient(135deg, #667eea, #764ba2); color: white; padding: 20px; border-radius: 8px; text-align: center; }}
            .job-details {{ background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0; }}
            .footer {{ text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #dee2e6; color: #6c757d; }}
            table {{ width: 100%; border-collapse: collapse; }}
            td {{ padding: 8px; border-bottom: 1px solid #dee2e6; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>🌱 Anacron Job Added Successfully</h2>
                <p>Your scheduled task has been configured</p>
            </div>

            <div class="job-details">
                <h3 style="color: #495057;">📋 Job Details:</h3>
                <table>
                    <tr>
                        <td style="font-weight: bold; width: 30%;">Job ID:</td>
                        <td><strong>#{job_details['id']}</strong></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Schedule:</td>
                        <td><code style="background: #e9ecef; padding: 4px 8px; border-radius: 4px;">{job_details['schedule']}</code></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Command:</td>
                        <td><code style="background: #e9ecef; padding: 4px 8px; border-radius: 4px;">{job_details['command']}</code></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Added Time:</td>
                        <td>{job_details['created_at']}</td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Status:</td>
                        <td><span style="color: #28a745;">● Active</span></td>
                    </tr>
                </table>
            </div>

            <div style="background: #e7f3ff; padding: 15px; border-radius: 8px; margin: 20px 0;">
                <h4 style="color: #004085; margin-top: 0;">💡 Anacron Advantage</h4>
                <p style="margin-bottom: 0; color: #004085;">
                    This job uses <strong>Anacron technology</strong> - it will run even when your system is offline 
                    and execute automatically when the system comes back online.
                </p>
            </div>

            <div style="text-align: center; color: #6c757d;">
                <p>You will receive another email when this job executes successfully.</p>
            </div>

            <div class="footer">
                <p>Best regards,<br><strong>Cron Dashboard System</strong><br>
                <small>Automated Notification System</small></p>
            </div>
        </div>
    </body>
    </html>
    """

    thread = threading.Thread(target=send_email, args=(email, subject, body))
    thread.daemon = True
    thread.start()


def send_job_executed_email(username, email, job_details, execution_time, output):
    """Send job execution notification"""
    subject = f"✅ Job #{job_details['id']} Executed Successfully - {execution_time}"
    body = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .header {{ background: linear-gradient(135deg, #28a745, #20c997); color: white; padding: 20px; border-radius: 8px; text-align: center; }}
            .execution-details {{ background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0; }}
            .output-preview {{ background: #e7f3ff; padding: 15px; border-radius: 8px; margin: 20px 0; }}
            .footer {{ text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #dee2e6; color: #6c757d; }}
            table {{ width: 100%; border-collapse: collapse; }}
            td {{ padding: 8px; border-bottom: 1px solid #dee2e6; }}
            pre {{ background: #f8f9fa; padding: 10px; border-radius: 5px; overflow-x: auto; white-space: pre-wrap; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>✅ Job Executed Successfully</h2>
                <p>Your scheduled task has completed execution</p>
            </div>

            <div class="execution-details">
                <h3 style="color: #495057;">📊 Execution Details:</h3>
                <table>
                    <tr>
                        <td style="font-weight: bold; width: 30%;">Job ID:</td>
                        <td><strong>#{job_details['id']}</strong></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Schedule:</td>
                        <td><code style="background: #e9ecef; padding: 4px 8px; border-radius: 4px;">{job_details['schedule']}</code></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Command:</td>
                        <td><code style="background: #e9ecef; padding: 4px 8px; border-radius: 4px;">{job_details['command']}</code></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Execution Time:</td>
                        <td><strong>{execution_time}</strong></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">System Status:</td>
                        <td><span style="color: #28a745;">● Online</span></td>
                    </tr>
                </table>
            </div>

            <div class="output-preview">
                <h4 style="color: #004085; margin-top: 0;">📝 Output Preview:</h4>
                <pre>{output[:500]}{'...' if len(output) > 500 else ''}</pre>
                <p style="color: #6c757d; font-size: 12px; margin-bottom: 0;">
                    {f'Output truncated. {len(output) - 500} more characters in logs.' if len(output) > 500 else 'Full output shown.'}
                </p>
            </div>

            <div style="text-align: center; color: #6c757d;">
                <p>This job was executed using Anacron technology for reliable scheduling.</p>
            </div>

            <div class="footer">
                <p>Best regards,<br><strong>Cron Dashboard System</strong><br>
                <small>Automated Notification System</small></p>
            </div>
        </div>
    </body>
    </html>
    """

    thread = threading.Thread(target=send_email, args=(email, subject, body))
    thread.daemon = True
    thread.start()


def send_job_deleted_email(username, email, job_details):
    """Send job deleted notification"""
    subject = "🗑️ Job Deleted - Cron Dashboard"
    body = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Arial, sans-serif; background-color: #f4f4f4; padding: 20px; }}
            .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
            .header {{ background: linear-gradient(135deg, #ff6b6b, #ee5a52); color: white; padding: 20px; border-radius: 8px; text-align: center; }}
            .job-details {{ background: #f8f9fa; padding: 20px; border-radius: 8px; margin: 20px 0; }}
            .footer {{ text-align: center; margin-top: 30px; padding-top: 20px; border-top: 1px solid #dee2e6; color: #6c757d; }}
            table {{ width: 100%; border-collapse: collapse; }}
            td {{ padding: 8px; border-bottom: 1px solid #dee2e6; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h2>🗑️ Job Deleted Successfully</h2>
                <p>A scheduled task has been removed from your dashboard</p>
            </div>

            <div class="job-details">
                <h3 style="color: #495057;">📋 Deleted Job Details:</h3>
                <table>
                    <tr>
                        <td style="font-weight: bold; width: 30%;">Job ID:</td>
                        <td><strong>#{job_details['id']}</strong></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Schedule:</td>
                        <td><code style="background: #e9ecef; padding: 4px 8px; border-radius: 4px;">{job_details['schedule']}</code></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Command:</td>
                        <td><code style="background: #e9ecef; padding: 4px 8px; border-radius: 4px;">{job_details['command']}</code></td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Deleted Time:</td>
                        <td>{job_details['deleted_at']}</td>
                    </tr>
                    <tr>
                        <td style="font-weight: bold;">Status:</td>
                        <td><span style="color: #dc3545;">● Removed</span></td>
                    </tr>
                </table>
            </div>

            <div style="background: #fff3cd; padding: 15px; border-radius: 8px; margin: 20px 0;">
                <h4 style="color: #856404; margin-top: 0;">⚠️ Important Note</h4>
                <p style="margin-bottom: 0; color: #856404;">
                    This job has been permanently removed from the scheduling system and will no longer execute.
                    If this was a mistake, you can recreate the job from the dashboard.
                </p>
            </div>

            <div class="footer">
                <p>Best regards,<br><strong>Cron Dashboard System</strong><br>
                <small>Automated Notification System</small></p>
            </div>
        </div>
    </body>
    </html>
    """

    thread = threading.Thread(target=send_email, args=(email, subject, body))
    thread.daemon = True
    thread.start()


# ---------- ANACRON EXECUTION ----------
def execute_anacron_jobs():
    """Check and execute anacron jobs that are due"""
    data = load_anacron_jobs()
    current_time = datetime.now()

    for job in data["jobs"]:
        if job.get("status", "active") != "active":
            continue

        job_id = str(job["id"])
        last_run_str = data["last_run"].get(job_id)

        # Check if job needs to run
        should_run = False
        if not last_run_str:
            should_run = True
        else:
            try:
                last_run = datetime.fromisoformat(last_run_str)
                days_since_last_run = (current_time - last_run).days
                if days_since_last_run >= job.get("anacron_days", 1):
                    should_run = True
            except Exception:
                # If last_run stored in a bad format, schedule it to run
                should_run = True

        if should_run:
            print(f"🚀 Executing job #{job_id}: {job['command']}")
            execute_job(job, current_time)

            # Update last run time
            data["last_run"][job_id] = current_time.isoformat()

    save_anacron_jobs(data)


def execute_job(job, execution_time):
    """Execute a single job"""
    try:
        command = job["command"]

        # Handle Python scripts
        if command.endswith('.py'):
            python_path = get_full_python_path()
            script_path = os.path.join(SCRIPT_DIR, command)
            if os.path.exists(script_path):
                full_command = f'{python_path} "{script_path}"'
            else:
                log_message = f"❌ Script not found: {command}\n"
                log_to_file(log_message)
                return
        else:
            full_command = command

        # Execute command
        result = subprocess.run(full_command, shell=True, capture_output=True, text=True)
        output = (result.stdout or "") + (result.stderr or "")

        # Log execution
        log_message = f"[{execution_time.strftime('%Y-%m-%d %H:%M:%S')}] Job #{job['id']} - {command}\n"
        log_message += f"Output: {output}\n"
        log_message += f"Exit Code: {result.returncode}\n"
        log_message += "─" * 50 + "\n"

        log_to_file(log_message)

        # Send email notification if enabled
        if job.get("email_notifications", True):
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            try:
                c.execute("SELECT username, email FROM users LIMIT 1")
                user_record = c.fetchone()
            except Exception as e:
                user_record = None
                print(f"⚠️ Error fetching user for email notification: {e}")
            finally:
                conn.close()

            if user_record and user_record[1]:
                username, email = user_record
                send_job_executed_email(username, email, job, execution_time.strftime("%Y-%m-%d %H:%M:%S"), output)

    except Exception as e:
        error_message = f"❌ Error executing job #{job.get('id', 'unknown')}: {str(e)}\n"
        log_to_file(error_message)


def log_to_file(message):
    """Log message to file"""
    with open(LOG_FILE, "a") as f:
        f.write(message)


# ---------- HELPERS ----------
def get_full_python_path():
    """Get the full path to Python interpreter"""
    try:
        result = subprocess.run(['which', 'python3'], capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        return '/usr/bin/python3'
    except:
        return '/usr/bin/python3'


def validate_script_exists(script_name):
    """Check if script exists in scripts directory"""
    script_path = os.path.join(SCRIPT_DIR, script_name)
    return os.path.exists(script_path)


def get_job_status_counts():
    """Get success and failure counts from logs"""
    success_count = 0
    failure_count = 0
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            for line in f:
                if "❌" in line or "error" in line.lower() or "fail" in line.lower():
                    failure_count += 1
                elif "✅" in line or "success" in line.lower():
                    success_count += 1
    return success_count, failure_count


# ---------- AUTH ----------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username'].strip()
        password = request.form['password'].strip()

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT password_hash FROM users WHERE username=?", (username,))
        user_record = c.fetchone()
        conn.close()

        if user_record and check_password_hash(user_record[0], password):
            session['user'] = username
            flash(f"🎉 Welcome {username}!", "success")
            return redirect(url_for('index'))
        flash("❌ Invalid username or password!", "danger")
    return render_template("login.html")


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username'].strip()
        email = request.form['email'].strip()
        password = request.form['password'].strip()

        # Basic validation
        if not username or not email or not password:
            flash("❌ Please fill in all fields!", "danger")
            return render_template("register.html")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        
        try:
            # Check if username already exists
            c.execute("SELECT id FROM users WHERE username=?", (username,))
            if c.fetchone():
                flash("❌ Username already exists!", "danger")
                return render_template("register.html")
            
            # Check if email already exists
            c.execute("SELECT id FROM users WHERE email=?", (email,))
            if c.fetchone():
                flash("❌ Email already registered!", "danger")
                return render_template("register.html")
            
            # Create new user
            hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
            c.execute("INSERT INTO users (username, password_hash, email) VALUES (?, ?, ?)",
                     (username, hashed_password, email))
            conn.commit()
            
            flash("✅ Registration successful! Please login.", "success")
            return redirect(url_for('login'))
            
        except Exception as e:
            conn.rollback()
            flash(f"❌ Registration failed: {str(e)}", "danger")
        finally:
            conn.close()

    return render_template("register.html")


@app.route('/logout')
def logout():
    session.pop('user', None)
    flash("👋 Logged out successfully!", "info")
    return redirect(url_for('login'))


# ---------- DASHBOARD ----------
@app.route('/')
def index():
    if 'user' not in session:
        return redirect(url_for('login'))

    # Execute due anacron jobs (also run periodically by background thread)
    try:
        execute_anacron_jobs()
    except Exception as e:
        print(f"⚠️ Error executing anacron jobs on index load: {e}")

    jobs = get_anacron_jobs()
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("SELECT COUNT(*) FROM jobs")
        total_jobs = c.fetchone()[0]
    except Exception:
        total_jobs = 0
    conn.close()

    success_count, failure_count = get_job_status_counts()
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            logs = f.readlines()[-50:]

    scripts = []
    if os.path.exists(SCRIPT_DIR):
        scripts = [f for f in os.listdir(SCRIPT_DIR) if f.endswith(".py")]

    return render_template("index.html",
                           jobs=jobs,
                           logs=logs,
                           total_jobs=total_jobs,
                           user=session['user'],
                           success_count=success_count,
                           failure_count=failure_count,
                           scripts=scripts,
                           EMAIL_CONFIG=EMAIL_CONFIG,
                           base_dir=BASE_DIR)


# ---------- ADD JOB ----------
@app.route('/add', methods=['GET', 'POST'])
def add_job():
    if 'user' not in session:
        return redirect(url_for('login'))

    scripts = []
    if os.path.exists(SCRIPT_DIR):
        scripts = [f for f in os.listdir(SCRIPT_DIR) if f.endswith(".py")]

    if request.method == 'POST':
        schedule = request.form['schedule'].strip()
        command = request.form['command'].strip()
        email_notifications = 'email_notifications' in request.form

        # Validate schedule format
        schedule_parts = schedule.split()
        if len(schedule_parts) != 5:
            flash("❌ Invalid cron schedule format. Must have 5 parts: minute hour day month weekday", "danger")
            return render_template("add_job.html", scripts=scripts)

        # Validate script exists for Python scripts
        if command.endswith('.py') and not validate_script_exists(command):
            flash(f"❌ Script '{command}' not found in scripts directory!", "danger")
            return render_template("add_job.html", scripts=scripts)

        # Save to database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO jobs (schedule, command, created_at, email_notifications) VALUES (?, ?, ?, ?)",
                      (schedule, command, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(email_notifications)))
            job_id = c.lastrowid
            conn.commit()
        except Exception as e:
            conn.rollback()
            flash(f"❌ Failed to add job to DB: {e}", "danger")
            conn.close()
            return render_template("add_job.html", scripts=scripts)

        # Get user email for notification
        try:
            c.execute("SELECT username, email FROM users WHERE username=?", (session['user'],))
            user_record = c.fetchone()
        except Exception as e:
            user_record = None
            print(f"⚠️ Error fetching user: {e}")
        conn.close()

        # Add to anacron
        anacron_job = add_anacron_job(job_id, schedule, command, bool(email_notifications))

        # Send email notification
        if user_record and email_notifications:
            username, email = user_record
            job_details = {
                'id': job_id,
                'schedule': schedule,
                'command': command,
                'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            send_job_added_email(username, email, job_details)

        flash("✅ Cron job added successfully! You'll receive email notifications.", "success")
        return redirect(url_for('index'))

    return render_template("add_job.html", scripts=scripts)


# ---------- DELETE JOB ----------
@app.route('/delete/<int:job_id>')
def delete_job(job_id):
    if 'user' not in session:
        return redirect(url_for('login'))

    # First, get job details before deleting (for email notification)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT schedule, command FROM jobs WHERE id=?", (job_id,))
    job_details = c.fetchone()
    
    # Remove from database
    c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    conn.commit()
    
    # Get user email for notification
    c.execute("SELECT username, email FROM users WHERE username=?", (session['user'],))
    user_record = c.fetchone()
    conn.close()

    # Remove from anacron
    remove_anacron_job(job_id)
    
    # Log the deletion
    log_message = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Job #{job_id} DELETED - Schedule: {job_details[0] if job_details else 'Unknown'}, Command: {job_details[1] if job_details else 'Unknown'}\n"
    log_to_file(log_message)
    
    # Send email notification for job deletion
    if user_record and user_record[1]:
        username, email = user_record
        job_info = {
            'id': job_id,
            'schedule': job_details[0] if job_details else 'Unknown',
            'command': job_details[1] if job_details else 'Unknown',
            'deleted_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        send_job_deleted_email(username, email, job_info)
    
    flash("🗑️ Job deleted successfully! You'll receive a confirmation email.", "warning")
    return redirect(url_for('index'))


# ---------- CLEAR LOGS ----------
@app.route('/clear_logs')
def clear_logs():
    if 'user' not in session:
        return redirect(url_for('login'))
    try:
        open(LOG_FILE, "w").close()
        flash("🧹 Logs cleared successfully!", "info")
    except Exception as e:
        flash(f"❌ Failed to clear logs: {e}", "danger")
    return redirect(url_for('index'))


# ---------- STREAM LOGS ----------
@app.route('/stream_logs')
def stream_logs():
    def generate():
        if not os.path.exists(LOG_FILE):
            open(LOG_FILE, 'w').close()

        with open(LOG_FILE, "r") as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    yield f"data:{line.strip()}\n\n"
                else:
                    time.sleep(1)
    return Response(generate(), mimetype='text/event-stream')


# ---------- RUN SCRIPT ----------
@app.route('/run_script', methods=['POST'])
def run_script():
    if 'user' not in session:
        return redirect(url_for('login'))

    script_name = request.form.get('script_name')
    if not script_name:
        return jsonify({"error": "Script name required"}), 400

    script_path = os.path.join(SCRIPT_DIR, script_name)

    if not os.path.exists(script_path):
        return jsonify({"error": f"Script '{script_name}' not found"}), 404

    def generate_output():
        try:
            process = subprocess.Popen(
                ['python3', script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=SCRIPT_DIR
            )
            for line in process.stdout:
                yield f"data:{line.strip()}\n\n"
            process.wait()
            yield f"data:✅ Script '{script_name}' finished with exit code {process.returncode}\n\n"
        except Exception as e:
            yield f"data:❌ Error running script: {str(e)}\n\n"

    return Response(generate_output(), mimetype='text/event-stream')


# ---------- TEST ANACRON ----------
@app.route('/test_anacron')
def test_anacron():
    """Test anacron functionality"""
    if 'user' not in session:
        return redirect(url_for('login'))

    try:
        # Force execution of all due jobs
        execute_anacron_jobs()
        flash("🔧 Anacron jobs executed successfully! Check logs for details.", "success")
    except Exception as e:
        flash(f"❌ Error testing anacron: {e}", "danger")

    return redirect(url_for('index'))


# ---------- CREATE SAMPLE SCRIPT ----------
@app.route('/create_sample_script')
def create_sample_script():
    """Create a sample Python script for testing"""
    if 'user' not in session:
        return redirect(url_for('login'))

    sample_script = """#!/usr/bin/env python3
import time
import sys
import datetime
import os

def main():
    print(f"🚀 Starting test script at {datetime.datetime.now()}")
    print(f"📁 Current directory: {os.getcwd()}")
    print(f"🐍 Python version: {sys.version}")
    print(f"📝 Script location: {__file__}")

    for i in range(1, 6):
        print(f"📦 Processing task: Step {i}/5")
        time.sleep(1)

    print("✅ Script completed successfully!")
    print(f"🏁 Finished at {datetime.datetime.now()}")

if __name__ == "__main__":
    main()
"""

    script_path = os.path.join(SCRIPT_DIR, "test_script.py")
    try:
        with open(script_path, "w") as f:
            f.write(sample_script)
        os.chmod(script_path, 0o755)
        flash("📝 Sample script 'test_script.py' created successfully!", "success")
    except Exception as e:
        flash(f"❌ Failed to create sample script: {e}", "danger")

    return redirect(url_for('index'))


# ---------- JOB HISTORY ----------
@app.route('/job_history')
def job_history():
    """Show job execution history"""
    if 'user' not in session:
        return redirect(url_for('login'))

    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r") as f:
            logs = f.readlines()[-100:]  # Last 100 lines

    return render_template("job_history.html", logs=logs)


# ---------- BACKGROUND ANACRON RUNNER ----------
def background_anacron_runner(poll_interval_seconds=60):
    """Background thread that periodically runs execute_anacron_jobs"""
    while True:
        try:
            execute_anacron_jobs()
        except Exception as e:
            print(f"⚠️ Background anacron runner error: {e}")
        time.sleep(poll_interval_seconds)


if __name__ == '__main__':
    # Create necessary directories
    os.makedirs(SCRIPT_DIR, exist_ok=True)
    init_anacron()

    # Create log file if it doesn't exist
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, 'w').close()

    # Start background anacron thread (daemon so it exits with the process)
    anacron_thread = threading.Thread(target=background_anacron_runner, args=(60,), daemon=True)
    anacron_thread.start()

    print(f"🚀 Starting Anacron Dashboard...")
    print(f"📁 Script Directory: {SCRIPT_DIR}")
    print(f"📁 Log File: {LOG_FILE}")
    print(f"📁 Anacron Jobs: {ANACRON_JOBS_FILE}")
    print(f"📧 Email configured for: {EMAIL_CONFIG['sender_email']}")
    print(f"🌐 Web Interface: http://localhost:5000")

    app.run(debug=True, host='0.0.0.0', port=5000)
