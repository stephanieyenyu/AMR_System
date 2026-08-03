from app.db import engine
from sqlalchemy import text

with engine.begin() as conn:
    conn.execute(text("ALTER TABLE packages ADD COLUMN IF NOT EXISTS schedule_reminder_sent_at TIMESTAMP"))
print("欄位新增完成")
