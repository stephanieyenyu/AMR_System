"""
封裝呼叫LINE Messaging API的邏輯
"""
from datetime import timedelta
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage,
    FlexMessage, FlexContainer, PushMessageRequest,
)
from app.config import settings
from app.models import now_taipei

configuration = Configuration(access_token=settings.LINE_CHANNEL_ACCESS_TOKEN)


def push_arrival_notification(line_user_id: str, package_id: str, unit: str, quantity: int = 1):
    """推播到貨通知，附「取貨」「預約取貨」「不收」三個按鈕。quantity是這個任務代表幾件實體包裹"""
    header_text = "有新包裹送達" if quantity <= 1 else f"有{quantity}件新包裹送達"

    # 預約取貨的時間選擇範圍：min直接用「現在」這個時間點，不要進位到下一個
    # 整點——半點制的捨入規則（main.py的parse_and_round_schedule_datetime）
    # 允許選當前小時內半點以前的時間去預約「當前這個時段」，如果min被設成
    # 下一個整點，時間滾輪會把整個當前小時都反灰，等於選不到本該可以選的
    # 時間。滾輪只需要擋掉「過去的時間」，該捨入成哪個時段是後端的邏輯，
    # 兩邊職責分開。
    # initial（picker打開時預設顯示的時間）維持選下一個整點，開放範圍到
    # 7天後的整點，只能選整點（LINE的datetimepicker本身可以選到分鐘，
    # 「只能整點」這件事實際上是收到postback後在後端把分鐘部分捨去強制執行的，
    # 不是LINE picker原生就能限制只能選整點）
    now = now_taipei()
    next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    max_time = next_hour + timedelta(days=7)
    initial_str = next_hour.strftime("%Y-%m-%dT%H:%M")
    min_str = now.strftime("%Y-%m-%dT%H:%M")
    max_str = max_time.strftime("%Y-%m-%dT%H:%M")

    body_contents = [
        {"type": "text", "text": f"門牌：{unit}", "wrap": True},
    ]
    if quantity > 1:
        body_contents.append(
            {"type": "text", "text": f"共{quantity}件，將一次出貨", "wrap": True, "size": "sm", "color": "#029C4D"}
        )
    body_contents.append(
        {"type": "text", "text": "※ 預約以整點時段計算：半點前算當前時段，半點後算下一時段", "size": "xs", "color": "#999999", "wrap": True, "margin": "md"}
    )

    contents = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": header_text, "weight": "bold", "size": "lg"}
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": body_contents,
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#029C4D",
                    "action": {
                        "type": "postback",
                        "label": "取貨",
                        "data": f"action=PICKUP_NOW&package_id={package_id}",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "datetimepicker",
                        "label": "預約取貨",
                        "data": f"action=SCHEDULE_PICKUP&package_id={package_id}",
                        "mode": "datetime",
                        "initial": initial_str,
                        "min": min_str,
                        "max": max_str,
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "postback",
                        "label": "不收",
                        "data": f"action=REJECT&package_id={package_id}",
                    },
                },
            ],
        },
    }

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=line_user_id,
                messages=[FlexMessage(alt_text="有新包裹送達", contents=FlexContainer.from_dict(contents))],
            )
        )


def reply_text(reply_token: str, text: str):
    """回覆一則純文字訊息"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)],
            )
        )


def reply_welcome_with_binding_instructions(reply_token: str):
    """新用戶加好友時，改用純文字說明綁定方式"""
    text = (
        "歡迎加入！請在此聊天室輸入以下資料完成綁定：\n"
        "門牌 姓名\n"
        "例如：5F-1 王小明"
    )
    reply_text(reply_token, text)


def push_status_update(line_user_id: str, text: str):
    """推播單純的狀態更新文字訊息"""
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=line_user_id,
                messages=[TextMessage(text=text)],
            )
        )


def push_arrived_notification(line_user_id: str, door_task_id: str, quantity: int = 1, task_type: str = "delivery"):
    """
    機器人抵達時，推播提醒+開啟掃碼+拒收/取消按鈕。

    door_task_id取代了原本的package_id：同一個收件人這一輪如果用了不只一扇門
    （例如中、小兩件包裹放在同一戶但不同艙門），door_task_id是同一組值，
    LIFF連結跟按鈕都帶這個值——住戶不管掃哪一扇門的QR、或按取消，
    都會一次處理「這個task底下所有的門」，不用逐扇門各自操作一次。
    quantity是這個task底下總共幾件實體包裹（可能橫跨多扇門），純粹顯示用途。

    task_type區分「delivery」（送貨，機器人帶包裹來給住戶）跟「return」
    （退貨，住戶請機器人來收件送回管理室）——兩種方向文案不同，但呼叫機器人
    的API完全一樣（沿用同一套door-tasks端點），只是這裡顯示給住戶看的文字換掉。
    """
    if task_type == "return":
        header_text = "機器人已抵達，可以退貨了" if quantity <= 1 else f"機器人已抵達，可退貨（共{quantity}件）"
        body_text = "掃碼開啟艙門，放入物品後在此頁按「放貨完成」關門" if quantity <= 1 \
            else f"掃碼開啟對應的{quantity}個艙門，放入物品後在此頁按「放貨完成」關門"
        cancel_label = "暫時不退貨"
        cancel_action = "CANCEL_RETURN"
    else:
        header_text = "機器人已抵達" if quantity <= 1 else f"機器人已抵達（共{quantity}件）"
        body_text = "請於 8 分鐘內完成取貨" if quantity <= 1 else f"共{quantity}件，掃碼後開啟對應艙門，請於 8 分鐘內完成取貨"
        cancel_label = "拒收"
        cancel_action = "REJECT_AT_DOOR"

    contents = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": header_text, "weight": "bold", "size": "lg"}
            ],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": body_text, "wrap": True},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#029C4D",
                    "action": {
                        "type": "uri",
                        "label": "開啟相機掃碼",
                        "uri": f"https://liff.line.me/{settings.LIFF_ID}?door_task_id={door_task_id}&task_type={task_type}",
                    },
                },
                {
                    "type": "button",
                    "style": "secondary",
                    "action": {
                        "type": "postback",
                        "label": cancel_label,
                        "data": f"action={cancel_action}&door_task_id={door_task_id}",
                    },
                },
            ],
        },
    }

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=line_user_id,
                messages=[FlexMessage(alt_text=header_text, contents=FlexContainer.from_dict(contents))],
            )
        )



def push_pickup_complete_button(line_user_id: str, package_id: str, quantity: int = 1):
    """
    掃碼驗證通過、艙門已開啟後，推播讓用戶確認取貨完成的按鈕。

    ⚠️ 這支函式目前main.py沒有任何地方呼叫——取貨完成已經改成LIFF頁面掃碼
    驗證成功後，直接在同一頁顯示「取貨完成」鍵（呼叫/door-tasks/{door_task_id}/complete），
    不再透過LINE推播這顆按鈕。保留這支函式定義只是還沒清掉，沒有實際作用。
    """
    body_text = "艙門已開啟，請取出包裹" if quantity <= 1 else f"{quantity}個艙門已開啟，請取出{quantity}件包裹"
    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {"type": "text", "text": body_text, "wrap": True},
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "style": "primary",
                    "color": "#029C4D",
                    "action": {
                        "type": "postback",
                        "label": "取貨完成",
                        "data": f"action=PICKUP_DONE&package_id={package_id}",
                    },
                },
            ],
        },
    }

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=line_user_id,
                messages=[FlexMessage(alt_text="請確認取貨完成", contents=FlexContainer.from_dict(contents))],
            )
        )


PACKAGE_STATUS_LABEL_ZH = {
    "pending": "待處理，請至到貨通知訊息點選「取貨」或「預約取貨」",
    "pickup_now": "已請求取貨，管理員準備中",
    "delivering": "機器人配送中",
    "arrived": "機器人已抵達，請至LINE通知點選掃碼取貨",
    "rejected_at_door": "已退回（拒收）",
    "returned_timeout": "已退回（逾時未取）",
    "voided": "已取消（不收）",
}

# 這兩種狀態代表包裹已經送出過、被退回，還沒真正結案，才有「幾天後作廢」的倒數概念。
# voided是住戶在到貨通知當下就直接不收，包裹根本沒出過門，本身就已經是作廢狀態，
# 沒有「倒數」這件事，所以不列在這裡。
RETURNED_STATUSES_WITH_DEADLINE = ("rejected_at_door", "returned_timeout")
VOID_DEADLINE_HOURS = 72


def reply_my_packages_text(reply_token: str, packages: list):
    """
    「我的包裹」：純文字列出這個人名下所有還沒真正結束的包裹狀態，不附任何按鈕。
    退回（拒收/逾時未取）的包裹會明確標示「已退回」，並附上實際的作廢時間
    （從pending_pickup_notified_at起算72小時，這個時間點是包裹轉成退回狀態時
    系統自動推播提醒的當下記錄的，不是憑空算的）。
    只要清單裡有任何一筆退回/不收的包裹，訊息最後會加一句提醒住戶聯繫管理室
    重新派貨——因為住戶自己在LINE這邊沒有辦法重新啟用退回的包裹，
    這是刻意的設計：退回之後要不要重新送，是管理員決定的事。
    """
    lines = [f"您目前有 {len(packages)} 筆包裹：\n"]
    has_returned_or_voided = False

    for i, package in enumerate(packages, start=1):
        status_text = PACKAGE_STATUS_LABEL_ZH.get(package.status, package.status)
        block = (
            f"{i}. 門牌：{package.unit}　件數：{package.package_count}件\n"
            f"   登記時間：{package.created_at.strftime('%m/%d %H:%M')}\n"
            f"   狀態：{status_text}"
        )

        if package.status in RETURNED_STATUSES_WITH_DEADLINE:
            has_returned_or_voided = True
            if package.pending_pickup_notified_at:
                deadline = package.pending_pickup_notified_at + timedelta(hours=VOID_DEADLINE_HOURS)
                block += f"\n   將於 {deadline.strftime('%m月%d日%H時')} 由管理員作廢，請盡快處理"
            else:
                block += "\n   作廢時間尚未確定，請盡快聯繫管理室"
        elif package.status == "voided":
            has_returned_or_voided = True

        lines.append(block)

    if has_returned_or_voided:
        lines.append("\n如有領取已退回包裹的需求，請盡快聯繫管理室協助重新派貨")

    reply_text(reply_token, "\n\n".join(lines))