# Railway Deployment Guide - RFQ Portal

## 🚀 Deploy to Railway with PostgreSQL

This guide will help you deploy your RFQ Portal to Railway with a PostgreSQL database.

---

## 📋 Prerequisites

- Railway account (free tier available: https://railway.app)
- GitHub account (to push your code)
- Your RFQ Portal code ready

---

## 🎯 Step-by-Step Deployment

### Step 1: Push Code to GitHub

1. **Create a new GitHub repository:**
   - Go to https://github.com/new
   - Name: `rfq-portal` (or your choice)
   - Make it Private (recommended)
   - Don't initialize with README

2. **Push your code:**
   ```bash
   cd C:\Projects\rfq_complete_package
   
   # Initialize git (if not already done)
   git init
   git add .
   git commit -m "Initial commit - RFQ Portal"
   
   # Add GitHub remote (replace with your repo URL)
   git remote add origin https://github.com/YOUR_USERNAME/rfq-portal.git
   git branch -M main
   git push -u origin main
   ```

---

### Step 2: Create Railway Project

1. **Go to Railway:**
   - Visit: https://railway.app
   - Sign up/Login with GitHub

2. **Create New Project:**
   - Click "New Project"
   - Select "Deploy from GitHub repo"
   - Choose your `rfq-portal` repository
   - Click "Deploy Now"

---

### Step 3: Add PostgreSQL Database

1. **In your Railway project:**
   - Click "New" → "Database" → "PostgreSQL"
   - Railway will automatically create a PostgreSQL database
   - Wait for it to provision (1-2 minutes)

2. **Link database to your app:**
   - Railway automatically sets DATABASE_URL
   - No manual configuration needed!

---

### Step 4: Set Environment Variables

1. **Click on your app service** (not the database)

2. **Go to "Variables" tab**

3. **Add these variables:**

   ```
   SECRET_KEY=<generate-with-command-below>
   FLASK_ENV=production
   FLASK_DEBUG=0
   PORT=5000
   BASE_URL=https://your-app-name.up.railway.app
   ADMIN_EMAIL=your-email@company.com
   RESEND_API_KEY=<your-resend-api-key>
   ```

4. **Generate SECRET_KEY:**
   - Open terminal:
   ```bash
   python -c "import secrets; print(secrets.token_hex(32))"
   ```
   - Copy output and paste as SECRET_KEY value

5. **Important:** 
   - `BASE_URL` will be your Railway app URL
   - After first deployment, Railway will give you the URL
   - Update BASE_URL with that URL

---

### Step 5: Configure Database Connection

Railway automatically provides `DATABASE_URL` but we need to ensure it's used correctly.

**Your app is already configured!** The `config.py` file reads `DATABASE_URL` from environment.

Just verify in Railway Variables tab that you see:
- `DATABASE_URL` (automatically set by Railway when you added PostgreSQL)

---

### Step 6: Deploy!

1. **Trigger deployment:**
   - Railway auto-deploys on every git push
   - Or click "Deploy" button in Railway dashboard

2. **Watch the build logs:**
   - Click "Deployments" tab
   - Watch the build process
   - Wait for "Success" status (3-5 minutes)

3. **Get your URL:**
   - Click "Settings" tab
   - Under "Domains", you'll see your Railway URL
   - Format: `https://rfq-portal-production-xxxx.up.railway.app`

---

### Step 7: Initialize Database

**On first deployment, you need to create tables:**

Railway doesn't have a terminal by default, so we'll add a database initialization endpoint.

**Option A: Add init endpoint (Recommended)**

Add this to your `app.py`:

```python
# Add at the end of app.py, before if __name__ == '__main__':

@app.route('/init-db-secret-endpoint')
def init_database():
    """One-time database initialization endpoint"""
    from models import User
    from utils.helpers import hash_pw
    import secrets as _s
    
    # Check if already initialized
    if User.query.first():
        return "Database already initialized", 400
    
    # Create tables
    db.create_all()
    
    # Create admin
    tmp_pw = _s.token_urlsafe(12)
    admin = User(
        name='Admin',
        email=os.environ.get('ADMIN_EMAIL', 'admin@rfq.com'),
        password=hash_pw(tmp_pw),
        role='Admin',
        is_active=1,
    )
    db.session.add(admin)
    db.session.commit()
    
    return f"Database initialized! Admin email: {admin.email}, Password: {tmp_pw}"
```

Then visit: `https://your-app.up.railway.app/init-db-secret-endpoint`

**Option B: Use Railway's CLI**

```bash
# Install Railway CLI
npm install -g @railway/cli

# Login
railway login

# Link to your project
railway link

# Run initialization
railway run python -c "from app import app, init_db; init_db(app)"
```

---

### Step 8: Configure Email Settings

1. **Access your deployed app:**
   - Go to: `https://your-app.up.railway.app`

2. **Login with admin credentials** (from init step)

3. **Go to Settings → Email Settings**

4. **Configure Gmail:**
   - Gmail Address: your-email@gmail.com
   - Gmail Password: (App Password - not regular password!)
   - Sender Name: Your Company
   - Base URL: https://your-app.up.railway.app

---

## 🎯 Final Configuration

### Update BASE_URL

After getting your Railway URL:

1. Go to Railway dashboard → Your app → Variables
2. Update `BASE_URL` to your actual Railway URL
3. Click "Redeploy" if needed

---

## 📊 Database Management

### View Database

In Railway:
- Click PostgreSQL service
- Click "Data" tab
- You can view tables and data

### Backup Database

Railway Pro plan includes automatic backups.

**Manual backup:**
```bash
# In Railway dashboard → PostgreSQL → Connect
# Copy connection string and run locally:
pg_dump <CONNECTION_STRING> > backup.sql
```

### Migrate Existing Data

If you have data in SQLite:

1. **Export from SQLite:**
   ```python
   # Run locally
   python export_data.py  # (create this script to export to JSON/CSV)
   ```

2. **Import to PostgreSQL:**
   ```python
   # Run on Railway
   python import_data.py  # (create this script to import)
   ```

---

## 🔧 Troubleshooting

### Build Fails

**Check logs in Railway → Deployments → Click deployment → View logs**

Common issues:
- Missing dependencies → Check requirements.txt
- Python version → Railway uses Python 3.11 by default

### Database Connection Error

Check:
- DATABASE_URL is set correctly
- PostgreSQL service is running
- Connection string format: `postgresql://user:pass@host:port/db`

### App Crashes

Check:
- Environment variables are set
- SECRET_KEY is present
- FLASK_ENV=production
- Logs in Railway dashboard

### Emails Not Sending

Check:
- Email settings configured in app
- Gmail App Password used (not regular password)
- BASE_URL is correct Railway URL

---

## 📈 Monitoring

### Railway Dashboard

Monitor:
- CPU usage
- Memory usage
- Request logs
- Deployment history

### Application Logs

View in Railway:
- Click your service
- "Deployments" tab
- Click "View Logs"

---

## 💰 Pricing

**Railway Free Tier:**
- $5 free credit per month
- Enough for small-medium usage
- ~500 hours of runtime

**Railway Pro ($20/month):**
- More resources
- Priority support
- Automatic backups
- Custom domains

---

## 🔐 Security Checklist

Before going live:

- [ ] FLASK_ENV=production
- [ ] FLASK_DEBUG=0
- [ ] Strong SECRET_KEY generated
- [ ] Admin password changed from default
- [ ] BASE_URL set to HTTPS Railway URL
- [ ] Gmail App Password used
- [ ] PostgreSQL (not SQLite)
- [ ] Sensitive data not in git repo
- [ ] .env file not committed (check .gitignore)

---

## 🚀 Post-Deployment Steps

1. **Test the application:**
   - Create test RFQ
   - Send to test vendor
   - Submit quote
   - Test Round 2
   - Close RFQ
   - Verify all emails working

2. **Change admin password:**
   - Login → Profile → Change Password

3. **Create users:**
   - Admin → Users → Add User

4. **Import vendors:**
   - Vendors → Import from Excel

5. **Monitor for a few days:**
   - Check Railway logs
   - Monitor email delivery
   - Check database growth

---

## 📝 Regular Maintenance

### Weekly
- Check Railway usage/credits
- Review email logs
- Check for failed deployments

### Monthly
- Review and close old RFQs
- Check database size
- Review vendor data
- Backup database (if on Pro plan)

---

## 🆘 Getting Help

**Railway Issues:**
- Railway Discord: https://discord.gg/railway
- Railway Docs: https://docs.railway.app

**Application Issues:**
- Check Railway logs
- Review error messages
- Check environment variables

---

## ✅ Deployment Checklist

- [ ] Code pushed to GitHub
- [ ] Railway project created
- [ ] PostgreSQL database added
- [ ] Environment variables set
- [ ] SECRET_KEY generated
- [ ] Database initialized
- [ ] Admin account created
- [ ] App accessible via Railway URL
- [ ] Email settings configured
- [ ] Test RFQ completed successfully
- [ ] All emails sending properly
- [ ] BASE_URL updated to Railway URL

---

## 🎉 You're Live!

Your RFQ Portal is now running on Railway with:
- ✅ PostgreSQL database (persistent)
- ✅ HTTPS enabled
- ✅ Auto-deployment on git push
- ✅ Professional hosting
- ✅ All features working

**Your app URL:** `https://your-app.up.railway.app`

Share this URL with your team and vendors!

---

## 📞 Quick Commands Reference

```bash
# Push updates
git add .
git commit -m "Update description"
git push origin main
# Railway auto-deploys!

# View logs (if Railway CLI installed)
railway logs

# Link to project
railway link

# Run commands on Railway
railway run <command>
```

---

**Good luck with your deployment! 🚀**
