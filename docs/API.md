# VidLingua 任务创建 API

服务启动后，接口默认地址为 `http://127.0.0.1:8001`，交互式接口说明在 `/api/docs`。API 与网页共用任务数据库和处理队列。创建成功后会自动开始处理，不需要再调用“开始处理”。

## 统一创建入口

`POST /api/jobs/create`，请求头 `Content-Type: application/json`。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `source` | 字符串，必填 | `local`、`youtube` 或 `batch` |
| `path` | 字符串 | `local` 时为单个视频路径；`batch` 时为文件夹路径。路径必须位于**运行服务的电脑**上 |
| `url` | 字符串 | `youtube` 时必填，普通视频、Shorts、直播回放地址均可 |
| `title` | 字符串 | 可选；用于单个本地或 YouTube 任务，默认取文件名或视频标题 |
| `separate_vocals` | 布尔值 | 是否分离人声并保留背景音，默认 `true` |
| `dubbing_engine` | 字符串 | `doubao` 或 `azure`，默认 `doubao`；须先在网页设置中配置对应服务 |
| `publish_to_bilibili` | 布尔值 | 本地或 YouTube 任务成片完成后，按源标题和中文字幕由 DeepSeek 生成投稿信息并投递 B 站；YouTube 默认 `true`，本地默认 `false` |

单个本地视频支持 MP4、M4V、MOV、MKV、WebM、AVI、WMV、FLV、TS。非 MP4 会先转换为工作用 MP4，源文件不改动。文件夹批处理只扫描当前层，不扫描子文件夹；成片写入源目录的 `Translate` 文件夹。批处理任务不投稿 B 站。网页上传本地 MP4 时也可勾选自动投稿。

### 本地视频

```powershell
$body = @{
  source = "local"
  path = "D:\Videos\lesson.mp4"
  title = "课程第一讲"
  separate_vocals = $true
  dubbing_engine = "azure"
} | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8001/api/jobs/create" -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

### YouTube 视频

```powershell
$body = @{ source = "youtube"; url = "https://www.youtube.com/watch?v=VIDEO_ID" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8001/api/jobs/create" -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

YouTube 地址会规范化并按视频 ID 去重。若已有同一视频的任务，返回原任务及原有投稿选择，`already_exists` 为 `true`，不会再次处理。需要重新处理时调用重跑接口。自动投稿的结果和失败原因可在任务详情的 B 站投稿区查看。

### 文件夹批处理

```powershell
$body = @{ source = "batch"; path = "D:\Videos\Course"; separate_vocals = $true } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:8001/api/jobs/create" -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body))
```

成功返回 HTTP `202`。单视频响应包含 `id`、`status`、`stage`、`detail_url` 等任务字段；批处理响应的 `kind` 为 `batch`，还包含 `children`、`count`、`output_dir`。`detail_url` 可以拼接到服务地址后在浏览器打开，例如 `http://127.0.0.1:8001/#job/<id>`。创建请求只负责入队，后续处理在后台进行；`202` 不表示翻译或成片已经完成。

## 查询与重跑

| 用途 | 接口 |
| --- | --- |
| 任务列表 | `GET /api/jobs` |
| 单视频详情与进度 | `GET /api/jobs/{id}` |
| 批处理详情及子任务 | `GET /api/batches/{id}` |
| 任务日志 | `GET /api/jobs/{id}/log` |
| 重跑单视频 | `POST /api/jobs/{id}/rerun` |
| 重跑整个批处理 | `POST /api/batches/{id}/rerun` |
| 批量重跑（可混选单视频和批处理） | `POST /api/jobs/bulk/rerun`，JSON `{ "ids": ["..."] }` |
| 批量删除（可混选单视频和批处理） | `DELETE /api/jobs/bulk`，JSON `{ "ids": ["..."] }` |

批处理会查找与视频同名的 `.srt` 或 `.vtt` 英文字幕。有可用字幕时直接导入句段，跳过 WhisperX；字幕无效时在任务日志中记录原因并回退语音识别。

例如查询任务：

```powershell
Invoke-RestMethod "http://127.0.0.1:8001/api/jobs/<id>"
```

请求数据无效时通常返回 HTTP `400`（例如文件不存在、格式不支持或 URL 无效）；任务正在运行等状态冲突返回 `409`；JSON 字段类型错误返回 `422`。响应中的 `detail` 字段给出原因。

## 现有创建接口

网页仍使用原接口：`POST /api/jobs` 接受 `multipart/form-data` 上传单个 MP4，`POST /api/jobs/youtube` 接受 YouTube JSON，`POST /api/batches` 接受文件夹 JSON。前两个接口只创建任务，随后需调用 `POST /api/jobs/{id}/run`；批处理接口会直接排队。新脚本建议使用统一入口。

当前服务没有独立的 API 身份验证。保持 `--host 127.0.0.1`，只允许本机访问；传入本地路径等同于允许服务读取该路径。
