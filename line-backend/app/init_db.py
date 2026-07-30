"""
執行這支腳本，會依照 models.py 的定義，在資料庫裡真正建立資料表。
只需要在資料表結構改變時執行，不用每次啟動伺服器都跑。

執行方式：python -m app.init_db
"""
from app.db import Base, engine
from app import models  # noqa: F401  # 必須import，讓SQLAlchemy知道有哪些表要建

if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    print("資料表建立完成！")