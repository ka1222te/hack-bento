# Apache Guacamole + guacamole-auth-json 拡張（JSON認証のみ・DB不要）
# HackBento の Attack Machine 機能から、サーバ側で生成した暗号化JSONトークンで
# 接続情報（VNC接続先・パスワード等）を渡し、Guacamole側のDBユーザ管理を不要にする。
ARG GUACAMOLE_VERSION=1.5.5
FROM guacamole/guacamole:${GUACAMOLE_VERSION}

ARG GUACAMOLE_VERSION=1.5.5
USER root

RUN curl -fsSL -o /tmp/guacamole-auth-json.tar.gz \
        "https://archive.apache.org/dist/guacamole/${GUACAMOLE_VERSION}/binary/guacamole-auth-json-${GUACAMOLE_VERSION}.tar.gz" \
    && tar -xzf /tmp/guacamole-auth-json.tar.gz -C /tmp \
    && mkdir -p /etc/guacamole/extensions \
    && cp /tmp/guacamole-auth-json-${GUACAMOLE_VERSION}/guacamole-auth-json-${GUACAMOLE_VERSION}.jar /etc/guacamole/extensions/ \
    && rm -rf /tmp/guacamole-auth-json.tar.gz /tmp/guacamole-auth-json-${GUACAMOLE_VERSION}

USER guacamole
