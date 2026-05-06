# TaskHub 新服务器部署清单

本项目是 Django 应用，推荐在服务器上使用 Gunicorn + Nginx + MySQL 部署。

## 上传到新的 GitHub 仓库

先确认本地状态：

```bash
git status
```

如果要彻底切到新仓库：

```bash
git remote rename origin old-origin
git remote add origin <新的 GitHub 仓库地址>
git add .env.example .gitignore core/settings.py docs/deployment.md
git commit -m "Prepare production deployment"
git push -u origin main
```

如果新仓库不是空仓库，先同步再推送：

```bash
git pull --rebase origin main
git push -u origin main
```

## 服务器基础环境

下面以 Ubuntu / Debian 为例。按实际情况替换 `/srv/taskhub`、仓库地址和域名。

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-dev build-essential nginx mysql-server
sudo mkdir -p /srv/taskhub
sudo chown "$USER":"$USER" /srv/taskhub
git clone <新的 GitHub 仓库地址> /srv/taskhub
cd /srv/taskhub
python3 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
python - <<'PY'
from django.core.management.utils import get_random_secret_key
print(get_random_secret_key())
PY
```

在服务器上编辑 `.env`：

```dotenv
MYSQL_DATABASE=taskhub
MYSQL_USER=taskhub_user
MYSQL_PASSWORD=<MySQL 密码>
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
SECRET_KEY=<刚生成的 SECRET_KEY>
DJANGO_DEBUG=0
ALLOWED_HOSTS=<你的域名>,127.0.0.1,localhost
CSRF_TRUSTED_ORIGINS=https://<你的域名>
SECURE_SSL_REDIRECT=0
SESSION_COOKIE_SECURE=1
CSRF_COOKIE_SECURE=1
```

初始化 Django：

```bash
. /srv/taskhub/.venv/bin/activate
cd /srv/taskhub
python manage.py check_env
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
```

## Gunicorn 服务

创建 `/etc/systemd/system/taskhub.service`：

```ini
[Unit]
Description=TaskHub Gunicorn
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/srv/taskhub
EnvironmentFile=/srv/taskhub/.env
ExecStart=/srv/taskhub/.venv/bin/gunicorn core.wsgi:application --bind 127.0.0.1:8000 --workers 3
Restart=always

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo chown -R www-data:www-data /srv/taskhub
sudo systemctl daemon-reload
sudo systemctl enable --now taskhub
sudo systemctl status taskhub
```

## Nginx 配置

创建 `/etc/nginx/sites-available/taskhub`：

```nginx
server {
    listen 80;
    server_name <你的域名>;

    client_max_body_size 50m;

    location /static/ {
        alias /srv/taskhub/staticfiles/;
    }

    location /media/ {
        alias /srv/taskhub/media/;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        add_header X-Frame-Options SAMEORIGIN always;
    }
}
```

启用站点：

```bash
sudo ln -s /etc/nginx/sites-available/taskhub /etc/nginx/sites-enabled/taskhub
sudo nginx -t
sudo systemctl reload nginx
```

配置 HTTPS 后，把服务器 `.env` 调整为：

```dotenv
CSRF_TRUSTED_ORIGINS=https://<你的域名>
SECURE_SSL_REDIRECT=1
SECURE_HSTS_SECONDS=31536000
```

最后重启：

```bash
sudo systemctl restart taskhub
```
