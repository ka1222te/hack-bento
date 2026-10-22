# Attack MachineでDocker/Docker Composeを使えるようにする手順

## Docker公式リポジトリを登録してインストール

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /tmp/docker.asc
sudo mv /tmp/docker.asc /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

※ apt-get updateでbookwormのコードネーム不一致エラーが出た場合は、lsb_release -csで確認した値に置き換えてください。

## dockerグループに追加してセッションに反映

```bash
sudo usermod -aG docker hackbento
exec su - hackbento
```

（パスワード: `hackbento` ）

## dockerdを手動起動

```bash
sudo dockerd --iptables=true --ip6tables=false --userland-proxy=false > /tmp/dockerd.log 2>&1 &
```

## 動作確認

```bash
groups
docker ps
docker compose version
```
