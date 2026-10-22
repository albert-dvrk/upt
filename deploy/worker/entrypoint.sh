#!/bin/sh
set -eu

home=/home/downloader
mkdir -p "$home/.ssh" /data/videos /run/sshd
cp /run/keys/downloader.pub "$home/.ssh/authorized_keys"
chown -R downloader:downloader "$home/.ssh" /data/videos
chmod 700 "$home/.ssh"
chmod 600 "$home/.ssh/authorized_keys"

ssh-keygen -A
exec /usr/sbin/sshd -D -e
