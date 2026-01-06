from sqlalchemy import create_engine, Column, Integer, String, ForeignKey, Boolean, BigInteger
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from sqlalchemy.pool import QueuePool
import os

SQLALCHEMY_DATABASE_URL = "sqlite:///./app.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    connect_args={"check_same_thread": False},   # 允许跨线程使用连接
    poolclass=QueuePool,                         # 支持并发连接池
    pool_size=500,                               # 基础连接池大小
    max_overflow=20000,                          # 最大溢出连接数
    pool_timeout=60,                             # 等待连接的超时时间 (秒)
    pool_recycle=3600                            # 连接回收时间 (秒，防止连接卡死)
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_admin = Column(Boolean, default=False)

    settings = relationship("UserSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")

class UserSettings(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True)

    nav_url = Column(String, default="") 
    nav_username = Column(String, default="")
    nav_password = Column(String, default="")

    tidal_access_token = Column(String, nullable=True)
    tidal_refresh_token = Column(String, nullable=True)
    tidal_expiry_time = Column(BigInteger, default=0)
    tidal_session_id = Column(String, nullable=True)
    tidal_country_code = Column(String, default="US")
    user = relationship("User", back_populates="settings")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    Base.metadata.create_all(bind=engine)