"""
資料表結構定義，對應《LINE模組_實作步驟.md》階段1規劃的兩張表
"""
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import Column, String, DateTime, Boolean, Integer
from sqlalchemy.dialects.postgresql import UUID

from app.db import Base


TAIPEI_TZ = ZoneInfo("Asia/Taipei")


def now_taipei() -> datetime:
    """
    回傳台灣當地時間（naive datetime，不帶tzinfo）。
    這幾張表的DateTime欄位都是不帶timezone的plain DateTime，
    所以這裡刻意strip掉tzinfo，直接存「數字看起來就是台灣時間」的值，
    存進去、讀出來都不用再另外做時區轉換。
    """
    return datetime.now(TAIPEI_TZ).replace(tzinfo=None)


class Package(Base):
    __tablename__ = "packages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    unit = Column(String(50), nullable=False)                  # 門牌
    line_user_id = Column(String(100), nullable=False)         # 收件人 LINE User ID
    status = Column(String(30), nullable=False, default="pending")
    task_type = Column(String(20), nullable=False, default="delivery")  # delivery（送貨，管理員建立）/ return（退貨，住戶主動申請）
    package_count = Column(Integer, nullable=False, default=1)  # 這個任務代表幾件實體包裹（1-4），決定要開幾個艙門
    # pending / pickup_now / delivering / arrived
    # / completed / returned_timeout / voided / rejected_at_door
    door_id = Column(String(10), nullable=True)                # 分配的艙門編號
    door_task_id = Column(UUID(as_uuid=True), nullable=True)    # 這個門「這一次」被使用的任務ID，同一個門不同次使用會是不同ID
                                                                  # 同一個door_task_id底下的所有包裹，狀態轉換（抵達/驗證/完成/拒收/逾時）全部綁在一起走
    creation_batch_id = Column(UUID(as_uuid=True), nullable=True)  # 建立包裹時quantity>1，同一批N筆包裹共用這個ID
                                                                     # 只發一次到貨通知，但住戶按取貨/預約/不收時要一次套用到整批
    door_assigned_at = Column(DateTime, nullable=True)          # 艙門分配（放置包裹開門）的時間，逾時判斷用
    stop_dispatched_at = Column(DateTime, nullable=True)        # 這一站真正呼叫/api/robot/dispatch派送出去的時間，防止並發重複派送同一站
    arrived_at = Column(DateTime, nullable=True)                # 機器人抵達時間，逾時判斷用
    returned_at = Column(DateTime, nullable=True)               # 機器人實際返回管理室的時間（拒收/逾時退回專用，門此時還沒開）
    return_door_opened_at = Column(DateTime, nullable=True)     # 管理員按「開門」，機器人真的開門讓管理員取出包裹的時間
    door_closed_at = Column(DateTime, nullable=True)            # 拒收後管理員取出包裹、按關門的時間
    acknowledged_at = Column(DateTime, nullable=True)            # 不收(voided)的包裹，管理員按「確定」已知悉的時間
    redispatched_at = Column(DateTime, nullable=True)            # 已重新派送的時間（例外處理頁按下「重新派貨」）
    redispatched_to = Column(UUID(as_uuid=True), nullable=True)  # 重新派送後，新建立的包裹ID
    pending_pickup_notified_at = Column(DateTime, nullable=True)  # 例外處理頁「通知住戶」的時間，只能通知一次
    scheduled_pickup_at = Column(DateTime, nullable=True)  # 住戶預約取貨的時間（整點），到這個時間前不能放置/派送
    created_at = Column(DateTime, default=now_taipei)
    updated_at = Column(DateTime, default=now_taipei, onupdate=now_taipei)
    case_closed_at = Column(DateTime, nullable=True)
    return_retrieved_at = Column(DateTime, nullable=True)  # 退貨任務：管理員確認已從艙門取出退貨件的時間


class LineBinding(Base):
    __tablename__ = "line_binding"

    line_user_id = Column(String(100), primary_key=True)
    unit = Column(String(50), nullable=False)
    name = Column(String(100), nullable=False)
    bound_at = Column(DateTime, default=now_taipei)
    status = Column(String(20), default="active")               # active / inactive
    solo_notify = Column(Boolean, nullable=False, default=True)   # 是否限本人接收包裹通知

class PackageRecipient(Base):
    __tablename__ = "package_recipients"

    package_id = Column(UUID(as_uuid=True), primary_key=True)
    unit = Column(String(50), nullable=False)                  # 門牌
    line_user_id = Column(String(100), primary_key=True)


class TaskLog(Base):
    """
    任務事件紀錄，給每日報表用。
    在此之前，系統裡的事件只用print()印到console，服務重啟或console關掉就消失了，
    沒辦法回頭查歷史。這張表把關鍵事件（建立包裹、分配艙門、派工、抵達、取貨、逾時退回等）
    真正存進資料庫，才能查「某一天發生過什麼事」。
    """
    __tablename__ = "task_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    package_id = Column(UUID(as_uuid=True), nullable=True)     # 有些事件（例如機器人連線失敗）不一定對應到特定包裹
    event_type = Column(String(50), nullable=False)
    # created / rejected / rejected_at_door / door_assigned / door_assign_failed / dispatched / dispatch_failed
    # / arrived / pickup_opened / pickup_open_failed / completed / complete_failed
    # / returned_timeout / returned / cancel_task_failed / returned_and_opened / return_failed
    # / door_closed / close_door_failed / notify_failed / voided_acknowledged / redispatched / case_closed
    # / line_binding_deleted / line_binding_updated / return_door_opened / return_door_open_failed / pending_pickup_notified
    # / pickup_requested / trip_completed / user_unfollowed
    # / robot_recharge_requested / robot_recharge_failed
    # / robot_recall_requested / robot_recall_failed
    # / assign_timeout / assign_timeout_failed / return_timeout / return_timeout_failed
    # / poll_returned_failed
    # / force_resolved
    # / pickup_scheduled
    # / multi_package_assigned
    # / task_recalled / package_deleted / door_released_manually
    # / return_requested / return_retrieved / return_cancelled
    level = Column(String(10), nullable=False, default="info")  # info / warning / error
    detail = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=now_taipei)