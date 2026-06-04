"""サイトのURLパスと競合するユーザ名の予約語リスト。"""

RESERVED_USERNAMES: frozenset[str] = frozenset({
    # トップレベルのページルート
    "health", "login", "logout", "explore", "new", "admin", "user",
    "setup-username", "setup_username",
    # API プレフィックス
    "api",
    # 静的ファイル
    "static",
    # 汎用的に問題になりそうな名前
    "root", "system", "hackbento", "support", "help",
})


def is_reserved(username: str) -> bool:
    return username.lower() in RESERVED_USERNAMES
