from ldap3 import Server,Connection,SASL,DIGEST_MD5,Tls,ALL,SUBTREE,MODIFY_ADD,MODIFY_REPLACE # LDAP認証に関するモジュール
from ldap3.utils.dn import escape_rdn
import ssl # SSLに関するモジュール
import os,sys # osに関するモジュール
import base64 # base64に関するモジュール
import hashlib # ハッシュ関数に関するモジュール

class LDAPClient():
    def __init__(self,ldap_uri,top_domain,admin_name="admin",mail_domain="example.com",ou_user="people",ou_group="groups",ssh_attr_name=None): # コンストラクタ
        self.ldap_uri = ldap_uri # 接続先LDAPのURI
        self.top_domain = top_domain # トップ識別ドメイン
        self.admin_name = admin_name # 管理者ユーザ名
        self.mail_domain = mail_domain # メールアドレスドメイン
        self.ou_user = ou_user # 組織(ユーザ)
        self.ou_group = ou_group # 組織(グループ)
        self.ssh_attr_name = ssh_attr_name # SSH公開鍵の属性名

    @staticmethod
    def hashed(string,num=256,digit=16,salt=b""): # ハッシュ化
        if num==256: # SHA-256
            sha256 = hashlib.sha256(string.encode("utf-8")+salt) # ハッシュ化を行う(ログイン認証はこちら)
            if digit==16: return sha256.hexdigest() # 16進数の文字列で返す
            elif digit==2: return sha256.digest() # 2進数の文字列で返す
        elif num==1: # SHA-1
            sha1 = hashlib.sha1(string.encode("utf-8")+salt)  # ハッシュ化を行う(websocket通信で使用するSHA-1はこちら)
            if digit==16: return sha1.hexdigest() # 16進数の文字列で返す
            elif digit==2: return sha1.digest() # 2進数の文字列で返す
        return None # 該当暗号化無し

    @staticmethod
    def ssha_hash(string): # ハッシュ化(SSHA)
        salt = os.urandom(4) # 4バイトのランダムなソルト生成
        hashed_bytes = LDAPClient.hashed(string,num=1,digit=2,salt=salt) # SHA1でハッシュ化(2進数出力s)
        return "{SSHA}"+base64.b64encode(hashed_bytes+salt).decode('utf-8') # ハッシュ化したバイトデータにソルトを再度加えてbase64エンコードしutf-8デコード

    def bind(self,bind_name,bind_password,is_admin=False): # LDAPバインド
        try:
            use_ssl = self.ldap_uri.lower().startswith("ldaps://") # ldaps:// のときのみ TLS
            tls_configuration = Tls(validate=ssl.CERT_NONE) if use_ssl else None # TLS設定
            server = Server(self.ldap_uri,use_ssl=use_ssl,tls=tls_configuration,get_info=ALL) # 接続先LDAPサーバ
            safe_name = escape_rdn(bind_name) # DN インジェクション対策
            if is_admin: bind_dn = f"cn={safe_name},{self.top_domain}" # 識別子
            else: bind_dn = f"uid={safe_name},ou={self.ou_user},{self.top_domain}" # 識別子

            #conn = Connection(server,authentication=SASL,sasl_mechanism='SCRAM-SHA-1',sasl_credentials=(bind_name,"a3.yamanashi.ac.jp",bind_password)) # LDAP接続
            if bind_name is None and bind_password is None and not is_admin: # anonymousで接続時
                conn = Connection(server) # LDAP接続
            else: # 通常ユーザ/管理者で接続時
                conn = Connection(server,user=bind_dn,password=bind_password)  # 通常ユーザ/管理者で接続
            return conn # セッションを返す
        except Exception as e: # 何かしらの例外発生時
            msg = f"Error(ldap_bind) : {e}" # デバッグ用
            raise Exception(msg)

    def authenticate(self,user_name,user_password,is_admin=False,admin_password=None): # LDAP認証
        bind_name = self.admin_name if is_admin else user_name # 実行ユーザ名
        bind_password = admin_password if is_admin else user_password # 実行ユーザパスワード
        conn = self.bind(bind_name,bind_password,is_admin) # LDAPバインド
        if conn is None or not conn.bind(): return False # LDAP接続失敗
        if hasattr(conn,"user") and (conn.user is None or conn.user==""): return False # anonymous接続も無効
        conn.unbind() # バインド解除
        return True # LDAP認証成功

    def whoami(self,user_name,user_password,is_admin=False,admin_password=None): # ユーザ名取得
        bind_name = self.admin_name if is_admin else user_name # 実行ユーザ名
        bind_password = admin_password if is_admin else user_password # 実行ユーザパスワード
        conn = self.bind(bind_name,bind_password,is_admin) # LDAPバインド
        if conn is None or not conn.bind(): return None # LDAP接続失敗
        if hasattr(conn,"user") and (conn.user is None or conn.user==""): return None # anonymous接続も無効
        try:
            conn.extended("1.3.6.1.4.1.4203.1.11.3")
            result = conn.result # コマンド結果取得
            conn.unbind() # バインド解除
            return result # 実行結果を返す
        except Exception as e: # 何かしらの例外発生時
            msg = f"Error(ldap_whoami) : {e}" # デバッグ用
            raise Exception(msg)

    def search(self,user_name,search_filter="(uid=*)",user_password="aaa",is_admin=False,admin_password=None): # ユーザ属性検索
        bind_name = self.admin_name if is_admin else user_name # 実行ユーザ名
        bind_password = admin_password if is_admin else user_password # 実行ユーザパスワード
        conn = self.bind(bind_name,bind_password,is_admin) # LDAPバインド
        if conn is None or not conn.bind(): return None # LDAP接続失敗
        user_dn = f"{self.top_domain}" # 検索する識別子
        try:
            conn.search(user_dn,search_filter,search_scope=SUBTREE,attributes=["*"]) # ユーザ検索
            result = conn.entries # コマンド結果取得
            conn.unbind() # バインド解除
            return result # 実行結果を返す
        except Exception as e: # 何かしらの例外発生時
            msg = f"Error(ldap_search) : {e}" # デバッグ用
            raise Exception(msg)
    
    def add(self,entry_name,last_name,first_name,display_name=None,entry_password="aaa",is_admin=False,admin_password=None): # ユーザ追加
        bind_name = self.admin_name if is_admin else entry_name # 実行ユーザ名
        bind_password = admin_password if is_admin else entry_password # 実行ユーザパスワード
        conn = self.bind(bind_name,bind_password,is_admin) # LDAPバインド
        if conn is None or not conn.bind(): return None # LDAP接続失敗
        if hasattr(conn,"user") and (conn.user is None or conn.user==""): return None # anonymous接続も無効
        entry_user_dn = f"uid={entry_name},ou={self.ou_user},{self.top_domain}" # 追加する識別子
        if display_name is None: display_name = last_name # 表示名がない場合，性とする
        entry_password = LDAPClient.ssha_hash(entry_password) # パスワードをハッシュ化
        user_dn = f"{self.top_domain}" # 検索する識別子
        number_init = 2001 # 数値初期値
        number = number_init # ユーザid
        while True: # Numberが決まるまでループ
            try:
                conn.search(user_dn,f"(gidNumber={number})",search_scope=SUBTREE,attributes=["*"]) # ユーザ検索
                result = conn.entries # コマンド結果取得
                if result is None or len(result)==0: break # 結果が得られないときブレイク
                number += 1 # 数値をインクリメント
            except Exception as e: raise e # 何かしらの例外発生時エラーを投げる
        entry_user_attributes = {
                "objectClass" : ["inetOrgPerson","posixAccount","shadowAccount","ldapPublicKey"],
                "cn" : f"{last_name} {first_name}",
                "sn" : f"{last_name}",
                "givenName" : f"{first_name}",
                "displayName" : f"{display_name}",
                "userPassword" : f"{entry_password}",
                "loginShell" : "/bin/bash",
                "uidNumber" : f"{number}",
                "gidNumber" : f"{number}",
                "mail" : f"{entry_name}@{self.mail_domain}",
                "homeDirectory" : f"/home/{entry_name}",
                "shadowExpire" : -1, # 0:常にアカウント無効，-1:常にアカウント有効
            }
        if self.ssh_attr_name is not None: entry_user_attributes[self.ssh_attr_name] = f"" # SSH公開鍵の属性追加
        entry_group_dn =  f"cn={entry_name},ou={self.ou_group},{self.top_domain}" # 追加する識別子
        entry_group_attributes = {
                "objectClass" : "posixGroup",
                "cn" : f"{entry_name}",
                "gidNumber" : f"{number}",
                "memberUid" : f"{number}"
            }
        try:
            result= {} # 追加結果
            conn.add(entry_user_dn,attributes=entry_user_attributes) # ユーザ追加
            result["user"] = conn.result # コマンド結果取得
            conn.add(entry_group_dn,attributes=entry_group_attributes) # グループ追加
            result["group"] = conn.result # コマンド結果取得
            conn.unbind() # バインド解除
            return result # 実行結果を返す
        except Exception as e: # 何かしらの例外発生時
            msg = f"Error(ldap_add) : {e}" # デバッグ用
            raise Exception(msg)

    def modify(self,user_name,update_attributes,user_password="aaa",is_admin=False,admin_password=None): # ユーザ属性変更
        bind_name = self.admin_name if is_admin else user_name # 実行ユーザ名
        bind_password = admin_password if is_admin else user_password # 実行ユーザパスワード
        conn = self.bind(bind_name,bind_password,is_admin) # LDAPバインド
        if conn is None or not conn.bind(): return None # LDAP接続失敗
        if hasattr(conn,"user") and (conn.user is None or conn.user==""): return None # anonymous接続も無効
        user_dn = f"uid={user_name},ou={self.ou_user},{self.top_domain}" # 変更する識別子
        try:
            conn.modify(user_dn,update_attributes) # ユーザ属性変更
            result = conn.result # コマンド結果取得
            conn.unbind() # バインド解除
            return result # 実行結果を返す
        except Exception as e: # 何かしらの例外発生時
            msg = f"Error(ldap_modify) : {e}" # エラーメッセージ
            raise Exception(msg)

    def delete(self,user_name,user_password="aaa",is_admin=False,admin_password=None): # ユーザ削除
        bind_name = self.admin_name if is_admin else user_name # 実行ユーザ名
        bind_password = admin_password if is_admin else user_password # 実行ユーザパスワード
        conn = self.bind(bind_name,bind_password,is_admin) # LDAPバインド
        if conn is None or not conn.bind(): return None # LDAP接続失敗
        if hasattr(conn,"user") and (conn.user is None or conn.user==""): return None # anonymous接続も無効
        user_dn = f"uid={user_name},ou={self.ou_user},{self.top_domain}" # 削除する識別子
        group_dn =  f"cn={user_name},ou={self.ou_group},{self.top_domain}" # 削除する識別子
        try:
            result= {} # 追加結果
            conn.delete(user_dn) # ユーザ追加
            result["user"] = conn.result # コマンド結果取得
            conn.delete(group_dn) # グループ追加
            result["group"] = conn.result # コマンド結果取得
            conn.unbind() # バインド解除
            return result # 実行結果を返す
        except Exception as e: # 何かしらの例外発生時
            msg = f"Error(ldap_delete) : {e}" # デバッグ用
            raise Exception(msg)

def main(): # main関数
    "環境変数"
    from dotenv import load_dotenv # 環境変数に関するモジュール
    load_dotenv() # .envから環境変数読み込み
    ldap_uri = os.getenv("LDAP_URI") # 接続先LDAPのURI読み込み
    top_domain = os.getenv("TOP_DOMAIN") # トップ識別ドメイン読み込み
    admin_name = os.getenv("ADMIN_NAME","admin") # 管理者名読み込み
    mail_domain = os.getenv("MAIL_DOMAIN","example.com") # メールアドレスドメイン読み込み
    ou_user = os.getenv("OU_USER","people") # ユーザユニット名読み込み
    ou_group = os.getenv("OU_GROUP","groups") # グループユニット名読み込み
    admin_password = os.getenv("ADMIN_PASSWORD",None) # 管理者パスワード読み込み
    ssh_attr_name = os.getenv("SSH_ATTR_NAME",None) # ssh公開鍵の属性名読み込み
    ldap_client = LDAPClient(ldap_uri,top_domain,admin_name,mail_domain,ou_user,ou_group,ssh_attr_name) # LDAPクライアント

    "認証テスト"
    user_name = None # ユーザ名
    name = user_name or "Anonymous" # ユーザ名(デバッグ用)
    user_password = None # ユーザ名に対するパスワード
    if ldap_client.authenticate(user_name,user_password): # LDAP認証(認証成功時)
        print(f"{name}: 認証に成功しました") # デバッグ用
    else: # 認証失敗時
        print(f"{name}: 認証に失敗しました") # デバッグ用
    
    "ユーザ追加&パスワード変更&削除テスト"
    entry_name = "tmp" # 追加/削除ユーザ名(学籍番号)
    entry_password = "tmp" # 追加パスワード  
    entry_new_password = "aaa" # 変更後パスワード  
    result = ldap_client.add(entry_name,"t","m","p",entry_password,is_admin=True,admin_password=admin_password) # ユーザ追加(uid，lats_name，first_name，ユーザパスワードの順)
    if result is not None and result["user"]["description"]=="success" and result["group"]["description"]=="success":
        print(f"ユーザ[{entry_name}]追加に成功しました\n") # デバッグ用
        print("変更前ユーザ")
        print(ldap_client.search(entry_name,search_filter=f"(uid={entry_name})",user_password=entry_password,is_admin=False,admin_password=admin_password),end="\n\n") # ユーザ検索
        
        update_attributes = { # 変更属性
            "userPassword" : [(MODIFY_REPLACE,[LDAPClient.ssha_hash(entry_new_password)])],
        } 
        result = ldap_client.modify(entry_name,update_attributes,user_password=entry_password,is_admin=False,admin_password=admin_password) # ユーザ属性変更
        
        print("変更後ユーザ") # デバッグ用
        print(ldap_client.search(entry_name,search_filter=f"(uid={entry_name})",user_password=entry_new_password,is_admin=False,admin_password=admin_password),end="\n\n") # ユーザ検索    
        result = ldap_client.delete(entry_name,user_password=entry_password,is_admin=True,admin_password=admin_password) # ユーザ削除
        if result is not None and result["user"]["description"]=="success" and result["group"]["description"]=="success":
            print(f"ユーザ[{entry_name}]削除に成功しました\n") # デバッグ用
        elif result is not None: print(f"[Error: delete_user {entry_name}] user->{result['user']['description']}, group->{result['group']['description']}") # エラー内容表示
        else: print(f"[Error: delete_user {entry_name}] LDAPの接続に失敗しました") # エラー内容表示
    elif result is not None: print(f"[Error: add_user {entry_name}] user->{result['user']['description']}, group->{result['group']['description']}") # エラー内容表示
    else: print(f"[Error: add_user {entry_name}] LDAPの接続に失敗しました") # エラー内容表示

    print("ユーザ一覧") # デバッグ用
    print(ldap_client.search(user_name,search_filter="(uid=*)",user_password=user_password,is_admin=True,admin_password=admin_password),end="\n\n") # ユーザ検索   

if __name__=="__main__": # メイン実行時
    main() # メイン関数実行
