# RFQ Portal - Complete Application Package

## 📦 What Is This?

This is the **COMPLETE RFQ Portal application** with all the latest features already implemented.

This is a **ready-to-run** Flask application for managing Request for Quotations (RFQs), vendor bidding, and award management.

---

## ✨ Features Included

### Core RFQ Management
- ✅ Create and manage RFQs with multiple lanes
- ✅ Send RFQ invitations to vendors via email
- ✅ Vendor quote submission (Round 1 and Round 2)
- ✅ Comparative analysis and ranking
- ✅ Winner selection and award notifications

### NEW - Advanced Features (Already Implemented!)
- ✅ **Round 2 Thank You Emails** - Automatic emails after Round 2 submission
- ✅ **Show Only Round 2 Bids** - Clean view filtering Round 1/2 quotes
- ✅ **Reason for Selection** - Document vendor selection rationale
- ✅ **Regret Emails** - Professional emails to non-winning Round 2 vendors

### Communication Features
- ✅ Email notifications at every stage
- ✅ Thank you emails after quote submission
- ✅ Award confirmation emails to winners
- ✅ Regret emails to non-winners (Round 2 only)
- ✅ Email logging and tracking

### Analytics & Reporting
- ✅ Vendor performance scorecards
- ✅ Savings analysis
- ✅ Rate trends and comparisons
- ✅ Lane-wise performance tracking

---

## 🚀 Quick Start (First Time Setup)

### Prerequisites
- Python 3.8 or higher
- Windows/Linux/Mac
- Gmail account for sending emails

### Installation Steps

#### 1. Extract the Package
```cmd
cd C:\Projects
# Extract rfq_complete_package.zip here
```

#### 2. Create Virtual Environment (Recommended)
```cmd
cd C:\Projects\rfq_complete_package
python -m venv venv
venv\Scripts\activate
```

#### 3. Install Dependencies
```cmd
pip install -r requirements.txt
```

#### 4. Configure Environment Variables
```cmd
# Copy the example file
copy .env.example .env

# Edit .env file and set:
# - SECRET_KEY (generate with: python -c "import secrets; print(secrets.token_hex(32))")
# - ADMIN_EMAIL (your admin email)
# - Other settings as needed
```

#### 5. Initialize Database
```cmd
python app.py
```

This will:
- Create the database
- Run migrations
- Create default admin account
- Display temporary admin password in console

#### 6. Access the Application
Open browser: **http://localhost:5000**

Login with:
- Email: (from .env ADMIN_EMAIL)
- Password: (shown in console on first run)

**⚠️ Change the password immediately after first login!**

---

## 📂 Project Structure

```
rfq_complete_package/
├── app.py                      # Main application entry point
├── config.py                   # Configuration settings
├── models.py                   # Database models
├── extensions.py               # Flask extensions
├── requirements.txt            # Python dependencies
├── .env.example                # Environment variables template
├── .gitignore                  # Git ignore rules
├── add_r2_thankyou_column.py  # Database migration script
│
├── blueprints/                 # Application modules
│   ├── auth/                   # Authentication & user management
│   ├── rfq/                    # RFQ creation & management
│   ├── results/                # Quote comparison & winner selection
│   ├── vendors/                # Vendor management
│   ├── admin/                  # Admin tools & settings
│   └── reports/                # Analytics & reporting
│
├── templates/                  # HTML templates
│   ├── base.html              # Base layout
│   ├── login.html             # Login page
│   ├── dashboard.html         # Main dashboard
│   ├── create_rfq.html        # RFQ creation form
│   ├── results.html           # Results & winner selection
│   └── ... (30+ templates)
│
├── static/                     # Static files
│   ├── css/                   # Stylesheets
│   └── shadowfax_logo.jpg     # Company logo
│
├── utils/                      # Helper functions
│   ├── helpers.py             # General utilities
│   └── email.py               # Email sending functions
│
├── uploads/                    # User uploaded files
│   └── schedules/             # Excel schedules
│
├── run.sh                      # Linux startup script
├── run.bat                     # Windows startup script
├── gunicorn.conf.py           # Production server config
└── nginx.conf                 # Nginx configuration example
```

---

## 🎯 How to Use

### 1. User Management
- Admin creates user accounts
- Roles: Admin, User
- Users can manage RFQs they create

### 2. Create RFQ
1. Dashboard → Create RFQ
2. Fill in details (subject, zone, deadline)
3. Add lanes (routes) with details
4. Add vendors with email addresses
5. Submit for approval

### 3. Send RFQ to Vendors
1. After approval → Send to Vendors
2. Vendors receive email with unique quote link
3. Vendors submit quotes online (no login needed)
4. System sends thank you emails automatically

### 4. Round 2 Bidding (Optional)
1. After Round 1 quotes received
2. Set target rates for each lane
3. Send Round 2 emails to selected vendors
4. Vendors submit revised quotes
5. System shows only Round 2 quotes (clean view)
6. Automatic thank you emails sent

### 5. Select Winners
1. Results page → Select Winner & Close
2. Choose winner for each lane
3. Add reason for selection (optional but recommended)
4. Close RFQ
5. System automatically sends:
   - Award emails to winners
   - Regret emails to non-winning Round 2 vendors

---

## ⚙️ Configuration

### Email Settings
Configure in: **Settings → Email Settings**

Required:
- Gmail address
- Gmail App Password (not regular password!)
- Sender name
- Base URL (for email links)

**How to get Gmail App Password:**
1. Go to Google Account Settings
2. Security → 2-Step Verification
3. App Passwords
4. Generate password for "Mail"
5. Use this password (not your regular Gmail password)

### User Preferences
Each user can customize:
- Dashboard preferences
- Notification settings
- Display preferences

---

## 🔧 Database

### Development (SQLite - Default)
- Database file: `rfq_portal.db`
- Automatically created on first run
- Suitable for development and small teams

### Production (PostgreSQL - Recommended)
Edit `.env`:
```
DATABASE_URL=postgresql://user:password@localhost:5432/rfq_db
```

### Backup Database
```cmd
# SQLite
copy rfq_portal.db rfq_portal_backup.db

# PostgreSQL
pg_dump rfq_db > rfq_backup.sql
```

---

## 📧 Email Workflow

### Complete Email Journey

**Round 1:**
1. Vendor receives RFQ invitation email
2. Vendor submits quote
3. Vendor receives "Thank You" email

**Round 2 (if applicable):**
1. Vendor receives Round 2 invitation with target rates
2. Vendor submits revised quote
3. Vendor receives "Round 2 Thank You" email

**Closing:**
1. Admin selects winners
2. **Winners** receive "Congratulations!" email
3. **Non-winning Round 2 vendors** receive "Thank You + Regret" email
4. Round 1 only vendors receive no email (they weren't in final round)

---

## 🐛 Troubleshooting

### App Won't Start
```cmd
# Check Python version
python --version

# Reinstall dependencies
pip install -r requirements.txt --upgrade

# Check for errors in console
```

### Database Issues
```cmd
# Reset database (WARNING: Deletes all data!)
del rfq_portal.db
python app.py
```

### Email Not Sending
1. Check Settings → Email Settings
2. Verify Gmail App Password (not regular password)
3. Check console logs for errors
4. Test with a simple email first

### Port Already in Use
```cmd
# Change port in .env
PORT=5001

# Or kill process using port 5000
# Windows: netstat -ano | findstr :5000
# Linux: lsof -ti:5000 | xargs kill
```

---

## 🔐 Security

### Important Security Settings

**Production Deployment:**
1. Set `FLASK_ENV=production` in `.env`
2. Use strong SECRET_KEY
3. Use PostgreSQL (not SQLite)
4. Enable HTTPS
5. Use gunicorn + nginx
6. Set up firewall rules

**Password Security:**
- Passwords hashed with bcrypt
- Rate limiting on login (5 attempts per 5 minutes)
- Session management with secure cookies

**Email Security:**
- Email passwords encrypted in database
- Use Gmail App Passwords (not regular passwords)

---

## 🚀 Production Deployment

### Using Gunicorn (Linux)
```bash
gunicorn -c gunicorn.conf.py app:app
```

### Using Systemd Service (Linux)
```bash
# Copy service file
sudo cp rfq_portal.service /etc/systemd/system/

# Enable and start
sudo systemctl enable rfq_portal
sudo systemctl start rfq_portal
```

### Using Nginx (Reverse Proxy)
```bash
# Use provided nginx.conf as template
sudo cp nginx.conf /etc/nginx/sites-available/rfq_portal
sudo ln -s /etc/nginx/sites-available/rfq_portal /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx
```

### Windows Production
Use waitress:
```cmd
pip install waitress
waitress-serve --port=5000 app:app
```

---

## 📊 Default Data

### First Run Creates:
- Admin user (email from .env, temporary password shown)
- Database tables
- Required directories

### Sample Data (Optional)
To test the application:
1. Login as admin
2. Create a test vendor
3. Create a test RFQ
4. Use your own email as vendor email for testing

---

## 🆘 Support

### Common Questions

**Q: How do I reset admin password?**
```python
python -c "from app import app, db; from models import User; from utils.helpers import hash_pw; app.app_context().push(); admin = User.query.filter_by(role='Admin').first(); admin.password = hash_pw('newpassword'); db.session.commit(); print('Password reset to: newpassword')"
```

**Q: How do I add more users?**
- Login as admin → Users → Add User

**Q: Can vendors see other vendors' quotes?**
- No, vendors only see their own submission

**Q: How do I change the company logo?**
- Replace `static/shadowfax_logo.jpg` with your logo

---

## 📝 License & Credits

This is a production-ready RFQ management system with advanced vendor communication features.

All features implemented and tested:
- Round 1 & Round 2 bidding
- Automatic email notifications
- Winner selection with reasons
- Professional regret emails
- Complete vendor lifecycle management

---

## 🎉 You're Ready!

Your RFQ Portal is ready to use with all advanced features enabled!

**Next Steps:**
1. Complete the setup (steps above)
2. Configure email settings
3. Create your first RFQ
4. Invite vendors
5. Manage bids professionally

**Questions?** Check the troubleshooting section above.

**Good luck with your procurement process!** 🚀
