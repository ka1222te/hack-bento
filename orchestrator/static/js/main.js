// shared utility — each page's inline script handles its own logic

// 環境（コンテナ/VM）の起動・停止・延長が行われたことを他タブ・他ページの
// ホーム画面(index.html)へ通知する。localStorageのstorageイベントは他タブでのみ
// 発火するため、自タブは呼び出し元で直接refreshする。
function notifyEnvsChanged() {
  localStorage.setItem('hb_envs_changed', String(Date.now()));
}

// クエリパラメータ等から受け取った "次に遷移する先" の値を検証する。
// オープンリダイレクト対策として、同一オリジンの相対パス（"/"始まり・"//"や"/\"は不可）のみ許可する。
function safeNextPath(value) {
  if (!value) return null;
  if (!value.startsWith('/')) return null;
  if (value.startsWith('//') || value.startsWith('/\\')) return null;
  return value;
}
