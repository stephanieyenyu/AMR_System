/* ============================================================
 * common.js — 四個管理頁面共用的小工具函式
 *
 * 目前先放 withButtonFeedback（按鈕點擊時顯示 loading 文字、避免重複點擊），
 * 之後搬其他頁面（報表/例外處理/Dashboard）時，會把各頁重複的
 * fetch 錯誤處理、門牌篩選等邏輯陸續搬進來，四頁共用同一份。
 * ============================================================ */

/**
 * 包住一個非同步操作，執行期間把按鈕disable、文字換成「更新中...」，
 * 結束後（不管成功失敗）自動還原，避免使用者連續點擊造成重複請求。
 */
async function withButtonFeedback(button, fn, loadingText) {
  const originalText = button.textContent;
  button.disabled = true;
  button.textContent = loadingText || '更新中...';
  try {
    await fn();
  } finally {
    button.textContent = originalText;
    button.disabled = false;
  }
}
