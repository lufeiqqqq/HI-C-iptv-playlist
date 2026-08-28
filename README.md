<!-- D:\AllCode\project\Python\M3U\HI-C-iptv-playlist\README.md -->
# HI-C IPTV Playlist

个人 IPTV 订阅源，每 12 小时自动更新。

## 订阅地址
https://raw.githubusercontent.com/mingliu91/aptv-playlist/refs/heads/main/playlist.m3u

## 本地运行
bash 
pip install -r requirements.txt 
python scripts/build.py

## 自动更新

GitHub Actions 每 12 小时自动构建并推送 `playlist.m3u`。