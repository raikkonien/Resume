# Resume

个人简历维护、附件存储与只读 MCP 服务，基于 FastAPI。管理员创建个人账号并配置全局 OCR 服务，各个人账号维护自己的独立档案。

## 镜像发布状态与前提

**本地双架构镜像已构建并验证。** `linux/amd64` 和 `linux/arm64` 已完成空目录启动、管理员登录、容器重建及 PDF 导出验证；109 项回归测试通过。发布源码仓库为 [raikkonien/Resume](https://github.com/raikkonien/Resume)，镜像地址为 `ghcr.io/raikkonien/resume:latest`。首次公开发布需要 Actions 构建成功、Package 可见性设为 **Public**，并完成无鉴权拉取验证；下方 GHCR 安装命令以这些条件完成为前提。发布状态可在仓库 Actions 与 Packages 页面查看；尚未公开时可使用下方离线包。若发布至其他账号或仓库，请将 `raikkonien/resume` 换成实际的小写 `owner/repository`。

宿主机需安装并启动 Docker Engine；macOS / Windows 可安装 Docker Desktop，Windows 使用 Linux 容器。可选的 Compose 用法需要 Docker Compose v2。发行目标为 `linux/amd64` 和 `linux/arm64`，发布命令会同时构建这两种架构。

使用公开镜像时只需一个可写的宿主机目录：它挂载至容器 `/data`，保存数据库、账号、附件、OCR 配置和会话密钥。无需源码、`.env`、预设管理员密码、Token 或 UID/GID 配置。Dockerfile 构建上下文采用白名单，镜像不包含宿主机 `data/`、`.env` 或 Git 历史。

### 现在使用离线镜像包

本次提供的离线镜像包不含个人数据。Intel / AMD 电脑（含常见 Windows、Linux、Intel Mac）使用 `dist/resume-amd64.tar.gz`，Apple Silicon 或 ARM64 Linux 使用 `dist/resume-arm64.tar.gz`。以下以 amd64 为例；ARM64 只需替换导入文件名，镜像名和目录挂载方式相同：

```sh
docker load --input resume-amd64.tar.gz
mkdir -p "$HOME/resume-data"
docker run -d --name resume \
  --publish 127.0.0.1:18081:8000 \
  --mount "type=bind,source=$HOME/resume-data,target=/data" \
  resume:distribution
```

Windows 导入命令相同，启动时沿用下方 PowerShell 的路径写法。下载地址：[GitHub Releases](https://github.com/raikkonien/Resume/releases)。单架构包也适用于旧版 Docker，避免加载多架构归档时丢失目标架构；直接从 GHCR 拉取则自动选择对应架构。`dist/resume-source.zip` 是不含数据、密钥及本地规划记录的发布源码包；镜像包和源码包均不应提交到 Git 仓库。`dist/SHA256SUMS` 用于校验这些文件。


## 安装：一个目录、一个容器

以下命令用于**新安装**。如果机器已有名为 `resume` 的容器或正在使用 18081 端口，不要覆盖或停止它；请使用新的容器名、独立目录和空闲端口。尤其不要把测试容器连接到正在运行的生产数据目录。

### Linux / macOS

```sh
mkdir -p "$HOME/resume-data"
docker run -d --name resume \
  --publish 127.0.0.1:18081:8000 \
  --mount "type=bind,source=$HOME/resume-data,target=/data" \
  ghcr.io/raikkonien/resume:latest
```

`$HOME/resume-data` 是绝对路径。也可使用其他可写绝对路径，例如 `--mount "type=bind,source=/Volumes/Ext SSD/resume-data,target=/data"`；目录必须先创建，整个 `--mount` 参数用引号包住以支持空格。容器会写入这个目录，宿主机权限、Docker Desktop 文件共享或 SELinux 策略也必须允许访问；不需要额外挂载第二个目录。

### Windows PowerShell

```powershell
$DataPath = Join-Path $HOME "resume-data"
New-Item -ItemType Directory -Force -Path $DataPath | Out-Null
docker run -d --name resume `
  --publish 127.0.0.1:18081:8000 `
  --mount "type=bind,source=$DataPath,target=/data" `
  ghcr.io/raikkonien/resume:latest
```

`$DataPath` 是绝对路径，带空格时保留引号。PowerShell 续行反引号后不能留空格。

### 首次登录

访问 <http://localhost:18081>，从日志取得初始管理员密码：

```sh
docker logs resume
```

全新空目录只创建管理员 `admin`，生成独立随机密码，日志中显示 `首次启动管理员账号 admin，初始密码（仅显示一次）`。应用只在第一次成功初始化时输出一次，不会在重启时再次生成；旧容器日志仍可能保留该条消息。请妥善保存密码，不要公开日志。

使用 `admin` 登录，在「系统管理 → 用户管理」中：

1. 修改管理员密码，保存后重新登录。密码为 8–256 位，建议使用长随机密码。
2. 创建自己的个人账号。用户名为 3–32 位小写英文字母、数字、下划线或连字符，以字母开头。
3. 退出管理员，使用个人账号登录，维护新建的空白个人档案。

管理员负责账号与全局 OCR 设置，不提供个人档案；`admin` 不能改名或删除。账号改名或重设密码会使该账号的现有登录失效。删除个人账号会永久删除其全部资料和附件。

镜像自带健康检查，无需安装 `curl`。`docker ps` 会显示 `starting` / `healthy` / `unhealthy`；详细状态可查看：

```sh
docker inspect --format '{{json .State.Health}}' resume
```

健康检查请求容器内部 `/health`，代表 HTTP 应用能够响应，不代表外部 OCR 服务可用。健康检查不会自动重启容器。

## 可选 OCR 与 MCP

### OCR：在管理员界面配置

在「系统管理 → OCR模型设置」填写 OpenAI 兼容 Chat Completions 接口的完整地址（例如 `https://your-service.example/v1/chat/completions`）、模型名称及可选 API Key。**先测试当前配置，测试成功后才能保存**。所有个人账号共用这份配置，保存在 `/data/resume.db`，升级和重启后保留。

已保存 API Key 不回显明文，留空保留原 Key，也可勾选清除。镜像不包含 OCR 模型，新安装的 OCR 默认未配置。管理员需提供从容器网络可访问、支持文档图像内容的兼容视觉 API、模型名称及其所需的 API Key。容器中的 `localhost` 指容器自己；连接 Docker Desktop 宿主机服务通常使用 `host.docker.internal`，Linux 需按实际网络配置可达地址。手工录入、附件上传和 PDF 导出不需要模型服务。

OCR 会把所选文档内容发送至指定服务，配置前确认服务可信、数据使用约定符合要求。数据库备份包含 OCR API Key，应按敏感资料保护。

### MCP：只读服务，当前没有 Token 管理界面

MCP 提供当前鉴权用户的简历与附件只读访问。**当前管理 UI 不提供个人账号 Token 的创建、查看或轮换**；不要把普通账号密码当作 Bearer Token。全新安装不要求启用 MCP，也不要求设置 `MCP_TOKEN`。

`MCP_TOKEN` 仅用于旧版首次迁移时绑定原账号 `zhangrui`，初始化完成后更改它不会创建或轮换凭据。已持有迁移 Token 的客户端使用 `Authorization: Bearer <token>`；读取返回的附件链接也需要相同请求头。

可选环境变量 `PUBLIC_BASE_URL` 决定 MCP 返回的附件链接前缀，默认 `http://localhost:18081`；`MCP_ALLOWED_HOSTS` 控制 MCP 允许的 Host，默认仅 localhost / 回环地址。如果调整端口或使用反向代理域名，应同步配置实际对外地址与允许的 Host，避免使用无约束的通配域名。这些是容器环境设置，不是 UI 选项。

## 数据与可选环境变量

所有应用持久状态都在同一 `/data` 挂载中：

```text
/data/
  resume.db                 # SQLite：账号、资料、OCR 设置、MCP 凭据等
  .session-key              # SECRET_KEY 未指定时自动创建的持久会话密钥
  <username>/               # 各个人账号附件目录
    <section>/<entry-id>/   # 按板块、记录保存附件
```

SQLite 运行时可能产生日志或辅助文件；备份应包含**整个目录及隐藏文件**。`.session-key` 会在重启、重建容器后复用。勿删除、修改或公开它；损坏时应从备份恢复。运行多个容器同时使用同一个目录不属于本文的部署方式。

环境变量完全可选，可用 `docker run -e NAME=value` 或自行保管的 `--env-file /absolute/path/private.env` 传入；不要把秘密写入镜像或提交仓库。`.env.example` 是可选模板，而不是首次安装的前置条件。

| 变量 | 用途 |
| --- | --- |
| `INITIAL_ADMIN_PASSWORD` | 仅首次初始化时指定 `admin` 密码；留空生成并记录随机密码。之后修改变量不会重设已有密码。 |
| `SECRET_KEY` | 可选显式会话密钥；留空使用 `/data/.session-key`。显式提供时需自行长期保存，更改会使已有会话失效。 |
| `PUBLIC_BASE_URL` | MCP 附件链接的对外地址，默认 `http://localhost:18081`。 |
| `MCP_ALLOWED_HOSTS` | MCP 允许的 Host，逗号分隔，默认 `localhost:*,127.0.0.1:*,[::1]:*`。 |
| `MAX_UPLOAD_MB` | 单个上传文件大小上限，默认 20 MB。 |
| `ADMIN_PASSWORD` | **仅旧版首次迁移**时保留原账号密码，不是新安装的必填项。 |
| `MCP_TOKEN` | **仅旧版首次迁移**时绑定原个人账号的只读 Token。 |

容器内 `DATA_DIR` 保持 `/data`。不需要通过环境变量改变存储位置，只需更换宿主机挂载目录。

### 旧版数据迁移

旧版无账号数据（包括旧 `uploads/`）首次迁移需要 `ADMIN_PASSWORD`，否则应用会拒绝启动以避免错误归属。迁移创建个人账号 `zhangrui`，把旧资料及附件归属到该账号，并独立创建 `admin`；可选 `MCP_TOKEN` 也仅绑定 `zhangrui`。空目录不设置 `ADMIN_PASSWORD`，就不会创建该旧账号。

先停止旧应用并完整备份，以**备份副本**和独立容器测试迁移，再安排实际切换。不要让新旧实例同时写同一数据目录，不要把真实数据或原 `.env` 用作构建上下文或发布文件。迁移控制变量不负责重设已存在的账号或 Token，之后通过管理页修改账号密码。

## 停止、启动与安全升级

```sh
docker stop resume
docker start resume
```

默认无自动重启策略：手动停止后不会自己启动，Docker / 主机重启后需要手动启动。停止或删除容器不等于删除宿主机挂载目录。

### 先停止，再备份

以下是 Linux / macOS 示例，务必在容器停止后执行；`docker cp` 能读取容器内 root 所有的 `.session-key`，避免 Linux 宿主机直接打包时缺少读取权限，并复制整个目录及隐藏文件。Windows 可采用同样的 `docker cp` 方式并替换备份路径。

```sh
docker stop resume
BACKUP_DIR="$HOME/resume-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
docker cp resume:/data/. "$BACKUP_DIR"
docker start resume
```

保管备份时按敏感资料处理。恢复时先停止应用，把完整备份目录（包括隐藏文件）复制到空的可写目录，再将该目录挂载至 `/data`；不要把备份复制进正在使用的数据目录。

### 使用新镜像重建容器

确认新版本发布后，先拉取，再停止和备份，最后仅替换容器，保留原挂载路径：

```sh
docker pull ghcr.io/raikkonien/resume:latest
docker stop resume
BACKUP_DIR="$HOME/resume-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
docker cp resume:/data/. "$BACKUP_DIR"
docker rm resume
docker run -d --name resume \
  --publish 127.0.0.1:18081:8000 \
  --mount "type=bind,source=$HOME/resume-data,target=/data" \
  ghcr.io/raikkonien/resume:latest
docker logs resume
docker inspect --format '{{json .State.Health}}' resume
```

如果初次部署使用了其他名称、路径、端口或可选环境变量，重建时必须沿用对应参数，尤其是显式设置的 `SECRET_KEY`。勿删除宿主机目录。需要固定版本时改用已发布的版本标签或 `@sha256:...` 摘要；`latest` 是可变标签。若数据库格式变更，回退镜像可能不足以回滚，应保留升级前的完整备份。

## Compose（可选）

只需取得本仓库的 `docker-compose.yml`，无需下载应用源码或创建 `.env`。默认使用预期 GHCR 镜像、`./data:/data` 和 `127.0.0.1:18081:8000`，继承镜像健康检查，无自动重启。以下命令同样以首次公开发布已完成为前提：

```sh
mkdir -p ./data
docker compose pull
docker compose up -d
docker compose logs resume
```

`./data` 相对 Compose 文件所在目录解析。若要复用上文 `docker run` 的数据目录，先停止原容器，再指定相同的绝对路径，并处理已有同名容器；不要并行启动两个实例。

支持可选 `IMAGE`、`DATA_PATH`、`BIND_ADDR`、`PORT` 及上表环境变量；可以在 shell 中设置，也可以自行复制 `.env.example` 为 `.env`。例如 Linux / macOS：

```sh
DATA_PATH="$HOME/resume-data" IMAGE=resume:local docker compose up -d --pull never
```

此示例使用文末构建的本地镜像，不需要 GHCR。`DATA_PATH` 选择宿主机目录，容器内始终是 `/data`。改为 `PORT=18082` 时，还应设置 `PUBLIC_BASE_URL=http://localhost:18082`。

日常停止 / 启动使用 `docker compose stop` / `docker compose start`。升级时先 `docker compose pull`，再 `docker compose stop`、完整备份所选数据目录，最后 `docker compose up -d`。`docker compose down` 移除容器后，bind mount 数据仍保留；不要清空宿主机目录。

## 端口与安全

默认只绑定 `127.0.0.1:18081`，仅供宿主机访问。不要无意改成 `0.0.0.0` 或去掉 IP 前缀：这会把登录页、附件和 API 暴露至宿主机可达网络。若已有生产服务占用 18081，测试使用其他端口，切勿为了安装而停止生产服务。

跨机器访问建议由受控反向代理提供 HTTPS，并配置防火墙、强密码与实际 `PUBLIC_BASE_URL` / `MCP_ALLOWED_HOSTS`。容器内仍需监听 `0.0.0.0:8000` 才能接受 Docker 端口转发，访问范围由宿主机发布地址控制。日志含首次密码，数据库含个人资料和 OCR 凭据；勿公开日志、备份、数据目录或 `.env`。

## 从源码构建与双架构发布

本地构建与运行已验证，下面的上传操作尚未执行。源码构建需 Docker 和仓库的 `Dockerfile`、`requirements.txt`、`requirements.lock`、`app/`：

```sh
docker build -t resume:local .
```

本地运行时把安装命令末尾镜像改为 `resume:local`，使用独立空目录及空闲端口。`requirements.txt` 是直接依赖清单，`requirements.lock` 是包含间接依赖的固定版本安装清单；镜像从 lock 安装。变更依赖时必须同步更新两者，并测试启动、回归功能与两种目标架构，不能只改单一文件。

Dockerfile 同时固定 Python 基础镜像的多架构 manifest 摘要，避免标签漂移改变构建输入。维护时定期更新基础镜像以获得安全修复：确认新的 `python:3.12-slim` manifest 包含 amd64 / arm64 后更新 `FROM` 摘要，并重新构建、测试两种架构；只重新构建而不更新摘要不会取得新的基础镜像修复。

### 推荐：GitHub Actions 自动发布

在发布专用仓库中提交 `app/`、`tests/`、`Dockerfile`、`requirements.txt`、`requirements.lock`、`docker-compose.yml`、`.dockerignore`、`.gitignore`、`.env.example`、`README.md` 和 `.github/`。不要提交 `data/`、真实 `.env`、`dist/`、本地规划记录或原始资料；不要上传整个工作目录。

配置 GitHub remote 并将源码推送到 `main` 后，`.github/workflows/publish-image.yml` 自动执行空目录启动、登录和重建验证，再用 GitHub 提供的 `GITHUB_TOKEN` 发布双架构镜像，无需在仓库另存个人 PAT。也可在 Actions 手动运行，或推送 `v*` 版本标签。镜像名由仓库自动生成，例如 `raikkonien/Resume` 对应 `ghcr.io/raikkonien/resume`；`main` 生成 `latest` / `main` 标签，版本标签生成对应版本，所有构建另有 SHA 标签。

首次发布后到 GitHub 个人主页的 Packages → 对应镜像 → Package settings，将可见性设为 **Public**。完成无鉴权拉取验证后，使用者才能直接运行本文 GHCR 安装命令。首次上传仍需要有效的 GitHub 登录及可达的 GitHub API / Git 服务。

### 可选：本地直接推送


发布者需要 GitHub 仓库和可用的 GHCR 写入凭据（如有 `write:packages` 权限的 PAT），并确保构建输入没有私有数据。以下示例的 owner / repository 按实际小写名称替换，Token 通过输入读取，不写入命令历史：

```sh
GHCR_USER=your-github-user
IMAGE=ghcr.io/your-owner/your-repository
printf 'GHCR token: '
read -r -s GHCR_TOKEN
printf '\n'
printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
unset GHCR_TOKEN
docker buildx create --name resume-release --use
docker buildx inspect --bootstrap
docker buildx build --platform linux/amd64,linux/arm64 \
  --tag "$IMAGE:latest" --push .
```

上述凭据读取示例用于 Bash；已有 buildx builder 可直接选择使用，无需重复创建。也可增加实际版本标签后推送，以便固定版本。首次推送后在 GitHub Package 设置中确认可见性为 **Public**，再检查双架构 manifest、无鉴权拉取、空目录初始登录、健康状态及重建后的数据保留，完成后再宣布镜像可安装。自动发布流程见 `.github/workflows/`；实际触发条件以 workflow 文件为准。公开前请使用离线包，不要把私有 Package 地址当作可匿名安装的镜像。
