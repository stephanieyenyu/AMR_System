"""
LINE 後端 - 主程式入口
"""
from datetime import datetime, timedelta
from typing import Optional, List
import secrets
import uuid
import requests

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import PlainTextResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError
from pydantic import BaseModel, Field

from linebot.v3 import WebhookParser
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, PostbackEvent, FollowEvent, UnfollowEvent, TextMessageContent

from app.config import settings
from app.db import get_db, SessionLocal
from app.models import LineBinding, Package, PackageRecipient, TaskLog, now_taipei
from app.line_verify import verify_liff_id_token
from app.line_messaging import (
    reply_welcome_with_binding_instructions,
    reply_text,
    push_arrival_notification,
    push_status_update,
    push_arrived_notification,
    reply_my_packages_text,
)
from apscheduler.schedulers.background import BackgroundScheduler

app = FastAPI(title="AMR 配送系統 - LINE 後端")

parser = WebhookParser(settings.LINE_CHANNEL_SECRET)


# ========== 管理員登入驗證（HTTP Basic Auth） ==========
# 只保護「只有管理員會用瀏覽器操作」的路由：/admin開頭的頁面與API、
# 以及建立包裹/放置包裹/銷案/重新派貨這幾支只有Dashboard會呼叫的動作。
# 刻意不保護：/webhook（LINE自己的簽章驗證）、/liff/scan與掃碼/取貨完成
# 這幾支（住戶端，住戶沒有帳密）、/packages/{id}/arrived與/returned
# （機器人模組會呼叫，機器人不會帶帳密，加了這層會直接打斷機器人callback）。
security = HTTPBasic()


def require_admin_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """
    FastAPI依賴：套用在需要登入的路由上（在@app.get/@app.post的dependencies=[...]裡加這支）。
    用secrets.compare_digest比對，避免用一般的==比字串時，透過回應時間差被推測出密碼內容。
    帳密目前是整個團隊共用一組（不是每人各自的帳號），符合現階段規模，
    之後如果要分權限（例如不同管理員看不到彼此的操作紀錄），才需要再往上升級。
    """
    correct_username = secrets.compare_digest(credentials.username, settings.ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, settings.ADMIN_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="帳號或密碼錯誤",
            headers={"WWW-Authenticate": "Basic realm=\"Aurobox Admin\""},
        )
    return credentials.username


@app.get("/")
async def health_check():
    return {"status": "ok", "message": "LINE backend is running", "env": settings.APP_ENV}


USAGE_INSTRUCTIONS_TEXT = (
    "使用說明\n"
    "\n"
    "【綁定門牌】僅需綁定一次\n"
    "輸入「門牌 姓名」，例如：5F-1 王小明\n"
    "\n"
    "【收件】\n"
    "1. 包裹送達會收到通知，選「取貨」「預約取貨」或「不收」\n"
    "2. 機器人抵達會再通知，掃描機器人螢幕的QR Code開門\n"
    "3. 取出包裹後按「取貨完成」\n"
    "（機器人抵達後如不想取，可按「拒收」）\n"
    "\n"
    "【退貨】\n"
    "1. 點選圖文選單「退貨」，選擇件數送出\n"
    "2. 機器人抵達會收到通知，掃描QR Code開門\n"
    "3. 放入物品後按「放貨完成」\n"
    "\n"
    "【查詢包裹】\n"
    "點選圖文選單「我的包裹」查看目前狀態\n"
    "\n"
    "【忘記按完成】\n"
    "掃碼開門後若不小心關掉頁面，點選圖文選單「關門」即可完成關門\n"
    "\n"
    "【通知設定】同門牌多人適用\n"
    "輸入「開啟限本人通知」：只有自己收到到貨通知\n"
    "輸入「關閉限本人通知」：同門牌所有人都收到\n"
    "\n"
    "如有問題，請聯繫社區管理員"
)


@app.post("/webhook")
async def line_webhook(request: Request):
    signature = request.headers.get("X-Line-Signature")
    if signature is None:
        raise HTTPException(status_code=400, detail="Missing X-Line-Signature header")

    body = await request.body()

    try:
        events = parser.parse(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=401, detail="Invalid signature")

    for event in events:
        try:
            if isinstance(event, FollowEvent):
                print(f"[follow] 新用戶加入好友, user_id={event.source.user_id}")
                reply_welcome_with_binding_instructions(event.reply_token)

            elif isinstance(event, UnfollowEvent):
                print(f"[unfollow] 用戶封鎖/退出, user_id={event.source.user_id}")
                db = SessionLocal()
                try:
                    binding = db.query(LineBinding).filter(
                        LineBinding.line_user_id == event.source.user_id
                    ).first()
                    if binding:
                        binding.status = "inactive"
                        db.commit()
                        log_event(
                            db, "user_unfollowed",
                            detail=f"unit={binding.unit} name={binding.name} 已設為inactive",
                        )
                    else:
                        # 查不到對應的綁定資料——常見於：這個user_id屬於舊的LINE channel，
                        # 換channel之後跟現在資料庫裡的line_user_id對不上，屬於預期內的情況，
                        # 但如果一直看到這個log卻預期應該要有綁定，就代表channel/資料對不起來
                        print(f"[unfollow] 查無對應綁定, user_id={event.source.user_id}")
                        log_event(
                            db, "user_unfollowed",
                            detail=f"查無對應的LineBinding, user_id={event.source.user_id}",
                            level="warning",
                        )
                finally:
                    db.close()

            elif isinstance(event, PostbackEvent):
                print(f"[postback] user_id={event.source.user_id}, data={event.postback.data}")
                handle_postback(event.postback.data, event.reply_token, event.source.user_id, event.postback.params)

            elif isinstance(event, MessageEvent):
                if isinstance(event.message, TextMessageContent):
                    text = event.message.text.strip()
                    if text == "我的包裹":
                        handle_my_packages_query(event.source.user_id, event.reply_token)
                    elif text == "關門":
                        handle_close_door_request(event.source.user_id, event.reply_token)
                    elif text == "開啟限本人通知":
                        handle_solo_notify_toggle(event.source.user_id, event.reply_token, True)
                    elif text == "關閉限本人通知":
                        handle_solo_notify_toggle(event.source.user_id, event.reply_token, False)
                    elif text == "使用說明":
                        reply_text(event.reply_token, USAGE_INSTRUCTIONS_TEXT)
                    else:
                        handle_text_binding(event.source.user_id, text, event.reply_token)
                else:
                    print(f"[message] user_id={event.source.user_id} (非文字訊息)")
        except Exception as e:
            # 這一批webhook可能包含多個事件，其中一個處理失敗不該讓後面的事件
            # 全部沒機會被處理到——沒有這層try/except的話，例外會一路往上竄出整個
            # for迴圈，導致這個event之後的所有事件都被跳過，而且這支route最後也不會
            # 回傳200給LINE（會變成500），LINE官方可能因此重送整批webhook，
            # 反而讓已經成功處理過的事件也有機會被重複觸發一次。
            # 這裡刻意用最外層的Exception接住、印出來就好，不特別區分例外種類——
            # 目的只是「保住這一批的其他事件」，不是要在這裡處理業務邏輯的錯誤。
            print(f"[webhook事件處理失敗] event={type(event).__name__}, error={e}")

    return PlainTextResponse("OK")


def handle_solo_notify_toggle(line_user_id: str, reply_token: str, enable: bool):
    db = SessionLocal()
    try:
        binding = db.query(LineBinding).filter(LineBinding.line_user_id == line_user_id).first()
        if not binding:
            reply_text(reply_token, "請先完成綁定（輸入：門牌 姓名）")
            return
        binding.solo_notify = enable
        db.commit()
        msg = "已開啟：包裹到貨只通知您本人" if enable else "已關閉：包裹到貨通知同門牌所有人"
        reply_text(reply_token, msg)
    finally:
        db.close()


def handle_text_binding(line_user_id: str, text: str, reply_token: str):
    """
    解析「門牌 姓名」格式的文字訊息。
    格式不對時，要先分辨這個人是不是已經綁定過——已綁定的人多半不是想重新綁定，
    只是打了看不懂的內容，這種情況改引導去使用說明，不要一律回「綁定格式不正確」
    誤導成好像他在嘗試綁定。
    """
    parts = text.split()
    if len(parts) == 2:
        unit, name = parts[0], parts[1]
        db = SessionLocal()
        try:
            existing = db.query(LineBinding).filter(LineBinding.line_user_id == line_user_id).first()
            if existing:
                existing.unit = unit
                existing.name = name
                existing.status = "active"
            else:
                existing = LineBinding(line_user_id=line_user_id, unit=unit, name=name)
                db.add(existing)
            db.commit()
            reply_text(reply_token, f"綁定成功！\n門牌：{unit}\n姓名：{name}")
        finally:
            db.close()
        return

    db = SessionLocal()
    try:
        already_bound = (
            db.query(LineBinding)
            .filter(LineBinding.line_user_id == line_user_id, LineBinding.status == "active")
            .first()
        )
    finally:
        db.close()

    if already_bound:
        reply_text(reply_token, "無法判斷您的需求，請點選「使用說明」或聯繫管理員")
    else:
        reply_text(reply_token, "格式不正確，請輸入：門牌 姓名\n例如：5F-1 王小明")


def handle_my_packages_query(line_user_id: str, reply_token: str):
    """
    「我的包裹」：純文字列出所有還沒真正結束的包裹狀態，不附任何按鈕。
    這裡刻意把退回（拒收/逾時未取）跟不收也納入查詢範圍——住戶需要知道
    自己有包裹被退回、還剩多久會作廢，不能因為狀態進了例外流程就從清單消失。
    真正要排除的只有：completed（已完成）、case_closed_at有值（管理員已經銷案）、
    redispatched_at有值（已經重新派送成一筆新包裹，這筆舊的不用再顯示）。
    要對包裹採取動作（取貨/預約取貨/不收），請至該筆包裹的到貨通知訊息
    點選對應按鈕，這裡純粹是查詢用途。
    """
    db = SessionLocal()
    try:
        packages = (
            db.query(Package)
            .filter(
                Package.line_user_id == line_user_id,
                Package.status != "completed",
                Package.case_closed_at.is_(None),
                Package.redispatched_at.is_(None),
            )
            .order_by(Package.created_at)
            .all()
        )
        if not packages:
            reply_text(reply_token, "目前無待取貨包裹")
        else:
            reply_my_packages_text(reply_token, packages)
    finally:
        db.close()


def handle_close_door_request(line_user_id: str, reply_token: str):
    """
    圖文選單「關門」：住戶已經掃碼開門、但在LIFF頁面按下「取貨完成」/「放貨完成」
    之前不小心關掉頁面，導致艙門一直開著、機器人卡在原地不會前往下一站或返航。

    這支提供LIFF之外的另一個入口，效果完全等同LIFF那顆按鈕（呼叫的就是同一支
    complete_pickup）——找出這個人名下目前狀態是arrived（門還開著）的任務，
    全部完成掉，不需要重新掃碼、也不需要記得door_task_id是什麼。

    找法：透過PackageRecipient查這個人是收件人的所有包裹，篩出還在arrived的，
    依door_task_id分組（同一組只需要對其中一筆呼叫complete_pickup，
    它內部會自己處理整組）。
    """
    db = SessionLocal()
    try:
        package_ids = [
            row.package_id for row in
            db.query(PackageRecipient.package_id)
            .filter(PackageRecipient.line_user_id == line_user_id)
            .all()
        ]
        arrived_packages = (
            db.query(Package)
            .filter(Package.id.in_(package_ids), Package.status == "arrived")
            .all()
        )
    finally:
        db.close()

    if not arrived_packages:
        reply_text(reply_token, "目前沒有開著、需要關門的任務")
        return

    # 同一個door_task_id只需要處理一次（complete_pickup內部會找出整組一起完成）
    seen_task_ids = set()
    labels_done = set()
    all_ok = True
    for p in arrived_packages:
        key = p.door_task_id or p.id
        if key in seen_task_ids:
            continue
        seen_task_ids.add(key)

        result = complete_pickup(str(p.id))
        labels_done.add("放貨完成" if p.task_type == "return" else "取貨完成")
        if not result["ok"]:
            all_ok = False

    if all_ok:
        reply_text(reply_token, f"已為您關門（{'、'.join(sorted(labels_done))}）")
    else:
        reply_text(reply_token, "部分任務關門失敗，請聯繫管理員協助處理")


def get_recipients(db: Session, package_id: str) -> list:
    """查詢這筆包裹當初通知過的所有LINE User ID"""
    rows = db.query(PackageRecipient).filter(PackageRecipient.package_id == package_id).all()
    return [row.line_user_id for row in rows]


def send_pending_pickup_notification(db: Session, package: Package) -> dict:
    """
    推播「包裹因故未能送達，暫存於管理室，超過72小時管理員將作廢」提醒給收件人。
    只要是被退回（拒收/逾時）或不收（作廢）的包裹，狀態一轉進去就會自動呼叫這支，
    不用等管理員手動點——例外處理頁的「通知住戶」按鈕保留下來當補發用
    （例如自動發送當下推播失敗，管理員可以再手動觸發一次）。

    voided（不收）跟拒收/逾時退回一樣都是「按下之後3天為期限」的作廢通知，
    文字用詞稍微不同（不收是住戶自己主動取消，拒收/逾時是送去才被退回），
    但期限概念完全一致，不再是voided沒有期限壓力的舊設計。

    只會真的送一次：package.pending_pickup_notified_at有值就直接跳過，
    回傳{"sent": True, "already_notified": True}代表「這次呼叫沒做事，之前已經發過了」，
    不是錯誤，呼叫端不用特別處理。

    只有真的至少成功通知到一位收件人，才會記錄pending_pickup_notified_at——
    如果全部收件人都推播失敗，這個欄位保持空白，例外處理頁會繼續顯示「通知住戶」
    按鈕讓管理員手動補發，而不是誤顯示「已通知」卻其實住戶什麼都沒收到。

    退貨任務（task_type=return，不管是暫時不退貨還是逾時沒放置也沒取消）
    文字內容不一樣：這兩種情況從頭到尾都沒有任何實體物品，不能沿用
    「您有一筆包裹暫存於管理室、將作廢」這句——那是講給「有東西在等你」的
    情境用的，這裡改成告知住戶「沒有收到退貨包裹」，不提72小時作廢期限
    （沒有東西，也就沒有東西需要作廢）。
    """
    if package.pending_pickup_notified_at is not None:
        return {"sent": True, "already_notified": True, "notified_count": 0, "notify_failed_count": 0}

    recipients = get_recipients(db, str(package.id))
    if not recipients:
        return {"sent": False, "already_notified": False, "notified_count": 0, "notify_failed_count": 0}

    if package.task_type == "return":
        message = (
            f"提醒您，您申請退貨的包裹（門牌：{package.unit}），管理室目前並未收到，"
            f"如仍有退貨需求，請重新申請或聯繫管理員協助。"
        )
    elif package.status == "voided":
        deadline_text = (now_taipei() + timedelta(hours=72)).strftime("%m月%d日%H時")
        message = (
            f"您方才取消收件的包裹（門牌：{package.unit}）將留存於管理室，"
            f"請盡快聯繫管理員領取。\n將於 {deadline_text} 由管理員作廢處理。"
        )
    else:
        deadline_text = (now_taipei() + timedelta(hours=72)).strftime("%m月%d日%H時")
        message = (
            f"您有一筆包裹（門牌：{package.unit}）因故未能送達，目前暫存於管理室，"
            f"請盡快聯繫管理員領取。\n將於 {deadline_text} 由管理員作廢處理。"
        )

    notify_failed_count = 0
    for line_user_id in recipients:
        try:
            push_status_update(line_user_id, message)
        except Exception as e:
            notify_failed_count += 1
            log_event(db, "notify_failed", detail=f"未取包裹提醒通知失敗: {e}", package_id=package.id, level="error")

    notified_count = len(recipients) - notify_failed_count

    if notified_count > 0:
        package.pending_pickup_notified_at = now_taipei()
        db.commit()
        log_event(
            db, "pending_pickup_notified",
            detail=f"通知 {notified_count}/{len(recipients)} 位收件人",
            package_id=package.id,
        )
    else:
        log_event(
            db, "notify_failed",
            detail="全部收件人推播皆失敗，未記錄pending_pickup_notified_at，保留給管理員手動補發",
            package_id=package.id, level="error",
        )

    return {
        "sent": notified_count > 0,
        "already_notified": False,
        "notified_count": notified_count,
        "notify_failed_count": notify_failed_count,
    }


def send_pending_pickup_notification_for_group(db: Session, group: list) -> dict:
    """
    同一次派送、同一批一起被退回的整組包裹（拒收、逾時、或不收）只發一次通知——
    不管門底下有幾扇、有幾件，對住戶來說都是同一件事被退回，不需要每一筆包裹
    各自收到一次幾乎一樣的推播。

    只用group裡第一筆發送，成功後把pending_pickup_notified_at同步寫回整組
    其他筆，讓每一筆各自的72小時作廢期限、例外處理頁的「已通知」狀態都正確。

    只給「系統自動判定同一批一起退回」的情境用（拒收、逾時、不收）；管理員
    在例外處理頁手動點「通知住戶」是針對他自己選的某一筆任務，維持呼叫
    單筆版本send_pending_pickup_notification，不套用這裡「整組只發一次」的邏輯。
    """
    if not group:
        return {"sent": False, "already_notified": False, "notified_count": 0, "notify_failed_count": 0}

    primary = group[0]
    result = send_pending_pickup_notification(db, primary)

    if primary.pending_pickup_notified_at is not None:
        for p in group[1:]:
            if p.pending_pickup_notified_at is None:
                p.pending_pickup_notified_at = primary.pending_pickup_notified_at
        db.commit()

    return result


def get_recipients_with_names(db: Session, package_id: str) -> list:
    """查詢這筆包裹的收件人清單，附上姓名（給例外處理頁用）"""
    rows = (
        db.query(PackageRecipient, LineBinding)
        .outerjoin(LineBinding, PackageRecipient.line_user_id == LineBinding.line_user_id)
        .filter(PackageRecipient.package_id == package_id)
        .all()
    )
    return [
        {"line_user_id": r.line_user_id, "name": b.name if b else "未知"}
        for r, b in rows
    ]


def parse_package_uuid(package_id: str):
    """
    packages.id 欄位是UUID型別，如果package_id不是合法UUID格式，
    直接拿去查DB會讓PostgreSQL在型別轉換時噴錯（invalid input syntax for type uuid），
    這個錯誤沒被攔截的話會變成500而不是乾淨的404。
    這裡先在Python端驗證格式，不合法就回傳None，不要讓這種輸入碰到DB。
    """
    try:
        return uuid.UUID(package_id)
    except (ValueError, AttributeError, TypeError):
        return None


def get_package_or_404(db: Session, package_id: str) -> Package:
    """FastAPI路由共用：查不到、或package_id格式本身就不合法，統一回傳乾淨的404"""
    parsed = parse_package_uuid(package_id)
    if parsed is None:
        raise HTTPException(status_code=404, detail="找不到這筆包裹")
    package = db.query(Package).filter(Package.id == parsed).first()
    if not package:
        raise HTTPException(status_code=404, detail="找不到這筆包裹")
    return package


def log_event(db: Session, event_type: str, detail: str = None, package_id=None, level: str = "info"):
    """
    記錄任務事件，給每日報表用。
    這裡刻意再開一個獨立的DB session來寫log，不共用呼叫端傳進來的db session，
    這樣就算呼叫端那個session之後rollback（例如外層流程failed），log紀錄本身還是保得住，
    不會因為主要交易失敗就連事件紀錄也一起消失。
    """
    print(f"[{level}][{event_type}] package_id={package_id} {detail or ''}")
    log_db = SessionLocal()
    try:
        # detail欄位是VARCHAR(500)，有些例外訊息（例如LINE API回傳的完整HTTP headers）
        # 遠超過這個長度，寫入會直接被DB拒絕、整筆log連同這次失敗紀錄一起消失。
        # 這裡先截斷，寧可紀錄不完整，也不要讓「記錄失敗」這件事本身也失敗。
        safe_detail = detail
        if safe_detail and len(safe_detail) > 500:
            safe_detail = safe_detail[:497] + "..."

        entry = TaskLog(
            package_id=package_id,
            event_type=event_type,
            level=level,
            detail=safe_detail,
        )
        log_db.add(entry)
        log_db.commit()
    except Exception as e:
        print(f"[錯誤] 寫入task_log失敗: {e}")
    finally:
        log_db.close()


def advance_trip_or_return(db: Session):
    """
    多包裹批次派送專用：一個包裹的任務結束（完成取貨/拒收/逾時，都算「這一站處理完」）之後呼叫。

    這一趟結束時，不能只靠「這一站是不是用/complete結束」來判斷機器人會不會
    自動返航——同一戶如果同時有送貨+退貨(暫時不退貨)兩個獨立任務，最後結案的
    可能是送貨那筆的/complete，也可能是退貨那筆「暫時不退貨」用的/cancel，
    所以這裡「沒有下一站、也沒有拒收/逾時/退貨待取回」時一律主動呼叫一次
    /api/door-tasks/return做確認，不去猜哪一種結案方式會讓機器人自動返航——
    這支API機器人端只是查自己資料庫裡FULL狀態的艙門，沒有東西要帶的話頂多是
    no-op，不會有副作用，比「賭一把猜對結案順序」安全。

    同一趟批次派送出去的包裹，全部會先被標成 delivering；機器人抵達某一站時，
    那一筆會被 robot_arrived 轉成 arrived；等那一站的結果出來，
    再回到這裡檢查「delivering」裡還有沒有其他還沒去過的站：
      - 還有：派送機器人去下一站（單一目的地，跟原本confirm_dispatch用的格式一樣）
      - 沒有了：這一趟結束了，檢查有沒有拒收/逾時、艙門還沒被釋放的包裹要主動帶回來

    「一站」是以door_task_id為單位，不是以門牌（unit）——就算同一戶同時有
    送貨、退貨兩個獨立任務（task_type不同、door_task_id不同），也必須分開
    各自呼叫一次/api/robot/dispatch，機器人不會因為「同一個門牌」就自動接續
    處理另一個door_task_id：實測發現機器人抵達其中一個door_task_id、那個
    任務結束之後，同一戶另一個door_task_id並不會自動被機器人接著處理，
    必須真的再發一次dispatch請求它才會動作。（這裡曾經誤以為「同一門牌只要
    去一趟」而把同門牌的所有door_task_id一起標記出發、甚至在偵測到「同一站
    還有一筆卡在delivering」時直接假設機器人已經在那裡、主動幫它補標成
    arrived並推播通知住戶——這是純軟體端的猜測，跟機器人實際有沒有真的
    到那裡完全無關，於是發生「LINE跟Dashboard都顯示已抵達，機器人卻完全
    沒有動作」的假訊號。已經拿掉這段自動判斷，狀態一律等機器人真的呼叫
    對應的callback才會改變，不再由軟體自己猜。）

    同一個door_task_id底下如果有多筆包裹（例如同收件人一次退貨2件、各自
    用不同門），這些包裹還是會一起被標記出發、一起等機器人回報抵達——
    這個「同一個door_task_id視為一站」的分組完全沒變，改變的只是「不同
    door_task_id即使同門牌也不能互相假設」這件事。

    同一站也可能同時存在兩個獨立任務（例如同一戶同時有一筆送貨、一筆退貨，
    因為task_type不同，door_task_id是分開的兩組）。其中一個任務先結束
    （例如退貨被取消、或某個task_type先完成/拒收），不代表這一站的事情
    全部辦完了——另一個task可能還在arrived狀態、等著住戶掃碼/確認，或者
    根本還沒被派送過去。這種情況下絕對不能讓機器人以為「沒有下一站了」就
    直接返航，所以要先確認「有沒有任何一筆還卡在arrived/delivering、
    等待機器人或住戶處理」，有的話這裡先不做任何事，等那一筆也處理完、
    再次呼叫這支時才繼續往下判斷。
    """
    # 有沒有任何一筆包裹目前正「在途中」——已經被真的dispatch過
    # （stop_dispatched_at有值）、但還沒有走到completed/rejected_at_door/
    # returned_timeout這些終態（還停在arrived或delivering）。不管是還在
    # 等機器人抵達回報（delivering），還是已經抵達、等住戶處理（arrived），
    # 都代表這一站的事還沒辦完，這裡先不做任何事、等它處理完、再次呼叫
    # 這支時才繼續往下判斷——不主動去猜「機器人應該已經到了」而幫它補狀態，
    # 一律照機器人實際回報的為準。
    still_pending_at_station = (
        db.query(Package)
        .filter(
            Package.status.in_(("arrived", "delivering")),
            Package.stop_dispatched_at.isnot(None),
        )
        .first()
    )
    if still_pending_at_station:
        log_event(
            db, "trip_wait",
            detail=f"unit={still_pending_at_station.unit} 還有任務尚未結束（狀態={still_pending_at_station.status}），"
                   f"暫不前往下一站或返航，等機器人真的回報才會繼續",
        )
        return

    # 用 with_for_update(nowait=True) 鎖住查詢：nowait代表鎖不到就立刻丟例外、不會傻等，
    # 因為handle_postback/advance_trip_or_return是一般的def、在async的webhook handler裡
    # 直接被呼叫（沒有丟進背景執行緒池），如果用一般的with_for_update()傻等鎖，
    # 等待期間會卡住整個uvicorn的事件迴圈，導致同一時間點進來的其他所有請求
    # （包括完全無關的包裹）都被卡住逾時，反而造成更大範圍的服務中斷。
    # 鎖不到就直接跳過：代表已經有另一個並發呼叫在處理同一批次的下一站了，
    # 讓那個呼叫處理就好，這裡不用等、也不用重複做。
    try:
        next_package = (
            db.query(Package)
            .filter(Package.status == "delivering", Package.stop_dispatched_at.is_(None))
            .order_by(Package.door_id)
            .with_for_update(nowait=True)
            .first()
        )
    except OperationalError:
        db.rollback()
        log_event(db, "dispatch_failed", detail="下一站包裹正被其他並發請求鎖住，本次跳過交給對方處理", level="warning")
        return

    if next_package:
        # 同一個door_task_id底下還沒派送的包裹一起標記出發，不只next_package自己一筆——
        # 不同door_task_id即使是同一門牌也不會一起標記，見函式最上面的說明。
        try:
            station_packages = (
                db.query(Package)
                .filter(
                    Package.status == "delivering",
                    Package.stop_dispatched_at.is_(None),
                    Package.door_task_id == next_package.door_task_id,
                )
                .with_for_update(nowait=True)
                .all()
            )
        except OperationalError:
            db.rollback()
            log_event(db, "dispatch_failed", detail="下一站包裹正被其他並發請求鎖住，本次跳過交給對方處理", level="warning")
            return

        # 先呼叫機器人、確認真的派送成功了才把stop_dispatched_at寫進DB——
        # 如果像原本那樣先寫進DB再呼叫機器人，一旦這次呼叫失敗（網路問題、
        # 機器人忙線等），這筆包裹會被誤判成「已經派送出去、正在等機器人
        # 回報抵達」，之後每次呼叫這支函式，都會被最上面的
        # still_pending_at_station擋下（它只看stop_dispatched_at有沒有值，
        # 不知道那次dispatch其實失敗了），整個系統會卡住——不會派下一站、
        # 也不會返航，而且沒有任何重試機制，只能靠管理員手動「叫回機器人」
        # 才能救回來。改成呼叫成功才寫入的話，就算這次失敗，這筆包裹還是
        # 保持「delivering、stop_dispatched_at是空的」，下一次任何一筆包裹
        # 結案觸發這支函式時，會被當成next_package重新嘗試一次，不會卡在
        # 假裝已經派送出去的錯誤狀態裡。
        ok, resp, error = call_robot_api(
            "POST", "/api/robot/dispatch",
            json={"door_task_id": str(next_package.door_task_id), "unit": next_package.unit},
            retries=1,
        )

        if ok:
            now = now_taipei()
            for p in station_packages:
                p.stop_dispatched_at = now
            db.commit()
            for p in station_packages:
                log_event(db, "dispatched", detail="批次路線，前往下一站", package_id=p.id)
        else:
            db.rollback()
            for p in station_packages:
                log_event(
                    db, "dispatch_failed",
                    detail=f"批次路線前往下一站失敗: {error}",
                    package_id=p.id, level="error",
                )
        return

    # 沒有下一站了，這一趟的所有站都處理完了。
    # 檢查這趟裡有沒有拒收/逾時、艙門還沒被admin關過（也就是還沒被機器人帶回來過）的包裹。
    pending_return = (
        db.query(Package)
        .filter(
            Package.status.in_(["rejected_at_door", "returned_timeout"]),
            Package.door_closed_at.is_(None),
        )
        .all()
    )

    # 退貨任務也要一併檢查：住戶「放貨完成」呼叫的是同一支/complete，但退貨方向
    # 艙門正確地維持full（裡面真的放了要退回的東西），機器人「艙門皆空才自動返航」
    # 的邏輯不會被觸發——這種情況不能假設機器人會自動回來，要跟拒收/逾時一樣
    # 主動呼叫/api/door-tasks/return把它帶回管理室。return_retrieved_at是空的
    # 代表管理員還沒確認取出過，這筆退貨件還沒真正結案。
    pending_return_items = (
        db.query(Package)
        .filter(
            Package.task_type == "return",
            Package.status == "completed",
            Package.return_retrieved_at.is_(None),
        )
        .all()
    )

    if not pending_return and not pending_return_items:
        # 不再區分「這一趟是不是靠force_return_check明確要求確認過」——理由見
        # 函式最上面的說明：同一戶送貨+退貨(暫時不退貨)交錯結案時，光看「這一站
        # 最後是被哪個呼叫端結束的」猜不準機器人有沒有真的自動返航，一律主動
        # 確認一次最安全。
        ok, resp, error = call_robot_api("POST", "/api/door-tasks/return", retries=1)
        if not ok:
            log_event(db, "return_failed", detail=f"確認機器人返航失敗: {error}", level="error")
        else:
            log_event(db, "trip_completed", detail="這趟結束，已主動確認機器人返航")
        return
    # 已確認：/api/door-tasks/return 呼叫一次，機器人會把所有還留在艙門裡（尚未釋放）的包裹
    # 一起帶回管理室，不需要對pending_return/pending_return_items清單裡每一筆各自呼叫一次。
    # 這支API機器人端是直接查自己資料庫裡FULL狀態的艙門，不吃也不讀body，所以不用帶任何ID；
    # 不管艙門是因為拒收/逾時是滿的，還是因為退貨放貨完成是滿的，都會一起被帶回來。
    ok, resp, error = call_robot_api("POST", "/api/door-tasks/return", retries=1)
    if not ok:
        log_event(db, "return_failed", detail=f"整趟結束、帶回拒收/逾時包裹或退貨件失敗: {error}", level="error")
    else:
        total = len(pending_return) + len(pending_return_items)
        log_event(
            db, "trip_completed",
            detail=f"這趟結束，機器人帶回 {total} 件拒收/逾時包裹或待取回的退貨件返回管理室",
        )


def call_robot_api(method: str, path: str, json: dict = None, timeout: int = 5, retries: int = 0):
    """
    統一呼叫機器人 API。
    回傳 (ok, response, error_message_or_None)。
    - 成功：response是200的requests.Response
    - 失敗但有收到回應（例如404/500）：response是那個非200的requests.Response，呼叫端可以自己檢查
      status_code / text 判斷要怎麼處理（例如判斷是不是「已經完成」這種可以視為成功的特定錯誤）
    - 失敗且完全連不上（timeout/連線被拒）：response是None
    retries=1 代表失敗後再試一次（間隔0秒，機器人API本身若是暫時性錯誤通常立即重試就有機會成功）。
    呼叫端要自己決定：ok=False時是要中止流程並回錯誤，還是繼續往下走但要大聲記錄。
    """
    url = f"{settings.ROBOT_API_BASE_URL}{path}"
    last_error = None
    last_resp = None
    for attempt in range(retries + 1):
        try:
            resp = requests.request(method, url, json=json, timeout=timeout)
        except requests.exceptions.RequestException as e:
            last_error = f"連線問題: {e}"
            continue
        last_resp = resp
        if resp.status_code == 200:
            return True, resp, None
        last_error = f"HTTP {resp.status_code}: {resp.text}"
    return False, last_resp, last_error

def is_door_actively_held(package: Package) -> bool:
    """
    這筆包裹目前是否還「握著」自己的door_id（這扇艙門還不能被別的任務拿去用）。

    共用邏輯，同時給delete_packages（判斷能不能刪除）跟resolve_door_choice
    （判斷選的門有沒有被別人佔用）用，避免兩邊各自維護一份定義、之後改一邊
    忘記改另一邊——這正是這次要修的bug的根源：resolve_door_choice原本只檢查
    status=="pickup_now"的佔用者，同門牌同收件人如果先送貨、那筆送貨任務已經
    走到delivering/arrived（艙門還沒釋放），這時候幫另一筆退貨任務選同一扇門，
    舊的檢查完全抓不到——因為它只找pickup_now狀態的佔用者，等於送貨、退貨
    能共用同一扇艙門，這是不對的，door_task_id雖然不同，但機器人上物理艙門
    只有一個，不能兩個任務同時宣稱擁有它。

    - pending/voided：從沒被分配過門或已作廢，不算佔用
    - pickup_now/delivering/arrived：艙門正被這個任務使用中（不管是還沒派送、
      派送中、還是已抵達等住戶處理），都算佔用
    - rejected_at_door/returned_timeout：機器人帶回來了，門還沒被管理員關過
      （door_closed_at是空的）之前，都算佔用；已經關過門就算釋放了
    - completed：送貨任務的completed代表已經取貨完成、艙門已釋放，不算佔用；
      但退貨任務(task_type=return)的completed代表「東西剛被住戶放進去」，
      艙門其實是滿的，要等管理員按「確認取出」（return_retrieved_at有值）
      之後才算真正釋放
    """
    if package.door_id is None:
        return False
    if package.status in ("pickup_now", "delivering", "arrived"):
        return True
    if package.status in ("rejected_at_door", "returned_timeout"):
        return package.door_closed_at is None
    if package.status == "completed" and package.task_type == "return":
        return package.return_retrieved_at is None
    return False


def try_assign_door(package_id: str, door_id: str, door_task_id, db: Session) -> tuple:
    """
    呼叫機器人「開這一個指定的艙門」（不是隨機給一個空的）——配合「管理員自己選要
    用哪個艙門」的設計。機器人team正式規格：POST /api/door-tasks/<door_task_id>/assign，
    body帶door_id、quantity、task_type。

    task_type告訴機器人這一次分配的艙門是「delivery」（送貨，機器人帶包裹來）
    還是「return」（退貨，機器人來收件、帶回管理室）——兩個方向對機器人來說
    後續動作可能不同（例如要不要主動確認艙門真的有放東西進去），所以在
    分配艙門的當下就先告知，不是等到後面才讓機器人自己猜。

    door_task_id是我們這邊產生的UUID，代表「這個門這一次被使用的任務」，放在路徑上；
    之後這個task_id底下的所有包裹，狀態轉換（抵達/驗證/完成/拒收/逾時）會綁在一起走。

    成功會把door_id/door_task_id/door_assigned_at寫進package並回傳(True, False)。
    機器人回400/409（這個門暫時不能用、被別人搶先用掉）回傳(False, True)，
    區分「不是壞掉，只是這個門暫時不能用」跟真正的連線失敗，讓呼叫端可以顯示
    比較不會誤導人的訊息。
    """
    package = db.query(Package).filter(Package.id == package_id).first()
    quantity = package.package_count if package and package.package_count else 1
    task_type = package.task_type if package and package.task_type else "delivery"

    ok, resp, error = call_robot_api(
        "POST", f"/api/door-tasks/{door_task_id}/assign",
        json={"door_id": door_id, "quantity": quantity, "task_type": task_type, "package_id": package_id},
    )
    if not ok:
        no_door_available = resp is not None and resp.status_code in (400, 409)
        log_event(
            db, "door_assign_failed",
            detail=(f"指定艙門 {door_id} 目前無法使用" if no_door_available else error),
            package_id=package_id,
            level="warning" if no_door_available else "error",
        )
        return False, no_door_available

    package.door_id = door_id
    package.door_task_id = door_task_id
    package.door_assigned_at = now_taipei()
    db.commit()

    log_event(
        db, "door_assigned",
        detail=f"door_id={door_id} door_task_id={door_task_id} quantity={quantity} task_type={task_type}",
        package_id=package_id,
    )

   # for line_user_id in get_recipients(db, package_id):
   #     push_status_update(line_user_id, f"已為您準備包裹，管理員正在安排放置艙門 {door_id}")

    return True, False

def parse_and_round_schedule_datetime(postback_params):
    """
    從datetimepicker的postback_params挖出使用者選的時間，捨入規則：
    整點到半點（含半點）之間，預約「當前這個時段」（例如選9:30，會變成9:00，
    對應9:00-10:00這個時段）；超過半點，預約「下一個時段」（例如選9:31，
    會變成10:00，對應10:00-11:00這個時段）。剛好選到整點就不用捨入。

    回傳 (selected_dt, selected_dt_raw, was_rounded, error_message)：
    - 成功：error_message是None，selected_dt是捨入後的整點（代表時段起點），
      selected_dt_raw是使用者原本選的時間
    - 失敗：selected_dt/selected_dt_raw是None，error_message是要回覆給使用者的文字
    """
    selected = None
    if postback_params is not None:
        selected = getattr(postback_params, "datetime", None)
        if selected is None and isinstance(postback_params, dict):
            selected = postback_params.get("datetime")

    if not selected:
        return None, None, False, "沒有收到您選擇的時間，請重新點選「預約取貨」"

    try:
        selected_dt_raw = datetime.strptime(selected, "%Y-%m-%dT%H:%M")
    except ValueError:
        return None, None, False, "時間格式有誤，請重新點選「預約取貨」"

    if selected_dt_raw.minute == 0 and selected_dt_raw.second == 0:
        selected_dt = selected_dt_raw.replace(second=0, microsecond=0)
        was_rounded = False
    elif selected_dt_raw.minute <= 30:
        # 整點到半點（含半點）：預約「當前這個時段」，例如選9:30 → 9:00-10:00時段
        selected_dt = selected_dt_raw.replace(minute=0, second=0, microsecond=0)
        was_rounded = True
    else:
        # 超過半點：預約「下一個時段」，例如選9:31 → 10:00-11:00時段
        selected_dt = selected_dt_raw.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        was_rounded = True

    # 這裡要用「使用者實際選的時間」(selected_dt_raw)判斷是不是未來，
    # 不能用捨入後的時段起點(selected_dt)——例如現在10:05選10:10，捨入規則
    # （分鐘<=30算當前時段）會把它捨入成10:00，如果拿10:00去跟現在比，
    # 10:00<=10:05會被誤判成「不是未來」，但使用者實際選的10:10明明就是未來。
    # 用selected_dt_raw比較才會跟使用者的直覺一致：只要你選的當下時間點還沒
    # 過去，就算捨入後的時段起點比現在早，也應該讓他能預約這個時段。
    if selected_dt_raw <= now_taipei():
        return None, None, False, "預約時間必須是未來的時段，請重新點選「預約取貨」"

    return selected_dt, selected_dt_raw, was_rounded, None


SCHEDULE_SLOT_CAPACITY = 20


def check_schedule_slot_capacity(db, selected_dt, unit):
    """
    同一個預約時段最多開放20「戶」預約（以門牌為單位，不是以包裹筆數——
    同一戶不管這次訂了幾件包裹、用同一個creation_batch_id建立幾筆，都只算一戶）。
    LINE原生的datetimepicker沒辦法把特定時段「反灰」不給選，只能等使用者選完、
    送出postback後在這裡驗證，超過上限就請他重新選擇其他時段。

    回傳None代表可以預約；回傳字串代表額滿的錯誤訊息，直接回覆給住戶。
    """
    existing_unit = {
        row[0] for row in
        db.query(Package.unit)
        .filter(Package.scheduled_pickup_at == selected_dt)
        .distinct()
        .all()
    }
    if unit in existing_unit:
        # 這一戶已經在這個時段預約過（例如同一批quantity>1的包裹），不算新增戶數
        return None
    if len(existing_unit) >= SCHEDULE_SLOT_CAPACITY:
        slot_end = selected_dt + timedelta(hours=1)
        return (
            f"{selected_dt.strftime('%m/%d %H:%M')}-{slot_end.strftime('%H:%M')} "
            f"這個時段預約已滿（上限{SCHEDULE_SLOT_CAPACITY}戶），請重新點選「預約取貨」選擇其他時段"
        )
    return None


def get_creation_batch_group(db, package, expected_status):
    """
    取得同一個creation_batch_id底下、狀態符合expected_status的所有包裹，並逐筆
    重新鎖定——建立包裹時quantity>1會產生多筆共用同一個creation_batch_id的包裹，
    只發一次到貨通知，但住戶按「取貨」/「預約取貨」/「不收」時要一次套用到整批，
    不能只動到通知裡代表用的那一筆。

    package本身已經在外層被鎖定過，這裡只需要額外鎖定「同批次的其他成員」。
    沒有creation_batch_id（quantity=1的一般情況）就只回傳自己，維持原本行為。
    鎖不到、或狀態跟預期的不一樣（代表同批次裡有一筆已經被其他操作動過），
    回傳None，呼叫端當作並發衝突處理，不要只處理一半。
    """
    if package.creation_batch_id is None:
        return [package]

    member_ids = [
        row.id for row in
        db.query(Package.id)
        .filter(Package.creation_batch_id == package.creation_batch_id)
        .all()
    ]

    group = []
    for pid in member_ids:
        if pid == package.id:
            group.append(package)
            continue
        try:
            p = db.query(Package).filter(Package.id == pid).with_for_update(nowait=True).first()
        except OperationalError:
            return None
        if not p or p.status != expected_status:
            return None
        group.append(p)
    return group


def handle_postback(data: str, reply_token: str, triggered_by: str, postback_params=None):
    """
    解析postback的data參數，格式類似 action=PICKUP_NOW&package_id=xxx。
    postback_params是LINE的datetimepicker這類「附加輸入」action回傳的額外資料
    （例如使用者選的時間），只有SCHEDULE_PICKUP會用到，其餘action都是None。

    REJECT_AT_DOOR、CANCEL_RETURN都是機器人已經抵達之後才會出現的按鈕，這時候
    住戶手上的是door_task_id（可能涵蓋不只一扇門），不是單一package_id，
    所以都拆成獨立函式處理，不走下面這段以package_id為主的共用查詢/鎖定邏輯。
    兩者語意不同：REJECT_AT_DOOR是送貨被拒收（機器人身上有實體包裹要帶回管理室，
    需要管理員後續取出處理）；CANCEL_RETURN是退貨被取消（住戶根本還沒放東西進去，
    沒有任何實體物品需要後續處理，直接結案就好）。
    """
    params = dict(item.split("=") for item in data.split("&"))
    action = params.get("action")

    if not action:
        return

    if action == "REJECT_AT_DOOR":
        handle_reject_at_door(params.get("door_task_id"), reply_token, triggered_by)
        return

    if action == "CANCEL_RETURN":
        handle_cancel_return(params.get("door_task_id"), reply_token)
        return

    package_id = params.get("package_id")
    if not package_id:
        return

    db = SessionLocal()
    try:
        parsed = parse_package_uuid(package_id)
        if parsed is None:
            reply_text(reply_token, "找不到這筆包裹資料，請聯繫管理員")
            return
        # 用 with_for_update(nowait=True) 鎖住這一列：LINE webhook偶爾會重送同一個事件
        # （例如我們這邊處理稍慢、觸發LINE的重試機制），如果兩個一模一樣的postback
        # 幾乎同時進來，沒有鎖的話兩邊都可能在對方寫入前讀到「還是舊狀態」，導致同一個動作
        # （拒收/派送）被重複觸發兩次。
        # 用nowait=True而不是傻等：這支函式是一般的def、在async的webhook handler裡
        # 直接被呼叫，沒有丟進背景執行緒池，如果傻等鎖會卡住整個uvicorn事件迴圈，
        # 讓同一時間點進來的其他所有請求（包括完全無關的包裹）都被拖累逾時，
        # 反而造成更大範圍的服務中斷。鎖不到就直接當成「這是重複的postback」跳過。
        try:
            package = db.query(Package).filter(Package.id == parsed).with_for_update(nowait=True).first()
        except OperationalError:
            db.rollback()
            reply_text(reply_token, "這筆包裹正在處理中，請稍候")
            return
        if not package:
            reply_text(reply_token, "找不到這筆包裹資料，請聯繫管理員")
            return

        if action == "SCHEDULE_PICKUP":
            if package.status != "pending":
                reply_text(reply_token, "這筆包裹已經在處理中了，請耐心等候")
                return

            selected_dt, selected_dt_raw, was_rounded, error = parse_and_round_schedule_datetime(postback_params)
            if error:
                reply_text(reply_token, error)
                return

            capacity_error = check_schedule_slot_capacity(db, selected_dt, package.unit)
            if capacity_error:
                reply_text(reply_token, capacity_error)
                return

            group = get_creation_batch_group(db, package, "pending")
            if group is None:
                db.rollback()
                reply_text(reply_token, "這筆包裹正在被其他操作處理中，請稍候再試")
                return

            for p in group:
                p.status = "pickup_now"
                p.scheduled_pickup_at = selected_dt
                log_event(
                    db, "pickup_scheduled",
                    detail=f"預約時段={selected_dt.strftime('%Y-%m-%d %H:%M')}",
                    package_id=p.id,
                )
            db.commit()
            slot_end = selected_dt + timedelta(hours=1)
            if was_rounded:
                reply_text(
                    reply_token,
                    f"預約取貨僅開放整點時段，您選擇的時間為 {selected_dt_raw.strftime('%m/%d %H:%M')}， "
                    f"系統已為您預約 {selected_dt.strftime('%m/%d %H:%M')}-{slot_end.strftime('%H:%M')} 進行送貨。"
                    "\n屆時請留意LINE通知",
                )
            else:
                reply_text(
                    reply_token,
                    f"已為您預約 {selected_dt.strftime('%m/%d %H:%M')}-{slot_end.strftime('%H:%M')} 取貨，"
                    "屆時機器人才會開始派送，請留意LINE通知",
                )
            return

        if action == "PICKUP_NOW":
            if package.status == "pending":
                group = get_creation_batch_group(db, package, "pending")
                if group is None:
                    db.rollback()
                    reply_text(reply_token, "這筆包裹正在被其他操作處理中，請稍候再試")
                    return

                for p in group:
                    p.status = "pickup_now"
                    log_event(db, "pickup_requested", package_id=p.id)
                db.commit()
                # 不再自動分配艙門——艙門是管理員在Dashboard按「放置包裹」時才會呼叫機器人開門，
                # 這裡只負責把狀態轉成pickup_now，讓這筆包裹出現在管理員的待放置清單裡
                reply_text(reply_token, "已收到您的取貨請求，管理員將盡快為您準備包裹！")
            else:
                # 已經處理過這個請求了（例如連點兩下），不要重複觸發
                reply_text(reply_token, "這筆包裹已經在處理中了，請耐心等候")

        elif action == "REJECT":
            if package.status == "pending":
                group = get_creation_batch_group(db, package, "pending")
                if group is None:
                    db.rollback()
                    reply_text(reply_token, "這筆包裹正在被其他操作處理中，請稍候再試")
                    return

                for p in group:
                    p.status = "voided"
                    log_event(db, "rejected", detail="住戶到貨通知直接按不收，包裹作廢", package_id=p.id)
                db.commit()
                reply_text(reply_token, "已為您取消這次收件，包裹不會派送，將維持在管理室")
                send_pending_pickup_notification_for_group(db, group)

                triggered_binding = db.query(LineBinding).filter(
                    LineBinding.line_user_id == triggered_by
                ).first()
                triggered_name = triggered_binding.name if triggered_binding else "同門牌住戶"

                for line_user_id in get_recipients(db, package_id):
                    if line_user_id != triggered_by:
                        try:
                            push_status_update(
                                line_user_id,
                                f"{triggered_name} 已取消這次收件，包裹不會派送",
                            )
                        except Exception as e:
                            log_event(db, "notify_failed", detail=f"推播拒收通知失敗: {e}", package_id=package_id, level="error")
            else:
                reply_text(reply_token, "這筆包裹目前無法取消收件")

        elif action == "PICKUP_DONE":
            if package.status != "arrived":
                db.close()
                reply_text(reply_token, "這筆包裹已經處理過了")
                return
            db.close()
            result = complete_pickup(package_id)
            if not result["ok"]:
                reply_text(reply_token, f"取貨確認失敗：{result['detail']}")
            return
    finally:
        db.close()


def handle_reject_at_door(door_task_id: str, reply_token: str, triggered_by: str):
    """
    住戶在機器人抵達後按「拒收」。door_task_id底下的所有包裹（可能分佈在不同扇門，
    只要是同一個收件人這一輪一起放置的）會一起被標記拒收，機器人要對每一扇門
    各自關門+關閉任務畫面。
    """
    if not door_task_id:
        reply_text(reply_token, "找不到這個艙門任務，請聯繫管理員")
        return

    try:
        task_uuid = uuid.UUID(door_task_id)
    except (ValueError, AttributeError, TypeError):
        reply_text(reply_token, "找不到這個艙門任務，請聯繫管理員")
        return

    db = SessionLocal()
    try:
        # 同樣用nowait=True，理由跟handle_postback共用邏輯那段一樣：LINE webhook
        # 偶爾重送、或跟其他操作同時發生時，鎖不到就直接跳過，不要傻等卡住事件迴圈。
        try:
            group = (
                db.query(Package)
                .filter(Package.door_task_id == task_uuid, Package.status == "arrived")
                .with_for_update(nowait=True)
                .all()
            )
        except OperationalError:
            db.rollback()
            reply_text(reply_token, "這個艙門任務正在處理中，請稍候")
            return

        if not group:
            reply_text(reply_token, "這個艙門任務目前無法拒收")
            return

        for p in group:
            p.status = "rejected_at_door"
            log_event(db, "rejected_at_door", detail="住戶在機器人抵達後按拒收", package_id=p.id)
        db.commit()

        reply_text(reply_token, "已為您取消取貨，包裹將由機器人送回管理室，請聯繫管理員協助處理")
        send_pending_pickup_notification_for_group(db, group)

        # triggered_binding = db.query(LineBinding).filter(
        #     LineBinding.line_user_id == triggered_by
        # ).first()
        # triggered_name = triggered_binding.name if triggered_binding else "同門牌住戶"

       # for line_user_id in get_recipients(db, str(group[0].id)):
       #     if line_user_id != triggered_by:
       #         push_status_update(
       #             line_user_id,
       #             f"{triggered_name} 已拒收，包裹將由機器人送回管理室",
       #         )

        # 機器人動作：呼叫一次/api/door-tasks/{door_task_id}/cancel，機器人自己會
        # 對這個task底下所有的門一起關門+關閉任務畫面（包裹此時還在艙門內，
        # 機器人還沒開始移動），不用像之前那樣逐扇門各自呼叫
        ok, resp, error = call_robot_api(
            "POST", f"/api/door-tasks/{door_task_id}/cancel", retries=1
        )
        if not ok:
            log_event(db, "cancel_task_failed", detail=error, package_id=group[0].id, level="error")

        # 這一站處理完了（拒收），但同一趟裡可能還有其他包裹在排隊等機器人送過去，
        # 不能在這裡就直接叫機器人返航——要不要返航、還是去下一站，交給下面統一判斷
        advance_trip_or_return(db)
    finally:
        db.close()


def handle_cancel_return(door_task_id: str, reply_token: str):
    """
    退貨任務：住戶按「暫時不退貨」。

    原本這裡是直接結案（status=completed+return_retrieved_at），艙門用
    /assign-timeout釋放——但機器人team從未確認過/assign-timeout是否會像
    /cancel一樣一併關閉任務畫面（收掉QR）。如果不會，機器人的任務就卡在
    「畫面上還顯示著QR、等被掃」，永遠不會往下走，等於整個任務卡住。
    /cancel已確認會確實關門+關閉任務畫面，所以改比照REJECT_AT_DOOR（送貨被
    拒收）走同一套已經驗證過的流程：呼叫/cancel、狀態轉成rejected_at_door，
    交給既有的「機器人帶回管理室 → 例外處理頁 → 開門/關門確認 → 銷案」流程
    處理，確保機器人的任務一定能收尾往下走。

    代價：這扇門實際上是空的，但機器人/資料庫都會把它當成「有東西要退回」
    看待（艙門標記full、需要管理員之後開門確認一次），管理員開門時會看到
    裡面沒有東西，直接關門、進例外處理頁銷案即可，不需要額外動作。

    這裡刻意不自動呼叫send_pending_pickup_notification_for_group——不是因為
    訊息內容不適用（send_pending_pickup_notification對task_type=return已經
    改成「沒有收到退貨包裹」這種正確的文字），而是保留給管理員自己判斷
    要不要通知：每次退貨被取消都自動推播，可能對「只是手滑點錯」這種情況
    也發一則提醒，管理員在例外處理頁看得到這筆、需要時再手動按「通知住戶」
    補發即可。
    """
    if not door_task_id:
        reply_text(reply_token, "找不到這個退貨任務，請聯繫管理員")
        return

    try:
        task_uuid = uuid.UUID(door_task_id)
    except (ValueError, AttributeError, TypeError):
        reply_text(reply_token, "找不到這個退貨任務，請聯繫管理員")
        return

    db = SessionLocal()
    try:
        try:
            group = (
                db.query(Package)
                .filter(Package.door_task_id == task_uuid, Package.status == "arrived")
                .with_for_update(nowait=True)
                .all()
            )
        except OperationalError:
            db.rollback()
            reply_text(reply_token, "這個退貨任務正在處理中，請稍候")
            return

        if not group:
            reply_text(reply_token, "這個退貨任務目前無法取消")
            return

        for p in group:
            p.status = "rejected_at_door"
            log_event(
                db, "return_cancelled",
                detail=f"住戶取消退貨，未放置任何物品，比照拒收流程處理（艙門 {p.door_id} 實際應為空）",
                package_id=p.id,
            )
        db.commit()

        reply_text(reply_token, "您已取消退貨")

        # 機器人動作：呼叫一次/api/door-tasks/{door_task_id}/cancel，已確認會對這個
        # task底下所有的門一起關門+關閉任務畫面（收掉QR），機器人才能真的往下走，
        # 不會卡在等這個QR被掃。跟真正的拒收唯一差異：這扇門其實是空的，機器人/
        # 資料庫仍會標記成full，管理員之後開門會看到裡面沒有東西，直接關門銷案即可。
        ok, resp, error = call_robot_api(
            "POST", f"/api/door-tasks/{door_task_id}/cancel", retries=1
        )
        if not ok:
            log_event(db, "cancel_task_failed", detail=error, package_id=group[0].id, level="error")

        advance_trip_or_return(db)
    finally:
        db.close()


# ========== 階段3.2 到貨通知 ==========

class CreatePackageRequest(BaseModel):
    unit: str
    recipient_name: Optional[str] = None
    quantity: int = Field(default=1, ge=1, le=4)

class PickupVerifyRequest(BaseModel):
    scanned_content: Optional[str] = None
    id_token: Optional[str] = None


@app.post("/packages", dependencies=[Depends(require_admin_auth)])
async def create_package(payload: CreatePackageRequest, db: Session = Depends(get_db)):
    """
    建立包裹：quantity決定要建立幾筆獨立的包裹任務（每一筆package_count都是1，
    不是像之前那樣單一筆用package_count代表件數）。這樣之後在「放置包裹」階段，
    管理員才能把每一件分開放進不同艙門（例如中小放H_01、大放H_02），系統不用
    事先假設這N件會放在同一扇門——門要怎麼分，是管理員自己判斷。

    quantity>1時，這N筆包裹會共用同一個creation_batch_id：住戶只會收到一次
    到貨通知（不會被N則幾乎一樣的通知洗版），但通知上「取貨」/「預約取貨」/
    「不收」這幾個按鈕，會透過creation_batch_id一次套用到整批，見handle_postback
    裡的get_creation_batch_group。
    """
    bindings = (
        db.query(LineBinding)
        .filter(LineBinding.unit == payload.unit, LineBinding.status == "active")
        .all()
    )
    if not bindings:
        raise HTTPException(
            status_code=404,
            detail=f"找不到門牌 {payload.unit} 的綁定資料，請先確認住戶已完成綁定",
        )

    targets = bindings
    if payload.recipient_name:
        matched = [b for b in bindings if b.name == payload.recipient_name]
        if matched and matched[0].solo_notify:
            targets = matched

    batch_id = uuid.uuid4() if payload.quantity > 1 else None

    packages = []
    for _ in range(payload.quantity):
        package = Package(
            unit=payload.unit,
            line_user_id=targets[0].line_user_id,
            status="pending",
            package_count=1,
            creation_batch_id=batch_id,
        )
        db.add(package)
        packages.append(package)
    db.commit()
    for package in packages:
        db.refresh(package)

    for package in packages:
        for binding in targets:
            db.add(PackageRecipient(package_id=package.id, line_user_id=binding.line_user_id, unit=payload.unit))
    db.commit()

    primary = packages[0]

    notify_failed = []
    for binding in targets:
        try:
            push_arrival_notification(binding.line_user_id, str(primary.id), payload.unit, payload.quantity)
        except Exception as e:
            # 包裹已經成功建立了，通知失敗不該讓整個request看起來像失敗，
            # 記下來讓管理員知道這個人可能沒收到通知就好
            notify_failed.append(binding.name)
            log_event(
                db, "notify_failed",
                detail=f"推播到貨通知給 {binding.name} 失敗: {e}",
                package_id=primary.id, level="error",
            )

    log_event(
        db, "created",
        detail=f"unit={payload.unit} quantity={payload.quantity} notified_count={len(targets)}"
        + (f" 通知失敗: {', '.join(notify_failed)}" if notify_failed else ""),
        package_id=primary.id,
    )

    return {
        "status": "ok",
        "package_id": str(primary.id),
        "package_ids": [str(p.id) for p in packages],
        "notified_count": len(targets) - len(notify_failed),
        "notify_failed": notify_failed,
    }

# ========== 管理員後台 API ==========

@app.get("/admin/packages", dependencies=[Depends(require_admin_auth)])
async def admin_list_packages(
    page: int = 1,
    page_size: int = 50,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    給後台頁面用的包裹清單，包含系統指派的艙門。
    後端分頁＋可選日期區間篩選（date_from/date_to格式YYYY-MM-DD），
    避免包裹資料長期累積後，每次都要把全部歷史包裹撈回來。
    """
    query = db.query(Package)

    if date_from:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="date_from格式錯誤，需要YYYY-MM-DD")
        query = query.filter(Package.created_at >= start)

    if date_to:
        try:
            end = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        except ValueError:
            raise HTTPException(status_code=400, detail="date_to格式錯誤，需要YYYY-MM-DD")
        query = query.filter(Package.created_at < end)

    total = query.count()

    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    packages = (
        query.order_by(Package.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": str(p.id),
                "unit": p.unit,
                "status": p.status,
                "task_type": p.task_type,
                "door_id": p.door_id,
                "door_task_id": str(p.door_task_id) if p.door_task_id else None,
                "line_user_id": p.line_user_id,
                "package_count": p.package_count,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "scheduled_pickup_at": p.scheduled_pickup_at.isoformat() if p.scheduled_pickup_at else None,
                "returned_at": p.returned_at.isoformat() if p.returned_at else None,
                "return_door_opened_at": p.return_door_opened_at.isoformat() if p.return_door_opened_at else None,
                "door_closed_at": p.door_closed_at.isoformat() if p.door_closed_at else None,
                "acknowledged_at": p.acknowledged_at.isoformat() if p.acknowledged_at else None,
                "return_retrieved_at": p.return_retrieved_at.isoformat() if p.return_retrieved_at else None,
            }
            for p in packages
        ],
    }


class DeletePackagesRequest(BaseModel):
    package_ids: List[str]


@app.post("/admin/packages/delete", dependencies=[Depends(require_admin_auth)])
async def delete_packages(payload: DeletePackagesRequest, db: Session = Depends(get_db)):
    """
    管理員在包裹清單勾選多筆後按「刪除已選」：直接從資料庫刪除這些包裹紀錄
    （連同對應的收件人綁定PackageRecipient）。這是硬刪除，刪掉就真的消失，
    跟系統裡其他操作（都是改status、留紀錄）完全不同，所以只讓管理員手動、
    明確勾選才能觸發，且每一筆都會在刪除前寫一筆task_log存證。

    安全限制：不允許刪除「艙門還在使用中、任務還沒走完」的包裹，判斷方式是：
    - status是pickup_now（已指派艙門）／delivering／arrived：艙門正在使用中
    - status是rejected_at_door／returned_timeout，但door_closed_at還沒設：
      機器人已經把包裹帶回來但艙門還沒被管理員關過，也算還在使用中
    這種包裹背後對應機器人身上一個實際開著/關著的艙門，資料庫紀錄消失但
    硬體狀態沒有跟著清掉，之後會對不起來、變成查無來源的艙門佔用。要刪除
    這種包裹，請先用「叫回機器人」或走完正常流程把艙門釋放掉，再回來刪除。
    completed／voided／已經door_closed_at的退回包裹，door_id即使還留著門號
    也只是歷史紀錄，不是實際佔用中，可以直接刪除。
    """
    if not payload.package_ids:
        raise HTTPException(status_code=400, detail="沒有指定要刪除的包裹")

    deleted = []
    skipped = []
    for pid in payload.package_ids:
        parsed = parse_package_uuid(pid)
        if parsed is None:
            skipped.append({"id": pid, "reason": "格式不正確"})
            continue

        package = db.query(Package).filter(Package.id == parsed).first()
        if not package:
            skipped.append({"id": pid, "reason": "找不到這筆包裹"})
            continue

        if is_door_actively_held(package):
            skipped.append({"id": pid, "reason": "艙門仍在使用中，請先叫回機器人或完成派送流程後再刪除"})
            continue

        log_event(
            db, "package_deleted",
            detail=f"unit={package.unit} status={package.status} 管理員手動刪除此筆紀錄",
            package_id=package.id,
        )
        db.query(PackageRecipient).filter(PackageRecipient.package_id == package.id).delete()
        db.delete(package)
        deleted.append(pid)

    db.commit()
    return {"status": "ok", "deleted": deleted, "skipped": skipped}


@app.get("/admin/packages/live", dependencies=[Depends(require_admin_auth)])
async def admin_live_packages(db: Session = Depends(get_db)):
    """
    Dashboard用：紅色提示框（拒收/逾時/不收待處理）、全部派送數量統計、
    機器人狀態艙門對應門牌，這三個都只需要「目前還在流程中、尚未真正結束」的包裹，
    不需要看歷史全量。

    排除掉三種已經真正結束的情況：completed（送貨任務，或退貨任務已經被管理員
    按過「確認取出」）、voided已經按過確定、拒收或逾時已經按過關門。
    退貨任務（task_type=return）狀態是completed但還沒被管理員標記取出
    （return_retrieved_at是空的）時，刻意不算「真正結束」，要留在清單裡
    讓Dashboard的提醒橫幅抓得到——這是它跟一般送貨任務completed後就算
    真正結束的唯一差異。

    排除之後剩下的資料量本質上被「目前同時在流程中的包裹數」
    限制住，不會隨著歷史包裹數量增加而變大，所以刻意跟 /admin/packages 的
    分頁查詢分開，取代原本Dashboard抓全部包裹來做這幾個判斷的做法。
    """
    packages = (
        db.query(Package)
        .filter(
            ~(
                (
                    (Package.status == "completed")
                    & ~((Package.task_type == "return") & (Package.return_retrieved_at.is_(None)))
                )
                | ((Package.status == "voided") & (Package.acknowledged_at.isnot(None)))
                | (Package.status.in_(("rejected_at_door", "returned_timeout")) & (Package.door_closed_at.isnot(None)))
            )
        )
        .order_by(Package.created_at.desc())
        .all()
    )
    return [
        {
            "id": str(p.id),
            "unit": p.unit,
            "status": p.status,
            "task_type": p.task_type,
            "door_id": p.door_id,
            "door_task_id": str(p.door_task_id) if p.door_task_id else None,
            "line_user_id": p.line_user_id,
            "package_count": p.package_count,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "scheduled_pickup_at": p.scheduled_pickup_at.isoformat() if p.scheduled_pickup_at else None,
            "returned_at": p.returned_at.isoformat() if p.returned_at else None,
            "return_door_opened_at": p.return_door_opened_at.isoformat() if p.return_door_opened_at else None,
            "door_closed_at": p.door_closed_at.isoformat() if p.door_closed_at else None,
            "acknowledged_at": p.acknowledged_at.isoformat() if p.acknowledged_at else None,
            "return_retrieved_at": p.return_retrieved_at.isoformat() if p.return_retrieved_at else None,
        }
        for p in packages
    ]


# 8種實際狀態 → 4種給管理員查詢用的簡化分類
STATUS_BUCKET = {
    "completed": "已完成",
    "delivering": "派送中",
    "arrived": "已抵達",
    "rejected_at_door": "拒收已退回",
    "returned_timeout": "逾時已退回",
    "pending": "尚未派工",
    "pickup_now": "尚未派工",
    "voided": "不派工",
}


def status_bucket_label(status: str, task_type: str = "delivery") -> str:
    """
    狀態→簡化分類文字，多一個task_type參數：rejected_at_door這個status值，
    退貨任務「暫時不退貨」（見handle_cancel_return）是比照送貨拒收的流程處理，
    共用同一個status只是為了共用「機器人帶回→開門/關門→銷案」這套機制，
    對管理員來說這兩件事語意完全不同——如果都顯示成「拒收」，會誤導管理員
    以為住戶拒收了一筆根本沒送去的退貨，所以這裡把顯示文字分開。
    """
    if status == "rejected_at_door" and task_type == "return":
        return "已取消退貨"
    return STATUS_BUCKET.get(status, status)


EXCEPTION_STATUSES = ("rejected_at_door", "returned_timeout", "voided")

@app.get("/admin/packages/by-unit", dependencies=[Depends(require_admin_auth)])
async def admin_packages_by_unit(unit: str, db: Session = Depends(get_db)):
    """管理員輸入門牌查詢這個門牌下所有包裹，狀態歸類成4種簡化分類方便一眼看懂"""
    packages = (
        db.query(Package)
        .filter(Package.unit == unit)
        .order_by(Package.created_at.desc())
        .all()
    )

    result = []
    for p in packages:
        binding = db.query(LineBinding).filter(LineBinding.line_user_id == p.line_user_id).first()
        if not binding:
            recipient_name = "未知"
        elif binding.status != "active":
            recipient_name = f"{binding.name}（已封鎖/停用）"
        else:
            recipient_name = binding.name
        result.append({
            "id": str(p.id),
            "unit": p.unit,
            "recipient_name": recipient_name,
            "raw_status": p.status,
            "task_type": p.task_type,
            "bucket": status_bucket_label(p.status, p.task_type),
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "pending_pickup_notified_at": p.pending_pickup_notified_at.isoformat() if p.pending_pickup_notified_at else None,
        })
    return result


@app.get("/admin/bindings", dependencies=[Depends(require_admin_auth)])
async def admin_list_bindings(db: Session = Depends(get_db)):
    """給建立包裹表單用的下拉選單資料，只列出還有效的綁定"""
    bindings = db.query(LineBinding).filter(LineBinding.status == "active").all()
    return [
        {"unit": b.unit, "name": b.name, "line_user_id": b.line_user_id, "solo_notify": b.solo_notify}
        for b in bindings
    ]


@app.get("/admin/line-bindings", dependencies=[Depends(require_admin_auth)])
async def admin_list_line_bindings(db: Session = Depends(get_db)):
    """
    所有門牌的所有綁定紀錄，可操作誤綁/惡意綁定紀錄。
    """
    bindings = (
        db.query(LineBinding)
        .order_by(LineBinding.unit, LineBinding.bound_at.desc())
        .all()
    )
    return [
        {
            "line_user_id": b.line_user_id,
            "unit": b.unit,
            "name": b.name,
            "status": b.status,
            "bound_at": b.bound_at.isoformat() if b.bound_at else None,
            "solo_notify": b.solo_notify,
        }
        for b in bindings
    ]


@app.post("/admin/line-bindings/{line_user_id}/delete", dependencies=[Depends(require_admin_auth)])
async def admin_delete_line_binding(line_user_id: str, db: Session = Depends(get_db)):
    """
    管理員手動刪除一筆LINE綁定（例如發現有人誤綁/惡意綁到不是自己的門牌）。
    這裡是真的把這一列從資料庫移除，不是設成inactive——LineBinding跟packages／
    package_recipients之間沒有外鍵約束，刪除後既有包裹的歷史紀錄不受影響
    （之後查不到對應binding時，既有程式碼會顯示「未知」或略過姓名資訊，不會壞掉）。
    """
    binding = db.query(LineBinding).filter(LineBinding.line_user_id == line_user_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="找不到這筆綁定")

    unit, name = binding.unit, binding.name
    db.delete(binding)
    db.commit()

    log_event(db, "line_binding_deleted", detail=f"unit={unit} name={name} line_user_id={line_user_id}")

    return {"status": "ok", "unit": unit, "name": name}


class UpdateLineBindingRequest(BaseModel):
    unit: str
    name: str


@app.post("/admin/line-bindings/{line_user_id}/update", dependencies=[Depends(require_admin_auth)])
async def admin_update_line_binding(
    line_user_id: str, payload: UpdateLineBindingRequest, db: Session = Depends(get_db)
):
    """
    管理員手動修改一筆LINE綁定的門牌或姓名——常見情境是住戶聯繫管理員反應
    綁定打錯字、或門牌搬遷，管理員直接從後台代為修正，不用請住戶自己
    重新在LINE聊天室輸入一次（重新輸入雖然也能做到，但如果住戶不方便操作
    手機，管理員代改更方便）。
    """
    binding = db.query(LineBinding).filter(LineBinding.line_user_id == line_user_id).first()
    if not binding:
        raise HTTPException(status_code=404, detail="找不到這筆綁定")

    old_unit, old_name = binding.unit, binding.name
    binding.unit = payload.unit
    binding.name = payload.name
    db.commit()

    log_event(
        db, "line_binding_updated",
        detail=f"管理員修改綁定：{old_unit} {old_name} → {payload.unit} {payload.name}",
    )

    return {"status": "ok", "unit": binding.unit, "name": binding.name}


@app.get("/admin/robot-status", dependencies=[Depends(require_admin_auth)])
async def admin_robot_status():
    """轉發呼叫機器人的即時狀態（位置、電量、各艙門狀況）"""
    try:
        resp = requests.get(f"{settings.ROBOT_API_BASE_URL}/api/dashboard/status", timeout=5)
        if resp.status_code != 200:
            return {"status": "error", "detail": f"機器人回應異常: {resp.status_code}"}
        return resp.json()
    except requests.exceptions.RequestException as e:
        return {"status": "error", "detail": f"無法連線到機器人: {e}"}


def require_robot_at_office():
    """
    開門/關門這兩個動作只有機器人真的在管理室（office）時才安全——機器人如果還在
    外面配送或返航途中，這時候手動開關門跟它實際任務狀態對不起來，可能誤觸發
    艙門硬體動作。這裡主動查一次/api/dashboard/status，比對current_location
    是不是settings.ROBOT_HOME_POINT_NAME，不是的話直接擋下，不呼叫真正的開關門API。
    """
    try:
        resp = requests.get(f"{settings.ROBOT_API_BASE_URL}/api/dashboard/status", timeout=5)
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"無法連線到機器人，無法確認機器人是否在管理室: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"機器人狀態查詢異常: {resp.status_code}")

    try:
        current_location = resp.json().get("data", {}).get("robot_status", {}).get("current_location")
    except (ValueError, AttributeError):
        current_location = None

    if current_location != settings.ROBOT_HOME_POINT_NAME:
        raise HTTPException(
            status_code=409,
            detail=f"機器人目前不在管理室（目前位置：{current_location or '未知'}），無法開關艙門",
        )


@app.post("/admin/robot/recall", dependencies=[Depends(require_admin_auth)])
async def admin_robot_recall(db: Session = Depends(get_db)):
    """
    管理員緊急工具：叫回機器人、終止目前任務——不管機器人現在正在執行什麼
    （配送中、返回中等），強制中斷並叫它回管理室。

    連帶把所有還在進行中的包裹任務（pickup_now已指派艙門／delivering／arrived）
    重置為「待派送」，原本指派的艙門一併清空。這裡不去猜機器人被打斷那一刻
    卡在哪個包裹的哪個階段——因為呼叫這支之後，不管原本在哪個階段，結果都一樣
    是機器人放棄原有任務、回管理室，所以統一重置最準確，不需要逐筆判斷。

    重置之後，管理員應接著用機器人狀態欄的「開啟艙門／關閉艙門」實際檢查
    機器人身上還留有哪些包裹並清空，再回到包裹清單重新「放置包裹→全部派送」。

    不重置：completed（已完成）、returned_timeout／rejected_at_door
    （本來就是走退回流程，回管理室是它們原定的下一步，不受這次叫回影響）、
    voided／pending（本來就沒有指派艙門，跟這次叫回無關）。

    候選名單不鎖、逐筆重新鎖定＋重新確認才動手：避免跟同時間管理員自己按的
    「放置包裹」／「全部派送」互相讀到對方寫到一半的資料。鎖不到（skip_locked）
    代表那一筆正在被別的操作處理，直接跳過，不計入這次的重置筆數。

    注意：這裡只保護了「叫回」這一側，place_package／admin_dispatch_batch
    目前還沒有加鎖，兩邊要完全避開競態，那兩支之後也需要補上同樣的機制。
    """
    ok, resp, error = call_robot_api("POST", "/api/robot/recall", retries=1)
    if not ok:
        log_event(db, "robot_recall_failed", detail=error, level="error")
        raise HTTPException(status_code=502, detail=f"呼叫機器人叫回失敗: {error}")

    candidate_ids = [
        row.id for row in
        db.query(Package.id)
        .filter(Package.status.in_(("pickup_now", "delivering")))
        .filter(Package.door_id.isnot(None))
        .all()
    ]

    reset_ids = []
    for package_id in candidate_ids:
        package = (
            db.query(Package)
            .filter(Package.id == package_id)
            .with_for_update(skip_locked=True)
            .first()
        )
        if not package:
            db.rollback()
            continue

        if package.status not in ("pickup_now", "delivering") or package.door_id is None:
            db.rollback()
            continue

        log_event(
            db, "task_recalled",
            detail=f"機器人緊急叫回，從 {package.status} 重置為 pickup_now，原艙門 {package.door_id} 已清空",
            package_id=package.id,
        )
        package.status = "pickup_now"
        package.door_id = None
        package.door_task_id = None
        package.door_assigned_at = None
        package.stop_dispatched_at = None
        package.arrived_at = None
        db.commit()
        reset_ids.append(package.id)

    log_event(
        db, "robot_recall_requested",
        detail=f"管理員手動叫回機器人、終止目前任務，連帶重置 {len(reset_ids)} 筆包裹任務",
    )
    return {"status": "ok", "reset_count": len(reset_ids)}


@app.post("/admin/robot/recharge", dependencies=[Depends(require_admin_auth)])
async def admin_robot_recharge(db: Session = Depends(get_db)):
    """管理員在Dashboard按「叫機器人回充電」，呼叫機器人回充電站"""
    ok, resp, error = call_robot_api("POST", "/api/robot/recharge", retries=1)
    if not ok:
        log_event(db, "robot_recharge_failed", detail=error, level="error")
        raise HTTPException(status_code=502, detail=f"呼叫機器人回充電站失敗: {error}")

    log_event(db, "robot_recharge_requested")
    return {"status": "ok"}


@app.post("/admin/doors/manual-open", dependencies=[Depends(require_admin_auth)])
async def admin_manual_open_doors(db: Session = Depends(get_db)):
    """
    機器人狀態欄的開門鍵：現在是艙門開門的**唯一入口**，取代原本紅色提示框
    裡逐筆包裹各自的「開門」按鈕。平常隨時都能按，用途有兩種：

    (1) 常態檢查：住戶按下「取貨完成」不代表艙門真的清空了（機器人沒有
        感測回報能力），如果一戶多件包裹住戶只拿走一部分，系統完全不會
        知道，只能靠管理員每次機器人任務完成返回時都打開檢查一次。
    (2) 拒收/逾時退回的正式流程：呼叫機器人開門成功後，會一併把所有
        「狀態是拒收/逾時、機器人已經返回(returned_at有值)、還沒開過門」
        的包裹都補上return_door_opened_at——因為機器人物理上是一次把
        所有FULL艙門打開，資料庫要跟著一次全部更新，不是只更新某一筆。

    技術上呼叫/api/doors/return-open（機器人一次打開所有FULL狀態的艙門）。
    ⚠️ 機器人端目前沒有「指定單一門號開門」的API，如果剛好有其他包裹
    正常流程中也處於FULL，會被一起打開，管理員使用時要留意艙門實際狀況。

    只有機器人真的在管理室（office）時才能操作，見require_robot_at_office()。
    """
    require_robot_at_office()

    ok, resp, error = call_robot_api("POST", "/api/doors/return-open", retries=1)
    if not ok:
        log_event(db, "manual_door_open_failed", detail=error, level="error")
        raise HTTPException(status_code=502, detail=f"呼叫機器人開門失敗: {error}")

    now = now_taipei()
    waiting_packages = (
        db.query(Package)
        .filter(
            Package.status.in_(("rejected_at_door", "returned_timeout")),
            Package.returned_at.isnot(None),
            Package.return_door_opened_at.is_(None),
        )
        .all()
    )
    for p in waiting_packages:
        p.return_door_opened_at = now
        log_event(db, "return_door_opened", package_id=p.id)
    db.commit()

    log_event(
        db, "manual_door_opened",
        detail=f"管理員開門，同時補上{len(waiting_packages)}筆等待中包裹的return_door_opened_at",
    )
    return {"status": "ok", "updated_count": len(waiting_packages)}


@app.post("/admin/doors/manual-close", dependencies=[Depends(require_admin_auth)])
async def admin_manual_close_doors(db: Session = Depends(get_db)):
    """
    機器人狀態欄的關門鍵：現在是艙門關門的**唯一入口**，取代原本紅色提示框
    裡逐筆包裹各自的「關門」按鈕，邏輯跟manual-open對稱。呼叫成功後，
    把所有「拒收/逾時、門已經開過、還沒關門」的包裹一併補上door_closed_at。

    只有機器人真的在管理室（office）時才能操作，見require_robot_at_office()。
    """
    require_robot_at_office()

    ok, resp, error = call_robot_api("POST", "/api/doors/return-complete", retries=1)
    if not ok:
        log_event(db, "manual_door_close_failed", detail=error, level="error")
        raise HTTPException(status_code=502, detail=f"呼叫機器人關門失敗: {error}")

    now = now_taipei()
    open_packages = (
        db.query(Package)
        .filter(
            Package.status.in_(("rejected_at_door", "returned_timeout")),
            Package.return_door_opened_at.isnot(None),
            Package.door_closed_at.is_(None),
        )
        .all()
    )
    for p in open_packages:
        p.door_closed_at = now
        log_event(db, "door_closed", package_id=p.id)
    db.commit()

    log_event(
        db, "manual_door_closed",
        detail=f"管理員關門，同時補上{len(open_packages)}筆包裹的door_closed_at",
    )
    return {"status": "ok", "updated_count": len(open_packages)}


@app.get("/admin/reports/daily", dependencies=[Depends(require_admin_auth)])
async def admin_daily_report(date: str, db: Session = Depends(get_db)):
    """
    每日報表：某一天的包裹狀態統計 + 任務時間軸。
    date格式：YYYY-MM-DD（依台灣當地日期，因為DB裡的時間本來就是存台灣時間，不用另外轉時區）。
    """
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="date格式錯誤，需要YYYY-MM-DD")

    day_start = datetime.combine(day, datetime.min.time())
    day_end = day_start + timedelta(days=1)

    # 包裹狀態統計：當天「有更新」的包裹（建立、狀態轉換都會更新updated_at）
    packages_today = (
        db.query(Package)
        .filter(Package.updated_at >= day_start, Package.updated_at < day_end)
        .order_by(Package.updated_at)
        .all()
    )

    status_summary = {}
    for p in packages_today:
        status_summary[p.status] = status_summary.get(p.status, 0) + 1

    # 任務時間軸
    logs_today = (
        db.query(TaskLog)
        .filter(TaskLog.created_at >= day_start, TaskLog.created_at < day_end)
        .order_by(TaskLog.created_at)
        .all()
    )

    # 任務時間軸的包裹查詢用清單：不能只看updated_at，因為很多背景排程失敗時只寫log、
    # 不會動到包裹欄位（例如check_assign_timeout/poll_robot_returned失敗時），
    # 這種情況下包裹的updated_at不會是今天，但今天確實有它的log紀錄，
    # 任務時間軸查門牌/狀態時應該要查得到，不然會誤判成「非當天建立/更新的包裹」。
    # 這裡用「今天有更新的包裹」∪「今天有log紀錄的包裹」的聯集，
    # 跟上面status_summary/package_count（真正的「今天有幾筆狀態異動」統計）分開，語意不同。
    log_package_ids = {log.package_id for log in logs_today if log.package_id}
    today_package_ids = {p.id for p in packages_today}
    extra_ids = log_package_ids - today_package_ids
    packages_for_lookup = list(packages_today)
    if extra_ids:
        packages_for_lookup += db.query(Package).filter(Package.id.in_(extra_ids)).all()

    return {
        "date": date,
        "package_status_summary": status_summary,
        "package_count": len(packages_today),
        "packages": [
            {
                "id": str(p.id),
                "unit": p.unit,
                "status": p.status,
                "door_id": p.door_id,
                "created_at": p.created_at.isoformat() if p.created_at else None,
                "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            }
            for p in packages_for_lookup
        ],
        "task_logs": [
            {
                "id": str(log.id),
                "package_id": str(log.package_id) if log.package_id else None,
                "event_type": log.event_type,
                "level": log.level,
                "detail": log.detail,
                "created_at": log.created_at.isoformat() if log.created_at else None,
            }
            for log in logs_today
        ],
        "log_count": len(logs_today),
    }

# ========== 階段3.4 管理員確認出發（放貨+派工合併） ==========

def resolve_door_choice(db: Session, package: Package, door_id: str) -> dict:
    """
    共用邏輯：管理員選了一扇門之後，決定「加入既有task」還是「開新task叫機器人開門」。
    place_package唯一的呼叫端（釋放艙門之後重新選門，也是走這支place_package，
    不再有獨立的reselect流程），確保只有一份判斷邏輯，不會分散在多處各自維護。

    「同一組door_task_id」的判斷條件是 line_user_id + unit + task_type 三者都相同——
    unit這個條件很關鍵：door_task_id代表的是「機器人這一趟要去的同一個實際地點」，
    同一個收件人如果剛好在不同門牌都有包裹待處理（例如測試時同一個LINE帳號對應
    兩個不同門牌），不能因為line_user_id相同就誤判成同一站，共用同一個door_task_id——
    這樣會導致機器人抵達其中一個地點、住戶操作（抵達/拒收/完成）時，另一個地點的
    包裹也被一起誤判狀態，機器人根本還沒去那一站就被當成已經處理完了。

    這扇門「是否已被佔用」改用is_door_actively_held廣義判斷，不再只看
    status=="pickup_now"——原本只檢查pickup_now的話，同一戶如果先送貨、
    那筆送貨任務已經走到delivering/arrived（艙門physically還沒釋放），這時候
    幫另一筆退貨任務選同一扇門，舊的檢查完全抓不到，等於同一戶的送貨、退貨
    可以共用同一扇艙門——但door_task_id不同，機器人上的艙門是同一個實體，
    絕對不能被兩個任務同時宣稱擁有。現在只有「對方是pickup_now、且line_user_id
    +unit+task_type都跟自己相同」才能加入既有task；只要對方還actively握著這扇門
    （不管什麼狀態），且不符合上述加入條件（包含task_type不同、或還沒真正pickup_now
    就已經走到後面階段的情況），一律擋下，不允許使用這扇門。

    呼叫前提：package.door_id必須已經是None（呼叫端負責先把舊的門處理乾淨）。
    """
    candidates = (
        db.query(Package)
        .filter(Package.door_id == door_id, Package.door_task_id.isnot(None), Package.id != package.id)
        .all()
    )
    active_occupants = [p for p in candidates if is_door_actively_held(p)]

    join_target = next(
        (
            p for p in active_occupants
            if p.status == "pickup_now"
            and p.line_user_id == package.line_user_id
            and p.unit == package.unit
            and p.task_type == package.task_type
        ),
        None,
    )

    if join_target:
        package.door_id = door_id
        package.door_task_id = join_target.door_task_id
        package.door_assigned_at = now_taipei()
        db.commit()
        log_event(
            db, "door_joined",
            detail=f"加入同收件人已開啟的艙門 {door_id}（door_task_id={join_target.door_task_id}），未呼叫機器人",
            package_id=package.id,
        )
        return {"status": "ok", "package_id": str(package.id), "door_id": door_id, "joined": True}

    if active_occupants:
        raise HTTPException(
            status_code=409,
            detail=f"艙門 {door_id} 目前正被其他任務使用中，請選擇其他艙門",
        )

    existing_recipient_task = (
        db.query(Package)
        .filter(
            Package.status == "pickup_now",
            Package.line_user_id == package.line_user_id,
            Package.unit == package.unit,
            Package.task_type == package.task_type,
            Package.door_task_id.isnot(None),
        )
        .first()
    )
    task_id = existing_recipient_task.door_task_id if existing_recipient_task else uuid.uuid4()

    assigned, no_door_available = try_assign_door(str(package.id), door_id, task_id, db)
    if not assigned:
        if no_door_available:
            raise HTTPException(status_code=409, detail=f"艙門 {door_id} 目前無法使用，請選擇其他艙門")
        raise HTTPException(status_code=502, detail="呼叫機器人開門失敗，請確認機器人與艙門連線狀態後再試")

    return {"status": "ok", "package_id": str(package.id), "door_id": door_id, "joined": bool(existing_recipient_task)}


class PlacePackageRequest(BaseModel):
    door_id: str


@app.post("/packages/{package_id}/place", dependencies=[Depends(require_admin_auth)])
async def place_package(package_id: str, payload: PlacePackageRequest, db: Session = Depends(get_db)):
    """
    管理員在包裹清單選擇要放進哪個艙門。只有 status=pickup_now 且還沒分配過艙門的
    包裹可以觸發，避免同一筆重複開門。判斷邏輯見resolve_door_choice。

    用列鎖重新鎖定＋重新讀取這一筆，避免雙分頁/連續手滑點擊同一筆包裹的「放置」，
    在彼此都還沒commit door_id之前，各自通過了同一次檢查。
    """
    package = get_package_or_404(db, package_id)

    try:
        db.refresh(package, with_for_update={"nowait": True})
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=409, detail="這筆包裹正在被其他操作處理中，請稍候再試")

    if package.status != "pickup_now":
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"這筆包裹目前狀態是 {package.status}，不是待放置的狀態",
        )

    if package.door_id is not None:
        db.rollback()
        raise HTTPException(status_code=400, detail="這筆包裹已經分配過艙門了")

    if package.scheduled_pickup_at is not None and now_taipei() < package.scheduled_pickup_at:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"這筆包裹預約於 {package.scheduled_pickup_at.strftime('%m/%d %H:%M')} 才能放置派送，請屆時再試",
        )

    door_id = payload.door_id
    if not door_id:
        db.rollback()
        raise HTTPException(status_code=400, detail="請選擇要放置的艙門")

    return resolve_door_choice(db, package, door_id)


@app.post("/packages/{package_id}/release-door", dependencies=[Depends(require_admin_auth)])
async def release_door(package_id: str, db: Session = Depends(get_db)):
    """
    管理員釋放這筆包裹目前佔用的艙門，把它從「已放置」退回「待放置」狀態，
    之後才能重新選另一扇門——艙門重選機制刻意設計成「一定要先釋放才能重選」，
    不再像舊版reselect-door那樣一次動作同時做「釋放舊門+指派新門」：那種寫法
    管理員很容易沒注意到舊門其實還沒真的處理好，畫面就已經跳到新選擇上。
    現在畫面上有door_id的包裹只會顯示「釋放」按鈕，釋放完成、door_id清空之後，
    畫面自然會換成跟第一次放置包裹一樣的選門下拉選單，走同一套判斷邏輯
    （見resolve_door_choice）。

    這扇門要不要真的呼叫機器人釋放，要看有沒有「同收件人的其他包裹」還在共用
    同一個door_task_id：
    - 沒有其他包裹共用：呼叫機器人 /assign-timeout，把這扇門真的釋放成empty
      （跟8分鐘自動逾時釋放用同一支API，這裡是管理員手動觸發）
    - 還有其他包裹共用（例如同收件人另一件用同一扇門）：不動那扇實體門，
      只把這一筆從裡面移出去，門留給還在用的其他包裹——這種情況下艙門實際
      狀態不會變成empty，因為它本來就還有其他東西正在使用

    只允許 status=pickup_now 且已經有door_id（已放置、還沒派送）的包裹使用。
    delivering／arrived這兩個狀態代表機器人已經實際出發了，不能用這支繞過去，
    這兩種情況要嘛等它自然跑完流程，要嘛用「叫回機器人」真的去中斷機器人的
    任務、把門打開。
    """
    package = get_package_or_404(db, package_id)

    try:
        db.refresh(package, with_for_update={"nowait": True})
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=409, detail="這筆包裹正在被其他操作處理中，請稍候再試")

    if package.status != "pickup_now" or package.door_id is None:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"這筆包裹目前狀態是 {package.status}、艙門是 {package.door_id or '尚未分配'}，"
                   f"不是「已放置、等待派送」的狀態，無法釋放",
        )

    old_door_id = package.door_id
    old_door_task_id = package.door_task_id

    sibling_count = (
        db.query(Package)
        .filter(
            Package.door_task_id == old_door_task_id,
            Package.id != package.id,
            Package.status == "pickup_now",
        )
        .count()
    )

    if sibling_count == 0:
        ok, resp, error = call_robot_api(
            "POST", f"/api/door-tasks/{old_door_task_id}/assign-timeout", retries=1
        )
        if not ok:
            db.rollback()
            raise HTTPException(status_code=502, detail=f"呼叫機器人釋放艙門 {old_door_id} 失敗，請稍後再試：{error}")
        log_event(db, "assign_timeout", detail=f"管理員手動釋放艙門 {old_door_id}", package_id=package.id)
    else:
        log_event(
            db, "door_released_manually",
            detail=f"艙門 {old_door_id} 還有同收件人其他包裹共用，只把這筆移出，不呼叫機器人",
            package_id=package.id,
        )

    package.door_id = None
    package.door_task_id = None
    package.door_assigned_at = None
    db.commit()

    return {"status": "ok", "package_id": str(package.id)}


@app.post("/admin/dispatch-batch", dependencies=[Depends(require_admin_auth)])
async def admin_dispatch_batch(db: Session = Depends(get_db)):
    """
    一次派送所有「已放置（分配好艙門）、還在等派送」的包裹，管理員全部裝載完之後只按一次。
    艙門是一次性全部關閉的（不是逐筆關），所以load這步只呼叫一次、不用帶package_id。
    機器人的dispatch API只接受單一目的地，這裡只會實際派往第一站，其餘站等機器人處理完
    第一站的結果（完成/拒收/逾時）之後，由 advance_trip_or_return 依序接續呼叫過去。

    查詢加上nowait=True的列鎖：擋下「兩個管理員/兩個分頁幾乎同時按全部派送」的情況，
    鎖不到代表已經有另一次派送正在處理中，直接回錯誤，不要讓兩邊都對機器人重複下
    「關門+出發」指令。鎖會一路保持到迴圈跑完、逐筆commit狀態時才釋放。
    """
    try:
        packages = (
            db.query(Package)
            .filter(Package.status == "pickup_now", Package.door_id.isnot(None))
            .order_by(Package.door_id)
            .with_for_update(nowait=True)
            .all()
        )
    except OperationalError:
        db.rollback()
        raise HTTPException(status_code=409, detail="有其他派送動作正在進行中，請稍候再試")

    if not packages:
        db.rollback()
        raise HTTPException(status_code=400, detail="目前沒有已放置、可以派送的包裹")

    # 關閉所有已裝載的艙門，一次性動作，機器人自己知道現在哪些門是滿的，不需要逐筆指定package_id
    ok, resp, error = call_robot_api("POST", "/api/doors/load", retries=1)
    if not ok:
        for package in packages:
            log_event(db, "dispatch_failed", detail=f"批次關門失敗: {error}", package_id=package.id, level="error")
        raise HTTPException(status_code=502, detail=f"艙門關閉失敗，請確認機器人與艙門連線狀態: {error}")

    # 已用真實錯誤訊息確認：/api/robot/dispatch 只吃單一目的地（point或unit），
    # 不支援一次帶多站清單。所以這裡只派送第一站，其餘站在each一站結束時，
    # 由 advance_trip_or_return 依序用同樣的單一目的地格式接續呼叫。
    first_package = packages[0]
    ok, resp, error = call_robot_api(
        "POST", "/api/robot/dispatch",
        json={"door_task_id": str(first_package.door_task_id), "unit": first_package.unit},
        retries=1,
    )

    # 只有跟first_package同一個door_task_id的包裹，這次才是真的被派送出去；
    # 其餘door_task_id（不管是不是同一門牌）先標記delivering、留在隊列裡，
    # 等這一站處理完，由advance_trip_or_return依序真的各自呼叫一次dispatch——
    # 不能因為同門牌就假設「一次dispatch machine就會連著處理好」，機器人並不會這樣。
    dispatched_unit = []
    for package in packages:
        if ok:
            package.status = "delivering"
            if package.door_task_id == first_package.door_task_id:
                package.stop_dispatched_at = now_taipei()
        db.commit()

        if ok:
            if package.door_task_id == first_package.door_task_id:
                log_event(
                    db, "dispatched",
                    detail=f"批次派送第一站（共{len(packages)}件已裝載），前往 {first_package.unit}",
                    package_id=package.id,
                )
                dispatched_unit.append(package.unit)
                # for line_user_id in get_recipients(db, str(package.id)):
                #     push_status_update(line_user_id, "機器人已出發，包裹正在配送中，請稍候")
            else:
                log_event(
                    db, "queued",
                    detail="已標記delivering、排入這一趟批次，等前面的站處理完才會真的呼叫機器人派送過去",
                    package_id=package.id,
                )
        else:
            log_event(
                db, "dispatch_failed",
                detail=f"艙門已關閉但批次派送失敗: {error}",
                package_id=package.id, level="error",
            )

    if not ok:
        raise HTTPException(
            status_code=502,
            detail=f"{len(packages)} 件艙門已關閉，但呼叫機器人出發失敗，包裹卡在已裝載、尚未出發的狀態，請聯繫管理員手動處理",
        )

    return {
        "status": "ok",
        "dispatched_count": len(packages),
        "total_quantity": sum(p.package_count for p in packages),
        "unit": dispatched_unit,
    }

# ========== 階段3.5 機器人抵達 ==========

def mark_group_arrived(db: Session, group: list):
    """
    把一組包裹（同一個door_task_id）標記成已抵達、推播通知住戶。目前唯一的呼叫點是
    robot_arrived——機器人真的呼叫/door-tasks/{id}/arrived回報抵達時才會執行。

    （這裡曾經還有另一個呼叫點：advance_trip_or_return發現「同一站」卡在delivering
    時，會假設機器人物理上已經在那裡、主動幫忙補標成arrived。已經拿掉了——同一戶
    就算有多個door_task_id，機器人也不會因為到過其中一個就自動處理另一個，那個假設
    是錯的，會讓LINE/Dashboard顯示已抵達，機器人卻完全沒有動作。現在狀態一律等機器人
    真的呼叫這支callback才會改變。）
    """
    now = now_taipei()
    for p in group:
        p.status = "arrived"
        p.arrived_at = now
        log_event(db, "arrived", package_id=p.id)
    db.commit()

    total_quantity = sum(p.package_count for p in group)
    recipients = set()
    for p in group:
        recipients.update(get_recipients(db, str(p.id)))

    door_task_id_str = str(group[0].door_task_id) if group[0].door_task_id else str(group[0].id)
    for line_user_id in recipients:
        try:
            push_arrived_notification(line_user_id, door_task_id_str, total_quantity, group[0].task_type)
        except Exception as e:
            log_event(db, "notify_failed", detail=f"推播抵達通知失敗: {e}", package_id=group[0].id, level="error")


@app.post("/door-tasks/{door_task_id}/arrived")
async def robot_arrived(door_task_id: str, db: Session = Depends(get_db)):
    """
    機器人抵達時呼叫，帶的是door_task_id（機器人team已確認回報用這個值，不是
    package_id）。把這個task底下所有還在delivering狀態的包裹（可能分佈在不同
    扇門）一起標記抵達，通知也彙整成一則，不會讓同一個收件人收到好幾則幾乎
    一樣的推播。
    """
    try:
        task_uuid = uuid.UUID(door_task_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="找不到這個艙門任務")

    group = (
        db.query(Package)
        .filter(Package.door_task_id == task_uuid, Package.status == "delivering")
        .all()
    )
    if not group:
        # 查不到「還在delivering」的成員，可能是這個task根本不存在，
        # 也可能是存在但狀態已經不是delivering了（例如抵達前就被拒收/叫回）——
        # 這兩種情況分開回報，比較好排查
        existing_any = db.query(Package).filter(Package.door_task_id == task_uuid).first()
        if not existing_any:
            raise HTTPException(status_code=404, detail="找不到這個艙門任務")
        raise HTTPException(
            status_code=400,
            detail=f"這個艙門任務目前狀態是 {existing_any.status}，不是配送中的狀態",
        )

    mark_group_arrived(db, group)

    return {"status": "ok", "door_task_id": door_task_id, "new_status": "arrived", "group_size": len(group)}


@app.post("/door-tasks/{door_task_id}/pickup-complete")
async def pickup_verify(door_task_id: str, payload: PickupVerifyRequest = None, db: Session = Depends(get_db)):
    """
    LIFF掃碼開門：QR內容就是door_task_id本身，不再是單一package_id——同一個
    door_task_id底下不管有幾扇門、幾筆包裹，掃一次就會把「所有」門一起打開，
    身分驗證也只看「這個task底下任一筆包裹的收件人」，不用逐筆比對每一筆包裹。
    """
    try:
        task_uuid = uuid.UUID(door_task_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="找不到這個艙門任務")

    group = (
        db.query(Package)
        .filter(Package.door_task_id == task_uuid, Package.status == "arrived")
        .all()
    )
    if not group:
        raise HTTPException(status_code=400, detail="這個艙門任務目前沒有等待取貨的包裹")

    scanned = payload.scanned_content if payload else None
    if not scanned or scanned != door_task_id:
        raise HTTPException(status_code=403, detail="取貨碼驗證失敗")

    id_token = payload.id_token if payload else None
    if not id_token:
        raise HTTPException(status_code=403, detail="缺少身分驗證資訊，請重新從LINE進入")

    try:
        claims = verify_liff_id_token(id_token, settings.LINE_LOGIN_CHANNEL_ID)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=f"身分驗證失敗：{e}")

    scanning_user_id = claims.get("sub")
    all_recipients = set()
    for p in group:
        all_recipients.update(get_recipients(db, str(p.id)))
    if scanning_user_id not in all_recipients:
        raise HTTPException(status_code=403, detail="您不是這個艙門任務的收件人，無法取貨")

    # 上面這些驗證步驟（尤其是呼叫LINE驗證id_token）需要一點時間，這段期間
    # 如果同一組任務被別的動作（例如逾時排程）改掉狀態，這裡要重新確認過。
    # 逐筆重新鎖定，用nowait=True鎖不到就立刻失敗，不要傻等卡住整個事件迴圈。
    locked_group = []
    for p in group:
        try:
            fresh = db.query(Package).filter(Package.id == p.id).with_for_update(nowait=True).first()
        except OperationalError:
            db.rollback()
            raise HTTPException(status_code=409, detail="這個艙門任務正在被其他操作處理中，請稍候再試")
        if not fresh or fresh.status != "arrived":
            db.rollback()
            raise HTTPException(status_code=400, detail="這個艙門任務的狀態已經改變，請重新整理後再試")
        locked_group.append(fresh)

    # 這裡沒有要改任何欄位，純粹確認完狀態就結束交易放掉鎖——機器人開門這個
    # 比較慢的外部呼叫刻意留在鎖外面執行，避免鎖住這些列太久，卡住排程或
    # 管理員對同一組任務的其他操作。
    db.commit()

    # 呼叫一次機器人開這個task底下的門——機器人team確認/api/door-tasks/{door_task_id}/
    # pickup-complete這支，機器人自己會找出door_task_id底下所有對應的門一起打開，
    # 我們這邊不用逐扇門迴圈呼叫。
    ok, resp, error = call_robot_api(
        "POST", f"/api/door-tasks/{door_task_id}/pickup-complete", retries=1
    )
    if not ok:
        log_event(db, "pickup_open_failed", detail=error, package_id=locked_group[0].id, level="error")
        raise HTTPException(status_code=502, detail="機器人開門失敗，請聯繫管理員協助取件")

    door_ids_opened = [p.door_id for p in locked_group]

    # 機器人開門這個關鍵動作已經成功了，記log只是附加動作，就算這段出錯，
    # 也不該讓整個request變成500回給LIFF——住戶端看到的應該還是「門已經開了」
    # 的成功畫面，附加動作失敗頂多在後台log裡看得到，不影響住戶體驗。
    try:
        log_event(
            db, "pickup_opened",
            detail=f"scanned_by={scanning_user_id} door_ids={door_ids_opened}",
            package_id=locked_group[0].id,
        )
    except Exception as e:
        print(f"[pickup_verify] 開門成功後的log流程發生未預期例外: {e}")

    return {"status": "ok", "message": "驗證通過，艙門已開啟", "door_ids": door_ids_opened}

def complete_pickup(package_id: str) -> dict:
    """
    真正的業務邏輯：把door_task_id底下所有包裹（可能分佈在不同扇門）一起標記完成、
    逐一通知機器人關門返航、推播通知。不依賴FastAPI路由，可以被 /docs 的API呼叫，
    也可以被 handle_postback 直接呼叫。回傳一個dict，裡面標明成功與否，呼叫的地方
    自己決定怎麼處理。
    """
    db = SessionLocal()
    try:
        parsed = parse_package_uuid(package_id)
        if parsed is None:
            return {"ok": False, "detail": "找不到這筆包裹"}

        # 同樣用nowait=True，理由跟handle_postback一樣：這支函式常常被async的
        # webhook handler直接呼叫，傻等鎖會卡住整個事件迴圈，影響到完全無關的請求。
        try:
            package = db.query(Package).filter(Package.id == parsed).with_for_update(nowait=True).first()
        except OperationalError:
            db.rollback()
            return {"ok": False, "detail": "這筆包裹正在處理中，請稍候再試"}
        if not package:
            return {"ok": False, "detail": "找不到這筆包裹"}

        if package.status != "arrived":
            return {"ok": False, "detail": f"這筆包裹目前狀態是 {package.status}，不是可以完成取貨的狀態"}

        if package.door_task_id is not None:
            try:
                group = (
                    db.query(Package)
                    .filter(Package.door_task_id == package.door_task_id, Package.status == "arrived")
                    .with_for_update(nowait=True)
                    .all()
                )
            except OperationalError:
                db.rollback()
                return {"ok": False, "detail": "這個艙門任務正在處理中，請稍候再試"}
        else:
            group = [package]

        for p in group:
            p.status = "completed"
        db.commit()

        # 呼叫一次機器人關這個task底下的所有門+釋放艙門——機器人team確認
        # /api/door-tasks/{door_task_id}/complete這支，機器人自己會找出door_task_id
        # 底下所有對應的門一起關閉，我們這邊不用逐扇門迴圈呼叫。內建邏輯：如果這是
        # 最後一個非空艙門，機器人會自己判斷、自動返航，這裡只要往下呼叫
        # advance_trip_or_return 去檢查「還有沒有下一站要去」就好。
        ok, resp, error = call_robot_api(
            "POST", f"/api/door-tasks/{package.door_task_id}/complete", retries=1
        )
        if ok:
            for p in group:
                log_event(db, "completed", package_id=p.id)
        else:
            # 使用者已經拿到包裹了（門在pickup_verify那步就開過），這件事不能反悔；
            # 但機器人關門釋放艙門這步確實失敗，需要人工去確認艙門實際狀態
            for p in group:
                log_event(
                    db, "complete_failed",
                    detail=f"取貨完成後關門釋放艙門失敗: {error}",
                    package_id=p.id, level="error",
                )

       # for line_user_id in get_recipients(db, package_id):
       #     push_status_update(line_user_id, "取貨完成，感謝使用！")

        advance_trip_or_return(db)

        return {"ok": True, "package_id": package_id, "new_status": "completed", "group_size": len(group)}
    finally:
        db.close()


@app.post("/door-tasks/{door_task_id}/complete")
async def door_task_complete(door_task_id: str):
    """
    API路由，LIFF頁面「取貨完成」按鈕呼叫，也可以在/docs測試。內部找出這個
    task_id底下任一筆包裹，轉呼叫complete_pickup()處理整組。
    """
    try:
        task_uuid = uuid.UUID(door_task_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="找不到這個艙門任務")

    db = SessionLocal()
    try:
        representative = db.query(Package).filter(Package.door_task_id == task_uuid).first()
    finally:
        db.close()

    if not representative:
        raise HTTPException(status_code=404, detail="找不到這個艙門任務")

    result = complete_pickup(str(representative.id))
    if not result["ok"]:
        raise HTTPException(status_code=400, detail=result["detail"])
    return {
        "status": "ok",
        "door_task_id": door_task_id,
        "new_status": result["new_status"],
        "group_size": result.get("group_size", 1),
    }

# ========== 階段3.7 逾時自動退回 ==========

def check_pickup_timeout():
    """
    檢查arrived狀態超過8分鐘還沒完成取貨的包裹，自動觸發退回：清QR+關門、機器人送回管理室+開門。
    以door_task_id為單位一起處理——同一個task底下的包裹arrived_at是同一時間設的
    （robot_arrived那支一次性設完整組），要嘛一起逾時、要嘛都還沒逾時。

    分兩階段：第一階段不鎖，只抓候選的door_task_id清單（這份快照允許稍微過時）；
    第二階段逐一task、逐筆重新用skip_locked鎖定＋重新確認條件仍成立才真的動手，
    只要這個task裡有任何一筆鎖不到或條件不成立，整個task這一輪就跳過、不處理，
    避免只退回一半。這樣才能避免「住戶剛好在第8分鐘完成取貨」跟這支排程同時
    發生時，排程拿著舊資料把住戶剛完成的completed蓋回returned_timeout——
    鎖不到（代表那一筆正被別的操作處理）就直接跳過，不等、不報錯，下一分鐘
    還會再檢查一次。
    """
    from datetime import timedelta

    db = SessionLocal()
    try:
        timeout_threshold = now_taipei() - timedelta(minutes=8)

        candidate_task_ids = [
            row[0] for row in
            db.query(Package.door_task_id)
            .filter(
                Package.status == "arrived",
                Package.arrived_at <= timeout_threshold,
                Package.door_task_id.isnot(None),
            )
            .distinct()
            .all()
        ]
        # 沒有door_task_id的舊資料（理論上不該出現，保留相容）單獨當一筆處理
        candidate_solo_ids = [
            row.id for row in
            db.query(Package.id)
            .filter(
                Package.status == "arrived",
                Package.arrived_at <= timeout_threshold,
                Package.door_task_id.is_(None),
            )
            .all()
        ]

        def process_group(group):
            for p in group:
                p.status = "returned_timeout"
                log_event(db, "returned_timeout", detail="超過8分鐘未取貨，自動觸發退回", package_id=p.id)
            db.commit()

            send_pending_pickup_notification_for_group(db, group)

            # 機器人動作：呼叫一次，機器人自己對這個task底下所有的門關門+關閉任務畫面
            # （清掉QR，包裹此時還在艙門內），不用逐扇門迴圈呼叫
            ok, resp, error = call_robot_api(
                "POST", f"/api/door-tasks/{group[0].door_task_id}/cancel", retries=1
            )
            if not ok:
                for p in group:
                    log_event(db, "cancel_task_failed", detail=f"逾時退回時: {error}", package_id=p.id, level="error")

            # 這一站處理完了（逾時），但同一趟裡可能還有其他包裹在排隊，
            # 不能在這裡就直接叫機器人返航——要不要返航、還是去下一站，交給下面統一判斷
            advance_trip_or_return(db)

        for task_id in candidate_task_ids:
            member_ids = [
                row.id for row in
                db.query(Package.id)
                .filter(Package.door_task_id == task_id, Package.status == "arrived")
                .all()
            ]

            group = []
            group_ok = True
            for package_id in member_ids:
                p = (
                    db.query(Package)
                    .filter(Package.id == package_id)
                    .with_for_update(skip_locked=True)
                    .first()
                )
                if not p or p.status != "arrived" or p.arrived_at is None or p.arrived_at > timeout_threshold:
                    group_ok = False
                    continue
                group.append(p)

            if not group or not group_ok:
                # 這個task裡只要有一筆鎖不到或條件不成立，整組這一輪都跳過，
                # 不要只退回一半，下一分鐘再重新檢查整組
                db.rollback()
                continue

            process_group(group)

        for package_id in candidate_solo_ids:
            package = (
                db.query(Package)
                .filter(Package.id == package_id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if not package:
                db.rollback()
                continue

            if package.status != "arrived" or package.arrived_at is None or package.arrived_at > timeout_threshold:
                db.rollback()
                continue

            process_group([package])
    finally:
        db.close()


def check_assign_timeout():
    """
    檢查「放置包裹」開的艙門，開了超過8分鐘管理員還沒實際裝箱派送（door_assigned_at超時、
    仍是pickup_now狀態、door_id還在），視為管理員最後沒有真的放進去，
    呼叫機器人 /api/door-tasks/{door_task_id}/assign-timeout 請它自動關門（門回到empty），
    我們這邊把door_id/door_assigned_at清掉，讓這筆包裹回到「還沒分配艙門」，
    可以在Dashboard重新按一次「放置包裹」再試一次。

    門檻定為8分鐘（不是10分鐘）：機器人超過10分鐘沒有動作會死機，所以逾時判斷
    必須抓在10分鐘之內，統一跟 check_pickup_timeout / check_return_timeout 用同樣的8分鐘。

    跟check_pickup_timeout同樣的兩階段鎖定寫法：候選名單不鎖，逐筆重新鎖定＋
    重新確認才動手，避免跟同時間管理員自己按的「放置包裹」互相蓋掉彼此的寫入。
    """
    from datetime import timedelta

    db = SessionLocal()
    try:
        timeout_threshold = now_taipei() - timedelta(minutes=8)
        candidate_ids = [
            row.id for row in
            db.query(Package.id)
            .filter(
                Package.status == "pickup_now",
                Package.door_id.isnot(None),
                Package.door_assigned_at.isnot(None),
                Package.door_assigned_at <= timeout_threshold,
            )
            .all()
        ]

        for package_id in candidate_ids:
            package = (
                db.query(Package)
                .filter(Package.id == package_id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if not package:
                db.rollback()
                continue

            if (
                package.status != "pickup_now"
                or package.door_id is None
                or package.door_assigned_at is None
                or package.door_assigned_at > timeout_threshold
            ):
                db.rollback()
                continue

            ok, resp, error = call_robot_api(
                "POST", f"/api/door-tasks/{package.door_task_id}/assign-timeout", retries=1
            )
            if not ok:
                log_event(db, "assign_timeout_failed", detail=error, package_id=package.id, level="error")
                db.rollback()
                continue

            log_event(
                db, "assign_timeout",
                detail=f"door_id={package.door_id} 超過8分鐘未派送，機器人已自動關門釋放",
                package_id=package.id,
            )
            package.door_id = None
            package.door_task_id = None
            package.door_assigned_at = None
            db.commit()
    finally:
        db.close()


def check_return_timeout():
    """
    檢查退回流程（拒收/逾時）管理員按「開門」之後，超過8分鐘還沒按「關門」完成取件
    （return_door_opened_at超時、door_closed_at仍是空的），視為管理員最後沒有實際取件，
    呼叫機器人 /api/doors/return-timeout 請它自動關門（包裹還在裡面，門維持full），
    我們這邊把return_door_opened_at重設回空，讓Dashboard的紅色提示框重新顯示「開門」按鈕，
    管理員可以再按一次重新開門取件。

    8分鐘：機器人超過10分鐘沒有動作會死機，所以門檻抓在10分鐘之內，
    跟 check_pickup_timeout / check_assign_timeout 三支統一用同樣的8分鐘。

    同樣的兩階段鎖定寫法：候選名單不鎖，逐筆重新鎖定＋重新確認才動手。
    """
    from datetime import timedelta

    db = SessionLocal()
    try:
        timeout_threshold = now_taipei() - timedelta(minutes=8)
        candidate_ids = [
            row.id for row in
            db.query(Package.id)
            .filter(
                Package.status.in_(("rejected_at_door", "returned_timeout")),
                Package.return_door_opened_at.isnot(None),
                Package.door_closed_at.is_(None),
                Package.return_door_opened_at <= timeout_threshold,
            )
            .all()
        ]

        for package_id in candidate_ids:
            package = (
                db.query(Package)
                .filter(Package.id == package_id)
                .with_for_update(skip_locked=True)
                .first()
            )
            if not package:
                db.rollback()
                continue

            if (
                package.status not in ("rejected_at_door", "returned_timeout")
                or package.return_door_opened_at is None
                or package.door_closed_at is not None
                or package.return_door_opened_at > timeout_threshold
            ):
                db.rollback()
                continue

            ok, resp, error = call_robot_api(
                "POST", "/api/doors/return-timeout", retries=1
            )
            if not ok:
                log_event(db, "return_timeout_failed", detail=error, package_id=package.id, level="error")
                db.rollback()
                continue

            log_event(
                db, "return_timeout",
                detail="開門後超過8分鐘未取件，機器人已自動關門",
                package_id=package.id,
            )
            package.return_door_opened_at = None
            db.commit()
    finally:
        db.close()


def poll_robot_returned():
    """
    機器人端的 /api/packages/return（我們呼叫它、通知機器人帶著拒收/逾時包裹回家）
    目前沒有做「抵達後回頭通知我們」這件事（不像 /api/robot/dispatch 有背景輪詢+通知），
    所以這裡由我們自己主動輪詢 /api/dashboard/status，比對 current_location
    是否已經等於機器人回到家的那個點位名稱（settings.ROBOT_HOME_POINT_NAME），
    一旦偵測到，代表機器人真的到家了，我們自己補上 returned_at，
    不用等機器人team補齊那段回報邏輯。

    只有「確實有包裹在等退回」（狀態是rejected_at_door/returned_timeout、returned_at還是空）
    時才會真的去打這支API，沒有包裹在等的話直接跳過，不會每分鐘都無意義地打一次。
    """
    db = SessionLocal()
    try:
        waiting_packages = (
            db.query(Package)
            .filter(
                Package.status.in_(("rejected_at_door", "returned_timeout")),
                Package.returned_at.is_(None),
            )
            .all()
        )
        if not waiting_packages:
            return

        ok, resp, error = call_robot_api("GET", "/api/dashboard/status")
        if not ok:
            log_event(db, "poll_returned_failed", detail=error, level="error")
            return

        try:
            current_location = resp.json().get("data", {}).get("robot_status", {}).get("current_location")
        except (ValueError, AttributeError):
            current_location = None

        if current_location != settings.ROBOT_HOME_POINT_NAME:
            return

        for package in waiting_packages:
            package.returned_at = now_taipei()
            log_event(
                db, "returned",
                detail=f"輪詢/api/dashboard/status偵測到機器人已回到{current_location}，自行補上returned_at",
                package_id=package.id,
            )
        db.commit()
    finally:
        db.close()


def check_stuck_dispatch():
    """
    安全網，補advance_trip_or_return「派送下一站失敗」的漏洞：那支函式失敗時
    只會記log，不會自動重試——要等下一次任何一筆別的包裹結案，才會有機會
    重新把同一個next_package當成候選再試一次。如果失敗的剛好是這一趟的
    最後一站（例如同一戶退貨結束後要接著送貨，但送貨那次dispatch呼叫失敗），
    後面就再也沒有其他包裹結案的事件會觸發它了，機器人會一直閒置在原地、
    這筆包裹卡在「delivering、還沒真的派送」，沒有人知道、也不會自動恢復。

    這裡每隔幾分鐘主動檢查一次：只要系統裡還有任何一筆包裹是delivering狀態
    （代表確實有一趟還在進行中，不是整個系統完全閒置），就呼叫一次
    advance_trip_or_return順便重試——不管上次是卡在「還沒派送成功」還是
    單純「還沒輪到它」，這支函式本身的判斷邏輯都能正確處理，不會因為多呼叫
    幾次就重複派工。系統完全閒置（沒有任何delivering中的包裹）時直接跳過，
    不會無謂地一直打機器人API。
    """
    db = SessionLocal()
    try:
        anything_in_flight = db.query(Package.id).filter(Package.status == "delivering").first()
        if not anything_in_flight:
            return
        advance_trip_or_return(db)
    finally:
        db.close()


scheduler = BackgroundScheduler()


# 原本這裡有一支 check_auto_close_case 排程：超過72小時自動幫管理員銷案。
# 已依需求刪除這個自動銷案的行為——現在不自動處理，改成在例外處理頁面
# 把超過72小時還沒銷案的包裹用視覺提醒標示出來（見admin_list_exceptions的
# needs_attention欄位），交給管理員自己判斷要銷案還是要重新派貨。


scheduler.add_job(check_pickup_timeout, "interval", minutes=1)
scheduler.add_job(check_assign_timeout, "interval", minutes=1)
scheduler.add_job(check_return_timeout, "interval", minutes=1)
scheduler.add_job(poll_robot_returned, "interval", seconds=20)
scheduler.add_job(check_stuck_dispatch, "interval", minutes=2)
scheduler.start()


# ========== 階段3.7 機器人真正返回管理室 ==========

@app.post("/packages/{package_id}/returned")
async def robot_returned(package_id: str, db: Session = Depends(get_db)):
    """
    機器人實際回到管理室時，由送貨機器人模組呼叫。
    這裡只記錄「機器人已經回來了」，艙門保持關閉——不再像之前那樣機器人一回來就自動開門，
    改成管理員在Dashboard按「開門」才會真的呼叫機器人開門（見下面的 open_return_door）。
    不通知住戶（退回當下已經通知過了），只留紀錄給管理員後台知道。
    """
    package = get_package_or_404(db, package_id)

    if package.status not in ("rejected_at_door", "returned_timeout"):
        raise HTTPException(
            status_code=400,
            detail=f"這筆包裹目前狀態是 {package.status}，不是退回中的狀態",
        )

    package.returned_at = now_taipei()
    db.commit()

    log_event(db, "returned", detail=f"status={package.status}", package_id=package.id)

    return {"status": "ok", "package_id": str(package.id)}


@app.post("/packages/{package_id}/acknowledge", dependencies=[Depends(require_admin_auth)])
async def acknowledge_voided_package(package_id: str, db: Session = Depends(get_db)):
    """
    不收（voided）的包裹沒有機器人動作要做，不需要開關門，
    純粹是管理員在Dashboard紅色提示框裡按「確定」，表示已經知悉這件事、不用再提醒。
    """
    package = get_package_or_404(db, package_id)

    if package.status != "voided":
        raise HTTPException(
            status_code=400,
            detail=f"這筆包裹目前狀態是 {package.status}，不是不收（作廢）的狀態",
        )

    if package.acknowledged_at is not None:
        raise HTTPException(status_code=400, detail="這筆包裹已經確認過了")

    package.acknowledged_at = now_taipei()
    db.commit()
    log_event(db, "voided_acknowledged", package_id=package.id)

    return {"status": "ok", "package_id": str(package.id)}


@app.post("/packages/{package_id}/confirm-return-retrieved", dependencies=[Depends(require_admin_auth)])
async def confirm_return_retrieved(package_id: str, db: Session = Depends(get_db)):
    """
    退貨任務：住戶已經放貨完成、機器人把包裹帶回管理室後，管理員實際從艙門
    取出退貨件，在Dashboard的提醒橫幅裡按「確認取出」，表示已經處理完這筆。
    這支只是打勾記錄，不涉及機器人動作——艙門本身的開關已經在「開啟艙門／
    關閉艙門」那兩顆按鈕處理過了，這裡純粹是讓這筆退貨任務從「待取出」
    提醒清單裡消失。
    """
    package = get_package_or_404(db, package_id)

    if package.task_type != "return":
        raise HTTPException(status_code=400, detail="這筆包裹不是退貨任務")

    if package.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"這筆退貨任務目前狀態是 {package.status}，還沒到「住戶已放貨完成」的階段",
        )

    if package.return_retrieved_at is not None:
        raise HTTPException(status_code=400, detail="這筆退貨件已經確認取出過了")

    package.return_retrieved_at = now_taipei()
    db.commit()
    log_event(db, "return_retrieved", package_id=package.id)

    return {"status": "ok", "package_id": str(package.id)}


@app.post("/packages/{package_id}/force-resolve", dependencies=[Depends(require_admin_auth)])
async def force_resolve_package(package_id: str, db: Session = Depends(get_db)):
    """
    管理員手動處理機器人硬體（例如直接在機器人端把艙門清空、跳過我們系統整套
    開門/關門流程）之後的補救用：直接把這筆包裹標記為已解決，不呼叫任何機器人API，
    純粹更新資料庫欄位，讓Dashboard的紅色提示框可以正常消失——不用再手動去
    資料庫刪包裹這麼粗暴、容易出錯又清不乾淨的做法。

    只允許對「失敗/退回、且還沒真正結案」的包裹使用：
    - voided：補acknowledged_at（效果跟正常按「確定」一樣）
    - rejected_at_door / returned_timeout：returned_at/return_door_opened_at/door_closed_at
      這串正常應該依序各自完成的時間戳記，只要還沒設過就一次補齊，因為既然是手動
      處理過了，中間這幾個步驟的先後順序已經不重要，直接視為整段流程都完成。
    """
    package = get_package_or_404(db, package_id)

    if package.status not in EXCEPTION_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"這筆包裹目前狀態是 {package.status}，不是失敗/退回狀態，不需要手動結案",
        )

    now = now_taipei()
    if package.status == "voided":
        if package.acknowledged_at is not None:
            raise HTTPException(status_code=400, detail="這筆包裹已經確認過了")
        package.acknowledged_at = now
    else:
        if package.door_closed_at is not None:
            raise HTTPException(status_code=400, detail="這筆包裹已經關門過了")
        if package.returned_at is None:
            package.returned_at = now
        if package.return_door_opened_at is None:
            package.return_door_opened_at = now
        package.door_closed_at = now

    db.commit()
    log_event(
        db, "force_resolved",
        detail="管理員手動處理機器人硬體後，直接標記為已解決（未呼叫機器人API）",
        package_id=package.id,
    )

    return {"status": "ok", "package_id": str(package.id)}

# ========== QR Code 掃描 LIFF ==========

@app.get("/liff/scan", response_class=HTMLResponse)
async def liff_scan_page():
    html = LIFF_SCAN_HTML.replace("__LIFF_ID__", settings.LIFF_ID)
    return HTMLResponse(content=html)


@app.get("/liff/return-request", response_class=HTMLResponse)
async def liff_return_request_page():
    html = LIFF_RETURN_REQUEST_HTML.replace("__LIFF_ID_RETURN__", settings.LIFF_ID_RETURN)
    return HTMLResponse(content=html)


class ReturnRequestPayload(BaseModel):
    id_token: str
    quantity: int = Field(default=1, ge=1, le=4)


@app.post("/liff/return-request/submit")
async def submit_return_request(payload: ReturnRequestPayload, db: Session = Depends(get_db)):
    """
    住戶在退貨LIFF頁面填完件數送出。跟管理員建立包裹（create_package）的角色相反：
    這裡是住戶自己申請，不是管理員登記，所以直接建立quantity筆task_type="return"
    的Package，狀態一開始就是pickup_now（不用像到貨通知那樣還要住戶回覆
    取貨/預約/不收，因為住戶剛剛才主動送出這個請求，等於已經確認要退貨了）。

    quantity>1時不套用creation_batch_id分組——那個機制是為了讓「到貨通知」的
    取貨/預約/不收postback一次套用到整批，退貨任務一開始就是pickup_now，
    不會經過那個通知/回覆流程，用不到這個分組。真正需要「一次處理」的地方
    是door_task_id（放置艙門時才產生），跟一般送貨任務共用同一套機制。
    """
    try:
        claims = verify_liff_id_token(payload.id_token, settings.LINE_LOGIN_CHANNEL_ID)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=f"身分驗證失敗：{e}")

    line_user_id = claims.get("sub")
    binding = (
        db.query(LineBinding)
        .filter(LineBinding.line_user_id == line_user_id, LineBinding.status == "active")
        .first()
    )
    if not binding:
        raise HTTPException(
            status_code=404,
            detail="找不到您的綁定資料，請先在LINE聊天室輸入「門牌 姓名」完成綁定",
        )

    packages = []
    for _ in range(payload.quantity):
        package = Package(
            unit=binding.unit,
            line_user_id=line_user_id,
            status="pickup_now",
            package_count=1,
            task_type="return",
        )
        db.add(package)
        packages.append(package)
    db.commit()
    for package in packages:
        db.refresh(package)
        db.add(PackageRecipient(package_id=package.id, line_user_id=line_user_id, unit=binding.unit))
    db.commit()

    for package in packages:
        log_event(
            db, "return_requested",
            detail=f"住戶申請退貨，unit={binding.unit} quantity={payload.quantity}",
            package_id=package.id,
        )

    return {
        "status": "ok",
        "package_ids": [str(p.id) for p in packages],
        "count": len(packages),
    }


@app.get("/admin/packages/exceptions", dependencies=[Depends(require_admin_auth)])
async def admin_list_exceptions(db: Session = Depends(get_db)):
    """
    例外處理頁用：列出所有拒收/逾時/不收的包裹，
    附上收件人姓名、是否已在主畫面處理過（確認/關門）、是否已重新派送過。

    兩種情況這筆紀錄不會出現在清單裡（但packages表本身完全不動，主畫面不受影響）：
    - 管理員手動按過「銷案」（case_closed_at 有值）
    - 已經重新派送過，且新建立的那筆包裹狀態已經是completed（住戶已經拿到重新派送的包裹了，
      不需要再手動銷案，自動視為結案）
    """
    packages = (
        db.query(Package)
        .filter(Package.status.in_(EXCEPTION_STATUSES))
        .filter(Package.case_closed_at.is_(None))
        .order_by(Package.created_at.desc())
        .all()
    )

    result = []
    for p in packages:
        if p.redispatched_to is not None:
            new_package = db.query(Package).filter(Package.id == p.redispatched_to).first()
            if new_package and new_package.status == "completed":
                continue

        resolved = (p.acknowledged_at is not None) if p.status == "voided" else (p.door_closed_at is not None)
        needs_attention = (
            p.pending_pickup_notified_at is not None
            and now_taipei() - p.pending_pickup_notified_at >= timedelta(hours=72)
        )
        result.append({
            "id": str(p.id),
            "unit": p.unit,
            "status": p.status,
            "task_type": p.task_type,
            "door_id": p.door_id,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "resolved": resolved,
            "needs_attention": needs_attention,
            "redispatched_at": p.redispatched_at.isoformat() if p.redispatched_at else None,
            "redispatched_to": str(p.redispatched_to) if p.redispatched_to else None,
            "pending_pickup_notified_at": p.pending_pickup_notified_at.isoformat() if p.pending_pickup_notified_at else None,
            "recipients": get_recipients_with_names(db, str(p.id)),
        })
    return result


class CloseCasesRequest(BaseModel):
    package_ids: List[str]


@app.post("/admin/packages/close-case-batch", dependencies=[Depends(require_admin_auth)])
async def close_cases_batch(payload: CloseCasesRequest, db: Session = Depends(get_db)):
    """
    例外處理頁：勾選多筆後按「全部銷案」，管理員已經跟住戶確認這些包裹不需要
    再處理（不重新派送）。這只影響例外處理頁面之後還會不會顯示這些紀錄，
    不會動packages表裡的status，主畫面（/admin）看到的資料完全不受影響。
    每一筆各自檢查，不符合資格的記下原因、跳過不動，不會因為其中一筆不符合
    就讓整批都失敗。（原本還有一支單筆版本供「手動銷案」彈窗使用，該功能
    已移除，現在銷案只有這支批次版本。）
    """
    if not payload.package_ids:
        raise HTTPException(status_code=400, detail="沒有指定要銷案的包裹")

    closed = []
    skipped = []
    for pid in payload.package_ids:
        parsed = parse_package_uuid(pid)
        if parsed is None:
            skipped.append({"id": pid, "reason": "格式不正確"})
            continue

        package = db.query(Package).filter(Package.id == parsed).first()
        if not package:
            skipped.append({"id": pid, "reason": "找不到這筆包裹"})
            continue

        if package.status not in EXCEPTION_STATUSES:
            skipped.append({"id": pid, "reason": f"狀態是{package.status}，不是可以銷案的失敗/退回狀態"})
            continue

        resolved = (package.acknowledged_at is not None) if package.status == "voided" \
            else (package.door_closed_at is not None)
        if not resolved:
            skipped.append({"id": pid, "reason": "尚未在主畫面完成確認/關門"})
            continue

        if package.case_closed_at is not None:
            skipped.append({"id": pid, "reason": "已經銷案過了"})
            continue

        package.case_closed_at = now_taipei()
        log_event(db, "case_closed", package_id=package.id)
        closed.append(pid)

    db.commit()
    return {"status": "ok", "closed": closed, "skipped": skipped}


@app.post("/packages/{package_id}/redispatch", dependencies=[Depends(require_admin_auth)])
async def redispatch_package(package_id: str, db: Session = Depends(get_db)):
    """
    例外處理頁：針對已在主畫面處理完（確認/關門）的失敗包裹，
    建立一筆全新的包裹，沿用原本的門牌與收件人綁定，重新走一次到貨通知流程。
    原本這筆失敗的包裹保持不動，只記錄「已重新派送到哪一筆」，方便追蹤。
    """
    old_package = get_package_or_404(db, package_id)

    if old_package.status not in EXCEPTION_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"這筆包裹目前狀態是 {old_package.status}，不是可以重新派送的失敗/退回狀態",
        )

    resolved = (old_package.acknowledged_at is not None) if old_package.status == "voided" \
        else (old_package.door_closed_at is not None)
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="這筆包裹尚未在主畫面完成確認/關門，請先處理後再重新派送",
        )

    if old_package.redispatched_at is not None:
        raise HTTPException(status_code=400, detail="這筆包裹已經重新派送過了")

    old_recipients = db.query(PackageRecipient).filter(PackageRecipient.package_id == old_package.id).all()
    if not old_recipients:
        raise HTTPException(status_code=400, detail="找不到原本的收件人綁定資料，無法重新派送")

    new_package = Package(unit=old_package.unit, line_user_id=old_package.line_user_id, status="pending")
    db.add(new_package)
    db.commit()
    db.refresh(new_package)

    for recipient in old_recipients:
        db.add(PackageRecipient(
            package_id=new_package.id,
            line_user_id=recipient.line_user_id,
            unit=old_package.unit,
        ))
    db.commit()

    notify_failed = []
    for recipient in old_recipients:
        binding = db.query(LineBinding).filter(LineBinding.line_user_id == recipient.line_user_id).first()
        name = binding.name if binding else recipient.line_user_id
        try:
            push_arrival_notification(recipient.line_user_id, str(new_package.id), old_package.unit)
        except Exception as e:
            notify_failed.append(name)
            log_event(db, "notify_failed", detail=f"重新派送推播給 {name} 失敗: {e}",
                       package_id=new_package.id, level="error")

    old_package.redispatched_at = now_taipei()
    old_package.redispatched_to = new_package.id
    db.commit()

    log_event(db, "redispatched", detail=f"重新派送為新包裹 {new_package.id}", package_id=old_package.id)
    log_event(
        db, "created",
        detail=f"unit={old_package.unit} 重新派送自舊包裹 {old_package.id}，"
               f"notified_count={len(old_recipients) - len(notify_failed)}"
               + (f" 通知失敗: {', '.join(notify_failed)}" if notify_failed else ""),
        package_id=new_package.id,
    )

    return {
        "status": "ok",
        "old_package_id": str(old_package.id),
        "new_package_id": str(new_package.id),
        "notified_count": len(old_recipients) - len(notify_failed),
        "notify_failed": notify_failed,
    }


@app.post("/packages/{package_id}/notify-pending-pickup", dependencies=[Depends(require_admin_auth)])
async def notify_pending_pickup(package_id: str, db: Session = Depends(get_db)):
    """
    例外處理頁「通知住戶」按鈕：正常情況下，包裹一轉成拒收/逾時/不收就已經自動發送過這則
    提醒了（見 send_pending_pickup_notification），這支API保留下來當補發用——例如自動發送
    當下推播失敗，管理員可以在這裡手動再觸發一次。
    """
    package = get_package_or_404(db, package_id)

    if package.status not in EXCEPTION_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"這筆包裹目前狀態是 {package.status}，不是失敗/退回狀態，不需要這則通知",
        )

    if package.pending_pickup_notified_at is not None:
        raise HTTPException(status_code=400, detail="這筆包裹已經通知過住戶了，不需要重複通知")

    result = send_pending_pickup_notification(db, package)
    if not result["sent"]:
        detail = (
            "找不到這筆包裹的收件人，無法通知"
            if result["notify_failed_count"] == 0
            else "推播給所有收件人皆失敗，請確認LINE綁定狀態後再試"
        )
        raise HTTPException(status_code=400, detail=detail)

    return {
        "status": "ok",
        "package_id": str(package.id),
        "notified_count": result["notified_count"],
        "notify_failed_count": result["notify_failed_count"],
    }


@app.post("/packages/{package_id}/notify-completed-leftover", dependencies=[Depends(require_admin_auth)])
async def notify_completed_leftover(package_id: str, db: Session = Depends(get_db)):
    """
    Dashboard「手動聯繫住戶」：管理員發現「系統判定任務已完成，但懷疑機器人返回時
    艙門裡還留有沒被拿走的包裹」這種情況（例如一戶多件包裹，住戶只拿走一部分卻
    按了取貨完成），主動通知住戶3天內要聯繫管理室，逾期管理員會自行作廢處理，
    之後就是「等住戶聯繫」，不會再有系統自動化動作。

    這支刻意不改動package.status（維持completed），純粹是提醒通知。
    不限制只能發一次——管理員可能會需要在3天期限快到、住戶還沒回應時再提醒一次，
    所以每次呼叫都允許重新發送，且每次都會把pending_pickup_notified_at
    更新成最新的發送時間（等於重新起算3天期限），同時這個欄位本身就是
    「已經通知過住戶」的註記，選擇視窗那邊會依這個欄位顯示「已通知過」提示，
    讓管理員在重新發送前能看到上次是什麼時候通知的。
    """
    package = get_package_or_404(db, package_id)

    if package.status != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"這筆包裹目前狀態是 {package.status}，不是已完成的狀態",
        )

    recipients = get_recipients(db, package_id)
    if not recipients:
        raise HTTPException(status_code=400, detail="找不到這筆包裹的收件人，無法通知")

    now = now_taipei()
    deadline_text = (now + timedelta(hours=72)).strftime("%m月%d日%H時")
    message = (
        f"您先前完成取貨的包裹（門牌：{package.unit}），經管理員確認可能仍有部分包裹"
        f"留存於管理室，請盡快聯繫管理員確認。\n將於 {deadline_text} 由管理員作廢處理。"
    )

    notify_failed_count = 0
    for line_user_id in recipients:
        try:
            push_status_update(line_user_id, message)
        except Exception as e:
            notify_failed_count += 1
            log_event(db, "notify_failed", detail=f"已完成包裹的補通知失敗: {e}", package_id=package.id, level="error")

    notified_count = len(recipients) - notify_failed_count
    if notified_count == 0:
        raise HTTPException(status_code=400, detail="推播給所有收件人皆失敗，請確認LINE綁定狀態後再試")

    package.pending_pickup_notified_at = now
    db.commit()
    log_event(
        db, "pending_pickup_notified",
        detail=f"管理員手動聯繫住戶(已完成任務疑似有遺漏包裹)，通知{notified_count}/{len(recipients)}人",
        package_id=package.id,
    )

    return {"status": "ok", "package_id": str(package.id), "notified_count": notified_count}



LIFF_SCAN_HTML = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>掃描取貨</title>
  <script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
  <style>
    body { font-family: sans-serif; padding: 20px; text-align: center; }
    button { width: 100%; padding: 14px; font-size: 18px; background: #06C755; color: white; border: none; border-radius: 6px; margin-top: 20px; }
    #message { margin-top: 16px; font-weight: bold; }
  </style>
</head>
<body>
  <h2 id="pageTitle">掃描 QR Code</h2>
  <p id="pageDesc">對準機器人螢幕的 QR Code 掃描</p>
  <button id="scanBtn" onclick="startScan()">開啟相機掃描</button>
  <button id="completeBtn" style="display:none;" onclick="completePickup()">取貨完成</button>
  <div id="message"></div>

  <script>
    const LIFF_ID = "__LIFF_ID__";
    let doorTaskId = null;
    let taskType = "delivery";

    function getUrlParams() {
      const params = new URLSearchParams(window.location.search);
      return { doorTaskId: params.get("door_task_id"), taskType: params.get("task_type") || "delivery" };
    }

    async function initLiff() {
      await liff.init({ liffId: LIFF_ID });
      if (!liff.isLoggedIn()) {
        liff.login();
        return;
      }
      const params = getUrlParams();
      doorTaskId = params.doorTaskId;
      taskType = params.taskType;

      // 送貨/退貨兩種方向文案不同，掃碼跟完成呼叫的API完全一樣（沿用同一套door-tasks端點）
      if (taskType === "return") {
        document.getElementById("pageDesc").textContent = "對準機器人螢幕的 QR Code 掃描，成功後放入要退回的物品";
        document.getElementById("completeBtn").textContent = "放貨完成";
      }

      if (!doorTaskId) {
        document.getElementById("message").textContent = "缺少任務資訊，請從LINE通知重新進入";
      }
    }

    async function startScan() {
        const messageEl = document.getElementById("message");
        const btn = document.getElementById("scanBtn");
        btn.disabled = true;
        btn.textContent = "掃描中...";
        try {
            const result = await liff.scanCodeV2();
            const scannedContent = result.value;
            const idToken = liff.getIDToken();

            const response = await fetch(`/door-tasks/${doorTaskId}/pickup-complete`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ scanned_content: scannedContent, id_token: idToken }),
            });

            if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || "驗證失敗");
            }

            messageEl.style.color = "green";
            messageEl.textContent = taskType === "return"
              ? "艙門已開啟，放入物品後按上方按鈕關門"
              : "艙門已開啟，取出包裹後按上方按鈕關門";
            // 掃描成功、門已經開了（同一組任務底下有幾扇門就會一起開），不需要再掃第二次；
            // 改顯示「取貨完成／放貨完成」鍵，不再依賴LINE推播，住戶直接在這一頁按下去就會關門
            btn.style.display = "none";
            const completeBtn = document.getElementById("completeBtn");
            completeBtn.style.display = "block";
        } catch (e) {
            messageEl.style.color = "red";
            messageEl.textContent = "掃描失敗：" + e.message;
            // 失敗要讓使用者能重新掃，按鈕維持原本可點的「開啟相機掃描」
            btn.textContent = "開啟相機掃描";
            btn.disabled = false;
        }
        }

    async function completePickup() {
        const messageEl = document.getElementById("message");
        const btn = document.getElementById("completeBtn");
        const doneLabel = taskType === "return" ? "放貨完成" : "取貨完成";
        btn.disabled = true;
        btn.textContent = "處理中...";
        try {
            const response = await fetch(`/door-tasks/${doorTaskId}/complete`, {
            method: "POST",
            });

            if (!response.ok) {
            const err = await response.json();
            throw new Error(err.detail || `${doneLabel}失敗`);
            }

            messageEl.style.color = "green";
            messageEl.textContent = (taskType === "return" ? "放貨完成，艙門已關閉" : "取貨完成，艙門已關閉");
            btn.textContent = "已完成";
            btn.style.background = "#999";
        } catch (e) {
            messageEl.style.color = "red";
            messageEl.textContent = `${doneLabel}失敗：` + e.message + "，請重新整理再試，或聯繫管理員";
            // 失敗要讓使用者能重試，鎖住的按鈕解開
            btn.disabled = false;
            btn.textContent = doneLabel;
        }
        }

    initLiff();
  </script>
</body>
</html>
"""

LIFF_RETURN_REQUEST_HTML = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>申請退貨</title>
  <script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
  <style>
    body { font-family: sans-serif; padding: 20px; text-align: center; }
    h2 { color: #E2231A; }
    .qty-row { display: flex; align-items: center; justify-content: center; gap: 16px; margin: 24px 0; }
    .qty-row button { width: 48px; height: 48px; font-size: 22px; border-radius: 50%; border: none; background: #f0f0f0; color: #333; }
    #qtyDisplay { font-size: 28px; font-weight: bold; min-width: 40px; }
    #submitBtn { width: 100%; padding: 14px; font-size: 18px; background: #E2231A; color: white; border: none; border-radius: 6px; margin-top: 12px; }
    #submitBtn:disabled { background: #ccc; }
    #message { margin-top: 16px; font-weight: bold; }
    p.hint { color: #888; font-size: 13px; }
  </style>
</head>
<body>
  <h2>申請退貨</h2>
  <p>選擇件數後送出，機器人準備好會通知您掃碼放貨</p>
  <div class="qty-row">
    <button onclick="changeQty(-1)">－</button>
    <div id="qtyDisplay">1</div>
    <button onclick="changeQty(1)">＋</button>
  </div>
  <p class="hint">最多一次 4 件</p>
  <button id="submitBtn" onclick="submitRequest()">送出退貨申請</button>
  <div id="message"></div>

  <script>
    const LIFF_ID_RETURN = "__LIFF_ID_RETURN__";
    let quantity = 1;

    function changeQty(delta) {
      const next = quantity + delta;
      if (next < 1 || next > 4) return;
      quantity = next;
      document.getElementById("qtyDisplay").textContent = quantity;
    }

    async function initLiff() {
      await liff.init({ liffId: LIFF_ID_RETURN });
      if (!liff.isLoggedIn()) {
        liff.login();
      }
    }

    async function submitRequest() {
      const messageEl = document.getElementById("message");
      const btn = document.getElementById("submitBtn");
      btn.disabled = true;
      btn.textContent = "送出中...";
      try {
        const idToken = liff.getIDToken();
        const response = await fetch("/liff/return-request/submit", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id_token: idToken, quantity: quantity }),
        });

        if (!response.ok) {
          const err = await response.json();
          throw new Error(err.detail || "申請失敗");
        }

        messageEl.style.color = "green";
        messageEl.textContent = "已送出退貨申請，機器人準備好後會傳LINE通知您";
        btn.textContent = "已送出";
      } catch (e) {
        messageEl.style.color = "red";
        messageEl.textContent = "申請失敗：" + e.message;
        btn.disabled = false;
        btn.textContent = "送出退貨申請";
      }
    }

    initLiff();
  </script>
</body>
</html>
"""

@app.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin_auth)])
async def admin_dashboard_page():
    html = ADMIN_DASHBOARD_HTML.replace("__ROBOT_HOME_POINT_NAME__", settings.ROBOT_HOME_POINT_NAME)
    return HTMLResponse(content=html)


ADMIN_DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>FlashBot Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: #f5f5f5; margin: 0; padding: 20px; color: #222; zoom: 1.15; }
  h1 { color: #E2231A; font-size: 22px; margin-bottom: 20px; }
  h2 { font-size: 16px; margin: 0 0 12px 0; color: #333; }
  .card { background: white; border-radius: 8px; padding: 16px; margin-bottom: 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
  select, button { padding: 8px 12px; font-size: 14px; border-radius: 6px;
    border: 1px solid #ccc; margin-right: 8px; margin-bottom: 8px; }
  button { background: #E2231A; color: white; border: none; cursor: pointer; }
  button:hover { background: #c41c14; }
  button.secondary { background: white; color: #E2231A; border: 1px solid #E2231A; }
  button.secondary:hover { background: #e9e9e9; }
  button.danger { background: #E2231A; color: white; border: 1px solid #E2231A; font-weight: bold; }
  button.danger:hover { background: #c41c14; }
  button:disabled { opacity: 0.6; cursor: default; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; }
  th { color: #888; font-weight: normal; }
  .status-badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; background: #eee; }
  .status-pickup_now { background: #fff3cd; color: #856404; }
  .status-delivering { background: #cce5ff; color: #004085; }
  .status-arrived { background: #d4edda; color: #155724; }
  .status-completed { background: #e2e3e5; color: #383d41; }
  .status-voided { background: #f8d7da; color: #721c24; }
  .status-rejected_at_door { background: #dc3545; color: white; font-weight: bold; }
  .status-returned_timeout { background: #dc3545; color: white; font-weight: bold; }
  .reject-alert { background: #dc3545; color: white; border-radius: 8px; padding: 14px 16px;
    margin-bottom: 20px; font-size: 14px; box-shadow: 0 1px 4px rgba(0,0,0,0.2); }
  .reject-alert b { display: block; font-size: 15px; margin-bottom: 6px; }
  .reject-alert ul { margin: 0; padding-left: 20px; }
  .reject-alert table { width: 100%; border-collapse: collapse; margin-top: 8px; }
  .reject-alert th, .reject-alert td { text-align: left; padding: 6px 10px; font-size: 14px;
    border-bottom: 1px solid rgba(255,255,255,0.35); }
  .reject-alert th { font-weight: normal; opacity: 0.85; }
  #createMsg { margin-top: 8px; font-size: 14px; }
  .robot-info { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 16px; }
  .robot-info div { font-size: 14px; text-align: center; }
  .robot-info b { display: block; color: #888; font-size: 12px; font-weight: normal; }
  .robot-status-header { display: flex; align-items: center; flex-wrap: wrap; gap: 20px; margin-bottom: 16px; }
  .robot-status-header h2 { margin: 0; white-space: nowrap; }
  .robot-status-header .robot-info { flex: 1; margin-bottom: 0; justify-content: space-evenly; }
  .robot-status-header button { margin-left: auto; margin-right: 0; margin-bottom: 0; flex-shrink: 0; }
  .card-header { display: flex; align-items: center; flex-wrap: wrap; gap: 20px; margin-bottom: 12px; }
  .card-header h2 { margin: 0; white-space: nowrap; }
  .card-header button { margin-left: auto; margin-right: 0; margin-bottom: 0; flex-shrink: 0; }
  .create-package-row { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
  .create-package-selects { display: flex; flex: 1; align-items: center;
    padding: 0 16px; flex-wrap: nowrap; gap: 16px; min-width: 0; }
  .create-package-selects select { flex: 1 1 0; min-width: 0; width: auto; }
  .create-package-row button { flex-shrink: 0; margin-left: 0; margin-right: 0; }
  .doors { display: flex; gap: 12px; border-top: 0.5px solid #eee; padding-top: 16px; }
  .door-box { flex: 1; padding: 12px; border-radius: 6px; text-align: center; font-size: 13px; }
  .door-EMPTY { background: #e9ecef; color: #666; }
  .door-ASSIGNED { background: #fff3cd; color: #856404; }
  .door-FULL { background: #f8d7da; color: #721c24; }
  .pkg-select-checkbox, #selectAllCheckbox {
    appearance: none; -webkit-appearance: none;
    width: 22px; height: 22px; border: 2px solid #ccc; border-radius: 7px;
    cursor: pointer; position: relative; vertical-align: middle; margin: 0;
  }
  .pkg-select-checkbox:checked, #selectAllCheckbox:checked {
    background: #E2231A; border-color: #E2231A;
  }
  .pkg-select-checkbox:checked::after, #selectAllCheckbox:checked::after {
    content: ''; position: absolute; left: 7px; top: 3px;
    width: 5px; height: 10px; border: solid white; border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }
  tr.selectable-row { cursor: pointer; }
  tr.selectable-row:hover { background: #fff5f5; }
</style>
</head>
<body>

<h1 style="display:flex;align-items:center;flex-wrap:wrap;gap:12px;">
  <span>FlashBot Dashboard</span>
  <a href="/admin/reports" style="font-size:14px;font-weight:normal;color:#E2231A;">查看每日報表 →</a>
  <a href="/admin/exceptions" style="font-size:14px;font-weight:normal;color:#E2231A;">退回/作廢包裹處理 →</a>
  <a href="/admin/residents" style="font-size:14px;font-weight:normal;color:#E2231A;">住戶綁定管理 →</a>
  <button class="secondary" style="margin-left:auto;" onclick="withButtonFeedback(this, refreshAll)">重新整理</button>
</h1>

<div class="card">
  <h2>建立包裹</h2>
  <div class="create-package-row">
    <div class="create-package-selects">
      <select id="unitSelect"><option value="">請選擇門牌</option></select>
      <select id="nameSelect"><option value="">請先選擇門牌</option></select>
      <select id="qtySelect">
        <option value="1">1件</option>
        <option value="2">2件</option>
        <option value="3">3件</option>
        <option value="4">4件</option>
      </select>
    </div>
    <button id="createBtn" onclick="createPackage()">建立包裹並通知</button>
  </div>
  <div id="createMsg"></div>
</div>

<div class="card">
  <div class="robot-status-header">
    <h2>機器人狀態</h2>
    <div id="robotInfo" class="robot-info">載入中...</div>
    <div style="margin-left:auto;display:flex;gap:10px;align-items:center;">
      <button class="secondary" style="margin-left:0;" onclick="robotRecall(this)" title="強制中斷機器人目前的任務並返回管理室">返回管理室</button>
      <button class="secondary" style="margin-left:0;" onclick="robotRecharge(this)">機器人充電</button>
    </div>
  </div>
  <div style="border-top:1px solid #eee;margin-bottom:10px;padding-top:16px;display:flex;gap:12px;align-items:center;">
    <div id="doorInfo" class="doors" style="flex:1;border-top:none;padding-top:0;"></div>
    <div style="display:flex;gap:10px;align-items:center;flex-shrink:0;">
      <button id="manualOpenBtn" class="secondary" style="margin-left:0;" onclick="manualOpenDoors(this)" title="打開所有艙門">開啟艙門</button>
      <button id="manualCloseBtn" class="secondary" style="margin-left:0;" onclick="manualCloseDoors(this)" title="請確認所有艙門皆空再關閉艙門">關閉艙門</button>
    </div>
  </div>
</div>

<div id="rejectAlert" class="reject-alert" style="display:none;"></div>
<div id="returnPendingAlert" class="reject-alert" style="display:none;background:#004085;"></div>

<div class="card">
  <div class="card-header">
    <h2>包裹清單</h2>
    <div style="display:flex;gap:8px;align-items:center;">
      <input type="text" id="unitQueryInput" placeholder="輸入門牌查詢" style="width:200px;height:36px;padding:0 10px;border-radius:6px;border:1px solid #ccc;font-size:14px;box-sizing:border-box;" />
      <button id="unitQueryBtn" style="margin-left:0;" onclick="queryByUnit()">查詢</button>
      <button id="unitQueryClearBtn" class="secondary" style="margin-left:0;" onclick="clearUnitQuery()">清除</button>
      <button class="secondary" style="margin-left:16px;" onclick="openManualContactModal()" title="通知住戶：已完成的任務可能仍有包裹留在艙門裡沒被拿走">手動聯繫住戶</button>
    </div>
    <div style="margin-left:auto;display:flex;gap:10px;align-items:center;">
      <button id="dispatchBatchBtn" style="margin-left:0;" onclick="dispatchBatch()">全部派送（<span id="pendingDispatchCount">0</span>）</button>
    </div>
  </div>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;">
    <button id="selectModeBtn" class="secondary" style="margin-left:0;" onclick="toggleSelectMode()">選取</button>
    <button id="deleteSelectedBtn" style="display:none;background:#dc3545;margin-left:0;" onclick="deleteSelectedPackages()" disabled>刪除已選（0）</button>
    <label style="font-size:13px;color:#888;margin-left:8px;">建立時間：</label>
    <input type="date" id="packageDateFrom" style="height:36px;padding:0 8px;border-radius:6px;border:1px solid #ccc;font-size:14px;box-sizing:border-box;" />
    <span style="color:#888;">至</span>
    <input type="date" id="packageDateTo" style="height:36px;padding:0 8px;border-radius:6px;border:1px solid #ccc;font-size:14px;box-sizing:border-box;" />
    <button id="dateFilterBtn" style="margin-left:0;" onclick="applyDateFilter()">套用</button>
    <button id="dateFilterClearBtn" class="secondary" style="margin-left:0;" onclick="clearDateFilter()">清除日期</button>
    <span id="dateFilterInfo" style="font-size:13px;color:#888;"></span>
    <span id="pendingRequestHint" style="margin-left:auto;font-size:13px;color:#E2231A;font-weight:bold;">目前有 0 筆任務尚未處理派送，請盡速處理</span>
  </div>
  <div id="unitQueryResult" style="margin-bottom:12px;"></div>
  <table>
    <thead><tr>
      <th id="selectColHeader" style="display:none;width:30px;"><input type="checkbox" id="selectAllCheckbox" onchange="toggleSelectAll(this)" /></th>
      <th>門牌</th><th>狀態</th><th>艙門</th><th>建立時間</th><th>預約時間</th><th>操作</th>
    </tr></thead>
    <tbody id="packageTableBody"><tr><td colspan="6">載入中...</td></tr></tbody>
  </table>
  <div style="display:flex;align-items:center;justify-content:flex-end;gap:16px;margin-top:10px;font-size:13px;color:#888;">
    <span id="packagePagerInfo"></span>
    <a id="packagePrevBtn" href="javascript:void(0)" onclick="prevPackagePage()" style="font-size:14px;color:#E2231A;cursor:pointer;">← 上一頁</a>
    <a id="packageNextBtn" href="javascript:void(0)" onclick="nextPackagePage()" style="font-size:14px;color:#E2231A;cursor:pointer;">下一頁 →</a>
  </div>
</div>

<div id="manualContactOverlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:100;align-items:center;justify-content:center;">
  <div style="background:white;border-radius:10px;padding:24px;width:420px;max-width:90vw;">
    <h3 style="margin:0 0 4px 0;">手動聯繫住戶</h3>
    <p style="font-size:13px;color:#888;margin:0 0 16px 0;">
      用於「系統判定任務已完成，但懷疑艙門裡還留有包裹沒被拿走」的情況，
      通知住戶3天內聯繫管理室，逾期將由管理員作廢。
    </p>
    <label style="font-size:13px;color:#888;display:block;margin-bottom:4px;">門牌</label>
    <select id="manualContactUnitSelect" style="width:100%;margin-bottom:12px;" onchange="updateManualContactPackageOptions()">
      <option value="">請選擇門牌</option>
    </select>
    <label style="font-size:13px;color:#888;display:block;margin-bottom:4px;">包裹任務（僅列出已完成的任務）</label>
    <select id="manualContactPackageSelect" style="width:100%;margin-bottom:16px;" disabled>
      <option value="">請先選擇門牌</option>
    </select>
    <div id="manualContactMsg" style="font-size:13px;margin-bottom:8px;"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;">
      <button class="secondary" style="margin:0;" onclick="closeManualContactModal()">取消</button>
      <button id="manualContactSendBtn" style="margin:0;" onclick="sendManualContactNotice()">發送3天作廢通知</button>
    </div>
  </div>
</div>

<script>
let bindingsData = [];
let packagesById = {};

async function withButtonFeedback(button, fn) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = '更新中...';
  try {
    await fn();
  } finally {
    button.textContent = originalText;
    button.disabled = false;
  }
}

async function loadBindings() {
  const resp = await fetch('/admin/bindings');
  bindingsData = await resp.json();
  const units = [...new Set(bindingsData.map(b => b.unit))];
  const unitSelect = document.getElementById('unitSelect');
  unitSelect.innerHTML = '<option value="">請選擇門牌</option>' +
    units.map(u => `<option value="${u}">${u}</option>`).join('');

  // 手動聯繫住戶modal的門牌下拉選單，用同一份門牌清單，不用另外打API
  const contactUnitSelect = document.getElementById('manualContactUnitSelect');
  if (contactUnitSelect) {
    contactUnitSelect.innerHTML = '<option value="">請選擇門牌</option>' +
      units.map(u => `<option value="${u}">${u}</option>`).join('');
  }
}

function openManualContactModal() {
  document.getElementById('manualContactOverlay').style.display = 'flex';
  document.getElementById('manualContactUnitSelect').value = '';
  document.getElementById('manualContactPackageSelect').innerHTML = '<option value="">請先選擇門牌</option>';
  document.getElementById('manualContactPackageSelect').disabled = true;
  document.getElementById('manualContactMsg').textContent = '';
}

function closeManualContactModal() {
  document.getElementById('manualContactOverlay').style.display = 'none';
}

async function updateManualContactPackageOptions() {
  const unit = document.getElementById('manualContactUnitSelect').value;
  const packageSelect = document.getElementById('manualContactPackageSelect');
  const msgEl = document.getElementById('manualContactMsg');
  msgEl.textContent = '';

  if (!unit) {
    packageSelect.innerHTML = '<option value="">請先選擇門牌</option>';
    packageSelect.disabled = true;
    return;
  }

  packageSelect.innerHTML = '<option value="">載入中...</option>';
  packageSelect.disabled = true;
  try {
    const resp = await fetch(`/admin/packages/by-unit?unit=${encodeURIComponent(unit)}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '查詢失敗');

    // 這個功能是給「已完成、但懷疑有包裹留在艙門裡」的情況用的，只列出已完成的任務
    const completedPackages = data.filter(p => p.raw_status === 'completed');
    if (completedPackages.length === 0) {
      packageSelect.innerHTML = '<option value="">這個門牌沒有已完成的包裹任務</option>';
      packageSelect.disabled = true;
      return;
    }
    packageSelect.innerHTML = '<option value="">請選擇包裹任務</option>' +
      completedPackages.map(p => {
        const createdAt = p.created_at ? p.created_at.replace('T', ' ').slice(0, 16) : '';
        const notifiedTag = p.pending_pickup_notified_at
          ? `（已通知過 ${p.pending_pickup_notified_at.replace('T', ' ').slice(0, 16)}）`
          : '';
        return `<option value="${p.id}" data-notified="${p.pending_pickup_notified_at || ''}">${createdAt}（收件人：${p.recipient_name}）${notifiedTag}</option>`;
      }).join('');
    packageSelect.disabled = false;
  } catch (e) {
    packageSelect.innerHTML = '<option value="">載入失敗</option>';
    msgEl.style.color = 'red';
    msgEl.textContent = '查詢失敗：' + e.message;
  }
}

async function sendManualContactNotice() {
  const packageSelect = document.getElementById('manualContactPackageSelect');
  const packageId = packageSelect.value;
  const msgEl = document.getElementById('manualContactMsg');
  if (!packageId) {
    msgEl.style.color = 'red';
    msgEl.textContent = '請選擇要通知的包裹任務';
    return;
  }

  const selectedOption = packageSelect.options[packageSelect.selectedIndex];
  const alreadyNotified = selectedOption.dataset.notified;
  const confirmText = alreadyNotified
    ? `這筆包裹先前已經通知過（${alreadyNotified.replace('T', ' ').slice(0, 16)}），確定要重新發送一次通知（3天期限會重新起算）嗎？`
    : '確定要發送「3天內未聯繫將作廢」的通知給這筆包裹的收件人嗎？';
  if (!confirm(confirmText)) return;

  const btn = document.getElementById('manualContactSendBtn');
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '發送中...';
  try {
    const resp = await fetch(`/packages/${packageId}/notify-completed-leftover`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '發送失敗');
    msgEl.style.color = 'green';
    msgEl.textContent = `已通知 ${data.notified_count} 位收件人`;
    setTimeout(closeManualContactModal, 1200);
  } catch (e) {
    msgEl.style.color = 'red';
    msgEl.textContent = '發送失敗：' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function updateNameOptions() {
  const unit = document.getElementById('unitSelect').value;
  const nameSelect = document.getElementById('nameSelect');
  const names = bindingsData.filter(b => b.unit === unit);
  nameSelect.innerHTML = '<option value="">請選擇收件人</option>' +
    names.map(b => `<option value="${b.name}">${b.name}</option>`).join('');
}

document.getElementById('unitSelect').addEventListener('change', updateNameOptions);

async function createPackage() {
  const unit = document.getElementById('unitSelect').value;
  const recipient_name = document.getElementById('nameSelect').value;
  const quantity = parseInt(document.getElementById('qtySelect').value, 10);
  const msgEl = document.getElementById('createMsg');
  const btn = document.getElementById('createBtn');
  if (!unit) { msgEl.style.color = 'red'; msgEl.textContent = '請先選擇門牌'; return; }
  if (!recipient_name) { msgEl.style.color = 'red'; msgEl.textContent = '請選擇收件人'; return; }

  btn.disabled = true;
  btn.textContent = '建立中...';
  try {
    const resp = await fetch('/packages', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unit, recipient_name: recipient_name || null, quantity }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '建立失敗');

    if (data.notify_failed && data.notify_failed.length > 0) {
      msgEl.style.color = '#b58105';
      msgEl.textContent = `建立成功，已通知 ${data.notified_count} 位住戶，但 ${data.notify_failed.join('、')} 通知失敗（請確認LINE綁定是否正常）`;
    } else {
      msgEl.style.color = 'green';
      msgEl.textContent = `建立成功，已通知 ${data.notified_count} 位住戶`;
    }

    document.getElementById('qtySelect').value = '1';   // ← 成功後重置件數
    loadPackages();
  } catch (e) {
    msgEl.style.color = 'red';
    msgEl.textContent = '錯誤：' + e.message;
    // 失敗不重置件數，維持原本選的值方便重試
  } finally {
    btn.disabled = false;
    btn.textContent = '建立包裹並通知';
  }
}

const STATUS_LABEL = {
  pending: '待處理', pickup_now: '待派送',
  delivering: '配送中', arrived: '已抵達', completed: '已完成',
  returned_timeout: '逾時（作廢）',
  voided: '不收（作廢）', rejected_at_door: '拒收（作廢）',
};

const PACKAGES_PER_PAGE = 50;
let currentPackagePage = 1;
let packagePageTotal = 0;
let activeDateFrom = '';
let activeDateTo = '';
let selectMode = false;
let selectedPackageIds = new Set();
let latestDoorStates = [];  // 最近一次機器人回報的艙門狀態，供放置包裹的艙門下拉選單使用
const ROBOT_HOME_POINT_NAME = "__ROBOT_HOME_POINT_NAME__";  // 機器人「在管理室」對應的位置名稱，開關艙門要先確認機器人在這裡

async function refreshAll() {
  // 整頁唯一的重新整理鍵：一次刷新機器人狀態、包裹清單/異常提示框、
  // 建立包裹表單的門牌/收件人下拉選單，取代原本分散在各卡片裡各自的重新整理鍵。
  await Promise.all([loadPackages(), loadRobotStatus(), loadBindings()]);
}

async function loadPackages() {
  // 一併刷新機器人狀態：包裹清單每次重新渲染都會重新畫艙門下拉選單，
  // 如果latestDoorStates沒有跟著同步更新，選單會一直用上一次不知道多久前
  // 的舊艙門狀態判斷可不可選，跟實際包裹清單的新舊對不起來。
  await Promise.all([loadLivePackages(), loadPackageTablePage(), loadRobotStatus()]);
}

async function loadLivePackages() {
  let livePackages;
  try {
    const resp = await fetch('/admin/packages/live');
    livePackages = await resp.json();
    if (!resp.ok) throw new Error(livePackages.detail || '未知錯誤');
  } catch (e) {
    document.getElementById('rejectAlert').style.display = 'block';
    document.getElementById('rejectAlert').innerHTML =
      `<b>機器人狀態/待處理清單載入失敗</b><div style="margin-top:6px;">${e.message}</div>`;
    return;
  }
  for (const p of livePackages) {
    packagesById[p.id] = p;
  }
  renderRejectAlert(livePackages);
  renderReturnPendingAlert(livePackages);
  renderDispatchBatchButton(livePackages);
  updateManualDoorButtonState(livePackages);
  updatePendingRequestHint(livePackages);
}

async function loadPackageTablePage() {
  const params = new URLSearchParams({ page: currentPackagePage, page_size: PACKAGES_PER_PAGE });
  if (activeDateFrom) params.set('date_from', activeDateFrom);
  if (activeDateTo) params.set('date_to', activeDateTo);

  let resp, data;
  try {
    resp = await fetch(`/admin/packages?${params.toString()}`);
    data = await resp.json();
  } catch (e) {
    // fetch本身失敗，或後端回傳的不是合法JSON（例如500的原始錯誤文字）
    document.getElementById('packageTableBody').innerHTML =
      `<tr><td colspan="${selectMode ? 7 : 6}" style="color:red">載入失敗：${e.message}</td></tr>`;
    return;
  }
  if (!resp.ok) {
    document.getElementById('packageTableBody').innerHTML =
      `<tr><td colspan="${selectMode ? 7 : 6}" style="color:red">載入失敗：${data.detail || '未知錯誤'}</td></tr>`;
    return;
  }
  packagePageTotal = data.total;

  const totalPages = Math.max(1, Math.ceil(packagePageTotal / PACKAGES_PER_PAGE));
  if (currentPackagePage > totalPages) {
    // 頁碼超出範圍（例如原本在看最後一頁,資料變少了),退回正確的最後一頁重新抓
    currentPackagePage = totalPages;
    return loadPackageTablePage();
  }

  for (const p of data.items) {
    packagesById[p.id] = p;
  }
  renderPackageTable(data.items, totalPages);
}

function applyDateFilter() {
  const from = document.getElementById('packageDateFrom').value;
  const to = document.getElementById('packageDateTo').value;
  const infoEl = document.getElementById('dateFilterInfo');

  if (from && to && from > to) {
    alert('起始日期不能晚於結束日期');
    return;
  }

  activeDateFrom = from;
  activeDateTo = to;
  currentPackagePage = 1;

  if (from || to) {
    infoEl.textContent = `篩選：${from || '最早'} 至 ${to || '最新'}`;
  } else {
    infoEl.textContent = '';
  }

  loadPackageTablePage();
}

function clearDateFilter() {
  document.getElementById('packageDateFrom').value = '';
  document.getElementById('packageDateTo').value = '';
  document.getElementById('dateFilterInfo').textContent = '';
  activeDateFrom = '';
  activeDateTo = '';
  currentPackagePage = 1;
  loadPackageTablePage();
}

function renderPackageTable(pageItems, totalPages) {
  const tbody = document.getElementById('packageTableBody');
  const infoEl = document.getElementById('packagePagerInfo');
  const prevBtn = document.getElementById('packagePrevBtn');
  const nextBtn = document.getElementById('packageNextBtn');

  const colCount = selectMode ? 7 : 6;

  if (packagePageTotal === 0) {
    tbody.innerHTML = `<tr><td colspan="${colCount}">目前沒有包裹</td></tr>`;
    infoEl.textContent = '';
    setPackagePagerLinkState(prevBtn, true);
    setPackagePagerLinkState(nextBtn, true);
    return;
  }

  const now = new Date();

  tbody.innerHTML = pageItems.map(p => {
    const checkboxCell = selectMode
      ? `<td><input type="checkbox" class="pkg-select-checkbox" data-id="${p.id}" ${selectedPackageIds.has(p.id) ? 'checked' : ''} onchange="togglePackageSelect('${p.id}', this.checked)" /></td>`
      : '';
    const label = (p.status === 'rejected_at_door' && p.task_type === 'return')
      ? '已取消退貨'
      : (STATUS_LABEL[p.status] || p.status);
    const createdAt = p.created_at ? p.created_at.replace('T', ' ').slice(0, 16) : '-';
    const door = p.door_id || '尚未分配';

    // 預約時間到了、但還沒放置（沒有door_id）：這是需要管理員動作的時刻，
    // 用醒目的橘色底色+文字提醒，跟一般預約中（時間還沒到，純文字顯示）做區隔
    let scheduledCell = '-';
    let rowStyle = '';
    if (p.scheduled_pickup_at) {
      const scheduledDate = new Date(p.scheduled_pickup_at);
      const scheduledText = p.scheduled_pickup_at.replace('T', ' ').slice(0, 16);
      const timeArrived = scheduledDate <= now;
      if (timeArrived && !p.door_id && p.status === 'pickup_now') {
        scheduledCell = `<span style="background:#ff9800;color:white;padding:2px 8px;border-radius:10px;font-size:12px;font-weight:bold;">預約時間已到 ${scheduledText}</span>`;
        rowStyle = 'background:#fff3e0;';
      } else {
        scheduledCell = scheduledText;
      }
    }

    // 拒收/逾時/不收這幾個狀態的操作按鈕，已經統一在上面的紅色提示框處理了，
    // 這裡不再重複放按鈕，避免同一筆包裹在畫面上出現兩個功能一樣的按鈕。
    // pickup_now分三種：還沒放置（要按「放置包裹」呼叫機器人開門）、已放置
    // （只顯示「釋放」，一定要先釋放才能重新選門——見releaseDoor）、等批次派送
    let action;
    if (p.status === 'pickup_now') {
      if (p.door_id) {
        action = `<button class="secondary" onclick="releaseDoor(this, '${p.id}')" title="呼叫機器人釋放這扇艙門，釋放後才能重新選擇">釋放</button>`;
      } else if (p.scheduled_pickup_at && new Date(p.scheduled_pickup_at) > now) {
        action = `<span style="opacity:0.6;">預約中，未到時間</span>`;
      } else {
        action = buildDoorPlacementSelect(p);
      }
    } else {
      action = '-';
    }

    const taskTypeBadge = p.task_type === 'return'
      ? `<span style="background:#004085;color:white;padding:1px 6px;border-radius:8px;font-size:11px;margin-left:4px;">退貨</span>`
      : '';
    const unitCell = (p.package_count > 1
      ? `${p.unit} <span style="background:#e3f2fd;color:#0d47a1;padding:1px 6px;border-radius:8px;font-size:11px;">${p.package_count}件</span>`
      : p.unit) + taskTypeBadge;

    const rowClass = selectMode ? 'selectable-row' : '';
    const rowClick = selectMode ? ` onclick="handleRowClick(event, '${p.id}')"` : '';

    return `<tr class="${rowClass}" style="${rowStyle}"${rowClick}>
      ${checkboxCell}
      <td>${unitCell}</td>
      <td><span class="status-badge status-${p.status}">${label}</span></td>
      <td>${door}</td><td>${createdAt}</td><td>${scheduledCell}</td><td>${action}</td>
    </tr>`;
  }).join('');

  infoEl.textContent = `共 ${packagePageTotal} 筆，第 ${currentPackagePage} / 共 ${totalPages} 頁`;
  setPackagePagerLinkState(prevBtn, currentPackagePage === 1);
  setPackagePagerLinkState(nextBtn, currentPackagePage === totalPages);
}

function toggleSelectMode() {
  if (selectMode) {
    exitSelectMode();
    loadPackageTablePage();
    return;
  }
  selectMode = true;
  document.getElementById('selectColHeader').style.display = 'table-cell';
  document.getElementById('selectModeBtn').textContent = '取消選取';
  document.getElementById('deleteSelectedBtn').style.display = 'inline-block';
  updateDeleteButtonState();
  loadPackageTablePage();
}

function exitSelectMode() {
  // 只重設選取狀態本身，不在這裡重新抓資料——呼叫端（取消選取按鈕／刪除完成後）
  // 各自決定要不要重抓，避免同一次操作重複打兩次API
  selectMode = false;
  selectedPackageIds.clear();
  document.getElementById('selectColHeader').style.display = 'none';
  document.getElementById('selectModeBtn').textContent = '選取';
  document.getElementById('deleteSelectedBtn').style.display = 'none';
  updateDeleteButtonState();
}

function handleRowClick(event, id) {
  // 點到checkbox、按鈕這些互動元件本身，交給它們各自的onclick/onchange處理，
  // 這裡不要重複觸發，不然點「放置包裹」會變成同時觸發放置又切換選取
  if (event.target.closest('input, button, a')) return;
  const checkbox = document.querySelector(`.pkg-select-checkbox[data-id="${id}"]`);
  if (!checkbox) return;
  checkbox.checked = !checkbox.checked;
  togglePackageSelect(id, checkbox.checked);
}

function togglePackageSelect(id, checked) {
  if (checked) {
    selectedPackageIds.add(id);
  } else {
    selectedPackageIds.delete(id);
  }
  updateDeleteButtonState();
}

function toggleSelectAll(checkbox) {
  document.querySelectorAll('.pkg-select-checkbox').forEach(cb => {
    cb.checked = checkbox.checked;
    if (checkbox.checked) {
      selectedPackageIds.add(cb.dataset.id);
    } else {
      selectedPackageIds.delete(cb.dataset.id);
    }
  });
  updateDeleteButtonState();
}

function updateDeleteButtonState() {
  const btn = document.getElementById('deleteSelectedBtn');
  const count = selectedPackageIds.size;
  btn.textContent = `刪除已選（${count}）`;
  btn.disabled = count === 0;
}

async function deleteSelectedPackages() {
  const ids = Array.from(selectedPackageIds);
  if (ids.length === 0) return;
  if (!confirm(`確定要刪除選取的 ${ids.length} 筆包裹紀錄嗎？此動作會直接從資料庫移除，無法復原。`)) return;

  const btn = document.getElementById('deleteSelectedBtn');
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '刪除中...';
  try {
    const resp = await fetch('/admin/packages/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ package_ids: ids }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '刪除失敗');

    if (data.skipped && data.skipped.length > 0) {
      const reasons = data.skipped.map(s => `${s.id.slice(0, 8)}...：${s.reason}`).join('\\n');
      alert(`已刪除 ${data.deleted.length} 筆，${data.skipped.length} 筆無法刪除：\n${reasons}`);
    } else {
      alert(`已刪除 ${data.deleted.length} 筆包裹紀錄`);
    }
    selectedPackageIds.clear();
    exitSelectMode();
    loadPackages();
  } catch (e) {
    alert('刪除失敗：' + e.message);
  } finally {
    updateDeleteButtonState();
  }
}

function setPackagePagerLinkState(el, disabled) {
  if (disabled) {
    el.style.color = '#ccc';
    el.style.pointerEvents = 'none';
    el.style.cursor = 'default';
  } else {
    el.style.color = '#E2231A';
    el.style.pointerEvents = 'auto';
    el.style.cursor = 'pointer';
  }
}

function prevPackagePage() {
  if (currentPackagePage > 1) {
    currentPackagePage -= 1;
    loadPackageTablePage();
  }
}

function nextPackagePage() {
  const totalPages = Math.max(1, Math.ceil(packagePageTotal / PACKAGES_PER_PAGE));
  if (currentPackagePage < totalPages) {
    currentPackagePage += 1;
    loadPackageTablePage();
  }
}

function renderDispatchBatchButton(packages) {
  const btn = document.getElementById('dispatchBatchBtn');
  const readyCount = packages.filter(p => p.status === 'pickup_now' && p.door_id).length;
  btn.innerHTML = `全部派送（<span id="pendingDispatchCount">${readyCount}</span>）`;
  btn.disabled = readyCount === 0;
}

function updatePendingRequestHint(packages) {
  // 常態提示：住戶按了取貨之後，不管管理員有沒有放置包裹、有沒有按全部派送，
  // 只要狀態還是pickup_now，就代表這個請求還沒真正被送出去，提醒管理員盡速處理。
  // 跟「全部派送(N)」按鈕的計數不同——那個只算「已經放置、等派送」的，
  // 這裡算全部還卡著的請求（含還沒放置的）。
  // 一律顯示，沒有待處理的也顯示「0件」，不隱藏這個提示。
  const hintEl = document.getElementById('pendingRequestHint');
  const pendingCount = packages.filter(p => p.status === 'pickup_now').length;
  hintEl.textContent = `目前有 ${pendingCount} 筆任務尚未處理派送，請盡速處理`;
  hintEl.style.display = 'inline';
}

function updateManualDoorButtonState(packages) {
  // 機器人狀態欄的開/關門鍵：平常白色(secondary)，只要有拒收/逾時退回、
  // 或退貨已放貨完成但還沒確認取出的包裹，就自動變紅色(danger)提醒管理員該去操作了。
  const openBtn = document.getElementById('manualOpenBtn');
  const closeBtn = document.getElementById('manualCloseBtn');
  if (!openBtn || !closeBtn) return;

  const returnPackages = packages.filter(p =>
    (p.status === 'rejected_at_door' || p.status === 'returned_timeout') && !p.door_closed_at
  );
  const pendingReturnItems = packages.filter(p =>
    p.task_type === 'return' && p.status === 'completed' && !p.return_retrieved_at
  );
  const needsOpen = returnPackages.some(p => p.returned_at && !p.return_door_opened_at) || pendingReturnItems.length > 0;
  const needsClose = returnPackages.some(p => p.return_door_opened_at && !p.door_closed_at) || pendingReturnItems.length > 0;

  openBtn.classList.toggle('danger', needsOpen);
  openBtn.classList.toggle('secondary', !needsOpen);
  closeBtn.classList.toggle('danger', needsClose);
  closeBtn.classList.toggle('secondary', !needsClose);
}

function renderRejectAlert(packages) {
  const alertEl = document.getElementById('rejectAlert');
  // 拒收/逾時退回（機器人已送回，等關門）+ 不收/作廢（不需要機器人動作，等管理員確認知悉）
  const pending = packages.filter(p =>
    ((p.status === 'rejected_at_door' || p.status === 'returned_timeout') && !p.door_closed_at)
    || (p.status === 'voided' && !p.acknowledged_at)
  );

  if (pending.length === 0) {
    alertEl.style.display = 'none';
    alertEl.innerHTML = '';
    return;
  }

  const reasonLabel = { rejected_at_door: '拒收', returned_timeout: '逾時未取', voided: '不收（作廢）' };
  const btnStyle = 'background:white;color:#dc3545;border:none;padding:6px 14px;border-radius:6px;font-size:13px;cursor:pointer;';

  // 拒收/逾時退回的開門/關門已經統一移到上面「機器人狀態」欄位的按鈕處理
  // （那邊的按鈕平常白色、有包裹在等待時會自動變紅色提醒），這裡不再重複放
  // 開關門按鈕，只顯示目前卡在哪個階段的狀態文字，並依狀態提示該按哪一顆。
  const returnPending = pending.filter(p => p.status !== 'voided');
  const anyWaitingOpen = returnPending.some(p => p.returned_at && !p.return_door_opened_at);
  const anyWaitingClose = returnPending.some(p => p.return_door_opened_at && !p.door_closed_at);
  let batchActionHtml = '';
  if (anyWaitingOpen) {
    batchActionHtml = `<span style="font-size:13px;opacity:0.9;">請至上方「機器人狀態」欄位按「檢查艙門」開門 ↑</span>`;
  } else if (anyWaitingClose) {
    batchActionHtml = `<span style="font-size:13px;opacity:0.9;">艙門已開啟，請確認清空後至上方「機器人狀態」欄位按「確認關門」↑</span>`;
  } else if (returnPending.length > 0) {
    batchActionHtml = `<span style="font-size:13px;opacity:0.9;">等待機器人返回</span>`;
  }

  alertEl.style.display = 'block';
  alertEl.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:10px;">
      <b>有 ${pending.length} 筆包裹需要處理，請確認</b>
      ${batchActionHtml}
    </div>
    <table>
      <thead><tr><th>門牌</th><th>艙門</th><th>原因</th><th>狀態</th><th></th></tr></thead>
      <tbody>
        ${pending.map(p => {
          let statusText, forceResolveHtml = '';
          if (p.status === 'voided') {
            statusText = `<button style="${btnStyle}" onclick="acknowledgeVoid(this, '${p.id}')">確定</button>`;
          } else if (!p.returned_at) {
            statusText = `<span style="opacity:0.8;">等待機器人返回</span>`;
            forceResolveHtml = `<a href="javascript:void(0)" onclick="forceResolve(this, '${p.id}')" style="font-size:12px;color:white;text-decoration:underline;cursor:pointer;">手動結案</a>`;
          } else if (!p.return_door_opened_at) {
            statusText = `<span style="opacity:0.8;">待開門</span>`;
            forceResolveHtml = `<a href="javascript:void(0)" onclick="forceResolve(this, '${p.id}')" style="font-size:12px;color:white;text-decoration:underline;cursor:pointer;">手動結案</a>`;
          } else {
            statusText = `<span style="opacity:0.8;">待關門</span>`;
            forceResolveHtml = `<a href="javascript:void(0)" onclick="forceResolve(this, '${p.id}')" style="font-size:12px;color:white;text-decoration:underline;cursor:pointer;">手動結案</a>`;
          }
          return `<tr>
          <td>${p.unit}</td>
          <td>${p.door_id || '-'}</td>
          <td>${(p.status === 'rejected_at_door' && p.task_type === 'return') ? '已取消退貨' : (reasonLabel[p.status] || p.status)}</td>
          <td>${statusText}</td>
          <td style="text-align:right;">${forceResolveHtml}</td>
        </tr>`;
        }).join('')}
      </tbody>
    </table>
  `;
}

function renderReturnPendingAlert(packages) {
  // 退貨任務：住戶已經放貨完成（status=completed），機器人應該已經把包裹
  // 帶回管理室，但管理員還沒按「確認取出」——這裡主動提醒，不是等管理員
  // 自己想到要去查包裹清單。跟送貨任務的completed不同，退貨的completed
  // 不代表「這件事結束了」，是代表「輪到管理員動作了」。
  const alertEl = document.getElementById('returnPendingAlert');
  const pending = packages.filter(p => p.task_type === 'return' && p.status === 'completed' && !p.return_retrieved_at);

  if (pending.length === 0) {
    alertEl.style.display = 'none';
    alertEl.innerHTML = '';
    return;
  }

  const btnStyle = 'background:white;color:#004085;border:none;padding:6px 14px;border-radius:6px;font-size:13px;cursor:pointer;';

  alertEl.style.display = 'block';
  alertEl.innerHTML = `
    <b>有 ${pending.length} 筆退貨件已送達管理室，請盡速取出</b>
    <table>
      <thead><tr><th>門牌</th><th>艙門</th><th>建立時間</th><th></th></tr></thead>
      <tbody>
        ${pending.map(p => {
          const createdAt = p.created_at ? p.created_at.replace('T', ' ').slice(0, 16) : '-';
          return `<tr>
          <td>${p.unit}</td>
          <td>${p.door_id || '-'}</td>
          <td>${createdAt}</td>
          <td style="text-align:right;"><button style="${btnStyle}" onclick="confirmReturnRetrieved(this, '${p.id}')">確認取出</button></td>
        </tr>`;
        }).join('')}
      </tbody>
    </table>
    <div style="font-size:12px;opacity:0.85;margin-top:6px;">請先用上方「機器人狀態」欄位的「開啟艙門／關閉艙門」實際取出物品，再按這裡的「確認取出」結案</div>
  `;
}

async function confirmReturnRetrieved(btn, packageId) {
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '處理中...';
  try {
    const resp = await fetch(`/packages/${packageId}/confirm-return-retrieved`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '確認失敗');
    loadPackages();
  } catch (e) {
    alert('確認失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

const BUCKET_STYLE = {
  '已完成': 'background:#e2e3e5;color:#383d41;',
  '派送中': 'background:#cfe2ff;color:#084298;',
  '拒收已退回': 'background:#dc3545;color:white;font-weight:bold;',
  '逾時已退回': 'background:#dc3545;color:white;font-weight:bold;',
  '已取消退貨': 'background:#dc3545;color:white;font-weight:bold;',
  '尚未派工': 'background:#fff3cd;color:#664d03;',
};

async function queryByUnit() {
  const unit = document.getElementById('unitQueryInput').value.trim();
  const resultEl = document.getElementById('unitQueryResult');
  const btn = document.getElementById('unitQueryBtn');
  if (!unit) {
    resultEl.innerHTML = '<span style="color:red">請輸入門牌</span>';
    return;
  }

  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '查詢中...';
  try {
    const resp = await fetch(`/admin/packages/by-unit?unit=${encodeURIComponent(unit)}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '查詢失敗');

    if (data.length === 0) {
      resultEl.innerHTML = `<span style="color:#888">門牌「${unit}」目前沒有任何包裹紀錄</span>`;
      return;
    }

    resultEl.innerHTML = `
      <div style="background:#f5f5f5;border-radius:8px;padding:8px 12px;">
        <table>
          <thead><tr><th>建立時間</th><th>狀態</th><th>收件人</th><th>包裹ID</th></tr></thead>
          <tbody>
            ${data.map(p => {
              const style = BUCKET_STYLE[p.bucket] || '';
              const createdAt = p.created_at ? p.created_at.replace('T', ' ').slice(0, 16) : '-';
              return `<tr>
                <td>${createdAt}</td>
                <td><span class="status-badge" style="${style}">${p.bucket}</span></td>
                <td>${p.recipient_name}</td>
                <td style="font-size:11px;color:#888">${p.id}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    `;
  } catch (e) {
    resultEl.innerHTML = `<span style="color:red">查詢失敗：${e.message}</span>`;
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function clearUnitQuery() {
  document.getElementById('unitQueryInput').value = '';
  document.getElementById('unitQueryResult').innerHTML = '';
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('unitQueryInput');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') queryByUnit();
    });
  }
  ['packageDateFrom', 'packageDateTo'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') applyDateFilter();
      });
    }
  });
});

async function placePackage(selectEl, packageId) {
  const doorId = selectEl.value;
  if (!doorId) return;
  selectEl.disabled = true;
  try {
    const resp = await fetch(`/packages/${packageId}/place`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ door_id: doorId }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '放置失敗');
    loadPackages();
  } catch (e) {
    alert('放置失敗：' + e.message);
    selectEl.disabled = false;
    selectEl.value = '';
  }
}

function buildDoorPlacementSelect(p) {
  // 用packagesById（loadLivePackages抓到的所有進行中包裹）算出這一輪裡
  // 每扇門目前被哪個收件人（line_user_id）佔用——door_task_id有值代表
  // 這一輪已經開過，不是舊資料殘留。
  const doorOccupants = {};
  for (const key in packagesById) {
    const other = packagesById[key];
    if (other.status === 'pickup_now' && other.door_id && other.door_task_id && other.id !== p.id) {
      doorOccupants[other.door_id] = other.line_user_id;
    }
  }

  const doorIds = latestDoorStates.length > 0
    ? latestDoorStates.map(d => d.door_number)
    : ['H_01', 'H_02', 'H_03', 'H_04']; // 機器人狀態還沒載入完成時的保底清單

  const options = doorIds.map(doorId => {
    const physical = latestDoorStates.find(d => d.door_number === doorId);
    const occupantLineUserId = doorOccupants[doorId];

    if (occupantLineUserId) {
      if (occupantLineUserId === p.line_user_id) {
        return `<option value="${doorId}">${doorId}（加入本人已開啟）</option>`;
      }
      return `<option value="${doorId}" disabled>${doorId}（其他收件人使用中）</option>`;
    }
    if (physical && (physical.status || '').toUpperCase() !== 'EMPTY') {
      return `<option value="${doorId}" disabled>${doorId}（艙門非空）</option>`;
    }
    return `<option value="${doorId}">${doorId}</option>`;
  }).join('');

  return `<select onchange="placePackage(this, '${p.id}')" style="max-width:190px;">
    <option value="" selected disabled>請選擇艙門</option>
    ${options}
  </select>`;
}

async function releaseDoor(btn, packageId) {
  if (!confirm('確定要釋放這扇艙門嗎？機器人會關閉這扇門並將狀態改回空的，釋放後才能重新選擇艙門。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '釋放中...';
  try {
    const resp = await fetch(`/packages/${packageId}/release-door`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '釋放失敗');
    loadPackages();
  } catch (e) {
    alert('釋放失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function dispatchBatch() {
  const btn = document.getElementById('dispatchBatchBtn');
  btn.disabled = true;
  const originalText = btn.innerHTML;
  btn.textContent = '派送中...';
  try {
    const resp = await fetch('/admin/dispatch-batch', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '派送失敗');
    alert(`已派送 ${data.dispatched_count} 筆（共 ${data.total_quantity} 件）包裹`);
    loadPackages();
  } catch (e) {
    alert('派送失敗：' + e.message);
    btn.innerHTML = originalText;
    btn.disabled = false;
  }
}

async function acknowledgeVoid(btn, packageId) {
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '確認中...';
  try {
    const resp = await fetch(`/packages/${packageId}/acknowledge`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '確認失敗');
    loadPackages();
  } catch (e) {
    alert('確認失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function forceResolve(el, packageId) {
  if (!confirm('確定要手動結案嗎？這不會呼叫機器人，只適用於你已經自己手動處理過機器人艙門實體狀態的情況，操作後這筆包裹會直接從提示框消失。')) return;
  const originalText = el.textContent;
  el.textContent = '處理中...';
  el.style.pointerEvents = 'none';
  try {
    const resp = await fetch(`/packages/${packageId}/force-resolve`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '手動結案失敗');
    loadPackages();
  } catch (e) {
    alert('手動結案失敗：' + e.message);
    el.textContent = originalText;
    el.style.pointerEvents = 'auto';
  }
}

async function manualOpenDoors(btn) {
  if (!confirm('將打開機器人上所有艙門，建議機器人每次返回時都執行檢查。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '開門中...';
  try {
    const resp = await fetch('/admin/doors/manual-open', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '開門失敗');
    loadPackages();
  } catch (e) {
    alert('開門失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function manualCloseDoors(btn) {
  if (!confirm('請確認所有艙門都已清空再按確認鍵關門。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '關門中...';
  try {
    const resp = await fetch('/admin/doors/manual-close', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '關門失敗');
    loadPackages();
  } catch (e) {
    alert('關門失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function robotRecall(btn) {
  if (!confirm('叫回機器人會強制中斷機器人正在執行的任何動作，進行中的包裹任務將重置為待派送。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '叫回中...';
  try {
    const resp = await fetch('/admin/robot/recall', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '叫回失敗');
    if (data.reset_count > 0) {
      alert(`機器人已叫回，${data.reset_count} 筆進行中的任務已重置為待派送，請於機器人回到管理室後開門確認艙門內容`);
    }
    loadRobotStatus();
    loadPackages();
  } catch (e) {
    alert('叫回失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function robotRecharge(btn) {
  if (!confirm('確定要叫機器人回充電站嗎？')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '呼叫中...';
  try {
    const resp = await fetch('/admin/robot/recharge', { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '呼叫失敗');
    alert('已通知機器人回充電站');
  } catch (e) {
    alert('呼叫失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function loadRobotStatus() {
  const infoEl = document.getElementById('robotInfo');
  const doorEl = document.getElementById('doorInfo');
  try {
    const resp = await fetch('/admin/robot-status');
    const data = await resp.json();
    if (data.status === 'error') {
      infoEl.innerHTML = `<span style="color:red">${data.detail}</span>`;
      doorEl.innerHTML = '';
      return;
    }
    const payload = data.data;
    const robot = payload.robot_status;
    const doors = payload.door_states;
    latestDoorStates = doors || [];

    // 機器人API目前把真正即時的數值塞在 robot_status.sources.v1/v2.data 裡，
    // 外層的 battery_level / current_location 是機器人那邊還沒同步更新的舊欄位，
    // state則是掛在data這層，不在robot_status裡面。
    // 這裡照實際結構讀，並保留舊路徑當fallback，以後機器人那邊修好了也不用再改。
    const src = (robot.sources && (robot.sources.v1 || robot.sources.v2))
      ? (robot.sources.v1 || robot.sources.v2).data
      : null;

    const state = payload.state || robot.state || robot.move_state || '未知';
    const battery = src?.battery ?? robot.battery ?? robot.battery_level ?? null;
    const mapName = src?.map_name ? src.map_name.replace(/^\\d+#\\d+#/, '') : null;
    const location = robot.current_location
      || mapName
      || (src?.position ? `(${src.position.x.toFixed(1)}, ${src.position.y.toFixed(1)})` : null);

    infoEl.innerHTML = `
      <div><b>狀態</b>${state}</div>
      <div><b>目前位置</b>${location || '未知'}</div>
      <div><b>電量</b>${battery !== null ? battery + '%' : '未知'}</div>`;

    // 開關艙門只有機器人真的在管理室時才能操作，後端也有同樣的檢查（雙重保險），
    // 這裡先在前端把按鈕disable掉，避免管理員按了才被拒絕
    const atOffice = location === ROBOT_HOME_POINT_NAME;
    const openBtn = document.getElementById('manualOpenBtn');
    const closeBtn = document.getElementById('manualCloseBtn');
    if (openBtn) {
      openBtn.disabled = !atOffice;
      openBtn.title = atOffice ? '打開所有艙門' : `機器人目前不在管理室（${location || '未知'}），無法開關艙門`;
    }
    if (closeBtn) {
      closeBtn.disabled = !atOffice;
      closeBtn.title = atOffice ? '請確認所有艙門皆空再關閉艙門' : `機器人目前不在管理室（${location || '未知'}），無法開關艙門`;
    }

    doorEl.innerHTML = doors.map(d => {
      const pkg = d.package_id ? packagesById[d.package_id] : null;
      // 正常情況顯示門牌；如果packagesById還沒抓到對應資料（例如剛載入頁面時兩個API還沒都回來），
      // 退回顯示package_id前8碼，之後下一次自動更新就會補正確
      const label = pkg ? pkg.unit : (d.package_id ? d.package_id.slice(0, 8) + '...' : '');
      return `
      <div class="door-box door-${(d.status || '').toUpperCase()}">
        <div>${d.door_number}</div><div>${d.status}</div>
        ${label ? `<div style="font-size:11px">${label}</div>` : ''}
      </div>`;
    }).join('');
  } catch (e) {
    infoEl.innerHTML = `<span style="color:red">無法載入：${e.message}</span>`;
  }
}

loadBindings();
loadPackages();
loadRobotStatus();
setInterval(loadPackages, 10000);
setInterval(loadRobotStatus, 10000);
</script>
</body>
</html>
"""


@app.get("/admin/reports", response_class=HTMLResponse, dependencies=[Depends(require_admin_auth)])
async def admin_reports_page():
    return HTMLResponse(content=ADMIN_REPORTS_HTML)

@app.get("/admin/exceptions", response_class=HTMLResponse, dependencies=[Depends(require_admin_auth)])
async def admin_exceptions_page():
    return HTMLResponse(content=ADMIN_EXCEPTIONS_HTML)

@app.get("/admin/residents", response_class=HTMLResponse, dependencies=[Depends(require_admin_auth)])
async def admin_residents_page():
    return HTMLResponse(content=ADMIN_RESIDENTS_HTML)


ADMIN_REPORTS_HTML = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>FlashBot 每日報表</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: #f5f5f5; margin: 0; padding: 20px; color: #222; zoom: 1.15; }
  h1 { color: #E2231A; font-size: 22px; margin-bottom: 20px; }
  h2 { font-size: 16px; margin: 0 0 12px 0; color: #333; }
  .card { background: white; border-radius: 8px; padding: 16px; margin-bottom: 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
  input, button { padding: 8px 12px; font-size: 14px; border-radius: 6px;
    border: 1px solid #ccc; margin-right: 8px; }
  button { background: #E2231A; color: white; border: none; cursor: pointer; }
  button:hover { background: #c41c14; }
  button:disabled { opacity: 0.6; cursor: default; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; }
  th { color: #888; font-weight: normal; }
  .summary-grid { display: flex; gap: 16px; flex-wrap: wrap; }
  .summary-box { background: #f9f9f9; border-radius: 8px; padding: 12px 20px; text-align: center; min-width: 90px; }
  .summary-box b { display: block; font-size: 22px; color: #E2231A; }
  .summary-box span { font-size: 12px; color: #888; }
  .level-error { color: #c41c14; font-weight: bold; }
  .level-warning { color: #b58105; }
  .level-info { color: #333; }
  .empty-hint { color: #999; font-size: 14px; padding: 12px 0; }
</style>
</head>
<body>

<h1>FlashBot 每日報表
  <a href="/admin" style="font-size:14px;font-weight:normal;color:#E2231A;margin-left:16px;">← 回 Dashboard</a>
  <a href="/admin/exceptions" style="font-size:14px;font-weight:normal;color:#E2231A;">退回/作廢包裹處理 →</a>
  <a href="/admin/residents" style="font-size:14px;font-weight:normal;color:#E2231A;">住戶綁定管理 →</a>
</h1>

<div class="card">
  <h2>選擇日期</h2>
  <input type="date" id="reportDate" />
  <button id="queryBtn" onclick="queryReport()">查詢</button>
</div>

<div class="card">
  <h2>包裹狀態統計</h2>
  <div id="summaryGrid" class="summary-grid"><div class="empty-hint">請選擇日期後查詢</div></div>
</div>

<div class="card">
  <div style="display:flex;align-items:center;flex-wrap:wrap;gap:12px;margin-bottom:12px;">
    <h2 style="margin:0;">任務時間軸</h2>
    <div id="logPagerInfo" style="font-size:13px;color:#888;"></div>
    <div style="margin-left:auto;display:flex;gap:12px;align-items:center;">
      <a id="logPrevBtn" href="javascript:void(0)" onclick="prevLogGroup()"
        style="font-size:14px;color:#E2231A;cursor:pointer;">← 上一筆</a>
      <span id="logPagerCount" style="font-size:13px;color:#888;white-space:nowrap;"></span>
      <a id="logNextBtn" href="javascript:void(0)" onclick="nextLogGroup()"
        style="font-size:14px;color:#E2231A;cursor:pointer;">下一筆 →</a>
    </div>
  </div>
  <table>
    <thead><tr><th>時間</th><th>等級</th><th>事件</th><th>內容</th></tr></thead>
    <tbody id="logTableBody"><tr><td colspan="4" class="empty-hint">請選擇日期後查詢</td></tr></tbody>
  </table>
</div>

<script>
// 預設帶入今天日期，方便直接查詢
const today = new Date();
const yyyy = today.getFullYear();
const mm = String(today.getMonth() + 1).padStart(2, '0');
const dd = String(today.getDate()).padStart(2, '0');
document.getElementById('reportDate').value = `${yyyy}-${mm}-${dd}`;

let logGroups = [];       // [{ packageId, logs }]，每個元素是一個包裹的所有紀錄
let currentGroupIndex = 0;
let packagesById = {};

async function queryReport() {
  const btn = document.getElementById('queryBtn');
  const date = document.getElementById('reportDate').value;
  if (!date) { alert('請先選擇日期'); return; }

  btn.disabled = true;
  btn.textContent = '查詢中...';
  try {
    const resp = await fetch(`/admin/reports/daily?date=${date}`);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '查詢失敗');

    renderSummary(data.package_status_summary, data.package_count);

    packagesById = Object.fromEntries((data.packages || []).map(p => [p.id, p]));
    logGroups = groupLogsByPackage(data.task_logs);
    currentGroupIndex = logGroups.length > 0 ? logGroups.length - 1 : 0;
    renderLogGroup();
  } catch (e) {
    alert('查詢失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '查詢';
  }
}

function renderSummary(summary, total) {
  const el = document.getElementById('summaryGrid');
  const keys = Object.keys(summary || {});
  if (keys.length === 0) {
    el.innerHTML = '<div class="empty-hint">這天沒有包裹狀態異動紀錄</div>';
    return;
  }
  el.innerHTML = `
    <div class="summary-box"><b>${total}</b><span>當日異動總數</span></div>
    ${keys.map(k => `<div class="summary-box"><b>${summary[k]}</b><span>${k}</span></div>`).join('')}
  `;
}

function groupLogsByPackage(logs) {
  // 依package_id分組，保留原本的時間順序（第一次出現該package_id的順序）；
  // package_id是null的紀錄（例如沒有對應特定包裹的系統事件）另外歸成一組
  const order = [];
  const map = {};
  (logs || []).forEach(log => {
    const key = log.package_id || '__no_package__';
    if (!map[key]) {
      map[key] = { packageId: log.package_id, logs: [] };
      order.push(key);
    }
    map[key].logs.push(log);
  });
  return order.map(key => map[key]);
}

function setPagerLinkState(el, disabled) {
  if (disabled) {
    el.style.color = '#ccc';
    el.style.pointerEvents = 'none';
    el.style.cursor = 'default';
  } else {
    el.style.color = '#E2231A';
    el.style.pointerEvents = 'auto';
    el.style.cursor = 'pointer';
  }
}

function renderLogGroup() {
  const tbody = document.getElementById('logTableBody');
  const infoEl = document.getElementById('logPagerInfo');
  const countEl = document.getElementById('logPagerCount');
  const prevBtn = document.getElementById('logPrevBtn');
  const nextBtn = document.getElementById('logNextBtn');

  if (logGroups.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-hint">這天沒有任務紀錄</td></tr>';
    infoEl.textContent = '';
    countEl.textContent = '';
    setPagerLinkState(prevBtn, true);
    setPagerLinkState(nextBtn, true);
    return;
  }

  const group = logGroups[currentGroupIndex];
  const pkg = group.packageId ? packagesById[group.packageId] : null;

  if (group.packageId) {
    infoEl.textContent = pkg
      ? `門牌：${pkg.unit}　狀態：${pkg.status}　包裹ID：${group.packageId}`
      : `包裹ID：${group.packageId}（非當天建立/更新的包裹，門牌資訊未顯示）`;
  } else {
    infoEl.textContent = '系統事件（無對應特定包裹）';
  }

  countEl.textContent = `第 ${currentGroupIndex + 1} / 共 ${logGroups.length} 筆包裹`;
  setPagerLinkState(prevBtn, currentGroupIndex === 0);
  setPagerLinkState(nextBtn, currentGroupIndex === logGroups.length - 1);

  tbody.innerHTML = group.logs.map(log => `
    <tr>
      <td>${log.created_at ? log.created_at.replace('T', ' ').slice(0, 19) : '-'}</td>
      <td class="level-${log.level}">${log.level}</td>
      <td>${log.event_type}</td>
      <td>${log.detail || ''}</td>
    </tr>
  `).join('');
}

function prevLogGroup() {
  if (currentGroupIndex > 0) {
    currentGroupIndex -= 1;
    renderLogGroup();
  }
}

function nextLogGroup() {
  if (currentGroupIndex < logGroups.length - 1) {
    currentGroupIndex += 1;
    renderLogGroup();
  }
}

queryReport();
</script>
</body>
</html>
"""

ADMIN_EXCEPTIONS_HTML = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>退回/作廢包裹處理</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: #f5f5f5; margin: 0; padding: 20px; color: #222; zoom: 1.15; }
  h1 { color: #E2231A; font-size: 22px; margin-bottom: 20px; }
  .card { background: white; border-radius: 8px; padding: 16px; margin-bottom: 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; }
  th { color: #888; font-weight: normal; }
  button { padding: 6px 14px; font-size: 13px; border-radius: 6px; border: none;
    background: #E2231A; color: white; cursor: pointer; }
  button:hover { background: #c41c14; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.secondary { background: white; color: #E2231A; border: 1px solid #E2231A; }
  button.secondary:hover { background: #e9e9e9; }
  select { padding: 8px 12px; font-size: 14px; border-radius: 6px; border: 1px solid #ccc; }
  .action-buttons { display: inline-flex; gap: 6px; }
  .status-badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; background: #eee; }
  .status-voided { background: #f8d7da; color: #721c24; }
  .status-rejected_at_door { background: #dc3545; color: white; }
  .status-returned_timeout { background: #dc3545; color: white; }
  .pill { padding: 2px 8px; border-radius: 10px; font-size: 12px; }
  .pill-waiting { background: #fff3cd; color: #856404; }
  .pill-resolved { background: #d4edda; color: #155724; }
  .pill-redispatched { background: #cce5ff; color: #004085; }
  .empty-hint { color: #999; font-size: 14px; padding: 12px 0; }
  .pkg-select-checkbox, #selectAllCheckbox {
    appearance: none; -webkit-appearance: none;
    width: 22px; height: 22px; border: 2px solid #ccc; border-radius: 7px;
    cursor: pointer; position: relative; vertical-align: middle; margin: 0;
  }
  .pkg-select-checkbox:checked, #selectAllCheckbox:checked {
    background: #E2231A; border-color: #E2231A;
  }
  .pkg-select-checkbox:checked::after, #selectAllCheckbox:checked::after {
    content: ''; position: absolute; left: 7px; top: 3px;
    width: 5px; height: 10px; border: solid white; border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }
  tr.selectable-row { cursor: pointer; }
  tr.selectable-row:hover { background: #fff5f5; }
  tr.needs-attention-row { background: #fff0f0; }
</style>
</head>
<body>

<h1>退回/作廢包裹處理
  <a href="/admin" style="font-size:14px;font-weight:normal;color:#E2231A;margin-left:16px;">← 回 Dashboard</a>
  <a href="/admin/reports" style="font-size:14px;font-weight:normal;color:#E2231A;">← 查看每日報表</a>
  <a href="/admin/residents" style="font-size:14px;font-weight:normal;color:#E2231A;">住戶綁定管理 →</a>
</h1>

<div class="card">
  <p style="font-size:13px;color:#888;margin-top:0;">
    主畫面的確認/關門流程須先完成，才能在這裡按「重新派貨」。
  </p>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;">
    <button id="selectModeBtn" class="secondary" style="margin-left:0;" onclick="toggleSelectMode()">選取</button>
    <button id="closeSelectedBtn" style="display:none;background:#dc3545;margin-left:0;" onclick="closeSelectedCases()" disabled>全部銷案（0）</button>
    <input type="text" id="unitFilterInput" placeholder="輸入門牌搜尋"
      style="width:220px;height:36px;padding:0 10px;border-radius:6px;border:1px solid #ccc;font-size:14px;box-sizing:border-box;" />
    <button id="unitFilterBtn" onclick="filterByUnit()"
      style="height:36px;padding:0 16px;font-size:14px;box-sizing:border-box;">查詢</button>
    <button id="unitFilterClearBtn" onclick="clearUnitFilter()"
      style="height:36px;padding:0 14px;font-size:14px;box-sizing:border-box;background:white;color:#E2231A;border:1px solid #E2231A;cursor:pointer;">清除</button>
    <span id="unitFilterCount" style="font-size:13px;color:#888;"></span>
  </div>
  <table>
    <thead><tr>
      <th id="selectColHeader" style="display:none;width:30px;"><input type="checkbox" id="selectAllCheckbox" onchange="toggleSelectAll(this)" /></th>
      <th>門牌</th><th>收件人</th><th>狀態</th><th>建立時間</th><th>主畫面處理</th><th>操作</th><th>已通知時間</th>
    </tr></thead>
    <tbody id="exceptionsTableBody"><tr><td colspan="7">載入中...</td></tr></tbody>
  </table>
</div>

<script>
const STATUS_LABEL = {
  voided: '不收（作廢）', rejected_at_door: '拒收', returned_timeout: '逾時未取',
};

let allExceptions = [];
let selectMode = false;
let selectedPackageIds = new Set();

async function loadExceptions() {
  const tbody = document.getElementById('exceptionsTableBody');
  try {
    const resp = await fetch('/admin/packages/exceptions');
    allExceptions = await resp.json();
    renderExceptions(allExceptions);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="${selectMode ? 8 : 7}" style="color:red">載入失敗：${e.message}</td></tr>`;
  }
}

function renderExceptions(packages) {
  const tbody = document.getElementById('exceptionsTableBody');
  const keyword = document.getElementById('unitFilterInput').value.trim();
  const colCount = selectMode ? 8 : 7;

  if (packages.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${colCount}" class="empty-hint">${keyword ? '找不到符合的門牌' : '目前沒有退回/作廢的包裹'}</td></tr>`;
    return;
  }
  tbody.innerHTML = packages.map(p => {
    const checkboxCell = selectMode
      ? `<td><input type="checkbox" class="pkg-select-checkbox" data-id="${p.id}" ${selectedPackageIds.has(p.id) ? 'checked' : ''} onchange="togglePackageSelect('${p.id}', this.checked)" /></td>`
      : '';
    const label = (p.status === 'rejected_at_door' && p.task_type === 'return')
      ? '已取消退貨'
      : (STATUS_LABEL[p.status] || p.status);
    const createdAt = p.created_at ? p.created_at.replace('T', ' ').slice(0, 16) : '-';
    const recipients = p.recipients.map(r => r.name).join('、') || '-';

    const notifiedCell = p.pending_pickup_notified_at
      ? p.pending_pickup_notified_at.replace('T', ' ').slice(0, 16)
      : '-';
    const notifyButton = p.pending_pickup_notified_at
      ? ''
      : `<button class="secondary" onclick="notifyPendingPickup(this, '${p.id}')">通知住戶</button>`;

    let resolvedPill, action;
    if (p.redispatched_at) {
      resolvedPill = '<span class="pill pill-redispatched">已重新派送</span>';
      action = `新包裹 ${p.redispatched_to.slice(0, 8)}...`;
    } else if (!p.resolved) {
      resolvedPill = '<span class="pill pill-waiting">尚未處理</span>';
      action = `<span class="action-buttons">
        <button disabled title="請先在主畫面確認/關門">重新派貨</button>
        ${notifyButton || '<span style="font-size:12px;color:#888;">已通知</span>'}
      </span>`;
    } else {
      resolvedPill = '<span class="pill pill-resolved">已處理</span>';
      action = `<span class="action-buttons">
        <button onclick="redispatch(this, '${p.id}')">重新派貨</button>
        ${notifyButton}
      </span>`;
    }

    const rowClass = (selectMode ? 'selectable-row ' : '') + (p.needs_attention ? 'needs-attention-row' : '');
    const rowClick = selectMode ? ` onclick="handleRowClick(event, '${p.id}')"` : '';
    const attentionBadge = p.needs_attention
      ? '<div style="color:#dc3545;font-size:11px;font-weight:bold;margin-top:2px;">⚠️ 已超過72小時未處理</div>'
      : '';

    return `<tr class="${rowClass}"${rowClick}>
      ${checkboxCell}
      <td>${p.unit}</td>
      <td>${recipients}</td>
      <td><span class="status-badge status-${p.status}">${label}</span></td>
      <td>${createdAt}</td>
      <td>${resolvedPill}${attentionBadge}</td>
      <td>${action}</td>
      <td>${notifiedCell}</td>
    </tr>`;
  }).join('');
}

function toggleSelectMode() {
  if (selectMode) {
    exitSelectMode();
    renderExceptions(allExceptions);
    return;
  }
  selectMode = true;
  document.getElementById('selectColHeader').style.display = 'table-cell';
  document.getElementById('selectModeBtn').textContent = '取消選取';
  document.getElementById('closeSelectedBtn').style.display = 'inline-block';
  updateCloseSelectedButtonState();
  renderExceptions(allExceptions);
}

function exitSelectMode() {
  selectMode = false;
  selectedPackageIds.clear();
  document.getElementById('selectColHeader').style.display = 'none';
  document.getElementById('selectModeBtn').textContent = '選取';
  document.getElementById('closeSelectedBtn').style.display = 'none';
  updateCloseSelectedButtonState();
}

function handleRowClick(event, id) {
  if (event.target.closest('input, button, a')) return;
  const checkbox = document.querySelector(`.pkg-select-checkbox[data-id="${id}"]`);
  if (!checkbox) return;
  checkbox.checked = !checkbox.checked;
  togglePackageSelect(id, checkbox.checked);
}

function togglePackageSelect(id, checked) {
  if (checked) {
    selectedPackageIds.add(id);
  } else {
    selectedPackageIds.delete(id);
  }
  updateCloseSelectedButtonState();
}

function toggleSelectAll(checkbox) {
  document.querySelectorAll('.pkg-select-checkbox').forEach(cb => {
    cb.checked = checkbox.checked;
    if (checkbox.checked) {
      selectedPackageIds.add(cb.dataset.id);
    } else {
      selectedPackageIds.delete(cb.dataset.id);
    }
  });
  updateCloseSelectedButtonState();
}

function updateCloseSelectedButtonState() {
  const btn = document.getElementById('closeSelectedBtn');
  const count = selectedPackageIds.size;
  btn.textContent = `全部銷案（${count}）`;
  btn.disabled = count === 0;
}

async function closeSelectedCases() {
  const ids = Array.from(selectedPackageIds);
  if (ids.length === 0) return;
  if (!confirm(`確定要將選取的 ${ids.length} 筆包裹全部銷案嗎？銷案後這些紀錄會從此頁面移除，主畫面資料不受影響，且無法復原。`)) return;

  const btn = document.getElementById('closeSelectedBtn');
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '銷案中...';
  try {
    const resp = await fetch('/admin/packages/close-case-batch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ package_ids: ids }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '銷案失敗');

    if (data.skipped && data.skipped.length > 0) {
      const reasons = data.skipped.map(s => `${s.id.slice(0, 8)}...：${s.reason}`).join('\\n');
      alert(`已銷案 ${data.closed.length} 筆，${data.skipped.length} 筆無法銷案：\n${reasons}`);
    } else {
      alert(`已銷案 ${data.closed.length} 筆`);
    }
    exitSelectMode();
    loadExceptions();
  } catch (e) {
    alert('銷案失敗：' + e.message);
  } finally {
    updateCloseSelectedButtonState();
  }
}

function filterByUnit() {
  const keyword = document.getElementById('unitFilterInput').value.trim().toLowerCase();
  const countEl = document.getElementById('unitFilterCount');
  if (!keyword) {
    countEl.textContent = '';
    renderExceptions(allExceptions);
    return;
  }
  const filtered = allExceptions.filter(p => p.unit.toLowerCase().includes(keyword));
  countEl.textContent = `符合「${keyword}」共 ${filtered.length} 筆`;
  renderExceptions(filtered);
}

function clearUnitFilter() {
  document.getElementById('unitFilterInput').value = '';
  document.getElementById('unitFilterCount').textContent = '';
  renderExceptions(allExceptions);
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('unitFilterInput');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') filterByUnit();
    });
  }
});

async function notifyPendingPickup(btn, packageId) {
  if (!confirm('確定要補發包裹通知給住戶嗎？（只能通知一次，請確認後再送出）')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '通知中...';
  try {
    const resp = await fetch(`/packages/${packageId}/notify-pending-pickup`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '通知失敗');
    if (data.notify_failed_count > 0) {
      alert(`已通知 ${data.notified_count} 位收件人，${data.notify_failed_count} 位通知失敗`);
    } else {
      alert(`已通知 ${data.notified_count} 位收件人`);
    }
    loadExceptions();
  } catch (e) {
    alert('通知失敗：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function redispatch(btn, packageId) {
  if (!confirm('確定要重新派送這筆包裹嗎？將建立一筆新包裹並重新通知住戶。')) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '派送中...';
  try {
    const resp = await fetch(`/packages/${packageId}/redispatch`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '重新派送失敗');
    if (data.notify_failed && data.notify_failed.length > 0) {
      alert(`已建立新包裹，但 ${data.notify_failed.join('、')} 通知失敗，請確認LINE綁定`);
    } else {
      alert('已建立新包裹並通知住戶');
    }
    loadExceptions();
  } catch (e) {
    alert('重新派送失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

loadExceptions();
</script>
</body>
</html>
"""

ADMIN_RESIDENTS_HTML = """
<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>住戶綁定管理</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: #f5f5f5; margin: 0; padding: 20px; color: #222; zoom: 1.15; }
  h1 { color: #E2231A; font-size: 22px; margin-bottom: 20px; }
  .card { background: white; border-radius: 8px; padding: 16px; margin-bottom: 20px;
    box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px; border-bottom: 1px solid #eee; }
  th { color: #888; font-weight: normal; }
  button { padding: 6px 14px; font-size: 13px; border-radius: 6px; border: none;
    background: #E2231A; color: white; cursor: pointer; }
  button:hover { background: #c41c14; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.secondary { background: white; color: #E2231A; border: 1px solid #E2231A; }
  button.secondary:hover { background: #e9e9e9; }
  .status-badge { padding: 2px 8px; border-radius: 10px; font-size: 12px; background: #eee; }
  .status-active { background: #d4edda; color: #155724; }
  .status-inactive { background: #e2e3e5; color: #383d41; }
  .empty-hint { color: #999; font-size: 14px; padding: 12px 0; }
</style>
</head>
<body>

<h1>住戶綁定管理
  <a href="/admin" style="font-size:14px;font-weight:normal;color:#E2231A;margin-left:16px;">← 回 Dashboard</a>
  <a href="/admin/reports" style="font-size:14px;font-weight:normal;color:#E2231A;">← 查看每日報表</a>
  <a href="/admin/exceptions" style="font-size:14px;font-weight:normal;color:#E2231A;">← 退回/作廢包裹處理</a>
</h1>

<div class="card">
  <p style="font-size:13px;color:#888;margin-top:0;">
    列出所有住戶的LINE綁定紀錄（含已停用），可以直接刪除帳號綁定；刪除後此用戶不會再收到包裹通知，且無法復原。
  </p>
  <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;">
    <input type="text" id="unitFilterInput" placeholder="輸入門牌搜尋"
      style="width:220px;height:36px;padding:0 10px;border-radius:6px;border:1px solid #ccc;font-size:14px;box-sizing:border-box;" />
    <button id="unitFilterBtn" onclick="filterByUnit()"
      style="height:36px;padding:0 16px;font-size:14px;box-sizing:border-box;">查詢</button>
    <button id="unitFilterClearBtn" onclick="clearUnitFilter()"
      style="height:36px;padding:0 14px;font-size:14px;box-sizing:border-box;background:white;color:#E2231A;border:1px solid #E2231A;cursor:pointer;">清除</button>
    <span id="unitFilterCount" style="font-size:13px;color:#888;"></span>
  </div>
  <table>
    <thead><tr><th>門牌</th><th>姓名</th><th>狀態</th><th>綁定時間</th></tr></thead>
    <tbody id="bindingsTableBody"><tr><td colspan="5">載入中...</td></tr></tbody>
  </table>
</div>

<div id="editBindingOverlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.4);z-index:100;align-items:center;justify-content:center;">
  <div style="background:white;border-radius:10px;padding:24px;width:360px;max-width:90vw;">
    <h3 style="margin:0 0 16px 0;">修改綁定</h3>
    <input type="hidden" id="editBindingLineUserId" />
    <label style="font-size:13px;color:#888;display:block;margin-bottom:4px;">門牌</label>
    <input type="text" id="editBindingUnitInput" style="width:100%;height:36px;padding:0 10px;border-radius:6px;border:1px solid #ccc;font-size:14px;box-sizing:border-box;margin-bottom:12px;" />
    <label style="font-size:13px;color:#888;display:block;margin-bottom:4px;">姓名</label>
    <input type="text" id="editBindingNameInput" style="width:100%;height:36px;padding:0 10px;border-radius:6px;border:1px solid #ccc;font-size:14px;box-sizing:border-box;margin-bottom:16px;" />
    <div id="editBindingMsg" style="font-size:13px;margin-bottom:8px;"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;">
      <button class="secondary" style="margin:0;" onclick="closeEditBindingModal()">取消</button>
      <button id="editBindingSaveBtn" style="margin:0;" onclick="saveEditBinding()">儲存</button>
    </div>
  </div>
</div>

<script>
let allBindings = [];

async function loadBindings() {
  const tbody = document.getElementById('bindingsTableBody');
  try {
    const resp = await fetch('/admin/line-bindings');
    allBindings = await resp.json();
    renderBindings(allBindings);
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" style="color:red">載入失敗：${e.message}</td></tr>`;
  }
}

function renderBindings(bindings) {
  const tbody = document.getElementById('bindingsTableBody');
  const keyword = document.getElementById('unitFilterInput').value.trim();

  if (bindings.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-hint">${keyword ? '找不到符合的門牌' : '目前沒有任何綁定紀錄'}</td></tr>`;
    return;
  }

  tbody.innerHTML = bindings.map(b => {
    const statusLabel = b.status === 'active' ? '生效中' : '已停用';
    const boundAt = b.bound_at ? b.bound_at.replace('T', ' ').slice(0, 16) : '-';
    return `<tr>
      <td>${b.unit}</td>
      <td>${b.name}</td>
      <td><span class="status-badge status-${b.status}">${statusLabel}</span></td>
      <td>${boundAt}</td>
      <td style="text-align:right;">
        <button class="secondary" onclick="openEditBindingModal('${b.line_user_id}', '${b.unit}', '${b.name}')">修改</button>
        <button onclick="deleteBinding(this, '${b.line_user_id}', '${b.unit}', '${b.name}')">刪除</button>
      </td>
    </tr>`;
  }).join('');
}

function filterByUnit() {
  const keyword = document.getElementById('unitFilterInput').value.trim().toLowerCase();
  const countEl = document.getElementById('unitFilterCount');
  if (!keyword) {
    countEl.textContent = '';
    renderBindings(allBindings);
    return;
  }
  const filtered = allBindings.filter(b => b.unit.toLowerCase().includes(keyword));
  countEl.textContent = `符合「${keyword}」共 ${filtered.length} 筆`;
  renderBindings(filtered);
}

function clearUnitFilter() {
  document.getElementById('unitFilterInput').value = '';
  document.getElementById('unitFilterCount').textContent = '';
  renderBindings(allBindings);
}

document.addEventListener('DOMContentLoaded', () => {
  const input = document.getElementById('unitFilterInput');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') filterByUnit();
    });
  }
});

function openEditBindingModal(lineUserId, unit, name) {
  document.getElementById('editBindingOverlay').style.display = 'flex';
  document.getElementById('editBindingLineUserId').value = lineUserId;
  document.getElementById('editBindingUnitInput').value = unit;
  document.getElementById('editBindingNameInput').value = name;
  document.getElementById('editBindingMsg').textContent = '';
}

function closeEditBindingModal() {
  document.getElementById('editBindingOverlay').style.display = 'none';
}

async function saveEditBinding() {
  const lineUserId = document.getElementById('editBindingLineUserId').value;
  const unit = document.getElementById('editBindingUnitInput').value.trim();
  const name = document.getElementById('editBindingNameInput').value.trim();
  const msgEl = document.getElementById('editBindingMsg');

  if (!unit || !name) {
    msgEl.style.color = 'red';
    msgEl.textContent = '門牌與姓名都不能是空的';
    return;
  }

  const btn = document.getElementById('editBindingSaveBtn');
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '儲存中...';
  try {
    const resp = await fetch(`/admin/line-bindings/${lineUserId}/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unit, name }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '修改失敗');
    loadBindings();
    closeEditBindingModal();
  } catch (e) {
    msgEl.style.color = 'red';
    msgEl.textContent = '修改失敗：' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

async function deleteBinding(btn, lineUserId, unit, name) {
  if (!confirm(`確定要刪除「${unit} ${name}」這筆綁定嗎？此操作無法復原，該LINE帳號之後將不會再收到這個門牌的包裹通知。`)) return;
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = '刪除中...';
  try {
    const resp = await fetch(`/admin/line-bindings/${lineUserId}/delete`, { method: 'POST' });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '刪除失敗');
    loadBindings();
  } catch (e) {
    alert('刪除失敗：' + e.message);
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

loadBindings();
</script>
</body>
</html>
"""