# 自动传单派发

在指定 QQ 群内自动发送「文字+图片」组合传单。**多用户全云端方案**：每个用户一个独立 QQ 账号 + 独立 NapCat 容器（`napcat-<用户名>`），各自监听群消息（计数）并负责发送传单（API 直发），全部运行在 Linux 云服务器上，无需本地电脑常驻。发送策略：**群内新消息达到阈值（如 30 条）且距上次发送超过间隔时间（如 30 分钟）时自动发送一次**，Web 管理台统一管理用户、群、发送内容（文字模板/图片）、群发送开关、发送记录与手动触发。

**空间监控（QQ 空间动态）**：每个用户可额外开启「空间监控」——定时轮询指定 QQ 用户的空间说说，关键词过滤后自动**转发（到独立目标列表）/点赞/评论（模板或 AI）**。Qzone 凭证直接从该用户自己的 NapCat 容器获取（`get_cookies`/`get_credentials`），无需额外扫码，**以用户本人 QQ 身份执行操作**。

## 部署配置

仓库只保存源码和配置模板，不包含可直接登录的账号、默认密码或实际服务器信息。
部署前设置自己的服务器地址、SMTP 配置、管理员密码和随机令牌；账号与群配置仅保存在服务器本地。

## 架构（多用户全云端）

```
┌─ Linux 云服务器 <SERVER_HOST> ─────────────────────────────────┐
│  users.json ──每 10s 对比──▶ users_watch_loop（动态启停引擎）    │
│                                                                │
│  每用户一个独立引擎 + 独立 NapCat 容器，互相隔离：              │
│   ┌─ napcat-admin 容器（:6099 WebUI）──反向WS──▶ OneBot :8082  │
│   │     （container_mgr 动态 docker run 创建）                  │
│   └─ napcat-<用户> 容器（:6099 WebUI）──反向WS──▶ OneBot :8082+idx│
│                                                                │
│  server.py（多用户核心）                                        │
│   · 每用户：事件进来 → 每群计数器 + 触发判断（含群开关）         │
│   · 触发后同一连接 call_action("send_group_msg") 直发           │
│   · 确认：API 返回 message_id 即成功；message_sent 事件软确认   │
│   · 每 3s dump users/<用户>/status.json（含最近 100 条发送记录） │
│   · 每 10s 热重载该用户 config.json；每 1s 轮询手动触发文件      │
│                                                                │
│  webapp.py（Web 管理台 :8081，多用户版）                        │
│   · 用户名+密码登录（users.json pbkdf2 哈希校验）               │
│   · admin：用户管理（创建/删除/重置密码/绑定 QQ/切换查看）       │
│   · 状态监控 / 群管理 / 内容与图片 / 发送记录 / 日志（按用户隔离）│
└────────────────────────────────────────────────────────────────┘
```

## 一、Web 管理台使用（日常操作）

浏览器打开 **http://<SERVER_HOST>:8081**，输入**用户名+密码**登录。

| 区块 | 功能 |
|---|---|
| 状态监控 | 各群计数/上次发送/开关/发送端在线状态（3 秒刷新），每行「手动触发」按钮 |
| 群管理 | 全局默认设置（阈值/间隔）；增删改群（群号、群名、阈值、间隔、**独立模板/图片**、**发送开关**）；自动检测到的群一键添加 |
| 内容与图片 | 编辑文字模板（`\n` 表示换行）；上传/删除/预览图片；设置默认图片（随机或指定） |
| **空间监控** | **QQ 空间动态监控：启用开关、监控列表（QQ 号）、关键词过滤（`-排除`/`+必须`）、动作（转发/点赞/评论）、评论模板或 AI、独立转发列表；状态与处理记录实时显示，支持「测试连接」** |
| 发送记录 | 最近 100 条发送历史（时间/群/成功失败/message_id/发送内容/错误信息） |
| 用户管理 | **仅管理员可见**：创建/删除用户、重置密码、NapCat 绑定流程、绑定状态 |
| 日志 | 当前用户服务器日志实时滚动查看（admin 查看自己时附加管理台日志） |

**多用户隔离**：普通用户登录后只能管理自己的群/内容/图片/日志；管理员在顶栏「查看：xxx」下拉切换目标用户，切换后所有管理操作作用于该用户。

**不同群发不同消息**：群管理 → 编辑群 → 「模板覆盖」与「指定图片」留空则跟随全局，填写则为该群专用。

**修改生效流程**：管理台保存 → 服务器约 10 秒内自动热加载（无需重启）。

### 用户注册流程（邮箱 + 验证码）

1. 管理员在「用户管理」→ 邀请码 → 生成（可设有效天数），发给目标用户
2. 用户打开管理台 → 登录页点「没有账号？注册」→ 填邮箱 + 邀请码 → 点「发送验证码」
3. 邮箱收到 6 位验证码（10 分钟有效，60 秒限流）→ 填码 + 设密码 → 注册成功（无需再激活）
4. 登录后进入**绑定引导页**扫码绑定自己的 QQ（见下），绑定完成后即可管理群

### 用户绑定流程（注册后自助，admin 可在「用户管理」代绑）

1. 注册登录后进入**绑定引导页**（或 admin 在「用户管理」点用户行「绑定」）
2. 页面直接展示 NapCat 登录二维码（**从容器读取，无需 WebUI**——NapCat 4.x WebUI 端口固定 6099，多容器端口冲突，扫码统一走二维码）
3. 用该用户的 QQ 号手机扫码登录（**须已在目标群**）
4. 自动检测登录 → 写入反向 WS 连接 → 重启容器 → 绑定完成（管理台显示已绑定 QQ 号）
5. 之后该用户的群消息事件自动进入其独立引擎；登录态保留在容器卷中，解绑后重新绑定**无需再次扫码**

## 二、服务器端运维

```bash
docker ps                                    # flyer-server + 各用户的 napcat-<用户名> 容器
docker logs flyer-server                     # 核心 + 管理台日志（同容器）
docker logs napcat-admin                     # 某用户 NapCat 日志（WebUI token / 登录状态）

# 重启 / 停止
cd ~/flyer/server && docker compose restart flyer-server
cd ~/flyer/server && docker compose stop flyer-server

# 代码更新后重建镜像并重启
cd ~/flyer/server && docker build -t flyer-image . && docker compose up -d --force-recreate flyer-server
```

**服务器端关键文件**（`~/flyer/server`，容器挂载，管理台可改大部分内容）：

| 文件/字段 | 说明 |
|---|---|
| `users.json` | 账号库：用户名 → 密码哈希（pbkdf2）/角色/端口/token/绑定状态。**含敏感信息，勿入库勿外传**；模板见 `users.json.example` |
| `users/<用户名>/config.json` | 用户业务配置：global（阈值/间隔）/groups/message（模板/图片） |
| `users/<用户名>/status.json` | 实时状态（server.py 每 3s dump，管理台读取） |
| `users/<用户名>/images/` | 该用户图片库（NapCat 容器挂载为 `/app/images`） |
| `config.json` | 系统配置：web（管理台端口）/logging/docker（NapCat 镜像、端口 base、服务器公网地址） |
| `logs/server_<用户名>_日期.log` | 每用户独立日志 |

**端口分配**：反向 WS 按创建顺序 `8082+idx`（admin=8082，出站连接无需安全组）。**NapCat 4.x 的 WebUI 端口固定 6099 且多容器冲突**，因此绑定扫码不依赖 WebUI——管理台直接从容器读取登录二维码展示（`get_qrcode_image`）。管理台应通过受控管理网络或 HTTPS 反向代理访问，限制 8081 的来源。

## 三、部署步骤（新服务器）

1. 上传 `server/` 目录到服务器 `~/flyer/server/`，`cd ~/flyer/server && docker build -t flyer-image .`（Dockerfile 已预装 docker CLI，用于动态创建 NapCat 容器）
2. 复制系统配置：`cp config.json.example config.json`，填写本机 SMTP 与服务器地址。准备账号库：`cp users.json.example users.json` 并按下方生成真实密码哈希与随机 token
3. `docker compose up -d`（仅 flyer-server；NapCat 容器由管理台绑定流程动态创建）
4. 浏览器打开 `http://服务器IP:8081`，用 admin 账号登录
5. 「用户管理」→ 点 admin 行「绑定」→ 在绑定引导页扫码登录发送账号（**须已在目标群里**）→ 自动完成反向 WS 配置
6. 管理台添加目标群（或修改 `users/<用户名>/config.json` 的 groups 段）

生成密码哈希与 token（服务器上任意 Python 环境）：

```bash
docker run --rm -it flyer-image python -c "
from werkzeug.security import generate_password_hash
import secrets
from getpass import getpass
print('password:', generate_password_hash(getpass('管理员密码: '), method='pbkdf2:sha256:600000'))
print('ws_access_token:', secrets.token_urlsafe(32))
print('webui_token:', secrets.token_urlsafe(16))
"
```

## 四、联调与验证

**手动触发**（命令行示例中的 `GROUP_ID` 必须先设置为获准操作的群号；管理台「状态监控」每行按钮，或命令行）：

```bash
cd ~/flyer/server && docker run --rm --network host -v /root/flyer/server:/app -w /app flyer-image python server.py --simulate admin "$GROUP_ID"
```

**发送确认链路**：`send_group_msg` API 返回 `message_id` 即确认成功（计数清零、记录到「发送记录」）；随后 message_sent 事件软确认（未收到仅告警，不误判失败）。**最终以群内实际收到为准**。

## 常见问题

| 问题 | 解决 |
|---|---|
| 管理台打不开 | 检查受控管理网络、反向代理与容器是否启动（`docker logs flyer-server`） |
| 登录提示用户名或密码错误 | 检查 users.json 中该用户密码哈希；密码由部署者在初始化时设置 |
| 状态页显示「NapCat 离线」 | 「用户管理」确认该用户已绑定；`docker logs napcat-<用户名>` 看反向 WS 连接 |
| 发不出（retcode 1200） | 图片文件在 napcat 容器内不存在——确认 `users/<用户名>/images/` 与容器挂载 `/app/images` 一致 |
| 图片没发出去 | 「内容与图片」确认图片已上传且在清单；图片路径为容器内 `/app/images/xxx.png` |
| 修改了配置不生效 | 服务器约 10 秒热加载；users.json（密码/token/端口）改动需重启容器 |
| 消息重复触发 | 确认「全局设置」的 count_own_messages 未误开；群开关是否误关 |
| 新建用户引擎没启动 | 等 10 秒（users_watch_loop 轮询）；`docker logs flyer-server` 看是否有启动报错 |
| 绑定后 WebUI 打不开 | NapCat 4.x WebUI 端口固定 6099（非配置的 6098），请通过受控管理网络或 SSH 隧道访问，不要向公网开放 WebUI |

## 风险提示 ⚠️

- 管理员密码由部署者自行设置，仓库不提供通用初始密码。
- **NapCat 属第三方协议端**，违反腾讯 QQ 用户协议，**存在账号被封禁/冻结的风险**——本方案中每个用户用自己的 QQ 号登录 NapCat，风险由该账号承担，请确认已接受
- 高频发送可能触发 QQ 风控/验证码——请控制发送频率（间隔 ≥ 1800 秒）
- 管理台应限制网络访问，**请勿泄露密码**；users.json 含密码哈希与 token，切勿提交到公开仓库
- 本工具仅限**合规的个人/学习场景**使用，风险自担

## 版本记录

- **v2.1（2026-08-21）**：集成 qqtranspond 的「空间监控」能力——每用户可选开启 QQ 空间动态监控（关键词过滤 → 转发/点赞/评论），Qzone 凭证经 NapCat `get_cookies`/`get_credentials` 自动获取（无需额外扫码），管理台新增「空间监控」标签页（配置 + 状态 + 测试连接）；新增 `server/qzone_client.py`、`server/keyword_filter.py`、`server/comment_gen.py`，依赖新增 `requests`
- **v2 多用户版（2026-08-12）**：每用户独立引擎/容器/端口/数据目录；webapp 多租户化（用户名+密码哈希登录、admin 用户管理与绑定流程）；flyer-server 挂载 docker.sock 动态创建容器
- **v1 单账号版（已归档）**：单账号全云端方案，napcat 静态容器 + 根目录配置；升级迁移时旧数据（`napcat/` 登录态、`images/` 图片、群配置）已迁入 `napcat-admin/` 与 `users/admin/`
