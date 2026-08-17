# event_type 完整清單（D-1 用）

來源：`task_logs` 實際資料，2026-08-14 快照，共 53 種、2,678 筆事件。
可直接取代 `models.py` 中過期的註解區塊。

---

## 可貼進 models.py 的版本

```python
# ── TaskLog.event_type 一覽（依 2026-08-14 資料庫實際紀錄，共 53 種）──
#
# 【建立與派送】
#   created                    包裹建立
#   queued                     排入佇列等待派送
#   door_assigned              艙門分配成功
#   door_joined                併入既有趟次（一趟多包裹）
#   multi_package_assigned     一次分配多個艙門
#   dispatched                 派送指令已送出
#   arrived                    機器人抵達門牌
#   trip_wait                  趟次等待中
#   trip_completed             整趟結束
#
# 【住戶取貨】
#   pickup_requested           住戶按下取貨
#   pickup_scheduled           住戶預約取貨
#   pickup_opened              掃碼開艙門成功
#   completed                  取貨完成
#   pending_pickup_notified    逾時未取提醒（排程）
#   schedule_reminder_sent     預約前提醒（排程）
#
# 【住戶拒收與作廢】
#   rejected                   住戶事前表示不收
#   rejected_at_door           住戶在機器人抵達後拒收
#   voided_acknowledged        作廢已確認
#
# 【退貨流程】
#   return_requested           住戶申請退貨
#   return_cancelled           住戶取消退貨
#   return_door_opened         退貨艙門開啟
#   return_retrieved           管理員確認取出退貨
#   returned                   機器人帶回
#   returned_and_opened        帶回並開門
#   returned_timeout           退貨逾時（排程）
#
# 【管理員操作】
#   door_closed                關閉艙門
#   manual_door_opened         手動開門
#   manual_door_closed         手動關門
#   door_released_manually     手動釋放艙門
#   case_closed                銷案
#   force_resolved             強制解決
#   redispatched               重新派送
#   package_deleted            刪除包裹
#   task_recalled              任務召回
#   robot_recall_requested     請求機器人召回
#   robot_recharge_requested   請求機器人回充
#
# 【LINE 綁定】
#   line_binding_updated       綁定資料更新
#   line_binding_deleted       綁定刪除
#   user_unfollowed            使用者封鎖／取消追蹤
#
# 【錯誤事件】（level=error 或 warning）
#   assign_timeout             艙門分配逾時
#   assign_timeout_failed      艙門分配逾時處理失敗
#   door_assign_failed         艙門分配失敗
#   dispatch_failed            派送失敗
#   cancel_task_failed         取消任務失敗
#   complete_failed            完成處理失敗
#   pickup_open_failed         取貨開門失敗
#   poll_returned_failed       返回輪詢失敗
#   return_failed              帶回失敗
#   return_open_failed         退貨開門失敗
#   return_door_open_failed    退貨艙門開啟失敗
#   robot_recall_failed        機器人召回失敗
#   show_qr_failed             QR Code 顯示失敗
#   notify_failed              推播失敗
```

---

## 原註解漏掉的 9 種

實際出現在資料庫、但舊註解沒列到的：

```
trip_completed          trip_wait               queued
door_joined             manual_door_opened      manual_door_closed
schedule_reminder_sent  return_open_failed      show_qr_failed
```

---

## 純字母排序版（給 D-3 的前端對照表用）

```
arrived                    pickup_scheduled
assign_timeout             poll_returned_failed
assign_timeout_failed      queued
cancel_task_failed         redispatched
case_closed                rejected
complete_failed            rejected_at_door
completed                  return_cancelled
created                    return_door_open_failed
dispatch_failed            return_door_opened
dispatched                 return_failed
door_assign_failed         return_open_failed
door_assigned              return_requested
door_closed                return_retrieved
door_joined                returned
door_released_manually     returned_and_opened
force_resolved             returned_timeout
line_binding_deleted       robot_recall_failed
line_binding_updated       robot_recall_requested
manual_door_closed         robot_recharge_requested
manual_door_opened         schedule_reminder_sent
multi_package_assigned     show_qr_failed
notify_failed              task_recalled
package_deleted            trip_completed
pending_pickup_notified    trip_wait
pickup_open_failed         user_unfollowed
pickup_opened              voided_acknowledged
pickup_requested
```

---

**備註**
中文說明是依事件名稱與實際使用情境推得，
貼進 `models.py` 前請對照程式碼確認語意無誤，特別是
`trip_wait`、`queued`、`task_recalled`、`returned_and_opened` 這幾個。
