"""
資料庫連線設定
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

engine = create_engine(settings.DATABASE_URL, echo=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """給API用的資料庫連線，用完自動關閉"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()