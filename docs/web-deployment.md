# 网页服务部署准备

本项目提供部署模板，未执行实际线上部署。保持单进程运行，限流器和任务容量仅在单进程内共享；若未来使用多个 worker/副本，需要先单独设计跨进程限流。

## 本机启动

```powershell
py -3.12 -m pip install -e ".[web]"
efinance web --host 127.0.0.1 --port 8000
```

默认本机无需认证。若设置认证，用户名和密码两个环境变量必须同时存在。绑定非本机地址强制要求它们；不写入账户数据库，不在源码保存密码。

```powershell
$env:EFINANCE_WEB_USERNAME = "your-user"
$env:EFINANCE_WEB_PASSWORD = "replace-with-your-secret"
efinance web --host 0.0.0.0 --port 8000
```

## Docker

`Dockerfile.web` 安装当前源码及网页依赖，使用非 root 用户，并授权安装包内原有 `efinance/data` 缓存目录供该用户写入。缓存路径与格式不变；CI 使用隔离 Python 导入安装包并实际创建临时文件，检查缓存写权限。设置两个认证环境变量后执行：

```powershell
docker compose -f compose.web.yaml up --build -d
```

Compose 只映射 `127.0.0.1:8000`。容器内部绑定 `0.0.0.0`，因此也要求认证。没有行情结果卷或数据库服务。既有 JSON 缓存仍在包内原路径，容器重建后可重新生成。

## Nginx 与 HTTPS

服务器建议使用同源反向代理，后台仅绑定本机。以下是模板，域名和证书路径必须替换为实际值：

```nginx
server {
    listen 443 ssl;
    server_name finance.example.com;
    ssl_certificate /etc/letsencrypt/live/finance.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/finance.example.com/privkey.pem;
    client_max_body_size 16k;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 180s;
        proxy_buffering off;
    }
}
server {
    listen 80;
    server_name finance.example.com;
    return 301 https://$host$request_uri;
}
```

部署到服务器时即使应用绑定本机，也应配置两个认证环境变量，由应用验证代理传入的 Authorization。Basic 认证必须配合 HTTPS。无需开放跨域访问，不设置宽泛 CORS。不要把模板中的示例密码用于线上。

服务存活检查为 `/api/health`；开启认证后检查也需携带认证。它不代表上游数据源可用。进程日志写 stdout/stderr，交由容器或进程管理器收集；网络诊断可手动运行 `efinance doctor --online`。

运行模型参考 [FastAPI 部署文档](https://fastapi.tiangolo.com/deployment/)。首次交付以本机验证为准，Docker、TLS、反向代理和服务器访问需在实际部署环境再次验收。
