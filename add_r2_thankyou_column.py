"""
Migration script to add thankyou_r2_sent column to RFQ table
Run this once to update your existing database
"""
from app import app, db
from sqlalchemy import text

def migrate():
    with app.app_context():
        try:
            # Check if column already exists
            result = db.session.execute(text("PRAGMA table_info(rfqs)"))
            columns = [row[1] for row in result]
            
            if 'thankyou_r2_sent' in columns:
                print('✅ Column thankyou_r2_sent already exists')
                return
            
            # Add the new column
            db.session.execute(text(
                "ALTER TABLE rfqs ADD COLUMN thankyou_r2_sent BOOLEAN DEFAULT 0"
            ))
            db.session.commit()
            print('✅ Successfully added thankyou_r2_sent column to rfqs table')
            
        except Exception as e:
            print(f'❌ Error during migration: {e}')
            db.session.rollback()

if __name__ == '__main__':
    migrate()
