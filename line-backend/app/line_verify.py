"""
驗證 LIFF 傳來的 ID Token，確認是真的LINE使用者身份
"""
import requests

LINE_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"


def verify_liff_id_token(id_token: str, channel_id: str) -> dict:
    """
    向LINE官方驗證ID Token，成功會回傳裡面包含的資訊(claims)，
    其中 claims['sub'] 就是這個用戶的 LINE User ID。
    驗證失敗會丟出例外。

    這裡刻意把requests.post()本身用try/except包住：原本只有處理
    「拿到回應、但狀態碼不是200」這種情況，沒有處理「根本連不上」
    （Timeout、ConnectionError等requests.exceptions.RequestException）。
    後者不是ValueError，main.py那邊的呼叫端只接住except ValueError，
    如果不在這裡先轉換，網路層例外會直接竄出去變成沒被接住的500，
    住戶在LIFF頁面會看到原始錯誤文字而不是清楚的中文訊息。
    統一轉成ValueError之後，不管是「LINE官方明確拒絕」還是「根本連不上」，
    呼叫端都能用同一個except ValueError接住，不用改main.py的程式碼。
    """
    try:
        resp = requests.post(
            LINE_VERIFY_URL,
            data={"id_token": id_token, "client_id": channel_id},
            timeout=5,
        )
    except requests.exceptions.RequestException as e:
        raise ValueError(f"無法連線到LINE驗證伺服器: {e}")

    if resp.status_code != 200:
        raise ValueError(f"ID Token 驗證失敗: {resp.text}")
    return resp.json()